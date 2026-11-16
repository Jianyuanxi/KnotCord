"""每周整理（第二轮，需求"可以有"第 2 条）。由 janitor 的每周项调用，只在持有租约的进程里跑。

三件事，都不调模型、都不删数据：
1. dedupe：去重/合并相似笔记。只处理 active、非 unfinished 的笔记，默认只动 origin='knotcord' 的
   （迁移来的旧数据用 consolidate.include_legacy 打开）。三条规则依次跑，每条笔记最多被标一次：
   - slot：同项目、同 slot，保留最新一条；
   - hash：同项目、同类型、content_hash 相同，保留最新一条；
   - title：同项目、同类型、标题归一化后高度相似（difflib 比值 >= title_similarity，数字串必须完全一致，
     且正文也相似），保留最新一条。只比排序后相邻的几条（正序、倒序各一遍），O(n log n)。
   被取代的标成 superseded、写 superseded_by/superseded_at，同时把 docs 标为非 active（搜索默认看不到，
   include_superseded 能看到）；它们的取用次数并到保留的那条上。
2. 降权：长期没被取用也没被提及的笔记在排序里降权。这里只提供打分函数 demotion_factor / note_factors，
   由检索层在算分时调用（接入点见函数文档），本模块不改检索。
3. 晋升提名：use_count 达到阈值的笔记列成"建议写进内置记忆文件"清单，按项目存 kv（promote:<项目>），
   开局注入读它并用一行提示。KnotCord 自己不写记忆文件。同一条提名后 promote_cooldown_days 天内不再提名。
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
import unicodedata

from knotcord import inject as _inject
from knotcord.core import config as _config
from knotcord.core import db, index, log

_log = log.get_logger("consolidate")

KEY_LAST = "consolidate:last"
KEY_NOMINATED = "consolidate:nominated"
PROMOTE_PREFIX = _inject.PROMOTE_PREFIX
_WINDOW = 6            # title 规则：排序后每条只和后面这么多条比
_BODY_CMP_CHARS = 600  # 比正文只看前这么多字
_MARKED_KEEP = 500     # consolidate:last 里最多记多少个被标记的编号（用于事后核对/撤销）
_DAY = 86400


# ---------------------------------------------------------------- 小工具

def _sec(ts) -> float:
    """库里 ts 大多是秒，迁移来的个别是毫秒。"""
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e11 else v


def _norm(text) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


_DIGITS = re.compile(r"\d+")


def _newest_key(r) -> tuple:
    return (_sec(r["ts"]), r["id"])


# ---------------------------------------------------------------- 1. 去重合并

def _load(conn, include_legacy: bool) -> list:
    sql = ("SELECT id, project, type, title, slot, content_hash, ts, origin, use_count, last_used FROM notes "
           "WHERE state='active' AND type<>'unfinished'")
    if not include_legacy:
        sql += " AND coalesce(origin, 'knotcord')='knotcord'"
    return conn.execute(sql).fetchall()


def _groups_by_key(rows, keyfn, rule: str) -> list[tuple]:
    buckets: dict = {}
    for r in rows:
        k = keyfn(r)
        if k is not None:
            buckets.setdefault(k, []).append(r)
    out = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=_newest_key, reverse=True)
        out.append((rule, members[0], members[1:]))
    return out


class _Bodies:
    """按需取正文（只在标题已经很像的候选对上才取）。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.cache: dict[int, str] = {}

    def get(self, nid: int) -> str:
        if nid not in self.cache:
            row = self.conn.execute("SELECT body FROM notes WHERE id=?", (nid,)).fetchone()
            self.cache[nid] = _norm(row[0])[:_BODY_CMP_CHARS] if row else ""
        return self.cache[nid]


def _title_pairs(conn, rows, cfg) -> dict[int, set[int]]:
    """标题相似的候选对 -> 邻接表（双向）。"""
    thr = float(cfg.get("consolidate.title_similarity", 0.9))
    body_thr = float(cfg.get("consolidate.body_similarity", 0.8))
    min_chars = int(cfg.get("consolidate.title_min_chars", 8))
    bodies = _Bodies(conn)
    buckets: dict = {}
    for r in rows:
        n = _norm(r["title"])
        if len(n) >= min_chars:
            buckets.setdefault((r["project"], r["type"]), []).append((n, r))
    adj: dict[int, set[int]] = {}
    seen_pairs: set[tuple[int, int]] = set()

    def consider(a, b) -> None:
        (na, ra), (nb, rb) = a, b
        key = (min(ra["id"], rb["id"]), max(ra["id"], rb["id"]))
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        if abs(len(na) - len(nb)) > max(len(na), len(nb)) * (1 - thr) + 1:
            return
        if _DIGITS.findall(na) != _DIGITS.findall(nb):  # 版本号、端口、IP 不同就是不同的事实
            return
        sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
        if sm.real_quick_ratio() < thr or sm.quick_ratio() < thr or sm.ratio() < thr:
            return
        ba, bb = bodies.get(ra["id"]), bodies.get(rb["id"])
        if (ba or bb) and difflib.SequenceMatcher(None, ba, bb, autojunk=False).ratio() < body_thr:
            return
        adj.setdefault(ra["id"], set()).add(rb["id"])
        adj.setdefault(rb["id"], set()).add(ra["id"])

    for items in buckets.values():
        if len(items) < 2:
            continue
        for order in (lambda x: x[0], lambda x: x[0][::-1]):
            s = sorted(items, key=order)
            for i, a in enumerate(s):
                for b in s[i + 1:i + 1 + _WINDOW]:
                    consider(a, b)
    return adj


