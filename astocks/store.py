"""SQLite 存储层：建表、写入、查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_day (
    trade_date       TEXT PRIMARY KEY,
    data_source      TEXT NOT NULL DEFAULT 'live',   -- live / simulated
    recorded_at      TEXT NOT NULL,
    universe_size    INTEGER,
    scanned_count    INTEGER,
    indicator_passed INTEGER,
    pool_size        INTEGER,
    pick_count       INTEGER,
    strong_count     INTEGER,
    watch_count      INTEGER,
    bench_code       TEXT,
    bench_open       REAL,
    bench_close      REAL,
    bench_pct        REAL,
    reviewed_at      TEXT,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS pick (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    rank          INTEGER,
    rating        TEXT,            -- 强推 / 可关注
    total_score   REAL,
    investability REAL,
    ref_price     REAL,            -- 记录时点价（开盘买入口径）
    prev_close    REAL,
    open_price    REAL,
    stop_loss     REAL,
    tp1           REAL,
    primary_entry REAL,
    holding_days  INTEGER,
    holding_tag   TEXT,
    reasons       TEXT,
    risks         TEXT,
    tags          TEXT,
    suggestion    TEXT,
    raw           TEXT,
    simulated     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE(trade_date, code)
);

CREATE TABLE IF NOT EXISTS outcome (
    pick_id        INTEGER PRIMARY KEY REFERENCES pick(id) ON DELETE CASCADE,
    trade_date     TEXT NOT NULL,
    code           TEXT NOT NULL,
    d0_open        REAL,
    d0_high        REAL,
    d0_low         REAL,
    d0_close       REAL,
    ret_d0         REAL,      -- ref_price -> 当日收盘
    ret_d0_open    REAL,      -- 当日开盘 -> 当日收盘（可执行口径）
    mfe_d0         REAL,      -- 当日最大浮盈
    mae_d0         REAL,      -- 当日最大浮亏
    hit_tp1        INTEGER,
    hit_stop       INTEGER,
    bench_ret_d0   REAL,
    excess_d0      REAL,
    ret_t1         REAL,
    ret_t3         REAL,
    ret_t5         REAL,
    excess_t1      REAL,
    excess_t3      REAL,
    excess_t5      REAL,
    horizon        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending',
    evaluated_at   TEXT
);

CREATE TABLE IF NOT EXISTS day_stat (
    trade_date   TEXT PRIMARY KEY,
    picks        INTEGER,
    strong       INTEGER,
    watch        INTEGER,
    win_d0       INTEGER,
    win_rate_d0  REAL,
    avg_ret_d0   REAL,
    med_ret_d0   REAL,
    avg_excess_d0 REAL,
    win_rate_excess REAL,
    best         TEXT,
    worst        TEXT,
    nav          REAL,
    bench_nav    REAL,
    bench_pct    REAL,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS market_context (
    trade_date TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- sectors / fundflow / profile
    payload    TEXT NOT NULL,      -- JSON
    source     TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, kind)
);

-- 本地日线库：站点只给约 320 根，过期就滚掉了，必须自己留档，回溯才不依赖上游
CREATE TABLE IF NOT EXISTS bar (
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    source     TEXT,
    updated_at TEXT,
    PRIMARY KEY (code, trade_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bar_sync (
    code         TEXT PRIMARY KEY,
    last_fetch_at TEXT,
    last_bar_date TEXT,
    bars         INTEGER,
    note         TEXT
);

-- 分钟线（5 分钟）：日线粒度看不出同一天里止盈与止损谁先到，持仓日必须用分钟路径重放
CREATE TABLE IF NOT EXISTS bar_min (
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ts         TEXT NOT NULL,     -- 'YYYY-MM-DD HH:MM:SS'
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    source     TEXT,
    PRIMARY KEY (code, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_bar_min_code_date ON bar_min (code, trade_date);

-- 推荐快照：每次抓取都留一份不可变副本，pick 被 --force 覆盖也改不到历史
CREATE TABLE IF NOT EXISTS pick_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id         INTEGER NOT NULL REFERENCES pick(id) ON DELETE CASCADE,
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    rating          TEXT,
    captured_at     TEXT NOT NULL,
    payload_sha     TEXT,
    schema_ver      TEXT,
    raw_json        TEXT,          -- 站点原始 pick 对象
    suggestion_json TEXT,          -- 当时的买卖策略建议
    plan_json       TEXT,          -- 当时解析出的执行计划（含参数修正记录）
    UNIQUE(pick_id, version)
);

-- 虚拟交易（纸面）：一笔推荐 = 一个头寸；成交明细另存 paper_exec，可完整审计
CREATE TABLE IF NOT EXISTS paper_trade (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id       INTEGER UNIQUE REFERENCES pick(id) ON DELETE CASCADE,
    snapshot_id   INTEGER,         -- 建仓所依据的推荐快照
    trade_date    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    rating        TEXT,
    total_score   REAL,
    entry_mode    TEXT,            -- limit（挂单等回踩）/ open（开盘直接成交）
    entry_target  REAL,
    valid_days    INTEGER,
    fill_date     TEXT,
    fill_price    REAL,
    qty           INTEGER,
    notional      REAL,
    stop_price    REAL,
    tp_prices     TEXT,            -- JSON 数组
    holding_days  INTEGER,
    exit_date     TEXT,
    exit_reason   TEXT,            -- take_profit / stop / timeout / expired
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / open / closed / expired
    shares_left   INTEGER,
    realized      REAL,
    last_price    REAL,
    last_date     TEXT,
    ret_pct       REAL,            -- 已实现+浮动的综合收益率
    bench_ret     REAL,            -- 同区间沪深300
    excess        REAL,            -- ret_pct - bench_ret
    hold_days     INTEGER,
    mfe           REAL,
    mae           REAL,
    path_src      TEXT,            -- minute / daily：止盈止损先后依据的粒度
    plan_json     TEXT,            -- 下单时锁定的计划（含口径修正记录）
    created_at    TEXT NOT NULL,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS paper_exec (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES paper_trade(id) ON DELETE CASCADE,
    exec_date  TEXT NOT NULL,
    code       TEXT NOT NULL,
    side       TEXT NOT NULL,      -- buy / sell
    price      REAL NOT NULL,
    qty        INTEGER NOT NULL,
    amount     REAL NOT NULL,
    fee        REAL NOT NULL,
    reason     TEXT,               -- entry / tp1 / tp2 / tp3 / stop / timeout
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_phase (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    scope        TEXT,            -- 强推 / 全部
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    bench_code   TEXT,
    bench_base   REAL,            -- 期初基准收盘
    bench_date   TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pick_date ON pick(trade_date);
CREATE INDEX IF NOT EXISTS idx_outcome_date ON outcome(trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_date ON paper_trade(trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_code ON paper_trade(code);
CREATE INDEX IF NOT EXISTS idx_exec_trade ON paper_exec(trade_id);
CREATE INDEX IF NOT EXISTS idx_snap_date ON pick_snapshot(trade_date, code);
CREATE INDEX IF NOT EXISTS idx_bar_date ON bar(trade_date);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """增量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会加字段）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trade)")}
    if cols and "snapshot_id" not in cols:
        conn.execute("ALTER TABLE paper_trade ADD COLUMN snapshot_id INTEGER")
    if cols and "path_src" not in cols:
        conn.execute("ALTER TABLE paper_trade ADD COLUMN path_src TEXT")


