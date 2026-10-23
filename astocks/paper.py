"""虚拟交易（纸面交易）：严格按推荐附带的 suggestion 买卖策略模拟成交与退出。

规则来源 —— 全部取自 pick.suggestion，不新增任何选股/择时参数：
  买点   primaryEntry（挂限价单等回踩），有效期 PAPER_ENTRY_VALID_DAYS 个交易日；
         买点 ≥ 开盘价时按开盘价成交（相当于开盘买）。
  止损   stopLoss；若止损位无效（缺失或 ≥ 买点），退化为 secondaryEntry（跌破次买点视为破位），
         两者都不可用时取消止损，仅按到期平仓 —— 修正过程写入 plan_json 的 notes 供审计。
  止盈   takeProfits 三档，按档位等分（1/3、1/3、余量）逐档减仓。
  到期   holdingDays 个交易日后剩余仓位按收盘价平仓。
  费用   佣金 万2.5（最低 5 元）+ 过户费 + 卖出印花税 万5。

撮合口径（2026-10-22 起）：
  · 日线粒度下同一天里止盈与止损谁先到无法判定。持仓日有 5 分钟线时按分钟路径逐根判定，
    没有则退化为「同日先止损后止盈」的保守口径，并在 path_src 标注 minute / daily。
  · 买入价受限价保护（不高于买点），卖出价按触发价再让 PAPER_SLIP_BPS 滑点。
  · A 股 T+1：建仓当日不允许卖出。
  · 一字跌停无法卖出、一字涨停无法买入；无成交量的停牌日整日跳过。
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta
from typing import Callable

from . import bars, config, store

Logger = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


# --------------------------------------------------------------------------- 计划

def build_plan(pick: dict) -> dict:
    """把 suggestion 转成可执行的交易计划，并对矛盾参数做显式修正。"""
    sug = pick.get("suggestion")
    if isinstance(sug, str):
        try:
            sug = json.loads(sug)
        except json.JSONDecodeError:
            sug = {}
    sug = sug or {}

    ref = pick.get("open_price") or pick.get("ref_price") or 0.0
    entry = sug.get("primaryEntry")
    notes: list[str] = []
    mode = "limit"

    if not isinstance(entry, (int, float)) or entry <= 0:
        entry, mode = ref, "open"
        notes.append(f"无有效买点，按开盘价 {ref:.2f} 成交")
    elif entry >= ref > 0:
        notes.append(f"买点 {entry:.2f} ≥ 开盘 {ref:.2f}，按开盘价成交")
        entry, mode = ref, "open"

    stop = sug.get("stopLoss")
    sec = sug.get("secondaryEntry")
    if not isinstance(stop, (int, float)) or stop <= 0 or stop >= entry:
        if isinstance(sec, (int, float)) and 0 < sec < entry:
            notes.append(f"止损位 {stop} 无效（≥买点），改用次买点 {sec:.2f} 作为破位线")
            stop = float(sec)
        else:
            notes.append(f"止损位 {stop} 无效且无次买点，取消止损")
            stop = None

    tps: list[float] = []
    for t in sug.get("takeProfits") or []:
        p = t.get("price") if isinstance(t, dict) else None
        if isinstance(p, (int, float)) and p > entry:
            tps.append(round(float(p), 3))
    tps = sorted({p for p in tps})
    if not tps:
        notes.append("无有效止盈档（均低于买点），仅按到期平仓")

    hold = sug.get("holdingDays")
    hold = int(hold) if isinstance(hold, (int, float)) and hold > 0 else 20

    return {
        "trade_date": pick["trade_date"],
        "code": pick["code"],
        "name": pick["name"],
        "schedule": [{"effective": pick["trade_date"], "stop": round(float(stop), 3) if stop else None,
                      "tps": tps, "hold": hold}],
        "rating": pick.get("rating"),
        "total_score": pick.get("total_score"),
        "entry_mode": mode,
        "entry_target": round(float(entry), 3),
        "valid_days": config.PAPER_ENTRY_VALID_DAYS,
        "stop_price": round(float(stop), 3) if stop else None,
        "tp_prices": tps,
        "holding_days": hold,
        "notes": notes,
        "suggestion": sug,
    }


def _ensure_schedule(plan: dict) -> dict:
    """老快照里没有 schedule，补一条基线版本，保证修订链完整可回溯。"""
    if not plan.get("schedule"):
        plan["schedule"] = [{
            "effective": plan["trade_date"], "stop": plan.get("stop_price"),
            "tps": list(plan.get("tp_prices") or []), "hold": plan.get("holding_days") or 20,
        }]
    return plan


def _fee(amount: float, side: str) -> float:
    fee = max(config.PAPER_MIN_FEE, amount * config.PAPER_FEE_BPS / 1e4)
    fee += amount * config.PAPER_TRANSFER_BPS / 1e4
    if side == "sell":
        fee += amount * config.PAPER_STAMP_BPS / 1e4
    return round(fee, 2)


def _lots_for(price: float, capital: float) -> int:
    """手数 = 目标金额 / 每手金额，四舍五入到整手（至少 1 手）。

    以手为下单单位（A 股只能整手买），但手数按金额反推，
    使每个强推标的的投入金额大致相同 —— 单价 7 元买 14 手、34 元买 3 手，
    两者金额都在 1 万元附近，涨跌 1% 对账户的贡献一致。
    """
    if price <= 0:
        return 0
    target = capital * getattr(config, "PAPER_TARGET_PCT", 0.10)
    return max(int(round(target / (price * 100))), 1)


def _qty(price: float, cash: float, capital: float | None = None) -> int:
    """单笔建仓股数。

    `PAPER_SIZING=lots`（默认）：先按固定手数下单，股数统一、看盈亏直观；
    超过单笔金额上限（`PAPER_MAX_POSITION_PCT`）或现金不够就逐手递减，
    连 1 手都买不起则返回 0（放弃建仓）。
    `PAPER_SIZING=amount`：按金额等权（单笔预算 = 总资金 × `PAPER_POSITION_PCT`）。
    """
    cap_total = float(capital if capital is not None else config.PAPER_CAPITAL)
    unit = price * (1 + (config.PAPER_FEE_BPS + config.PAPER_TRANSFER_BPS) / 1e4)
    if unit <= 0:
        return 0
    cash = cash * 0.999                      # 预留最低佣金
    if getattr(config, "PAPER_SIZING", "lots") == "amount":
        budget = min(cap_total * config.PAPER_POSITION_PCT, cash)
        return max(int(budget // unit // 100) * 100, 0)
    limit = cap_total * getattr(config, "PAPER_MAX_POSITION_PCT", 0.15)
    lots = _lots_for(price, cap_total)
    while lots >= 1:
        qty = lots * 100
        if qty * unit <= min(limit, cash):
            return qty
        lots -= 1
    return 0


def _budget() -> float:
    """展示用：单笔名义预算。"""
    if getattr(config, "PAPER_SIZING", "lots") == "amount":
        return config.PAPER_CAPITAL * config.PAPER_POSITION_PCT
    return config.PAPER_CAPITAL * getattr(config, "PAPER_TARGET_PCT", 0.10)


def _tranches(qty: int, n: int) -> list[int]:
    """三档等分，且每档必须是整手（100 股）；余数并入最后一档。

    A 股卖出同样是 100 股整数倍（清仓可含零股），按 qty//n 直接分会出现
    166 股这种挂不出去的委托。
    """
    if n <= 0 or qty <= 0:
        return []
    base = (qty // 100 // n) * 100
    if base <= 0:
        return [qty]
    out = [base] * (n - 1)
    out.append(qty - base * (n - 1))
    return [q for q in out if q > 0]


# --------------------------------------------------------------------------- 撮合约束

def _tick(px: float, up: bool) -> float:
    t = config.PAPER_TICK
    n = round(px / t, 6)
    return round((math.ceil(n) if up else math.floor(n)) * t, 2)


def _buy_px(raw: float, cap: float) -> float:
    """买入：滑点向上让价，但限价单不得高于买点。"""
    return min(cap, _tick(raw * (1 + config.PAPER_SLIP_BPS / 1e4), up=True))


def _sell_px(raw: float) -> float:
    """卖出：按触发价再让滑点（下跌途中卖出拿不到理想价）。"""
    return _tick(raw * (1 - config.PAPER_SLIP_BPS / 1e4), up=False)


def _band(code: str, name: str | None) -> float:
    if name and "ST" in name.upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("43", "83", "87", "92")):
        return 0.30
    return 0.10


def _limits(code: str, name: str | None, pre_close: float | None) -> tuple[float | None, float | None]:
    if not pre_close:
        return None, None
    b = _band(code, name)
    return round(pre_close * (1 + b), 2), round(pre_close * (1 - b), 2)


def _minutes_ok(seq: list[dict]) -> bool:
    """只有覆盖整段交易时段的分钟线才可用于判定先后，半截数据比日线更危险。"""
    if not seq or len(seq) < 40:
        return False
    first, last = str(seq[0].get("ts"))[11:16], str(seq[-1].get("ts"))[11:16]
    return first <= "09:40" and last >= "14:55"


def _sell_events(path: list[tuple], plan: dict, tps: list[float], tp_done: list[bool],
                 tranches: list[int], shares: int, dn_limit: float | None) -> tuple[list[tuple], int]:
    """沿时间序列走一遍，返回当日卖出事件 [(reason, price, qty)] 与剩余股数。

    path 元素为 (open, high, low, close)，按时间升序；日线退化时只有一根。
    同一根 bar 内止损与止盈都满足时仍先判止损（保守），分钟粒度已把误差压到 5 分钟内。
    """
    out: list[tuple] = []
    left = shares
    for o, h, l, _c in path:
        if left <= 0:
            break
        # 一字跌停：全天封死，卖不出去
        if dn_limit and h <= dn_limit and l <= dn_limit:
            continue
        stop = plan["stop_price"]
        if stop and l <= stop:
            out.append(("stop", o if o <= stop else stop, left))
            left = 0
            break
        for k, tp in enumerate(tps):
            if tp_done[k] or left <= 0:
                continue
            if h >= tp:
                q = min(tranches[k], left)
                out.append((f"tp{k + 1}", max(tp, o), q))
                left -= q
                tp_done[k] = True
    return out, left



# --------------------------------------------------------------------------- 组合模拟

class _Sim:
    """按交易日推进的资金账户模拟：先处理当日卖出回款，再按现金与持仓槽位建仓。

    与原「逐笔独立回放」相比，唯一的差别是仓位受总资金约束——每笔预算 = 总资金/持仓上限，
    现金不够时能买多少买多少，连一手都不够就放弃（cash_short）。卖点判定逻辑完全沿用。
    """

    def __init__(self, conn, cal, cutoff, bench, *, capital, max_pos, budget,
                 offline=False, log=_noop):
        self.conn = conn
        self.cal = cal
        self.idx = {d: i for i, d in enumerate(cal)}
        self.cutoff = cutoff
        self.bench = bench or {}
        self.capital = float(capital)
        self.cash = float(capital)
        self.max_pos = int(max_pos)
        self.budget = float(budget)
        self.offline = offline
        self.log = log
        self._bars: dict[str, dict[str, dict]] = {}
        self._mins: dict[str, dict[str, list[dict]]] = {}
        self.orders: dict[int, dict] = {}
        self.days: list[dict] = []

    # ---------------------------------------------------------------- 数据
    def _kline(self, code: str, start: str) -> dict[str, dict]:
        if code not in self._bars:
            self._bars[code] = bars.ensure(self.conn, code, start=start, end=self.cutoff,
                                           log=self.log, offline=self.offline)
        return self._bars[code]

    def _minute(self, code: str, start: str) -> dict[str, list[dict]]:
        if code not in self._mins:
            self._mins[code] = bars.minutes_of(self.conn, code, start=start, end=self.cutoff)
        return self._mins[code]

    # ---------------------------------------------------------------- 账户
    def open_positions(self) -> list[dict]:
        return [s for s in self.orders.values() if s["status"] == "open" and s["shares_left"] > 0]

    def open_value(self) -> float:
        return sum((s["shares_left"] * (s["last_price"] or s["fill_price"] or 0))
                   for s in self.open_positions())

    def open_cost(self) -> float:
        out = 0.0
        for s in self.open_positions():
            out += s["cost"] * s["shares_left"] / s["qty"] if s["qty"] else 0.0
        return out

    def equity(self) -> float:
        return self.cash + self.open_value()

    def has_open(self, code: str, rating: str | None) -> bool:
        return any(s["code"] == code and s["rating"] == rating
                   for s in self.open_positions())

    def revise(self, code: str, rating: str | None, plan: dict, day: str) -> int | None:
        """持仓期间来了同标的的新强推：把止损/止盈/持有期改成新版本，从当日生效。

        买点不参与修订——已经建仓的票不因新买点而加仓，避免同一标的堆仓。
        """
        for s in self.open_positions():
            if s["code"] != code or s["rating"] != rating:
                continue
            sch = [x for x in (s["plan"].get("schedule") or []) if x["effective"] != day]
            sch.append({"effective": day, "stop": plan.get("stop_price"),
                        "tps": list(plan.get("tp_prices") or []),
                        "hold": plan.get("holding_days")})
            sch.sort(key=lambda x: x["effective"])
            s["plan"]["schedule"] = sch
            return s["id"]
        return None

    def plan_of(self, tid: int) -> dict | None:
        st = self.orders.get(tid)
        return st["plan"] if st else None

    def _apply_schedule(self, s: dict, day: str) -> None:
        """按 schedule 取当日应生效的那一版参数（重放与增量都走这里，保证可复现）。"""
        sch = s["plan"].get("schedule") or []
        cur = None
        for x in sch:
            if x["effective"] <= day:
                cur = x
        if cur is None:
            return
        p = s["plan"]
        p["stop_price"] = cur.get("stop")
        if cur.get("hold"):
            p["holding_days"] = cur["hold"]
        new_tps = list(cur.get("tps") or [])
        if new_tps != list(p.get("tp_prices") or []):
            p["tp_prices"] = new_tps
            s["tp_done"] = [False] * len(new_tps)

    def cancel_pending(self, code: str, rating: str | None, before: str) -> int:
        n = 0
        for s in self.orders.values():
            if (s["status"] == "pending" and s["code"] == code
                    and s["rating"] == rating and s["plan"]["trade_date"] < before):
                s["status"], s["exit_reason"] = "expired", "superseded"
                n += 1
        return n

    # ---------------------------------------------------------------- 挂单
    def add(self, tid: int, row: dict, plan: dict) -> None:
        self.orders[tid] = {
            "id": tid, "code": plan.get("code") or row["code"],
            "name": plan.get("name") or row.get("name"), "rating": row.get("rating"),
            "plan": plan, "status": "pending", "fill_date": None, "fill_price": None,
            "qty": 0, "shares_left": 0, "cost": 0.0, "proceeds": 0.0, "realized": 0.0,
            "last_price": None, "last_date": None, "mfe": 0.0, "mae": 0.0, "hold": 0,
            "exit_reason": None, "exit_date": None, "path_src": None, "execs": [],
            "tp_done": [False] * len(plan.get("tp_prices") or []),
        }

    # ---------------------------------------------------------------- 推进
    def step(self, day: str) -> None:
        if day > self.cutoff:
            return
        self._exits(day)
        self._entries(day)
        self._mark_new(day)
        self._expire(day)
        self._record(day)

    def _mark_new(self, day: str) -> None:
        """当日新建仓按当日收盘价估值。

        _entries 在建仓时只能把 last_price 设成成交价；若不在这里补一次收盘价，
        当日新仓会按成本计价，账户净值少算当天的浮动。
        """
        for s in self.open_positions():
            if s["fill_date"] != day:
                continue
            bar = self._kline(s["code"], s["plan"]["trade_date"]).get(day)
            if not bar:
                continue
            hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            s["last_price"], s["last_date"] = cl, day
            s["mfe"] = max(s["mfe"], hi / s["fill_price"] - 1)
            s["mae"] = min(s["mae"], lo / s["fill_price"] - 1)

    def _record(self, day: str) -> None:
        """逐日账户快照：收盘后「10 万本金还剩多少」。"""
        eq = self.equity()
        prev = self.days[-1]["equity"] if self.days else self.capital
        self.days.append({
            "trade_date": day,
            "cash": round(self.cash, 2),
            "open_cost": round(self.open_cost(), 2),
            "open_value": round(self.open_value(), 2),
            "equity": round(eq, 2),
            "unrealized": round(self.open_value() - self.open_cost(), 2),
            "realized": round(self.cash - self.capital + self.open_cost(), 2),
            "positions": len(self.open_positions()),
            "pnl_d0": round(eq - prev, 2),
            "ret_d0": ((eq / prev - 1) if prev else 0.0),
            "cum_ret": ((eq / self.capital - 1) if self.capital else 0.0),
        })

    def _exits(self, day: str) -> None:
        for s in list(self.orders.values()):
            if s["status"] != "open" or s["shares_left"] <= 0:
                continue
            code, plan = s["code"], s["plan"]
            self._apply_schedule(s, day)
            bar = self._kline(code, plan["trade_date"]).get(day)
            if not bar:
                continue
            op, hi, lo, cl = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            s["mfe"] = max(s["mfe"], hi / s["fill_price"] - 1)
            s["mae"] = min(s["mae"], lo / s["fill_price"] - 1)
            s["last_price"], s["last_date"] = cl, day
            s["hold"] = self.idx[day] - self.idx[s["fill_date"]]
            if not (bar.get("volume") or 0):
                continue                      # 停牌不撮合，持仓顺延
            if config.PAPER_ENFORCE_T1 and day == s["fill_date"]:
                continue                      # T+1：建仓当日不卖
            if s["hold"] >= plan["holding_days"]:
                self._sell(s, day, "timeout", cl, s["shares_left"])
                continue
            seq = self._minute(code, plan["trade_date"]).get(day) if config.PAPER_MINUTE_PATH else None
            if seq and not _minutes_ok(seq):
                seq = None
            if seq:
                path = [(float(m["open"]), float(m["high"]), float(m["low"]), float(m["close"]))
                        for m in seq]
                s["path_src"] = "minute"
            else:
                path = [(op, hi, lo, cl)]
                s["path_src"] = s["path_src"] or "daily"
            i = self.idx[day]
            prev_close = self._kline(code, plan["trade_date"]).get(self.cal[i - 1], {}).get("close") if i else None
            _up, dn = _limits(code, s["name"], prev_close)
            tps = plan["tp_prices"]
            tranches = _tranches(s["shares_left"], len(tps))
            events, left = _sell_events(path, plan, tps, s["tp_done"], tranches,
                                        s["shares_left"], dn)
            for reason, raw_px, q in events:
                self._sell(s, day, reason, raw_px, q)
            s["shares_left"] = left
            if left <= 0:
                s["exit_reason"] = s["exit_reason"] or "take_profit"

    def _sell(self, s: dict, day: str, reason: str, raw_px: float, q: int) -> None:
        if q <= 0:
            return
        px = _sell_px(raw_px)
        amt = round(px * q, 2)
        fee = _fee(amt, "sell")
        s["execs"].append({"exec_date": day, "side": "sell", "price": px, "qty": q,
                           "amount": amt, "fee": fee, "reason": reason})
        s["proceeds"] += amt - fee
        s["cash_out"] = s.get("cash_out", 0.0) + amt - fee
        self.cash += amt - fee
        s["shares_left"] -= q
        s["exit_date"] = day
        if reason == "stop":
            s["exit_reason"] = "stop"
        if s["shares_left"] <= 0:
            s["status"] = "closed"
            s["exit_reason"] = s["exit_reason"] or reason

    def _entries(self, day: str) -> None:
        for s in sorted(self.orders.values(), key=lambda x: x["id"]):
            if s["status"] != "pending":
                continue
            plan = s["plan"]
            i0 = self.idx.get(plan["trade_date"])
            if i0 is None or day not in self.idx:
                continue
            i = self.idx[day]
            if i < i0 or i > i0 + plan["valid_days"] - 1:
                continue
            kbars = self._kline(s["code"], plan["trade_date"])
            bar = kbars.get(day)
            if not bar or not (bar.get("volume") or 0):
                continue                      # 停牌/无成交，挂单继续有效
            op = float(bar["open"])
            lo = float(bar["low"])
            prev_close = kbars.get(self.cal[i - 1], {}).get("close") if i else None
            up, _dn = _limits(s["code"], s["name"], prev_close)
            if up and op >= up and lo >= up:
                continue                      # 一字涨停买不到
            target = float(plan["entry_target"])
            if plan["entry_mode"] == "open":
                price = _buy_px(op, op)
            elif op <= target:
                price = _buy_px(op, target)
            elif lo <= target:
                px = None
                mseq = self._minute(s["code"], plan["trade_date"]).get(day) or []
                if _minutes_ok(mseq):
                    for m in mseq:
                        if float(m["low"]) <= target:
                            px = min(target, float(m["open"]))
                            break
                price = _buy_px(px if px is not None else target, target)
            else:
                continue
            self._open(s, day, price)

    def _open(self, s: dict, day: str, price: float) -> None:
        plan = s["plan"]
        # 同标的同评级已持仓则不再建仓：建单时查过一遍，但旧挂单可能和新挂单同一天才成交
        if any(o["code"] == s["code"] and o["rating"] == s["rating"]
               for o in self.open_positions()):
            s["status"], s["exit_reason"] = "expired", "duplicate"
            return
        if plan.get("stop_price") and price <= plan["stop_price"]:
            s["status"], s["exit_reason"], s["fill_date"] = "expired", "gap_below_stop", day
            return
        if len(self.open_positions()) >= self.max_pos:
            s["status"], s["exit_reason"] = "expired", "no_slot"
            return
        qty = _qty(price, self.cash, self.capital)
        if qty < 100:
            s["status"], s["exit_reason"] = "expired", "cash_short"
            return
        amt = round(price * qty, 2)
        fee = _fee(amt, "buy")
        cost = amt + fee
        self.cash -= cost
        s.update(status="open", fill_date=day, fill_price=price, qty=qty,
                 shares_left=qty, cost=cost, last_price=price, last_date=day)
        s["execs"].append({"exec_date": day, "side": "buy", "price": price, "qty": qty,
                           "amount": amt, "fee": fee, "reason": "entry"})

    def _expire(self, day: str) -> None:
        for s in self.orders.values():
            if s["status"] != "pending":
                continue
            plan = s["plan"]
            i0 = self.idx.get(plan["trade_date"])
            if i0 is None:
                continue
            end_i = i0 + plan["valid_days"] - 1
            if end_i >= len(self.cal):
                continue                      # 窗口还没走完
            if day >= self.cal[end_i]:
                s["status"], s["exit_reason"] = "expired", "expired"

    # ---------------------------------------------------------------- 结果
    def result(self, s: dict) -> dict:
        qty, left = s["qty"], s["shares_left"]
        cost = s["cost"]
        sold = qty - left
        cost_sold = cost * sold / qty if qty else 0.0
        realized = round(s["proceeds"] - cost_sold, 2)
        out = {
            "status": s["status"], "fill_date": s["fill_date"], "fill_price": s["fill_price"],
            "qty": qty, "shares_left": left, "realized": realized,
            "last_price": s["last_price"], "last_date": s["last_date"],
            "hold_days": s["hold"], "mfe": round(s["mfe"], 4), "mae": round(s["mae"], 4),
            "exit_reason": s["exit_reason"], "exit_date": s["exit_date"],
            "path_src": s["path_src"], "execs": s["execs"], "ret_pct": None,
            "bench_ret": None, "excess": None, "notional": None,
        }
        if not qty:
            return out
        out["notional"] = round(s["fill_price"] * qty, 2)
        if left == 0:
            out["ret_pct"] = round(realized / cost, 4) if cost else None
        elif s["last_price"]:
            out["ret_pct"] = round((s["proceeds"] + left * s["last_price"] - cost) / cost, 4) if cost else None
        end = s["exit_date"] or s["last_date"]
        b0 = self.bench.get(s["fill_date"]) if s["fill_date"] else None
        b1 = self.bench.get(end) if end else None
        if b0 and b1 and b0.get("close"):
            out["bench_ret"] = round(float(b1["close"]) / float(b0["close"]) - 1, 4)
            if out["ret_pct"] is not None:
                out["excess"] = round(out["ret_pct"] - out["bench_ret"], 4)
        return out


# --------------------------------------------------------------------------- 跟踪期

def active_phase(conn) -> dict | None:
    return store.one(conn, "SELECT * FROM paper_phase ORDER BY id DESC LIMIT 1")


def start_phase(conn, start: str | None = None, end: str | None = None,
                name: str | None = None, scope: str = "强推", log: Logger = _noop) -> dict:
    """开启一期模拟跟踪：记录期初基准，并清空重建该期账本。"""
    cal = bars.calendar(conn, log)
    start = start or datetime.now().strftime("%Y-%m-%d")
    if not end:
        end = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")

    cutoff0 = _closed_cutoff(cal) or ""
    if start in cal and start <= cutoff0:
        base_date = start
    else:
        prior = [d for d in cal if d < start]
        base_date = prior[-1] if prior else None
    base_close = None
    if base_date:
        bench = bars.ensure(conn, config.BENCH_CODE, log=log)
        base_close = float(bench[base_date]["close"]) if base_date in bench else None

    conn.execute("DELETE FROM paper_phase WHERE start_date=?", (start,))
    conn.execute(
        """INSERT INTO paper_phase (name, scope, start_date, end_date, bench_code, bench_base,
               bench_date, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (name or f"第{_phase_count(conn) + 1}期", scope, start, end, config.BENCH_CODE,
         base_close, base_date, f"模拟跟踪 {start} → {end}", store.now_iso()),
    )
    conn.commit()
    log(f"🚩 开启{name or '新一期'}模拟跟踪：{start} → {end}（期初基准 "
        f"{config.BENCH_NAME} {base_close:.2f} @{base_date}）" if base_close else
        f"🚩 开启模拟跟踪：{start} → {end}（基准数据缺失）")
    rebuild(conn, None, config.PAPER_SCOPE, log)
    return active_phase(conn)


