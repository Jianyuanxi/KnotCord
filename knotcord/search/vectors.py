"""向量一路：嵌入哪些文本、补向量、内存里的向量矩阵、按意思找候选。

- 文本口径（doc_texts）：和关键词索引用的是同一批原表字段（notes、summaries、events 里的提示、内置记忆文件），
  但不含 files（文件路径对语义没有贡献）；超长的由 Embedder.encode 按 embed.max_tokens 截断。
- 补向量（backfill）：持有租约的 worker 调，一次最多 embed.round_docs 条、每批 embed.batch 条，
  批与批之间看 stop_event；写库在一个短事务里，第一句确认租约还是自己的（细则 1.1）。
  "缺向量"没有单独状态：docs 里有、docs_vec 里没有（或模型名不同）的就是待嵌的（细则 2.3）。
- 内存矩阵：第一次检索时把 docs_vec 读成 float32 矩阵；之后只比 max(doc_id) 和 kv 里的失效计数，
  有新增就追加，有失效（文档被改写重新索引、被删）才整体重读。
- 全部重依赖（numpy、onnxruntime）在函数里 import；任何异常都不抛给检索，向量一路返回空。
"""

from __future__ import annotations

import json
import threading
import time

from knotcord.core import config, db, log
from knotcord.core.index import VEC_GEN_KEY, bump_vec_gen
from knotcord.core.lease import LeaseLost

from . import embed

_log = log.get_logger("vectors")

_MISSING_SQL = (
    "SELECT d.id AS id, d.src AS src, d.src_id AS src_id FROM docs d "
    "WHERE d.active=1 AND d.id>? "
    "AND NOT EXISTS (SELECT 1 FROM docs_vec v WHERE v.doc_id=d.id AND v.model=?) "
    "ORDER BY d.id LIMIT ?")


# ---------------------------------------------------------------- 文本口径

