"""Claude Code transcript（~/.claude/projects/*/*.jsonl）的增量采集（细则 2.3「入库」）。

流程：二进制 seek 到 sessions.read_offset → 按块读完整行 → 解析（事务外）→ 一个事务里写 events、
读取位置、sessions 行、kv 里的元数据。块是 16 MB 一个事务，进程随时被杀都能接着读。
幂等靠 UNIQUE(uuid)：消息用行 uuid，工具调用用 tool_use_id。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime

from knotcord.core import config as _config
from knotcord.core import db as _db
from knotcord.core import filters as _filters
from knotcord.core import index as _index
from knotcord.core import log as _log
from knotcord.core import project as _project
from knotcord.core import redact as _redact
from . import trim as _trim

CHUNK_BYTES = 16 * 1024 * 1024   # 一个事务最多处理这么多字节（测试里会调小）
PROMPT_CAP = 10_000              # 用户提示全文；超长粘贴（整段日志）头尾截断
ASSISTANT_CAP = 4_000            # 每轮最终回答
HEAD_BYTES = 1024                # 文件开头指纹的长度

_MARKERS = (b'"type":"user"', b'"type":"assistant"', b'"type": "user"', b'"type": "assistant"')
_IMAGE_MARK = re.compile(r"\[Image: source: [^\]]*\]")
# 不是用户输入的"系统塞的"提示：命令回显、后台任务通知、观察者会话
_SKIP_PROMPT_PREFIX = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command-", "<task-notification>",
    "<observed_from_primary", "<scheduled-task", "<artifact-content-auth", "[Request interrupted by user",
)

_log_ = _log.get_logger("ingest")


def _epoch(s) -> int | None:
    if not isinstance(s, str):
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _akey(text: str) -> str:
    """同一段回答的指纹：压空白后取头尾各 200 字。长回答入库时被截过中段，头尾不变，指纹仍一致。"""
    c = " ".join(text.split())
    return hashlib.sha1((c[:200] + "\x00" + c[-200:]).encode("utf-8", "replace")).hexdigest()[:20]


# ---------------------------------------------------------------- 解析（不碰数据库）

class _Parsed:
    __slots__ = ("ops", "cwd", "first_ts", "last_ts", "nlines")

    def __init__(self) -> None:
        self.ops: list[dict] = []
        self.cwd: str | None = None
        self.first_ts: int | None = None
        self.last_ts: int | None = None
        self.nlines = 0


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 只取文字块，图片等其他块整块丢弃
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _prompt_op(d: dict, text: str, line: int | None, ts: int) -> dict | None:
    if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
        return None
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None  # task-notification、peer 等：不是用户敲的
    stripped = text.lstrip()
    if stripped.startswith(_SKIP_PROMPT_PREFIX):
        return None
    if _redact.is_fully_private(text):
        return None
    clean = _trim.prepare_text(_IMAGE_MARK.sub("", text)).strip()
    if not clean:
        return None
    return {"op": "prompt", "uuid": d["uuid"], "ts": ts, "line": line, "text": _trim.clip(clean, PROMPT_CAP)}


def _parse_block(block: bytes, first_line: int | None, cfg) -> _Parsed:
    out = _Parsed()
    skipped: set[str] = set()   # 本块里被过滤的 tool_use_id：结果也不用处理
    lines = block.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    out.nlines = len(lines)
    for i, raw in enumerate(lines):
        if not any(m in raw for m in _MARKERS):
            continue  # attachment、queue-operation 等十几种行，一律跳过
        line = None if first_line is None else first_line + i
        try:
            d = json.loads(raw)
        except ValueError as e:
            _log_.warning("transcript 第 %s 行解析失败，跳过: %s", line, e)
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
            continue
        uuid = d.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        ts = _epoch(d.get("timestamp"))
        if ts is None:
            ts = out.last_ts or int(time.time())
        if out.first_ts is None:
            out.first_ts = ts
        out.last_ts = max(ts, out.last_ts or 0)
        if out.cwd is None and isinstance(d.get("cwd"), str) and d["cwd"]:
            out.cwd = d["cwd"]
        try:
            if d["type"] == "assistant":
                _assistant_ops(d, uuid, line, ts, cfg, skipped, out.ops)
            else:
                _user_ops(d, uuid, line, ts, skipped, out.ops)
        except Exception as e:  # 一行的怪结构不能卡住整个会话
            _log_.warning("transcript 第 %s 行处理失败，跳过: %r", line, e)
    return out


def _assistant_ops(d: dict, uuid: str, line, ts: int, cfg, skipped: set, ops: list) -> None:
    msg = d.get("message")
    if not isinstance(msg, dict) or d.get("isApiErrorMessage") or msg.get("model") == "<synthetic>":
        return
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return
    final = msg.get("stop_reason") not in (None, "tool_use")  # 边调工具边说的话 stop_reason 是 tool_use
    texts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif bt == "tool_use":
            tid, name = b.get("id"), b.get("name")
            if not isinstance(tid, str) or not isinstance(name, str):
                continue
            inp = b.get("input")
            if _filters.skip_tool(name, inp if isinstance(inp, dict) else None, cfg):
                skipped.add(tid)
                continue
            brief, part = _trim.input_part(name, inp)
            ops.append({"op": "use", "uuid": tid, "ts": ts, "line": line, "tool": name,
                        "brief": brief, "body": part})
    if final and texts:
        prepared = _trim.prepare_text("\n\n".join(texts)).strip()
        if prepared:
            ops.append({"op": "assistant", "uuid": uuid, "ts": ts, "line": line,
                        "text": _trim.clip(prepared, ASSISTANT_CAP), "key": _akey(prepared)})


def _user_ops(d: dict, uuid: str, line, ts: int, skipped: set, ops: list) -> None:
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts: list[str] = []
        has_result = False
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                has_result = True
                tid = b.get("tool_use_id")
                if isinstance(tid, str) and tid not in skipped:
                    prepared = _trim.prepare_text(_result_text(b.get("content")))
                    ops.append({"op": "result", "uuid": tid, "ts": ts, "line": line,
                                "clip": _trim.make_clipped(prepared), "error": bool(b.get("is_error"))})
            elif b.get("type") == "text" and isinstance(b.get("text"), str):
                texts.append(b["text"])
        if has_result:
            return
        text = "\n".join(texts)
    else:
        return
    op = _prompt_op(d, text, line, ts)
    if op:
        ops.append(op)


# ---------------------------------------------------------------- 写库

_INS = ("INSERT OR IGNORE INTO events(session_id, project, ts, uuid, kind, tool, brief, body, src_line) "
        "VALUES(?,?,?,?,?,?,?,?,?)")


def _apply(conn, sid: str, project: str | None, ops: list[dict]) -> int:
    new = 0
    for op in ops:
        kind = op["op"]
        if kind == "prompt":
            cur = conn.execute(_INS, (sid, project, op["ts"], op["uuid"], "prompt", None,
                                      _trim.oneline(op["text"], 80), op["text"], op["line"]))
            if cur.rowcount == 1:
                new += 1
                _index.index_doc(conn, "prompt", cur.lastrowid, project or "", op["ts"], op["text"])
        elif kind == "assistant":
            if conn.execute("SELECT 1 FROM events WHERE uuid=?", (op["uuid"],)).fetchone():
                continue
            row = conn.execute("SELECT id FROM events WHERE uuid=?", (f"lam:{sid}:{op['key']}",)).fetchone()
            if row:  # Stop 时先记过这段回答：把它认成 transcript 里的这一行，不再记第二遍
                conn.execute("UPDATE events SET uuid=?, ts=?, src_line=? WHERE id=?",
                             (op["uuid"], op["ts"], op["line"], row[0]))
                continue
            cur = conn.execute(_INS, (sid, project, op["ts"], op["uuid"], "assistant", None,
                                      _trim.oneline(op["text"], 80), op["text"], op["line"]))
            new += cur.rowcount
        elif kind == "use":
            cur = conn.execute(_INS, (sid, project, op["ts"], op["uuid"], "tool", op["tool"],
                                      op["brief"], op["body"], op["line"]))
            new += cur.rowcount
        elif kind == "result":
            row = conn.execute("SELECT id, tool, body FROM events WHERE uuid=? AND kind='tool'",
                               (op["uuid"],)).fetchone()
            if row is None:
                continue  # 被过滤的工具，或调用行不在库里
            part, _ = _trim.split_body(row["body"])
            body = _trim.join_body(part, _trim.result_part(row["tool"], op["clip"], op["error"]))
            if body != row["body"]:
                conn.execute("UPDATE events SET body=? WHERE id=?", (body, row["id"]))
    return new


def _upsert_session(conn, sid: str, client: str, project, cwd, path: str, offset: int,
                    first_ts, last_ts) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions(id, client, project, cwd, transcript_path, read_offset, started_at, last_seen) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "client=coalesce(client, excluded.client), project=coalesce(project, excluded.project), "
        "cwd=coalesce(cwd, excluded.cwd), transcript_path=excluded.transcript_path, "
        "read_offset=excluded.read_offset, started_at=coalesce(started_at, excluded.started_at), "
        "last_seen=max(coalesce(last_seen, 0), excluded.last_seen)",
        (sid, client, project, cwd, path, offset, first_ts or now, last_ts or first_ts or now),
    )


# ---------------------------------------------------------------- 读取位置和文件指纹

def _meta_key(sid: str) -> str:
    return f"ingest:{sid}"


def _load_meta(conn, sid: str) -> dict:
    raw = _db.kv_get(conn, _meta_key(sid))
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        return m if isinstance(m, dict) else {}
    except ValueError:
        return {}


def _head_sha(path: str, k: int) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(k)).hexdigest()


def _read_transcript(conn, sid: str, path: str, size: int, row, project, cwd, client, cfg) -> int:
    npath = os.path.normcase(os.path.abspath(path))
    meta = _load_meta(conn, sid)
    offset = int(row["read_offset"] or 0) if row is not None else 0
    lines = meta.get("lines")  # 已读行数；未知（别的代码建的 sessions 行）就不写 src_line
    k, head = int(meta.get("k") or 0), meta.get("h")
    if offset > 0:
        ok = size >= offset and (not meta.get("p") or meta["p"] == npath)
        if ok and k and head:
            try:
                ok = _head_sha(path, k) == head
            except OSError:
                ok = False
        if not ok:
            _log_.warning("transcript 变短或开头变了，从头重读: %s", sid)
            offset, lines, k, head = 0, 0, 0, None
    else:
        lines, k, head = 0, 0, None
    if size <= offset:
        return 0

    total = 0
    pos = offset
    buf = b""
    state_project = project
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            data = f.read(CHUNK_BYTES)
            if not data:
                break
            buf = buf + data if buf else data
            cut = buf.rfind(b"\n")
            if cut < 0:
                continue  # 还没读到整行
            block, buf = buf[:cut + 1], buf[cut + 1:]
            parsed = _parse_block(block, None if lines is None else lines + 1, cfg)
            if state_project is None and (cwd or parsed.cwd):
                state_project = _project.project_key(cwd or parsed.cwd)
            new_pos = pos + len(block)
            if not k:  # 文件开头指纹：下次读之前用它确认文件没被换掉
                k = min(HEAD_BYTES, new_pos)
                head = _head_sha(path, k)
            new_lines = None if lines is None else lines + parsed.nlines
            with _db.write_tx(conn):
                total += _apply(conn, sid, state_project, parsed.ops)
                _upsert_session(conn, sid, client, state_project, cwd or parsed.cwd, path, new_pos,
                                parsed.first_ts, parsed.last_ts)
                _db.kv_set(conn, _meta_key(sid), json.dumps(
                    {"p": npath, "k": k, "h": head, "lines": new_lines}, separators=(",", ":")))
            pos, lines = new_pos, new_lines
    return total


# ---------------------------------------------------------------- Stop 时的最后一条回答

def _record_last_message(conn, sid: str, text: str, project, cwd, path: str, client: str) -> int:
    prepared = _trim.prepare_text(text).strip()
    if not prepared:
        return 0
    key = _akey(prepared)
    lam = f"lam:{sid}:{key}"
    now = int(time.time())
    with _db.write_tx(conn):
        _upsert_session_if_missing(conn, sid, client, project, cwd, path, now)
        if conn.execute("SELECT 1 FROM events WHERE uuid=?", (lam,)).fetchone():
            return 0
        last_prompt = conn.execute(
            "SELECT coalesce(max(id), 0) FROM events WHERE session_id=? AND kind='prompt'", (sid,)).fetchone()[0]
        for r in conn.execute(
                "SELECT body FROM events WHERE session_id=? AND kind='assistant' AND id>? "
                "AND uuid NOT LIKE 'lam:%' ORDER BY id DESC LIMIT 10", (sid, last_prompt)):
            if _akey(r[0] or "") == key:
                return 0  # transcript 里已经有这一段了
        body = _trim.clip(prepared, ASSISTANT_CAP)
        cur = conn.execute(_INS, (sid, project, now, lam, "assistant", None, _trim.oneline(body, 80), body, None))
        return cur.rowcount


def _upsert_session_if_missing(conn, sid, client, project, cwd, path, now) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions(id, client, project, cwd, transcript_path, read_offset, started_at, last_seen) "
        "VALUES(?,?,?,?,?,0,?,?)", (sid, client, project, cwd, path or None, now, now))


# ---------------------------------------------------------------- 入口

def ingest_session(conn, session_id: str, transcript_path: str, *, cwd: str | None = None,
                   last_assistant_message: str | None = None, client: str = "claude") -> int:
    """从 sessions.read_offset 起增量读，返回新写入的 events 行数。

    数据库错误会原样抛出（事务已回滚、位置没动），调用方据此决定 pending 行是否保留。
    """
    cfg = _config.load()
    path = os.fspath(transcript_path) if transcript_path else ""
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    project = row["project"] if row is not None and row["project"] else None
    if project is None and cwd:
        project = _project.project_key(cwd)
    total = 0
    st = None
    if path:
        try:
            st = os.stat(path)
        except OSError as e:
            _log_.warning("transcript 打不开 %s: %s", session_id, e)
    if st is not None:
        total += _read_transcript(conn, session_id, path, st.st_size, row, project, cwd, client, cfg)
        if project is None:
            r2 = conn.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
            project = r2[0] if r2 else None
    if last_assistant_message:
        total += _record_last_message(conn, session_id, last_assistant_message, project, cwd, path, client)
    return total
