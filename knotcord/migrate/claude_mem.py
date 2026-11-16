"""从 claude-mem 迁移（方案书第 6 节"迁移"）。

源库只读打开；先可选地快照整库到 KNOTCORD_HOME/legacy/claude-mem.db，再把五类数据导进主库。
幂等：笔记/总结靠 (origin, legacy_id)，提示靠 events.uuid，会话靠 id；重跑条数不变。
每 1000 条提交一次，正文和索引在同一个事务里写。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from knotcord.core import db, paths
from knotcord.core.index import index_doc
from knotcord.core.redact import redact

ORIGIN = "claude-mem"
BATCH = 1000
INDEX_CAP = 20000  # 单条索引文本上限，防止个别超长提示撑大索引

_TYPE_MAP = {
    "discovery": "discovery", "change": "change", "feature": "feature", "bugfix": "bugfix",
    "decision": "decision", "refactor": "refactor",
    "发现": "discovery", "变更": "change", "决策": "decision", "功能": "feature",
    "优化": "refactor", "修复": "bugfix", "故障": "bugfix", "重构": "refactor",
}


def _sec(v) -> int:
    """epoch 统一存秒：旧库是毫秒；小于 1e11 的当作已经是秒。"""
    if not v:
        return 0
    v = int(v)
    return v // 1000 if v >= 100_000_000_000 else v


def _norm_type(raw) -> tuple[str, str | None]:
    """返回 (规范 type, 需要记进 tags 的原值)。"""
    t = (raw or "").strip()
    mapped = _TYPE_MAP.get(t.lower() if t.isascii() else t)
    if mapped:
        return mapped, (t if t != mapped else None)
    return "change", (t or None)


def _jlist(s) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return [str(s)]
    if isinstance(v, list):
        return [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in v]
    return [str(v)]


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _r(s) -> str:
    return redact(s) if s else ""


def _open_src(src_path: str) -> sqlite3.Connection:
    uri = "file:" + Path(src_path).resolve().as_posix() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _snapshot(src: sqlite3.Connection, log) -> Path:
    dst_dir = paths.home() / "legacy"
    dst_dir.mkdir(parents=True, exist_ok=True)
    final = dst_dir / "claude-mem.db"
    tmp = paths.tmp_dir() / "claude-mem.db.snapshot"
    if tmp.exists():
        tmp.unlink()
    d = sqlite3.connect(str(tmp))
    try:
        src.backup(d)
    finally:
        d.close()
    os.replace(tmp, final)
    log(f"快照完成: {final} ({final.stat().st_size // 1024 // 1024} MB)")
    return final


def _has_table(src, name) -> bool:
    return src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _chunks(cur, n=BATCH):
    while True:
        rows = cur.fetchmany(n)
        if not rows:
            return
        yield rows


def _count(conn, sql, args=()) -> int:
    return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- 各表

def _sessions(conn, src, log) -> tuple[dict, int, int]:
    """返回 (memory_session_id -> content_session_id 映射, 源条数, 目标条数)。"""
    mem2content: dict[str, str] = {}
    sess_project: dict[str, str] = {}
    ids: list[str] = []
    rows = src.execute("SELECT * FROM sdk_sessions ORDER BY id").fetchall()
    with db.write_tx(conn):
        for r in rows:
            cid = r["content_session_id"]
            if not cid:
                continue
            ids.append(cid)
            if r["memory_session_id"]:
                mem2content[r["memory_session_id"]] = cid
            sess_project[cid] = r["project"] or ""
            started = _sec(r["started_at_epoch"])
            last = _sec(r["completed_at_epoch"]) or started
            conn.execute(
                "INSERT OR IGNORE INTO sessions(id, client, project, cwd, transcript_path, read_offset, "
                "started_at, last_seen, legacy_memory_session_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, r["platform_source"] or "claude", r["project"] or "", r["cwd"], None, 0,
                 started, last, r["memory_session_id"]),
            )
    have = {x[0] for x in conn.execute("SELECT id FROM sessions")}
    tgt = sum(1 for i in set(ids) if i in have)
    return {"m2c": mem2content, "proj": sess_project}, len(rows), tgt


def _notes(conn, src, m2c, log) -> tuple[int, int]:
    done = {x[0] for x in conn.execute(
        "SELECT legacy_id FROM notes WHERE origin=? AND type!='legacy' AND legacy_id IS NOT NULL", (ORIGIN,))}
    cur = src.execute("SELECT * FROM observations ORDER BY id")
    n_src = 0
    for rows in _chunks(cur):
        n_src += len(rows)
        with db.write_tx(conn):
            for r in rows:
                if r["id"] in done:
                    continue
                typ, orig = _norm_type(r["type"])
                tags = _jlist(r["concepts"])
                if orig:
                    tags.append(f"orig:{orig}")
                files: list[str] = []
                for f in _jlist(r["files_modified"]) + _jlist(r["files_read"]):
                    if f not in files:
                        files.append(f)
                body = "\n".join(x for x in (r["subtitle"], r["narrative"]) if x)
                if not body and r["text"]:
                    body = r["text"]
                title = _r(r["title"])
                body = _r(body)
                facts = [_r(x) for x in _jlist(r["facts"])]
                ts = _sec(r["created_at_epoch"])
                msid = r["memory_session_id"]
                sid = m2c.get(msid, msid)
                cur2 = conn.execute(
                    "INSERT INTO notes(project, session_id, ts, type, title, body, facts, files, tags, state, "
                    "model, origin, legacy_id, content_hash, last_mentioned) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                    (r["project"] or "", sid, ts, typ, title, body, _dumps(facts), _dumps(files), _dumps(tags),
                     r["generated_by_model"], ORIGIN, r["id"], r["content_hash"], ts))
                text = "\n".join(x for x in (title, body, "\n".join(facts), " ".join(tags)) if x)
                index_doc(conn, "note", cur2.lastrowid, r["project"] or "", ts, text[:INDEX_CAP])
        log(f"  notes {n_src}")
    return n_src, _count(conn, "SELECT count(*) FROM notes WHERE origin=? AND type!='legacy'", (ORIGIN,))


def _summaries(conn, src, m2c, log) -> tuple[int, int]:
    done = {x[0] for x in conn.execute(
        "SELECT legacy_id FROM summaries WHERE origin=? AND legacy_id IS NOT NULL", (ORIGIN,))}
    cur = src.execute("SELECT * FROM session_summaries ORDER BY id")
    n_src = 0
    for rows in _chunks(cur):
        n_src += len(rows)
        with db.write_tx(conn):
            for r in rows:
                if r["id"] in done:
                    continue
                f = {k: _r(r[k]) for k in ("request", "investigated", "learned", "completed", "next_steps", "notes")}
                extra = {k: v for k, v in (
                    ("investigated", f["investigated"]), ("notes", f["notes"]),
                    ("files_read", r["files_read"]), ("files_edited", r["files_edited"])) if v}
                ts = _sec(r["created_at_epoch"])
                msid = r["memory_session_id"]
                sid = m2c.get(msid, msid)
                cur2 = conn.execute(
                    "INSERT INTO summaries(session_id, project, ts, request, completed, learned, next_steps, "
                    "extra, origin, legacy_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, r["project"] or "", ts, f["request"], f["completed"], f["learned"], f["next_steps"],
                     _dumps(extra) if extra else None, ORIGIN, r["id"]))
                text = "\n".join(v for v in (f["request"], f["investigated"], f["learned"], f["completed"],
                                             f["next_steps"], f["notes"]) if v)
                index_doc(conn, "summary", cur2.lastrowid, r["project"] or "", ts, text[:INDEX_CAP])
        log(f"  summaries {n_src}")
    return n_src, _count(conn, "SELECT count(*) FROM summaries WHERE origin=?", (ORIGIN,))


def _prompts(conn, src, sess, log) -> tuple[int, int]:
    done = {x[0] for x in conn.execute("SELECT uuid FROM events WHERE uuid LIKE 'legacy-prompt-%'")}
    cur = src.execute("SELECT * FROM user_prompts ORDER BY id")
    n_src = 0
    for rows in _chunks(cur):
        n_src += len(rows)
        with db.write_tx(conn):
            for r in rows:
                uuid = f"legacy-prompt-{r['id']}"
                if uuid in done:
                    continue
                body = _r(r["prompt_text"])
                sid = r["content_session_id"]
                proj = sess["proj"].get(sid, "")
                ts = _sec(r["created_at_epoch"])
                brief = " ".join(body.split())[:60]
                cur2 = conn.execute(
                    "INSERT OR IGNORE INTO events(session_id, project, ts, uuid, kind, brief, body, origin, "
                    "legacy_id) VALUES(?,?,?,?,'prompt',?,?,?,?)",
                    (sid, proj, ts, uuid, brief, body, ORIGIN, r["id"]))
                if cur2.rowcount:
                    index_doc(conn, "prompt", cur2.lastrowid, proj, ts, body[:INDEX_CAP])
        log(f"  prompts {n_src}")
    return n_src, _count(conn, "SELECT count(*) FROM events WHERE kind='prompt' AND origin=?", (ORIGIN,))


def _work_state(conn, src, log) -> tuple[int, int]:
    done = {x[0] for x in conn.execute(
        "SELECT legacy_id FROM notes WHERE origin=? AND type='legacy' AND legacy_id IS NOT NULL", (ORIGIN,))}
    cur = src.execute("SELECT * FROM work_state_entries ORDER BY id")
    n_src = 0
    for rows in _chunks(cur):
        n_src += len(rows)
        with db.write_tx(conn):
            for r in rows:
                if r["id"] in done:
                    continue
                try:
                    fields = json.loads(r["fields"]) if r["fields"] else {}
                except ValueError:
                    fields = {"raw": r["fields"]}
                if isinstance(fields, dict):
                    body = "\n".join(f"{k}: {v}" for k, v in fields.items())
                    head = fields.get("task") or fields.get("title") or ""
                else:
                    body, head = str(fields), ""
                lname = r["list_name"] or ""
                title = _r(f"{lname}: {head}" if head else lname)
                body = _r(body)
                tags = ["work_state"] + ([f"list:{lname}"] if lname else [])
                ts = _sec(r["created_at_epoch"])
                cur2 = conn.execute(
                    "INSERT INTO notes(project, ts, type, title, body, facts, files, tags, state, origin, "
                    "legacy_id, last_mentioned) VALUES(?,?,'legacy',?,?,?,?,?,'active',?,?,?)",
                    (r["project"] or "", ts, title, body, "[]", "[]", _dumps(tags), ORIGIN, r["id"], ts))
                index_doc(conn, "note", cur2.lastrowid, r["project"] or "", ts,
                          "\n".join(x for x in (title, body) if x)[:INDEX_CAP])
    return n_src, _count(conn, "SELECT count(*) FROM notes WHERE origin=? AND type='legacy'", (ORIGIN,))


# ---------------------------------------------------------------- 入口

def run(conn, src_path: str, *, snapshot: bool = True, log=print) -> dict:
    """返回 {表名: {"源条数", "目标条数", "一致"}}。源库只读，快照用 SQLite backup 接口。"""
    src = _open_src(src_path)
    try:
        if snapshot:
            _snapshot(src, log)
        src.execute("BEGIN")  # 读事务：整个迁移期间看到同一份数据（旧库可能还在写）
        if not _has_table(src, "observations"):
            raise ValueError(f"不是 claude-mem 库: {src_path}")
        result: dict[str, dict] = {}

        def rec(name: str, s: int, t: int) -> None:
            result[name] = {"源条数": s, "目标条数": t, "一致": s == t}
            log(f"{name}: 源 {s} / 目标 {t}" + ("" if s == t else "  <-- 不一致"))

        sess, s, t = _sessions(conn, src, log)
        rec("sdk_sessions", s, t)
        rec("observations", *_notes(conn, src, sess["m2c"], log))
        rec("session_summaries", *_summaries(conn, src, sess["m2c"], log))
        rec("user_prompts", *_prompts(conn, src, sess, log))
        if _has_table(src, "work_state_entries"):
            rec("work_state_entries", *_work_state(conn, src, log))
        return result
    finally:
        src.close()