def init_db(path: str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- 写入

def upsert_trading_day(conn: sqlite3.Connection, row: dict) -> None:
    cols = ",".join(row)
    vals = ",".join("?" for _ in row)
    updates = ",".join(f"{k}=excluded.{k}" for k in row if k != "trade_date")
    conn.execute(
        f"INSERT INTO trading_day ({cols}) VALUES ({vals}) "
        f"ON CONFLICT(trade_date) DO UPDATE SET {updates}",
        list(row.values()),
    )


def insert_pick(conn: sqlite3.Connection, row: dict) -> int:
    cols = ",".join(row)
    vals = ",".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT INTO pick ({cols}) VALUES ({vals}) "
        f"ON CONFLICT(trade_date, code) DO UPDATE SET "
        f"{','.join(f'{k}=excluded.{k}' for k in row if k not in ('trade_date', 'code'))}",
        list(row.values()),
    )
    return int(cur.lastrowid or 0)


def upsert_outcome(conn: sqlite3.Connection, row: dict) -> None:
    cols = ",".join(row)
    vals = ",".join("?" for _ in row)
    updates = ",".join(f"{k}=excluded.{k}" for k in row if k != "pick_id")
    conn.execute(
        f"INSERT INTO outcome ({cols}) VALUES ({vals}) "
        f"ON CONFLICT(pick_id) DO UPDATE SET {updates}",
        list(row.values()),
    )