def _phase_count(conn) -> int:
    r = store.one(conn, "SELECT COUNT(*) AS n FROM paper_phase")
    return int(r["n"]) if r else 0


def _business_days(start: str, end: str) -> list[str]:
    """未来交易日历拿不到，用工作日近似（该口径会略高估，遇法定节假日需手工修正）。"""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def phase_status(conn, asof: str | None = None) -> dict | None:
    ph = active_phase(conn)
    if not ph:
        return None
    cal = bars.calendar(conn)
    cutoff = asof or _closed_cutoff(cal) or cal[-1]
    window = _business_days(ph["start_date"], ph["end_date"])
    done = [d for d in window if d <= cutoff]
    sm = summary(conn, "强推")
    verdict, reason = _verdict(sm["closed"], sm["win_rate"], sm["avg_ret"])
    return {
        **ph,
        "total_days": len(window),
        "elapsed": len(done),
        "remaining": max(len(window) - len(done), 0),
        "progress": (len(done) / len(window)) if window else None,
        "cutoff": cutoff,
        "calendar_note": "按工作日估算",
        "closed": sm["closed"],
        "open": sm["open"],
        "pending": sm["pending"],
        "orders": sm["orders"],
        "total_pnl": sm["total_pnl"],
        "win_rate": sm["win_rate"],
        "avg_ret": sm["avg_ret"],
        "verdict": verdict,
        "verdict_reason": reason,
    }


