"""目录布局。KNOTCORD_HOME 优先，否则 ~/.knotcord。tmp/logs/backups 用到时才建。"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    env = os.environ.get("KNOTCORD_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".knotcord"


def _ensure(sub: str) -> Path:
    p = home() / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    # 不建目录：只读连接不应该凭空造出 ~/.knotcord
    return home() / "knotcord.db"


def config_path() -> Path:
    return home() / "config.toml"


def tmp_dir() -> Path:
    return _ensure("tmp")


def logs_dir() -> Path:
    return _ensure("logs")


def backups_dir() -> Path:
    return _ensure("backups")
