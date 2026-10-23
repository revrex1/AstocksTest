"""本地日线库：读写 SQLite 的 bar 表，作为复盘与虚拟交易的唯一数据来源。

为什么要落库：
1. 站点 `/api/kline` 只回约 320 根，滚过去就没了，历史回溯会凭空断档；
2. 每次复盘都重拉一遍日线，几十只票就是几十次请求，回溯 N 次就重复 N 次；
3. 落库后 `--offline` 可完全离线重放，结果可复现。

刷新策略：每只票每个自然日最多抓一次（`bar_sync` 记录），其余全部走本地。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from . import config, source, store

Logger = Callable[[str], None]


def _noop(msg: str) -> None:
    pass


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def save(conn, code: str, bars: Iterable[dict], src: str = "site") -> int:
    """批量写入日线（幂等 upsert）。"""
    now = store.now_iso()
    rows = [
        (code, b["date"], b.get("open"), b.get("high"), b.get("low"),
         b.get("close"), b.get("volume"), src, now)
        for b in bars
        if b.get("date") and b.get("close") is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO bar (code, trade_date, open, high, low, close, volume, source, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(code, trade_date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
             volume=excluded.volume, source=excluded.source, updated_at=excluded.updated_at""",
        rows,
    )
    return len(rows)


def _sync_meta(conn, code: str) -> dict | None:
    return store.one(conn, "SELECT * FROM bar_sync WHERE code=?", (code,))


def _touch(conn, code: str, last_bar: str | None, note: str = "") -> None:
    n = store.one(conn, "SELECT COUNT(*) AS n FROM bar WHERE code=?", (code,)) or {"n": 0}
    conn.execute(
        """INSERT INTO bar_sync (code, last_fetch_at, last_bar_date, bars, note)
           VALUES (?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET last_fetch_at=excluded.last_fetch_at,
             last_bar_date=excluded.last_bar_date, bars=excluded.bars, note=excluded.note""",
        (code, store.now_iso(), last_bar, n["n"], note),
    )


def refresh(conn, code: str, log: Logger = _noop, force: bool = False) -> int:
    """从站点拉一次日线并落库；同一自然日默认不重复拉。"""
    meta = _sync_meta(conn, code)
    if not force and meta and str(meta["last_fetch_at"])[:10] == _today():
        return 0
    try:
        bars = source.fetch_kline(code)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠  {code} 日线拉取失败：{exc}")
        return 0
    n = save(conn, code, bars)
    last = bars[-1]["date"] if bars else None
    _touch(conn, code, last, f"fetched {n}")
    conn.commit()
    return n