# --------------------------------------------------------------------------- 建仓 / 同步

def _has_open_position(conn, code: str, day: str, rating: str | None) -> bool:
    """按评级分账：强推账本与可关注账本互不影响，避免一个池子占住另一个池子的仓位。

    只有真正成交的 open 头寸才算占仓；未成交的挂单不占仓——同一只票的新建议应当
    撤掉旧挂单后重新挂，而不是被一条更深的旧限价单长期压住。
    """
    return bool(store.one(
        conn,
        """SELECT 1 FROM paper_trade
            WHERE code=? AND trade_date<=? AND rating IS ? AND status='open' LIMIT 1""",
        (code, day, rating),
    ))


def _cancel_pending(conn, code: str, day: str, rating: str | None) -> int:
    """撤掉同标的同评级的历史挂单（最新建议优先）。"""
    cur = conn.execute(
        """UPDATE paper_trade SET status='expired', exit_reason='superseded', updated_at=?
            WHERE code=? AND trade_date<? AND rating IS ? AND status='pending'""",
        (store.now_iso(), code, day, rating),
    )
    return cur.rowcount or 0


def _revise_row(conn, tid: int, plan: dict) -> None:
    """把持仓的参数修订写回账本（止损/止盈/持有期 + schedule 版本链）。"""
    row = store.one(conn, "SELECT plan_json FROM paper_trade WHERE id=?", (tid,))
    if not row:
        return
    base = json.loads(row["plan_json"] or "{}")
    sch = plan.get("schedule") or base.get("schedule") or []
    base["schedule"] = sch
    latest = sch[-1] if sch else {}
    stop = latest.get("stop", plan.get("stop_price"))
    tps = list(latest.get("tps") or [])
    hold = latest.get("hold") or plan.get("holding_days")
    base["stop_price"], base["tp_prices"], base["holding_days"] = stop, tps, hold
    conn.execute(
        """UPDATE paper_trade SET stop_price=?, tp_prices=?, holding_days=?, plan_json=?, updated_at=?
           WHERE id=?""",
        (stop, store.dump(tps), hold, store.dump(base), store.now_iso(), tid),
    )


