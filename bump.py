#!/usr/bin/env python3
"""Bump the project version number in _version.py.

Usage:
    python bump.py 1.5        # update _version.py from current to 1.5
    python bump.py --check     # show current version only

The script auto-stages _version.py via ``git add``.
You still need to commit and push manually.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VERSION_FILE = PROJECT_ROOT / "_version.py"


def _git(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + cmd, cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, check=False,
    )


def read_version() -> str:
    """Read __version__ from _version.py."""
    if not VERSION_FILE.exists():
        return "?"
    try:
        with open(VERSION_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("__version__"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "?"


def main() -> None:
    if len(sys.argv) < 2:
        print(f"当前版本: v{read_version()}")
        print(f"用法: python bump.py <新版本号>  例如: python bump.py 1.5")
        sys.exit(0)

    arg = sys.argv[1]

    if arg == "--check":
        print(f"v{read_version()}")
        return

    new_ver = arg
    old_ver = read_version()

    if old_ver == new_ver:
        print(f"版本号未变化 (v{old_ver})，无需更新。")
        sys.exit(0)

    try:
        with open(VERSION_FILE, "w") as fh:
            fh.write(f'__version__ = "{new_ver}"\n')
    except OSError as e:
        print(f"✗ 写入失败: {e}")
        sys.exit(1)

    _git(["add", str(VERSION_FILE)])
    print(f"✓ 版本号已更新: v{old_ver} → v{new_ver}")
    print(f"  请 commit 并 push:")
    print(f"    git commit -m 'chore: bump version to v{new_ver}'")
    print(f"    git push")


if __name__ == "__main__":
    main()
