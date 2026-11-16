"""工具过滤名单（方案书第 2 节）。Bash 与 PowerShell 一视同仁。"""

from __future__ import annotations

import fnmatch

from . import config as _config

_SHELL_TOOLS = {"bash", "powershell"}


def _cmd_skipped(command: str, prefixes: list[str]) -> bool:
    cmd = command.strip().lower()
    candidates = [cmd]
    if cmd.startswith("rtk "):  # rtk 会把 git status 改写成 rtk git status
        candidates.append(cmd[4:].lstrip())
    for c in candidates:
        for p in prefixes:
            p = p.lower()
            # 前缀后必须是结尾或空白：ls 不匹配 lsof，type 不匹配 typecheck
            if c == p or (c.startswith(p) and c[len(p)].isspace()):
                return True
    return False


def skip_tool(name: str, tool_input: dict | None, cfg=None) -> bool:
    """True 表示这次工具调用不记。"""
    cfg = cfg or _config.load()
    name = name or ""
    low = name.lower()
    for pat in cfg.get("filters.skip_tools", []):
        if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(low, pat.lower()):
            return True
    for frag in cfg.get("filters.skip_name_contains", []):
        if frag.lower() in low:
            return True
    if low in _SHELL_TOOLS and isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and _cmd_skipped(command, cfg.get("filters.skip_cmd_prefixes", [])):
            return True
    return False