def create_orders(conn, trade_date: str, ratings: tuple[str, ...] = config.PAPER_SCOPE,
                  log: Logger = _noop, sim: "_Sim | None" = None) -> int:
    """当日建单。

    三种情形（2026-10-23 规则）：
      1. 同标的**已持仓** → 不新建仓，把止损/止盈/持有期替换成今天这版建议（次日生效）；
      2. 同标的**有未成交挂单** → 旧单作废（`superseded`），按今天的建议重挂；
      3. 都没有 → 新挂单。
    原则：策略在「未达成实际成交」前可修正；成交后买点冻结，但止盈止损仍可随新建议调整。
    """
    picks = store.get_picks(conn, trade_date)
    made = 0
    for p in picks:
        if p.get("rating") not in ratings:
            continue
        if store.one(conn, "SELECT 1 FROM paper_trade WHERE pick_id=?", (p["id"],)):
            continue
        snap = store.latest_snapshot(conn, p["id"])
        plan = json.loads(snap["plan_json"]) if snap and snap.get("plan_json") else build_plan(p)
        plan.setdefault("trade_date", p["trade_date"])
        plan.setdefault("code", p["code"])
        plan.setdefault("valid_days", config.PAPER_ENTRY_VALID_DAYS)
        if not plan.get("entry_target"):
            continue
        _ensure_schedule(plan)
        rating = p.get("rating")

        # 情形 1：持仓中 → 只修订参数
        tid = sim.revise(p["code"], rating, plan, trade_date) if sim else None
        if tid:
            merged = sim.plan_of(tid) or plan
            _revise_row(conn, tid, merged)
            log(f"🔄 {plan['code']} {plan['name']} 持仓中，按新建议更新止损/止盈"
                f"（止损 {plan.get('stop_price')}，止盈 {plan.get('tp_prices')}）")
            continue

        # 情形 2：未成交挂单作废，按新建议重挂
        if sim:
            n = sim.cancel_pending(p["code"], rating, trade_date)
            n += _cancel_pending(conn, p["code"], trade_date, rating)
            if n:
                log(f"♻️  {plan['code']} {plan['name']} 作废 {n} 张未成交挂单，按新建议重挂")

        status = "pending"
        cur = conn.execute(
            """INSERT INTO paper_trade
               (pick_id, snapshot_id, trade_date, code, name, rating, total_score, entry_mode, entry_target,
                valid_days, stop_price, tp_prices, holding_days, status, shares_left, plan_json,
                exit_reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, ?, 0, ?, ?, ?)""",
            (p["id"], snap["id"] if snap else None, plan["trade_date"], plan["code"],
             plan.get("name") or p["name"], plan.get("rating") or rating,
             plan.get("total_score") if plan.get("total_score") is not None else p.get("total_score"),
             plan["entry_mode"], plan["entry_target"], plan["valid_days"],
             plan["stop_price"], store.dump(plan["tp_prices"]), plan["holding_days"],
             status, store.dump(plan), None, store.now_iso()),
        )
        if sim:
            sim.add(int(cur.lastrowid), p, plan)
        made += 1
        if plan["notes"]:
            log(f"📝 {plan['code']} {plan['name']}：{'；'.join(plan['notes'])}")
    return made


