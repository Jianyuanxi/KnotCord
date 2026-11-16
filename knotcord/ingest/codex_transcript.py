"""Codex CLI / Desktop 会话记录（~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<线程id>.jsonl）的增量采集。

和 claude_transcript 同一套口径：按字节位置增量读 → 先过滤、再脱敏、再精简 → 一个事务写 events、
读取位置、sessions 行、kv 元数据。写库、会话行、读取位置的小函数直接复用 claude_transcript 的。

只认 `event_msg` / `item_completed` 这一种行（本机 0.115 到 0.167 的文件都有；11-16 实测）：
- UserMessage          → prompt
- AgentMessage         → assistant（只收 phase=final_answer；老版本没有 phase 字段的按最终回答收）
- CommandExecution     → tool（PowerShell / Bash，输入和结果在同一条里）
- FileChange           → tool（Edit）
- McpToolCall          → tool（名字写成 mcp__<server>__<tool>，和 Claude 那边的过滤名单同一套写法）
- Reasoning、WebSearch、ImageView、SubAgentActivity、ContextCompaction 等：不记
不读 response_item：它带系统注入的 developer/user 消息和加密推理，子代理文件里还有继承来的父会话历史。

去重键：codex:<线程id>:<item.id>。老的外部导入会话 item.id 只是 "item-1"，所以必须带线程 id。
不采集：子代理线程（thread_source=subagent、guardian_review）、其他工具借 Codex 当模型的会话
（首条提示以 <observed_from_primary 开头）、已被 Codex 压缩成 .zst 的老文件（扫描那一层只看 .jsonl）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time

from knotcord.core import config as _config
from knotcord.core import db as _db
from knotcord.core import filters as _filters
from knotcord.core import log as _log
from knotcord.core import project as _project
from knotcord.core import redact as _redact
from . import claude_transcript as _ct
from . import trim as _trim

PREFIX = "codex:"
SKIP_KEY = "codex:skip:"          # kv：这个线程不采集（子代理等），值是原因
PROMPT_CAP = _ct.PROMPT_CAP
ASSISTANT_CAP = _ct.ASSISTANT_CAP
DIFF_KEEP = 600                   # FileChange 的 diff 在 body 里最多留这么多字

_SKIP_THREAD_SOURCES = ("subagent", "guardian_review")
_INTERNAL_PROMPT = ("<observed_from_primary",)
# 不是用户输入的"系统塞的"提示：Claude 那边的名单再加 Codex 自己注入的几种
_SKIP_PROMPT_PREFIX = _ct._SKIP_PROMPT_PREFIX + (
    "<environment_context", "<user_instructions", "<turn_aborted", "<subagent_notification",
    "# AGENTS.md instructions",
)
_MARKERS = (b'"item_completed"', b'"session_meta"')
_NAME_RE = re.compile(
    r"^rollout-.*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")
_CMD_FLAG = re.compile(r"-command|-[a-z]?c")
_META_PEEK =512 * 1024           # 第一行（session_meta 带整段系统指令，约 2 万字）最多读这么多

_log_ = _log.get_logger("ingest.codex")


def thread_id_of(path: str) -> str | None:
    """从文件名取线程 id；取不到就读第一行的 payload.id。"""
    m = _NAME_RE.match(os.path.basename(path))
    if m:
        return m.group(1).lower()
    try:
        with open(path, "rb") as f:
            first = f.readline(_META_PEEK)
        d = json.loads(first)
        pid = d["payload"]["id"]
        return pid if isinstance(pid, str) and pid else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- 解析（不碰数据库）

class _Parsed:
    __slots__ = ("ops", "cwd", "first_ts", "last_ts", "nlines", "meta", "internal", "prompts")

    def __init__(self) -> None:
        self.ops: list[dict] = []
        self.cwd: str | None = None
        self.first_ts: int | None = None
        self.last_ts: int | None = None
        self.nlines = 0
        self.meta: dict | None = None     # 第一条 session_meta 的 payload（只留判断用的几个字段）
        self.internal = False             # 首条提示是别的工具塞的
        self.prompts = 0


def _skip_reason(meta: dict | None) -> str | None:
    if not meta:
        return None
    ts = meta.get("thread_source")
    if ts in _SKIP_THREAD_SOURCES:
        return str(ts)
    if isinstance(meta.get("source"), dict):   # {"subagent": ...} 一类
        return "subagent"
    return None


def _text_of(blocks) -> str:
    """UserMessage 的块类型是 text，AgentMessage 的是 Text；图片（local_image）整块丢弃。"""
    if not isinstance(blocks, list):
        return ""
    parts = []
    for b in blocks:
        if isinstance(b, dict) and str(b.get("type", "")).lower() == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(parts)


def _clipped(text, code: int | None = None) -> _trim.Clipped:
    c = _trim.make_clipped(_trim.prepare_text(text if isinstance(text, str) else ""))
    return dataclasses.replace(c, exit_code=code) if code is not None else c


def _parse_block(block: bytes, first_line: int | None, sid: str, cfg) -> _Parsed:
    out = _Parsed()
    lines = block.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    out.nlines = len(lines)
    for i, raw in enumerate(lines):
        if not any(m in raw for m in _MARKERS):
            continue  # world_state、response_item、token_count 等一律跳过
        line = None if first_line is None else first_line + i
        try:
            d = json.loads(raw)
        except ValueError as e:
            _log_.warning("rollout 第 %s 行解析失败，跳过: %s", line, e)
            continue
        if not isinstance(d, dict):
            continue
        pl = d.get("payload")
        if not isinstance(pl, dict):
            continue
        kind = d.get("type")
        if kind == "session_meta":
            if out.meta is None:
                out.meta = {"thread_source": pl.get("thread_source"), "source": pl.get("source")}
                if isinstance(pl.get("cwd"), str) and pl["cwd"]:
                    out.cwd = pl["cwd"]
            continue
        if kind != "event_msg" or pl.get("type") != "item_completed":
            continue
        item = pl.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        ts = _ct._epoch(d.get("timestamp"))
        if ts is None:
            ts = out.last_ts or int(time.time())
        if out.first_ts is None:
            out.first_ts = ts
        out.last_ts = max(ts, out.last_ts or 0)
        try:
            _item_ops(item, f"{PREFIX}{sid}:{item['id']}", line, ts, cfg, out)
        except Exception as e:  # 一条怪结构不能卡住整个会话
            _log_.warning("rollout 第 %s 行处理失败，跳过: %r", line, e)
    return out


def _item_ops(item: dict, key: str, line, ts: int, cfg, out: _Parsed) -> None:
    ty = item.get("type")
    if ty == "UserMessage":
        text = _text_of(item.get("content"))
        out.prompts += 1
        if out.prompts == 1 and text.lstrip().startswith(_INTERNAL_PROMPT):
            out.internal = True
        if text.lstrip().startswith(_SKIP_PROMPT_PREFIX) or _redact.is_fully_private(text):
            return
        clean = _trim.prepare_text(text).strip()
        if clean:
            out.ops.append({"op": "prompt", "uuid": key, "ts": ts, "line": line,
                            "text": _trim.clip(clean, PROMPT_CAP)})
    elif ty == "AgentMessage":
        if item.get("phase") not in (None, "final_answer"):
            return  # commentary：边干活边说的话
        prepared = _trim.prepare_text(_text_of(item.get("content"))).strip()
        if prepared:
            out.ops.append({"op": "assistant", "uuid": key, "ts": ts, "line": line,
                            "text": _trim.clip(prepared, ASSISTANT_CAP), "key": _ct._akey(prepared)})
    elif ty == "CommandExecution":
        _command_ops(item, key, line, ts, cfg, out.ops)
    elif ty == "FileChange":
        _filechange_ops(item, key, line, ts, out.ops)
    elif ty == "McpToolCall":
        _mcp_ops(item, key, line, ts, cfg, out.ops)


def _shell_of(argv) -> tuple[str, str]:
    """(工具名, 脚本)。本机实测 argv 都是 [pwsh.exe, (-NoProfile,) -Command, 脚本]。"""
    if isinstance(argv, str):
        return "Bash", argv
    if not isinstance(argv, list) or not argv:
        return "Bash", ""
    argv = [str(x) for x in argv]
    exe = os.path.basename(argv[0].replace("\\", "/")).lower()
    name = "PowerShell" if ("pwsh" in exe or "powershell" in exe) else "Bash"
    script = ""
    for i in range(1, len(argv) - 1):  # -Command / -c / -lc（bash 常见的合并写法）后面那一项是脚本
        if _CMD_FLAG.fullmatch(argv[i].lower()):
            script = argv[i + 1]
            break
    return name, script or " ".join(argv[1:] if len(argv) > 1 else argv)


def _command_ops(item: dict, key: str, line, ts: int, cfg, ops: list) -> None:
    name, script = _shell_of(item.get("command"))
    inp = {"command": script}
    if _filters.skip_tool(name, inp, cfg):
        return
    brief, part = _trim.input_part(name, inp)
    code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
    failed = item.get("status") == "failed" or (code is not None and code != 0)
    ops.append({"op": "use", "uuid": key, "ts": ts, "line": line, "tool": name, "brief": brief, "body": part})
    ops.append({"op": "result", "uuid": key, "ts": ts, "line": line,
                "clip": _clipped(item.get("aggregated_output"), code), "error": failed})


def _filechange_ops(item: dict, key: str, line, ts: int, ops: list) -> None:
    changes = item.get("changes")
    if not isinstance(changes, dict) or not changes:
        return
    paths = [str(p) for p in changes]
    diffs = []
    for p, c in changes.items():
        if isinstance(c, dict) and isinstance(c.get("unified_diff"), str):
            diffs.append(c["unified_diff"])
    brief = _trim.oneline(paths[0] if len(paths) == 1 else f"{paths[0]} 等 {len(paths)} 个文件", 110)
    diff = _trim.clip(_trim.prepare_text("\n".join(diffs)), DIFF_KEEP) if diffs else ""
    part = _trim._safe_input("\n".join(paths) + (("\n" + diff) if diff else ""))
    failed = item.get("status") == "failed"
    ops.append({"op": "use", "uuid": key, "ts": ts, "line": line, "tool": "Edit", "brief": brief, "body": part})
    ops.append({"op": "result", "uuid": key, "ts": ts, "line": line,
                "clip": _clipped(item.get("stderr") or item.get("stdout")), "error": failed})


def _mcp_ops(item: dict, key: str, line, ts: int, cfg, ops: list) -> None:
    server, tool = item.get("server"), item.get("tool")
    if not isinstance(server, str) or not isinstance(tool, str):
        return
    name = f"mcp__{server}__{tool}"
    args = item.get("arguments")
    args = args if isinstance(args, dict) else {}
    if _filters.skip_tool(name, args, cfg):
        return
    brief, part = _trim.input_part(name, args)
    res = item.get("result")
    content = res.get("content") if isinstance(res, dict) else None
    failed = item.get("status") == "failed" or (isinstance(res, dict) and bool(res.get("isError")))
    ops.append({"op": "use", "uuid": key, "ts": ts, "line": line, "tool": name, "brief": brief, "body": part})
    ops.append({"op": "result", "uuid": key, "ts": ts, "line": line,
                "clip": _clipped(_ct._result_text(content)), "error": failed})


# ---------------------------------------------------------------- 读文件（和 claude_transcript._read_transcript 同构）

def _read_rollout(conn, sid: str, path: str, size: int, row, project, cwd, client, cfg) -> int:
    npath = os.path.normcase(os.path.abspath(path))
    meta = _ct._load_meta(conn, sid)
    offset = int(row["read_offset"] or 0) if row is not None else 0
    lines = meta.get("lines")
    k, head = int(meta.get("k") or 0), meta.get("h")
    if offset > 0:
        ok = size >= offset and (not meta.get("p") or meta["p"] == npath)
        if ok and k and head:
            try:
                ok = _ct._head_sha(path, k) == head
            except OSError:
                ok = False
        if not ok:
            _log_.warning("rollout 变短或开头变了，从头重读: %s", sid)
            offset, lines, k, head = 0, 0, 0, None
    else:
        lines, k, head = 0, 0, None
    if size <= offset:
        return 0

    total = 0
    pos = offset
    buf = b""
    state_project = project
    fresh = offset == 0
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            data = f.read(_ct.CHUNK_BYTES)
            if not data:
                break
            buf = buf + data if buf else data
            cut = buf.rfind(b"\n")
            if cut < 0:
                continue
            block, buf = buf[:cut + 1], buf[cut + 1:]
            parsed = _parse_block(block, None if lines is None else lines + 1, sid, cfg)
            if fresh:
                reason = _skip_reason(parsed.meta) or ("internal" if parsed.internal else None)
                if reason:
                    _db.kv_set(conn, SKIP_KEY + sid, reason)
                    return 0
                fresh = False
            if state_project is None and (cwd or parsed.cwd):
                # env={}：不让 CLAUDE_PROJECT_DIR 把别的会话的项目名套到 Codex 会话上
                state_project = _project.project_key(cwd or parsed.cwd, env={})
            new_pos = pos + len(block)
            if not k:
                k = min(_ct.HEAD_BYTES, new_pos)
                head = _ct._head_sha(path, k)
            new_lines = None if lines is None else lines + parsed.nlines
            with _db.write_tx(conn):
                total += _ct._apply(conn, sid, state_project, parsed.ops)
                _ct._upsert_session(conn, sid, client, state_project, cwd or parsed.cwd, path, new_pos,
                                    parsed.first_ts, parsed.last_ts)
                _db.kv_set(conn, _ct._meta_key(sid), json.dumps(
                    {"p": npath, "k": k, "h": head, "lines": new_lines}, separators=(",", ":")))
            pos, lines = new_pos, new_lines
    return total


# ---------------------------------------------------------------- 入口

def ingest_session(conn, session_id: str, transcript_path: str, *, cwd: str | None = None,
                   last_assistant_message: str | None = None, client: str = "codex") -> int:
    """签名和 claude_transcript.ingest_session 一致（work.ingest_one 按 client 分派）。返回新写入的 events 行数。"""
    cfg = _config.load()
    if _db.kv_get(conn, SKIP_KEY + session_id):
        return 0
    path = os.fspath(transcript_path) if transcript_path else ""
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    project = row["project"] if row is not None and row["project"] else None
    if project is None and cwd:
        project = _project.project_key(cwd, env={})
    total = 0
    st = None
    if path:
        try:
            st = os.stat(path)
        except OSError as e:
            _log_.warning("rollout 打不开 %s: %s", session_id, e)
    if st is not None:
        total += _read_rollout(conn, session_id, path, st.st_size, row, project, cwd, client, cfg)
        if project is None:
            r2 = conn.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
            project = r2[0] if r2 else None
    if last_assistant_message:
        total += _ct._record_last_message(conn, session_id, last_assistant_message, project, cwd, path, client)
    return total


def ingest_file(conn, path: str) -> int:
    """扫描用：只给文件路径，线程 id 从文件名取。"""
    sid = thread_id_of(path)
    if not sid:
        return 0
    return ingest_session(conn, sid, path)
