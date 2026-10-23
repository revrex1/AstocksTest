"""报告层：开盘记录单、收盘复盘单、滚动策略评估、HTML 看板。"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime

from . import bars, config, paper, stats, store


def _p(x, digits: int = 2) -> str:
    return "-" if x is None else f"{x * 100:+.{digits}f}%"


def _pct(x, digits: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def _num(x, digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------- 市场环境（连接器数据）

def _ctx_sectors(conn, day):
    ctx = store.get_context(conn, day, "sectors")
    if not ctx:
        return []
    rows = ctx.get("sectors") if isinstance(ctx, dict) else ctx
    return rows or []


def _ctx_fundflow(conn, day) -> dict:
    ctx = store.get_context(conn, day, "fundflow")
    if not ctx:
        return {}
    rows = ctx.get("stocks") if isinstance(ctx, dict) else ctx
    if isinstance(rows, dict):
        return rows
    return {r.get("code"): r for r in (rows or []) if r.get("code")}


def _env_md(conn, day) -> list[str]:
    sectors = _ctx_sectors(conn, day)
    funds = _ctx_fundflow(conn, day)
    if not sectors and not funds:
        return []
    out = ["", "## 市场环境", ""]
    if sectors:
        out += ["| 板块 | 涨跌幅 | 主力净流入(亿) | 领涨股 |", "|---|---|---|---|"]
        for r in sectors[:8]:
            inflow = r.get("mainNetInflowYi")
            out.append(f"| {r.get('name', '-')} | {_p(r.get('changePct') / 100 if isinstance(r.get('changePct'), (int, float)) else None)} | "
                       f"{_num(inflow, 2)} | {r.get('leader', '-')} |")
    if funds:
        picks = store.get_picks(conn, day)
        rows = [(p["code"], p["name"], funds.get(p["code"])) for p in picks if funds.get(p["code"])]
        if rows:
            out += ["", "| 推荐股主力资金 | 当日净流入(亿) | 5日(亿) |", "|---|---|---|"]
            for code, name, f in rows:
                out.append(f"| {code} {name} | {_num(f.get('mainNetInflowYi'), 2)} | "
                           f"{_num(f.get('mainNetInflow5dYi'), 2)} |")
    out.append("")
    return out


def _env_html(conn, day) -> str:
    sectors = _ctx_sectors(conn, day)
    funds = _ctx_fundflow(conn, day)
    if not sectors and not funds:
        return ""
    blocks = []
    if sectors:
        rows = "".join(
            f"<tr><td class='l'>{html.escape(str(r.get('name', '-')))}</td>"
            f"{_cell_pct((r.get('changePct') or 0) / 100 if r.get('changePct') is not None else None)}"
            f"<td>{_num(r.get('mainNetInflowYi'), 2)}</td>"
            f"<td class='l'>{html.escape(str(r.get('leader', '-')))}</td></tr>"
            for r in sectors[:8])
        blocks.append("<div class='panel'><table><thead><tr><th class='l'>行业板块</th><th>涨跌幅</th>"
                      "<th>主力净流入(亿)</th><th class='l'>领涨股</th></tr></thead><tbody>"
                      f"{rows}</tbody></table></div>")
    if funds:
        picks = store.get_picks(conn, day)
        rows = "".join(
            f"<tr><td class='l'>{p['code']} {html.escape(p['name'])}</td>"
            f"<td>{_num((funds.get(p['code']) or {}).get('mainNetInflowYi'), 2)}</td>"
            f"<td>{_num((funds.get(p['code']) or {}).get('mainNetInflow5dYi'), 2)}</td></tr>"
            for p in picks if funds.get(p["code"]))
        if rows:
            blocks.append("<div class='panel'><table><thead><tr><th class='l'>推荐股</th>"
                          "<th>主力净流入(亿)</th><th>5日(亿)</th></tr></thead><tbody>"
                          f"{rows}</tbody></table></div>")
    return "".join(blocks)


# --------------------------------------------------------------------------- Markdown

def open_report(conn, trade_date: str) -> str:
    day = store.get_day(conn, trade_date)
    picks = store.get_join(conn, trade_date)
    if not day:
        return ""
    lines = [
        f"# 开盘记录 · {trade_date}",
        "",
        f"- 记录时间：{day['recorded_at']}（{'模拟回放' if day['data_source'] == 'simulated' else '实时'}）",
        f"- 数据源：{config.BASE_URL}/picks",
        f"- 漏斗：全 A 池 {day['universe_size']} → 扫描 {day['scanned_count']} → 指标通过 "
        f"{day['indicator_passed']} → 研究池 {day['pool_size']}",
        f"- 推荐：{day['pick_count']} 只（强推 {day['strong_count']} / 可关注 {day['watch_count']}）",
        "",
    ]
    if not picks:
        lines += ["> 当日无推荐标的（漏斗未锁定）。空仓即策略的一种输出，收盘照常记 0 收益。", ""]
        return _write(os.path.join(config.REPORT_DIR, f"{trade_date}_open.md"), "\n".join(lines))

    lines += [
        "| # | 代码 | 名称 | 评级 | 综合分 | 可投性 | 记录价 | 止损 | 首目标 | 建议持有 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in picks:
        lines.append(
            f"| {p['rank']} | {p['code']} | {p['name']} | {p['rating']} | {_num(p['total_score'])} | "
            f"{_num(p['investability'])} | {_num(p['ref_price'], 2)} | {_num(p['stop_loss'], 2)} | "
            f"{_num(p['tp1'], 2)} | {p['holding_tag'] or '-'} {p['holding_days'] or ''}天 |"
        )
    lines += ["", "## 推荐理由", ""]
    for p in store.pick_meta_rows(picks):
        reasons = "；".join(p.get("reasons") or []) or "-"
        risks = "；".join(p.get("risks") or []) or "无"
        lines.append(f"- **{p['code']} {p['name']}**：{reasons}  \n  风险：{risks}")
    lines.append("")
    return _write(os.path.join(config.REPORT_DIR, f"{trade_date}_open.md"), "\n".join(lines))


def close_report(conn, trade_date: str, source: str = "live") -> str:
    day = store.get_day(conn, trade_date)
    picks = store.pick_meta_rows(store.get_join(conn, trade_date))
    stat = store.one(conn, "SELECT * FROM day_stat WHERE trade_date=?", (trade_date,))
    if day and day.get("data_source") == "simulated" and source == "live":
        source = "all"
    ev = stats.evaluate(conn, source=source)

    lines = [
        f"# 收盘复盘 · {trade_date}",
        "",
        f"- 复盘时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 基准：{config.BENCH_NAME} {_p(day['bench_pct']) if day else '-'}",
        f"- 当日推荐：{day['pick_count'] if day else 0} 只（强推 {day['strong_count'] if day else 0}）",
    ]
    if stat:
        lines += [
            f"- 当日表现：胜率 {_pct(stat['win_rate_d0'])}｜均收益 {_p(stat['avg_ret_d0'])}｜"
            f"中位数 {_p(stat['med_ret_d0'])}｜均超额 {_p(stat['avg_excess_d0'])}",
        ]
    lines.append("")

    if picks:
        lines += [
            "## 个股表现",
            "",
            "| # | 代码 | 名称 | 评级 | 记录价 | 收盘 | 当日 | 超额 | 最大浮盈 | 最大浮亏 | 触及止盈 | 跌破止损 | T+1 | T+3 | T+5 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for p in picks:
            lines.append(
                f"| {p['rank']} | {p['code']} | {p['name']} | {p['rating']} | {_num(p['ref_price'], 2)} | "
                f"{_num(p.get('d0_close'), 2)} | {_p(p.get('ret_d0'))} | {_p(p.get('excess_d0'))} | "
                f"{_p(p.get('mfe_d0'))} | {_p(p.get('mae_d0'))} | "
                f"{'✅' if p.get('hit_tp1') else ('—' if p.get('hit_tp1') is None else '❌')} | "
                f"{'⚠️' if p.get('hit_stop') else ('—' if p.get('hit_stop') is None else '✅')} | "
                f"{_p(p.get('ret_t1'))} | {_p(p.get('ret_t3'))} | {_p(p.get('ret_t5'))} |"
            )
    else:
        lines += ["## 个股表现", "", "> 当日无推荐，0 笔持仓，组合收益记 0。", ""]

    lines += _env_md(conn, trade_date)

    lines += [
        "",
        "## 策略可靠性（累计）",
        "",
        f"- 结论：**{ev['verdict']}** —— {ev['verdict_reason']}",
        f"- 样本：{ev['days']} 个交易日 / {ev['eval_picks']} 条可评估推荐",
        f"- 胜负：胜率 {_pct(ev['base']['win_rate'])}｜盈亏比 "
        f"{_num(ev['base']['profit_factor'], 2)}｜均盈利 {_p(ev['base']['avg_win'])}｜均亏损 {_p(ev['base']['avg_loss'])}",
        f"- 超额：超额胜率 {_pct(ev['base']['excess_win_rate'])}｜平均超额 {_p(ev['base']['avg_excess'])}",
        f"- 净值：策略 {_num(ev['nav'], 4)} vs 基准 {_num(ev['bench_nav'], 4)}"
        f"（超额 {_p(ev['excess_nav'])}）｜最大回撤 {_p(ev['max_dd'])}｜基准回撤 {_p(ev['bench_max_dd'])}",
        f"- 分层：强推胜率 {_pct((ev['by_rating'].get('强推') or {}).get('win_rate'))}｜"
        f"可关注胜率 {_pct((ev['by_rating'].get('可关注') or {}).get('win_rate'))}",
        "",
    ]
    return _write(os.path.join(config.REPORT_DIR, f"{trade_date}_close.md"), "\n".join(lines))


def strategy_report(conn, source: str = "live") -> str:
    ev = stats.evaluate(conn, source=source)
    if ev["eval_picks"] == 0:
        ev_all = stats.evaluate(conn, source="all")
        if ev_all["eval_picks"]:
            ev = ev_all
            source = "all"
    note = "，**当前仅模拟回放数据**" if source == "all" else ""
    lines = [
        "# 荐股策略可靠性评估",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 数据源：{config.BASE_URL}/picks（每日开盘记录 + 收盘核对）{note}",
        f"- 结论：**{ev['verdict']}** —— {ev['verdict_reason']}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---|---|",
        f"| 交易日数 | {ev['days']} | 有推荐记录的交易日 |",
        f"| 可评估推荐 | {ev['eval_picks']} | 已有 T+0 收益 |",
        f"| 当日胜率 | {_pct(ev['base']['win_rate'])} | 收盘价高于记录价的比例 |",
        f"| 平均收益 | {_p(ev['base']['avg'])} | 等权，记录价→当日收盘 |",
        f"| 收益中位数 | {_p(ev['base']['med'])} | 抗极值 |",
        f"| 盈亏比 | {_num(ev['base']['profit_factor'], 2)} | 总盈利/总亏损 |",
        f"| 超额胜率 | {_pct(ev['base']['excess_win_rate'])} | 跑赢{config.BENCH_NAME}的比例 |",
        f"| 平均超额 | {_p(ev['base']['avg_excess'])} | 相对基准 |",
        f"| 止盈触及率 | {_pct(ev['tp1_rate'])} | 当日最高价 ≥ 首目标 |",
        f"| 止损击穿率 | {_pct(ev['stop_rate'])} | 当日最低价 ≤ 止损位 |",
        f"| 策略净值 | {_num(ev['nav'], 4)} | 逐日等权复利 |",
        f"| 基准净值 | {_num(ev['bench_nav'], 4)} | {config.BENCH_NAME} |",
        f"| 最大回撤 | {_pct(ev['max_dd'])} | 策略 |",
        f"| 年化夏普 | {_num(ev['sharpe'], 2)} | 日收益序列 |",
        "",
        "## 分层对比",
        "",
        "| 分层 | 样本 | 胜率 | 平均收益 |",
        "|---|---|---|---|",
    ]
    for k, v in ev["by_rating"].items():
        lines.append(f"| {k} | {v['n']} | {_pct(v['win_rate'])} | {_p(v['avg'])} |")
    for k, v in ev["by_rank"].items():
        lines.append(f"| {k} | {v['n']} | {_pct(v['win_rate'])} | {_p(v['avg'])} |")
    lines += ["", "## 持有期表现", "", "| 区间 | 样本 | 胜率 | 平均收益 |", "|---|---|---|---|"]
    for k, v in ev["by_horizon"].items():
        lines.append(f"| {k} | {v['n']} | {_pct(v['win_rate'])} | {_p(v['avg'])} |")

    ph = paper.phase_status(conn)
    if ph:
        lines += [
            "",
            "## 模拟跟踪（强推 · 纸面执行）",
            "",
            f"- 跟踪期：{ph['name']} · {ph['start_date']} → {ph['end_date']}"
            f"（{ph['total_days']} 个交易日，按工作日估算）",
            f"- 进度：{ph['elapsed']}/{ph['total_days']}（{_pct(ph['progress'])}），"
            f"剩余 {ph['remaining']} 个交易日 · 结算截至 {ph['cutoff']}",
            f"- 建仓单 {ph['orders']} · 持仓 {ph['open']} · 平仓 {ph['closed']} · 待触及买点 {ph['pending']}",
            f"- 平仓胜率 {_pct(ph['win_rate'])} · 平均每笔 {_p(ph['avg_ret'])} · "
            f"累计已实现 {ph['total_pnl']:,.0f} 元",
            f"- 结论：**{ph['verdict']}** —— {ph['verdict_reason']}",
        ]
    lines.append("")
    return _write(os.path.join(config.REPORT_DIR, "strategy_review.md"), "\n".join(lines))


# --------------------------------------------------------------------------- 虚拟交易

EXIT_LABEL = {"take_profit": "止盈", "stop": "止损", "timeout": "到期", "expired": "未成交",
              "gap_below_stop": "跳空破位", "duplicate": "已有持仓"}


def _view_suffix(view: str) -> str:
    return "" if view == "strong" else "_all"


def _avg_sell(t) -> float | None:
    sells = [e for e in t["execs"] if e["side"] == "sell"]
    qty = sum(e["qty"] for e in sells)
    return (sum(e["amount"] for e in sells) / qty) if qty else None


def _tp_progress(t) -> str:
    if not t["tp_prices"]:
        return "-"
    done = sum(1 for e in t["execs"] if e["side"] == "sell" and (e["reason"] or "").startswith("tp"))
    return f"{done}/{len(t['tp_prices'])}"


def paper_report(conn, view: str = "strong", asof: str | None = None) -> str:
    sm = paper.summary(conn, view)
    rows = paper.ledger(conn, view)
    closed = [r for r in rows if r["status"] == "closed"]
    opened = [r for r in rows if r["status"] == "open"]
    pending = [r for r in rows if r["status"] == "pending"]
    expired = [r for r in rows if r["status"] == "expired"]
    scope = "强推" if view != "all" else "全部评级"
    verdict, reason = paper._verdict(sm["closed"], sm["win_rate"], sm["avg_ret"])
    acct = sm["account"]
    curve = paper.equity_curve(conn)
    dd_txt = f"{sm['max_dd_yuan']:,.0f} 元" if closed else "—"
    bar_sm = bars.summary(conn)
    ph = paper.phase_status(conn, asof) if view == "strong" else None
    phase_lines = []
    if ph:
        phase_lines = [
            f"- 跟踪期：{ph['name']} · {ph['start_date']} → {ph['end_date']}"
            f"（{ph['total_days']} 个交易日）",
            f"- 进度：已过 {ph['elapsed']}/{ph['total_days']}（{_pct(ph['progress'])}），"
            f"剩余 {ph['remaining']} 个交易日 · 结算截至 {ph['cutoff']}",
            f"- 期初基准：{config.BENCH_NAME} {_num(ph['bench_base'], 2)}"
            f"（{ph['bench_date']} 收盘）",
        ]

    lines = [
        f"# 虚拟交易台账 · {scope}",
        "",
        f"- 截至：{asof or (max((r['last_date'] or r['trade_date'] for r in rows), default='—'))}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 规则：买点 `primaryEntry` 挂限价单，有效 {config.PAPER_ENTRY_VALID_DAYS} 个交易日；"
        f"止损 `stopLoss`（无效时退化为次买点）；止盈 `takeProfits` 三档各减 1/3；"
        f"持满 `holdingDays` 剩余仓位按收盘平仓",
        f"- 费用：佣金 {config.PAPER_FEE_BPS / 100:.3f}%（最低 {config.PAPER_MIN_FEE:.0f} 元）"
        f"＋卖出印花税 {config.PAPER_STAMP_BPS / 100:.2f}%＋过户费 {config.PAPER_TRANSFER_BPS / 100:.4f}%",
        f"- 资金：总资金 {config.PAPER_CAPITAL:,.0f} 元，最多同时持 {config.PAPER_MAX_POSITIONS} 只；"
        f"以「手」下单，手数由目标金额反推（每只约 {config.PAPER_CAPITAL * config.PAPER_TARGET_PCT:,.0f} 元），"
        f"单笔不超过总资金 {config.PAPER_MAX_POSITION_PCT:.0%}",
        *phase_lines,
        "",
        "## 策略结论",
        "",
        f"- **{verdict}** —— {reason}",
        "",
        "## 资金账户",
        "",
        "| 项目 | 金额（元） | 占总资金 |",
        "|---|---|---|",
        f"| 总资金 | {acct['capital']:,.0f} | 100% |",
        f"| 现金 | {acct['cash']:,.0f} | {_pct(acct['cash'] / acct['capital'] if acct['capital'] else None)} |",
        f"| 持仓成本 | {acct['open_cost']:,.0f} | {_pct(acct['open_cost'] / acct['capital'] if acct['capital'] else None)} |",
        f"| 持仓市值 | {acct['open_value']:,.0f} | {_pct(acct['open_value'] / acct['capital'] if acct['capital'] else None)} |",
        f"| 浮动盈亏 | {acct['unrealized']:+,.0f} | {_pct(acct['unrealized'] / acct['open_cost'] if acct['open_cost'] else None)} |",
        f"| 已实现盈亏 | {acct['realized']:+,.0f} | |",
        f"| **总资产** | **{acct['equity']:,.0f}** | {_pct(acct['total_return'])} |",
        f"| 仓位（市值/总资产） | {_pct(acct['exposure'])} | 剩余 {acct['max_positions'] - len(acct['positions'])} 个持仓槽位 |",
        "",
        f"## 每日账户（{acct['capital']:,.0f} 元本金逐日结算）",
        "",
        "| 日期 | 现金 | 持仓市值 | 总资产 | 当日盈亏 | 当日收益 | 累计收益 | 持仓数 |",
        "|---|---|---|---|---|---|---|---|",
        *[f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} |"
          for r in _account_rows(curve, acct["capital"])],
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---|---|",
        f"| 建仓单数 | {sm['orders']} | 每个推荐一条 |",
        f"| 已成交 | {sm['filled']} | 成交率 {_pct(sm['fill_rate'])} |",
        f"| 已平仓 / 持仓中 / 未成交失效 | {sm['closed']} / {sm['open']} / {sm['expired']} | 待触及买点 {sm['pending']} 笔 |",
        f"| 平仓胜率 | {_pct(sm['win_rate'])} | 已实现收益 > 0 的比例 |",
        f"| 平均每笔收益 | {_p(sm['avg_ret'])} | 生效单笔本金，含费用 |",
        f"| 收益中位数 | {_p(sm['med_ret'])} | 抗极值 |",
        f"| 盈亏比 | {_num(sm['profit_factor'], 2)} | 总盈利/总亏损 |",
        f"| 平均盈利 / 平均亏损 | {_p(sm['avg_win'])} / {_p(sm['avg_loss'])} | |",
        f"| 累计已实现盈亏 | {sm['total_pnl']:,.0f} 元 | 已平仓合计 |",
        f"| 平均超额 | {_p(sm['avg_excess'])} | 相对{config.BENCH_NAME}同区间，样本 {sm['excess_n']} |",
        f"| 平均持有 | {_num(sm['avg_hold'], 0)} 个交易日 | |",
        f"| 已实现回撤 | {dd_txt} | 累计盈亏的峰谷差 |",
        f"| 退出分布 | {'、'.join(f'{EXIT_LABEL.get(k, k)} {v}' for k, v in sm['exit_reasons'].items()) or '—'} | |",
        "",
    ]

    lines += [
        "## 执行质量",
        "",
        "| 诊断项 | 数值 | 读法 |",
        "|---|---|---|",
        f"| 买点成交率 | {_pct(sm['fill_rate'])} | 偏低＝买点挂得太保守，或推荐后直接单边走高 |",
        f"| 止损触发占比 | {_pct(sm['stop_rate'])} | 偏高＝止损太紧或入场位置差 |",
        f"| 任一档止盈触达率 | {_pct(sm['tp_touch_rate'])} | 偏低＝目标位定得太远 |",
        f"| 到期平仓占比 | {_pct(sm['timeout_rate'])} | 偏高＝区间震荡，趋势判断失效 |",
        f"| 平均最大浮盈 / 浮亏 | {_p(sm['avg_mfe'])} / {_p(sm['avg_mae'])} | 浮盈远高于实现收益＝拿不住 |",
        f"| 平均持有 | {_num(sm['avg_hold'], 0)} 个交易日 | 与建议持有期对比 |",
        "",
    ]
    if sm["by_rating"]:
        lines += ["## 分层（按评级）", "", "| 评级 | 平仓笔数 | 胜率 | 平均收益 |", "|---|---|---|---|"]
        for k, v in sm["by_rating"].items():
            lines.append(f"| {k} | {v['n']} | {_pct(v['win_rate'])} | {_p(v['avg'])} |")
        lines.append("")

    if acct["positions"]:
        lines += ["## 当前持仓（按最新收盘价估值）", "",
                  "| 代码 | 名称 | 评级 | 建仓日 | 成交价 | 数量 | 现价 | 成本 | 市值 | 浮盈 | 浮动 | 含费浮动 | 止损 | 止盈进度 | 持有 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in acct["positions"]:
            lines.append(
                f"| {r['code']} | {r['name']} | {r['rating']} | {r['fill_date']} | {_num(r['fill_price'], 2)} | "
                f"{r['qty']} | {_num(r['last_price'], 2)} | {r['cost']:,.0f} | {r['value']:,.0f} | "
                f"{r['pnl']:+,.0f} | {_p(r['pnl_pct'])} | {_p(r['ret_pct'])} | {_num(r['stop_price'], 2)} | "
                f"{_tp_progress(r)} | {r['hold_days']}/{r['holding_days']} |")
        lines.append("")

    if closed:
        lines += ["## 已平仓", "",
                  "| 代码 | 名称 | 评级 | 成交日 | 成交价 | 平仓日 | 均价 | 收益 | 超额 | 持有 | 退出 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(closed, key=lambda x: x["exit_date"] or ""):
            lines.append(
                f"| {r['code']} | {r['name']} | {r['rating']} | {r['fill_date']} | {_num(r['fill_price'], 2)} | "
                f"{r['exit_date']} | {_num(_avg_sell(r), 2)} | {_p(r['ret_pct'])} | {_p(r['excess'])} | "
                f"{r['hold_days']}日 | {EXIT_LABEL.get(r['exit_reason'], r['exit_reason'])} |")
        lines.append("")

    if pending or expired:
        lines += ["## 未成交", "", "| 代码 | 名称 | 评级 | 推荐日 | 挂单价 | 状态 |", "|---|---|---|---|---|---|"]
        for r in rows:
            if r["status"] not in ("pending", "expired", "skipped"):
                continue
            label = {"pending": "等回踩", "expired": EXIT_LABEL.get(r["exit_reason"], "超期撤单"),
                     "skipped": "已有持仓，未建仓"}[r["status"]]
            lines.append(f"| {r['code']} | {r['name']} | {r['rating']} | {r['trade_date']} | "
                         f"{_num(r['entry_target'], 2)} | {label} |")
        lines.append("")

    adjusted, seen = [], set()
    for r in rows:
        notes = json.loads(r["plan_json"]).get("notes") or []
        key = (r["code"], tuple(notes))
        if notes and key not in seen:
            seen.add(key)
            adjusted.append((r["code"], r["name"], notes))
    if adjusted:
        lines += ["## 执行偏差（计划参数被修正的记录）", ""]
        for code, name, notes in adjusted:
            lines.append(f"- **{code} {name}**：{'；'.join(notes)}")
        lines.append("")

    lines += ["## 口径说明", "",
              "1. 买点只按 `primaryEntry` 挂单，未做二次加仓；买点高于开盘价时按开盘价成交（不会追高）。",
              "2. 持仓日有 5 分钟线时按分钟路径判定止盈止损先后（`path_src=minute`）；"
              "缺分钟线时退化为「同日先止损后止盈」的保守口径（`path_src=daily`）。",
              f"3. 滑点：买入价受限价保护（不高于买点），卖出价按触发价再让 {config.PAPER_SLIP_BPS:.0f}bp。",
              "4. 约束：A 股 T+1（建仓当日不卖）、一字跌停卖不出、一字涨停买不进、停牌日不撮合。",
              "5. 本台账不参与选股参数调整，仅记录「按建议执行」的结果。",
              f"6. 数据底座：本地日线库 {bar_sm['bars']:,} 根 / {bar_sm['codes']} 只，"
              f"{bar_sm['from'] or '—'} → {bar_sm['to'] or '—'}；`--offline` 可完全离线重放。",
              "7. 建仓依据来自 `pick_snapshot` 快照（不可变），推荐被覆盖也不影响历史回溯。",
              ""]
    return _write(os.path.join(config.REPORT_DIR, f"paper_trading{_view_suffix(view)}.md"), "\n".join(lines))


def _svg_trade_bars(closed: list[dict], w: int = 1100, h: int = 180) -> str:
    if not closed:
        return '<div class="empty">暂无平仓样本</div>'
    ds = sorted(closed, key=lambda r: r["exit_date"] or "")
    pad_l, pad_r, pad_t, pad_b = 52, 12, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    peak = max(max(abs(r["ret_pct"] or 0) for r in ds), 0.01)
    bw = min(56.0, max(4.0, iw / len(ds) * 0.55))
    zero = pad_t + ih / 2
    bars = []
    for i, r in enumerate(ds):
        cx = pad_l + iw * (i + 0.5) / len(ds)
        v = r["ret_pct"] or 0
        hh = abs(v) / peak * (ih / 2 - 4)
        y0 = zero - hh if v > 0 else zero
        color = "#d9363e" if v > 0 else "#12965a"
        bars.append(f'<rect x="{cx - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(hh, 0.8):.1f}" '
                    f'fill="{color}" opacity=".78" rx="1.5"><title>{r["code"]} {r["name"]} {v * 100:+.2f}% '
                    f'{r["fill_date"]}→{r["exit_date"]}</title></rect>')
    ticks = "".join(
        f'<text x="{pad_l - 8}" y="{zero - v / peak * (ih / 2 - 6) + 4:.1f}" text-anchor="end" font-size="10" '
        f'fill="#9aa1ac">{v * 100:+.1f}%</text>'
        for v in (peak, peak / 2, -peak / 2, -peak))
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" style="height:auto;display:block">
<line x1="{pad_l}" y1="{zero:.1f}" x2="{w - pad_r}" y2="{zero:.1f}" stroke="#e3e6ea"/>
{ticks}{''.join(bars)}
<text x="{pad_l}" y="{h - 8}" font-size="10" fill="#9aa1ac">逐笔平仓收益（{len(ds)} 笔）</text>
</svg>"""