def _cost_of(row: dict) -> float:
    """该笔建仓的总成本（成交额 + 买入费用）。"""
    if not row.get("qty") or not row.get("fill_price"):
        return 0.0
    amt = round(float(row["fill_price"]) * int(row["qty"]), 2)
    return amt + _fee(amt, "buy")


def account(conn) -> dict:
    """虚拟盘资金账户（跨评级共用一笔钱，与 view 无关）。"""
    rows = ledger(conn, "all")
    cash = float(config.PAPER_CAPITAL)
    open_cost = open_value = realized = 0.0
    held = []
    for r in rows:
        cost = _cost_of(r)
        if not cost:
            continue
        realized += float(r["realized"] or 0.0)
        if r["status"] == "open" and r.get("shares_left"):
            left = int(r["shares_left"])
            rem = cost * left / int(r["qty"])
            open_cost += rem
            val = left * float(r["last_price"] or r["fill_price"])
            open_value += val
            held.append({**r, "cost": round(rem, 2), "value": round(val, 2),
                         "pnl": round(val - rem, 2),
                         "pnl_pct": (val / rem - 1) if rem else None})
    cash = config.PAPER_CAPITAL + realized - open_cost
    equity = cash + open_value
    held.sort(key=lambda x: -(x["pnl"] or 0))
    return {
        "capital": float(config.PAPER_CAPITAL),
        "cash": round(cash, 2),
        "open_cost": round(open_cost, 2),
        "open_value": round(open_value, 2),
        "realized": round(realized, 2),
        "unrealized": round(open_value - open_cost, 2),
        "equity": round(equity, 2),
        "total_return": (equity / config.PAPER_CAPITAL - 1) if config.PAPER_CAPITAL else None,
        "exposure": (open_value / equity) if equity else None,
        "positions": held,
        "max_positions": config.PAPER_MAX_POSITIONS,
        "budget": round(_budget(), 2),
    }


