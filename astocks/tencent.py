"""腾讯财经公开行情源（qt.gtimg.cn）：独立于磐石选股站点的备用校验源。

无需 key，字段含义：
 1 名称 / 2 代码 / 3 现价 / 4 昨收 / 5 今开 / 30 时间 / 31 涨跌 / 32 涨跌% /
 33 最高 / 34 最低 / 38 换手率 / 39 PE(TTM) / 43 振幅 / 44 流通市值(亿) /
 45 总市值(亿) / 46 PB / 47 涨停 / 48 跌停 / 49 量比
"""
from __future__ import annotations

import json
import urllib.request

URL = "https://qt.gtimg.cn/q="


def normalize(code: str) -> str:
    """600519 -> sh600519；300274 -> sz300274；sh000300 原样返回。"""
    c = code.strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if c.startswith(("60", "68", "90", "51", "58")):
        return "sh" + c
    if c.startswith(("00", "30", "20", "15", "16", "12")):
        return "sz" + c
    return "sh" + c


def _f(v, idx: int) -> float | None:
    try:
        return float(v[idx])
    except (ValueError, IndexError):
        return None


def fetch_quotes(codes: list[str]) -> list[dict]:
    """返回结构与站点 /api/quote 尽量一致，便于上层无感切换。"""
    if not codes:
        return []
    symbols = [normalize(c) for c in codes]
    req = urllib.request.Request(
        URL + ",".join(symbols),
        headers={"User-Agent": "astocks-tracker/1.0", "Referer": "https://gu.qq.com/"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("gbk", "replace")

    out: list[dict] = []
    for chunk in text.split(";"):
        if "~" not in chunk:
            continue
        f = chunk.split("~")
        if len(f) < 50:
            continue
        price = _f(f, 3)
        if not price:
            continue
        out.append({
            "secCode": f[2],
            "code": "".join(ch for ch in f[0] if ch.isalpha()).lower() + f[2],
            "name": f[1],
            "price": price,
            "prevClose": _f(f, 4),
            "open": _f(f, 5),
            "high": _f(f, 33),
            "low": _f(f, 34),
            "change": _f(f, 31),
            "changePct": _f(f, 32),
            "turnover": _f(f, 38),
            "peTTM": _f(f, 39),
            "amplitude": _f(f, 43),
            "floatCap": _f(f, 44),
            "totalCap": _f(f, 45),
            "pb": _f(f, 46),
            "limitUp": _f(f, 47),
            "limitDown": _f(f, 48),
            "volumeRatio": _f(f, 49),
            "time": f[30],
            "source": "tencent",
        })
    return out


MKLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param="


def fetch_minutes(code: str, period: str = "m5", n: int = 320) -> list[dict]:
    """5 分钟线。返回节点结构与 westock 的 data_kline 对齐，便于共用落库。

    腾讯返回 [时间, 开, 收, 高, 低, 量, {}, 额]，时间形如 202610231455。
    """
    sym = normalize(code)
    url = f"{MKLINE}{sym},{period},,{n}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    node = (payload.get("data") or {}).get(sym) or {}
    raw = node.get(period) or node.get("m5") or []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue
        ts = str(row[0])
        if len(ts) != 12:
            continue
        out.append({
            "ts": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:00",
            "open": float(row[1]), "close": float(row[2]),
            "high": float(row[3]), "low": float(row[4]),
            "volume": float(row[5] or 0),
        })
    return out
