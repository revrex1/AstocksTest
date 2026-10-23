"""数据出口：把库里的复盘结果导成机读 JSON，供外部站点/看板直接消费。

产物（默认 site/data/）：
  index.json        —— 日期清单 + 累计指标 + 每日一行摘要
  <YYYY-MM-DD>.json —— 单日明细（推荐、指标、板块、资金流、虚拟持仓）

原则：只导出库里已有的东西，不做任何重新计算或推断；缺什么就留空并给出 null。
"""
from __future__ import annotations

import json
import os

from . import config, paper, stats, store


def _r(v, n=4):
    return None if v is None else round(float(v), n)


def _jit(v):
    """JSON 文本字段还原（pick.suggestion 等可能以字符串形式存储）。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _strategy(p: dict, snap: dict | None) -> dict | None:
    """当时给出的交易策略建议。优先取不可变快照，快照缺失才退回当前 pick 行。

    站点原始字段原样保留，不翻译、不换算，避免二手口径失真。
    """
    sug = _jit(snap.get("suggestion_json")) if snap else None
    src = "snapshot"
    if not sug:
        sug = _jit(p.get("suggestion"))
        src = "pick"
    if not isinstance(sug, dict):
        return None

    tps = []
    for t in sug.get("takeProfits") or []:
        if isinstance(t, dict):
            tps.append({
                "level": t.get("level"),
                "pct": t.get("pct"),
                "price": _r(t.get("price"), 3),
                "reason": t.get("reason"),
            })

    signals = []
    for s in sug.get("signals") or []:
        if isinstance(s, dict):
            signals.append({
                "type": s.get("type"),
                "title": s.get("title"),
                "condition": s.get("condition"),
            })

    return {
        "source": src,
        "captured_at": snap.get("captured_at") if snap else None,
        "snapshot_version": snap.get("version") if snap else None,
        "rating": sug.get("rating"),
        "rating_reason": sug.get("ratingReason"),
        "total_weight": sug.get("totalWeight"),
        "entry": {"primary": _r(sug.get("primaryEntry"), 3),
                  "secondary": _r(sug.get("secondaryEntry"), 3)},
        "stop_loss": _r(sug.get("stopLoss"), 3),
        "resistance": _r(sug.get("resistance"), 3),
        "take_profits": tps,
        "holding_days": sug.get("holdingDays"),
        "holding_tag": sug.get("holdingTag"),
        "signals": signals,
        "reasons": _jit(p.get("reasons")) or [],
        "risks": _jit(p.get("risks")) or [],
        "tags": _jit(p.get("tags")) or [],
    }


def _execution(snap: dict | None) -> dict | None:
    """实际执行口径：站点参数矛盾时做过哪些退化、按什么价挂单。"""
    plan = _jit(snap.get("plan_json")) if snap else None
    if not isinstance(plan, dict):
        return None
    return {
        "entry_mode": plan.get("entry_mode"),
        "entry_target": _r(plan.get("entry_target"), 3),
        "valid_days": plan.get("valid_days"),
        "stop_price": _r(plan.get("stop_price"), 3),
        "tp_prices": [_r(x, 3) for x in plan.get("tp_prices") or []],
        "holding_days": plan.get("holding_days"),
        "notes": plan.get("notes") or [],
    }


def _day_payload(conn, day: str, stat: dict | None) -> dict:
    picks = store.pick_meta_rows(store.get_join(conn, day))
    rows = []
    for p in picks:
        snap = store.latest_snapshot(conn, p.get("id")) if p.get("id") else None
        rows.append({
            "rank": p.get("rank"),
            "code": p.get("code"),
            "name": p.get("name"),
            "rating": p.get("rating"),
            "total_score": _r(p.get("total_score"), 2),
            "investability": _r(p.get("investability"), 2),
            "ref_price": _r(p.get("ref_price"), 3),
            "open": _r(p.get("d0_open"), 3),
            "high": _r(p.get("d0_high"), 3),
            "low": _r(p.get("d0_low"), 3),
            "close": _r(p.get("d0_close"), 3),
            "ret_d0": _r(p.get("ret_d0")),
            "excess_d0": _r(p.get("excess_d0")),
            "mfe_d0": _r(p.get("mfe_d0")),
            "mae_d0": _r(p.get("mae_d0")),
            "ret_t1": _r(p.get("ret_t1")),
            "ret_t3": _r(p.get("ret_t3")),
            "ret_t5": _r(p.get("ret_t5")),
            "hit_tp1": p.get("hit_tp1"),
            "hit_stop": p.get("hit_stop"),
            "stop_loss": _r(p.get("stop_loss"), 3),
            "primary_entry": _r(p.get("primary_entry"), 3),
            "strategy": _strategy(p, snap),
            "execution": _execution(snap),
        })

    day_row = store.get_day(conn, day) or {}
    sectors = store.get_context(conn, day, "sectors")
    fundflow = store.get_context(conn, day, "fundflow")
    holdings = [
        {
            "code": t.get("code"), "name": t.get("name"), "rating": t.get("rating"),
            "fill_date": t.get("fill_date"), "fill_price": _r(t.get("fill_price"), 3),
            "qty": t.get("qty"), "last_price": _r(t.get("last_price"), 3),
            "ret_pct": _r(t.get("ret_pct")), "stop_price": _r(t.get("stop_price"), 3),
            "exit_date": t.get("exit_date"), "exit_reason": t.get("exit_reason"),
        }
        for t in paper.ledger(conn, "all") if t.get("status") == "open"
    ]

    return {
        "date": day,
        "source": day_row.get("data_source"),
        "bench_name": config.BENCH_NAME,
        "bench_pct": _r(day_row.get("bench_pct")),
        "stats": {
            "picks": (stat or {}).get("picks"),
            "win_rate": _r((stat or {}).get("win_rate_d0")),
            "avg_ret": _r((stat or {}).get("avg_ret_d0")),
            "med_ret": _r((stat or {}).get("med_ret_d0")),
            "avg_excess": _r((stat or {}).get("avg_excess_d0")),
            "nav": _r((stat or {}).get("nav")),
            "bench_nav": _r((stat or {}).get("bench_nav")),
        } if stat else None,
        "picks": rows,
        "sectors": (sectors or {}).get("sectors") if isinstance(sectors, dict) else sectors,
        "fundflow": (fundflow or {}).get("stocks") if isinstance(fundflow, dict) else fundflow,
        "paper_holdings": holdings,
    }


def build(conn, source: str = "live") -> dict:
    """返回完整导出结构（不落盘），便于单测与二次加工。"""
    ev = stats.evaluate(conn, source=source)
    # 直接用 evaluate 里按 source 过滤并重算过净值链的逐日表：
    # day_stat.nav 是全量累积（含 simulated 回放日），直接导出会和胜率/超额不同源。
    day_rows = ev.get("day_rows") or []

    stat_by_day = {d["trade_date"]: d for d in day_rows}
    days = [d["trade_date"] for d in day_rows]

    index = {
        "updated_at": store.now_iso(),
        "source": source,
        "bench": {"code": config.BENCH_CODE, "name": config.BENCH_NAME},
        "latest": days[-1] if days else None,
        "count": len(days),
        "dates": days,
        "summary": {
            "nav": _r(ev["nav"]),
            "bench_nav": _r(ev["bench_nav"]),
            "excess_nav": _r(ev["excess_nav"]),
            "win_rate": _r(ev["base"]["win_rate"]),
            "avg": _r(ev["base"]["avg"]),
            "avg_excess": _r(ev["base"]["avg_excess"]),
            "profit_factor": _r(ev["base"]["profit_factor"]),
            "samples": ev["eval_picks"],
            "days": ev["days"],
            "max_dd": _r(ev["max_dd"]),
            "verdict": ev["verdict"],
            "verdict_reason": ev["verdict_reason"],
        },
        "daily": [
            {
                "date": d["trade_date"],
                "picks": d["picks"],
                "strong": d["strong"],
                "watch": d["watch"],
                "win_rate": _r(d["win_rate_d0"]),
                "avg_ret": _r(d["avg_ret_d0"]),
                "avg_excess": _r(d["avg_excess_d0"]),
                "bench_pct": _r(d["bench_pct"]),
                "nav": _r(d["nav"]),
                "bench_nav": _r(d.get("bench_nav")),
            }
            for d in day_rows
        ],
        "layers": {k: {"n": v["n"], "win_rate": _r(v["win_rate"]), "avg": _r(v["avg"]),
                       "profit_factor": _r(v["profit_factor"])}
                   for k, v in {**ev["by_rating"], **ev["by_rank"]}.items()},
        "horizons": {k: {"n": v["n"], "win_rate": _r(v["win_rate"]), "avg": _r(v["avg"])}
                     for k, v in ev["by_horizon"].items()},
        "paper": paper.summary(conn, "all"),
    }
    per_day = {d: _day_payload(conn, d, stat_by_day.get(d)) for d in days}
    return {"index": index, "days": per_day}


def _dump(path: str, obj) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    return path


def write(conn, outdir: str | None = None, source: str = "live") -> dict:
    """落盘 index.json + 每个交易日一个 JSON。"""
    out = outdir or os.path.join(config.ROOT, "site", "data")
    data = build(conn, source)
    written = []
    for day, payload in data["days"].items():
        written.append(_dump(os.path.join(out, f"{day}.json"), payload))
    written.append(_dump(os.path.join(out, "index.json"), data["index"]))
    return {"dir": out, "files": len(written), "days": len(data["days"]),
            "index": written[-1]}