def paper_page(conn, view: str = "strong") -> str:
    sm = paper.summary(conn, view)
    rows = paper.ledger(conn, view)
    closed = [r for r in rows if r["status"] == "closed"]
    opened = [r for r in rows if r["status"] == "open"]
    pending = [r for r in rows if r["status"] == "pending"]
    verdict, reason = paper._verdict(sm["closed"], sm["win_rate"], sm["avg_ret"])
    scope = "强推" if view != "all" else "全部评级"
    ph = paper.phase_status(conn) if view == "strong" else None
    phase_txt = ""
    if ph:
        phase_txt = (f"<br>跟踪期 <b>{html.escape(ph['name'])}</b> · {ph['start_date']} → {ph['end_date']}"
                     f" · 进度 {ph['elapsed']}/{ph['total_days']} 个交易日（剩余 {ph['remaining']}）"
                     f" · 期初基准 {config.BENCH_NAME} {_num(ph['bench_base'], 2)} @{ph['bench_date']}")

    def card(k, v, cls=""):
        return f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'

    acct = sm["account"]
    curve = paper.equity_curve(conn)
    day_rows = "".join(
        f"<tr><td class='l'>{d['trade_date']}</td><td>{d['cash']:,.0f}</td>"
        f"<td>{d['open_value']:,.0f}</td><td><b>{d['equity']:,.0f}</b></td>"
        f"<td class=\"{'up' if d['pnl_d0'] >= 0 else 'down'}\">{d['pnl_d0']:+,.0f}</td>"
        f"{_cell_pct(d['ret_d0'])}{_cell_pct(d['cum_ret'])}<td>{d['positions']}</td></tr>"
        for d in reversed(curve)) or "<tr><td colspan='8' class='muted'>暂无</td></tr>"
    cards = "".join([
        card("总资产", f'{acct["equity"]:,.0f} 元', "up" if (acct["total_return"] or 0) >= 0 else "down"),
        card("账户收益", _p(acct["total_return"]), "up" if (acct["total_return"] or 0) >= 0 else "down"),
        card("浮动盈亏", f'{acct["unrealized"]:+,.0f} 元', "up" if acct["unrealized"] >= 0 else "down"),
        card("已实现", f'{acct["realized"]:+,.0f} 元', "up" if acct["realized"] >= 0 else "down"),
        card("仓位", _pct(acct["exposure"])),
        card("平仓胜率", _pct(sm["win_rate"])),
        card("盈亏比", _num(sm["profit_factor"], 2)),
        card("样本", f'{sm["closed"]} 平仓 / {sm["open"]} 持仓'),
    ])

    pos_html = "".join(
        f"<tr><td class='l'>{r['code']}</td><td class='l'>{html.escape(r['name'] or '')}</td>"
        f"<td class='l'>{html.escape(r['rating'] or '')}</td><td class='l'>{r['fill_date']}</td>"
        f"<td>{_num(r['fill_price'], 2)}</td><td>{r['qty']}</td><td>{_num(r['last_price'], 2)}</td>"
        f"<td>{r['cost']:,.0f}</td><td>{r['value']:,.0f}</td>"
        f"<td class=\"{'up' if r['pnl'] >= 0 else 'down'}\">{r['pnl']:+,.0f}</td>"
        f"{_cell_pct(r['pnl_pct'])}{_cell_pct(r['ret_pct'])}<td>{_num(r['stop_price'], 2)}</td>"
        f"<td>{_tp_progress(r)}</td><td>{r['hold_days']}/{r['holding_days']}</td></tr>"
        for r in acct["positions"]) or "<tr><td colspan='15' class='muted'>当前无持仓</td></tr>"

    closed_html = "".join(
        f"<tr><td class='l'>{r['code']}</td><td class='l'>{html.escape(r['name'] or '')}</td>"
        f"<td class='l'>{html.escape(r['rating'] or '')}</td><td class='l'>{r['fill_date']}</td>"
        f"<td>{_num(r['fill_price'], 2)}</td><td class='l'>{r['exit_date']}</td>"
        f"<td>{_num(_avg_sell(r), 2)}</td>{_cell_pct(r['ret_pct'])}{_cell_pct(r['excess'])}"
        f"<td>{r['hold_days']}日</td><td class='l'>{EXIT_LABEL.get(r['exit_reason'], r['exit_reason'] or '-')}</td></tr>"
        for r in sorted(closed, key=lambda x: x["exit_date"] or "", reverse=True)
    ) or "<tr><td colspan='11' class='muted'>暂无平仓</td></tr>"

    pend_html = "".join(
        f"<tr><td class='l'>{r['code']}</td><td class='l'>{html.escape(r['name'] or '')}</td>"
        f"<td class='l'>{html.escape(r['rating'] or '')}</td><td class='l'>{r['trade_date']}</td>"
        f"<td>{_num(r['entry_target'], 2)}</td><td class='l'>"
        f"{ {'pending': '等回踩', 'skipped': '已有持仓'}.get(r['status'], EXIT_LABEL.get(r['exit_reason'], '超期撤单')) }</td></tr>"
        for r in rows if r["status"] in ("pending", "expired", "skipped")) or \
        "<tr><td colspan='6' class='muted'>无</td></tr>"

    vcls = "up" if verdict in ("可沿用",) else ("down" if verdict == "需修正" else "flat")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>虚拟交易台账 · {scope}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>虚拟交易台账 · {scope}</h1>