def upsert_day_stat(conn: sqlite3.Connection, row: dict) -> None:
    cols = ",".join(row)
    vals = ",".join("?" for _ in row)
    updates = ",".join(f"{k}=excluded.{k}" for k in row if k != "trade_date")
    conn.execute(
        f"INSERT INTO day_stat ({cols}) VALUES ({vals}) "
        f"ON CONFLICT(trade_date) DO UPDATE SET {updates}",
        list(row.values()),
    )


# --------------------------------------------------------------------------- 查询

def rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def get_day(conn: sqlite3.Connection, trade_date: str) -> dict | None:
    return one(conn, "SELECT * FROM trading_day WHERE trade_date=?", (trade_date,))


def get_picks(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    return rows(conn, "SELECT * FROM pick WHERE trade_date=? ORDER BY rank", (trade_date,))


def get_join(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    return rows(
        conn,
        """SELECT p.*, o.d0_open, o.d0_high, o.d0_low, o.d0_close,
                  o.ret_d0, o.ret_d0_open, o.mfe_d0, o.mae_d0, o.excess_d0,
                  o.bench_ret_d0, o.ret_t1, o.ret_t3, o.ret_t5,
                  o.excess_t1, o.excess_t3, o.excess_t5, o.hit_tp1, o.hit_stop,
                  o.horizon, o.status
             FROM pick p LEFT JOIN outcome o ON o.pick_id = p.id
            WHERE p.trade_date = ?
            ORDER BY p.rank""",
        (trade_date,),
    )


def latest_days(conn: sqlite3.Connection, limit: int = 20, source: str | None = None) -> list[dict]:
    if source:
        return rows(conn, "SELECT * FROM trading_day WHERE data_source=? ORDER BY trade_date DESC LIMIT ?",
                    (source, limit))
    return rows(conn, "SELECT * FROM trading_day ORDER BY trade_date DESC LIMIT ?", (limit,))


def pick_meta(row: dict) -> dict:
    """把 pick 行里的 JSON 文本字段还原，便于报告渲染。"""
    out = dict(row)
    for k in ("reasons", "risks", "tags", "suggestion"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except json.JSONDecodeError:
                out[k] = None
    return out


def save_pick_snapshot(conn: sqlite3.Connection, pick_id: int, row: dict,
                       plan: dict | None = None, schema_ver: str = "v1") -> int:
    """追加一版推荐快照（不可变）。同一 pick 多次记录则 version 递增。"""
    import hashlib

    ver = one(conn, "SELECT COALESCE(MAX(version),0)+1 AS v FROM pick_snapshot WHERE pick_id=?",
              (pick_id,))["v"]
    raw_obj = row.get("raw")
    if isinstance(raw_obj, str):
        try:
            raw_obj = json.loads(raw_obj)
        except json.JSONDecodeError:
            raw_obj = None
    if not isinstance(raw_obj, (dict, list)):
        raw_obj = {k: v for k, v in row.items() if k != "raw"}
    raw = json.dumps(raw_obj, ensure_ascii=False, sort_keys=True)
    cur = conn.execute(
        """INSERT INTO pick_snapshot (pick_id, trade_date, code, version, rating, captured_at,
               payload_sha, schema_ver, raw_json, suggestion_json, plan_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (pick_id, row["trade_date"], row["code"], ver, row.get("rating"), now_iso(),
         hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], schema_ver,
         raw, row.get("suggestion"), json.dumps(plan, ensure_ascii=False) if plan else None),
    )
    return int(cur.lastrowid or 0)


def latest_snapshot(conn: sqlite3.Connection, pick_id: int) -> dict | None:
    return one(conn, "SELECT * FROM pick_snapshot WHERE pick_id=? ORDER BY version DESC LIMIT 1",
               (pick_id,))


def upsert_context(conn: sqlite3.Connection, trade_date: str, kind: str,                   payload: Any, source: str | None = None) -> None:
    conn.execute(
        """INSERT INTO market_context (trade_date,kind,payload,source,created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(trade_date,kind) DO UPDATE SET
             payload=excluded.payload, source=excluded.source, created_at=excluded.created_at""",
        (trade_date, kind, json.dumps(payload, ensure_ascii=False), source, now_iso()),
    )


def get_context(conn: sqlite3.Connection, trade_date: str, kind: str) -> Any:
    r = conn.execute(
        "SELECT payload FROM market_context WHERE trade_date=? AND kind=?", (trade_date, kind)
    ).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except json.JSONDecodeError:
        return None


def pick_meta_rows(rows_: Iterable[dict]) -> list[dict]:
    return [pick_meta(r) for r in rows_]


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
