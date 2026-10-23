# Astocks · 磐石选股交易日跟踪复盘

对 [磐石选股](https://bqgxkzv5v9.coze.site/picks) 的每日推荐做**开盘记录 → 入库 → 收盘核对 → 策略可靠性评估**的闭环模型。

- 开盘（09:31）：抓取站点当日研判推荐股，连同记录时点价、评分、评级、止损/止盈位一并入库
- 收盘（15:05）：用日线核对每只推荐股的当日与 T+1/3/5 表现，计算胜率、超额、盈亏比、净值与回撤
- 报告：每日开盘单 / 收盘单（Markdown）+ 滚动策略评估 + 单页 HTML 看板

## 快速开始

```bash
cd D:/Codebase/Astocks
python -m astocks open                 # 开盘记录（非交易日自动跳过）
python -m astocks close                # 收盘复盘（自动补齐历史 T+N）
python -m astocks status               # 表格查看
python -m astocks quote 300274,600519,sh000300   # 随手查行情（个股/指数混写）
python -m astocks quote 600519 --src tencent     # 走腾讯财经公开接口（交叉校验）
python -m astocks report --source all  # 重生成报告与看板
python -m astocks context --kind sectors --file ctx.json   # 写入板块环境（连接器数据）
python -m astocks simulate --date 2026-10-16   # 回放验证全链路
python -m astocks paper                # 虚拟交易：按建议买卖点纸面执行（只统计强推）
python -m astocks paper --view all     # 同上，含可关注（做分层对比）
python -m astocks paper --rebuild      # 清空后按历史推荐全量重放（口径调整后重算用）
python -m astocks paper --start-phase 2026-10-19 --end 2026-11-18 --name 第一期   # 开启一期跟踪
python -m astocks paper --rebuild --offline   # 只用本地日线库重放，不联网
python -m astocks minutes --file data/min_20261022.json   # 写入 5 分钟线，用于判定止盈止损先后
python -m astocks site                     # 生成可分享的多日回溯单页站 site/index.html
python -m astocks push                     # 同步导出数据到 docs/ 并推送 GitHub Pages（open/close 自动调用）
python -m astocks bars --force        # 补/刷新本地日线库（推荐股 + 基准）
python -m astocks bars                # 体检：覆盖区间、根数、库体积
python -m astocks snapshot            # 为历史推荐补建不可变快照
```

Windows 计划任务（工作日 09:31 / 15:05，节假日由脚本自行跳过）——**本机已注册**：

```powershell
# 已安装：Astocks-Open / Astocks-Close，下次运行 2026-10-19 09:31
powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1            # 重装/修复
powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1 -Uninstall # 卸载
```

日志：`data/logs/YYYY-MM-DD.log`。首次运行前若要验证链路，用 `python -m astocks simulate --date <交易日>`。

看板：`reports/dashboard.html` 只统计实盘记录；`reports/dashboard_demo.html` 含模拟回放数据（用于预览效果）。

## 运行节奏

| 时间 | 任务 | 动作 | 失败/漏跑的处理 |
|---|---|---|---|
| 工作日 09:31 | Astocks-Open | 拉研判 → 记录推荐股 + 开盘价入库 | 非交易日直接跳过；任务延迟启动时仍按**开盘价**记账，不产生滑价偏差 |
| 工作日 15:05 | Astocks-Close | 核对当日 + 补齐历史 T+1/3/5 → 出报告 | 所有 `status != done` 的历史推荐每轮重算；日线未更新时用收盘快照兜底，次日自动覆盖 |

- 交易日判定不靠星期几：取 `sh000001` 行情快照的 `time`，与当天日期比对，节假日/调休自动跳过。
- 运行条件：当前注册为「用户已登录时运行」。要改成锁屏/未登录也跑，需**以管理员身份**执行一次安装脚本（S4U 需要批处理登录权限，普通权限会退回登录模式）。
- 电脑关机导致整天没跑：开盘记录无法补（站点只给实时推荐），收盘核对会在次日自动补算已有记录。

## 数据源

站点是 Next.js 前端，以下内部接口无需鉴权（从前端 chunk 反查）：

| 接口 | 用途 |
|---|---|
| `POST /api/research` `{"maxCandidates":80,"pickMax":8}` | 全 A 研判：研究池 + 推荐（**单次 60~90s**） |
| `GET /api/quote?codes=300274,002460` | 实时快照，`time` 字段用于判定当日是否为交易日 |
| `GET /api/kline?code=300274` | 日线约 320 根，指数用 `sh` 前缀（`sh000300`） |
| `GET https://qt.gtimg.cn/q=sh600519` | **腾讯财经公开接口**（无需 key），作为备用/交叉校验源；收盘核对时站点不可用会自动切换 |

原始响应全部落盘到 `data/raw/<日期>/`，便于审计与重算。

### 连接器（已接通）

| 连接器 | 用途 | 落地方式 |
|---|---|---|
| 腾讯自选股 `westock-mcp` | `data_quote` 快照（含 52 周高低、5/10/20/60 日涨幅）、`data_fund_flow` 个股资金流、`data_sector` 行业排行与主力净流入 | 定时任务收盘后用 `astocks context` 写入 `market_context` 表，进入复盘单与看板 |
| 同花顺 iFinD `ifind-mcp` | `get_stock_summary` 估值（PE/PB/PEG/EV-EBITDA）、申万+中信行业、主营构成；`search_securities` 智能选股 | 开盘简报中补充基本面；暂不落库 |

`context` 的 JSON 结构（金额统一为亿元）：

```json
{"sectors":[{"name":"地面兵装Ⅱ","changePct":5.96,"mainNetInflowYi":7.6,"leader":"北方长龙"}]}
{"stocks":{"300274":{"code":"300274","name":"阳光电源","mainNetInflowYi":0.29,"mainNetInflow5dYi":-8.03}}}
```

## 目录

```
astocks/          采集、入库、复盘、统计、报告
  source.py       接口客户端（重试 + 原始落盘）
  bars.py         本地日线库（读写 bar 表，按日刷新一次）
  record.py       开盘记录 + 推荐快照
  review.py       收盘复盘 + T+N 跟踪
  stats.py        策略可靠性指标与评级
  paper.py        虚拟交易（按建议买卖点模拟成交、止盈止损、到期平仓）
  report.py       Markdown + HTML 看板
  store.py        SQLite 表结构
run.py            计划任务入口（带日志）
data/astocks.db   SQLite
reports/          报告与 dashboard.html
tasks/            计划任务安装脚本
```

## 口径（重要）

| 项 | 定义 |
|---|---|
| 记录价 `ref_price` | **当日开盘价**（`quote.open`），缺失时退化为记录时点实时价；作为买入基准 |
| 当日收益 `ret_d0` | 记录价 → 当日收盘 |
| 可执行口径 `ret_d0_open` | 当日开盘 → 当日收盘（对照用，剔除记录时点偏差） |
| 超额 `excess_d0` | `ret_d0` − 沪深300 当日涨跌 |
| T+N | 记录价 → 第 N 个交易日收盘；超额同基准同期 |
| 组合净值 | 每日推荐**等权**、逐日复利；无推荐日记 0 收益 |
| 胜率 | `ret_d0 > 0` 的比例；超额胜率单独统计 |
| 评级 | 样本 < 5 条固定「样本不足」；胜率≥55% 且超额≥0.5% 为「可靠」；≤40% 且超额≤−1% 为「反向信号」 |

未计佣金、印花税、滑点与涨跌停无法成交的情形。

## 数据库

| 表 | 内容 |
|---|---|
| `trading_day` | 每日一行：漏斗数字、推荐数、基准、复盘时间、`data_source`(live/simulated) |
| `pick` | 每只推荐股：代码、评级、综合分、记录价、止损/止盈、理由、原始 JSON |
| `outcome` | 每只票的当日与 T+1/3/5 收益、超额、最大浮盈/浮亏、止盈止损触发、跟踪进度 |
| `day_stat` | 每日组合统计与净值 |
| `paper_trade` | 虚拟头寸：计划买点、成交价量、止损/止盈、已实现盈亏、状态 |
| `paper_exec` | 虚拟成交流水：买/卖、价格、数量、费用、触发原因（entry/tp1-3/stop/timeout） |
| `paper_phase` | 跟踪期：起止日期、期初基准与其日期 |
| `pick_snapshot` | **推荐快照**：每次抓取的原始 payload + 买卖策略 + 解析出的执行计划，按版本追加，永不覆盖 |
| `bar` | **本地日线库**：code+trade_date 主键，站点 320 根过期后仍可回溯 |
| `bar_sync` | 日线刷新台账：每只票最后一次抓取时间与最新 bar 日期（每日最多拉一次） |

## 虚拟交易（纸面执行）

**目的**：验证「推荐附带的买卖策略」本身值不值钱 —— 不只看推荐日涨跌，而是按建议的买点、止损、止盈、持有期真实走一遍。

**规则全部来自 `pick.suggestion`，不新增任何选股/择时参数：**

| 环节 | 规则 |
|---|---|
| 建仓 | `primaryEntry` 挂限价单，有效期 3 个交易日；买点 ≥ 开盘价则按开盘价成交（不追高）；跳空低开直接跌破止损位则放弃（不接飞刀） |
| 去重 | 同标的已有持仓时不重复建仓（`skipped`）；**不撤旧挂单**——每张建议单独立有效 3 天，同标的多单并行，先触及者成交 |
| 止损 | `stopLoss`；无效（缺失或 ≥ 买点）时退化为 `secondaryEntry` 作为破位线，两者皆无则取消止损 |
| 止盈 | `takeProfits` 三档，每档减 1/3；跳空高开按开盘价成交 |
| 到期 | 持满 `holdingDays` 个交易日，剩余仓位按收盘价平仓 |
| 日内顺序 | 有 5 分钟线按分钟路径逐根判定（`path_src=minute`）；否则退化为「同日先止损后止盈」（`path_src=daily`） |
| 滑点 | 买入受限价保护（不高于买点），卖出按触发价再让 5bp |
| 约束 | T+1（建仓当日不卖）、一字跌停卖不出、一字涨停买不进、停牌日不撮合 |
| 费用 | 佣金 万2.5（最低 5 元）+ 过户费 0.001% + 卖出印花税 0.05% |

**分钟线**：连接器（腾讯自选股 `data_kline period=m5`）拉到的分钟线落 `bar_min` 表，命令
`python -m astocks minutes --file data/min_<date>.json`（格式 `{"sh600938":[节点数组]}`，也接受 MCP 原始 `{"code":..,"nodes":[..]}`）。
没入库的持仓日自动退化为 `path_src=daily`，不会报错，但同一天的止盈止损先后只能取保守解。

**资金口径（2026-10-23 起）**：总资金 10 万元，最多同时持 5 只，单笔预算 2 万元；建仓受真实现金约束——现金不够就少买，连一手都买不起则放弃（`cash_short`），满仓时新信号标记 `no_slot`。逐日收盘结算，`paper_day` 表存当日现金/持仓市值/总资产/收益率，`reports/paper_trading.html` 出净值曲线。当日 15:00 前不把当日 K 线当收盘数据，避免用盘中快照误判止损/止盈。

**减仓整手**：三档止盈每档取 100 股整数倍，余数并入最后一档（A 股卖不出 166 股这种委托）。

**逐笔审计**：`python -m astocks audit --start 2026-10-19` 会按同一套规则**单笔独立重算**全部强推，与组合账本对账，输出 `reports/paper_audit.md`（逐笔成交判定 + 当日走势 + 与账本分歧）。规则口径出问题时，先用它定位是规则错还是仓位约束。

**产出**：`reports/paper_trading.md`（强推口径）与 `reports/paper_trading.html`；`--view all` 另生成 `paper_trading_all.*` 做强推/可关注分层对比。看板 `dashboard.html` 内嵌「虚拟交易 · 强推」区块。

**评估**：平仓样本 ≥ 10 笔才出结论（「可沿用 / 观察中 / 需修正」）；指标含胜率、期望值、盈亏比、平均超额、平均持有、退出原因分布、已实现回撤。`open` / `close` 会顺带结算，无需单独跑。

**跟踪期**：`--start-phase` 记录期初基准（起始日前一交易日收盘）、起止日期与交易日数，并清空重建该期账本。期进度（已过/总交易日）会写进虚拟交易台账与 `strategy_review.md`。交易日数按工作日估算（未来日历拿不到），遇法定节假日需手工修正。

**交易范围**：`PAPER_SCOPE=("强推",)` —— 只对当日强推下单，可关注仅记录不交易。

## 数据出口（给外部建站/看板消费）

`python -m astocks export` 把复盘结果导成机读 JSON，默认写到 `site/data/`：

| 文件 | 内容 |
|---|---|
| `index.json` | 日期清单、累计指标（净值/基准/超额/胜率/盈亏比/评级）、每日一行摘要、分层/持有期、虚拟交易汇总 |
| `<YYYY-MM-DD>.json` | 单日明细：推荐表（记录价/开高低收/当日超额/浮盈浮亏/T+1~T+5/止盈止损）、当日指标、板块排行、主力资金、当前持仓、**每条推荐的原始策略与执行口径** |

每条推荐额外带两块策略数据：

| 字段 | 内容 |
|---|---|
| `strategy` | 当时站点给的策略原文：`rating_reason`、`entry.primary/secondary`、`stop_loss`、`resistance`、`take_profits[]`（档位/涨幅/价格/理由）、`holding_days`+`holding_tag`、`signals[]`（buy/add/reduce/sell + 触发条件）、`reasons[]`、`risks[]`、`tags[]`、`captured_at` + `snapshot_version` |
| `execution` | 实际执行口径：`entry_mode`、`entry_target`、`valid_days`、`stop_price`、`tp_prices[]`、`notes[]`（站点参数矛盾时的退化说明，如「止损位 45.81 无效（≥买点），改用次买点 43.52」） |

`strategy` 优先取 `pick_snapshot`（不可变，记录时点固化），只有快照缺失才退回当前 `pick` 行，`source` 字段标明来源。展示页要「点开看当时是怎么说的」，直接读 `strategy`，不要读 `execution`——后者是修正后的执行值，两者可能不同。

约定：
- 数值一律用小数（`-0.037` 表示 -3.7%），金额单位已在字段名里写明（`mainNetInflowYi` 为亿元）。
- 缺什么就是 `null`，不填充、不推断；`sectors` 无数据时为 `null`。
- 净值按 `live` 口径重算（不含模拟回放日），与 `index.summary.nav` 同源。
- `open` / `close` 跑完会自动重建，无需单独执行。

`python -m astocks site` 另产 `site/preview.html`（单文件、多日回溯、`#YYYY-MM-DD` 深链），仅供本地预览与截图，`index.html` 留给外部建站工具的输出。

## 站点自动更新（GitHub Pages）

`open` / `close` 结束时除了导出 JSON，还会自动把数据同步到 Pages 发布目录并推送，站点无需重新发版：

```
site/data/*.json  ──复制──▶  docs/data/*.json  ──commit+push──▶  GitHub Pages（源目录 master:/docs）
dist/index.html   ──复制──▶  docs/index.html   （仅当 dist 存在且内容不同）
```

手动触发与排查：

```bash
python -m astocks push                  # 同步 + 提交 + 推送
python -m astocks push --dry-run        # 只看会同步哪些文件
python -m astocks push --export         # 先重新导出再推送
python -m astocks push --force          # 内容无变化也提交（调试用）
```

行为约定：

| 项 | 说明 |
|---|---|
| 幂等 | 内容一致不提交、不推送；`open`/`close` 各一次，每天最多 2 个提交 |
| 影响面 | 只提交 `docs/data/` 与 `docs/index.html`，不牵连仓库其他改动 |
| 失败处理 | 只记日志并写 `data/site_push.json`，不阻断复盘主链路 |
| 远端分叉 | push 被拒时自动 `pull --rebase --autostash` 后重试一次 |
| 模拟数据 | `source != live`（`--simulate` 回放）时不推送，避免污染公开站点 |
| 生效延迟 | `git push` → Pages 重建约 1 分钟；CDN `max-age=600`，最坏 10 分钟后全量刷新 |

**网络前提**：本机 `github.com:443` 被代理拦截（CONNECT 502），所以远端用 SSH 而非 HTTPS：

```bash
git remote -v            # origin  git@github.com:revrex1/AstocksTest.git
git config core.sshCommand   # ssh -i ~/.ssh/astocks_github -o IdentitiesOnly=yes ...
```

`~/.ssh/astocks_github` 是仓库级部署密钥（write 权限），只对该仓库有效，换机器需重新生成并在仓库 Settings → Deploy keys 注册。`docs/data/` 必须被 git 跟踪（Pages 直接托管），不要加进 `.gitignore`；`site/data/`、`site/preview.html` 是本地构建产物，已忽略。

## 数据留存（回溯前提）

两点硬要求，都落在库里：

1. **推荐策略快照不可变**：`record_open` 每抓一条推荐，就把「站点原始 payload + 当时的 suggestion + 解析出的执行计划」写入 `pick_snapshot`（`payload_sha` 校验、`version` 递增）。之后 `pick` 表被 `--force` 重写、或站点改字段，历史策略仍能原样复现。老数据用 `astocks snapshot` 补建，标注 `schema_ver=v1-backfill`。
2. **日线本地留档**：站点 `/api/kline` 只回约 320 根，滚过去就断档。所有复盘、虚拟交易统一走 `bar` 表（`bars.ensure`），每只票每自然日最多联网拉一次，其余全部读本地。`--offline` 可完全断网重放，结果可复现。

代价：DB 从 ~90KB 涨到 ~400KB（5 只票 × 321 根）。按每天新增 2 只票估算，一年约 +3MB，可忽略。

查询示例：

```bash
sqlite3 data/astocks.db "select trade_date,code,rating,ref_price,d0_close,round(ret_d0*100,2) from pick join outcome using(trade_date) order by trade_date desc limit 20"
sqlite3 data/astocks.db "select trade_date,code,status,fill_date,fill_price,round(ret_pct*100,2),exit_reason from paper_trade order by trade_date desc"
```

## 局限

1. 站点只提供**实时**研判，没有历史推荐存档，因此无法回填往日记录；模型从首次运行日起累积样本。
2. 研究池是站点按总市值截取的前 80 只，不是全市场，推荐天然偏大中盘。
3. 单次研判约 70s，若 09:31 抓取失败会自动重试 2 次；仍失败则当日记空，次日可 `--date` 补记（价格会偏离开盘）。
4. 样本不足 5 条时不做策略结论，只记录数据。