<div class="sub">严格按推荐附带的买点/止损/止盈/持有期执行 · 总资金 {config.PAPER_CAPITAL:,.0f} 元
 · 最多持 {config.PAPER_MAX_POSITIONS} 只 · 以手下单、每只约 {config.PAPER_CAPITAL * config.PAPER_TARGET_PCT:,.0f} 元（等权）
 · 含佣金与印花税 · 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}{phase_txt}</div>

<div class="cards">{cards}</div>
<div class="banner"><b class="{vcls}">策略结论：{verdict}</b> —— {html.escape(reason)}</div>

<h2>资金账户</h2>
<div class="panel"><table><thead><tr><th class="l">项目</th><th>金额（元）</th><th>说明</th></tr></thead><tbody>
<tr><td class="l">总资金</td><td>{acct["capital"]:,.0f}</td><td class="l">初始本金</td></tr>
<tr><td class="l">现金</td><td>{acct["cash"]:,.0f}</td><td class="l">可动用于下一笔建仓</td></tr>
<tr><td class="l">持仓成本</td><td>{acct["open_cost"]:,.0f}</td><td class="l">{len(acct["positions"])} / {acct["max_positions"]} 个槽位占用</td></tr>
<tr><td class="l">持仓市值</td><td>{acct["open_value"]:,.0f}</td><td class="l">按 {acct["positions"][0]["last_date"] if acct["positions"] else "—"} 收盘价</td></tr>
<tr><td class="l"><b>总资产</b></td><td><b>{acct["equity"]:,.0f}</b></td><td class="l">现金 + 持仓市值</td></tr>
<tr><td class="l">账户累计收益</td><td class="{'up' if (acct["total_return"] or 0) >= 0 else 'down'}">{_p(acct["total_return"])}</td><td class="l">含浮动的口径</td></tr>
</tbody></table></div>

