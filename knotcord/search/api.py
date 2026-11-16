"""三步检索：search -> timeline -> get。返回给 agent 看的纯文本，每条一行。

编号前缀：n 笔记、u 没做完的笔记、s 总结、p 提示、m 内置记忆文件、e events 任意一行。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time

from knotcord.core import config, db
from knotcord.core.index import fts_search

_ID_RE = re.compile(r"^([nuspme])(\d+)$")
_HAN_RE = re.compile("[㐀-䶿一-鿿豈-﫿]")
_TITLE_CAP = 120
_PROMPT_CAP = 60


# ---------------------------------------------------------------- 小工具

def _one(s, cap=_TITLE_CAP) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= cap else s[:cap] + "…"


def _date(ts) -> str:
    return time.strftime("%m-%d", time.localtime(ts)) if ts else "--"


def _dt(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "--"


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _chunked(ids: list, n: int = 500):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _prefix(src: str, note_type: str | None = None) -> str:
    if src == "note":
        return "u" if note_type == "unfinished" else "n"
    return {"summary": "s", "prompt": "p", "memfile": "m"}[src]


def _parse_id(s: str):
    m = _ID_RE.match(str(s).strip().lower())
    return (m.group(1), int(m.group(2))) if m else None


def _vector_candidates(conn, query: str, *, project, limit, include_superseded,
                       cfg=None) -> list[tuple[int, float]]:
    """向量一路，返回 (doc_id, 余弦相似度)，从高到低。模型不在、依赖缺失、没有向量或任何出错都返回空。"""
    from knotcord.search import vectors  # 延迟：numpy、onnxruntime 只在这里才会被带进来
    return vectors.candidates(conn, query, project=project, limit=limit, include_superseded=include_superseded,
                              cfg=cfg)


# ---------------------------------------------------------------- search

def _like_hits(conn, q, project, include_superseded) -> list[tuple]:
    """单个汉字：fts 没有结果，对正文做 LIKE 兜底。返回 (src, src_id, project, ts)。"""
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    pw = " AND project=?" if project is not None else ""
    pa = [project] if project is not None else []
    out: list[tuple] = []
    sqls = [
        ("note", "SELECT id, project, ts FROM notes WHERE (title LIKE ?1 ESCAPE '\\' OR body LIKE ?1 ESCAPE '\\')"
                 + ("" if include_superseded else " AND state='active'") + pw),
        ("summary", "SELECT id, project, ts FROM summaries WHERE (request LIKE ?1 ESCAPE '\\' "
                    "OR completed LIKE ?1 ESCAPE '\\')" + pw),
        ("prompt", "SELECT id, project, ts FROM events WHERE kind='prompt' AND body LIKE ?1 ESCAPE '\\'" + pw),
        ("memfile", "SELECT id, project, mtime FROM memfiles WHERE (name LIKE ?1 ESCAPE '\\' "
                    "OR description LIKE ?1 ESCAPE '\\')" + pw),
    ]
    for src, sql in sqls:
        # 参数编号：?1 是模式；项目用匿名占位会和 ?1 混用，这里统一改成 ?2
        sql = sql.replace("project=?", "project=?2")
        rows = conn.execute(sql + " ORDER BY 3 DESC LIMIT 50", [pat] + pa).fetchall()
        out.extend((src, r[0], r[1], r[2]) for r in rows)
    return out


def _details(conn, hits: list[tuple]) -> dict:
    """按 (src, src_id) 批量取显示用的信息。"""
    by: dict[str, list[int]] = {}
    for h in hits:
        by.setdefault(h[0], []).append(h[1])
    out: dict[tuple, dict] = {}
    for src, ids in by.items():
        for part in _chunked(sorted(set(ids))):
            if src == "note":
                sql = "SELECT id, type, title, state, superseded_by FROM notes WHERE id IN (%s)"
            elif src == "summary":
                sql = "SELECT id, request FROM summaries WHERE id IN (%s)"
            elif src == "prompt":
                sql = "SELECT id, body FROM events WHERE id IN (%s)"
            else:
                sql = "SELECT id, name, description FROM memfiles WHERE id IN (%s)"
            for r in conn.execute(sql % _in(part), part):
                out[(src, r["id"])] = dict(r)
    return out


def _line(src, sid, project, ts, d) -> str:
    proj = project or "-"
    if src == "note":
        p = _prefix("note", d["type"])
        s = f"{p}{sid} {_date(ts)} {proj} {d['type'] or 'note'} {_one(d['title'])}"
        if d["state"] == "superseded" and d["superseded_by"]:
            s += f" [被 n{d['superseded_by']} 取代]"
        return s
    if src == "summary":
        return f"s{sid} {_date(ts)} {proj} summary {_one(d['request'])}"
    if src == "prompt":
        return f"p{sid} {_date(ts)} {proj} prompt {_one(d['body'], _PROMPT_CAP)}"
    return f"m{sid} {proj} memfile {_one(d['name'], 60)}：{_one(d['description'])}"


def search(conn, query: str, *, project: str | None = None, current_project: str | None = None,
           since: int | None = None, until: int | None = None, limit: int = 10,
           include_superseded: bool = False) -> str:
    query = (query or "").strip()
    if not query or limit <= 0:
        return "无结果"
    cfg = config.load()
    cand = int(cfg.get("search.candidates", 50))
    k = float(cfg.get("search.rrf_k", 60))
    boost = float(cfg.get("search.project_boost", 1.2))
    over = 5 if (since is not None or until is not None) else 1  # 有时间过滤时多取，避免被过滤光

    scores: dict[int, float] = {}
    meta: dict[int, tuple] = {}  # doc_id -> (src, src_id, project, ts)
    kw = fts_search(conn, query, project=project, limit=cand * over, active_only=not include_superseded)
    for rank, r in enumerate(kw, 1):
        scores[r["doc_id"]] = scores.get(r["doc_id"], 0.0) + 1.0 / (k + rank)
        meta[r["doc_id"]] = (r["src"], r["src_id"], r["project"], r["ts"])

    if cfg.get("search.vector", True):
        vec = _vector_candidates(conn, query, project=project, limit=cand * over,
                                 include_superseded=include_superseded, cfg=cfg)
        for rank, (doc_id, _s) in enumerate(vec, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    use: dict[int, int] = {}
    ids = list(scores)
    for part in _chunked(ids):
        for r in conn.execute(
                f"SELECT id, src, src_id, project, ts, active, use_count FROM docs WHERE id IN ({_in(part)})", part):
            use[r["id"]] = r["use_count"] or 0
            if r["id"] not in meta:  # 只有向量路命中的文档
                if (project is not None and r["project"] != project) or (not r["active"] and not include_superseded):
                    scores.pop(r["id"], None)
                    continue
                meta[r["id"]] = (r["src"], r["src_id"], r["project"], r["ts"])

    stale: dict[int, float] = {}  # 笔记 id -> 久未取用的降权乘数（只含被降权的）
    if cfg.get("consolidate.enabled", True):
        note_ids = [meta[d][1] for d in scores if d in meta and meta[d][0] == "note"]
        if note_ids:
            from knotcord import consolidate
            stale = consolidate.note_factors(conn, note_ids, cfg=cfg)

    rel: set[str] = set()   # 项目地图里本项目声明的相关项目：略微加分，加得比本项目少
    same: set[str] = set()  # 地图里本项目的子目录项目（或本项目是它的子目录）：视同本项目
    if current_project and cfg.get("projmap.enabled", True):
        from knotcord import projmap
        for x in projmap.related(conn, current_project):  # 只看本项目自己的出边
            (same if x.get("kind") == "subdir" else rel).add(x["project"])
    rel_boost = min(float(cfg.get("search.related_boost", 1.1)), boost)

    items: list[tuple] = []  # (score, ts, src, src_id, project, ts)
    for doc_id, sc in scores.items():
        if doc_id not in meta:
            continue
        src, sid, proj, ts = meta[doc_id]
        if current_project and (proj == current_project or proj in same):
            sc *= boost
        elif proj in rel:
            sc *= rel_boost
        if use.get(doc_id, 0) > 0:
            sc *= 1.05
        if src == "note":
            sc *= stale.get(sid, 1.0)
        items.append((sc, ts or 0, src, sid, proj, ts))

    if not items and len(query) == 1 and _HAN_RE.match(query):
        for src, sid, proj, ts in _like_hits(conn, query, project, include_superseded):
            items.append((0.0, ts or 0, src, sid, proj, ts))

    if since is not None:
        items = [x for x in items if (x[1] or 0) >= since]
    if until is not None:
        items = [x for x in items if (x[1] or 0) <= until]
    items.sort(key=lambda x: (-x[0], -x[1]))

    det = _details(conn, [(x[2], x[3]) for x in items])
    lines: list[str] = []
    for sc, _t, src, sid, proj, ts in items:
        d = det.get((src, sid))
        if d is None:
            continue  # 文档在、正文行没了（孤儿索引）
        if src == "note" and d["state"] != "active" and not include_superseded:
            continue
        lines.append(_line(src, sid, proj, ts, d))
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "无结果"


# ---------------------------------------------------------------- timeline

_RANK = {"prompt": 0, "tool": 1, "note": 2, "summary": 3}


def _anchor(conn, pfx: str, num: int):
    """返回 (session_id, ts, 项目, 条目) 或 None；条目是 timeline 里的 (key, 文本)。"""
    if pfx in ("n", "u"):
        r = conn.execute("SELECT id, session_id, ts, type, title FROM notes WHERE id=?", (num,)).fetchone()
        if r:
            return r["session_id"], r["ts"] or 0, ("note", r["id"], r["type"], r["title"])
    elif pfx == "s":
        r = conn.execute("SELECT id, session_id, ts, request FROM summaries WHERE id=?", (num,)).fetchone()
        if r:
            return r["session_id"], r["ts"] or 0, ("summary", r["id"], None, r["request"])
    else:  # p / e
        r = conn.execute("SELECT id, session_id, ts, kind, tool, brief, body FROM events WHERE id=?", (num,)).fetchone()
        if r:
            return r["session_id"], r["ts"] or 0, ("event", r["id"], r["kind"], r)
    return None


def _tl_item(kind: str, row) -> tuple:
    """-> (排序键, 行文本不含 '>' 前缀)。"""
    if kind == "event":
        r = row
        ts = r["ts"] or 0
        if r["kind"] == "prompt":
            return (ts, 0, r["id"]), f"p{r['id']} {_dt(ts)} prompt {_one(r['brief'] or r['body'], _PROMPT_CAP)}"
        if r["kind"] == "tool":
            return (ts, 1, r["id"]), f"e{r['id']} {_dt(ts)} tool {r['tool'] or ''}: {_one(r['brief'])}"
        return (ts, 1, r["id"]), f"e{r['id']} {_dt(ts)} {r['kind']} {_one(r['brief'] or r['body'])}"
    if kind == "note":
        p = _prefix("note", row["type"])
        return (row["ts"] or 0, 2, row["id"]), f"{p}{row['id']} {_dt(row['ts'])} {row['type'] or 'note'} {_one(row['title'])}"
    return (row["ts"] or 0, 3, row["id"]), f"s{row['id']} {_dt(row['ts'])} summary {_one(row['request'])}"


def timeline(conn, id: str, *, before: int = 3, after: int = 3) -> str:
    parsed = _parse_id(id)
    if parsed is None:
        return f"无效编号: {id}"
    pfx, num = parsed
    if pfx == "m":
        return f"m{num} 是内置记忆文件，没有时间线；用 get 看路径后直接 Read"
    a = _anchor(conn, pfx, num)
    if a is None:
        return f"找不到 {pfx}{num}"
    sid, ats, (akind, aid, _x, arow) = a
    before, after = max(0, int(before)), max(0, int(after))
    a_ev = akind == "event"
    anchor_key = None
    items: dict[tuple, tuple] = {}

    def add(kind, row):
        key, text = _tl_item(kind, row)
        items[(kind, row["id"])] = (key, text)

    if akind == "event":
        add("event", arow)
    elif akind == "note":
        add("note", conn.execute("SELECT id, ts, type, title FROM notes WHERE id=?", (aid,)).fetchone())
    else:
        add("summary", conn.execute("SELECT id, ts, request FROM summaries WHERE id=?", (aid,)).fetchone())
    anchor_key = (akind, aid)

    if sid:
        n = max(before, after) + 5
        ev_cols = "id, ts, kind, tool, brief, body"
        ev_where = "session_id=? AND kind IN ('prompt','tool')"
        # events：同秒的靠 id 切开；notes、summaries：只靠 ts
        for r in conn.execute(f"SELECT {ev_cols} FROM events WHERE {ev_where} AND (ts<? OR (ts=? AND id<=?)) "
                              "ORDER BY ts DESC, id DESC LIMIT ?", (sid, ats, ats, aid if a_ev else 1 << 60, n)):
            add("event", r)
        for r in conn.execute(f"SELECT {ev_cols} FROM events WHERE {ev_where} AND (ts>? OR (ts=? AND id>=?)) "
                              "ORDER BY ts, id LIMIT ?", (sid, ats, ats, aid if a_ev else 0, n)):
            add("event", r)
        for r in conn.execute("SELECT id, ts, type, title FROM notes WHERE session_id=? AND ts<=? "
                              "ORDER BY ts DESC, id DESC LIMIT ?", (sid, ats, n)):
            add("note", r)
        for r in conn.execute("SELECT id, ts, type, title FROM notes WHERE session_id=? AND ts>=? "
                              "ORDER BY ts, id LIMIT ?", (sid, ats, n)):
            add("note", r)

    ordered = sorted(items.items(), key=lambda kv: kv[1][0])
    pos = next((i for i, (k, _v) in enumerate(ordered) if k == anchor_key), 0)
    window = ordered[max(0, pos - before): pos + after + 1]
    out = [("> " if k == anchor_key else "") + v[1] for k, v in window]
    if not sid:
        out.append("(这条没有会话信息，没有前后文)")
    return "\n".join(out)


# ---------------------------------------------------------------- get

def _jl(s) -> list[str]:
    """facts/files/tags 存的是 JSON 数组串；容错成普通文本。"""
    import json
    if not s:
        return []
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return [str(s)]
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _source_block(conn, note) -> list[str]:
    out: list[str] = []
    evs = [int(x) for x in re.findall(r"\d+", note["source_events"] or "")][:30]
    lines: list[int] = []
    if evs:
        out.append("source_events:")
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, kind, tool, brief, body, src_line FROM events WHERE id IN ({_in(evs)})", evs)}
        for i in evs:
            r = rows.get(i)
            if r is None:
                out.append(f"  e{i} (已不在库中)")
                continue
            txt = _one(r["brief"] or r["body"], 160)
            tool = f" {r['tool']}" if r["tool"] else ""
            out.append(f"  e{i} {r['kind']}{tool} {txt}")
            if r["src_line"]:
                lines.append(r["src_line"])
    srow = conn.execute("SELECT transcript_path FROM sessions WHERE id=?", (note["session_id"],)).fetchone() \
        if note["session_id"] else None
    path = srow["transcript_path"] if srow else None
    if not path:
        out.append("transcript: 无记录（旧库迁移的数据没有原文位置）")
    elif os.path.exists(path):
        ln = (" 行 " + ",".join(str(x) for x in sorted(set(lines)))) if lines else ""
        out.append(f"transcript: {path}{ln}")
    else:
        out.append(f"transcript: {path} 原文已不在（以上为库内精简版）")
    return out


def _get_one(conn, pfx: str, num: int, with_source: bool):
    """返回 (文本, 触碰的 (src, src_id)) 或 (None, None)。"""
    if pfx in ("n", "u"):
        r = conn.execute("SELECT * FROM notes WHERE id=?", (num,)).fetchone()
        if r is None:
            return None, None
        p = _prefix("note", r["type"])
        out = [f"{p}{r['id']} {r['type']} [{r['project'] or '-'}] {_dt(r['ts'])}",
               f"title: {r['title'] or ''}"]
        if r["body"]:
            out.append("body:\n" + r["body"])
        for label, key in (("facts", "facts"), ("files", "files"), ("tags", "tags")):
            vals = _jl(r[key])
            if vals:
                out.append(f"{label}: " + ("\n" + "\n".join("- " + v for v in vals) if label == "facts"
                                           else ", ".join(vals)))
        st = r["state"] or "active"
        if st == "superseded":
            when = f" {_dt(r['superseded_at'])}" if r["superseded_at"] else ""
            st = f"superseded，被 n{r['superseded_by']} 取代{when}" if r["superseded_by"] else "superseded"
        out.append(f"state: {st}")
        if with_source:
            out.extend(_source_block(conn, r))
        return "\n".join(out), ("note", r["id"])
    if pfx == "s":
        r = conn.execute("SELECT * FROM summaries WHERE id=?", (num,)).fetchone()
        if r is None:
            return None, None
        out = [f"s{r['id']} summary [{r['project'] or '-'}] {_dt(r['ts'])}"]
        for k in ("request", "completed", "learned", "next_steps"):
            out.append(f"{k}: {r[k] or ''}")
        return "\n".join(out), ("summary", r["id"])
    if pfx in ("p", "e"):
        r = conn.execute("SELECT * FROM events WHERE id=?", (num,)).fetchone()
        if r is None:
            return None, None
        pre = "p" if r["kind"] == "prompt" else "e"
        head = f"{pre}{r['id']} {r['kind']}" + (f" {r['tool']}" if r["tool"] else "") + \
               f" [{r['project'] or '-'}] {_dt(r['ts'])}"
        out = [head]
        if r["kind"] != "prompt" and r["brief"]:
            out.append(f"brief: {r['brief']}")
        out.append(r["body"] or "")
        src = "prompt" if r["kind"] == "prompt" else None
        return "\n".join(out), (("prompt", r["id"]) if src else None)
    r = conn.execute("SELECT * FROM memfiles WHERE id=?", (num,)).fetchone()
    if r is None:
        return None, None
    out = [f"m{r['id']} memfile [{r['project'] or '-'}]", f"path: {r['path']}", f"name: {r['name'] or ''}",
           f"description: {r['description'] or ''}", "正文请直接 Read 这个文件"]
    return "\n".join(out), ("memfile", r["id"])


def _touch(conn, touched: list[tuple]) -> None:
    """取用计数加 1；写失败（只读连接、被锁）就跳过。"""
    if not touched:
        return
    now = int(time.time())
    try:
        with db.write_tx(conn):
            for src, sid in touched:
                if src == "note":
                    conn.execute("UPDATE notes SET use_count=COALESCE(use_count,0)+1, last_used=? WHERE id=?",
                                 (now, sid))
                conn.execute("UPDATE docs SET use_count=COALESCE(use_count,0)+1 WHERE src=? AND src_id=?",
                             (src, sid))
    except sqlite3.Error:
        pass


def get(conn, ids: list[str], *, with_source: bool = False) -> str:
    if isinstance(ids, str):
        ids = [ids]
    flat = [t for x in ids for t in re.split(r"[,\s]+", str(x).strip()) if t]
    blocks: list[str] = []
    touched: list[tuple] = []
    for raw in flat:
        parsed = _parse_id(raw)
        if parsed is None:
            blocks.append(f"{raw}: 无效编号")
            continue
        text, touch = _get_one(conn, parsed[0], parsed[1], with_source)
        if text is None:
            blocks.append(f"{raw}: 找不到")
            continue
        blocks.append(text)
        if touch:
            touched.append(touch)
    _touch(conn, touched)
    return "\n\n".join(blocks) if blocks else "无结果"
