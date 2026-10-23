# AGENTS.md · 跨智能体协作约定

> 给 Qoder / Codex / Claude / 其他自动化看的仓库说明书。**改本仓库任何东西之前，先读完这一页。**
> 维护者：rex（revrex1）。最后更新：2026-10-23。

---

## 1. 这个仓库是什么

对第三方站点「磐石选股」（`https://bqgxkzv5v9.coze.site/picks`）的每日推荐做**记录 → 入库 → 复盘 → 量化模拟 → 发布**的闭环。

Python 3.13，**纯 stdlib + sqlite，零第三方依赖**。入口 `python -m astocks <cmd>`，代码在 `astocks/`。

本机 Python 绝对路径（定时任务用）：
`C:\Users\Suma\.workbuddy\binaries\python\versions\3.13.12\python.exe`

---

## 2. 分工边界（最重要）

| 谁 | 负责 | 不负责 |
|---|---|---|
| **本仓库（astocks）** | 抓数、入库、复盘统计、虚拟盘模拟、报告生成、**发布 docs/ 下的文件** | 页面设计与前端交互的深度定制 |
| **建站端（Qoder 等）** | 首页视觉、交互、多页导航、分享 | 不要直接改 `docs/` 下的文件，也不要改 `data/*.db` |

**硬规则：`docs/` 是发布产物目录，只由 `astocks push` 写入。** 手工改 `docs/` 会在下次 open/close 被覆盖。

---

## 3. 发布契约

每次 `open` / `close` 执行完，`_after_work()` 会自动：

```
site/data/*.json  ──▶ docs/data/*.json      （每日明细 + index.json + paper.json）
dist/index.html   ──▶ docs/index.html       （复盘首页）
dist/paper.html   ──▶ docs/paper.html       （虚拟交易台账）
git commit + push origin/master → GitHub Pages
```

- 线上地址：https://revrex1.github.io/AstocksTest/
- 台账：https://revrex1.github.io/AstocksTest/paper.html
- 机读：https://revrex1.github.io/AstocksTest/data/index.json、`/data/paper.json`、`/data/YYYY-MM-DD.json`
- Pages 源目录 = `master:/docs`；构建约 50s，CDN `max-age=600`（最坏 10 分钟）
- **本机 github.com:443 被代理拦截**，push 走 SSH + 仓库部署密钥（`~/.ssh/astocks_github`，仓库级 `core.sshCommand` 已配好）。HTTPS push 不可用，别去改 remote。

### 发布白名单（`astocks/sitepush.py`）

只允许：`docs/data/*.json`、`docs/index.html`、`docs/paper.html`。
新增发布位置**必须同步改** `ALLOW_EXACT` / `ALLOW_DIRS`，否则被闸门挡下（`reason=blocked`，不复制不提交不推送）。

另有一道敏感信息闸门：内容命中本地盘符路径 / 私钥 / SSH 公钥 / 凭据字段 / Bearer / 内网 IP / 本机配置目录 / `git@github` 任一特征即整体放弃。

### 首页模板归谁

`dist/index.html` 当前由本仓库生成：`python -m astocks site --out dist/index.html`（模板在 `astocks/report.py::site_page`）。

**如果建站端要重建首页，必须保留：**
1. 顶部/虚拟交易段的入口 `<a href="paper.html">查看完整交易台账 →</a>`
2. 数据取数契约：`DATA_BASE + "/index.json"`（见第 4 节）

否则改完通知我，同步更新本文件与 README。

---

## 4. 数据契约（给前端消费）

### `docs/data/index.json`

```jsonc
{
  "updated_at": "2026-10-23 18:52:36",
  "source": "live",                 // live | all（all 含模拟回放日）
  "bench": {"code": "sh000300", "name": "沪深300"},
  "latest": "2026-10-23",
  "count": 8, "dates": [...],
  "summary": { "nav", "bench_nav", "excess_nav", "win_rate", "avg",
               "avg_excess", "profit_factor", "samples", "days", "max_dd",
               "verdict", "verdict_reason" },
  "daily": [ {"date","picks","strong","watch","win_rate","avg_ret",
              "avg_excess","bench_pct","nav","bench_nav","provisional"} ],
  "layers": {...}, "horizons": {...},
  "pending": ["2026-10-23"],        // 已开盘未复盘，统计位为 null
  "paper": { ...summary, "url": "paper.html", "data_url": "data/paper.json" }
}
```

**约束**：收盘前（15:00 之前）当天数据是盘中快照；`provisional=true` 的日子统计位为 `null`，别渲染成 0。

### `docs/data/paper.json`（虚拟盘，2026-10-23 新增）

```jsonc
{
  "capital": 100000, "equity": 100206.7, "cash": 70030.7,
  "open_cost": 29969.3, "open_value": 30176.0,
  "unrealized": 206.7, "realized": 0.0,
  "total_return": 0.0021, "exposure": 0.301, "max_positions": 8,
  "curve": [ {"date","cash","value","equity","pnl","ret","cum","positions"} ],
  "positions": [ {"code","name","rating","fill_date","fill_price","qty",
                  "last_price","cost","value","pnl","pnl_pct","stop_price","ret_pct"} ],
  "trades": [ {"trade_date","code","name","rating","status","exit_reason",
               "entry_target","fill_date","fill_price","qty","ret_pct","hold_days"} ],
  "stats": {"orders","filled","open","closed","pending","expired",
            "fill_rate","win_rate","verdict"}
}
```

