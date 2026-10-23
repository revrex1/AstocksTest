"""命令行入口：python -m astocks <command>"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import (bars, config, export, paper, record, report, review, sitepush,
               source, stats, store, tencent)


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _after_work(conn, trade_date: str, kind: str, source: str = "live") -> None:
    try:
        paper.sync(conn, log=_log)
    except Exception as exc:  # noqa: BLE001  虚拟交易失败不影响主链路
        _log(f"⚠  虚拟交易结算失败：{exc}")
    if kind == "open":
        p = report.open_report(conn, trade_date)
    else:
        p = report.close_report(conn, trade_date, source=source)
    s = report.strategy_report(conn, source)
    d = report.write_dashboard(conn, source=source)
    t = report.paper_report(conn)
    report.write_paper_page(conn)
    try:
        site = report.write_site(conn, source=source)
        _log(f"🌐 分享站：{site}")
    except Exception as exc:  # noqa: BLE001  站点生成失败不影响主链路
        _log(f"⚠  分享站生成失败：{exc}")
    try:
        ex = export.write(conn, source=source)
        _log(f"📦 数据导出：{ex['days']} 个交易日 → {ex['dir']}")
    except Exception as exc:  # noqa: BLE001  导出失败不影响主链路
        _log(f"⚠  数据导出失败：{exc}")
    else:
        # 导出成功才同步，避免把半成品推上站点
        if source == "live":
            try:
                sitepush.run(trade_date=trade_date, log=_log)
            except Exception as exc:  # noqa: BLE001  推送失败不影响主链路
                _log(f"⚠  站点同步异常：{exc}")
        else:
            _log("⏭  当前为模拟回放数据，不推送站点")
    _log(f"📄 报告：{p}")
    _log(f"📄 评估：{s}")
    _log(f"📄 虚拟交易台账：{t}")
    _log(f"📊 看板：{d}")


def cmd_open(args) -> int:
    res = record.record_open(args.date, simulate=args.simulate, force=args.force, log=_log)
    if res.get("skipped"):
        return 0
    conn = store.init_db()
    _after_work(conn, res["trade_date"], "open", "simulated" if args.simulate else "live")
    return 0


def cmd_close(args) -> int:
    # 收盘前强制重拉当日日线：bars 的「每票每自然日一次」TTL 会让 15:00 后的复盘
    # 复用 09:32 的盘中快照价（2026-10-22 实测错 5/8 只），必须在这里堵住。
    recheck = bool(getattr(args, "recheck", False))
    try:
        conn0 = store.init_db()
        latest = store.latest_days(conn0, limit=1)
        day0 = args.date or (latest[0]["trade_date"] if latest else None)
        if recheck:
            # 全量重算：上游会回溯改写历史日线（前复权基准变化），整库重拉一遍
            _log("♻️  强制重算模式：全量重拉历史日线")
            bars.backfill(conn0, bars.all_codes(conn0), log=_log, force=True)
            codes = []
        else:
            codes = [p["code"] for p in store.get_picks(conn0, day0)] if day0 else []
        # 基准指数同样受 TTL 影响：不刷会用 09:32 快照当收盘价，当日超额方向可能直接反转
        # （2026-10-23 实测：沪深300 快照 -1.42% vs 实际 +1.71%，超额 +2.37% → -0.76%）。
        if config.BENCH_CODE not in codes:
            codes.append(config.BENCH_CODE)
        if codes:
            _log(f"🔄 收盘前强制刷新当日日线：{len(codes)} 只")
            bars.backfill(conn0, codes, log=_log, force=True)
    except Exception as exc:  # noqa: BLE001  刷新失败不阻断复盘，按本地已有数据继续
        _log(f"⚠  当日日线强制刷新失败：{exc}")

    res = review.review_close(args.date, include_simulated=args.sim, log=_log, recheck=recheck)
    conn = store.init_db()
    _after_work(conn, res["trade_date"], "close", "all" if args.sim else "live")
    return 0


def cmd_simulate(args) -> int:
    """回放验证：用历史日线开盘价当买点，跑通「记录→复盘→报告」全链路。"""
    day = args.date or datetime.now().strftime("%Y-%m-%d")
    _log(f"🧪 模拟回放 {day}")
    record.record_open(day, simulate=True, force=True, log=_log)
    review.review_close(day, include_simulated=True, log=_log)
    conn = store.init_db()
    _after_work(conn, day, "close", "all")
    report.open_report(conn, day)
    return 0


def cmd_paper(args) -> int:
    """虚拟交易：建仓 → 逐日结算 → 台账与评估。"""
    conn = store.init_db()
    if args.start_phase:
        start = None if args.start_phase == "today" else args.start_phase
        ph = paper.start_phase(conn, start, args.end, args.name, log=_log)
        res = {"cutoff": None, "updated": 0}
        _log(f"🚩 {ph['name']}：{ph['start_date']} → {ph['end_date']}，期初基准 {ph['bench_base']}")
    elif args.rebuild:
        res = paper.rebuild(conn, args.asof, log=_log, offline=args.offline)
    else:
        res = paper.sync(conn, args.asof, log=_log, offline=args.offline)

    sm = paper.summary(conn, args.view)
    rows = paper.ledger(conn, args.view)
    scope = "强推" if args.view != "all" else "全部评级"
    verdict, reason = paper._verdict(sm["closed"], sm["win_rate"], sm["avg_ret"])
    ph = paper.phase_status(conn)

    head = f"\n虚拟交易 · {scope}（结算截至 {ph['cutoff'] if ph else res.get('cutoff') or '—'}）"
    print(head)
    if ph:
        print(f"跟踪期：{ph['name']} · {ph['start_date']} → {ph['end_date']} · "
              f"进度 {ph['elapsed']}/{ph['total_days']} 个交易日（剩余 {ph['remaining']}）· "
              f"期初基准 {ph['bench_code']} {ph['bench_base']:.2f} @{ph['bench_date']}")
    print("-" * 86)
    if rows:
        print(f"{'代码':<9}{'名称':<9}{'评级':<7}{'建仓日':<12}{'成交价':>8}{'现价':>8}"
              f"{'浮动':>9}{'持有':>7}  状态")
        for r in rows:
            state = {"open": "持仓", "closed": "已平仓", "pending": "等回踩",
                     "expired": "未成交", "skipped": "已有持仓"}.get(r["status"], r["status"])
            print(f"{r['code']:<9}{r['name'][:8]:<9}{str(r['rating'])[:6]:<7}{r['trade_date']:<12}"
                  f"{_fmt(r['fill_price'], 2):>8}{_fmt(r['last_price'], 2):>8}"
                  f"{_pct(r['ret_pct']):>9}{str(r['hold_days'] or '-'):>7}  {state}")
        print("-" * 86)
    print(f"成交 {sm['filled']} / 建仓单 {sm['orders']}（成交率 {sm['fill_rate'] * 100:.0f}%） · "
          f"平仓 {sm['closed']} · 持仓 {sm['open']} · 未成交 {sm['expired'] + sm['pending']}")
    wr = "-" if sm["win_rate"] is None else f"{sm['win_rate'] * 100:.0f}%"
    print(f"平仓胜率 {wr} · 平均每笔 {_pct(sm['avg_ret'])} · "
          f"盈亏比 {_fmt(sm['profit_factor'], 2)} · 累计已实现 {sm['total_pnl']:,.0f} 元")
    print(f"策略结论：{verdict} —— {reason}")

    p = report.paper_report(conn, args.view, res.get("cutoff"))
    h = report.write_paper_page(conn, args.view)
    if args.view != "strong":
        report.paper_report(conn, "strong", res.get("cutoff"))
        report.write_paper_page(conn, "strong")
    report.write_dashboard(conn, source="all")
    _log(f"📄 台账：{p}")
    _log(f"📄 页面：{h}")
    return 0


def cmd_bars(args) -> int:
    """本地日线库：补数、体检。"""
    conn = store.init_db()
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else bars.all_codes(conn)
    if not codes:
        _log("库里还没有推荐记录，先跑 `astocks open` 或用 --codes 指定")
        return 1
    _log(f"🗄  待补 {len(codes)} 只（{'强制重拉' if args.force else '每只每日最多拉一次'}）")
    bars.backfill(conn, codes, log=_log, force=args.force)
    sm = bars.summary(conn)
    print(f"\n本地日线库：{sm['bars']:,} 根 / {sm['codes']} 只 · "
          f"{sm['from'] or '—'} → {sm['to'] or '—'} · DB {sm['size_kb']:,.0f} KB")
    print(f"{'代码':<10}{'起':<12}{'止':<12}{'根数':>6}")
    print("-" * 42)
    for code in codes[:40]:
        lo, hi, n = bars.span(conn, code)
        print(f"{code:<10}{str(lo or '-'):<12}{str(hi or '-'):<12}{n:>6}")
    return 0


def cmd_snapshot(args) -> int:
    """为历史推荐补建快照（老数据没有快照时用，标注为 backfill）。"""
    conn = store.init_db()
    missing = store.rows(
        conn,
        """SELECT p.* FROM pick p LEFT JOIN pick_snapshot s ON s.pick_id = p.id
            WHERE s.id IS NULL ORDER BY p.trade_date, p.rank""")
    if not missing:
        _log("✅ 所有推荐都已有快照")
        return 0
    for p in missing:
        store.save_pick_snapshot(conn, p["id"], p, paper.build_plan(p), schema_ver="v1-backfill")
    conn.commit()
    _log(f"🧾 补建快照 {len(missing)} 条（schema_ver=v1-backfill，captured_at 为补录时间）")
    return 0


def cmd_status(args) -> int:
    conn = store.init_db()
    days = store.latest_days(conn, limit=args.limit)
    if not days:
        _log("数据库为空，先跑 `python -m astocks open`")
        return 0
    ev = stats.evaluate(conn, source="live")
    tag = ""
    if ev["eval_picks"] == 0:
        ev_all = stats.evaluate(conn, source="all")
        if ev_all["eval_picks"]:
            ev, tag = ev_all, "（含模拟回放）"
    print(f"\n{'日期':<12}{'推荐':>5}{'强推':>5}{'胜率':>8}{'均收益':>9}{'均超额':>9}{'净值':>8}  来源")
    print("-" * 68)
    for d in days:
        st = store.one(conn, "SELECT * FROM day_stat WHERE trade_date=?", (d["trade_date"],)) or {}
        wr = f"{st['win_rate_d0'] * 100:.0f}%" if st.get("win_rate_d0") is not None else "-"
        ar = f"{st['avg_ret_d0'] * 100:+.2f}%" if st.get("avg_ret_d0") is not None else "-"
        ae = f"{st['avg_excess_d0'] * 100:+.2f}%" if st.get("avg_excess_d0") is not None else "-"
        nv = f"{st['nav']:.4f}" if st.get("nav") is not None else "-"
        print(f"{d['trade_date']:<12}{d['pick_count'] or 0:>5}{d['strong_count'] or 0:>5}"
              f"{wr:>8}{ar:>9}{ae:>9}{nv:>8}  {d['data_source']}")
    print("-" * 68)
    print(f"策略评级：{ev['verdict']} —— {ev['verdict_reason']}{tag}")
    print(f"累计：{ev['days']} 日 / {ev['eval_picks']} 条可评估 · 净值 {ev['nav'] or 1:.4f} · "
          f"基准 {ev['bench_nav'] or 1:.4f} · 超额 {ev['excess_nav'] or 0:+.2%}")

    ps = paper.summary(conn, "strong")
    if ps["orders"]:
        verdict, _ = paper._verdict(ps["closed"], ps["win_rate"], ps["avg_ret"])
        wr = "-" if ps["win_rate"] is None else f"{ps['win_rate'] * 100:.0f}%"
        print(f"虚拟交易（强推）：建仓单 {ps['orders']} · 成交 {ps['filled']} · 平仓 {ps['closed']} · "
              f"胜率 {wr} · 平均 {_pct(ps['avg_ret'])} · {verdict}")
    return 0


def cmd_quote(args) -> int:
    """随手查行情：支持 300274 / sh000300 混写。"""
    codes = [c.strip() for c in args.codes.replace("，", ",").split(",") if c.strip()]
    if not codes:
        _log("用法：astocks quote 300274,002460,sh000300")
        return 1

    quotes, src_name = _quotes(codes, getattr(args, "src", "auto"))
    if not quotes:
        _log("没拿到行情，检查代码是否正确（指数需带 sh/sz 前缀）")
        return 1

    print(f"\n数据源：{src_name}")
    print(f"{'代码':<10}{'名称':<9}{'现价':>8}{'涨跌':>9}{'开盘':>8}{'最高':>8}{'最低':>8}"
          f"{'换手':>7}{'量比':>6}{'PE':>8}{'PB':>6}{'总市值(亿)':>10}  时间")
    print("-" * 104)
    for q in quotes:
        print(f"{q.get('secCode', ''):<10}{(q.get('name') or '')[:8]:<9}"
              f"{_fmt(q.get('price'), 2):>8}{_pct(q.get('changePct')):>9}"
              f"{_fmt(q.get('open'), 2):>8}{_fmt(q.get('high'), 2):>8}{_fmt(q.get('low'), 2):>8}"
              f"{_fmt(q.get('turnover'), 2):>7}{_fmt(q.get('volumeRatio'), 2):>6}"
              f"{_fmt(q.get('peTTM'), 1):>8}{_fmt(q.get('pb'), 2):>6}"
              f"{_fmt(q.get('totalCap'), 1):>10}  {q.get('time', '')[:8]} {q.get('time', '')[8:12]}")
    print()
    return 0


def _quotes(codes: list[str], src: str = "auto"):
    """统一取价：auto 优先站点、失败退腾讯；也可强制指定。"""
    if src == "tencent":
        return tencent.fetch_quotes(codes), "腾讯财经 qt.gtimg.cn"
    if src == "site":
        return source.fetch_quotes(codes), "磐石选股 /api/quote"
    try:
        q = source.fetch_quotes(codes)
        if q:
            return q, "磐石选股 /api/quote"
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠  站点行情不可用（{exc}），切换腾讯备用源")
    return tencent.fetch_quotes(codes), "腾讯财经 qt.gtimg.cn（备用）"


def _fmt(v, digits: int = 2) -> str:
    return "-" if not isinstance(v, (int, float)) else f"{v:.{digits}f}"


def _pct(v) -> str:
    return "-" if not isinstance(v, (int, float)) else f"{v:+.2f}%"


def cmd_context(args) -> int:
    """把 MCP 拉到的市场环境（板块/资金流）写入库，供报告与看板使用。"""
    conn = store.init_db()
    day = args.date
    if not day:
        latest = store.latest_days(conn, limit=1)
        day = latest[0]["trade_date"] if latest else None
    if not day:
        _log("库里还没有交易日记录，先跑 open 或用 --date 指定")
        return 1

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    store.upsert_context(conn, day, args.kind, payload, args.source)
    conn.commit()
    _log(f"📥 {day} · {args.kind} 已入库")

    p = report.write_dashboard(conn, source=args.source_mode)
    report.close_report(conn, day, source=args.source_mode)
    _log(f"📊 已重建看板：{p}")
    return 0


def cmd_minutes(args) -> int:
    """把连接器拉到的分钟线写入库，供虚拟交易判定止盈止损先后。"""
    conn = store.init_db()
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    # 兼容三种写法：{code: [nodes]}、{code: {"nodes": [...]}}、{"code":..,"nodes":[...]}
    if "nodes" in payload:
        payload = {payload.get("code") or args.code: payload}
    total, codes = 0, 0
    for code, val in payload.items():
        nodes = val.get("nodes") if isinstance(val, dict) else val
        if not nodes:
            continue
        n = bars.save_minutes(conn, code, nodes, src=args.source)
        total += n
        codes += 1
        lo, hi, cnt = bars.minutes_span(conn, code)
        _log(f"🕒 {code} 写入 {n} 根分钟线（库内 {cnt} 根 · {lo}~{hi}）")
    _log(f"🕒 分钟线入库：{codes} 只 / {total} 根")
    return 0 if total else 1


def cmd_site(args) -> int:
    """生成可分享的多日回溯单页站。"""
    conn = store.init_db()
    p = report.write_site(conn, args.out, source=args.source)
    _log(f"🌐 站点：{p}")
    return 0


def cmd_export(args) -> int:
    """导出机读 JSON（供外部站点/看板消费）。"""
    conn = store.init_db()
    res = export.write(conn, args.out, source=args.source)
    _log(f"📦 数据导出：{res['days']} 个交易日 / {res['files']} 个文件 → {res['dir']}")
    _log(f"📦 索引：{res['index']}")
    return 0


def cmd_push(args) -> int:
    """把 site/data 的 JSON 同步到 docs/ 并推送到 GitHub Pages。"""
    if args.export:
        conn = store.init_db()
        res = export.write(conn, source=args.source)
        _log(f"📦 数据导出：{res['days']} 个交易日 → {res['dir']}")
    rec = sitepush.run(trade_date=args.date, dry_run=args.dry_run, force=args.force, log=_log)
    if rec.get("ok"):
        _log("📡 站点同步完成" + ("（已推送）" if rec.get("pushed") else f"（{rec.get('reason')}）"))
        return 0
    _log(f"⚠  站点同步未完成：{rec.get('reason')} {rec.get('error') or ''}".rstrip())
    return 1


def cmd_report(args) -> int:
    conn = store.init_db()
    day = args.date
    if day:
        report.open_report(conn, day)
        report.close_report(conn, day, source=args.source)
    report.strategy_report(conn)
    p = report.write_dashboard(conn, source=args.source, filename=args.out)
    _log(f"📊 看板：{p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="astocks", description="磐石选股 · 交易日跟踪复盘")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open", help="开盘记录推荐股并入库")
    p.add_argument("--date", help="YYYY-MM-DD，默认今天")
    p.add_argument("--force", action="store_true", help="覆盖当日已有记录")
    p.add_argument("--simulate", action="store_true", help="用日线开盘价模拟记录价")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("close", help="收盘复盘并更新策略统计")
    p.add_argument("--date", help="YYYY-MM-DD，默认最近交易日")
    p.add_argument("--sim", action="store_true", help="连带模拟记录一起统计")
    p.add_argument("--recheck", action="store_true",
                   help="全量重算：重拉所有历史日线并重评全部记录（修历史数据污染）")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("simulate", help="回放某日全链路（验证用）")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("status", help="查看记录与策略评级")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("paper", help="虚拟交易：按建议买卖点纸面执行并评估")
    p.add_argument("--rebuild", action="store_true", help="清空后按历史推荐全量重放")
    p.add_argument("--asof", help="结算截止日 YYYY-MM-DD，默认最近已收盘交易日")
    p.add_argument("--view", default="strong", choices=["strong", "all"],
                   help="统计口径：strong=仅强推（默认） / all=全部评级")
    p.add_argument("--start-phase", nargs="?", const="today", metavar="DATE",
                   help="开启新一期模拟跟踪（默认从今天起），并清空重建账本")
    p.add_argument("--end", help="跟踪期结束日 YYYY-MM-DD，默认起始日 +30 天")
    p.add_argument("--name", help="跟踪期名称，默认「第N期」")
    p.add_argument("--offline", action="store_true", help="只用本地日线库，不联网重放")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("bars", help="本地日线库：补数 / 体检")
    p.add_argument("--codes", help="逗号分隔代码，默认补全部推荐过的股票 + 基准")
    p.add_argument("--force", action="store_true", help="忽略「每只每日一次」限制，强制重拉")
    p.set_defaults(func=cmd_bars)

    p = sub.add_parser("snapshot", help="为历史推荐补建不可变快照")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("quote", help="随手查行情（个股/指数）")
    p.add_argument("codes", help="逗号分隔，如 300274,002460,sh000300")
    p.add_argument("--src", default="auto", choices=["auto", "site", "tencent"],
                   help="行情源：auto=站点优先、腾讯兜底")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("context", help="写入市场环境数据（板块/资金流，来自连接器）")
    p.add_argument("--kind", required=True, choices=["sectors", "fundflow", "profile"])
    p.add_argument("--file", help="JSON 文件路径；缺省从 stdin 读")
    p.add_argument("--date", help="YYYY-MM-DD，默认库内最新交易日")
    p.add_argument("--source", default="westock/ifind", help="数据来源备注")
    p.add_argument("--source-mode", default="live", choices=["live", "all"],
                   help="重建看板时的统计口径")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("minutes", help="写入分钟线（5 分钟），用于判定止盈止损先后")
    p.add_argument("--file", help="JSON 文件路径；缺省从 stdin 读")
    p.add_argument("--code", help="单只代码（payload 里没有 code 字段时用）")
    p.add_argument("--source", default="westock", help="数据来源备注")
    p.set_defaults(func=cmd_minutes)

    p = sub.add_parser("site", help="生成可分享的多日回溯单页站（site/index.html）")
    p.add_argument("--out", help="输出路径，默认 site/index.html")
    p.add_argument("--source", default="live", choices=["live", "all"])
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("export", help="导出机读 JSON（index.json + 每日明细），供外部站点消费")
    p.add_argument("--out", help="输出目录，默认 site/data")
    p.add_argument("--source", default="live", choices=["live", "all"])
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("push", help="同步导出 JSON 到 docs/ 并推送到 GitHub Pages")
    p.add_argument("--date", help="提交信息里的交易日，默认不写")
    p.add_argument("--source", default="live", choices=["live", "all"])
    p.add_argument("--export", action="store_true", help="推送前先重新导出")
    p.add_argument("--dry-run", action="store_true", help="只看会同步哪些文件，不复制不推送")
    p.add_argument("--force", action="store_true", help="内容无变化也提交（调试用）")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("report", help="重新生成报告与看板")
    p.add_argument("--date")
    p.add_argument("--source", default="live", choices=["live", "all"])
    p.add_argument("--out", default="dashboard.html")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
