"""配置：默认值照细则第 7 节，config.toml 存在就用 tomllib 覆盖。访问：cfg.get("extract.chars_codex")。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import paths

# 过滤名单默认值：方案书第 2 节
_SKIP_TOOLS = [
    "Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch", "TodoWrite", "Skill",
    "mcp__ccd_turn__*",
    "mcp__ccd_session__mark_chapter",
    # KnotCord 自己的工具、claude-mem 的 mcp-search（防止回灌）
    "mcp__plugin_knotcord_*", "mcp__knotcord__*",
    "mcp__plugin_claude-mem_*", "mcp__mcp-search__*",
]
_SKIP_NAME_CONTAINS = [
    "screenshot", "read_page", "get_page_text", "find", "read_console", "read_network",
]
_SKIP_CMD_PREFIXES = [
    "ls", "dir", "cat", "type", "head", "tail", "echo", "pwd", "which", "where",
    "git status", "git log", "git diff", "git show",
    "rtk read",
    "Get-ChildItem", "Get-Content", "Test-Path",
]

DEFAULTS: dict[str, Any] = {
    "inject.budget_tokens": 2000,
    "inject.hard_chars": 9000,
    "inject.card_tokens": 600,
    "inject.unfinished_max": 8,
    "inject.unfinished_days": 14,
    "extract.chars_codex": 30000,
    "extract.chars_deepseek": 12000,
    "extract.turns_codex": 8,
    "extract.turns_deepseek": 4,
    "extract.max_chars": 60000,
    "extract.idle_minutes": 15,
    "extract.timeout_s": 120,
    "extract.retries": 3,
    "budget.daily_calls": 150,
    "budget.daily_cny": 3.0,
    "lease.ttl_s": 90,
    "lease.heartbeat_s": 30,
    "drain.max_minutes": 10,
    "search.limit": 10,
    "search.candidates": 50,
    "search.rrf_k": 60,
    "search.project_boost": 1.2,
    "search.vector": True,
    "search.unload_minutes": 30,
    "retain.log_days": 14,
    "retain.log_mb": 50,
    "retain.event_body_days": 180,
    "janitor.hours": 6,
    "recall.enabled": False,
    "filters.skip_tools": _SKIP_TOOLS,
    "filters.skip_name_contains": _SKIP_NAME_CONTAINS,
    "filters.skip_cmd_prefixes": _SKIP_CMD_PREFIXES,
}


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return True


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


def load(path: Path | None = None) -> Config:
    data: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    p = path or paths.config_path()
    try:
        exists = p.exists()
    except OSError:
        exists = False
    if exists:
        try:
            with open(p, "rb") as f:
                user = _flatten(tomllib.load(f))
        except Exception as e:  # 坏配置不能拖垮 hook：回落默认值并记日志
            from . import log
            log.get_logger("config").warning("config.toml 解析失败，使用默认值: %s", e)
            user = {}
        for k, v in user.items():
            if k in DEFAULTS and not _type_ok(DEFAULTS[k], v):
                from . import log
                log.get_logger("config").warning("config.toml 的 %s 类型不对，忽略", k)
                continue
            data[k] = v
    return Config(data)
