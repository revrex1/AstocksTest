"""逐笔审计：把每条强推按「声明规则」独立重算一遍，与虚拟盘账本对账。

为什么单独写一份而不复用 paper._Sim：
  _Sim 是带资金约束的组合模拟（多笔互相影响现金与槽位），出问题时很难判断
  是规则错了还是仓位错了。这里做**单笔、无资金约束**的独立判定，只回答四个问题：
    1. 这笔按规则能不能成交？哪天、什么价？
    2. 成交后有没有减仓（止盈档）？
    3. 有没有触及止损？哪天、什么价？
    4. 到今天为止处于什么状态？

声明规则（R1~R10，与 README「虚拟交易口径」一致）：
  R1 挂单：推荐日 D0 起以 primaryEntry 挂限价买单，有效 PAPER_ENTRY_VALID_DAYS 个交易日。
     · primaryEntry 缺失，或 ≥ D0 开盘价 → 退化为止损前市价单，按 D0 开盘价成交。
  R2 成交价：开盘 ≤ 买点 → 按开盘价（更优价）成交；否则当日最低 ≤ 买点 → 按买点成交。
  R3 逾期：窗口内最低价从未触及买点 → 撤单（expired）。
  R4 破位放弃：成交价 ≤ 止损价 → 买点本身已在破位线下方，放弃建仓（gap_below_stop）。
  R5 止损：成交次日起，任一交易日最低 ≤ 止损价 → 按 min(止损价, 当日开盘) 卖出全部余量。
  R6 止盈：三档各减 1/3；当日最高 ≥ 档位 → 按 max(档位, 当日开盘) 卖出该档数量。
  R7 同日冲突：日线无法判定先后 → 保守「先止损后止盈」；有完整分钟线时按分钟路径。
  R8 T+1：成交当日不得卖出。
  R9 涨跌停：一字涨停不买、一字跌停不卖。
  R10 到期：持有满 holdingDays 个交易日，余量按收盘价平仓。

与账本对账时，只比「状态 / 成交日 / 成交价 / 是否有减仓 / 是否止损」，
数量差异（资金约束导致）单独标注，不算规则错误。
"""
from __future__ import annotations

import json
from typing import Callable

from . import bars, config, store

Logger = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


