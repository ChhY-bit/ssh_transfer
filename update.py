#!/usr/bin/env python3
"""
SSH Transfer — auto-update from GitHub.

Fetches the latest version of the project from GitHub and applies it.
Useful for headless servers where pulling manually is inconvenient.

Usage:
    python update.py                  # update to latest (requires clean tree)
    python update.py --check          # only check for new commits, don't apply
    python update.py --force          # discard local changes before updating
    python update.py --deps           # auto-detect env & update dependencies
    python update.py --deps pip       # force pip install -r requirements.txt
    python update.py --deps conda     # force conda env update
    python update.py --branch dev     # track a different branch (default: main)
    python update.py --source gitee   # fetch from Gitee instead of GitHub

The script must be run from inside the project directory (where .git lives).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_URL = "https://github.com/ChhY-bit/ssh_transfer.git"
GITEE_URL = "https://gitee.com/ChhY-bit/ssh_transfer.git"
DEFAULT_BRANCH = "main"
DEFAULT_SOURCE = "github"

# ANSI colours (None if stdout is not a tty)
_INFO = "\033[1;36m" if sys.stdout.isatty() else ""
_OK = "\033[1;32m" if sys.stdout.isatty() else ""
_WARN = "\033[1;33m" if sys.stdout.isatty() else ""
_ERR = "\033[1;31m" if sys.stdout.isatty() else ""
_BOLD = "\033[1m" if sys.stdout.isatty() else ""
_RST = "\033[0m" if sys.stdout.isatty() else ""


# ---------------------------------------------------------------------------
# Version reading (direct file read to survive git pull)
# ---------------------------------------------------------------------------

def _read_version() -> str:
    """Read __version__ from _version.py.  Returns '?' on failure."""
    ver_file = PROJECT_ROOT / "_version.py"
    if not ver_file.exists():
        return "?"
    try:
        with open(ver_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("__version__"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "?"


def _git_short_hash() -> str:
    """Return short (7-char) git commit hash, or empty string."""
    result = _git(["rev-parse", "--short", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""



# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def _is_conda_env() -> bool:
    """Return True if we're running inside an active conda environment."""
    return bool(os.environ.get("CONDA_PREFIX", ""))

def _conda_env_name() -> str:
    """Return the active conda environment name, or empty string."""
    return os.environ.get("CONDA_DEFAULT_ENV", "")

def _is_venv() -> bool:
    """Return True if we're inside a Python virtual environment (venv/virtualenv)."""
    return bool(os.environ.get("VIRTUAL_ENV", ""))

def _detect_env() -> str:
    """Return 'conda', 'venv', or 'system' based on the current Python environment."""
    if _is_conda_env():
        return "conda"
    if _is_venv():
        return "venv"
    return "system"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess.  Prints stderr on failure."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print(f"{_ERR}命令未找到: {cmd[0]}{_RST}")
        sys.exit(1)


def _git(cmd: list[str]) -> subprocess.CompletedProcess:
    """Shorthand for ``git <cmd>`` inside the project root."""
    return _run(["git"] + cmd, cwd=str(PROJECT_ROOT))


def _is_git_repo() -> bool:
    """Return True if PROJECT_ROOT is a git working tree."""
    return (PROJECT_ROOT / ".git").exists()


def _is_clean() -> bool:
    """Return True if the working tree has no uncommitted changes."""
    result = _git(["status", "--porcelain"])
    return result.returncode == 0 and result.stdout.strip() == ""


def _current_branch() -> str:
    """Return the current branch name, or empty string on failure."""
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _commits_behind(branch: str) -> int:
    """Return how many commits local HEAD is behind origin/<branch>."""
    result = _git(["rev-list", "--count", f"HEAD..origin/{branch}"])
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def _new_commits(branch: str) -> list[str]:
    """Return one-line log of commits that remote has but local doesn't."""
    result = _git(
        ["log", "--oneline", "--no-decorate", f"HEAD..origin/{branch}"]
    )
    return [l for l in result.stdout.strip().split("\n") if l] if result.returncode == 0 else []


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _source_url(source: str) -> str:
    """Return the remote URL for the given source name."""
    return GITEE_URL if source == "gitee" else GITHUB_URL


