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

# 项目地图里不参与的项目名（不分大小写，* 通配）；纯数字、长度小于 3 的另有规则，不用列
_PROJMAP_EXCLUDE = [
    # 通用目录名、系统目录名（会话在这些目录里开，项目名取成了目录名）
    "desktop", "date", "win", "app", "scripts", "temp", "tmp", "cache", "local", "roaming", "appdata", "memory",
    "program files*", "system32", "windows", "users", "documents", "downloads", "download", "src", "docs", "test",
    "tests", "bin", "lib", "build", "dist", "ppt", "workspace",
    # 临时会话、点开头的配置目录、路径编码出来的名字（D--xxx-desktop-----）
    "new-chat*", "scratch*", "scratchpad", "emptydir", ".*", "*--*", "新建*", "未命名*", "untitled*",
    # 课程里的泛称、Codex 自动起的目录名
    "实验*", "作业*", "files-mentioned*",
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
    "search.vector_min": 0.5,  # 向量一路的最低余弦相似度，低于它不进融合；0 = 不设。实测（3.7 万条库）：乱码/无关查询 top5 在 0.45~0.49，相关查询 top5 在 0.52~0.67
    "embed.batch": 8,  # 补向量：每批嵌几条（实测 8 比 32 省 100 MB 内存、速度不降）
    "embed.round_docs": 256,  # 补向量：worker 每一轮最多嵌几条（嵌完一轮回去处理队列再接着嵌）
    "embed.max_tokens": 256,  # 每条文本最多取前多少个 token（模型上限 512）
    "embed.threads": 4,  # onnxruntime 线程数
    "retain.log_days": 14,
    "retain.log_mb": 50,
    "retain.event_body_days": 180,
    "janitor.hours": 6,
    "worker.start_delay_s": 60,
    "recall.enabled": False,
    "codex.enabled": True,          # 扫描 Codex 会话目录；目录不存在时什么都不做
    "codex.scan_s": 60,             # 扫描间隔（秒）；多个 server 之间靠 kv 抢，同一间隔内只有一个真扫
    "codex.lookback_days": 2,       # 只看最近这么多天内改过的 .jsonl；调大会把更早的会话也补进来并触发提炼
    "codex.sessions_dir": "",       # 空 = $CODEX_HOME/sessions，再退回 ~/.codex/sessions
    "recall.budget_tokens": 400,
    "recall.max_items": 5,
    "recall.min_chars": 12,
    "recall.min_score": 7.5,    # 在真实库副本上标定：纯汉字提示里"相关"和"凑巧"的笔记都在 6～7 分，7.5 才稳
    "recall.min_coverage": 0.2,
    "recall.cross_project": False,
    "recall.timeout_ms": 600,
    "recall.query_chars": 300,
    "consolidate.enabled": True,
    "consolidate.include_legacy": False,
    "consolidate.title_similarity": 0.9,
    "consolidate.body_similarity": 0.8,
    "consolidate.title_min_chars": 8,
    "consolidate.stale_days": 90,
    "consolidate.stale_factor": 0.8,
    "consolidate.promote_min_uses": 5,
    "consolidate.promote_max": 5,
    "consolidate.promote_cooldown_days": 30,
    "search.related_boost": 1.1,  # 相关项目（projmap）的条目加分，必须小于 search.project_boost，search() 里会截到不超过它
    "projmap.enabled": True,
    "projmap.max": 3,             # 每个项目最多记几个相关项目
    "projmap.min_score": 25.0,    # 真实库副本上标定：25 分以下基本是一方的普通词撞上了另一个项目名（22 分的几对都不可信）
    "projmap.exclude": _PROJMAP_EXCLUDE,
    "corrections.similarity": 0.7,  # 纠错标题归"同类"的相似度，比去重的 0.9 宽：同一个错换个说法也算；库里还没有 correction 笔记，未在真实数据上标定
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
