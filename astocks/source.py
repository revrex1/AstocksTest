"""数据源客户端：封装 bqgxkzv5v9.coze.site 的内部 JSON 接口。

接口清单（从前端 chunk 反查得到，均为公开无鉴权）：
  POST /api/research        {"maxCandidates":80,"pickMax":8}  -> 全量研判（stock 池 + picks）
  GET  /api/quote?codes=..  -> 实时快照（含 time 字段，可用于判定交易日）
  GET  /api/kline?code=..   -> 日线（约 320 根，指数用 sh 前缀）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from . import config


class SourceError(RuntimeError):
    pass


def _now_stamp() -> str:
    return datetime.now().strftime("%H%M%S")


def _raw_dump(kind: str, payload: Any, trade_date: str) -> str:
    d = os.path.join(config.RAW_DIR, trade_date)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{kind}_{_now_stamp()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def _request(path: str, method: str = "GET", body: dict | None = None,
             timeout: int | None = None, retries: int | None = None) -> Any:
    url = config.BASE_URL + path
    timeout = timeout or config.HTTP_TIMEOUT
    retries = config.HTTP_RETRIES if retries is None else retries
    data = json.dumps(body).encode() if body is not None else None
    last: Exception | None = None

    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "astocks-tracker/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("error"):
                raise SourceError(str(payload["error"]))
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, SourceError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise SourceError(f"{method} {path} 失败：{last}")


# --------------------------------------------------------------------------- 研判

def fetch_research(max_candidates: int = config.MAX_CANDIDATES,
                   pick_max: int = config.PICK_MAX,
                   trade_date: str | None = None) -> dict:
    """拉取一次完整研判。耗时 60~90s，返回 stocks/picks/universeSize 等。"""
    payload = {"maxCandidates": max_candidates, "pickMax": pick_max}
    result = _request("/api/research", method="POST", body=payload)
    _raw_dump("research", result, trade_date or datetime.now().strftime("%Y-%m-%d"))
    return result


# --------------------------------------------------------------------------- 行情

def fetch_quotes(codes: list[str], trade_date: str | None = None) -> list[dict]:
    if not codes:
        return []
    out: list[dict] = []
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        data = _request(f"/api/quote?codes={','.join(chunk)}", timeout=30)
        out.extend(data.get("quotes") or [])
    if trade_date:
        _raw_dump("quotes", out, trade_date)
    return out


def fetch_kline(code: str, trade_date: str | None = None) -> list[dict]:
    data = _request(f"/api/kline?code={code}", timeout=30)
    bars = data.get("bars") or []
    if trade_date and bars:
        _raw_dump(f"kline_{code}", bars, trade_date)
    return bars


# --------------------------------------------------------------------------- 交易日

def market_snapshot_time() -> str:
    """返回行情快照的时间戳（YYYYMMDDHHMMSS）。节假日会停在上一交易日。"""
    quotes = fetch_quotes([config.MARKET_CODE])
    if not quotes:
        raise SourceError("无法获取市场快照时间")
    return str(quotes[0].get("time") or "")


def is_trading_day(day: str) -> bool:
    """day: YYYY-MM-DD。以行情快照日期为准（比星期几判断可靠，自动覆盖节假日）。"""
    ts = market_snapshot_time()
    if len(ts) < 8:
        return False
    return ts[:8] == day.replace("-", "")


def trading_calendar() -> list[str]:
    """用沪深300 日线还原交易日历（升序）。"""
    bars = fetch_kline(config.BENCH_CODE)
    return [b["date"] for b in bars]