def _star_clusters(rows, adj: dict[int, set[int]]) -> list[tuple]:
    """从新到旧挑簇首，只收和簇首直接相似的、且比它旧的成员（不传递：A~B、B~C 不会把 A、C 绑在一起）。"""
    by_id = {r["id"]: r for r in rows}
    used: set[int] = set()
    out = []
    for r in sorted((by_id[i] for i in adj if i in by_id), key=_newest_key, reverse=True):
        if r["id"] in used:
            continue
        drops = [by_id[i] for i in adj[r["id"]]
                 if i in by_id and i not in used and _newest_key(by_id[i]) < _newest_key(r)]
        if not drops:
            continue
        drops.sort(key=_newest_key, reverse=True)
        used.add(r["id"])
        used.update(d["id"] for d in drops)
        out.append(("title", r, drops))
    return out


def plan_dedupe(conn, cfg) -> list[tuple]:
    """只算不写：返回 [(规则, 保留的行, [被取代的行])]。每条笔记最多出现一次。"""
    rows = _load(conn, bool(cfg.get("consolidate.include_legacy", False)))
    groups: list[tuple] = []
    taken: set[int] = set()

    def absorb(new_groups) -> None:
        for rule, keep, drops in new_groups:
            if keep["id"] in taken:
                continue
            drops = [d for d in drops if d["id"] not in taken]
            if not drops:
                continue
            taken.add(keep["id"])
            taken.update(d["id"] for d in drops)
            groups.append((rule, keep, drops))

    absorb(_groups_by_key(rows, lambda r: (r["project"], r["slot"]) if r["slot"] and r["project"] else None, "slot"))
    rest = [r for r in rows if r["id"] not in taken]
    absorb(_groups_by_key(rest, lambda r: (r["project"], r["type"], r["content_hash"])
                          if r["content_hash"] and r["project"] else None, "hash"))
    rest = [r for r in rows if r["id"] not in taken]
    absorb(_star_clusters(rest, _title_pairs(conn, rest, cfg)))
    return groups


def apply_dedupe(conn, groups: list[tuple], now: float) -> int:
    """标记被取代的笔记（单个写事务）。返回实际标记条数。"""
    n = 0
    ts = int(now)
    with db.write_tx(conn):
        for _rule, keep, drops in groups:
            for d in drops:
                cur = conn.execute("UPDATE notes SET state='superseded', superseded_by=?, superseded_at=? "
                                   "WHERE id=? AND state='active'", (keep["id"], ts, d["id"]))
                if not cur.rowcount:
                    continue
                n += 1
                index.set_doc_active(conn, "note", d["id"], False)
                uses = int(d["use_count"] or 0)
                if uses:  # 被取用过的次数不丢，并到保留的那条上
                    conn.execute("UPDATE notes SET use_count=coalesce(use_count,0)+?, last_used=max(coalesce(last_used,0),?) "
                                 "WHERE id=?", (uses, int(d["last_used"] or 0), keep["id"]))
                    conn.execute("UPDATE docs SET use_count=coalesce(use_count,0)+? WHERE src='note' AND src_id=?",
                                 (uses, keep["id"]))
    return n


# ---------------------------------------------------------------- 2. 降权（给检索层调用）

def demotion_factor(ts, last_mentioned, last_used, *, now: float | None = None, cfg=None) -> float:
    """一条笔记在排序里的乘数：创建、被提及、被取用三个时间里最晚的一个比 stale_days 还早，就返回
    consolidate.stale_factor（默认 0.8），否则 1.0。

    检索层接入点：knotcord/search/api.py 的 search() 里给 sc 乘 boost / 1.05 的那段（"for doc_id, sc in
    scores.items()"），对 src=='note' 的条目再乘 note_factors(conn, [src_id...]).get(src_id, 1.0)。
    """
    cfg = cfg or _config.load()
    now = time.time() if now is None else now
    latest = max(_sec(ts), _sec(last_mentioned), _sec(last_used))
    if latest <= 0:
        return 1.0  # 没有任何时间信息：不降权
    if now - latest > int(cfg.get("consolidate.stale_days", 90)) * _DAY:
        return float(cfg.get("consolidate.stale_factor", 0.8))
    return 1.0