<h2>每日账户曲线（{acct["capital"]:,.0f} 元本金 · 强推策略）</h2>
<div class="panel">{_svg_account(curve, acct["capital"])}</div>

<h2>逐日结算</h2>
<div class="panel"><table><thead><tr><th class="l">日期</th><th>现金</th><th>持仓市值</th>
<th>总资产</th><th>当日盈亏</th><th>当日收益</th><th>累计收益</th><th>持仓数</th></tr></thead>
<tbody>{day_rows}</tbody></table></div>

<h2>当前持仓（浮动盈亏）</h2>
<div class="panel"><table><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">评级</th>
<th class="l">建仓日</th><th>成交价</th><th>数量</th><th>现价</th><th>成本</th><th>市值</th><th>浮盈</th>
<th>浮动</th><th>含费浮动</th><th>止损</th><th>止盈进度</th><th>持有</th></tr></thead><tbody>{pos_html}</tbody></table></div>

<h2>逐笔平仓收益</h2>
<div class="panel">{_svg_trade_bars(closed)}</div>

<h2>已平仓</h2>
<div class="panel"><table><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">评级</th>
<th class="l">成交日</th><th>成交价</th><th class="l">平仓日</th><th>均价</th><th>收益</th><th>超额</th>
<th>持有</th><th class="l">退出</th></tr></thead><tbody>{closed_html}</tbody></table></div>

<h2>未成交</h2>
<div class="panel"><table><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">评级</th>
<th class="l">推荐日</th><th>挂单价</th><th class="l">状态</th></tr></thead><tbody>{pend_html}</tbody></table></div>