def _sig(row_status, execs) -> tuple:
    return (
        row_status["status"], row_status["fill_date"], row_status["fill_price"],
        row_status["qty"], row_status["shares_left"], row_status["realized"],
        row_status["ret_pct"], row_status["exit_reason"], row_status["exit_date"],
        row_status["hold_days"],
        [(e["exec_date"], e["side"], e["price"], e["qty"], e["reason"]) for e in execs],
    )


def _load_sim(conn, cal: list[str], cutoff: str, bench: dict, offline: bool, log: Logger) -> _Sim:
    """把已入库的挂单装进组合模拟器（rebuild 后为空，由 create_orders 陆续补进）。"""
    sim = _Sim(conn, cal, cutoff, bench, capital=config.PAPER_CAPITAL,
               max_pos=config.PAPER_MAX_POSITIONS, budget=_budget(),
               offline=offline, log=log)
    # superseded = 已被同日更晚的推荐撤掉的历史挂单，重放时不能再复活，否则增量 sync
    # 与 --rebuild 的结果会分叉（2026-10-23 实测：同一份日线，一个 3 仓一个 5 仓）。
    for t in store.rows(conn, "SELECT * FROM paper_trade WHERE trade_date<=? AND status!='skipped'"
                              " AND NOT (status='expired' AND exit_reason='superseded')"
                              " ORDER BY trade_date, id", (cutoff,)):
        plan = json.loads(t["plan_json"])
        plan.setdefault("code", t["code"])
        plan.setdefault("name", t["name"])
        plan.setdefault("valid_days", config.PAPER_ENTRY_VALID_DAYS)
        sim.add(t["id"], t, _ensure_schedule(plan))
    return sim


