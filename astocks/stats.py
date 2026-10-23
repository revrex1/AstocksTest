"""策略可靠性评估：胜率、盈亏比、超额、净值与回撤、分层对比、结论。"""
from __future__ import annotations

import math
import statistics
from typing import Iterable

from . import config, store


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0, "win_rate": None, "avg": None, "med": None,
                "profit_factor": None, "avg_win": None, "avg_loss": None, "std": None}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": len(rets),
        "win_rate": len(wins) / len(rets),
        "avg": statistics.fmean(rets),
        "med": statistics.median(rets),
        "avg_win": statistics.fmean(wins) if wins else None,
        "avg_loss": statistics.fmean(losses) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        "std": statistics.pstdev(rets) if len(rets) > 1 else None,
    }


def _max_drawdown(navs: Iterable[float]) -> float | None:
    peak, mdd = None, 0.0
    seen = False
    for v in navs:
        if v is None:
            continue
        seen = True
        peak = v if peak is None else max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1)
    return mdd if seen else None


def evaluate(conn, source: str = "live", last_n_days: int | None = None) -> dict:
    """source: live / simulated / all"""
    where = "" if source == "all" else ("WHERE p.simulated = 0" if source == "live" else "WHERE p.simulated = 1")
    rows = store.rows(
        conn,
        f"""SELECT p.trade_date, p.code, p.name, p.rank, p.rating, p.total_score,
                   p.investability, o.ret_d0, o.excess_d0, o.ret_d0_open,
                   o.ret_t1, o.ret_t3, o.ret_t5, o.excess_t1, o.excess_t3, o.excess_t5,
                   o.mfe_d0, o.mae_d0, o.hit_tp1, o.hit_stop, o.horizon, o.status
              FROM pick p JOIN outcome o ON o.pick_id = p.id
              {where}
             ORDER BY p.trade_date DESC, p.rank""",
    )
    if last_n_days:
        dates = sorted({r["trade_date"] for r in rows}, reverse=True)[:last_n_days]
        keep = set(dates)
        rows = [r for r in rows if r["trade_date"] in keep]

    rets = [_f(r["ret_d0"]) for r in rows if r["ret_d0"] is not None]
    excess = [_f(r["excess_d0"]) for r in rows if r["excess_d0"] is not None]
    base = _stats_of([r for r in rets if r is not None])
    base["excess_n"] = len(excess)
    base["excess_win_rate"] = (sum(1 for e in excess if e > 0) / len(excess)) if excess else None
    base["avg_excess"] = statistics.fmean(excess) if excess else None
    base["med_excess"] = statistics.median(excess) if excess else None

    def split(key) -> dict:
        groups: dict[str, list[float]] = {}
        for r in rows:
            if r["ret_d0"] is None:
                continue
            groups.setdefault(str(key(r)), []).append(r["ret_d0"])
        return {k: _stats_of(v) for k, v in groups.items()}

    by_rating = split(lambda r: r["rating"] or "未标注")
    by_rank = split(lambda r: "Top3" if (r["rank"] or 99) <= 3 else "4名以后")
    by_horizon = {}
    for n in config.HORIZONS:
        vals = [r[f"ret_t{n}"] for r in rows if r.get(f"ret_t{n}") is not None]
        by_horizon[f"T+{n}"] = {
            "n": len(vals),
            "avg": statistics.fmean(vals) if vals else None,
            "win_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
        }

    day_rows = store.rows(conn, "SELECT * FROM day_stat ORDER BY trade_date")
    if source == "live":
        sim_days = {d["trade_date"] for d in store.rows(
            conn, "SELECT trade_date FROM trading_day WHERE data_source='simulated'")}
        day_rows = [d for d in day_rows if d["trade_date"] not in sim_days]
    if last_n_days:
        day_rows = day_rows[-last_n_days:]

    # day_stat.nav 是全量累积值（含 simulated 回放日）。按 source 过滤后必须重算净值链，
    # 否则 nav 与胜率/超额不同源（live 只有 2 天、nav 却含 5 天）。
    _nav, _bnav = 1.0, 1.0
    for d in day_rows:
        _nav *= 1 + (d["avg_ret_d0"] or 0.0)
        _bnav *= 1 + (d["bench_pct"] or 0.0)
        d["nav"], d["bench_nav"] = _nav, _bnav

    navs = [1.0] + [d["nav"] for d in day_rows]
    bnavs = [1.0] + [d["bench_nav"] for d in day_rows]
    nav = navs[-1] if navs else None
    bnav = bnavs[-1] if bnavs else None
    daily = [d["avg_ret_d0"] for d in day_rows if d["avg_ret_d0"] is not None]
    sharpe = None
    if len(daily) > 2:
        sd = statistics.pstdev(daily)
        if sd > 0:
            sharpe = statistics.fmean(daily) / sd * math.sqrt(244)

    tp_hit = [r["hit_tp1"] for r in rows if r["hit_tp1"] is not None]
    stop_hit = [r["hit_stop"] for r in rows if r["hit_stop"] is not None]

    verdict, reason = _verdict(base)

    return {
        "source": source,
        "days": len({r["trade_date"] for r in rows}),
        "picks": len(rows),
        "eval_picks": base["n"],
        "base": base,
        "by_rating": by_rating,
        "by_rank": by_rank,
        "by_horizon": by_horizon,
        "tp1_rate": (sum(tp_hit) / len(tp_hit)) if tp_hit else None,
        "stop_rate": (sum(stop_hit) / len(stop_hit)) if stop_hit else None,
        "nav": nav,
        "bench_nav": bnav,
        "excess_nav": (nav / bnav - 1) if (nav and bnav) else None,
        "max_dd": _max_drawdown(navs),
        "bench_max_dd": _max_drawdown(bnavs),
        "sharpe": sharpe,
        "verdict": verdict,
        "verdict_reason": reason,
        "latest_date": max((r["trade_date"] for r in rows), default=None),
        # 按 source 过滤并重算过净值链的逐日表，供导出/看板复用，避免再算一遍算歪
        "day_rows": day_rows,
    }


def _verdict(base: dict) -> tuple[str, str]:
    n = base["n"]
    if n < config.MIN_SAMPLE:
        return "样本不足", f"仅 {n} 条可评估样本（阈值 {config.MIN_SAMPLE}），不作结论"
    wr, ex = base["win_rate"], base["avg_excess"]
    if wr is None or ex is None:
        return "数据不足", "缺少收益或基准数据"
    if wr >= config.STRONG_WIN and ex >= config.STRONG_EXCESS:
        return "可靠", f"胜率 {wr:.0%}、平均超额 {ex * 100:+.2f}%，具备正向选股能力"
    if wr <= config.REVERSE_WIN and ex <= config.REVERSE_EXCESS:
        return "反向信号", f"胜率仅 {wr:.0%}、平均超额 {ex * 100:+.2f}%，建议反向使用"
    if wr < config.WEAK_WIN:
        return "偏弱", f"胜率 {wr:.0%} 低于 45%，超额 {ex * 100:+.2f}%"
    return "中性", f"胜率 {wr:.0%}、超额 {ex * 100:+.2f}%，无显著优势"