<div class="note">
规则来源：每条推荐的 <code>suggestion</code> 字段，本页不新增任何选股/择时参数。<br>
执行口径：买点挂限价单（有效期 {config.PAPER_ENTRY_VALID_DAYS} 个交易日，逾期撤单）；止损位无效（缺失或高于买点）时退化为次买点；
止盈三档各减 1/3；持满 <code>holdingDays</code> 剩余仓位按收盘平仓。同日止损优先于止盈。<br>
费用：佣金 {config.PAPER_FEE_BPS / 100:.3f}%（最低 {config.PAPER_MIN_FEE:.0f} 元）、卖出印花税
{config.PAPER_STAMP_BPS / 100:.2f}%、过户费 {config.PAPER_TRANSFER_BPS / 100:.4f}%；未计滑点与涨跌停无法成交。<br>
本页仅为纪律执行记录，不构成投资建议；样本 &lt; {config.PAPER_MIN_SAMPLE} 笔时不做策略结论。<br>
数据底座：本地日线库 {bars.summary(conn)['bars']:,} 根 / {bars.summary(conn)['codes']} 只
（{bars.summary(conn)['from'] or '—'} → {bars.summary(conn)['to'] or '—'}）；建仓依据为不可变快照。
</div>
</div></body></html>"""


def audit_report(conn, start: str = "2026-10-19", end: str | None = None) -> str:
    from . import audit as _audit
    return _write(os.path.join(config.REPORT_DIR, "paper_audit.md"),
                  _audit.markdown(conn, start=start, end=end))


def write_paper_site(conn, view: str = "strong") -> str:
    """生成发布用的交易台账页（dist/paper.html → docs/paper.html）。"""
    html = paper_page(conn, view)
    nav = ('<div class="sub" style="margin-bottom:10px">'
           '<a href="index.html">← 返回复盘首页</a>　·　'
           '<a href="data/paper.json">机读数据 paper.json</a></div>')
    html = html.replace("<h1>", nav + "<h1>", 1)
    return _write(os.path.join(config.ROOT, "dist", "paper.html"), html)


def write_paper_page(conn, view: str = "strong") -> str:
    return _write(os.path.join(config.REPORT_DIR, f"paper_trading{_view_suffix(view)}.html"),
                  paper_page(conn, view))


# --------------------------------------------------------------------------- HTML

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1a1d21;font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.3px}
h2{font-size:15px;margin:26px 0 12px;padding-left:9px;border-left:3px solid #2f6fed}
.sub{color:#6b7280;font-size:12.5px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:12px;margin:18px 0 6px}
.card{background:#fff;border:1px solid #e7e9ee;border-radius:10px;padding:13px 15px}
.card .k{font-size:12px;color:#6b7280}
.card .v{font-size:20px;font-weight:600;margin-top:5px;font-variant-numeric:tabular-nums}
.up{color:#d9363e}.down{color:#12965a}.flat{color:#6b7280}
.banner{margin-top:16px;padding:14px 17px;border-radius:10px;background:#eef3ff;border:1px solid #d3e0ff}
.banner b{font-size:15px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e7e9ee;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #f0f1f4;white-space:nowrap}
th{background:#fafbfc;color:#5b6270;font-weight:500;font-size:12px;text-align:right}
th:first-child,td:first-child{text-align:left}
th.l,td.l{text-align:left}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#fbfcfe}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;border:1px solid #d9dde6;color:#4b5563}
.tag.strong{background:#fdecec;border-color:#f7cccd;color:#c22a33}
.tag.watch{background:#eef2ff;border-color:#d6ddfb;color:#3b52b4}
.panel{background:#fff;border:1px solid #e7e9ee;border-radius:10px;padding:16px 14px 8px;overflow-x:auto}
.legend{font-size:12px;color:#6b7280;margin:0 0 6px 2px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 5px 0 12px}
.note{font-size:12px;color:#6b7280;line-height:1.75;margin-top:10px}
.muted{color:#9aa1ac}
.empty{background:#fff;border:1px dashed #dcdfe6;border-radius:10px;padding:26px;text-align:center;color:#8b919c}
"""


