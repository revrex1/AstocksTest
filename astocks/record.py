"""开盘记录：抓取站点当日推荐股 + 记录时点价格，落库。

口径说明：
- ref_price 取「记录时点」的实时价（09:31 时约等于开盘价），作为后续收益的买入基准。
- open_price / prev_close 同时留存，收盘复盘时可比较「开盘价买入口径」与「记录时点口径」。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from . import config, paper, source, store

Logger = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


def _positive(*vals) -> float | None:
    """返回第一个 >0 的数值，用于价格兜底。"""
    for v in vals:
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _index_row(bench: dict) -> dict:
    """基准当日快照；收盘复盘时会补 bench_close。"""
    pct = bench.get("changePct")
    return {
        "bench_code": config.BENCH_CODE,
        "bench_open": bench.get("open"),
        "bench_pct": (pct / 100.0) if isinstance(pct, (int, float)) else None,
    }


def record_open(trade_date: str | None = None,
                simulate: bool = False,
                force: bool = False,
                log: Logger = _noop) -> dict:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    conn = store.init_db()

    existing = store.get_day(conn, trade_date)
    if existing and not force:
        log(f"⏭  {trade_date} 已记录（{existing['pick_count']} 只），跳过。--force 可覆盖")
        return {"trade_date": trade_date, "skipped": True, "pick_count": existing["pick_count"]}

    if not simulate and not source.is_trading_day(trade_date):
        log(f"⏸  {trade_date} 非交易日（行情快照未更新），不记录")
        return {"trade_date": trade_date, "skipped": True, "reason": "not_trading_day"}

    log("⚡ 拉取全 A 研判（约 60~90s）…")
    research = source.fetch_research(trade_date=trade_date)
    picks = research.get("picks") or []
    stocks = {s["code"]: s for s in (research.get("stocks") or [])}
    log(f"✅ 研判完成：池 {research.get('universeSize')} / 扫描 {research.get('scannedCount')} / "
        f"入围 {len(stocks)} / 推荐 {len(picks)}")

    codes = [p["stock"]["code"] for p in picks]
    quotes = {q.get("secCode"): q for q in source.fetch_quotes(codes + [config.BENCH_CODE], trade_date)}
    bench = quotes.get(config.BENCH_CODE.replace("sh", ""), {})

    # 模拟模式：用当日日线开盘价代替实时价，便于回放验证链路
    klines: dict[str, dict] = {}
    if simulate:
        for c in codes:
            bars_list = source.fetch_kline(c)
            bars = {b["date"]: b for b in bars_list}
            if trade_date in bars:
                idx = [b["date"] for b in bars_list].index(trade_date)
                klines[c] = {
                    "bar": bars[trade_date],
                    "prev": bars[bars_list[idx - 1]["date"]]["close"] if idx > 0 else None,
                }

    rows_added = 0
    for p in picks:
        s = p.get("stock", {})
        code = s.get("code")
        if not code:
            continue
        q = quotes.get(code, {})
        sim = klines.get(code, {})
        bar = sim.get("bar", {})
        sug = p.get("suggestion") or {}
        # 优先用开盘价作为买入基准：即使任务延迟执行（电脑休眠/开机晚），
        # 记录的仍是当日真实开盘价，而不是延迟时点的滑价。
        ref = _positive(bar.get("open"), q.get("open"), q.get("price"), s.get("price"))
        if not ref:
            log(f"⚠  {code} 无有效价格（停牌/未开盘），仅存档不计收益")
        elif not (bar.get("open") or q.get("open")):
            log(f"⚠  {code} 开盘价缺失，改用现价 {ref}（口径偏差）")
        row = {
            "trade_date": trade_date,
            "code": code,
            "name": s.get("name") or q.get("name") or code,
            "rank": p.get("rank"),
            "rating": sug.get("rating") or "可关注",
            "total_score": p.get("totalScore"),
            "investability": p.get("investability"),
            "ref_price": ref,
            "prev_close": q.get("prevClose") or sim.get("prev") or bar.get("open"),
            "open_price": _positive(bar.get("open"), q.get("open")),
            "stop_loss": sug.get("stopLoss"),
            "tp1": (sug.get("takeProfits") or [{}])[0].get("price") if sug.get("takeProfits") else None,
            "primary_entry": sug.get("primaryEntry"),
            "holding_days": sug.get("holdingDays"),
            "holding_tag": sug.get("holdingTag"),
            "reasons": store.dump(p.get("reasons") or []),
            "risks": store.dump(p.get("risks") or []),
            "tags": store.dump(p.get("tags") or []),
            "suggestion": store.dump(sug),
            "raw": store.dump(p),
            "simulated": 1 if simulate else 0,
            "created_at": store.now_iso(),
        }
        store.insert_pick(conn, row)
        # 留一份不可变快照：pick 后续被 --force 覆盖，历史推荐策略仍可原样回溯
        pid = store.one(conn, "SELECT id FROM pick WHERE trade_date=? AND code=?",
                        (trade_date, code))["id"]
        store.save_pick_snapshot(conn, pid, row, paper.build_plan({**row, "id": pid}))
        rows_added += 1

    strong = sum(1 for p in picks if (p.get("suggestion") or {}).get("rating") == "强推")
    day = {
        "trade_date": trade_date,
        "data_source": "simulated" if simulate else "live",
        "recorded_at": store.now_iso(),
        "universe_size": research.get("universeSize"),
        "scanned_count": research.get("scannedCount"),
        "indicator_passed": research.get("indicatorPassed"),
        "pool_size": research.get("poolSize"),
        "pick_count": rows_added,
        "strong_count": strong,
        "watch_count": rows_added - strong,
        "reviewed_at": None,
        "note": "模拟回放" if simulate else None,
        **_index_row(bench),
    }
    store.upsert_trading_day(conn, day)
    conn.commit()
    log(f"💾 入库完成：{trade_date} 推荐 {rows_added} 只（强推 {strong} / 可关注 {rows_added - strong}）")

    return {"trade_date": trade_date, "skipped": False, "pick_count": rows_added,
            "strong": strong, "watch": rows_added - strong, "research": research}