def note_factors(conn, note_ids, *, now: float | None = None, cfg=None) -> dict[int, float]:
    """批量版：返回 {笔记 id: 乘数}，只含被降权的（< 1.0）；查不到或没降权的不在里面，调用方 .get(id, 1.0)。"""
    cfg = cfg or _config.load()
    now = time.time() if now is None else now
    ids = [int(i) for i in note_ids]
    out: dict[int, float] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        sql = ("SELECT id, ts, last_mentioned, last_used FROM notes WHERE id IN (%s)" % ",".join("?" * len(part)))
        for r in conn.execute(sql, part):
            f = demotion_factor(r["ts"], r["last_mentioned"], r["last_used"], now=now, cfg=cfg)
            if f < 1.0:
                out[r["id"]] = f
    return out


def stale_count(conn, cfg, now: float) -> int:
    """现在有多少条 active 笔记会被降权（只用来汇报）。"""
    cutoff = now - int(cfg.get("consolidate.stale_days", 90)) * _DAY

    def s(col: str) -> str:
        return f"coalesce(CASE WHEN {col}>100000000000 THEN {col}/1000.0 ELSE {col} END, 0)"
    latest = f"max({s('ts')}, {s('last_mentioned')}, {s('last_used')})"
    return conn.execute(f"SELECT count(*) FROM notes WHERE state='active' AND {latest}>0 AND {latest}<?",
                        (cutoff,)).fetchone()[0]


# ---------------------------------------------------------------- 3. 晋升提名

def nominate(conn, cfg, now: float) -> dict[str, list[dict]]:
    """按项目生成"建议写进记忆文件"清单，写 kv。返回 {项目: [{id, title, uses}]}。"""
    min_uses = int(cfg.get("consolidate.promote_min_uses", 5))
    per_proj = int(cfg.get("consolidate.promote_max", 5))
    cooldown = int(cfg.get("consolidate.promote_cooldown_days", 30)) * _DAY
    try:
        history = json.loads(db.kv_get(conn, KEY_NOMINATED, "{}") or "{}")
        if not isinstance(history, dict):
            history = {}
    except ValueError:
        history = {}
    history = {k: v for k, v in history.items() if isinstance(v, (int, float)) and now - v < cooldown}
    rows = conn.execute(
        "SELECT id, project, title, use_count FROM notes WHERE state='active' AND type<>'unfinished' "
        "AND coalesce(use_count,0)>=? AND project IS NOT NULL AND project<>'' "
        "ORDER BY use_count DESC, coalesce(last_used,0) DESC, id DESC", (min_uses,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        if str(r["id"]) in history:
            continue
        lst = out.setdefault(r["project"], [])
        if len(lst) < per_proj:
            lst.append({"id": r["id"], "title": " ".join(str(r["title"] or "").split())[:80],
                        "uses": int(r["use_count"])})
    with db.write_tx(conn):
        conn.execute("DELETE FROM kv WHERE key LIKE ?", (PROMOTE_PREFIX + "%",))
        for proj, items in out.items():
            conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (PROMOTE_PREFIX + proj, json.dumps(items, ensure_ascii=False)))
            for it in items:
                history[str(it["id"])] = int(now)
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY_NOMINATED, json.dumps(history)))
    return out


# ---------------------------------------------------------------- 入口

def run(conn, cfg, now: float | None = None) -> dict:
    """跑一遍每周整理。三个阶段互相独立，某一段出错只记日志、不影响其余。"""
    now = time.time() if now is None else now
    if not cfg.get("consolidate.enabled", True):
        return {"enabled": False}
    res: dict = {}
    marked: list[int] = []
    try:
        groups = plan_dedupe(conn, cfg)
        res["superseded"] = apply_dedupe(conn, groups, now)
        res["groups"] = len(groups)
        res["by_rule"] = {r: sum(len(g[2]) for g in groups if g[0] == r) for r in ("slot", "hash", "title")}
        marked = [d["id"] for g in groups for d in g[2]][:_MARKED_KEEP]
    except sqlite3.Error as e:
        _log.warning("去重合并失败: %s: %s", type(e).__name__, e)
        res["dedupe_error"] = str(e)[:100]
    try:
        res["stale"] = stale_count(conn, cfg, now)
    except sqlite3.Error as e:
        _log.warning("统计降权笔记失败: %s", e)
    try:
        res["promote"] = sum(len(v) for v in nominate(conn, cfg, now).values())
    except sqlite3.Error as e:
        _log.warning("晋升提名失败: %s", e)
        res["promote_error"] = str(e)[:100]
    try:
        db.kv_set(conn, KEY_LAST, json.dumps({"at": int(now), "superseded": res.get("superseded", 0),
                                              "ids": marked}))
    except sqlite3.Error:
        pass
    _log.info("每周整理完成: %s", res)
    return res
