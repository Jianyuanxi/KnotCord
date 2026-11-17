"""组一批的 prompt（方案书第 3 节"每批喂给模型的内容"）。

结构：观察日期 / 项目 / 现状卡 / 本会话当前总结 / 相关笔记（编号 1..n）/ 没做完的事（编号 1..n）/
相关内置记忆文件 / 本批操作记录（序号 1..n）。笔记和没做完的事用整数编号代替真实 id：省 token，也防模型编造 id。
最后整段再过一遍 redact（发出去就收不回）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

from knotcord.core import index
from knotcord.core.redact import redact

from .backlog import sec

NOTES_N = 15
UNFINISHED_N = 30
MEMFILES_N = 5
_NOTE_BODY_SHOW = 150


@dataclass
class PromptCtx:
    project: str = ""
    notes: list[int] = field(default_factory=list)            # 编号 n -> notes.id = notes[n-1]
    unfinished: list[dict] = field(default_factory=list)      # 编号 n -> {id, text}
    memfiles: list[dict] = field(default_factory=list)        # {path, name, label, mtime}
    event_ids: list[int] = field(default_factory=list)        # 序号 n -> events.id
    event_ts: list[int] = field(default_factory=list)         # 秒
    batch_text: str = ""                                      # 本批原文（刷新"没做完"的提及时间用）
    card_json: str | None = None


def _fmt(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(sec(ts)))


def event_text(ev) -> str:
    """一条事件的正文部分（不含序号和时间）。取字规则与 backlog._CHARS_SQL 一致。"""
    kind = ev["kind"]
    brief, body = ev["brief"] or "", ev["body"] or ""
    if kind == "tool":
        head = f"工具 {ev['tool'] or ''}: {brief}".rstrip()
        return head + (f"\n    {body}" if body else "")
    who = "用户" if kind == "prompt" else "助手"
    return f"{who}: {body or brief}"


def event_view(ev, lean_on: bool = True) -> str:
    """喂给模型的那一版正文：开着去噪就走 lean.render，否则是原文。"""
    if lean_on:
        from . import lean
        return lean.render(ev)
    return event_text(ev)


def event_len(ev, lean_on: bool = False) -> int:
    """切批时用的字数：正文 + 固定的序号/时间前缀。lean_on 时按去噪后的字数算（一批能装更多）。"""
    return len(event_view(ev, lean_on)) + 16


# ---------------------------------------------------------------- 检索用的查询词

_FILE_RE = re.compile(
    r"[\w.\-]+\.(?:py|ts|tsx|js|jsx|md|json5?|toml|ya?ml|rs|go|c|cc|cpp|h|hpp|java|kt|ets|html|css|sh|ps1|sql|db|"
    r"txt|cfg|ini|conf|xml|lock)\b", re.IGNORECASE)
_HOST_RE = re.compile(
    r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|cn|app|xyz|me|top|cc|ai))\b",
    re.IGNORECASE)


def _query(events) -> str:
    """文件名、主机名在前，再接用户提示原文的开头（fts_search 放宽成 OR 时只取前 64 个词，顺序有意义）。"""
    files: list[str] = []
    hosts: list[str] = []
    prompts: list[str] = []
    for ev in events:
        text = f"{ev['brief'] or ''} {ev['body'] or ''}"
        for m in _FILE_RE.findall(text):
            m = os.path.basename(m.replace("\\", "/"))
            if m.lower() not in (x.lower() for x in files):
                files.append(m)
        for m in _HOST_RE.findall(text):
            if m.lower() not in (x.lower() for x in hosts) and m.lower() not in (f.lower() for f in files):
                hosts.append(m)
        if ev["kind"] == "prompt" and len(prompts) < 4:
            prompts.append((ev["body"] or ev["brief"] or "")[:400])
    return " ".join(files[:10] + hosts[:5] + prompts)[:1500]


# ---------------------------------------------------------------- 取各项上下文

def _latest_card(conn, project: str) -> str | None:
    r = conn.execute("SELECT card FROM state_cards WHERE project=? ORDER BY ts DESC, id DESC LIMIT 1",
                     (project,)).fetchone()
    return r["card"] if r and r["card"] else None


def _session_summary(conn, session_id: str) -> dict | None:
    r = conn.execute("SELECT request, completed, learned, next_steps FROM summaries WHERE session_id=? "
                     "ORDER BY (origin='knotcord') DESC, id DESC LIMIT 1", (session_id,)).fetchone()
    return dict(r) if r else None


def _related_notes(conn, query: str, project: str) -> list[sqlite3.Row]:
    if not query.strip():
        return []
    hits = index.fts_search(conn, query, project=project, srcs=("note",), limit=NOTES_N * 2)
    out: list[sqlite3.Row] = []
    for h in hits:
        r = conn.execute("SELECT id, type, ts, title, body, slot FROM notes "
                         "WHERE id=? AND state='active' AND type<>'unfinished'", (h["src_id"],)).fetchone()
        if r:
            out.append(r)
        if len(out) >= NOTES_N:
            break
    return out


def _unfinished_rows(conn, project: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, ts, title, body FROM notes WHERE project=? AND type='unfinished' "
                        "AND state='active' ORDER BY ts DESC, id DESC LIMIT ?", (project, UNFINISHED_N)).fetchall()


def _related_memfiles(conn, query: str) -> list[dict]:
    if not query.strip():
        return []
    try:
        hits = index.fts_search(conn, query, srcs=("memfile",), limit=MEMFILES_N)
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for h in hits:
        r = conn.execute("SELECT path, name, description, mtime FROM memfiles WHERE id=?",
                         (h["src_id"],)).fetchone()
        if r:
            label = os.path.basename(r["path"] or "") or (r["name"] or "")
            out.append({"path": r["path"], "name": r["name"] or "", "label": label,
                        "description": r["description"] or "", "mtime": sec(r["mtime"])})
    return out


# ---------------------------------------------------------------- 组装

def build(conn, cfg, session_id: str, project: str, events: list, *, now: float,
          max_chars: int = 60000) -> tuple[str, PromptCtx]:
    ctx = PromptCtx(project=project)
    ctx.event_ids = [ev["id"] for ev in events]
    ctx.event_ts = [sec(ev["ts"]) for ev in events]

    query = _query(events)
    notes = _related_notes(conn, query, project)
    ctx.notes = [r["id"] for r in notes]
    unf = _unfinished_rows(conn, project)
    # body 是全文、title 是它的前 60 字：有 body 只给 body，别重复两遍
    ctx.unfinished = [{"id": r["id"], "text": r["body"] or r["title"] or ""}
                      for r in unf]
    ctx.memfiles = _related_memfiles(conn, query)
    ctx.card_json = _latest_card(conn, project)
    summ = _session_summary(conn, session_id)

    parts: list[str] = []
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    first, last = (_fmt(ctx.event_ts[0]), _fmt(ctx.event_ts[-1])) if events else ("", "")
    parts.append(f"# 观察日期\n今天是 {today}。本批操作记录的时间范围：{first} 至 {last}。"
                 "正文里的相对时间（昨天、刚才、上周）一律换成绝对日期。")
    parts.append(f"# 项目\n{project or '(未知)'}")
    parts.append("# 项目当前现状卡\n" + (ctx.card_json or "(还没有)"))
    if summ and any(summ.values()):
        parts.append("# 本会话当前总结\n" + json.dumps(summ, ensure_ascii=False))
    else:
        parts.append("# 本会话当前总结\n(还没有)")

    if notes:
        lines = []
        for i, r in enumerate(notes, 1):
            body = (r["body"] or "")[:_NOTE_BODY_SHOW]
            slot = f" slot={r['slot']}" if r["slot"] else ""
            lines.append(f"[{i}] {r['type']} {_fmt(r['ts'])[:5]}{slot} {r['title']}：{body}")
        parts.append("# 相关的现有笔记（supersedes 只能填这里的编号）\n" + "\n".join(lines))
    else:
        parts.append("# 相关的现有笔记（supersedes 只能填这里的编号）\n(没有)")

    if unf:
        lines = [f"[{i}] {_fmt(r['ts'])[:5]} {u['text'][:200]}" for i, (r, u) in enumerate(zip(unf, ctx.unfinished), 1)]
        parts.append("# 没做完的事（unfinished_resolved 只能填这里的编号）\n" + "\n".join(lines))
    else:
        parts.append("# 没做完的事（unfinished_resolved 只能填这里的编号）\n(没有)")

    if ctx.memfiles:
        lines = [f"- {m['label']}：{m['description'][:150]}" for m in ctx.memfiles]
        parts.append("# 相关的内置记忆文件（memory_conflicts 的 file 只能照抄这里的文件名）\n" + "\n".join(lines))

    lean_on = bool(cfg.get("extract.lean", True))
    lines = []
    for i, ev in enumerate(events, 1):
        line = f"[{i}] {_fmt(ev['ts'])} {event_view(ev, lean_on)}"
        if len(line) > max_chars:
            line = line[:max_chars] + "…(截断)"
        lines.append(line)
    ctx.batch_text = "\n".join(lines)
    parts.append("# 本批操作记录\n" + ctx.batch_text)

    return redact("\n\n".join(parts)), ctx
