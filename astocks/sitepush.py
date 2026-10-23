"""站点数据同步与发布：把导出的 JSON 推到 GitHub Pages 仓库。

数据流
    site/data/*.json  ──复制──▶  docs/data/*.json  ──git commit+push──▶  GitHub Pages
    dist/index.html   ──复制──▶  docs/index.html

页面侧（docs/index.html）用 `DATA_BASE + "/index.json"` 取数，所以只要 docs/data
更新并推送成功，站点在 Pages 重新构建后（约 1 分钟）就是最新数据，无需重新发版。

设计约束
1. 只提交 docs/ 下这两个位置，绝不牵连仓库里其他未跟踪/未提交的改动。
2. 内容没变就不提交、不推送（幂等）——每天两次调用不会产生空提交。
3. 任何失败只返回结果、只写日志，不影响 open/close 主链路。
4. 所有 git 调用禁交互（GIT_TERMINAL_PROMPT=0）并带超时，避免定时任务挂死。
5. 推送被拒（远端有新提交）时自动 pull --rebase 后重试一次，仍失败则如实报告。
6. 网络限制说明：本机 github.com:443 被代理拦截（CONNECT 502），因此远端使用
   SSH 通道 + 仓库级部署密钥，见 config.SITE_PUSH_SSH_KEY。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import config, store

Logger = "callable"


def _noop(*_a, **_k) -> None:
    return None


def _git_bin() -> str | None:
    """定位 git：环境变量 → PATH → 常见安装位置（定时任务的 PATH 可能更窄）。"""
    cand = os.environ.get("ASTOCKS_GIT")
    if cand and os.path.isfile(cand):
        return cand
    found = shutil.which("git")
    if found:
        return found
    for p in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "git.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
        r"E:\Program Files\WorkBuddy\resources\vendor\PortableGit\mingw64\bin\git.EXE",
    ):
        if p and os.path.isfile(p):
            return p
    return None


def _env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"          # 绝不等待交互输入
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "echo")
    env["GIT_PAGER"] = "cat"
    key = config.SITE_PUSH_SSH_KEY
    if key and os.path.isfile(os.path.expanduser(key)):
        env["GIT_SSH_COMMAND"] = (
            f'ssh -i "{os.path.expanduser(key)}" -o IdentitiesOnly=yes '
            "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
        )
    return env


def _git(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    git = _git_bin()
    if not git:
        raise RuntimeError("找不到 git 可执行文件")
    return subprocess.run(
        [git, *args], cwd=config.ROOT, env=_env(), timeout=timeout or config.SITE_PUSH_TIMEOUT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _same(a: str, b: str) -> bool:
    if not os.path.isfile(b):
        return False
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def _pairs() -> list[tuple[str, str]]:
    """(源文件, 目标文件) 配对表。"""
    out: list[tuple[str, str]] = []
    src_dir = config.SITE_DATA_DIR
    dst_dir = os.path.join(config.SITE_PUBLISH_DIR, "data")
    if os.path.isdir(src_dir):
        for fn in sorted(os.listdir(src_dir)):
            if fn.endswith(".json"):
                out.append((os.path.join(src_dir, fn), os.path.join(dst_dir, fn)))
    dist_page = config.SITE_PUSH_SRC_PAGE
    if dist_page and os.path.isfile(dist_page):
        out.append((dist_page, os.path.join(config.SITE_PUBLISH_DIR, "index.html")))
    return out


def sync_files(dry_run: bool = False) -> list[str]:
    """按需复制，返回本次真正发生变化的仓库相对路径。"""
    changed: list[str] = []
    for src, dst in _pairs():
        if _same(src, dst):
            continue
        rel = os.path.relpath(dst, config.ROOT).replace("\\", "/")
        changed.append(rel)
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    return changed


def _rel(path: str) -> str:
    return os.path.relpath(path, config.ROOT).replace("\\", "/")


def _write_status(rec: dict) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.SITE_PUSH_STATUS, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001  状态文件写不进不影响结果
        pass


def _commit_msg(trade_date: str | None, changed: list[str]) -> str:
    head = f"数据更新：{trade_date}" if trade_date else "数据更新"
    data_n = sum(1 for c in changed if c.startswith("docs/data/"))
    page_n = len(changed) - data_n
    bits = [f"{data_n} 个数据文件"] if data_n else []
    if page_n:
        bits.append("站点页面")
    return f"{head}（{'、'.join(bits)}，自动）"


def _ahead(branch: str) -> int:
    """本地领先远端多少个提交（含上次推送失败遗留的）。跟踪分支不存在时按需推送处理。"""
    r = _git(["rev-list", "--count", f"refs/remotes/{config.SITE_PUSH_REMOTE}/{branch}..HEAD"],
             timeout=15)
    if r.returncode != 0:
        return 1
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return 1


def run(trade_date: str | None = None, *, dry_run: bool = False, force: bool = False,
        log=_noop) -> dict:
    """同步并推送。返回 {ok, pushed, changed, commit, reason, error}。"""
    rec: dict = {"at": store.now_iso(), "ok": False, "pushed": False,
                 "changed": [], "commit": None, "reason": None, "error": None}
    try:
        if not config.SITE_PUSH:
            rec.update(ok=True, reason="disabled")
            log("⏭  站点同步已关闭（config.SITE_PUSH=False）")
            return rec
        if not _git_bin():
            rec.update(reason="no-git", error="找不到 git")
            log("⚠  站点同步跳过：找不到 git 可执行文件")
            return rec

        top = _git(["rev-parse", "--show-toplevel"], timeout=15)
        if top.returncode != 0:
            rec.update(reason="not-a-repo", error=(top.stderr or "").strip()[:200])
            log(f"⚠  站点同步跳过：{config.ROOT} 不是 git 仓库")
            return rec

        changed = sync_files(dry_run=dry_run)
        rec["changed"] = changed
        if dry_run:
            ahead = _ahead(config.SITE_PUSH_BRANCH)
            rec.update(ok=True, reason="dry-run")
            log(f"📡 [dry-run] 待同步 {len(changed)} 个文件"
                + (f"：{', '.join(changed)}" if changed else "（无变化）")
                + f"；待推送提交 {ahead} 个")
            return rec

        branch = config.SITE_PUSH_BRANCH
        paths = [p for p in changed if os.path.exists(os.path.join(config.ROOT, p))]
        committed = False
        if paths or force:
            target = paths or [_rel(os.path.join(config.SITE_PUBLISH_DIR, "data")),
                               _rel(os.path.join(config.SITE_PUBLISH_DIR, "index.html"))]
            target = [p for p in target if os.path.exists(os.path.join(config.ROOT, p))]
            if target:
                add = _git(["add", "-A", "--", *target], timeout=30)
                if add.returncode != 0:
                    raise RuntimeError(f"git add 失败：{(add.stderr or '').strip()[:300]}")
                diff = _git(["diff", "--cached", "--quiet", "--", *target], timeout=20)
                if diff.returncode != 0 or force:
                    msg = _commit_msg(trade_date, paths or target)
                    commit = _git(["-c", f"user.name={config.SITE_PUSH_USER}",
                                   "-c", f"user.email={config.SITE_PUSH_EMAIL}",
                                   "commit", "-m", msg, "--", *target], timeout=30)
                    if commit.returncode != 0:
                        raise RuntimeError(
                            f"git commit 失败：{(commit.stderr or commit.stdout or '').strip()[:300]}")
                    sha = _git(["rev-parse", "--short", "HEAD"], timeout=15).stdout.strip()
                    rec["commit"] = sha
                    committed = True
                    log(f"✅ 已提交 {sha}：{msg}")

        # 上次推送失败会留下未推送提交：即使本次无文件变化也要补推，否则永远卡在本地
        ahead = _ahead(branch)
        if not committed and ahead == 0:
            rec.update(ok=True, reason="no-change")
            log("📡 站点数据无变化且无待推提交，跳过推送")
            return rec
        if ahead:
            log(f"📤 待推送提交 {ahead} 个")

        push = _git(["push", config.SITE_PUSH_REMOTE, f"HEAD:{branch}"],
                    timeout=config.SITE_PUSH_PUSH_TIMEOUT)
        if push.returncode != 0:
            err = (push.stderr or push.stdout or "").strip()
            rec["error"] = err[:400]
            # 远端有新提交时先变基再重试一次，避免定时任务永久卡住
            rebase = _git(["-c", "pull.rebase=true", "pull", "--rebase", "--autostash",
                           config.SITE_PUSH_REMOTE, branch], timeout=config.SITE_PUSH_PUSH_TIMEOUT)
            if rebase.returncode == 0:
                push = _git(["push", config.SITE_PUSH_REMOTE, f"HEAD:{branch}"],
                            timeout=config.SITE_PUSH_PUSH_TIMEOUT)
            if push.returncode != 0:
                rec["error"] = (push.stderr or push.stdout or err).strip()[:400]
                raise RuntimeError(f"git push 失败：{rec['error']}")

        rec.update(ok=True, pushed=True, reason="pushed")
        log(f"🚀 已推送 {len(paths)} 个文件 / {ahead or 1} 个提交到 "
            f"{config.SITE_PUSH_REMOTE}/{branch}，站点约 1 分钟后刷新")
    except subprocess.TimeoutExpired:
        rec.update(reason="timeout", error="git 操作超时")
        log("⚠  站点同步超时（不影响主链路）")
    except Exception as exc:  # noqa: BLE001  同步失败不阻断复盘
        rec["reason"] = rec.get("reason") or "error"
        rec["error"] = rec.get("error") or str(exc)[:400]
        log(f"⚠  站点同步失败（不影响主链路）：{exc}")
    finally:
        _write_status(rec)
    return rec


def status() -> dict | None:
    """最近一次同步结果（供报告/排查使用）。"""
    try:
        with open(config.SITE_PUSH_STATUS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None