def check(branch: str, source: str = DEFAULT_SOURCE) -> int:
    """Fetch from remote and report how many new commits are available.

    Returns the number of commits behind (0 = up-to-date).
    """
    url = _source_url(source)
    print(f"{_INFO}⟳ 正在从 {source} ({url}) 获取更新…{_RST}")
    # Fetch by URL directly — never change origin, so user's push target is untouched
    result = _git(["fetch", url, f"{branch}:refs/remotes/origin/{branch}"])
    if result.returncode != 0:
        print(f"{_ERR}✗ 无法连接 ({source}){_RST}")
        print(result.stderr)
        return -1

    behind = _commits_behind(branch)
    if behind == 0:
        print(f"{_OK}✓ 已是最新版本 (v{_read_version()}){_RST}")
        return 0
    elif behind > 0:
        commits = _new_commits(branch)
        print(f"{_WARN}⬆ {behind} 个新提交可用:{_RST}")
        for c in commits:
            print(f"    {c}")
        return behind
    else:
        print(f"{_ERR}✗ 无法比较版本（远程分支 origin/{branch} 存在吗？）{_RST}")
        return -1


def _update_deps(method: str) -> bool:
    """Install/update dependencies using the specified package manager.

    *method* is one of ``"auto"``, ``"pip"``, or ``"conda"``.
    When ``"auto"``, the currently active environment is detected.
    """
    if method == "auto":
        env_type = _detect_env()
    else:
        env_type = method  # "pip" or "conda"

    print(f"{_INFO}当前环境: {env_type}{_RST}")

    if env_type == "conda":
        # Use conda env update
        yml = PROJECT_ROOT / "environment.yml"
        if not yml.exists():
            print(f"{_WARN}⚠ environment.yml 不存在，回退到 pip{_RST}")
            return _pip_install()

        name = _conda_env_name()
        print(f"{_INFO}⟳ conda env update ({name or 'ssh_transfer'}) …{_RST}")
        result = _run([
            "conda", "env", "update",
            "-f", str(yml),
            "--prune",
        ])
        if result.returncode != 0:
            # conda env update may fail if the env wasn't created from the file
            print(f"{_WARN}⚠ conda env update 失败，尝试 pip 回退…{_RST}")
            return _pip_install()
        print(f"{_OK}✓ conda 依赖已更新{_RST}")
        return True

    elif env_type == "pip":
        return _pip_install()

    else:
        # Bare system Python — warn but try pip anyway
        print(f"{_WARN}⚠ 未检测到虚拟环境，将直接 pip install（可能需 sudo）{_RST}")
        return _pip_install()


def _pip_install() -> bool:
    """Run ``pip install -r requirements.txt``.  Return True on success."""
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        print(f"{_WARN}⚠ requirements.txt 不存在{_RST}")
        return False
    result = _run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)]
    )
    if result.returncode == 0:
        print(f"{_OK}✓ pip 依赖已更新{_RST}")
        return True
    else:
        print(f"{_WARN}⚠ pip install 失败，请手动检查{_RST}")
        if result.stderr:
            print(result.stderr.strip()[-500:])
        return False


