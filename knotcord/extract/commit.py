"""一批提炼结果的落库：一个 BEGIN IMMEDIATE 事务，第一句确认租约还是自己的（细则 2.3）。

事务里只做数据库写入，不调模型、不读大文件。进程死在事务途中 SQLite 自动回滚，
所以库里要么是完整的一批，要么什么都没变（events 没有 batch_id，下次重新组批）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import sqlite3
import unicodedata

from knotcord.core import db, index, log

from .backlog import sec
from .prompt import PromptCtx
from .validate import Validated

_log = log.get_logger("extract")
_MENTION_OVERLAP = 0.5   # 没做完的条目和本批原文的词重合度达到这个比例，就算"本批提到了"
_CARD_KEEP = 5           # 每个项目留最近几份现状卡


def content_hash(project: str, title: str) -> str:
    """项目 + 归一化标题（NFKC、小写、去标点空白）的哈希，用于快速去重。"""
    norm = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title or "").lower())
    return hashlib.sha1(f"{project}\x00{norm}".encode("utf-8")).hexdigest()


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _chunks(ids: list[int], n: int = 500):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def stamp_events(conn: sqlite3.Connection, batch_id: int, event_ids: list[int]) -> None:
    """给事件打上批号（调用方在事务里）。dead 批也走这个：这些事件之后走零模型兜底，不再提炼。"""
    for ch in _chunks(event_ids):
        conn.execute(f"UPDATE events SET batch_id=? WHERE batch_id IS NULL AND id IN ({','.join('?' * len(ch))})",
                     (batch_id, *ch))


# ---------------------------------------------------------------- 笔记和过时标记

def _newer(new_ts: int, new_id: int, new_session: str, old) -> bool:
    """第三道关（时间规则）：新笔记时间晚于旧笔记才取代。同一会话里时间戳相同（秒级精度撞车），
    后写入的算新，否则一批里同 slot 的两条笔记会互相取代不了。"""
    old_ts = sec(old["ts"])
    if new_ts > old_ts:
        return True
    return new_ts == old_ts and old["session_id"] == new_session and old["id"] < new_id


def _supersede(conn, old_id: int, new_id: int, now: int) -> None:
    cur = conn.execute("UPDATE notes SET state='superseded', superseded_by=?, superseded_at=? "
                       "WHERE id=? AND state='active'", (new_id, now, old_id))
    if cur.rowcount:
        index.set_doc_active(conn, "note", old_id, False)


def _insert_note(conn, project: str, session_id: str, n: dict, ts: int, now: int, ctx: PromptCtx,
                 model: str) -> int | None:
    h = content_hash(project, n["title"])
    if conn.execute("SELECT 1 FROM notes WHERE project=? AND content_hash=? LIMIT 1", (project, h)).fetchone():
        return None  # 同项目已有相同标题的笔记
    slot = n["slot"] or None
    src_ids = [ctx.event_ids[i - 1] for i in n["source"]]
    cur = conn.execute(
        "INSERT INTO notes(project, session_id, ts, type, title, body, facts, files, tags, slot, state, "
        "source_events, model, origin, content_hash, last_mentioned) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?,?, 'knotcord', ?,?)",
        (project, session_id, ts, n["type"], n["title"], n["body"], _j(n["facts"]), _j(n["files"]),
         _j(n["tags"]), slot, _j(src_ids), model, h, ts))
    nid = cur.lastrowid
    text = " ".join([n["title"], n["body"], *n["facts"], *n["files"], *n["tags"]])
    index.index_doc(conn, "note", nid, project, ts, text)

    olds: dict[int, sqlite3.Row] = {}
    if slot:  # 第一道关：同项目同 slot 的旧有效笔记
        for r in conn.execute("SELECT id, ts, session_id FROM notes WHERE project=? AND slot=? "
                              "AND state='active' AND type<>'unfinished' AND id<>?", (project, slot, nid)):
            olds[r["id"]] = r
    for num in n["supersedes"]:  # 第二道关：模型给的编号（已在校验里限定在喂进去的范围内）
        old_id = ctx.notes[num - 1] if 1 <= num <= len(ctx.notes) else None
        if old_id and old_id != nid and old_id not in olds:
            r = conn.execute("SELECT id, ts, session_id FROM notes WHERE id=? AND project=? AND state='active'",
                             (old_id, project)).fetchone()
            if r:
                olds[old_id] = r
    for old in olds.values():
        if _newer(ts, nid, session_id, old):
            _supersede(conn, old["id"], nid, now)
    return nid


# ---------------------------------------------------------------- 没做完的事

def _mentioned(item_text: str, batch_tokens: set[str]) -> bool:
    toks = list(dict.fromkeys(index.segment(item_text).split()))[:30]
    if not toks:
        return False
    hit = sum(1 for t in toks if t in batch_tokens)
    return hit >= max(1, math.ceil(_MENTION_OVERLAP * len(toks)))


def _unfinished(conn, project: str, session_id: str, v: Validated, ctx: PromptCtx, ts: int) -> dict:
    stats = {"unfinished_new": 0, "resolved": 0, "mentioned": 0}
    resolved_ids: set[int] = set()
    for num in v.unfinished_resolved:
        uid = ctx.unfinished[num - 1]["id"]
        cur = conn.execute("UPDATE notes SET state='resolved' WHERE id=? AND type='unfinished' AND state='active'",
                           (uid,))
        if cur.rowcount:
            resolved_ids.add(uid)
            stats["resolved"] += 1
    # 模型没法直接说"提到了"：喂进去的、没被 resolved 的条目，词重合度够高就刷新 last_mentioned
    if ctx.unfinished:
        batch_tokens = set(index.segment(ctx.batch_text).split())
        for u in ctx.unfinished:
            if u["id"] not in resolved_ids and _mentioned(u["text"], batch_tokens):
                conn.execute("UPDATE notes SET last_mentioned=? WHERE id=? AND state='active'", (ts, u["id"]))
                stats["mentioned"] += 1
    for text in v.unfinished_new:
        h = content_hash(project, text)
        row = conn.execute("SELECT id FROM notes WHERE project=? AND content_hash=? AND type='unfinished' "
                           "AND state='active' LIMIT 1", (project, h)).fetchone()
        if row:  # 已有同样的条目：只刷新提及时间
            conn.execute("UPDATE notes SET last_mentioned=? WHERE id=?", (ts, row["id"]))
            continue
        title = text[:60]
        cur = conn.execute(
            "INSERT INTO notes(project, session_id, ts, type, title, body, facts, files, tags, slot, state, "
            "source_events, model, origin, content_hash, last_mentioned) "
            "VALUES(?,?,?, 'unfinished', ?,?, '[]','[]','[]', NULL, 'active', '[]', NULL, 'knotcord', ?,?)",
            (project, session_id, ts, title, text if len(text) > len(title) else "", h, ts))
        index.index_doc(conn, "note", cur.lastrowid, project, ts, text)
        stats["unfinished_new"] += 1
    return stats


# ---------------------------------------------------------------- 总结、现状卡、记忆文件提示

def _summary(conn, project: str, session_id: str, v: Validated, ts: int) -> None:
    s = v.summary
    if not s:
        return
    row = conn.execute("SELECT id FROM summaries WHERE session_id=? AND origin='knotcord' ORDER BY id DESC LIMIT 1",
                       (session_id,)).fetchone()
    if row:
        sid = row["id"]
        conn.execute("UPDATE summaries SET project=?, ts=?, request=?, completed=?, learned=?, next_steps=? "
                     "WHERE id=?", (project, ts, s["request"], s["completed"], s["learned"], s["next_steps"], sid))
    else:
        sid = conn.execute("INSERT INTO summaries(session_id, project, ts, request, completed, learned, next_steps, "
                           "origin) VALUES(?,?,?,?,?,?,?, 'knotcord')",
                           (session_id, project, ts, s["request"], s["completed"], s["learned"],
                            s["next_steps"])).lastrowid
    index.index_doc(conn, "summary", sid, project, ts,
                    " ".join([s["request"], s["completed"], s["learned"], s["next_steps"]]))


def _state_card(conn, project: str, batch_id: int, v: Validated, now: int) -> bool:
    if not v.card:
        return False
    new = json.dumps(v.card, ensure_ascii=False, sort_keys=True)
    last = conn.execute("SELECT card FROM state_cards WHERE project=? ORDER BY ts DESC, id DESC LIMIT 1",
                        (project,)).fetchone()
    if last:
        try:
            if json.dumps(json.loads(last["card"]), ensure_ascii=False, sort_keys=True) == new:
                return False  # 现状卡没变，不插新行
        except ValueError:
            pass
    conn.execute("INSERT INTO state_cards(project, ts, batch_id, card) VALUES(?,?,?,?)",
                 (project, now, batch_id, _j(v.card)))
    conn.execute("DELETE FROM state_cards WHERE project=? AND id NOT IN "
                 "(SELECT id FROM state_cards WHERE project=? ORDER BY ts DESC, id DESC LIMIT ?)",
                 (project, project, _CARD_KEEP))
    return True


def _conflicts(conn, project: str, v: Validated, ctx: PromptCtx, ts: int, now: int) -> int:
    """file 必须是喂进去的文件之一，否则丢弃。mem_flags.file 存 memfiles.path（能直接对上 memfiles 取 mtime）。
    文件的修改时间晚于本批最后一条事件，说明在这批之后已经被改过了（多半就是改好了），不再标。"""
    n = 0
    for c in v.conflicts:
        want = c["file"].strip().lower()
        for m in ctx.memfiles:
            if want in {m["label"].lower(), (m["name"] or "").lower(), (m["path"] or "").lower()}:
                if m["mtime"] and m["mtime"] > ts:
                    break
                conn.execute(
                    "INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(file) DO UPDATE SET project=excluded.project, why=excluded.why, "
                    "flagged_at=excluded.flagged_at", (m["path"], project, c["why"], now))
                n += 1
                break
    return n


# ---------------------------------------------------------------- 入口

def commit_batch(conn: sqlite3.Connection, lease, *, batch_id: int, session_id: str, project: str,
                 v: Validated, ctx: PromptCtx, provider: str, model: str, tokens_in: int, tokens_out: int,
                 cost_cny: float, now: float) -> dict:
    now_i = int(now)
    ts_batch = max(ctx.event_ts) if ctx.event_ts else now_i
    stats = {"notes": 0, "superseded": 0, "unfinished_new": 0, "resolved": 0, "mentioned": 0,
             "card": False, "conflicts": 0}
    model_name = model or provider
    with db.write_tx(conn):
        lease.assert_mine_in_tx()  # 第一句：不是自己的租约就整批放弃，不写任何东西
        if not v.skip:
            for n in v.notes:
                ts = max((ctx.event_ts[i - 1] for i in n["source"]), default=ts_batch)
                if _insert_note(conn, project, session_id, n, ts, now_i, ctx, model_name) is not None:
                    stats["notes"] += 1
            u = _unfinished(conn, project, session_id, v, ctx, ts_batch)
            stats.update(u)
            _summary(conn, project, session_id, v, ts_batch)
            stats["card"] = _state_card(conn, project, batch_id, v, now_i)
            stats["conflicts"] = _conflicts(conn, project, v, ctx, ts_batch, now_i)
        stamp_events(conn, batch_id, ctx.event_ids)
        conn.execute("UPDATE batches SET status='done', provider=?, tokens_in=?, tokens_out=?, cost_cny=?, "
                     "error=NULL, next_try=NULL WHERE id=?",
                     (provider, int(tokens_in), int(tokens_out), float(cost_cny), batch_id))
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    except sqlite3.Error:
        pass
    _render_inject(conn, project)
    return stats


def _render_inject(conn, project: str) -> None:
    """提交之后重拼注入文本。inject 不存在或抛异常只记日志，不影响已提交的批。"""
    try:
        mod = importlib.import_module("knotcord.inject")
        mod.render(conn, project)
    except Exception as e:
        _log.warning("inject.render 失败 (project=%s): %s: %s", project, type(e).__name__, str(e)[:200])
