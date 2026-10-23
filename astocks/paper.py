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


def _fee(amount: float, side: str) -> float:
    fee = max(config.PAPER_MIN_FEE, amount * config.PAPER_FEE_BPS / 1e4)
    fee += amount * config.PAPER_TRANSFER_BPS / 1e4
    if side == "sell":
        fee += amount * config.PAPER_STAMP_BPS / 1e4
    return round(fee, 2)


def _qty(price: float) -> int:
    gross = config.PAPER_NOTIONAL / (price * (1 + (config.PAPER_FEE_BPS + config.PAPER_TRANSFER_BPS) / 1e4))
    return int(gross // 100) * 100


def _tranches(qty: int, n: int) -> list[int]:
    if n <= 0:
        return []
    base = qty // n
    out = [base] * n
    out[-1] = qty - base * (n - 1)
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



# --------------------------------------------------------------------------- 成交回放

def _replay(plan: dict, bars: dict[str, dict], cal: list[str], bench: dict[str, dict],
            cutoff: str, mins: dict[str, list[dict]] | None = None) -> dict | None:
    """把一个计划从建仓日起重放到 cutoff，返回完整台账状态（幂等）。"""
    idx = {d: i for i, d in enumerate(cal)}
    i0 = idx.get(plan["trade_date"])
    if i0 is None:
        return None
    code, name = plan.get("code") or "", plan.get("name")
    mins = mins or {}

    res: dict = {
        "status": "pending", "fill_date": None, "fill_price": None, "qty": 0,
        "shares_left": 0, "realized": 0.0, "last_price": None, "last_date": None,
        "ret_pct": None, "hold_days": None, "mfe": None, "mae": None,
        "exit_reason": None, "exit_date": None, "bench_ret": None, "excess": None,
        "path_src": None, "execs": [],
    }

    last_valid_i = min(i0 + plan["valid_days"] - 1, len(cal) - 1)
    fill: tuple[str, float, int] | None = None
    for i in range(i0, last_valid_i + 1):
        d = cal[i]
        if d > cutoff:
            break
        bar = bars.get(d)
        if not bar or not (bar.get("volume") or 0):
            continue  # 停牌/无成交，挂单继续有效
        op = float(bar["open"])
        hi, lo = float(bar["high"]), float(bar["low"])
        up, _dn = _limits(code, name, bars.get(cal[i - 1], {}).get("close") if i else None)
        if up and op >= up and lo >= up:
            continue  # 一字涨停买不到
        if plan["entry_mode"] == "open":
            fill = (d, _buy_px(op, op), i)
            break
        target = float(plan["entry_target"])
        if op <= target:
            fill = (d, _buy_px(op, target), i)
            break
        if lo <= target:
            # 分钟级：找出第一次触及买点的那根 bar，跳空则按该 bar 开盘价成交
            px = None
            mseq = mins.get(d) or []
            if not _minutes_ok(mseq):
                mseq = []
            for m in mseq:
                if float(m["low"]) <= target:
                    px = min(target, float(m["open"]))
                    break
            fill = (d, _buy_px(px if px is not None else target, target), i)
            break

    if not fill:
        window_end = i0 + plan["valid_days"] - 1
        if window_end <= len(cal) - 1 and cal[window_end] <= cutoff:
            res["status"] = "expired"
            res["exit_reason"] = "expired"
        return res

    fill_date, fill_price, fi = fill
    if plan["stop_price"] and fill_price <= plan["stop_price"]:
        # 跳空低开直接穿透破位线：买点前提已失效，不接飞刀
        res.update(status="expired", exit_reason="gap_below_stop", fill_date=fill_date)
        return res
    qty = _qty(fill_price)
    if qty < 100:
        res.update(status="expired", exit_reason="expired")
        return res

    buy_amt = round(fill_price * qty, 2)
    buy_fee = _fee(buy_amt, "buy")
    cost = buy_amt + buy_fee
    res["execs"].append({"exec_date": fill_date, "side": "buy", "price": fill_price,
                         "qty": qty, "amount": buy_amt, "fee": buy_fee, "reason": "entry"})

    tps = plan["tp_prices"]
    tranches = _tranches(qty, len(tps))
    tp_done = [False] * len(tps)
    shares = qty
    sells = 0.0
    mfe, mae = 0.0, 0.0
    last_price, last_date = fill_price, fill_date
    exit_reason, exit_date = None, None
    hold = 0
    path_src = None

    for i in range(fi, len(cal)):
        d = cal[i]
        if d > cutoff:
            break
        bar = bars.get(d)
        if not bar:
            continue
        hi, lo, cl, op = float(bar["high"]), float(bar["low"]), float(bar["close"]), float(bar["open"])
        mfe = max(mfe, hi / fill_price - 1)
        mae = min(mae, lo / fill_price - 1)
        last_price, last_date, hold = cl, d, i - fi
        if not (bar.get("volume") or 0):
            continue  # 停牌日不撮合，持仓顺延

        # T+1：建仓当日不能卖出
        if config.PAPER_ENFORCE_T1 and d == fill_date:
            continue
        if hold >= plan["holding_days"] and shares > 0:
            px = _sell_px(cl)
            amt = round(px * shares, 2)
            fee = _fee(amt, "sell")
            res["execs"].append({"exec_date": d, "side": "sell", "price": px, "qty": shares,
                                 "amount": amt, "fee": fee, "reason": "timeout"})
            sells += amt - fee
            shares = 0
            exit_reason, exit_date = "timeout", d
            break

        seq = mins.get(d) if config.PAPER_MINUTE_PATH else None
        if seq and not _minutes_ok(seq):
            seq = None  # 半截分钟数据不可信，退回日线口径
        if seq:
            path = [(float(m["open"]), float(m["high"]), float(m["low"]), float(m["close"]))
                    for m in seq]
            path_src = "minute"
        else:
            path = [(op, hi, lo, cl)]
            path_src = path_src or "daily"
        prev_close = bars.get(cal[i - 1], {}).get("close") if i else None
        _up, dn = _limits(code, name, prev_close)
        events, shares = _sell_events(path, plan, tps, tp_done, tranches, shares, dn)
        for reason, raw_px, q in events:
            px = _sell_px(raw_px)
            amt = round(px * q, 2)
            fee = _fee(amt, "sell")
            res["execs"].append({"exec_date": d, "side": "sell", "price": px, "qty": q,
                                 "amount": amt, "fee": fee, "reason": reason})
            sells += amt - fee
            exit_date = d
            if reason == "stop":
                exit_reason = "stop"
        if shares <= 0:
            exit_reason = exit_reason or "take_profit"
            break

    sold = qty - shares
    cost_sold = cost * sold / qty if qty else 0.0
    realized = round(sells - cost_sold, 2)
    res.update(
        status="closed" if shares == 0 else "open",
        fill_date=fill_date, fill_price=fill_price, qty=qty, shares_left=shares,
        realized=realized, last_price=last_price, last_date=last_date,
        mfe=round(mfe, 4), mae=round(mae, 4),
        hold_days=hold, exit_reason=exit_reason, exit_date=exit_date,
        path_src=path_src,
    )
    if shares == 0:
        res["ret_pct"] = round(realized / cost, 4) if cost else None
    else:
        res["ret_pct"] = round((sells + shares * last_price - cost) / cost, 4) if cost else None

    end = exit_date or last_date
    b0, b1 = bench.get(fill_date), bench.get(end) if end else None
    if b0 and b1 and b0.get("close"):
        res["bench_ret"] = round(float(b1["close"]) / float(b0["close"]) - 1, 4)
        if res["ret_pct"] is not None:
            res["excess"] = round(res["ret_pct"] - res["bench_ret"], 4)
    return res


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
    rebuild(conn, None, ("强推", "可关注"), log)
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


def create_orders(conn, trade_date: str, ratings: tuple[str, ...] = ("强推", "可关注"),
                  log: Logger = _noop) -> int:
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
        dup = _has_open_position(conn, p["code"], trade_date, p.get("rating"))
        if not dup:
            n = _cancel_pending(conn, p["code"], trade_date, p.get("rating"))
            if n:
                log(f"♻️  {plan['code']} {plan['name']} 撤销 {n} 张旧挂单，按新建议重新挂单")
        status = "skipped" if dup else "pending"
        conn.execute(
            """INSERT INTO paper_trade
               (pick_id, snapshot_id, trade_date, code, name, rating, total_score, entry_mode, entry_target,
                valid_days, stop_price, tp_prices, holding_days, status, shares_left, plan_json,
                exit_reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, ?, 0, ?, ?, ?)""",
            (p["id"], snap["id"] if snap else None, plan["trade_date"], plan["code"],
             plan.get("name") or p["name"], plan.get("rating") or p.get("rating"),
             plan.get("total_score") if plan.get("total_score") is not None else p.get("total_score"),
             plan["entry_mode"], plan["entry_target"], plan["valid_days"],
             plan["stop_price"], store.dump(plan["tp_prices"]), plan["holding_days"],
             status, store.dump(plan), "duplicate" if dup else None, store.now_iso()),
        )
        made += 1 if not dup else 0
        if dup:
            log(f"⏭  {plan['code']} {plan['name']} 已有持仓，本次不重复建仓")
        elif plan["notes"]:
            log(f"📝 {plan['code']} {plan['name']}：{'；'.join(plan['notes'])}")
    return made


def _sig(row_status, execs) -> tuple:
    return (
        row_status["status"], row_status["fill_date"], row_status["fill_price"],
        row_status["qty"], row_status["shares_left"], row_status["realized"],
        row_status["ret_pct"], row_status["exit_reason"], row_status["exit_date"],
        row_status["hold_days"],
        [(e["exec_date"], e["side"], e["price"], e["qty"], e["reason"]) for e in execs],
    )


def settle(conn, asof: str | None = None, log: Logger = _noop,
           cache: dict[tuple, dict[str, dict]] | None = None,
           bench: dict[str, dict] | None = None,
           offline: bool = False) -> dict:
    cal = bars.calendar(conn, log, offline=offline)
    if not cal:
        raise RuntimeError("交易日历为空（本地日线库为空，先跑 `astocks bars --force`）")
    cutoff = asof or _closed_cutoff(cal)
    if not cutoff:
        return {"cutoff": None, "updated": 0}

    bench = bench if bench is not None else bars.ensure(
        conn, config.BENCH_CODE, log=log, offline=offline)
    cache = cache if cache is not None else {}
    trades = store.rows(conn, "SELECT * FROM paper_trade WHERE trade_date<=? ORDER BY trade_date, id", (cutoff,))
    updated = 0
    for t in trades:
        if t["status"] == "skipped":
            continue
        code = t["code"]
        # 缓存必须按 (代码, 建仓日, cutoff) 分开：sync 逐日推进时若只按代码缓存，
        # 首日存下的日线会截断在首日，后续交易日的开高低永远看不到，买点永不触发。
        ckey = (code, t["trade_date"], cutoff)
        if ckey not in cache:
            cache[ckey] = bars.ensure(conn, code, start=t["trade_date"], end=cutoff,
                                      log=log, offline=offline)
        plan = json.loads(t["plan_json"])
        if "code" not in plan:
            plan["code"] = code
        if "name" not in plan:
            plan["name"] = t["name"]
        mkey = (code, "min")
        if mkey not in cache:
            cache[mkey] = bars.minutes_of(conn, code, start=t["trade_date"], end=cutoff)
        res = _replay(plan, cache[ckey], cal, bench, cutoff, mins=cache[mkey])
        if not res:
            continue
        old_execs = store.rows(conn, "SELECT * FROM paper_exec WHERE trade_id=? ORDER BY exec_date, id", (t["id"],))
        if _sig(t, old_execs) == _sig(res, res["execs"]):
            continue

        conn.execute("DELETE FROM paper_exec WHERE trade_id=?", (t["id"],))
        for e in res["execs"]:
            conn.execute(
                """INSERT INTO paper_exec (trade_id, exec_date, code, side, price, qty, amount, fee, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (t["id"], e["exec_date"], code, e["side"], e["price"], e["qty"],
                 e["amount"], e["fee"], e["reason"], store.now_iso()),
            )
        conn.execute(
            """UPDATE paper_trade SET status=?, fill_date=?, fill_price=?, qty=?, shares_left=?,
                   realized=?, last_price=?, last_date=?, ret_pct=?, hold_days=?, mfe=?, mae=?,
                   exit_reason=?, exit_date=?, bench_ret=?, excess=?, path_src=?, updated_at=?
               WHERE id=?""",
            (res["status"], res["fill_date"], res["fill_price"], res["qty"], res["shares_left"],
             res["realized"], res["last_price"], res["last_date"], res["ret_pct"], res["hold_days"],
             res["mfe"], res["mae"], res["exit_reason"], res["exit_date"], res["bench_ret"],
             res["excess"], res.get("path_src"), store.now_iso(), t["id"]),
        )
        updated += 1
    conn.commit()
    return {"cutoff": cutoff, "updated": updated}


def _closed_cutoff(cal: list[str]) -> str | None:
    """当日 15:00 前，最新一根 K 线是盘中快照，不能当作收盘数据。"""
    last = cal[-1]
    now = datetime.now()
    if last == now.strftime("%Y-%m-%d") and now.hour < config.PAPER_CLOSE_HOUR:
        return cal[-2] if len(cal) > 1 else None
    return last


def sync(conn, asof: str | None = None,
         ratings: tuple[str, ...] = ("强推", "可关注"), log: Logger = _noop,
         offline: bool = False) -> dict:
    """按交易日顺序推进：先结算已建仓头寸，再为当日推荐建仓（避免用未来信息去重）。"""
    cal = bars.calendar(conn, log, offline=offline)
    cutoff = asof or _closed_cutoff(cal) or cal[-1]
    phase = active_phase(conn)
    since = phase["start_date"] if phase else None
    days = [d["trade_date"] for d in store.rows(
        conn, "SELECT trade_date FROM trading_day ORDER BY trade_date")
        if not since or d["trade_date"] >= since]
    cache: dict[tuple, dict[str, dict]] = {}
    bench = bars.ensure(conn, config.BENCH_CODE, log=log, offline=offline)
    made = 0
    for d in days:
        settle(conn, asof=d if d <= cutoff else cutoff, log=log, cache=cache, bench=bench,
               offline=offline)
        made += create_orders(conn, d, ratings, log)
    conn.commit()
    res = settle(conn, cutoff, log, cache=cache, bench=bench, offline=offline)
    if made:
        log(f"🧾 新建虚拟头寸 {made} 笔")
    log(f"🔁 虚拟交易结算至 {res['cutoff']}：更新 {res['updated']} 笔")
    return {**res, "created": made}


def rebuild(conn, asof: str | None = None,
            ratings: tuple[str, ...] = ("强推", "可关注"), log: Logger = _noop,
            offline: bool = False) -> dict:
    conn.execute("DELETE FROM paper_exec")
    conn.execute("DELETE FROM paper_trade")
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