def _write_days(conn, sim: _Sim) -> int:
    """写入逐日账户快照（含无持仓的平淡日，便于画连续净值）。"""
    if not sim.days:
        return 0
    row = store.one(conn, "SELECT MIN(trade_date) AS d FROM paper_trade")
    first = row["d"] if row and row["d"] else None
    rows = [d for d in sim.days if not first or d["trade_date"] >= first]
    conn.execute("DELETE FROM paper_day")
    now = store.now_iso()
    conn.executemany(
        """INSERT INTO paper_day (trade_date, cash, open_cost, open_value, equity, unrealized,
               realized, positions, pnl_d0, ret_d0, cum_ret, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(d["trade_date"], d["cash"], d["open_cost"], d["open_value"], d["equity"],
          d["unrealized"], d["realized"], d["positions"], d["pnl_d0"], d["ret_d0"],
          d["cum_ret"], now) for d in rows],
    )
    conn.commit()
    return len(rows)


def equity_curve(conn) -> list[dict]:
    return store.rows(conn, "SELECT * FROM paper_day ORDER BY trade_date")


def _write_results(conn, sim: _Sim, cutoff: str) -> int:
    """把模拟结果落库；与旧值一致则跳过，保证幂等。"""
    _write_days(conn, sim)
    updated = 0
    for tid, st in sim.orders.items():
        res = sim.result(st)
        t = store.one(conn, "SELECT * FROM paper_trade WHERE id=?", (tid,))
        if not t:
            continue
        old_execs = store.rows(conn, "SELECT * FROM paper_exec WHERE trade_id=? ORDER BY exec_date, id", (tid,))
        if _sig(t, old_execs) == _sig(res, res["execs"]):
            continue
        conn.execute("DELETE FROM paper_exec WHERE trade_id=?", (tid,))
        for e in res["execs"]:
            conn.execute(
                """INSERT INTO paper_exec (trade_id, exec_date, code, side, price, qty, amount, fee, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (tid, e["exec_date"], st["code"], e["side"], e["price"], e["qty"],
                 e["amount"], e["fee"], e["reason"], store.now_iso()),
            )
        conn.execute(
            """UPDATE paper_trade SET status=?, fill_date=?, fill_price=?, qty=?, notional=?,
                   shares_left=?, realized=?, last_price=?, last_date=?, ret_pct=?, hold_days=?,
                   mfe=?, mae=?, exit_reason=?, exit_date=?, bench_ret=?, excess=?, path_src=?, updated_at=?
               WHERE id=?""",
            (res["status"], res["fill_date"], res["fill_price"], res["qty"], res["notional"],
             res["shares_left"], res["realized"], res["last_price"], res["last_date"],
             res["ret_pct"], res["hold_days"], res["mfe"], res["mae"], res["exit_reason"],
             res["exit_date"], res["bench_ret"], res["excess"], res["path_src"],
             store.now_iso(), tid),
        )
        updated += 1
    conn.commit()
    return updated


def settle(conn, asof: str | None = None, log: Logger = _noop,
           cache: dict | None = None, bench: dict[str, dict] | None = None,
           offline: bool = False) -> dict:
    """按资金账户重放到截止日（幂等：每次从头重放并重写成交）。"""
    cal = bars.calendar(conn, log, offline=offline)
    if not cal:
        raise RuntimeError("交易日历为空（本地日线库为空，先跑 `astocks bars --force`）")
    cutoff = asof or _closed_cutoff(cal)
    if not cutoff:
        return {"cutoff": None, "updated": 0}
    bench = bench if bench is not None else bars.ensure(
        conn, config.BENCH_CODE, log=log, offline=offline)
    sim = _load_sim(conn, cal, cutoff, bench, offline, log)
    for d in cal:
        if d > cutoff:
            break
        sim.step(d)
    updated = _write_results(conn, sim, cutoff)
    return {"cutoff": cutoff, "updated": updated,
            "cash": round(sim.cash, 2), "equity": round(sim.equity(), 2)}


def _closed_cutoff(cal: list[str]) -> str | None:
    """当日 15:00 前，最新一根 K 线是盘中快照，不能当作收盘数据。"""
    last = cal[-1]
    now = datetime.now()
    if last == now.strftime("%Y-%m-%d") and now.hour < config.PAPER_CLOSE_HOUR:
        return cal[-2] if len(cal) > 1 else None
    return last


def sync(conn, asof: str | None = None,
         ratings: tuple[str, ...] = config.PAPER_SCOPE, log: Logger = _noop,
         offline: bool = False) -> dict:
    """按交易日顺序推进：当日先建单、再撮合，保证去重用的是「昨日收盘前」的持仓。"""
    cal = bars.calendar(conn, log, offline=offline)
    cutoff = asof or _closed_cutoff(cal) or cal[-1]
    phase = active_phase(conn)
    since = phase["start_date"] if phase else None
    days = {d["trade_date"] for d in store.rows(
        conn, "SELECT trade_date FROM trading_day ORDER BY trade_date")
        if not since or d["trade_date"] >= since}
    bench = bars.ensure(conn, config.BENCH_CODE, log=log, offline=offline)
    sim = _load_sim(conn, cal, cutoff, bench, offline, log)
    made = 0
    for d in cal:
        if d > cutoff:
            break
        if d in days:
            made += create_orders(conn, d, ratings, log, sim)
        sim.step(d)
    conn.commit()
    updated = _write_results(conn, sim, cutoff)
    if made:
        log(f"🧾 新建虚拟头寸 {made} 笔")
    log(f"🔁 虚拟交易结算至 {cutoff}：更新 {updated} 笔｜现金 {sim.cash:,.0f}"
        f"｜市值 {sim.open_value():,.0f}｜总资产 {sim.equity():,.0f}")
    return {"cutoff": cutoff, "updated": updated, "created": made,
            "cash": round(sim.cash, 2), "equity": round(sim.equity(), 2)}


def rebuild(conn, asof: str | None = None,
            ratings: tuple[str, ...] = config.PAPER_SCOPE, log: Logger = _noop,
            offline: bool = False) -> dict:
    conn.execute("DELETE FROM paper_exec")
    conn.execute("DELETE FROM paper_trade")
    conn.execute("DELETE FROM paper_day")
    conn.commit()
    log("🧹 虚拟交易表已清空，按历史推荐重放")
    return sync(conn, asof, ratings, log, offline)


# --------------------------------------------------------------------------- 统计

def ledger(conn, view: str = "strong") -> list[dict]:
    where = "" if view == "all" else "WHERE t.rating='强推'"
    rows = store.rows(conn, f"SELECT t.* FROM paper_trade t {where} ORDER BY t.trade_date DESC, t.id DESC")
    for r in rows:
        r["tp_prices"] = json.loads(r["tp_prices"]) if r["tp_prices"] else []
        r["execs"] = store.rows(
            conn, "SELECT * FROM paper_exec WHERE trade_id=? ORDER BY exec_date, id", (r["id"],))
    return rows


def summary(conn, view: str = "strong") -> dict:
    rows = ledger(conn, view)
    closed = [r for r in rows if r["status"] == "closed" and r["ret_pct"] is not None]
    opened = [r for r in rows if r["status"] == "open"]
    pending = [r for r in rows if r["status"] == "pending"]
    expired = [r for r in rows if r["status"] == "expired"]
    skipped = [r for r in rows if r["status"] == "skipped"]

    rets = [r["ret_pct"] for r in closed]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pnl = [r["realized"] for r in closed]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for r in sorted(closed, key=lambda x: x["exit_date"] or ""):
        cum += r["realized"] or 0.0
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    excess = [r["excess"] for r in closed if r["excess"] is not None]
    holds = [r["hold_days"] for r in closed if r["hold_days"] is not None]
    mfes = [r["mfe"] for r in closed if r["mfe"] is not None]
    maes = [r["mae"] for r in closed if r["mae"] is not None]
    tp_touch = sum(1 for r in closed if any(
        (e["reason"] or "").startswith("tp") for e in r.get("execs") or []))
    reasons: dict[str, int] = {}
    for r in closed:
        reasons[r["exit_reason"] or "-"] = reasons.get(r["exit_reason"] or "-", 0) + 1

    by_rating: dict[str, dict] = {}
    for rating in sorted({r["rating"] or "未标注" for r in closed}):
        sub = [r["ret_pct"] for r in closed if (r["rating"] or "未标注") == rating]
        by_rating[rating] = {
            "n": len(sub),
            "win_rate": (sum(1 for x in sub if x > 0) / len(sub)) if sub else None,
            "avg": statistics.fmean(sub) if sub else None,
        }

    return {
        "view": view,
        "orders": len(rows),
        "filled": len(closed) + len(opened),
        "closed": len(closed),
        "open": len(opened),
        "pending": len(pending),
        "expired": len(expired),
        "skipped": len(skipped),
        "fill_rate": (len(closed) + len(opened)) / len(rows) if rows else None,
        "win_rate": len(wins) / len(rets) if rets else None,
        "avg_ret": statistics.fmean(rets) if rets else None,
        "med_ret": statistics.median(rets) if rets else None,
        "avg_win": statistics.fmean(wins) if wins else None,
        "avg_loss": statistics.fmean(losses) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        "expectancy": statistics.fmean(rets) if rets else None,
        "total_pnl": sum(pnl) if pnl else 0.0,
        "total_notional": sum(r["qty"] * r["fill_price"] for r in closed if r["qty"] and r["fill_price"]),
        "avg_hold": statistics.fmean(holds) if holds else None,
        "excess_n": len(excess),
        "avg_excess": statistics.fmean(excess) if excess else None,
        "excess_win": (sum(1 for e in excess if e > 0) / len(excess)) if excess else None,
        "max_dd_yuan": mdd,
        "avg_mfe": statistics.fmean(mfes) if mfes else None,
        "avg_mae": statistics.fmean(maes) if maes else None,
        "tp_touch_rate": (tp_touch / len(closed)) if closed else None,
        "stop_rate": (reasons.get("stop", 0) / len(closed)) if closed else None,
        "timeout_rate": (reasons.get("timeout", 0) / len(closed)) if closed else None,
        "exit_reasons": reasons,
        "by_rating": by_rating,
        "account": account(conn),
        "verdict": _verdict(len(closed), len(wins) / len(rets) if rets else None,
                            statistics.fmean(rets) if rets else None),
    }


def _verdict(n: int, wr: float | None, avg: float | None) -> tuple[str, str]:
    if n < config.PAPER_MIN_SAMPLE:
        return "样本不足", f"仅 {n} 笔平仓样本（阈值 {config.PAPER_MIN_SAMPLE}），暂不下结论"
    if wr is None or avg is None:
        return "数据不足", "缺少收益数据"
    if wr >= 0.55 and avg > 0:
        return "可沿用", f"{n} 笔平仓胜率 {wr:.0%}、期望 {avg * 100:+.2f}%/笔，执行纪律有效"
    if wr <= 0.40 and avg < 0:
        return "需修正", f"{n} 笔平仓胜率 {wr:.0%}、期望 {avg * 100:+.2f}%/笔，买卖点需重设"
    return "观察中", f"{n} 笔平仓胜率 {wr:.0%}、期望 {avg * 100:+.2f}%/笔，尚无显著优势"