def range_of(conn, code: str, start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT trade_date AS date, open, high, low, close, volume FROM bar WHERE code=?"
    params: list = [code]
    if start:
        sql += " AND trade_date>=?"
        params.append(start)
    if end:
        sql += " AND trade_date<=?"
        params.append(end)
    sql += " ORDER BY trade_date"
    return store.rows(conn, sql, params)


def ensure(conn, code: str, start: str | None = None, end: str | None = None,
           log: Logger = _noop, offline: bool = False) -> dict[str, dict]:
    """拿到指定区间的日线（date -> bar）。本地缺失时按需补，offline 则只用本地。"""
    if not offline:
        refresh(conn, code, log)
    bars = range_of(conn, code, start, end)
    if not bars:
        log(f"⚠  {code} 本地无 {start or '—'}~{end or '—'} 日线，且禁止联网补数")
    return {b["date"]: b for b in bars}


def span(conn, code: str) -> tuple[str | None, str | None, int]:
    r = store.one(
        conn,
        "SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi, COUNT(*) AS n FROM bar WHERE code=?",
        (code,),
    )
    return (r["lo"], r["hi"], r["n"]) if r else (None, None, 0)


def backfill(conn, codes: Iterable[str], log: Logger = _noop, force: bool = False) -> dict:
    """批量补数（新部署 / 换机器 / 长期停跑后用）。"""
    codes = list(dict.fromkeys(codes))
    total, hit = 0, 0
    for code in codes:
        n = refresh(conn, code, log, force=force)
        total += n
        hit += 1 if n else 0
    log(f"🗄  日线补齐：{hit}/{len(codes)} 只更新，共 {total} 根")
    return {"updated_codes": hit, "bars": total}


def calendar(conn, log: Logger = _noop, offline: bool = False) -> list[str]:
    """交易日历：直接用基准指数日线的日期序列，升序。"""
    if not offline:
        refresh(conn, config.BENCH_CODE, log)
    return [r["date"] for r in range_of(conn, config.BENCH_CODE)]


def all_codes(conn) -> list[str]:
    """需要留档的代码：推荐过的股票 + 基准指数。"""
    rows = store.rows(conn, "SELECT DISTINCT code FROM pick ORDER BY code")
    codes = [r["code"] for r in rows]
    if config.BENCH_CODE not in codes:
        codes.append(config.BENCH_CODE)
    return codes


# ------------------------------------------------------------------ 分钟线

def save_minutes(conn, code: str, nodes: Iterable[dict], src: str = "westock") -> int:
    """写入 5 分钟线。nodes 兼容腾讯自选股 data_kline(period=m5) 的返回结构。"""
    rows = []
    for n in nodes:
        ts = str(n.get("date") or n.get("ts") or "").strip()
        if not ts or len(ts) < 16:
            continue
        close = n.get("last", n.get("close"))
        if close is None:
            continue
        rows.append((
            code, ts[:10], ts,
            n.get("open"), n.get("high"), n.get("low"), close,
            n.get("volume"), src,
        ))
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO bar_min (code, trade_date, ts, open, high, low, close, volume, source)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(code, ts) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume, source=excluded.source""",
        rows,
    )
    conn.commit()
    return len(rows)


def backfill_minutes(conn, codes: Iterable[str], log: Logger = _noop,
                     force: bool = False) -> dict:
    """从腾讯财经抓 5 分钟线落库（约 320 根 ≈ 6.5 个交易日，覆盖最近一周）。"""
    from . import tencent
    codes = list(dict.fromkeys(codes))
    total, hit = 0, 0
    for code in codes:
        if not force:
            lo, hi, n = minutes_span(conn, code)
            if n and hi == _today():
                continue
        try:
            nodes = tencent.fetch_minutes(code)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠  {code} 分钟线拉取失败：{exc}")
            continue
        n = save_minutes(conn, code, nodes, src="tencent")
        total += n
        hit += 1 if n else 0
    log(f"⏱  分钟线补齐：{hit}/{len(codes)} 只更新，共 {total} 根")
    return {"updated_codes": hit, "bars": total}


def minutes_of(conn, code: str, start: str | None = None,
               end: str | None = None) -> dict[str, list[dict]]:
    """返回 {trade_date: [按时间升序的分钟 bar]}。"""
    sql = "SELECT trade_date, ts, open, high, low, close, volume FROM bar_min WHERE code=?"
    params: list = [code]
    if start:
        sql += " AND trade_date>=?"
        params.append(start)
    if end:
        sql += " AND trade_date<=?"
        params.append(end)
    sql += " ORDER BY ts"
    out: dict[str, list[dict]] = {}
    for r in store.rows(conn, sql, params):
        out.setdefault(r["trade_date"], []).append(r)
    return out


def minutes_span(conn, code: str) -> tuple[str | None, str | None, int]:
    r = store.one(conn, "SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi, COUNT(*) AS n "
                        "FROM bar_min WHERE code=?", (code,)) or {}
    return r.get("lo"), r.get("hi"), r.get("n") or 0



def summary(conn) -> dict:
    r = store.one(conn, "SELECT COUNT(*) AS bars, COUNT(DISTINCT code) AS codes, "
                        "MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM bar") or {}
    return {
        "bars": r.get("bars") or 0,
        "codes": r.get("codes") or 0,
        "from": r.get("lo"),
        "to": r.get("hi"),
        "size_kb": _db_size_kb(),
    }


def _db_size_kb() -> float:
    import os
    try:
        return round(os.path.getsize(config.DB_PATH) / 1024, 1)
    except OSError:
        return 0.0