def _val(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and x > 0 else None


def _buy(raw: float, cap: float) -> float:
    """买入价：让 5bp 滑点向上取整到分，但限价单不得高于买点。"""
    t = config.PAPER_TICK
    n = round(raw * (1 + config.PAPER_SLIP_BPS / 1e4) / t, 6)
    return round(min(cap, -(-n // 1) * t), 2)


def _sell(raw: float) -> float:
    t = config.PAPER_TICK
    n = round(raw * (1 - config.PAPER_SLIP_BPS / 1e4) / t, 6)
    return round((n // 1) * t, 2)


def _limits(code: str, name: str | None, pre_close: float | None) -> tuple[float | None, float | None]:
    if not pre_close:
        return None, None
    band = 0.05 if (name and "ST" in name.upper()) else (
        0.20 if code.startswith(("300", "301", "688", "689")) else 0.10)
    return round(pre_close * (1 + band), 2), round(pre_close * (1 - band), 2)


def build_rules(pick: dict) -> dict:
    """把 suggestion 翻成可判定规则，并记录与上游参数的冲突。"""
    sug = pick.get("suggestion")
    if isinstance(sug, str):
        try:
            sug = json.loads(sug)
        except json.JSONDecodeError:
            sug = {}
    sug = sug or {}
    issues: list[str] = []

    ref = _val(pick.get("open_price")) or _val(pick.get("ref_price")) or 0.0
    entry = _val(sug.get("primaryEntry"))
    mode = "limit"
    if entry is None:
        entry, mode = ref, "open"
        issues.append("缺 primaryEntry，按开盘价市价成交")
    elif ref and entry >= ref:
        issues.append(f"买点 {entry:.2f} ≥ 开盘 {ref:.2f}，退化为开盘价成交")
        entry, mode = ref, "open"

    stop = _val(sug.get("stopLoss"))
    sec = _val(sug.get("secondaryEntry"))
    if stop is None or (entry and stop >= entry):
        if sec and entry and 0 < sec < entry:
            issues.append(f"stopLoss={sug.get('stopLoss')} 相对买点无效，改用次买点 {sec:.2f} 作破位线")
            stop = sec
        else:
            issues.append(f"stopLoss={sug.get('stopLoss')} 相对买点无效且无次买点，无止损")
            stop = None

    tps = []
    for t in sug.get("takeProfits") or []:
        p = t.get("price") if isinstance(t, dict) else None
        if isinstance(p, (int, float)) and entry and p > entry:
            tps.append(round(float(p), 3))
    tps = sorted(set(tps))
    if not tps:
        issues.append("止盈档均低于买点，无止盈")

    hold = sug.get("holdingDays")
    hold = int(hold) if isinstance(hold, (int, float)) and hold > 0 else 20

    return {"entry": round(float(entry or 0), 3), "mode": mode, "stop": stop,
            "tps": tps, "hold": hold, "issues": issues, "suggestion": sug}


def audit_one(conn, pick: dict, cal: list[str], kb: dict[str, dict[str, dict]],
              cutoff: str, log: Logger = _noop) -> dict:
    """单笔独立判定（无资金约束，数量按 config 单笔预算折算）。"""
    code, d0 = pick["code"], pick["trade_date"]
    rule = build_rules(pick)
    i0 = cal.index(d0) if d0 in cal else None
    row = {
        "trade_date": d0, "code": code, "name": pick["name"], "rating": pick["rating"],
        "entry": rule["entry"], "mode": rule["mode"], "stop": rule["stop"],
        "tps": rule["tps"], "hold": rule["hold"], "issues": rule["issues"],
        "status": "pending", "fill_date": None, "fill_price": None, "qty": 0,
        "events": [], "days": 0, "mfe": None, "mae": None, "exit_reason": None,
        "path": [],
    }
    if i0 is None:
        row.update(status="expired", exit_reason="no_calendar")
        return row

    # ---- R1/R2/R3 建仓
    fill_i = None
    for i in range(i0, min(i0 + config.PAPER_ENTRY_VALID_DAYS, len(cal))):
        d = cal[i]
        if d > cutoff:
            break
        bar = kb.get(d)
        if not bar or not (bar.get("volume") or 0):
            continue                                     # 停牌，挂单顺延
        op, lo = float(bar["open"]), float(bar["low"])
        prev = kb.get(cal[i - 1], {}).get("close") if i else None
        up, _dn = _limits(code, pick["name"], prev)
        if up and op >= up and lo >= up:
            continue                                     # R9 一字涨停买不到
        if rule["mode"] == "open":
            fill_i, price = i, _buy(op, op)
            break
        if op <= rule["entry"]:
            fill_i, price = i, _buy(op, rule["entry"])
            break
        if lo <= rule["entry"]:
            fill_i, price = i, _buy(rule["entry"], rule["entry"])
            break

    if fill_i is None:
        end_i = i0 + config.PAPER_ENTRY_VALID_DAYS - 1
        row["status"] = "expired" if (end_i < len(cal) and cal[min(end_i, len(cal) - 1)] <= cutoff) else "pending"
        row["exit_reason"] = "expired" if row["status"] == "expired" else None
        return row

    fill_date = cal[fill_i]
    # R4 破位放弃
    if rule["stop"] and price <= rule["stop"]:
        row.update(status="expired", exit_reason="gap_below_stop", fill_date=fill_date)
        return row

    qty = int(config.PAPER_CAPITAL / config.PAPER_MAX_POSITIONS / price // 100) * 100
    row.update(fill_date=fill_date, fill_price=round(price, 2), qty=qty, status="open")
    shares = qty
    tps = rule["tps"]
    tranche = []
    if tps:
        base = (qty // 100 // len(tps)) * 100
        if base <= 0:
            tranche = [qty]
        else:
            tranche = [base] * (len(tps) - 1) + [qty - base * (len(tps) - 1)]
    done = [False] * len(tps)
    mfe = mae = 0.0

    # ---- R5/R6/R7/R8/R10 逐日推进
    for i in range(fill_i, len(cal)):
        d = cal[i]
        if d > cutoff:
            break
        bar = kb.get(d)
        if not bar:
            continue
        op, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                          float(bar["low"]), float(bar["close"]))
        mfe = max(mfe, hi / price - 1)
        mae = min(mae, lo / price - 1)
        row["days"] = i - fill_i
        row["path"].append({"date": d, "open": op, "high": hi, "low": lo, "close": cl,
                            "shares": shares})
        if not (bar.get("volume") or 0):
            continue                                     # 停牌不撮合
        if d == fill_date:
            continue                                     # R8 T+1
        prev = kb.get(cal[i - 1], {}).get("close") if i else None
        _up, dn = _limits(code, pick["name"], prev)
        if dn and hi <= dn and lo <= dn:
            continue                                     # R9 一字跌停卖不出

        if shares and rule["hold"] and (i - fill_i) >= rule["hold"]:
            px = _sell(cl)
            row["events"].append({"date": d, "kind": "timeout", "price": px, "qty": shares})
            shares = 0
            row["exit_reason"] = "timeout"
            break

        if shares and rule["stop"] and lo <= rule["stop"]:
            px = _sell(op if op <= rule["stop"] else rule["stop"])
            row["events"].append({"date": d, "kind": "stop", "price": px, "qty": shares})
            shares = 0
            row["exit_reason"] = "stop"
            break

        for k, tp in enumerate(tps):
            if done[k] or shares <= 0:
                continue
            if hi >= tp:
                px = _sell(max(tp, op))
                q = min(tranche[k], shares)
                row["events"].append({"date": d, "kind": f"tp{k + 1}", "price": px, "qty": q})
                shares -= q
                done[k] = True
        if shares <= 0:
            row["exit_reason"] = "take_profit"
            break

    row.update(days=row["days"], mfe=round(mfe, 4), mae=round(mae, 4))
    if shares == 0:
        row["status"] = "closed"
        row["exit_reason"] = row["exit_reason"] or "closed"
    else:
        row["status"] = "open"
        row["shares_left"] = shares
    row["last"] = float(row["path"][-1]["close"]) if row["path"] else None
    return row


def run(conn, start: str = "2026-10-19", end: str | None = None,
        log: Logger = _noop, offline: bool = True) -> dict:
    cal = bars.calendar(conn, log, offline=offline)
    cutoff = end or (cal[-1] if cal else None)
    picks = store.rows(
        conn,
        """SELECT * FROM pick WHERE rating='强推' AND trade_date>=? AND trade_date<=?
            ORDER BY trade_date, rank""", (start, cutoff))
    kb_cache: dict[str, dict[str, dict]] = {}
    rows = []
    for p in picks:
        if p["code"] not in kb_cache:
            kb_cache[p["code"]] = bars.ensure(conn, p["code"], start=start, end=cutoff,
                                              log=log, offline=True)
        rows.append(audit_one(conn, p, cal, kb_cache[p["code"]], cutoff, log))

    # 与账本对账
    ledger = {r["pick_id"]: r for r in store.rows(conn, "SELECT * FROM paper_trade")}
    for r in rows:
        p = next((x for x in picks if x["code"] == r["code"] and x["trade_date"] == r["trade_date"]), None)
        t = ledger.get(p["id"]) if p else None
        r["book"] = None if not t else {
            "status": t["status"], "fill_date": t["fill_date"],
            "fill_price": t["fill_price"], "qty": t["qty"], "exit_reason": t["exit_reason"],
        }
        r["diff"] = _diff(r, r["book"])
    return {"cutoff": cutoff, "rows": rows, "calendar": cal}


def _diff(r: dict, b: dict | None) -> list[str]:
    if not b:
        return ["账本未建单"]
    if b["status"] in ("skipped",) or (b["status"] == "expired" and r["status"] == "open"):
        # 组合层仓位约束：单笔审计不知道「同标的已持仓」，这类差异是预期的，不算规则错
        return []
    out = []
    a_status = "closed" if r["status"] == "closed" else r["status"]
    if b["status"] != a_status and not (r["status"] == "open" and b["status"] == "open"):
        out.append(f"状态 规则={a_status} / 账本={b['status']}")
    if r["fill_date"] and b["fill_date"] and r["fill_date"] != b["fill_date"]:
        out.append(f"成交日 规则={r['fill_date']} / 账本={b['fill_date']}")
    if r["fill_price"] and b["fill_price"] and abs(r["fill_price"] - b["fill_price"]) > 0.011:
        out.append(f"成交价 规则={r['fill_price']:.2f} / 账本={b['fill_price']:.2f}")
    kinds = {e["kind"] for e in r["events"]}
    if ("stop" in kinds) != (b["exit_reason"] == "stop"):
        out.append("止损判定不一致")
    if any(k.startswith("tp") for k in kinds) and b["status"] == "expired":
        out.append("规则有止盈但账本未成交")
    return out


# --------------------------------------------------------------------------- 报告

def evidence(res: dict, conn) -> list[dict]:
    """给每笔补齐「当日走势 / 买点距离 / 窗口内极值」等证据字段。"""
    out = []
    for r in res["rows"]:
        pick = store.one(conn, "SELECT * FROM pick WHERE trade_date=? AND code=? AND rating='强推'",
                         (r["trade_date"], r["code"]))
        ref = float(pick["ref_price"]) if pick and pick.get("ref_price") else None
        win = []
        cal = res["calendar"]
        if r["trade_date"] in cal:
            i0 = cal.index(r["trade_date"])
            for d in cal[i0:i0 + config.PAPER_ENTRY_VALID_DAYS]:
                if d > res["cutoff"]:
                    break
                win.append(d)
        lo_win = min((p["low"] for p in r["path"] if p["date"] in win), default=None)
        # 窗口内最低价要从日线拿（path 只有成交后）
        kb = bars.range_of(conn, r["code"], start=r["trade_date"], end=res["cutoff"])
        lows = {b["date"]: b["low"] for b in kb}
        lo_win = min((lows[d] for d in win if d in lows), default=None)
        out.append({
            **r, "ref": ref,
            "entry_gap": (r["entry"] / ref - 1) if ref else None,
            "window": win,
            "window_low": lo_win,
            "low_vs_entry": (lo_win / r["entry"] - 1) if lo_win and r["entry"] else None,
        })
    return out


def markdown(conn, start: str = "2026-10-19", end: str | None = None) -> str:
    res = run(conn, start=start, end=end)
    rows = evidence(res, conn)
    sm = __import__("astocks.paper", fromlist=["x"]).summary(conn, "strong")
    acct = sm["account"]

    filled = [r for r in rows if r["fill_date"]]
    opened = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] == "closed"]
    pend = [r for r in rows if r["status"] == "pending"]
    exp = [r for r in rows if r["status"] == "expired"]
    tp_hits = [r for r in rows if any(e["kind"].startswith("tp") for e in r["events"])]
    stop_hits = [r for r in rows if any(e["kind"] == "stop" for e in r["events"])]
    diffs = [r for r in rows if r["diff"]]

    L: list[str] = []
    L += [f"# 强推策略逐笔回溯审计 · {start} → {res['cutoff']}", "",
          f"- 生成时间：__TIME__",
          f"- 口径：**单笔独立判定、无资金约束**，与虚拟盘账本（组合层，含现金/槽位约束）对账",
          f"- 样本：{len(rows)} 笔强推（10-19 起）",
          ""]

    L += ["## 一、结论", "",
          f"- 单笔口径可成交 **{len(filled)}** 笔（{len(filled) / len(rows):.0%}）；"
          f"组合口径累计建仓 {sm['filled']} 笔、当前在手 {len(acct['positions'])} 只、已平仓 {sm['closed']} 笔"
          f"（差集为「同标的已持仓」未重复建仓）",
          f"- 未成交失效：{len(exp)} 笔；仍在挂单：{len(pend)} 笔",
          f"- 触及止盈：{len(tp_hits)} 笔；触及止损：{len(stop_hits)} 笔",
          f"- 当前持仓 {acct['positions'].__len__()} 只，总资产 {acct['equity']:,.0f} 元"
          f"（{acct['total_return']:+.2%}）",
          f"- 规则与账本存在实质分歧：**{len(diffs)}** 笔",
          ""]

    L += ["## 二、逐笔回溯（当日走势 → 是否成交 → 是否减仓/止盈止损）", "",
          "| 推荐日 | 代码 | 名称 | 记录价 | 买点 | 买点距记录价 | 止损 | 有效期 | 窗口最低 | 最低距买点 | 结果 | 成交日/价 | 数量 | 事件 | 当前 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        win = f"{r['window'][0][5:]}~{r['window'][-1][5:]}" if r["window"] else "-"
        ev = "；".join(f"{e['date'][5:]} {_ev_name(e['kind'])} {e['price']:.2f}×{e['qty']}"
                       for e in r["events"]) or "-"
        L.append(
            f"| {r['trade_date']} | {r['code']} | {r['name']} | {r['ref'] or 0:.2f} | "
            f"{r['entry']:.2f} | {_pct(r['entry_gap'])} | {r['stop'] or 0:.2f} | {win} | "
            f"{r['window_low'] or 0:.2f} | {_pct(r['low_vs_entry'])} | {_st(r['status'], r['exit_reason'])} | "
            f"{r['fill_date'] or '-'} @{r['fill_price'] or 0:.2f} | {r['qty'] or 0} | {ev} | "
            f"{_pct(r['mfe'])} / {_pct(r['mae'])} |")
    L.append("")

    # ---- 三、组合账本逐日
    curve = {d["trade_date"]: d for d in
             __import__("astocks.paper", fromlist=["x"]).equity_curve(conn)}
    all_fills = [r for r in rows if r["fill_date"]]
    L += ["## 三、按日账本（组合口径 · 10 万本金）", "",
          "| 日期 | 当日强推 | 新成交(单笔口径) | 当日止盈 | 当日止损 | 收盘持仓 | 账户总资产 | 当日收益 |",
          "|---|---|---|---|---|---|---|---|"]
    for d in sorted({r["trade_date"] for r in rows}):
        cur = curve.get(d, {})
        L.append(
            f"| {d} | {sum(1 for r in rows if r['trade_date'] == d)} | "
            f"{sum(1 for r in all_fills if r['fill_date'] == d)} | "
            f"{sum(1 for r in rows for e in r['events'] if e['date'] == d and e['kind'].startswith('tp'))} | "
            f"{sum(1 for r in rows for e in r['events'] if e['date'] == d and e['kind'] == 'stop')} | "
            f"{cur.get('positions', '-')} | {cur.get('equity', 0):,.0f} | {_pct(cur.get('ret_d0'))} |")
    L.append("")

    # ---- 四、当日强推 × 当日走势
    L += ["## 四、当日强推 × 当日走势", "",
          "「当日涨跌」= 收盘 / 记录价 − 1；「振幅」= (最高−最低)/记录价；"
          "「最低距买点」< 0 表示当日最低已跌穿买点（触发成交）。", ""]
    for d in sorted({r["trade_date"] for r in rows}):
        sub = [r for r in rows if r["trade_date"] == d]
        L += [f"### {d}（{len(sub)} 只强推）", "",
              "| 代码 | 名称 | 记录价 | 开盘 | 最高 | 最低 | 收盘 | 当日涨跌 | 振幅 | 买点 | 最低距买点 | 当日是否成交 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sub:
            bar = bars.range_of(conn, r["code"], start=d, end=d)
            b = bar[0] if bar else {}
            ref = r["ref"] or b.get("open") or 0
            chg = (b["close"] / ref - 1) if ref and b.get("close") else None
            amp = ((b["high"] - b["low"]) / ref) if ref and b.get("high") else None
            hit = "是" if r["fill_date"] == d else ("窗口内 " + r["fill_date"] if r["fill_date"] else "否")
            L.append(
                f"| {r['code']} | {r['name']} | {ref:.2f} | {b.get('open', 0):.2f} | "
                f"{b.get('high', 0):.2f} | {b.get('low', 0):.2f} | {b.get('close', 0):.2f} | "
                f"{_pct(chg)} | {_pct(amp)} | {r['entry']:.2f} | {_pct(r['low_vs_entry'])} | {hit} |")
        L.append("")

    if diffs:
        L += ["## 五、与账本的分歧", "", "| 推荐日 | 代码 | 名称 | 差异 |", "|---|---|---|---|"]
        L += [f"| {r['trade_date']} | {r['code']} | {r['name']} | {'；'.join(r['diff'])} |" for r in diffs]
        L.append("")
    else:
        L += ["## 五、与账本的分歧", "", "- 无（单笔审计与组合账本完全一致）", ""]

    L += ["## 六、规则声明（本次审计依据，已落到代码）", "",
          "| 编号 | 规则 | 本次状态 |", "|---|---|---|",
          "| R1 | 推荐日 D0 起，以 `primaryEntry` 挂限价买单，有效 "
          f"{config.PAPER_ENTRY_VALID_DAYS} 个交易日 | 沿用 |",
          "| R2 | 开盘 ≤ 买点按开盘价成交；否则当日最低 ≤ 买点按买点成交 | 沿用 |",
          "| R3 | 窗口内未触及买点 → 撤单失效 | 沿用 |",
          "| R4 | 成交价 ≤ 止损价 → 放弃建仓（买点已在破位线下） | 沿用 |",
          "| R5 | 成交次日起最低 ≤ 止损 → 按 min(止损, 开盘) 清仓 | 沿用 |",
          "| R6 | 三档止盈各减 1/3，**每档必须整手（100 股）** | **本次修正**（原按 qty/3 会出现 166 股） |",
          "| R7 | 同日既触止损又触止盈 → 保守先止损；有完整分钟线时按分钟路径 | 沿用（当前无分钟线） |",
          "| R8 | T+1：成交当日不得卖出 | 沿用 |",
          "| R9 | 一字涨停不买、一字跌停不卖；停牌日不撮合 | 沿用 |",
          "| R10 | 持有满 `holdingDays` 个交易日，余量按收盘价平仓 | 沿用 |",
          "| R11 | 同标的已有持仓时，后续推荐不重复建仓（skipped） | 沿用 |",
          "| R12 | **不再「撤旧挂新」**：同标的多张买单并行有效，先触及者成交 | **本次修正** |",
          ""]

    L += ["## 七、本次修正记录", "",
          "### 1. 撤旧挂新 → 改为不撤单（影响最大）", "",
          "旧规则：同一标的来了新推荐，就撤掉未成交的旧挂单、按新买点重挂。",
          "问题：站点每天把买点下调（本例普遍比记录价低 6%），每来一次就撤单重挂，",
          "委托永远等不到成交 —— 等价于一个未声明的「越跌越不买」择时。",
          "实证：10-20 的 601898 买点 14.70，10-21 最低 14.68 已触发；10-20 的 600011 买点 7.40，",
          "10-21 最低 7.39 已触发。两笔都被撤单吃掉，账本原先显示「未成交」。",
          "新规则：每张建议单独立有效 3 个交易日，同标的多单并存，靠成交时的持仓去重防止重复建仓。",
          "",
          "### 2. 三档减仓按整手", "",
          "旧规则：`qty // 3`，500 股会拆成 166/166/168，A 股卖不出非整手委托。",
          "新规则：每档取 100 股整数倍，余数并入最后一档。",
          "",
          "### 3. 分钟线缺失（数据缺口，未修）", "",
          "- `bar_min` 表为空，10-19 以来**没有任何 5 分钟线**。",
          "- 后果：同一天既触止损又触止盈时，只能按「先止损后止盈」的保守口径判定，",
          "  这会系统性低估收益（若实际是先冲高再跳水，止盈单本可成交）。",
          "- 补齐方式：`astocks minutes --file <json>`（数据源为腾讯自选股 `data_kline period=m5`）。",
          ""]

    return "\n".join(L).replace("__TIME__", store.now_iso())


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def _ev_name(kind: str) -> str:
    return {"stop": "止损", "timeout": "到期平仓"}.get(kind, "止盈" + kind[2:] if kind.startswith("tp") else kind)


def _st(status: str, reason: str | None) -> str:
    return {"open": "持仓", "closed": "已平仓", "pending": "挂单中",
            "expired": {"gap_below_stop": "破位放弃", "duplicate": "重复跳过",
                        "expired": "未触及失效"}.get(reason or "expired", "失效")}.get(status, status)
