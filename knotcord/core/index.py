"""关键词索引：汉字二元组 + 英文整词（方案书第 5 节）。

索引和查询共用同一条切分规则 segment()：
- 连续汉字切成重叠二元组（"宝塔面板" -> "宝塔 塔面 面板"），孤立的单个汉字保留成单字；
- 英文数字按整词小写；带连字符等连接符的词（3x-ui、config.toml）拆成相邻的几个词，
  查询时作为一个短语，所以 "3x-ui" 能命中含 "3x-ui" 的文档，也不会让 "-" 变成 FTS5 语法。
FTS5 表 docs_fts 是 contentless，rowid 等于 docs.id，正文和索引在同一个事务里写。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import contextmanager

from . import db as _db

_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f"
# 一段汉字 | 一段字母数字（不含汉字、不含下划线）
_TOKEN_RE = re.compile(rf"([{_HAN}]+)|((?:(?![{_HAN}])[^\W_])+)")
_JOINERS = set("-_./'@:")  # 夹在两个英文词之间的单个连接符：合成一个短语

# 高频虚词二元组：只在"放宽成 OR"那一步剔除
_STOP_BIGRAMS = frozenset(
    "怎么 什么 是不 不是 的话 可以 一个 这个 那个 我们 你们 他们 是否 如何 为什 有没 还是 或者 以及 因为 所以 是多 就是 然后 但是".split()
)

_MAX_OR_TERMS = 64


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = text.encode("utf-8", "replace").decode("utf-8")  # 孤立代理字符会让 sqlite 编码失败
    return unicodedata.normalize("NFKC", text).lower()


class _Phrase:
    __slots__ = ("han", "tokens", "single")

    def __init__(self, han: bool, tokens: list[str], single: bool = False) -> None:
        self.han = han
        self.tokens = tokens
        self.single = single  # 孤立的单个汉字


def _parse(text: str) -> list[_Phrase]:
    text = _clean(text)
    phrases: list[_Phrase] = []
    prev_word_end = -1
    for m in _TOKEN_RE.finditer(text):
        han_run, word = m.group(1), m.group(2)
        if han_run:
            if len(han_run) == 1:
                phrases.append(_Phrase(True, [han_run], True))
            else:
                phrases.append(_Phrase(True, [han_run[i:i + 2] for i in range(len(han_run) - 1)]))
            prev_word_end = -1
        else:
            gap = text[prev_word_end:m.start()] if prev_word_end >= 0 else ""
            if phrases and not phrases[-1].han and len(gap) == 1 and gap in _JOINERS:
                phrases[-1].tokens.append(word)
            else:
                phrases.append(_Phrase(False, [word]))
            prev_word_end = m.end()
    return phrases


def segment(text: str) -> str:
    """切成空格分隔的索引词串。"""
    if not text:
        return ""
    return " ".join(t for p in _parse(text) for t in p.tokens)


# ---------------------------------------------------------------- 写入

@contextmanager
def _in_tx(conn: sqlite3.Connection):
    # 调用方已开事务就并入；没开就自己开一个，保证"正文+索引"原子
    with _db.write_tx(conn):
        yield


def index_doc(conn: sqlite3.Connection, src: str, src_id: int, project: str, ts: int,
              text: str, *, active: bool = True) -> int:
    """upsert docs 行并重写 docs_fts（删旧插新同一事务），返回 doc_id。"""
    seg = segment(text)
    with _in_tx(conn):
        rows = conn.execute(
            "INSERT INTO docs(src, src_id, project, ts, active) VALUES(?,?,?,?,?) "
            "ON CONFLICT(src, src_id) DO UPDATE SET project=excluded.project, ts=excluded.ts, "
            "active=excluded.active RETURNING id",
            (src, src_id, project, ts, 1 if active else 0),
        ).fetchall()
        doc_id = rows[0][0]
        conn.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
        conn.execute("INSERT INTO docs_fts(rowid, seg) VALUES(?, ?)", (doc_id, seg))
    return doc_id


def remove_doc(conn: sqlite3.Connection, src: str, src_id: int) -> None:
    with _in_tx(conn):
        row = conn.execute("SELECT id FROM docs WHERE src=? AND src_id=?", (src, src_id)).fetchone()
        if row is None:
            return
        doc_id = row[0]
        conn.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
        conn.execute("DELETE FROM docs_vec WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def set_doc_active(conn: sqlite3.Connection, src: str, src_id: int, active: bool) -> None:
    with _in_tx(conn):
        conn.execute("UPDATE docs SET active=? WHERE src=? AND src_id=?",
                     (1 if active else 0, src, src_id))


# ---------------------------------------------------------------- 查询

def _quote(tokens: list[str]) -> str:
    # 切分后的词只含字母数字，不可能带引号；仍然统一包成 FTS5 字符串，AND/NEAR 之类都成了普通词
    return '"' + " ".join(tokens).replace('"', '""') + '"'


def _run(conn: sqlite3.Connection, match: str, project, srcs, limit, active_only) -> list[sqlite3.Row]:
    sql = [
        "SELECT d.id AS doc_id, d.src AS src, d.src_id AS src_id, d.project AS project, d.ts AS ts,",
        "       -bm25(docs_fts) AS score",
        "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid",
        "WHERE docs_fts MATCH ?",
    ]
    args: list = [match]
    # 过滤放在 LIMIT 之前，已取代/停用的旧条目不会把前 N 名占满
    if active_only:
        sql.append("AND d.active = 1")
    if project is not None:
        sql.append("AND d.project = ?")
        args.append(project)
    if srcs:
        sql.append("AND d.src IN (%s)" % ",".join("?" * len(srcs)))
        args.extend(srcs)
    sql.append("ORDER BY bm25(docs_fts), d.ts DESC LIMIT ?")
    args.append(int(limit))
    try:
        return conn.execute("\n".join(sql), args).fetchall()
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower() or "malformed" in str(e).lower():
            return []  # 兜底：任何输入都不能把语法错误抛给上层
        raise


def fts_search(conn: sqlite3.Connection, query: str, *, project: str | None = None,
               srcs: tuple[str, ...] | None = None, limit: int = 50,
               active_only: bool = True) -> list[sqlite3.Row]:
    """返回 doc_id, src, src_id, project, ts, score（越大越好）。

    第一步：每段汉字或英文作短语，段间 AND。结果少于 3 条，第二步放宽成二元组/词 OR + bm25，
    严格命中的排在前面，放宽补进来的排在后面（score 只在各自阶段内可比，列表顺序就是最终顺序）。
    整个查询只有单个汉字（切不出可搜的词）时返回空列表，由上层 LIKE 兜底。
    """
    if not query or not isinstance(query, str) or limit <= 0:
        return []
    phrases = _parse(query)
    # 孤立单字索引里只在"孤立"时才有，和别的词 AND 会误杀，丢掉；丢完没东西就返回空
    usable = [p for p in phrases if not p.single]
    if not usable:
        return []
    strict = _run(conn, " AND ".join(_quote(p.tokens) for p in usable),
                  project, srcs, limit, active_only)
    if len(strict) >= 3:
        return strict
    terms: list[str] = []
    for p in usable:
        for t in p.tokens:
            if t not in terms:
                terms.append(t)
    kept = [t for t in terms if t not in _STOP_BIGRAMS] or terms
    kept = kept[:_MAX_OR_TERMS]
    if len(kept) == 1 and len(usable) == 1 and len(usable[0].tokens) == 1:
        return strict  # 本来就是单个词，放宽没有意义
    relaxed = _run(conn, " OR ".join(_quote([t]) for t in kept),
                   project, srcs, limit + len(strict), active_only)
    seen = {r["doc_id"] for r in strict}
    out = list(strict)
    for r in relaxed:
        if r["doc_id"] not in seen:
            out.append(r)
            seen.add(r["doc_id"])
        if len(out) >= limit:
            break
    return out