`trades[].status` ∈ `pending | open | closed | expired | skipped`；
`exit_reason` ∈ `expired | superseded | duplicate | gap_below_stop | no_slot | cash_short | stop | timeout | take_profit`。

---

## 5. 口径红线（不许擅自改）

这些是策略评估的**执行假设**，改了历史就不可比。要改先问 rex。

### 复盘统计
- `ret_d0` = 当日收盘 / 记录价 − 1，记录价是 09:31 抓取时点价（`pick.ref_price`）
- 基准 = 沪深300（`sh000300`），超额 = 个股收益 − 基准同日收益
- **基准会回溯修正**：跨日比较必须重读库，不要引用旧报告里的数字
- 15:00 前当日 K 线是盘中快照，不能当收盘价

### 虚拟盘（`paper.py`）
- 总资金 **10 万元**，最多同时持 **8 只**，只做**强推**
- 仓位：**以手为单位下单，手数 = round(总资金×10% ÷ 每手金额)**，至少 1 手；单笔金额上限 15%
- 买点 `primaryEntry` 挂限价单，有效 **3 个交易日**；买点 ≥ 开盘按开盘价成交
- 止损 `stopLoss`；无效（缺失或 ≥ 买点）退化为 `secondaryEntry` 作破位线
- 止盈三档各减 1/3，**每档必须整手（100 股）**
- T+1、一字涨停不买、一字跌停不卖、停牌不撮合
- **同日既触止损又触止盈**：有完整 5 分钟线按分钟路径；否则保守「先止损后止盈」
- **策略修正规则**：
  - 同一标的再来强推、**旧单未成交** → 旧单作废（`superseded`），按新建议重挂
  - 同一标的**已持仓** → 不新建仓，但止损/止盈/持有期替换为新版本（`plan_json.schedule` 按日生效）
  - 买点不参与持仓期修订（不加仓）
- 结果：买点普遍比记录价低 3%~8%，撤旧重挂 ⇒ 成交率约 10%，长期空仓是**策略特性不是 bug**

### 数据源
- 站点 `/api/kline` 会**回溯改写历史**（前复权基准变化），日线被改过就要跑 `close --recheck`
- `outcome.status='done'` 的记录默认冻结，不随日线自愈
- iFinD `PEG(LYR)` 与站点 PEG 口径不同，不可互换
- iFinD 批量查询会**静默漏标的**，必须逐只调用

---

## 6. 常用命令

```bash
python -m astocks open                 # 09:31 开盘记录
python -m astocks close                # 15:05 收盘复盘（自动重拉当日日线 + 基准）
python -m astocks close --recheck      # 全量重拉历史日线 + 强制重评全部记录
python -m astocks paper --rebuild      # 清空虚拟盘按历史重放（改口径后必须跑）
python -m astocks audit --start 2026-10-19   # 逐笔审计，与账本对账
python -m astocks minutes --fetch      # 抓最近一周 5 分钟线（腾讯财经，无需连接器）
python -m astocks push                 # 手动发布（open/close 已自动调）
python -m astocks push --dry-run       # 只看会发布哪些文件
python -m astocks site --out dist/index.html   # 重建首页
```

计划任务：工作日 09:31 / 15:05（`tasks/install_tasks.ps1`），日志 `data/logs/YYYY-MM-DD.log`。

---

## 7. 已知坑（都踩过）

1. **`bars.refresh` 每票每自然日只联网一次**。15:00 后复盘会复用 09:32 的盘中快照 → `close` 已内置强制刷新（含基准指数），别再手工跳过。
2. **基准漏刷会让超额方向反转**。2026-10-23 实测：沪深300 快照 -1.42% vs 实际 +1.71%，超额 +2.37% → -0.76%。
3. **`.gitignore` 必须锚定根目录**：写 `data/` 会把 `docs/data/` 一起忽略，必须写 `/data/`。
4. **`superseded` 的行装载时不能再复活**，否则增量 sync 与 `--rebuild` 结果分叉。
5. **成交去重要在 `_open()` 里再查一次**：旧挂单与新挂单可能同一天才成交。
6. **当日新仓要按当日收盘价补估值**（`_mark_new`），否则按成本计价、净值少算浮动。
7. 站点 `data_sector` 的 `limit` 对 `mode=ranking` 不生效，会返回约 110 个行业，调用方自己截断。
8. 平台 sites 发布不可用（`*.app.workbuddy.host` 公网 NXDOMAIN），分享只能走 GitHub Pages。

---

## 8. 改动前检查清单

- [ ] 动 `data/astocks.db` 前先 `cp data/astocks.db data/astocks.db.bak_<日期>` 
- [ ] 动口径 → 必须 `paper --rebuild` + `close --recheck`，否则新旧口径混在一张表里
- [ ] 动发布白名单 → 同步改 `sitepush.ALLOW_EXACT` / `ALLOW_DIRS` + 本文档第 3 节
- [ ] 动首页 → 保留 `paper.html` 入口 + `index.json` 契约
- [ ] 改完跑一次 `python -m astocks close`，确认 `push` 没有 `blocked`
- [ ] 破坏性操作先备份；能修不删

---

## 9. 交接要点（给下一个接手的人）

- 别相信任何写死在报告里的数字，**一切从 `data/astocks.db` 重算**。
- 报告里出现「样本不足」不是故障：平仓样本 < 10 笔不出策略结论是设计。
- `reports/paper_audit.md` 是规则体检表。数字对不上时先看它 —— 能区分「规则错」还是「仓位约束」。
- 虚拟盘亏损/空仓不一定是 bug，先看 `paper_audit.md` 的「与账本分歧」是否为 0。
