#!/usr/bin/env python3
"""从 docs/data/*.json 恢复本地数据库。

用途：换机迁移 / data/astocks.db 丢失后，用已发布的机读 JSON（这是唯一可能
      幸存到外部的副本）重建「原始记录层」：
        - pick          推荐明细（评级、记录价、买点、止损、止盈、理由）
        - trading_day   每日元信息
        - market_context 板块 / 资金流

不重建、也不该由本脚本重建的部分（走既有自愈链路，口径与平时完全一致）：

    python -m astocks bars --force          # 拉齐历史日线
    python -m astocks snapshot              # 给恢复出来的 pick 补不可变快照
    python -m astocks close --recheck       # 重算 outcome / day_stat / 净值
    python -m astocks paper --start-phase 2026-10-19 --end 2026-11-18 --name 第一期
                                           # 重放虚拟盘账本

用法：
    python tasks/restore_from_docs.py                 # 增量（已存在的交易日跳过）
    python tasks/restore_from_docs.py --force         # 覆盖已有 pick
    python tasks/restore_from_docs.py --fill-prev     # 顺带用 bar 表补 prev_close
    python tasks/restore_from_docs.py --dry-run       # 只看会写什么
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from astocks import config, paper, store  # noqa: E402

DOCS_DATA = os.path.join(ROOT, "docs", "data")


def _num(x):
    return float(x) if isinstance(x, (int, float)) else None


def _int(x):
    return int(x) if isinstance(x, (int, float)) else None


def _pick_row(day: dict, p: dict) -> dict:
    """把导出 JSON 的 pick 还原成 pick 表行。"""
    stg = p.get("strategy") or {}
    entry = stg.get("entry") or {}
    tps = stg.get("take_profits") or []

    # suggestion：导出 JSON 把它拆进了 strategy，这里按站点原始结构拼回去，
    # 保证 paper.build_plan 读到的买点/止损/止盈/持有期与当初一致。
    sug = {
        "rating": p.get("rating") or stg.get("rating"),
        "primaryEntry": _num(p.get("primary_entry")) or _num(entry.get("primary")),
        "secondaryEntry": _num(entry.get("secondary")),
        "stopLoss": _num(p.get("stop_loss")) or _num(stg.get("stop_loss")),
        "takeProfits": [
            {"level": t.get("level"), "pct": t.get("pct"),
             "price": t.get("price"), "reason": t.get("reason")}
            for t in tps if isinstance(t, dict)
        ],
        "holdingDays": _int(stg.get("holding_days")),
        "holdingTag": stg.get("holding_tag"),
    }

    # raw：原始 pick 对象的近似重组（理由/风险/标签都在 strategy 里）
    raw = {
        "rank": p.get("rank"),
        "stock": {"code": p.get("code"), "name": p.get("name"), "price": p.get("ref_price")},
        "totalScore": p.get("total_score"),
        "investability": p.get("investability"),
        "reasons": stg.get("reasons") or [],
        "risks": stg.get("risks") or [],
        "tags": stg.get("tags") or [],
        "suggestion": sug,
        "recoveredFrom": "docs/data",
        "capturedAt": stg.get("captured_at"),
    }

    return {
        "trade_date": day["date"],
        "code": p.get("code"),
        "name": p.get("name") or p.get("code"),
        "rank": p.get("rank"),
        "rating": p.get("rating") or stg.get("rating") or "可关注",
        "total_score": _num(p.get("total_score")),
        "investability": _num(p.get("investability")),
        "ref_price": _num(p.get("ref_price")),
        "prev_close": None,               # 由 --fill-prev 用日线库补
        "open_price": _num(p.get("open")),
        "stop_loss": sug["stopLoss"],
        "tp1": sug["takeProfits"][0]["price"] if sug["takeProfits"] else None,
        "primary_entry": sug["primaryEntry"],
        "holding_days": sug["holdingDays"],
        "holding_tag": sug["holdingTag"],
        "reasons": store.dump(stg.get("reasons") or []),
        "risks": store.dump(stg.get("risks") or []),
        "tags": store.dump(stg.get("tags") or []),
        "suggestion": store.dump(sug),
        "raw": store.dump(raw),
        # 导出 JSON 的 source 决定这条记录是否计入 live 统计：
        # 早期 10-14~10-16 是 simulate 回放，必须标 simulated=1，否则会污染 live 胜率。
        "simulated": 1 if (day.get("source") or "").lower() == "simulated" else 0,
        "created_at": stg.get("captured_at") or f"{day['date']} 09:32:00",
    }


def restore(force: bool = False, dry_run: bool = False) -> dict:
    files = sorted(f for f in glob.glob(os.path.join(DOCS_DATA, "*.json"))
                   if os.path.basename(f) not in ("index.json", "paper.json"))
    if not files:
        raise SystemExit(f"没有找到任何日明细：{DOCS_DATA}（先 git checkout 恢复 docs/data）")

    conn = store.init_db()
    n_day = n_pick = n_snap = n_day_skip = 0
    marks: list[str] = []

    for path in files:
        with open(path, encoding="utf-8") as fh:
            day = json.load(fh)
        d0 = day.get("date")
        if not d0:
            print(f"⚠  {os.path.basename(path)} 没有 date 字段，跳过")
            continue

        existing = store.get_day(conn, d0)
        if existing and existing.get("pick_count") and not force:
            n_day_skip += 1
            continue

        picks = [p for p in (day.get("picks") or []) if p.get("code")]
        strong = sum(1 for p in picks if (p.get("rating") or "") == "强推")
        marks.append(f"{d0}: {len(picks)} 只（强推 {strong}）")

        if dry_run:
            n_day += 1
            n_pick += len(picks)
            continue

        if force:
            conn.execute("DELETE FROM pick WHERE trade_date=?", (d0,))
            conn.execute("DELETE FROM outcome WHERE trade_date=?", (d0,))
            conn.execute("DELETE FROM day_stat WHERE trade_date=?", (d0,))

        captured = sorted(str((p.get("strategy") or {}).get("captured_at") or "") for p in picks)
        recorded_at = next((c for c in captured if c), f"{d0} 09:32:00")

        store.upsert_trading_day(conn, {
            "trade_date": d0,
            "data_source": day.get("source") or "live",
            "recorded_at": recorded_at,
            "pick_count": len(picks),
            "strong_count": strong,
            "watch_count": len(picks) - strong,
            "bench_code": config.BENCH_CODE,
            "bench_pct": _num(day.get("bench_pct")),
            "reviewed_at": None,
            "note": "由 docs/data 恢复（迁移重建）",
        })
        n_day += 1

        for p in picks:
            store.insert_pick(conn, _pick_row(day, p))
            n_pick += 1

        for kind in ("sectors", "fundflow"):
            payload = day.get(kind)
            if payload:
                store.upsert_context(conn, d0, kind, payload, source="docs/data")

    if not dry_run:
        conn.commit()

        # 快照：raw 里已带着 strategy 原文（capturedAt / 理由 / 止盈档），
        # 打 v1-recovered 标记便于与线上真实快照区分。
        missing = store.rows(
            conn,
            """SELECT p.* FROM pick p LEFT JOIN pick_snapshot s ON s.pick_id = p.id
                WHERE s.id IS NULL ORDER BY p.trade_date, p.rank""")
        for row in missing:
            store.save_pick_snapshot(
                conn, row["id"], row, paper.build_plan(row), schema_ver="v1-recovered")
            n_snap += 1
        conn.commit()

    return {"days": n_day, "picks": n_pick, "snapshots": n_snap,
            "skipped_days": n_day_skip, "marks": marks}


def fill_prev() -> int:
    """用本地日线库补 pick.prev_close（导出 JSON 没带前收）。"""
    conn = store.init_db()
    n = conn.execute(
        """UPDATE pick SET prev_close = (
               SELECT b.close FROM bar b
                WHERE b.code = pick.code AND b.trade_date < pick.trade_date
                ORDER BY b.trade_date DESC LIMIT 1)
            WHERE prev_close IS NULL
              AND EXISTS (SELECT 1 FROM bar b
                           WHERE b.code = pick.code AND b.trade_date < pick.trade_date)"""
    ).rowcount
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="从 docs/data/*.json 恢复数据库")
    ap.add_argument("--force", action="store_true", help="覆盖已有交易日（先删该日 pick/outcome/day_stat）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--fill-prev", action="store_true", help="用 bar 表补 prev_close")
    args = ap.parse_args()

    res = restore(force=args.force, dry_run=args.dry_run)
    print(f"交易日 {res['days']} 个（跳过已有 {res['skipped_days']}）· 推荐 {res['picks']} 条"
          f" · 快照 {res['snapshots']} 条")
    for m in res["marks"]:
        print("   ", m)
    if args.dry_run:
        print("（dry-run，未写入）")
        return 0
    if args.fill_prev:
        print(f"prev_close 补齐 {fill_prev()} 条")
    print("\n下一步：")
    print("  python -m astocks bars --force")
    print("  python -m astocks snapshot")
    print("  python -m astocks close --recheck")
    print("  python -m astocks paper --start-phase 2026-10-19 --end 2026-11-18 --name 第一期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
