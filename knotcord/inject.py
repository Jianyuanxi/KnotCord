"""开头注入文本（方案书第 4 节）。只许 import 标准库和 knotcord.core。

render()：worker/drain 在提炼落库后整段重拼，存进 kv 的 inject:<项目>。
read_for_hook()：SessionStart 只读 kv，再补第 7 项（最新会话还没提炼的提示原文前 80 字）。
装填：按顺序贪心，装不下的块整块跳过、继续试后面的；token 预算和 9000 字符硬上限都要满足。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from knotcord.core import config as _config
from knotcord.core import db as _db
from knotcord.core import log as _log
from knotcord.core import tokens as _tokens

_log_ = _log.get_logger("inject")

HEADER = "翻旧账：内置记忆 → knotcord search/timeline/get → ~/.claude/projects/**/*.jsonl"
_CLOSE = "</knotcord-context>"
_KEY_POINTS_MAX = 8
_REQUEST_MAX = 50
_DONE_MAX = 60
_KEY_DAYS = 7
_TAIL_CHARS = 80
PROMOTE_PREFIX = "promote:"  # kv 键 promote:<项目>，由 consolidate.nominate 写
_PROMOTE_SHOW = 3
_PROMOTE_TITLE = 40
_SENT_END = re.compile(r"[。！？!?；;\n]|\.(?=\s|$)")


def _one(text) -> str:
    """压成一行，并防止内容里的标签把外层标签提前闭合。"""
    s = " ".join(str(text or "").split())
    return s.replace("<knotcord-context", "<​knotcord-context").replace(_CLOSE, "<​/knotcord-context>")


def _md(ts) -> str:
    try:
        return time.strftime("%m-%d", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "??-??"


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    m = _SENT_END.search(text)
    s = text[:m.start()] if m else text
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _head(project: str, now: float) -> str:
    p = str(project).replace('"', "'")
    return f'<knotcord-context project="{p}" at="{time.strftime("%m-%d %H:%M", time.localtime(now))}">\n{HEADER}\n'


# ---------------------------------------------------------------- 各块

def _join_val(v) -> str:
    if isinstance(v, (list, tuple)):
        return "；".join(_one(x) for x in v if str(x or "").strip())
    return _one(v)


def _card_block(conn, project: str, cfg) -> str:
    row = conn.execute("SELECT card FROM state_cards WHERE project=? ORDER BY ts DESC, id DESC LIMIT 1",
                       (project,)).fetchone()
    if row is None:
        return ""
    try:
        card = json.loads(row[0])
    except (TypeError, ValueError):
        _log_.warning("现状卡不是合法 JSON，跳过: %s", project)
        return ""
    if not isinstance(card, dict):
        return ""
    cap = int(cfg.get("inject.card_tokens", 600))

    def build(max_items: int | None) -> str:
        lines = []
        for label, key in (("目标", "goal"), ("现状", "status"), ("决定", "decisions"),
                           ("坑", "pitfalls"), ("下一步", "next")):
            v = card.get(key)
            if isinstance(v, (list, tuple)) and max_items is not None:
                v = list(v)[:max_items]
            s = _join_val(v)
            if s:
                lines.append(f"{label}：{s}")
        return ("## 现状\n" + "\n".join(lines) + "\n") if lines else ""

    for mi in (None, 3, 2, 1, 0):  # 超过卡的 token 上限：先砍各列表的尾部
        block = build(mi)
        if not block:
            return ""
        if _tokens.estimate(block) <= cap:
            return block
    return ""


def _unfinished_block(conn, project: str, cfg, now: float) -> str:
    cutoff = now - int(cfg.get("inject.unfinished_days", 14)) * 86400
    rows = conn.execute(
        "SELECT id, ts, title, body FROM notes WHERE project=? AND type='unfinished' AND state='active' "
        "AND coalesce(last_mentioned, ts) >= ? ORDER BY coalesce(last_mentioned, ts) DESC, id DESC LIMIT ?",
        (project, cutoff, int(cfg.get("inject.unfinished_max", 8)))).fetchall()
    lines = [f"u{r['id']} {_md(r['ts'])} {_one(r['title'] or r['body'])[:120]}" for r in rows]
    return ("## 没做完\n" + "\n".join(lines) + "\n") if lines else ""


def _resolve_memfile(conn, file: str) -> str | None:
    if os.path.isabs(file):
        return file
    row = conn.execute("SELECT path FROM memfiles WHERE name=? OR path=?", (file, file)).fetchone()
    if row:
        return row[0]
    tail = file.replace("/", "\\").lstrip("\\")
    for (p,) in conn.execute("SELECT path FROM memfiles"):
        if p and p.replace("/", "\\").lower().endswith("\\" + tail.lower()):
            return p
    return None


def _flags_block(conn, project: str) -> str:
    rows = conn.execute("SELECT file, why, flagged_at FROM mem_flags WHERE project=? OR project IS NULL "
                        "OR project='' ORDER BY flagged_at DESC", (project,)).fetchall()
    shown, stale = [], []
    for r in rows:
        path = _resolve_memfile(conn, r["file"])
        if path is not None:
            try:
                if os.stat(path).st_mtime > (r["flagged_at"] or 0):
                    stale.append(r["file"])  # 文件在标记之后改过：当作已核实，直接删
                    continue
            except FileNotFoundError:
                stale.append(r["file"])
                continue
            except OSError:
                pass
        shown.append(f"{os.path.basename(r['file'].replace(chr(92), '/'))}：{_one(r['why'])}")
    if stale:
        with _db.write_tx(conn):
            conn.executemany("DELETE FROM mem_flags WHERE file=?", [(f,) for f in stale])
    return ("## 记忆文件可能过时\n" + "\n".join(shown) + "\n") if shown else ""


def _sessions_block(conn, project: str) -> str:
    rows = conn.execute(
        "SELECT id, ts, request, completed FROM summaries WHERE project=? AND id IN "
        "(SELECT max(id) FROM summaries WHERE project=? GROUP BY session_id) "
        "ORDER BY ts DESC, id DESC LIMIT 5", (project, project)).fetchall()
    lines = []
    for r in rows:
        req = _first_sentence(r["request"], _REQUEST_MAX)
        done = _first_sentence(r["completed"], _DONE_MAX)
        if not req and not done:
            continue
        lines.append(f"S{r['id']} {_md(r['ts'])} {req}" + (f" → {done}" if done else ""))
    return ("## 最近会话\n" + "\n".join(lines) + "\n") if lines else ""


def _points_block(conn, project: str, now: float) -> str:
    rows = conn.execute(
        "SELECT id, ts, type, title FROM notes WHERE project=? AND state='active' AND ts>=? "
        "AND type!='unfinished' AND (type IN ('decision','bugfix') OR tags LIKE '%gotcha%') "
        "ORDER BY ts DESC, id DESC LIMIT ?", (project, now - _KEY_DAYS * 86400, _KEY_POINTS_MAX)).fetchall()
    lines = [f"n{r['id']} {_md(r['ts'])} {_one(r['type'])} {_one(r['title'])[:120]}" for r in rows]
    return ("## 要点\n" + "\n".join(lines) + "\n") if lines else ""


# ---------------------------------------------------------------- 装填

def _fits(text: str, cfg) -> bool:
    return (len(text) <= int(cfg.get("inject.hard_chars", 9000))
            and _tokens.estimate(text) <= int(cfg.get("inject.budget_tokens", 2000)))


def render(conn, project: str) -> str:
    """按方案书第 4 节顺序重拼注入文本（第 7 项除外），写入 kv 的 inject:<project>，返回文本。"""
    cfg = _config.load()
    now = time.time()
    blocks = [
        _card_block(conn, project, cfg),
        _unfinished_block(conn, project, cfg, now),
        _flags_block(conn, project),
        _sessions_block(conn, project),
        _points_block(conn, project, now),
    ]
    head = _head(project, now)
    accepted: list[str] = []
    for blk in blocks:
        if not blk:
            continue
        if _fits(head + "".join(accepted) + blk + _CLOSE, cfg):
            accepted.append(blk)
        # 装不下：整块跳过，继续试后面的
    text = (head + "".join(accepted) + _CLOSE) if accepted else ""
    _db.kv_set(conn, f"inject:{project}", text)
    return text


# ---------------------------------------------------------------- SessionStart 用

def _tail_block(conn, project: str, exclude_session: str | None, cfg, now: float) -> str:
    cutoff = now - int(cfg.get("inject.unfinished_days", 14)) * 86400
    sql = "SELECT id FROM sessions WHERE project=? AND last_seen>=?"
    args: list = [project, cutoff]
    if exclude_session:
        sql += " AND id!=?"
        args.append(exclude_session)
    sess = conn.execute(sql + " ORDER BY last_seen DESC LIMIT 1", args).fetchone()
    if sess is None:
        return ""
    sid = sess[0]
    cnt, first_id = conn.execute(
        "SELECT count(*), min(id) FROM events WHERE session_id=? AND batch_id IS NULL AND kind='prompt'",
        (sid,)).fetchone()
    if not cnt:
        return ""
    ts, body = conn.execute("SELECT ts, body FROM events WHERE id=?", (first_id,)).fetchone()
    files = []
    for (brief,) in conn.execute(
            "SELECT brief FROM events WHERE session_id=? AND batch_id IS NULL AND kind='tool' "
            "AND tool IN ('Edit','Write') ORDER BY id DESC LIMIT 12", (sid,)):
        name = os.path.basename((brief or "").replace("\\", "/"))
        if name and name not in files:
            files.append(name)
        if len(files) >= 3:
            break
    line = f"未提炼 {_md(ts)} {str(sid)[:8]}：{_one(body)[:_TAIL_CHARS]}"
    if cnt > 1:
        line += f"（另有 {cnt - 1} 条提示）"
    if files:
        line += "｜改过：" + "、".join(files)
    return "## 未提炼\n" + line + "\n"


def _corrupt_line(conn) -> str:
    """细则 5.3：quick_check 没过时 janitor 在 kv 的 db_corrupt 里写了说明，注入第一行就写出来。不受预算约束。"""
    try:
        msg = _db.kv_get(conn, "db_corrupt", "") or ""
    except sqlite3.Error:
        return ""
    msg = msg.split("（quick_check")[0].strip()
    return f"! {_one(msg)}\n" if msg else ""


def _promote_block(conn, project: str) -> str:
    """每周整理（consolidate.nominate）按项目存的晋升提名：一行提示，列前 3 条。"""
    try:
        raw = _db.kv_get(conn, PROMOTE_PREFIX + project, "") or ""
        items = json.loads(raw) if raw else []
    except (sqlite3.Error, ValueError):
        return ""
    if not isinstance(items, list) or not items:
        return ""
    names = []
    for it in items[:_PROMOTE_SHOW]:
        if isinstance(it, dict):
            names.append(f"n{it.get('id')} {_one(it.get('title'))[:_PROMOTE_TITLE]}")
    if not names:
        return ""
    return f"有 {len(items)} 条笔记被反复取用，可考虑写进记忆文件：{'；'.join(names)}\n"


def _related_block(conn, project: str, cfg) -> str:
    """项目地图（projmap）里本项目的相关项目：一行（只给 Codex 用）。没算过、关着、没有相关项目都不输出。"""
    if not cfg.get("projmap.enabled", True):
        return ""
    from knotcord import projmap
    items = projmap.related(conn, project)
    parts = [f"{_one(it['project'])}（{_one(it.get('why'))}）" if it.get("why") else _one(it["project"])
             for it in items]
    return ("相关项目：" + "、".join(parts) + "\n") if parts else ""


def read_for_hook(conn, project: str, *, exclude_session: str | None = None, client: str = "claude") -> str:
    """读 kv 里拼好的文本，再补：库损坏提示（第一行，不占预算）、第 7 项、相关项目行、晋升提名行（后三项受预算约束）。
    相关项目行只给 Codex（client="codex"）：Claude Code 本来就会加载项目的 CLAUDE.local.md，不重复输出。
    无内容返回空串。conn 可以是只读连接。"""
    cfg = _config.load()
    text = _db.kv_get(conn, f"inject:{project}", "") or ""
    now = time.time()
    corrupt = _corrupt_line(conn)
    optional = [b for b in (_tail_block(conn, project, exclude_session, cfg, now),
                            _related_block(conn, project, cfg) if client == "codex" else "",
                            _promote_block(conn, project)) if b]
    if not corrupt and not optional:
        return text
    base = text[:-len(_CLOSE)] if text.endswith(_CLOSE) else _head(project, now)
    if corrupt:  # 插在开头标签行之后、抬头之前：整段注入的第一行内容
        first, _, rest = base.partition("\n")
        base = first + "\n" + corrupt + rest
    cur, changed = base, bool(corrupt)
    for blk in optional:
        if _fits(cur + blk + _CLOSE, cfg):
            cur += blk
            changed = True
    return (cur + _CLOSE) if changed else text
