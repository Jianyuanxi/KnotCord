"""server worker 和 drain 共用的干活函数（细则 8：drain 不能 import server，所以放在这里）。

- process_pending：处理 pending 表（SessionEnd/Stop/PreCompact hook 写的），成功入库后才删行（细则 2.3）
- ingest_one：读一个会话的 transcript 增量
- extract_pass：对该提炼的会话做提炼（持有租约的进程才调）
- backfill_sids：transcript 30 分钟没动、还有未提炼内容的会话（细则 1.6）
"""

from __future__ import annotations

import os
import time

from knotcord.core import db, log
from knotcord.extract import backlog, pipeline
from knotcord.ingest import codex_transcript
from knotcord.ingest.claude_transcript import ingest_session

_log = log.get_logger("work")

PENDING_MAX_AGE_S = 24 * 3600  # 一直读失败的 pending 行最多留这么久
BACKFILL_IDLE_S = 30 * 60
KEY_CODEX_SCAN = "codex:scan_at"   # kv：上次扫描 Codex 目录的时间，多个 server 靠它抢"这一轮谁来扫"


def _last_msg_key(sid: str) -> str:
    return f"last_msg:{sid}"


def ingest_one(conn, sid: str, path: str | None, cwd: str | None = None, last: str | None = None,
               client: str | None = None) -> int:
    """读一个会话的增量。数据库或文件读取错误原样抛出，由调用方决定 pending 行留不留。
    client='codex'（参数给，或 sessions 表里记的）走 Codex 的 rollout 解析，其余按 Claude transcript。"""
    r = conn.execute("SELECT transcript_path, client FROM sessions WHERE id=?", (sid,)).fetchone()
    if not path:
        path = r["transcript_path"] if r and r["transcript_path"] else ""
    fn = codex_transcript.ingest_session if (client or (r["client"] if r else None)) == "codex" else ingest_session
    return fn(conn, sid, path or "", cwd=cwd, last_assistant_message=last or None)


def process_pending(conn, *, now: float | None = None) -> tuple[int, set[str]]:
    """处理 pending 表。返回 (新入库的 events 数, 本轮见到 reason='end' 的会话集合)。

    一个会话的所有 pending 行合并成一次读取。成功提交后才删这些行（只删读之前就存在的）；
    失败的行留着下次再试，超过 24 小时还失败就丢弃（transcript 多半已经没了）。
    """
    now = time.time() if now is None else now
    rows = conn.execute("SELECT rowid AS rid, session_id, client, transcript_path, reason, enqueued_at "
                        "FROM pending ORDER BY rowid").fetchall()
    if not rows:
        return 0, set()
    by_sid: dict[str, list] = {}
    for r in rows:
        by_sid.setdefault(r["session_id"], []).append(r)
    total = 0
    ended: set[str] = set()
    for sid, items in by_sid.items():
        path = next((r["transcript_path"] for r in reversed(items) if r["transcript_path"]), None)
        key = _last_msg_key(sid)
        last = db.kv_get(conn, key)
        rids = [r["rid"] for r in items]
        try:
            total += ingest_one(conn, sid, path, None, last,
                                next((r["client"] for r in reversed(items) if r["client"]), None))
        except Exception as e:
            oldest = min((r["enqueued_at"] or now) for r in items)
            if now - oldest > PENDING_MAX_AGE_S:
                _log.warning("pending 行读取失败超过 24 小时，丢弃: session=%s %s", sid[:8], type(e).__name__)
                _delete_pending(conn, rids)
            else:
                _log.warning("pending 读取失败，留待重试: session=%s %s: %s", sid[:8], type(e).__name__,
                             str(e)[:150])
            continue
        _delete_pending(conn, rids, last_msg=(key, last) if last is not None else None)
        if any(r["reason"] == "end" for r in items):
            ended.add(sid)
    return total, ended


def _delete_pending(conn, rids: list[int], last_msg: tuple[str, str] | None = None) -> None:
    with db.write_tx(conn):
        conn.executemany("DELETE FROM pending WHERE rowid=?", [(i,) for i in rids])
        if last_msg:  # 只删读到的那一份；中途有更新的 Stop 写进来就留着
            conn.execute("DELETE FROM kv WHERE key=? AND value=?", last_msg)


# ---------------------------------------------------------------- 扫描 Codex 会话目录

_codex_seen: dict[str, tuple[int, int]] = {}   # 路径 → (mtime_ns, size)，没变的文件不再打开


def codex_sessions_dir(cfg) -> str:
    d = cfg.get("codex.sessions_dir") or ""
    if d:
        return d
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "sessions")


def _claim_codex_scan(conn, now: float, interval: float) -> bool:
    """每个 server 各有一个 worker，同一个间隔内只让一个去扫：先无锁看一眼，到期再在写事务里复查并占位。"""
    def due(v) -> bool:
        try:
            return v is None or not (0 <= now - float(v) < interval)
        except ValueError:
            return True

    if not due(db.kv_get(conn, KEY_CODEX_SCAN)):
        return False
    with db.write_tx(conn):
        if not due(db.kv_get(conn, KEY_CODEX_SCAN)):
            return False
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY_CODEX_SCAN, repr(now)))
    return True


