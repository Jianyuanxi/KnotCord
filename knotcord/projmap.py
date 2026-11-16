"""项目地图：自动算"项目之间的联系"。只许 import 标准库和 knotcord.core。

写进 kv：键 projmap:<项目名>，值是 JSON 列表 [{"project": "X", "score": 数, "why": "互提 12 次，共用文件 3 个"}]，
从高到低，最多 projmap.max 个。读用 related()。

两种信号（都不调模型、不联网）：
1. 互提：项目 A 的笔记/总结/提示正文里出现项目 B 的名字，用 FTS 索引按项目限定计数（以文档为单位，一篇只算一次），
   A→B 和 B→A 相加。名字是另一个项目名的"短语子串"时（new-chat 和 new-chat-2），短名只算没命中长名的文档。
2. 共用文件：A 的笔记 files 里的绝对路径落在 B 的工作目录下（B 的目录取 sessions 表的 cwd）。一条路径落在多个项目目录下时
   归最深的那个；A、B 两个方向的不同路径数相加。

得分 = 互提次数 + 共用文件数 * FILE_WEIGHT；低于 projmap.min_score 不算相关。
不参与的项目名：纯数字、长度小于 3、命中 projmap.exclude（不分大小写，支持 * 通配）。这些名字既不当来源也不当目标。
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sqlite3
import time

from knotcord.core import config as _config
from knotcord.core import db, index, log

_log = log.get_logger("projmap")

KEY_PREFIX = "projmap:"
KEY_AT = "projmap_at"      # 上次重算时间（不以 projmap: 开头，免得和叫 at 的项目重名）
FILE_WEIGHT = 3            # 一个共用文件抵几次互提：文件是动手改过的证据，比名字出现在文字里更硬
MIN_NAME_LEN = 3
_SRCS = ("note", "summary", "prompt")
_ABS_RE = re.compile(r"^(?:[a-z]:/|/)")


# ---------------------------------------------------------------- 项目名是否参与

def name_ok(name, cfg) -> bool:
    n = str(name or "").strip()
    if len(n) < MIN_NAME_LEN or n.isdigit():
        return False
    low = n.lower()
    for pat in cfg.get("projmap.exclude", []) or []:
        if fnmatch.fnmatchcase(low, str(pat).lower()):
            return False
    return True


def _name_key(name: str) -> str | None:
    """名字在 FTS 里的 MATCH 表达式；切不出可搜的词（只有单个汉字、全是符号）返回 None。"""
    return index.phrase_match(name)


# ---------------------------------------------------------------- 信号 1：互提

def _mention_docs(conn, name: str) -> dict[int, tuple[str, str, int]]:
    """提到 name 的文档 {doc_id: (文档所属项目, src, src_id)}。"""
    match = _name_key(name)
    if match is None:
        return {}
    sql = ("SELECT d.id, d.project, d.src, d.src_id FROM docs_fts JOIN docs d ON d.id=docs_fts.rowid "
           "WHERE docs_fts MATCH ? AND d.active=1 AND d.src IN (%s) AND d.project IS NOT NULL AND d.project<>''"
           % ",".join("?" * len(_SRCS)))
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(sql, (match, *_SRCS))}
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower() or "malformed" in str(e).lower():
            return {}
        raise


_TEXT_SQL = {
    "note": "SELECT coalesce(title,'')||' '||coalesce(body,'')||' '||coalesce(facts,'')||' '||coalesce(files,'') "
            "FROM notes WHERE id=?",
    "summary": "SELECT coalesce(request,'')||' '||coalesce(completed,'')||' '||coalesce(learned,'')||' '||"
               "coalesce(next_steps,'') FROM summaries WHERE id=?",
    "prompt": "SELECT coalesce(body,'') FROM events WHERE id=?",
}


def _is_plain_word(name: str) -> bool:
    """纯英文字母、除首字母外都是小写（Road、Building、Span）：这类名字也是普通英语词，FTS 不分大小写会把
    "building a system"、"road barrier" 都算成提到项目。驼峰（MyTool）、全大写（API）、带数字符号的够独特，不用核。"""
    return name.isascii() and name.isalpha() and name[1:] == name[1:].lower()


def _case_ok(conn, name: str, docs: dict[int, tuple[str, str, int]]) -> dict[int, tuple[str, str, int]]:
    """只留原文里按项目名原样大小写、整词出现的文档。"""
    rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])")
    out = {}
    for i, v in docs.items():
        row = conn.execute(_TEXT_SQL[v[1]], (v[2],)).fetchone()
        if row and row[0] and rx.search(row[0]):
            out[i] = v
    return out


def _contains(longer: str, shorter: str) -> bool:
    """shorter 的切词序列是 longer 的连续子序列（FTS 里搜 shorter 会同时命中含 longer 的文档）。"""
    return f" {index.segment(shorter)} " in f" {index.segment(longer)} "


def _mention_pairs(conn, names: list[str]) -> dict[tuple[str, str], int]:
    """{(提到的人 A, 被提到的 B): 文档数}，A≠B，A、B 都在 names 里。"""
    docs = {n: _mention_docs(conn, n) for n in names}
    node = set(names)
    out: dict[tuple[str, str], int] = {}
    for b in names:
        mine = docs[b]
        if not mine:
            continue
        for x in names:  # 更长的名字已经把这些文档认走了（认走用的是未经大小写核对的全集）
            if x != b and _contains(x, b):
                mine = {i: v for i, v in mine.items() if i not in docs[x]}
        if _is_plain_word(b):
            mine = _case_ok(conn, b, mine)
        for p, _src, _sid in mine.values():
            if p != b and p in node:
                out[(p, b)] = out.get((p, b), 0) + 1
    return out


# ---------------------------------------------------------------- 信号 2：共用文件

def _norm_path(p) -> str:
    s = posixpath.normpath(str(p or "").replace("\\", "/").strip().lower())
    return s.rstrip("/")


def _home() -> str:
    return _norm_path(os.path.expanduser("~"))


def _project_dir(project: str, cwd: str) -> str | None:
    """sessions.cwd 可能是项目里的子目录：路径里有一段等于项目名就截到那一段。
    家目录本身（及其上层）、磁盘根、AppData 下的临时/暂存目录不当项目目录，否则整个家目录都算它的。"""
    d = _norm_path(cwd)
    if not _ABS_RE.match(d):
        return None
    parts = d.split("/")
    low = project.lower()
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == low:
            d = "/".join(parts[:i + 1])
            break
    home = _home()
    if len([x for x in d.split("/") if x]) < 2 or home == d or home.startswith(d + "/"):
        return None
    if "/appdata/" in d + "/" and home and d.startswith(home + "/"):
        return None
    return d


def _project_dirs(conn, names: set[str]) -> dict[str, set[str]]:
    """{目录: {项目...}}。"""
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT DISTINCT project, cwd FROM sessions WHERE cwd IS NOT NULL AND cwd<>'' "
                          "AND project IS NOT NULL"):
        if r[0] in names:
            d = _project_dir(r[0], r[1])
            if d:
                out.setdefault(d, set()).add(r[0])
    return out


def _owners(path: str, dirs: dict[str, set[str]]) -> set[str]:
    """文件落在哪个项目目录下：最深的那个目录的项目。"""
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):  # 目录本身也算（i = len-1 是文件所在目录）
        owners = dirs.get("/".join(parts[:i]))
        if owners:
            return owners
    return set()


def _file_pairs(conn, names: set[str], dirs: dict[str, set[str]]) -> dict[tuple[str, str], int]:
    """{(写笔记的 A, 目录属于的 B): 不同路径数}。"""
    seen: set[tuple[str, str, str]] = set()
    out: dict[tuple[str, str], int] = {}
    if not dirs:
        return out
    for r in conn.execute("SELECT project, files FROM notes WHERE files IS NOT NULL AND files NOT IN ('', '[]') "
                          "AND project IS NOT NULL"):
        a = r[0]
        if a not in names:
            continue
        try:
            files = json.loads(r[1])
        except (TypeError, ValueError):
            continue
        if not isinstance(files, list):
            continue
        for f in files:
            p = _norm_path(f)
            if not _ABS_RE.match(p):
                continue
            for b in _owners(p, dirs):
                if b != a and (a, b, p) not in seen:
                    seen.add((a, b, p))
                    out[(a, b)] = out.get((a, b), 0) + 1
    return out


# ---------------------------------------------------------------- 汇总

def compute(conn, cfg) -> dict[str, list[dict]]:
    """只算不写：{项目: [{project, score, why}]}，只含有相关项目的。"""
    projects = {r[0] for r in conn.execute(
        "SELECT project FROM notes WHERE project IS NOT NULL AND project<>'' "
        "UNION SELECT project FROM summaries WHERE project IS NOT NULL AND project<>'' "
        "UNION SELECT project FROM sessions WHERE project IS NOT NULL AND project<>''")}
    names = sorted(p for p in projects if name_ok(p, cfg))
    node = set(names)
    mention = _mention_pairs(conn, names)
    files = _file_pairs(conn, node, _project_dirs(conn, node))

    edges: dict[tuple[str, str], list[int]] = {}  # 无序对 -> [互提数, 共用文件数]
    for (a, b), n in mention.items():
        edges.setdefault(tuple(sorted((a, b))), [0, 0])[0] += n
    for (a, b), n in files.items():
        edges.setdefault(tuple(sorted((a, b))), [0, 0])[1] += n

    min_score = float(cfg.get("projmap.min_score", 25))
    out: dict[str, list[dict]] = {}
    for (a, b), (m, f) in edges.items():
        score = m + f * FILE_WEIGHT
        if score < min_score:
            continue
        why = "，".join(x for x in (f"互提 {m} 次" if m else "", f"共用文件 {f} 个" if f else "") if x)
        out.setdefault(a, []).append({"project": b, "score": score, "why": why})
        out.setdefault(b, []).append({"project": a, "score": score, "why": why})
    top = max(1, int(cfg.get("projmap.max", 3)))
    for p, items in out.items():
        items.sort(key=lambda x: (-x["score"], x["project"]))
        out[p] = items[:top]
    return out


def build(conn, cfg, now: float | None = None) -> dict[str, list[dict]]:
    """重算并写 kv（先清掉旧的 projmap:*，免得已经不相关的项目留着旧结果）。关着返回 {}，也不动 kv。"""
    now = time.time() if now is None else now
    if not cfg.get("projmap.enabled", True):
        return {}
    result = compute(conn, cfg)
    with db.write_tx(conn):
        conn.execute("DELETE FROM kv WHERE key LIKE ?", (KEY_PREFIX + "%",))
        for p, items in result.items():
            conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (KEY_PREFIX + p, json.dumps(items, ensure_ascii=False)))
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY_AT, str(int(now))))
    _log.info("项目地图已重算: %d 个项目有相关项目", len(result))
    return result


def related(conn, project: str) -> list[dict]:
    """读 kv；没算过、读不到、内容不对都返回空列表。"""
    try:
        raw = db.kv_get(conn, KEY_PREFIX + str(project), "") or ""
        items = json.loads(raw) if raw else []
    except (sqlite3.Error, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict) and x.get("project")]