def apply(branch: str, force: bool, deps: str, source: str = DEFAULT_SOURCE) -> bool:
    """Fetch and pull the latest commits.  Return True on success."""
    url = _source_url(source)

    # 1. Fetch by URL directly — never change origin, so user's push target is untouched
    print(f"{_INFO}⟳ 获取远程更新 ({source})…{_RST}")
    result = _git(["fetch", url, f"{branch}:refs/remotes/origin/{branch}"])
    if result.returncode != 0:
        print(f"{_ERR}✗ 无法连接 ({source}){_RST}")
        return False

    behind = _commits_behind(branch)
    if behind == 0:
        print(f"{_OK}✓ 已是最新版本 (v{_read_version()})，无需更新{_RST}")
        return True
    elif behind < 0:
        print(f"{_WARN}本地领先远程 (v{_read_version()})，跳过拉取{_RST}")
        return True

    commits = _new_commits(branch)
    print(f"{_INFO}发现 {behind} 个新提交:{_RST}")
    for c in commits:
        print(f"    {c}")

    # 2. Snapshot current version
    old_ver = _read_version()

    # 3. Handle dirty tree
    if not _is_clean():
        if force:
            print(f"{_WARN}⚠ 工作区有未提交的更改，正在丢弃…{_RST}")
            _git(["reset", "--hard"])
            _git(["clean", "-fd"])
        else:
            print(
                f"{_ERR}✗ 工作区有未提交的更改。{_RST}\n"
                f"    请先 commit 或 stash，或使用 {_BOLD}--force{_RST} 强制覆盖。"
            )
            return False

    # 4. Merge (already fetched, no network needed)
    print(f"{_INFO}⟳ 正在合并更新…{_RST}")
    merge = _git(["merge", "--ff-only", f"origin/{branch}"])
    if merge.returncode != 0:
        # Try rebase as fallback
        print(f"{_WARN}fast-forward 失败，尝试 rebase…{_RST}")
        merge = _git(["rebase", f"origin/{branch}"])
        if merge.returncode != 0:
            print(f"{_ERR}✗ 合并失败:{_RST}")
            print(merge.stderr)
            return False

    new_ver = _read_version()
    if old_ver != "?" and new_ver != "?" and old_ver != new_ver:
        print(f"{_OK}✓ 已更新: {_BOLD}v{old_ver} → v{new_ver}{_RST}")
    else:
        print(f"{_OK}✓ 已更新到最新版本{_RST}")
    print(merge.stdout.strip() if merge.stdout.strip() else "  (fast-forward)")

    # 5. Dependencies
    if deps:
        _update_deps(deps)

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global PROJECT_ROOT
    PROJECT_ROOT = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="SSH Transfer — 自动更新到最新版本（GitHub / Gitee）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            示例:
              {sys.argv[0]}                        # 从 GitHub 更新
              {sys.argv[0]} --source gitee          # 从 Gitee 更新
              {sys.argv[0]} --check                 # 仅检查是否有更新
              {sys.argv[0]} --force                 # 丢弃本地修改后更新
              {sys.argv[0]} --deps                  # 更新后同步安装依赖
              {sys.argv[0]} --branch dev            # 使用 dev 分支
        """),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="仅检查新版本，不实际更新",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="丢弃本地未提交的更改后强制更新",
    )
    parser.add_argument(
        "--deps", nargs="?", const="auto", default="",
        choices=["auto", "pip", "conda"],
        help="更新后同步更新依赖: auto (自动检测), pip, conda",
    )
    parser.add_argument(
        "--branch", default=DEFAULT_BRANCH,
        help=f"跟踪的分支名（默认: {DEFAULT_BRANCH}）",
    )
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE,
        choices=["github", "gitee"],
        help=f"更新来源（默认: {DEFAULT_SOURCE}，可选: github / gitee）",
    )
    args = parser.parse_args()

    # -- preamble ------------------------------------------------------------

    print(f"{_BOLD}SSH Transfer — 自动更新{_RST}")
    sha = _git_short_hash()
    sha_str = f" ({sha})" if sha else ""
    print(f"  当前版本: v{_read_version()}{sha_str}")
    print(f"  项目路径: {PROJECT_ROOT}")

    if not _is_git_repo():
        print(f"{_ERR}✗ 当前目录不是 Git 仓库{_RST}")
        print(f"  请先克隆项目: git clone {GITHUB_URL}")
        sys.exit(1)

    branch = args.branch
    source = args.source
    current = _current_branch()
    if current and current != branch:
        print(f"{_WARN}⚠ 当前在 '{current}' 分支，将跟踪 '{branch}'{_RST}")

    # -- action --------------------------------------------------------------

    if args.check:
        behind = check(branch, source=source)
        if behind > 0:
            print(f"\n{_INFO}运行 {_BOLD}python update.py{_INFO} 来应用更新。{_RST}")
        sys.exit(0 if behind >= 0 else 1)
    else:
        ok = apply(branch, force=args.force, deps=args.deps, source=source)
        if ok:
            print(f"\n{_OK}{_BOLD}✓ 更新完成！{_RST}")
            print(f"  启动: {_BOLD}python tui.py{_RST} (终端) 或 {_BOLD}python gui.py{_RST} (图形)")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