def _svg_equity(days: list[dict], w: int = 1100, h: int = 230) -> str:
    pts = [(d["trade_date"], d["nav"], d["bench_nav"]) for d in days if d.get("nav") is not None]
    if len(pts) < 2:
        return '<div class="empty">样本不足 2 个交易日，净值曲线待累积</div>'
    pad_l, pad_r, pad_t, pad_b = 46, 12, 14, 26
    vals = [v for _, v, _ in pts] + [b for _, _, b in pts if b is not None]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo * 0.995, hi * 1.005
    span = hi - lo
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def x(i): return pad_l + iw * (i / (len(pts) - 1))
    def y(v): return pad_t + ih * (1 - (v - lo) / span)

    def path(idx):
        return " ".join(
            ("M" if i == 0 else "L") + f"{x(i):.1f},{y(p[idx]):.1f}"
            for i, p in enumerate(pts) if p[idx] is not None)

    grid = "".join(
        f'<line x1="{pad_l}" y1="{y(lo + span * k / 4):.1f}" x2="{w - pad_r}" y2="{y(lo + span * k / 4):.1f}" '
        f'stroke="#eef0f3"/><text x="{pad_l - 8}" y="{y(lo + span * k / 4) + 4:.1f}" text-anchor="end" '
        f'font-size="10" fill="#9aa1ac">{lo + span * k / 4:.3f}</text>'
        for k in range(5))
    labels = "".join(
        f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="middle" font-size="10" fill="#9aa1ac">{pts[i][0][5:]}</text>'
        for i in (0, len(pts) // 2, len(pts) - 1))
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" style="height:auto;display:block">
{grid}
<path d="{path(2)}" fill="none" stroke="#b9c0cc" stroke-width="1.6" stroke-dasharray="4 3"/>
<path d="{path(1)}" fill="none" stroke="#d9363e" stroke-width="2.2"/>
{labels}
</svg>"""


def _svg_account(curve: list[dict], capital: float, w: int = 1100, h: int = 220) -> str:
    """10 万本金逐日账户曲线：红线=总资产，灰虚线=本金基准。"""
    pts = [(d["trade_date"], d["equity"]) for d in curve]
    if not pts:
        return '<div class="empty">暂无账户数据</div>'
    pad_l, pad_r, pad_t, pad_b = 62, 14, 16, 26
    vals = [v for _, v in pts] + [capital]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-6:
        lo, hi = lo - 100, hi + 100
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    n = len(pts)

    def x(i): return pad_l + (iw * (i / (n - 1)) if n > 1 else iw / 2)
    def y(v): return pad_t + ih * (1 - (v - lo) / span)

    line = " ".join(("M" if i == 0 else "L") + f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(pts))
    area = f"{line} L{x(n - 1):.1f},{y(lo):.1f} L{x(0):.1f},{y(lo):.1f} Z"
    grid = "".join(
        f'<line x1="{pad_l}" y1="{y(lo + span * k / 4):.1f}" x2="{w - pad_r}" y2="{y(lo + span * k / 4):.1f}" '
        f'stroke="#eef0f3"/><text x="{pad_l - 8}" y="{y(lo + span * k / 4) + 4:.1f}" text-anchor="end" '
        f'font-size="10" fill="#9aa1ac">{lo + span * k / 4:,.0f}</text>'
        for k in range(5))
    base = (f'<line x1="{pad_l}" y1="{y(capital):.1f}" x2="{w - pad_r}" y2="{y(capital):.1f}" '
            f'stroke="#b9c0cc" stroke-width="1.4" stroke-dasharray="5 4"/>'
            f'<text x="{w - pad_r}" y="{y(capital) - 6:.1f}" text-anchor="end" font-size="10" '
            f'fill="#9aa1ac">本金 {capital:,.0f}</text>')
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" fill="#d9363e">'
        f'<title>{pts[i][0]} 总资产 {v:,.0f} 元</title></circle>'
        for i, (_, v) in enumerate(pts))
    labels = "".join(
        f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="middle" font-size="10" fill="#9aa1ac">{pts[i][0][5:]}</text>'
        for i in sorted({0, n // 2, n - 1}))
    up = pts[-1][1] >= capital
    stroke = "#d9363e" if up else "#0f9960"
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" style="height:auto;display:block">
<defs><linearGradient id="accg" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="{stroke}" stop-opacity=".14"/>
<stop offset="100%" stop-color="{stroke}" stop-opacity="0"/></linearGradient></defs>
{grid}{base}
<path d="{area}" fill="url(#accg)" stroke="none"/>
<path d="{line}" fill="none" stroke="{stroke}" stroke-width="2.2"/>
{dots}{labels}
</svg>"""


def _account_rows(curve: list[dict], capital: float) -> list[list[str]]:
    out = []
    for d in reversed(curve):
        chg = f"{d['pnl_d0']:+,.0f}"
        out.append([d["trade_date"], f"{d['cash']:,.0f}", f"{d['open_value']:,.0f}",
                    f"**{d['equity']:,.0f}**", chg, _p(d["ret_d0"]), _p(d["cum_ret"]),
                    str(d["positions"])])
    return out


def _svg_bars(days: list[dict], w: int = 1100, h: int = 180) -> str:
    ds = [d for d in days if d.get("avg_ret_d0") is not None]
    if not ds:
        return '<div class="empty">暂无已复盘交易日</div>'
    pad_l, pad_r, pad_t, pad_b = 46, 12, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    peak = max(abs(d["avg_ret_d0"]) for d in ds) or 0.01
    bw = min(56.0, max(4.0, iw / len(ds) * 0.55))
    zero = pad_t + ih / 2
    bars = []
    for i, d in enumerate(ds):
        cx = pad_l + iw * (i + 0.5) / len(ds)
        r = d["avg_ret_d0"]
        hh = abs(r) / peak * (ih / 2 - 4)
        y0 = zero - hh if r > 0 else zero
        color = "#d9363e" if r > 0 else "#12965a"
        bars.append(f'<rect x="{cx - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(hh, 0.8):.1f}" '
                    f'fill="{color}" opacity=".78" rx="1.5"><title>{d["trade_date"]} {r * 100:+.2f}%</title></rect>')
    ticks = "".join(
        f'<text x="{pad_l - 8}" y="{zero - v / peak * (ih / 2 - 6) + 4:.1f}" text-anchor="end" font-size="10" '
        f'fill="#9aa1ac">{v * 100:+.1f}%</text>'
        for v in (peak, peak / 2, -peak / 2, -peak))
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" style="height:auto;display:block">
<line x1="{pad_l}" y1="{zero:.1f}" x2="{w - pad_r}" y2="{zero:.1f}" stroke="#e3e6ea"/>
{ticks}{''.join(bars)}
<text x="{pad_l}" y="{h - 8}" font-size="10" fill="#9aa1ac">{ds[0]['trade_date']}</text>
<text x="{w - pad_r}" y="{h - 8}" text-anchor="end" font-size="10" fill="#9aa1ac">{ds[-1]['trade_date']}</text>
</svg>"""


def _cell_pct(v, bold=False) -> str:
    if v is None:
        return '<td class="muted">-</td>'
    cls = "up" if v > 0 else ("down" if v < 0 else "flat")
    return f'<td class="{cls}">{v * 100:+.2f}%</td>'


def dashboard(conn, source: str = "live") -> str:
    ev = stats.evaluate(conn, source=source)
    days = store.rows(conn, "SELECT * FROM day_stat ORDER BY trade_date")
    if source == "live":
        sim = {d["trade_date"] for d in store.rows(
            conn, "SELECT trade_date FROM trading_day WHERE data_source='simulated'")}
        days = [d for d in days if d["trade_date"] not in sim]
    today = days[-1]["trade_date"] if days else None
    picks = store.pick_meta_rows(store.get_join(conn, today)) if today else []
    day = store.get_day(conn, today) if today else None

    src_note = " · <b>含模拟回放</b>（仅验证链路，不计入实盘评级）" if source == "all" else ""

    def card(k, v, cls=""):
        return f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'

    cards = "".join([
        card("策略累计净值", _num(ev["nav"], 4), "up" if (ev["nav"] or 1) >= 1 else "down"),
        card(f"vs {config.BENCH_NAME}", _p(ev["excess_nav"]),
             "up" if (ev["excess_nav"] or 0) >= 0 else "down"),
        card("当日胜率", _pct(ev["base"]["win_rate"])),
        card("平均单票收益", _p(ev["base"]["avg"]),
             "up" if (ev["base"]["avg"] or 0) >= 0 else "down"),
        card("平均超额", _p(ev["base"]["avg_excess"]),
             "up" if (ev["base"]["avg_excess"] or 0) >= 0 else "down"),
        card("可评估样本", f'{ev["eval_picks"]} / {ev["days"]}日'),
        card("最大回撤", _pct(ev["max_dd"]), "down"),
    ])

    rows_html = []
    for p in picks:
        rating = p["rating"] or ""
        tag = f'<span class="tag {"strong" if rating == "强推" else "watch"}">{html.escape(rating)}</span>'
        rows_html.append(
            f"<tr><td>{p['rank']}</td><td class='l'>{p['code']}</td><td class='l'>{html.escape(p['name'])}</td>"
            f"<td class='l'>{tag}</td><td>{_num(p['total_score'])}</td><td>{_num(p['ref_price'], 2)}</td>"
            f"{_cell_pct(p.get('ret_d0'))}{_cell_pct(p.get('excess_d0'))}{_cell_pct(p.get('mfe_d0'))}"
            f"{_cell_pct(p.get('mae_d0'))}{_cell_pct(p.get('ret_t1'))}{_cell_pct(p.get('ret_t3'))}"
            f"{_cell_pct(p.get('ret_t5'))}</tr>")
    picks_table = (
        "<table><thead><tr><th>#</th><th class='l'>代码</th><th class='l'>名称</th><th class='l'>评级</th>"
        "<th>综合分</th><th>记录价</th><th>当日</th><th>超额</th><th>最大浮盈</th><th>最大浮亏</th>"
        "<th>T+1</th><th>T+3</th><th>T+5</th></tr></thead><tbody>"
        + ("".join(rows_html) or "<tr><td colspan='13' class='muted'>当日无推荐标的（空仓）</td></tr>")
        + "</tbody></table>")

    # 近 20 日流水
    hist = store.rows(conn, """
        SELECT s.trade_date, s.picks, s.win_rate_d0, s.avg_ret_d0, s.avg_excess_d0, s.bench_pct, s.nav
          FROM day_stat s ORDER BY s.trade_date DESC LIMIT 20""")
    hist_html = "".join(
        f"<tr><td class='l'>{h['trade_date']}</td><td>{h['picks'] or 0}</td>"
        f"<td>{_pct(h['win_rate_d0'])}</td>{_cell_pct(h['avg_ret_d0'])}{_cell_pct(h['avg_excess_d0'])}"
        f"{_cell_pct(h['bench_pct'])}<td>{_num(h['nav'], 4)}</td></tr>"
        for h in hist) or "<tr><td colspan='7' class='muted'>暂无记录</td></tr>"

    layer_html = "".join(
        f"<tr><td class='l'>{html.escape(k)}</td><td>{v['n']}</td><td>{_pct(v['win_rate'])}</td>"
        f"{_cell_pct(v['avg'])}<td>{_num(v['profit_factor'], 2)}</td></tr>"
        for k, v in {**ev["by_rating"], **ev["by_rank"]}.items())

    horizon_html = "".join(
        f"<tr><td class='l'>{k}</td><td>{v['n']}</td><td>{_pct(v['win_rate'])}</td>{_cell_pct(v['avg'])}</tr>"
        for k, v in ev["by_horizon"].items())

    env_html = _env_html(conn, today) if today else ""
    env_block = f"<h2>市场环境</h2>{env_html}" if env_html else ""

    # 虚拟交易（强推口径）
    ps = paper.summary(conn, "strong")
    pled = paper.ledger(conn, "strong")
    p_open = [r for r in pled if r["status"] == "open"]
    pv, pr = paper._verdict(ps["closed"], ps["win_rate"], ps["avg_ret"])
    pph = paper.phase_status(conn)
    phase_note = (f"跟踪期 {html.escape(pph['name'])} {pph['start_date']} → {pph['end_date']} · "
                  f"进度 {pph['elapsed']}/{pph['total_days']} 个交易日 · "
                  f"期初基准 {config.BENCH_NAME} {_num(pph['bench_base'], 2)}　·　") if pph else ""
    p_cards = "".join([
        card("总资产", f'{ps["account"]["equity"]:,.0f} 元',
             "up" if (ps["account"]["total_return"] or 0) >= 0 else "down"),
        card("账户收益", _p(ps["account"]["total_return"]),
             "up" if (ps["account"]["total_return"] or 0) >= 0 else "down"),
        card("浮动盈亏", f'{ps["account"]["unrealized"]:+,.0f} 元',
             "up" if ps["account"]["unrealized"] >= 0 else "down"),
        card("已实现", f'{ps["account"]["realized"]:+,.0f} 元',
             "up" if ps["account"]["realized"] >= 0 else "down"),
        card("仓位", _pct(ps["account"]["exposure"])),
        card("跟踪进度", f"{pph['elapsed']}/{pph['total_days']}" if pph else "-"),
        card("持仓 / 平仓", f'{ps["open"]} / {ps["closed"]}'),
    ])
    acct = ps["account"]
    p_rows = "".join(
        f"<tr><td class='l'>{r['code']}</td><td class='l'>{html.escape(r['name'] or '')}</td>"
        f"<td class='l'>{r['fill_date']}</td><td>{_num(r['fill_price'], 2)}</td><td>{r['qty']}</td>"
        f"<td>{_num(r['last_price'], 2)}</td><td>{r['cost']:,.0f}</td><td>{r['value']:,.0f}</td>"
        f"<td class=\"{'up' if r['pnl'] >= 0 else 'down'}\">{r['pnl']:+,.0f}</td>"
        f"{_cell_pct(r['pnl_pct'])}<td>{_num(r['stop_price'], 2)}</td>"
        f"<td>{_tp_progress(r)}</td><td>{r['hold_days']}/{r['holding_days']}</td></tr>"
        for r in acct["positions"]) or "<tr><td colspan='13' class='muted'>当前无虚拟持仓</td></tr>"
    p_curve = paper.equity_curve(conn)
    p_day_rows = "".join(
        f"<tr><td class='l'>{d['trade_date']}</td><td>{d['cash']:,.0f}</td>"
        f"<td>{d['open_value']:,.0f}</td><td><b>{d['equity']:,.0f}</b></td>"
        f"<td class=\"{'up' if d['pnl_d0'] >= 0 else 'down'}\">{d['pnl_d0']:+,.0f}</td>"
        f"{_cell_pct(d['cum_ret'])}<td>{d['positions']}</td></tr>"
        for d in reversed(p_curve)) or "<tr><td colspan='6' class='muted'>暂无</td></tr>"
    paper_block = f"""<h2>虚拟交易 · 强推 <span class="sub">（{acct["capital"]:,.0f} 元本金 · 按建议买卖点纸面执行）</span></h2>
<div class="cards">{p_cards}</div>
<div class="panel">{_svg_account(p_curve, acct["capital"])}</div>
<div class="panel"><table><thead><tr><th class="l">日期</th><th>现金</th><th>持仓市值</th>
<th>总资产</th><th>当日盈亏</th><th>累计收益</th><th>持仓数</th></tr></thead>
<tbody>{p_day_rows}</tbody></table></div>
<div class="panel"><table><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">建仓日</th>
<th>成交价</th><th>数量</th><th>现价</th><th>成本</th><th>市值</th><th>浮盈</th><th>浮动</th>
<th>止损</th><th>止盈进度</th><th>持有</th></tr></thead><tbody>{p_rows}</tbody></table></div>
<div class="note">{phase_note}结论：<b>{pv}</b> —— {html.escape(pr)}　·　
<a href="paper_trading.html">完整台账与逐笔成交</a></div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>磐石选股 · 跟踪复盘终端</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>磐石选股 · 交易日跟踪复盘</h1>
<div class="sub">数据源 <a href="{config.BASE_URL}/picks">{config.BASE_URL}/picks</a>
 · 最新记录 {today or '—'} · 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}
 · 口径：开盘记录时点价 → 当日收盘，基准 {config.BENCH_NAME}{src_note}</div>

<div class="cards">{cards}</div>

<div class="banner"><b class="{'up' if ev['verdict'] == '可靠' else 'down' if ev['verdict'] in ('偏弱', '反向信号') else 'flat'}">
策略评级：{ev['verdict']}</b> —— {html.escape(ev['verdict_reason'])}</div>

<h2>当日推荐表现 · {today or '—'}</h2>
<div class="panel">{picks_table}</div>
<div class="note">记录价 = 开盘记录时点实时价；当日 = 记录价→收盘；超额 = 当日 − {config.BENCH_NAME}。</div>

{env_block}
{paper_block}
<h2>净值曲线</h2>
<div class="legend"><i style="background:#d9363e"></i>策略（等权复利）<i style="background:#b9c0cc"></i>{config.BENCH_NAME}</div>
<div class="panel">{_svg_equity(days)}</div>

<h2>每日组合收益</h2>
<div class="panel">{_svg_bars(days)}</div>

<h2>分层表现</h2>
<div class="panel"><table><thead><tr><th class="l">分层</th><th>样本</th><th>胜率</th><th>平均收益</th><th>盈亏比</th></tr></thead>
<tbody>{layer_html or "<tr><td colspan='5' class='muted'>暂无</td></tr>"}</tbody></table></div>

<h2>持有期跟踪</h2>
<div class="panel"><table><thead><tr><th class="l">区间</th><th>样本</th><th>胜率</th><th>平均收益</th></tr></thead>
<tbody>{horizon_html or "<tr><td colspan='4' class='muted'>暂无</td></tr>"}</tbody></table></div>

<h2>近 20 个交易日流水</h2>
<div class="panel"><table><thead><tr><th class="l">日期</th><th>推荐数</th><th>胜率</th><th>组合收益</th><th>超额</th>
<th>基准</th><th>净值</th></tr></thead><tbody>{hist_html}</tbody></table></div>

<div class="note">
口径与局限：① 推荐来自第三方站点实时研判接口，非历史存档，无法回填往日推荐；② 收益按记录时点价等权计算，未计佣金滑点；
③ 当日收盘价与 T+N 收盘价取自站点日线接口；④ 样本 &lt; {config.MIN_SAMPLE} 条时评级固定为「样本不足」。
</div>
</div></body></html>"""


def write_dashboard(conn, source: str = "live", filename: str = "dashboard.html") -> str:
    return _write(os.path.join(config.REPORT_DIR, filename), dashboard(conn, source))


# --------------------------------------------------------------------------- 分享站

SITE_CSS = """
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 6px}
.chip{padding:6px 12px;border:1px solid #dfe3ea;background:#fff;border-radius:20px;font-size:12.5px;
      color:#4b5563;cursor:pointer;font-variant-numeric:tabular-nums}
.chip:hover{border-color:#b9c6e6}
.chip.on{background:#2f6fed;border-color:#2f6fed;color:#fff}
.chip .p{font-size:11px;opacity:.72;margin-left:6px}
section.day[hidden]{display:none}
.hint{font-size:12px;color:#8b919c;margin:2px 0 0}
tr.rowtog{cursor:pointer}
tr.rowtog:hover{background:#f7f9fe}
.caret{color:#aeb6c2;font-size:10px;margin-left:4px}
tr.rowtog.open .caret{color:#2f6fed}
tr.srow>td{background:#fbfcfe;padding:0;border-bottom:1px solid #e7e9ee}
.strateg{padding:12px 16px 14px;font-size:12.5px;line-height:1.7;color:#3d4450}
.st-head{color:#5b6270;margin-bottom:8px}
.st-grid{display:flex;flex-wrap:wrap;gap:18px;margin:6px 0 10px}
.st-grid>div{font-variant-numeric:tabular-nums}
.st-grid .k{color:#8b919c;margin-right:6px;font-size:12px}
.st-sec{font-weight:600;color:#2f6fed;margin:10px 0 4px;font-size:12px}
.strateg ul{margin:0;padding-left:18px}
.warnline{margin-top:10px;padding:7px 10px;border-radius:6px;background:#fff7e6;
          border:1px solid #ffe0a3;color:#8a5a00;font-size:12px}
"""

SITE_JS = """
(function(){
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var secs  = Array.prototype.slice.call(document.querySelectorAll('section.day'));
  function show(d){
    var hit = false;
    chips.forEach(function(c){ var on = c.getAttribute('data-d') === d;
      c.classList.toggle('on', on); if(on){ hit = true; } });
    secs.forEach(function(s){ s.hidden = s.getAttribute('data-d') !== d; });
    if(hit && history.replaceState){ history.replaceState(null, '', '#' + d); }
  }
  chips.forEach(function(c){ c.addEventListener('click', function(){ show(c.getAttribute('data-d')); }); });
  var h = (location.hash || '').replace('#','');
  var has = chips.filter(function(c){ return c.getAttribute('data-d') === h; }).length > 0;
  show(has ? h : (chips[0] ? chips[0].getAttribute('data-d') : ''));

  Array.prototype.slice.call(document.querySelectorAll('tr.rowtog')).forEach(function(tr){
    tr.addEventListener('click', function(){
      var t = tr.getAttribute('data-t');
      var row = document.querySelector('tr.srow[data-t="' + t + '"]');
      if(!row){ return; }
      row.hidden = !row.hidden;
      tr.classList.toggle('open', !row.hidden);
    });
  });
})();
"""


def _strategy_html(p: dict, snap: dict | None) -> str:
    """渲染「当时给出的交易策略建议」：站点原始参数 + 执行口径修正。"""
    sug = p.get("suggestion")
    if isinstance(sug, str):
        try:
            sug = json.loads(sug)
        except json.JSONDecodeError:
            sug = None
    if not isinstance(sug, dict):
        return "<div class='note'>本条推荐未附带策略建议</div>"

    def esc(v):
        return html.escape(str(v)) if v not in (None, "") else "—"

    tps = "".join(
        f"<li>第{t.get('level')}档 <b>{_num(t.get('price'), 2)}</b>"
        f"（+{t.get('pct')}%，{esc(t.get('reason'))}）</li>"
        for t in sug.get("takeProfits") or [] if isinstance(t, dict))
    sigs = "".join(
        f"<li><span class='tag'>{esc(s.get('type'))}</span> {esc(s.get('title'))}："
        f"{esc(s.get('condition'))}</li>"
        for s in sug.get("signals") or [] if isinstance(s, dict))
    reasons = "；".join(p.get("reasons") or []) or "—"
    risks = "；".join(p.get("risks") or []) or "—"

    plan = snap.get("plan_json") if snap else None
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError:
            plan = None
    notes = (plan or {}).get("notes") or []
    exec_note = ("<div class='warnline'>执行口径修正：" + "；".join(html.escape(n) for n in notes) + "</div>"
                 if notes else "")

    cap = f"（快照 {esc(snap.get('captured_at'))}）" if snap else ""
    return f"""<div class="strateg">
<div class="st-head">评级理由{cap}：{esc(sug.get('ratingReason'))}</div>
<div class="st-grid">
<div><span class="k">主买点</span>{_num(sug.get('primaryEntry'), 2)}</div>
<div><span class="k">次买点</span>{_num(sug.get('secondaryEntry'), 2)}</div>
<div><span class="k">止损</span>{_num(sug.get('stopLoss'), 2)}</div>
<div><span class="k">阻力</span>{_num(sug.get('resistance'), 2)}</div>
<div><span class="k">持有</span>{esc(sug.get('holdingDays'))} 日 · {esc(sug.get('holdingTag'))}</div>
</div>
<div class="st-sec">止盈分档</div><ul>{tps or '<li>—</li>'}</ul>
<div class="st-sec">操作信号</div><ul>{sigs or '<li>—</li>'}</ul>
<div class="st-sec">推荐逻辑</div><div class="st-x">{html.escape(reasons)}</div>
<div class="st-sec">风险提示</div><div class="st-x">{html.escape(risks)}</div>
{exec_note}
</div>"""


def _day_section(conn, d: dict, idx: int) -> str:
    """单个交易日的详情区块（回溯用）。"""
    day = d["trade_date"]
    picks = store.pick_meta_rows(store.get_join(conn, day))
    rows = []
    for n, p in enumerate(picks):
        rating = p["rating"] or ""
        tag = f'<span class="tag {"strong" if rating == "强推" else "watch"}">{html.escape(rating)}</span>'
        snap = store.latest_snapshot(conn, p.get("id")) if p.get("id") else None
        tid = f"{day}-{n}"
        rows.append(
            f"<tr class='rowtog' data-t='{tid}'><td>{p['rank']}</td><td class='l'>{p['code']}</td>"
            f"<td class='l'>{html.escape(p['name'])} <span class='caret'>▸</span></td>"
            f"<td class='l'>{tag}</td><td>{_num(p['total_score'])}</td><td>{_num(p['ref_price'], 2)}</td>"
            f"{_cell_pct(p.get('ret_d0'))}{_cell_pct(p.get('excess_d0'))}{_cell_pct(p.get('mfe_d0'))}"
            f"{_cell_pct(p.get('mae_d0'))}{_cell_pct(p.get('ret_t1'))}{_cell_pct(p.get('ret_t3'))}"
            f"{_cell_pct(p.get('ret_t5'))}</tr>")
        rows.append(
            f"<tr class='srow' data-t='{tid}' hidden><td colspan='13'>{_strategy_html(p, snap)}</td></tr>")
    table = (
        "<table><thead><tr><th>#</th><th class='l'>代码</th><th class='l'>名称</th><th class='l'>评级</th>"
        "<th>综合分</th><th>记录价</th><th>当日</th><th>超额</th><th>最大浮盈</th><th>最大浮亏</th>"
        "<th>T+1</th><th>T+3</th><th>T+5</th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan='13' class='muted'>当日无推荐标的（空仓）</td></tr>")
        + "</tbody></table>")

    def card(k, v, cls=""):
        return f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'

    def cls_of(v):
        return "" if v is None else ("up" if v >= 0 else "down")

    prov = "provisional" in d.keys() and d["provisional"]
    cards = "".join([
        card("推荐数", d["picks"] or 0),
        card("胜率", _pct(d["win_rate_d0"])),
        card("组合收益", _p(d["avg_ret_d0"]), cls_of(d["avg_ret_d0"])),
        card("超额", _p(d["avg_excess_d0"]), cls_of(d["avg_excess_d0"])),
        card(f"{config.BENCH_NAME}{'（开盘）' if prov else ''}",
             _p(d["bench_pct"]), cls_of(d["bench_pct"])),
        card("净值", _num(d["nav"], 4)),
    ])
    env = _env_html(conn, day)
    env_block = f"<h2>市场环境 · {day}</h2><div class='panel'>{env}</div>" if env else ""
    note = ("<div class='hint'>盘中快照：当日推荐已记录，收益/超额/净值待收盘复盘后结算，"
            "此处显示 &quot;-&quot;。</div>") if prov else ""

    return f"""<section class="day" data-d="{day}"{'' if idx == 0 else ' hidden'}>
<div class="cards">{cards}</div>
<h2>当日推荐 · {day}{' <span class="muted">（未复盘）</span>' if prov else ''}</h2>
{note}
<div class="panel">{table}</div>
{env_block}
</section>"""


def site_page(conn, source: str = "live") -> str:
    """生成可分享的多日回溯站：单文件、带日期切换与 hash 深链。"""
    ev = stats.evaluate(conn, source=source)
    days = store.rows(conn, "SELECT * FROM day_stat ORDER BY trade_date")
    if source == "live":
        sim = {r["trade_date"] for r in store.rows(
            conn, "SELECT trade_date FROM trading_day WHERE data_source='simulated'")}
        days = [d for d in days if d["trade_date"] not in sim]
    # 当日已开盘但尚未收盘复盘：把未结算的推荐也放进来，否则首页永远差一天
    done = {d["trade_date"] for d in days}
    days = days + [p for p in stats.pending_days(conn, source) if p["trade_date"] not in done]
    days.sort(key=lambda d: d["trade_date"])
    if not days:
        return "<!DOCTYPE html><meta charset='utf-8'><p>暂无数据</p>"

    latest = days[-1]["trade_date"]
    chips = "".join(
        f'<button class="chip" data-d="{d["trade_date"]}">{d["trade_date"]}'
        f'<span class="p">{_p(d["avg_ret_d0"])}</span></button>'
        for d in reversed(days))
    sections = "".join(_day_section(conn, d, i) for i, d in enumerate(reversed(days)))

    def card(k, v, cls=""):
        return f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'

    cards = "".join([
        card("策略累计净值", _num(ev["nav"], 4), "up" if (ev["nav"] or 1) >= 1 else "down"),
        card(f"vs {config.BENCH_NAME}", _p(ev["excess_nav"]),
             "up" if (ev["excess_nav"] or 0) >= 0 else "down"),
        card("胜率", _pct(ev["base"]["win_rate"])),
        card("平均单票收益", _p(ev["base"]["avg"]),
             "up" if (ev["base"]["avg"] or 0) >= 0 else "down"),
        card("平均超额", _p(ev["base"]["avg_excess"]),
             "up" if (ev["base"]["avg_excess"] or 0) >= 0 else "down"),
        card("可评估样本", f'{ev["eval_picks"]} / {ev["days"]}日'),
        card("最大回撤", _pct(ev["max_dd"]), "down"),
    ])

    hist = "".join(
        f"<tr><td class='l'><a href='#{h['trade_date']}'>{h['trade_date']}</a></td><td>{h['picks'] or 0}</td>"
        f"<td>{_pct(h['win_rate_d0'])}</td>{_cell_pct(h['avg_ret_d0'])}{_cell_pct(h['avg_excess_d0'])}"
        f"{_cell_pct(h['bench_pct'])}<td>{_num(h['nav'], 4)}</td></tr>"
        for h in reversed(days))

    layer = "".join(
        f"<tr><td class='l'>{html.escape(k)}</td><td>{v['n']}</td><td>{_pct(v['win_rate'])}</td>"
        f"{_cell_pct(v['avg'])}<td>{_num(v['profit_factor'], 2)}</td></tr>"
        for k, v in {**ev["by_rating"], **ev["by_rank"]}.items())
    horizon = "".join(
        f"<tr><td class='l'>{k}</td><td>{v['n']}</td><td>{_pct(v['win_rate'])}</td>{_cell_pct(v['avg'])}</tr>"
        for k, v in ev["by_horizon"].items())

    ps = paper.summary(conn, "strong")
    pled = paper.ledger(conn, "strong")
    p_open = [r for r in pled if r["status"] == "open"]
    pv, pr = paper._verdict(ps["closed"], ps["win_rate"], ps["avg_ret"])
    acct = ps["account"]
    p_rows = "".join(
        f"<tr><td class='l'>{r['code']}</td><td class='l'>{html.escape(r['name'] or '')}</td>"
        f"<td class='l'>{r['fill_date']}</td><td>{_num(r['fill_price'], 2)}</td><td>{r['qty']}</td>"
        f"<td>{_num(r['last_price'], 2)}</td><td>{r['cost']:,.0f}</td><td>{r['value']:,.0f}</td>"
        f"<td class=\"{'up' if r['pnl'] >= 0 else 'down'}\">{r['pnl']:+,.0f}</td>"
        f"{_cell_pct(r['pnl_pct'])}<td>{_num(r['stop_price'], 2)}</td>"
        f"<td>{_tp_progress(r)}</td><td>{r['hold_days']}/{r['holding_days']}</td></tr>"
        for r in acct["positions"]) or "<tr><td colspan='13' class='muted'>当前无虚拟持仓</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>磐石选股 · 跟踪复盘</title>
<meta name="description" content="第三方选股建议的每日跟踪与策略可靠性评估">
<style>{CSS}{SITE_CSS}</style></head>
<body><div class="wrap">
<h1>磐石选股 · 跟踪复盘</h1>
<div class="sub">数据源 <a href="{config.BASE_URL}/picks" target="_blank" rel="noopener">{config.BASE_URL}/picks</a>
 · 最新记录 {latest} · 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}
 · 口径：开盘记录时点价 → 当日收盘，基准 {config.BENCH_NAME}</div>

<div class="cards">{cards}</div>

<div class="banner"><b class="{'up' if ev['verdict'] == '可靠' else 'down' if ev['verdict'] in ('偏弱', '反向信号') else 'flat'}">
策略评级：{ev['verdict']}</b> —— {html.escape(ev['verdict_reason'])}</div>

<h2>按交易日回溯</h2>
<div class="chips">{chips}</div>
<div class="hint">点日期切换；链接末尾带 <code>#YYYY-MM-DD</code> 可直接分享某一天。</div>

{sections}

<h2>量化模拟 · 强推 · {ps["account"]["capital"]:,.0f} 元本金 <span class="sub">（逐日收盘结算，当前总资产 {ps["account"]["equity"]:,.0f} 元）</span></h2>
<div class="panel">{_svg_account(paper.equity_curve(conn), ps["account"]["capital"])}</div>
<h2>虚拟交易 · 强推 <span class="sub">（按建议买卖点纸面执行）</span></h2>
<div class="panel"><table><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">建仓日</th>
<th>成交价</th><th>数量</th><th>现价</th><th>成本</th><th>市值</th><th>浮盈</th><th>浮动</th>
<th>止损</th><th>止盈进度</th><th>持有</th></tr></thead><tbody>{p_rows}</tbody></table></div>
<div class="note">持仓 {ps['open']} 笔 · 已平仓 {ps['closed']} 笔 · 结论：<b>{pv}</b> —— {html.escape(pr)}
　·　<a href="paper.html"><b>查看完整交易台账（10 万本金逐日结算）→</b></a></div>

<h2>净值曲线</h2>
<div class="legend"><i style="background:#d9363e"></i>策略（等权复利）<i style="background:#b9c0cc"></i>{config.BENCH_NAME}</div>
<div class="panel">{_svg_equity(days)}</div>

<h2>每日组合收益</h2>
<div class="panel">{_svg_bars(days)}</div>

<h2>分层表现</h2>
<div class="panel"><table><thead><tr><th class="l">分层</th><th>样本</th><th>胜率</th><th>平均收益</th><th>盈亏比</th></tr></thead>
<tbody>{layer or "<tr><td colspan='5' class='muted'>暂无</td></tr>"}</tbody></table></div>

<h2>持有期跟踪</h2>
<div class="panel"><table><thead><tr><th class="l">区间</th><th>样本</th><th>胜率</th><th>平均收益</th></tr></thead>
<tbody>{horizon or "<tr><td colspan='4' class='muted'>暂无</td></tr>"}</tbody></table></div>

<h2>全部交易日</h2>
<div class="panel"><table><thead><tr><th class="l">日期</th><th>推荐数</th><th>胜率</th><th>组合收益</th><th>超额</th>
<th>基准</th><th>净值</th></tr></thead><tbody>{hist}</tbody></table></div>

<div class="note">
口径与局限：① 推荐来自第三方站点实时研判接口，非历史存档；② 收益按记录时点价等权计算，未计佣金滑点；
③ 收盘价与 T+N 收盘价取自站点日线接口，偶有次日修正；④ 样本 &lt; {config.MIN_SAMPLE} 条时评级固定为「样本不足」；
⑤ 本页仅为个人跟踪记录，不构成任何投资建议。
</div>
</div>
<script>{SITE_JS}</script>
</body></html>"""


def write_site(conn, out: str | None = None, source: str = "live") -> str:
    path = out or os.path.join(config.ROOT, "site", "preview.html")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    html = site_page(conn, source)
    _write(path, html)
    dist_path = os.path.join(config.ROOT, "dist", "index.html")
    _write(dist_path, html)
    return path
