"""收盘复盘：用日线核对推荐股当日与 T+1/3/5 表现，计算策略可靠性指标。"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Callable

from . import bars, config, source, store, tencent

Logger = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


def _safe_quotes(codes: list[str], day: str | None = None, log: Logger = _noop) -> list[dict]:
    """站点行情优先；失败时切腾讯公开接口，保证收盘核对不因单一源挂掉而中断。"""
    try:
        q = source.fetch_quotes(codes, day)
        if q:
            return q
    except Exception as exc:  # noqa: BLE001
        log(f"⚠  站点行情不可用（{exc}），切换腾讯备用源")
    try:
        return tencent.fetch_quotes(codes)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠  备用行情源同样失败：{exc}")
        return []


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def _safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def _evaluate_pick(conn, pick: dict, calendar: list[str], bench: dict[str, dict],
                   kline_cache: dict[str, dict[str, dict]], log: Logger,
                   synthetic: dict[str, dict] | None = None, offline: bool = False) -> dict | None:
    synthetic = synthetic or {}
    code = pick["code"]
    d0 = pick["trade_date"]
    if d0 not in calendar:
        log(f"⚠  {code} 推荐日 {d0} 不在交易日历内，跳过")
        return None

    if code not in kline_cache:
        kline_cache[code] = bars.ensure(conn, code, start=d0, log=log, offline=offline)
    kbars = kline_cache[code]
    b0 = kbars.get(d0) or synthetic.get(code)
    if b0 is None:
        log(f"⚠  {code} {d0} 无日线（停牌/未收盘），保持 pending")
        return None

    ref = pick.get("ref_price") or b0["open"]
    i = calendar.index(d0)

    def bar_at(offset: int) -> dict | None:
        j = i + offset
        if j >= len(calendar):
            return None
        return kbars.get(calendar[j])

    def bench_ret_at(offset: int) -> float | None:
        j = i + offset
        if j >= len(calendar) or j - 1 < 0:
            return None
        prev_bench = bench.get(calendar[j - 1])
        cur_bench = bench.get(calendar[j])
        if not prev_bench or not cur_bench:
            return None
        return _safe_div(cur_bench["close"], prev_bench["close"])

    bench_d0 = bench_ret_at(0)
    bench_d0 = (bench_d0 - 1) if bench_d0 is not None else None

    ret_d0 = _safe_div(b0["close"], ref)
    ret_d0 = (ret_d0 - 1) if ret_d0 is not None else None
    ret_d0_open = _safe_div(b0["close"], b0["open"])
    ret_d0_open = (ret_d0_open - 1) if ret_d0_open is not None else None

    row = {
        "pick_id": pick["id"],
        "trade_date": d0,
        "code": code,
        "d0_open": b0["open"],
        "d0_high": b0["high"],
        "d0_low": b0["low"],
        "d0_close": b0["close"],
        "ret_d0": ret_d0,
        "ret_d0_open": ret_d0_open,
        "mfe_d0": _safe_div(b0["high"], ref, ) - 1 if ref else None,
        "mae_d0": _safe_div(b0["low"], ref) - 1 if ref else None,
        "hit_tp1": None,
        "hit_stop": None,
        "bench_ret_d0": bench_d0,
        "excess_d0": (ret_d0 - bench_d0) if (ret_d0 is not None and bench_d0 is not None) else None,
    }

    tp1, stop = pick.get("tp1"), pick.get("stop_loss")
    if tp1:
        row["hit_tp1"] = 1 if b0["high"] >= tp1 else 0
    if stop:
        row["hit_stop"] = 1 if b0["low"] <= stop else 0

    horizon = 0
    for n in config.HORIZONS:
        bn = bar_at(n)
        rr = _safe_div(bn["close"], ref) if bn else None
        br = bench_ret_at(n)
        row[f"ret_t{n}"] = (rr - 1) if rr is not None else None
        row[f"excess_t{n}"] = ((rr - 1) - (br - 1)) if (rr is not None and br is not None) else None
        if bn is not None:
            horizon = n
    row["horizon"] = horizon
    row["status"] = "done" if horizon == max(config.HORIZONS) else ("partial" if horizon else "pending")
    row["evaluated_at"] = store.now_iso()
    return row


def _aggregate_day(conn, trade_date: str, bench_pct: float | None) -> dict | None:
    data = store.rows(
        conn,
        """SELECT p.code, p.rating, o.ret_d0, o.excess_d0
             FROM pick p JOIN outcome o ON o.pick_id = p.id
            WHERE p.trade_date=? AND o.ret_d0 IS NOT NULL""",
        (trade_date,),
    )
    if not data:
        return None
    rets = [d["ret_d0"] for d in data if d["ret_d0"] is not None]
    excess = [d["excess_d0"] for d in data if d["excess_d0"] is not None]
    wins = sum(1 for r in rets if r > 0)
    best = max(data, key=lambda d: d["ret_d0"])
    worst = min(data, key=lambda d: d["ret_d0"])
    return {
        "trade_date": trade_date,
        "picks": len(data),
        "strong": sum(1 for d in data if d["rating"] == "强推"),
        "watch": sum(1 for d in data if d["rating"] != "强推"),
        "win_d0": wins,
        "win_rate_d0": wins / len(rets) if rets else None,
        "avg_ret_d0": statistics.fmean(rets) if rets else None,
        "med_ret_d0": statistics.median(rets) if rets else None,
        "avg_excess_d0": statistics.fmean(excess) if excess else None,
        "win_rate_excess": (sum(1 for e in excess if e > 0) / len(excess)) if excess else None,
        "best": f"{best['code']}:{best['ret_d0'] * 100:+.2f}%",
        "worst": f"{worst['code']}:{worst['ret_d0'] * 100:+.2f}%",
        "bench_pct": bench_pct,
        "created_at": store.now_iso(),
    }


def _recompute_nav(conn) -> None:
    days = store.rows(conn, "SELECT trade_date, avg_ret_d0, bench_pct FROM day_stat ORDER BY trade_date")
    nav, bnav = 1.0, 1.0
    for d in days:
        r = d["avg_ret_d0"] if d["avg_ret_d0"] is not None else 0.0
        b = d["bench_pct"] if d["bench_pct"] is not None else 0.0
        nav *= (1 + r)
        bnav *= (1 + b)
        conn.execute("UPDATE day_stat SET nav=?, bench_nav=? WHERE trade_date=?", (nav, bnav, d["trade_date"]))


def review_close(trade_date: str | None = None,
                 include_simulated: bool = False,
                 log: Logger = _noop,
                 offline: bool = False) -> dict:
    """复盘指定日期，并顺带补齐历史未满 5 个交易日的跟踪。"""
    conn = store.init_db()
    calendar = bars.calendar(conn, log, offline=offline)
    if not calendar:
        raise RuntimeError("交易日历为空，无法复盘（本地日线库为空，先跑 `astocks bars --force`）")

    target = trade_date or calendar[-1]
    bench_bars = bars.ensure(conn, config.BENCH_CODE, log=log, offline=offline)

    # 日线接口通常在收盘后一段时间才更新；15:05 复盘时若当日 K 线尚未生成，
    # 用行情快照兜底算 T+0，次日再跑时会被真实日线覆盖（自愈）。
    synthetic: dict[str, dict] = {}
    if target not in calendar:
        q = _safe_quotes([config.BENCH_CODE], log=log)
        ok = bool(q) and str(q[0].get("time", ""))[:8] == target.replace("-", "") and q[0].get("price")
        if ok:
            calendar = calendar + [target]
            bench_bars[target] = {
                "date": target, "open": q[0].get("open"), "high": q[0].get("high"),
                "low": q[0].get("low"), "close": q[0].get("price"),
            }
            log(f"ℹ  {target} 日线尚未更新，改用收盘快照计算当日收益")
        else:
            log(f"⚠  {target} 无行情快照，回退到最近交易日 {calendar[-1]}")
            target = calendar[-1]

    # 待评估：目标日 + 所有跟踪未完成的记录
    pending = store.rows(
        conn,
        """SELECT p.* FROM pick p LEFT JOIN outcome o ON o.pick_id=p.id
            WHERE (o.pick_id IS NULL OR o.status != 'done')
              AND p.trade_date <= ?
              AND (? = 1 OR p.simulated = 0)
            ORDER BY p.trade_date, p.rank""",
        (target, 1 if include_simulated else 0),
    )
    if not pending:
        log(f"📭 无待评估标的（{target}）")

    # 目标日若缺日线，批量取快照补 T+0
    today_codes = sorted({p["code"] for p in pending if p["trade_date"] == target})
    if today_codes:
        for q in _safe_quotes(today_codes, target, log):
            sec = q.get("secCode")
            price = q.get("price")
            if not sec or not price:
                continue
            if str(q.get("time", ""))[:8] != target.replace("-", ""):
                continue
            synthetic[sec] = {
                "date": target, "open": q.get("open") or price, "high": q.get("high") or price,
                "low": q.get("low") or price, "close": price,
            }

    kline_cache: dict[str, dict[str, dict]] = {}
    done = 0
    for pick in pending:
        row = _evaluate_pick(conn, pick, calendar, bench_bars, kline_cache, log, synthetic, offline)
        if row:
            store.upsert_outcome(conn, row)
            done += 1
    conn.commit()

    # 聚合每日统计
    days = {d["trade_date"]: d for d in store.rows(conn, "SELECT * FROM trading_day")}
    touched = 0
    for d0, d in days.items():
        if d0 > target:
            continue
        if d.get("data_source") == "simulated" and not include_simulated:
            continue
        bench_prev = bench_bars.get(calendar[calendar.index(d0) - 1]) if d0 in calendar and calendar.index(d0) > 0 else None
        bench_cur = bench_bars.get(d0)
        bench_pct = None
        if bench_prev and bench_cur:
            bench_pct = bench_cur["close"] / bench_prev["close"] - 1
        stat = _aggregate_day(conn, d0, bench_pct)
        if stat:
            store.upsert_day_stat(conn, stat)
            touched += 1
            conn.execute(
                "UPDATE trading_day SET reviewed_at=?, bench_close=?, bench_pct=? WHERE trade_date=?",
                (store.now_iso(), bench_cur["close"] if bench_cur else None, bench_pct, d0),
            )
    _recompute_nav(conn)
    conn.commit()

    log(f"🔍 复盘 {target}：评估 {done} 条记录，聚合 {touched} 个交易日")
    return {"trade_date": target, "evaluated": done, "days": touched}
