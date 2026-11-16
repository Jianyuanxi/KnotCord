"""命令行与 hook 之外所有进程的启动入口：python -I -X utf8 <插件根>\\knotcord\\boot.py <子命令>。

-I 会关掉用户 site 和脚本目录，所以这里手工补两样（细则 6.1）：
  1. 插件根目录（让 import knotcord 可用）；
  2. 第三方依赖目录：~/.knotcord/venv/Lib/site-packages（存在才加）；没有 venv 时退回当前解释器的用户 site
     （本机的 mcp 装在那里）。环境变量 KNOTCORD_EXTRA_SITE 可额外指定一个目录，测试用。
不走 venv 的 python.exe 启动器：它会多一层进程，退出时杀不干净（细则 1.1）。
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _setup_path() -> None:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    home = os.environ.get("KNOTCORD_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".knotcord"
    cands = [os.environ.get("KNOTCORD_EXTRA_SITE"), str(base / "venv" / "Lib" / "site-packages")]
    added = False
    for c in cands:
        if c and os.path.isdir(c):
            site.addsitedir(c)
            added = True
    if not added:
        try:
            us = site.getusersitepackages()
        except Exception:
            us = None
        if us and os.path.isdir(us):
            site.addsitedir(us)


def run(argv: list[str] | None = None) -> int:
    _setup_path()
    from knotcord.cli import main
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())