def _jl(s) -> list[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return [str(s)]
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _join(*parts) -> str:
    return "\n".join(str(p) for p in parts if p)


def _in(n: int) -> str:
    return ",".join("?" * n)


def doc_texts(conn, rows) -> dict[int, str]:
    """rows: (doc_id, src, src_id) 序列 -> {doc_id: 要嵌入的文本}。原行没了的（孤儿索引）不在结果里。"""
    by: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        by.setdefault(r[1], []).append((r[0], r[2]))
    out: dict[int, str] = {}
    for src, pairs in by.items():
        sids = [p[1] for p in pairs]
        did = {p[1]: p[0] for p in pairs}
        if src == "note":
            for r in conn.execute(f"SELECT id, title, body, facts, tags FROM notes WHERE id IN ({_in(len(sids))})", sids):
                out[did[r["id"]]] = _join(r["title"], r["body"], "\n".join(_jl(r["facts"])), " ".join(_jl(r["tags"])))
        elif src == "summary":
            for r in conn.execute("SELECT id, request, learned, completed, next_steps, extra FROM summaries "
                                  f"WHERE id IN ({_in(len(sids))})", sids):
                extra: dict = {}
                try:
                    extra = json.loads(r["extra"]) if r["extra"] else {}
                except (ValueError, TypeError):
                    pass
                out[did[r["id"]]] = _join(r["request"], extra.get("investigated"), r["learned"], r["completed"],
                                          r["next_steps"], extra.get("notes"))
        elif src == "prompt":
            for r in conn.execute(f"SELECT id, body FROM events WHERE id IN ({_in(len(sids))})", sids):
                out[did[r["id"]]] = r["body"] or ""
        elif src == "memfile":
            from pathlib import Path

            from .memfiles import INDEX_CAP, parse_file
            for r in conn.execute(f"SELECT id, path, name, description FROM memfiles WHERE id IN ({_in(len(sids))})",
                                  sids):
                try:
                    name, desc, body = parse_file(Path(r["path"]))
                    out[did[r["id"]]] = _join(name, desc, body)[:INDEX_CAP]
                except OSError:  # 文件已经没了：退回登记的名字和描述
                    out[did[r["id"]]] = _join(r["name"], r["description"])
    return {k: v for k, v in out.items() if v and v.strip()}


# ---------------------------------------------------------------- 补向量

def missing_count(conn) -> int:
    return conn.execute(
        "SELECT count(*) FROM docs d WHERE d.active=1 AND NOT EXISTS "
        "(SELECT 1 FROM docs_vec v WHERE v.doc_id=d.id AND v.model=?)", (embed.MODEL_ID,)).fetchone()[0]


def has_missing(conn) -> bool:
    """有没有缺向量的有效文档（最多扫到第一条就停，比 missing_count 便宜）。"""
    return conn.execute(
        "SELECT 1 FROM docs d WHERE d.active=1 AND NOT EXISTS "
        "(SELECT 1 FROM docs_vec v WHERE v.doc_id=d.id AND v.model=?) LIMIT 1", (embed.MODEL_ID,)).fetchone() is not None


def backfill(conn, cfg, lease, stop_event, *, max_docs: int | None = None) -> tuple[int, bool]:
    """补一轮。返回 (本轮写入条数, 后面是否还有)。模型不可用、向量关闭、租约不是自己的、stop_event 置位都直接返回。"""
    if not cfg.get("search.vector", True) or stop_event.is_set():
        return 0, False
    if lease is not None and not lease.is_mine():
        return 0, False
    if not embed.model_present():
        return 0, False
    batch = max(1, int(cfg.get("embed.batch", 8)))
    quota = max(1, int(max_docs if max_docs is not None else cfg.get("embed.round_docs", 256)))
    done = 0
    cursor = 0
    emb = None
    while done < quota and not stop_event.is_set():
        rows = conn.execute(_MISSING_SQL, (cursor, embed.MODEL_ID, batch)).fetchall()
        if not rows:
            embed.release(bulk=True)  # 补完了：把批量用的那个（带内存池的）模型放掉，常驻内存回落
            return done, False
        cursor = rows[-1]["id"]
        texts = doc_texts(conn, [(r["id"], r["src"], r["src_id"]) for r in rows])
        if not texts:
            continue  # 这一批全是孤儿或空文本：跳过，游标已前移
        if emb is None:
            emb = embed.get(cfg, bulk=True)
            if emb is None:
                return done, False
        ids = list(texts)
        vecs = emb.encode([texts[i] for i in ids], batch_size=batch)
        # encode 返回的已经是 float32，直接转字节
        params = [(i, embed.MODEL_ID, vecs[k].tobytes(), i) for k, i in enumerate(ids)]
        try:
            with db.write_tx(conn):
                if lease is not None:
                    lease.assert_mine_in_tx()
                mx = conn.execute("SELECT max(doc_id) FROM docs_vec").fetchone()[0] or 0
                conn.executemany(
                    "INSERT OR REPLACE INTO docs_vec(doc_id, model, vec) "
                    "SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM docs WHERE id=?)", params)
                if min(ids) < mx:  # 比已有最大编号小的补进来：内存矩阵只会追加，通知它整体重读
                    bump_vec_gen(conn)
        except LeaseLost:
            _log.info("补向量中途租约被别人接手，本轮放弃")
            return done, False
        done += len(ids)
    if stop_event.is_set():
        embed.release(bulk=True)
    return done, (not stop_event.is_set()) and done >= quota


# ---------------------------------------------------------------- 内存矩阵

class _Index:
    __slots__ = ("ids", "mat", "n", "dim", "model", "maxid", "gen")


_idx: _Index | None = None
_idx_lock = threading.Lock()


def _gen(conn) -> str:
    return db.kv_get(conn, VEC_GEN_KEY, "0") or "0"


def _read_rows(cur, dim: int, ids, mat, n: int) -> int:
    """把 (doc_id, vec) 游标读进预分配的 ids/mat，从第 n 行起写；返回新的 n。长度不对的行跳过。"""
    import numpy as np
    want = dim * 4
    while True:
        chunk = cur.fetchmany(2000)
        if not chunk:
            return n
        good = [(r[0], r[1]) for r in chunk if r[1] is not None and len(r[1]) == want]
        if not good:
            continue
        m = len(good)
        ids[n:n + m] = [g[0] for g in good]
        mat[n:n + m] = np.frombuffer(b"".join(g[1] for g in good), dtype=np.float32).reshape(m, dim)
        n += m


def _load_full(conn, dim: int) -> _Index:
    import numpy as np
    gen = _gen(conn)  # 先读失效计数再读数据：读的过程中有改动，下次比对会发现不一致而重读
    total = conn.execute("SELECT count(*) FROM docs_vec WHERE model=?", (embed.MODEL_ID,)).fetchone()[0]
    cap = total + max(1024, total // 8)  # 留出追加的余量，追加时不用整块复制
    ix = _Index()
    ix.dim, ix.model, ix.gen = dim, embed.MODEL_ID, gen
    ix.ids = np.empty(cap, dtype=np.int64)
    ix.mat = np.empty((cap, dim), dtype=np.float32)
    cur = conn.execute("SELECT doc_id, vec FROM docs_vec WHERE model=? ORDER BY doc_id", (embed.MODEL_ID,))
    ix.n = _read_rows(cur, dim, ix.ids, ix.mat, 0)
    ix.maxid = int(ix.ids[ix.n - 1]) if ix.n else 0
    return ix


def _append(conn, ix: _Index) -> _Index:
    import numpy as np
    rows = conn.execute("SELECT doc_id, vec FROM docs_vec WHERE doc_id>? AND model=? ORDER BY doc_id",
                        (ix.maxid, embed.MODEL_ID)).fetchall()
    if not rows:
        return ix
    need = ix.n + len(rows)
    if need > len(ix.ids):  # 余量不够：换一块更大的（旧块留给还在用它的查询）
        cap = need + max(1024, need // 8)
        ids = np.empty(cap, dtype=np.int64)
        mat = np.empty((cap, ix.dim), dtype=np.float32)
        ids[:ix.n] = ix.ids[:ix.n]
        mat[:ix.n] = ix.mat[:ix.n]
        ix.ids, ix.mat = ids, mat
    n = _read_rows(_Rows(rows), ix.dim, ix.ids, ix.mat, ix.n)
    ix.n = n
    ix.maxid = int(ix.ids[n - 1]) if n else ix.maxid
    return ix


class _Rows:
    """让已取出的行列表也支持 fetchmany，复用 _read_rows。"""

    def __init__(self, rows) -> None:
        self._rows = rows
        self._pos = 0

    def fetchmany(self, k: int):
        part = self._rows[self._pos:self._pos + k]
        self._pos += k
        return part


def get_index(conn, dim: int):
    """返回最新的内存矩阵快照 (ids, mat, n)；没有向量返回 None。"""
    global _idx
    with _idx_lock:
        ix = _idx
        mx = conn.execute("SELECT max(doc_id) FROM docs_vec").fetchone()[0] or 0
        gen = _gen(conn)
        if ix is None or ix.dim != dim or ix.model != embed.MODEL_ID or ix.gen != gen or mx < ix.maxid:
            ix = _load_full(conn, dim)
        elif mx > ix.maxid:
            ix = _append(conn, ix)
        _idx = ix
        if ix.n == 0:
            return None
        return ix.ids, ix.mat, ix.n


def release() -> None:
    global _idx
    with _idx_lock:
        _idx = None


def maybe_unload(minutes: float) -> bool:
    """worker 心跳调：模型和矩阵 minutes 分钟没人用就一起释放。"""
    if embed.maybe_unload(minutes):
        release()
        return True
    return False


def index_size() -> tuple[int, int]:
    """(行数, 矩阵字节数)；没加载是 (0, 0)。给自查和测试看内存用。"""
    ix = _idx
    return (ix.n, ix.mat.nbytes) if ix is not None else (0, 0)


# ---------------------------------------------------------------- 查询

def candidates(conn, query: str, *, project, limit: int, include_superseded: bool,
               cfg=None) -> list[tuple[int, float]]:
    """返回 [(doc_id, 余弦相似度)]，从高到低，最多 limit 条。任何问题都返回空列表。"""
    try:
        cfg = cfg or config.load()
        emb = embed.get(cfg)
        if emb is None:
            return []
        snap = get_index(conn, emb.dim)
        if snap is None:
            return []
        import numpy as np
        ids, mat, n = snap
        ids = ids[:n]
        q = emb.encode_query(query)
        sc = mat[:n] @ q
        # 过滤放在取前 N 之前（和关键词一路一样），已取代的旧条目、别的项目不会把名额占满
        if not include_superseded:
            bad = [r[0] for r in conn.execute("SELECT id FROM docs WHERE active=0")]
            if bad:
                sc[np.isin(ids, np.array(bad, dtype=np.int64))] = -np.inf
        if project is not None:
            keep = [r[0] for r in conn.execute("SELECT id FROM docs WHERE project=?", (project,))]
            sc[~np.isin(ids, np.array(keep, dtype=np.int64))] = -np.inf
        k = min(int(limit), n)
        if k <= 0:
            return []
        top = np.argpartition(-sc, k - 1)[:k]
        top = top[np.argsort(-sc[top])]
        floor = float(cfg.get("search.vector_min", 0.0))
        return [(int(ids[i]), float(sc[i])) for i in top if np.isfinite(sc[i]) and sc[i] >= floor]
    except Exception as e:  # 检索不能因为向量一路出错而失败
        _log.warning("向量检索出错，本次只用关键词: %s: %s", type(e).__name__, str(e)[:200])
        return []