def _recent_rollouts(root: str, horizon: float):
    """(路径, mtime_ns, size)：只看 .jsonl（Codex 把老会话压成 .jsonl.zst，那些不再变），mtime 晚于 horizon。"""
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                continue
            p = os.path.join(dp, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= horizon:
                yield p, st.st_mtime_ns, st.st_size


def scan_codex(conn, cfg, now: float | None = None) -> int:
    """扫 Codex 会话目录里最近改过的 rollout 文件，增量入库。返回新写入的 events 数。
    Codex 目录不存在、codex.enabled=false、本间隔已有别的进程扫过，都立刻返回 0。出错只记日志。"""
    if not cfg.get("codex.enabled", True):
        return 0
    root = codex_sessions_dir(cfg)
    if not os.path.isdir(root):
        return 0
    now = time.time() if now is None else now
    if not _claim_codex_scan(conn, now, float(cfg.get("codex.scan_s", 60))):
        return 0
    horizon = now - float(cfg.get("codex.lookback_days", 2)) * 86400
    total = 0
    seen: dict[str, tuple[int, int]] = {}
    for path, mt, size in _recent_rollouts(root, horizon):
        key = (mt, size)
        seen[path] = key
        if _codex_seen.get(path) == key:
            continue
        try:
            total += codex_transcript.ingest_file(conn, path)
        except Exception as e:  # 一个文件出错不拖垮整轮；不记入 seen，下一轮再试
            seen.pop(path, None)
            _log.warning("扫描 Codex 会话出错 %s: %s: %s", os.path.basename(path), type(e).__name__, str(e)[:150])
    _codex_seen.clear()
    _codex_seen.update(seen)
    return total


def backfill_sids(conn, cfg, now: float | None = None) -> list[str]:
    """有未提炼内容、且 transcript 文件 30 分钟没动过（文件不在就看最后事件时间）的会话。"""
    now = time.time() if now is None else now
    out: list[tuple[float, str]] = []
    for sid, st in backlog.unbatched_stats(conn).items():
        r = conn.execute("SELECT transcript_path, last_seen FROM sessions WHERE id=?", (sid,)).fetchone()
        stamp = max(st["last_ts"], backlog.sec(r["last_seen"]) if r else 0)
        path = r["transcript_path"] if r else None
        if path:
            try:
                stamp = max(stamp, os.stat(path).st_mtime)
            except OSError:
                pass
        if now - stamp >= BACKFILL_IDLE_S:
            out.append((st["last_ts"], sid))
    return [sid for _, sid in sorted(out)]


def extract_pass(conn, cfg, lease, stop_event, *, flush_sids=(), backfill: bool = False) -> int:
    """提炼一轮：先做 flush_sids（会话结束的，全部提炼），再做到期的，backfill=True 时再补历史积压。
    返回完成的批数。租约不是自己的、stop_event 置位就停。"""
    done = 0
    seen: set[str] = set()

    def run(sid: str, flush: bool) -> None:
        nonlocal done
        if sid in seen or stop_event.is_set() or not lease.is_mine():
            return
        seen.add(sid)
        try:
            done += pipeline.run_session(conn, sid, cfg, lease, flush=flush, stop_event=stop_event)
        except Exception as e:  # 一个会话出错不拖垮整轮
            _log.warning("提炼会话 %s 出错: %s: %s", sid[:8], type(e).__name__, str(e)[:200])

    for sid in flush_sids:
        run(sid, True)
    for sid in pipeline.due_sessions(conn, cfg):
        run(sid, False)
    if backfill:
        for sid in backfill_sids(conn, cfg):
            run(sid, True)
    return done


# ---------------------------------------------------------------- 补向量（search/vectors.py 做实事）

_embed_fail_until = 0.0
_embed_next = 0.0  # 上一轮没有活干时，歇这么久再查（缺向量的查询要扫 docs，不必每秒一次）
EMBED_FAIL_BACKOFF_S = 300
EMBED_IDLE_S = 30


def embed_due(conn, cfg) -> bool:
    """有缺向量的文档、且模型和依赖可用：没拿到租约的 server 据此去抢（和提炼积压、清理到期同级）。"""
    if not cfg.get("search.vector", True) or time.time() < _embed_fail_until:
        return False
    try:
        from knotcord.search import embed, vectors
        return embed.model_present() and vectors.has_missing(conn)
    except Exception:
        return False


def embed_pass(conn, cfg, lease, stop_event) -> tuple[int, bool]:
    """给缺向量的文档补一轮，返回 (本轮条数, 后面是否还有)。任何异常只记日志并歇一阵，不影响 worker 的其他活。"""
    global _embed_fail_until, _embed_next
    if time.time() < max(_embed_fail_until, _embed_next):
        return 0, False
    try:
        from knotcord.search import vectors
        n, more = vectors.backfill(conn, cfg, lease, stop_event)
        if not more:
            _embed_next = time.time() + EMBED_IDLE_S
        if n:
            _log.info("补向量 %d 条%s", n, "，还有" if more else "，已补完")
        return n, more
    except Exception as e:
        _embed_fail_until = time.time() + EMBED_FAIL_BACKOFF_S
        _log.warning("补向量出错，%d 秒后再试: %s: %s", EMBED_FAIL_BACKOFF_S, type(e).__name__, str(e)[:200])
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        return 0, False
