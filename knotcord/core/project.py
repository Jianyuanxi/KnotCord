"""项目键：CLAUDE_PROJECT_DIR 优先；否则向上找 .git 取那层目录名；找不到取 cwd 目录名。不调 git。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def _name_of(p: Path) -> str:
    if p.name:
        return p.name
    # 盘符根目录：C:\ -> "C"
    return p.anchor.replace(":", "").replace("\\", "").replace("/", "") or "root"


def project_key(cwd: str, env: Mapping | None = None) -> str:
    env = os.environ if env is None else env
    forced = env.get("CLAUDE_PROJECT_DIR")
    if forced:
        return _name_of(Path(forced))
    start = Path(cwd) if cwd else Path.cwd()
    cur = start
    for _ in range(64):  # 防御：不会有这么深的目录
        try:
            if (cur / ".git").exists():  # 目录或文件（worktree）都算
                return _name_of(cur)
        except OSError:
            pass
        if cur.parent == cur:
            break
        cur = cur.parent
    return _name_of(start)
