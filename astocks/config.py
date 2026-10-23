"""全局配置：路径、数据源、口径参数。"""
from __future__ import annotations

import os

BASE_URL = "https://bqgxkzv5v9.coze.site"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
REPORT_DIR = os.path.join(ROOT, "reports")
DB_PATH = os.path.join(DATA_DIR, "astocks.db")

# 基准与市场温度计
BENCH_CODE = "sh000300"
BENCH_NAME = "沪深300"
MARKET_CODE = "sh000001"

# 与站点页面一致的默认参数（80 = 页面「研究池大小」默认值，pickMax=8 为其锁定上限）
MAX_CANDIDATES = 80
PICK_MAX = 8

HTTP_TIMEOUT = 240          # /api/research 单次实测 60~90s
HTTP_RETRIES = 2

# 跟踪期（交易日）
HORIZONS = (1, 3, 5)

# 策略评级阈值
MIN_SAMPLE = 5              # 少于此样本数不下结论
STRONG_WIN = 0.55
STRONG_EXCESS = 0.005
WEAK_WIN = 0.45
REVERSE_WIN = 0.40
REVERSE_EXCESS = -0.01

# ------------------------------------------------------------------ 虚拟交易（纸面）
# 规则全部来自推荐附带的 suggestion（买点/止损/三档止盈/持有天数），
# 这里只是执行口径，不参与选股，也不改任何选股参数。
PAPER_CAPITAL = 100_000         # 虚拟盘总资金（元）；建仓受现金约束，不是无限加仓
PAPER_MAX_POSITIONS = 8         # 同时持仓上限（放宽：不再死守 5 只）
PAPER_POSITION_PCT = 0.12       # 单笔预算占总资金比例，10 万 × 12% = 1.2 万/笔
PAPER_SCOPE = ("强推",)         # 虚拟盘只做当日强推，可关注仅记录不交易
PAPER_ENTRY_VALID_DAYS = 3      # 挂单有效交易日，逾期未触及买点则撤单
PAPER_FEE_BPS = 2.5             # 佣金 万2.5
PAPER_MIN_FEE = 5.0             # 单笔最低佣金
PAPER_STAMP_BPS = 5.0           # 印花税（仅卖出）万5
PAPER_TRANSFER_BPS = 0.1        # 过户费 十万分之一
PAPER_MIN_SAMPLE = 10           # 平仓样本少于此数不出策略结论
PAPER_CLOSE_HOUR = 15           # 当日 15:00 前不把当日 K 线当收盘数据

# 撮合约束（2026-10-22 起启用）
PAPER_SLIP_BPS = 5.0            # 滑点 万5：买入按限价封顶不让价，卖出按触发价再让 5bp
PAPER_TICK = 0.01               # 最小报价单位（元）
PAPER_ENFORCE_T1 = True         # 建仓当日不得卖出（A 股 T+1）
PAPER_ENFORCE_LIMIT = True      # 涨跌停一字板不可成交
PAPER_MINUTE_PATH = True        # 有分钟线时按分钟路径判定止盈止损先后

# ------------------------------------------------------ 站点数据同步（GitHub Pages）
# site/data/*.json --复制--> docs/data/*.json --commit+push--> Pages 自动重建
SITE_PUSH = True
SITE_DATA_DIR = os.path.join(ROOT, "site", "data")       # astocks export 的产物
SITE_PUBLISH_DIR = os.path.join(ROOT, "docs")            # Pages 的发布根目录
SITE_PUSH_SRC_PAGE = os.path.join(ROOT, "dist", "index.html")  # 建站产物，存在才同步
SITE_PUSH_REMOTE = "origin"
SITE_PUSH_BRANCH = "master"
SITE_PUSH_USER = "revrex1"
SITE_PUSH_EMAIL = "revrex1@users.noreply.github.com"
# 本机 github.com:443 被代理拦截，走 SSH + 仓库部署密钥（已在仓库 Settings 注册，只读=False）
SITE_PUSH_SSH_KEY = os.environ.get(
    "ASTOCKS_PUSH_SSH_KEY", os.path.join(os.path.expanduser("~"), ".ssh", "astocks_github"))
SITE_PUSH_TIMEOUT = 60          # 单个 git 命令超时（秒）
SITE_PUSH_PUSH_TIMEOUT = 120    # push/pull 这类联网操作超时（秒）
SITE_PUSH_STATUS = os.path.join(DATA_DIR, "site_push.json")  # 最近一次同步结果
