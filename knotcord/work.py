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
from knotcord.ingest.claude_transcript import ingest_session

_log = log.get_logger("work")

PENDING_MAX_AGE_S = 24 * 3600  # 一直读失败的 pending 行最多留这么久
BACKFILL_IDLE_S = 30 * 60


def _last_msg_key(sid: str) -> str:
    return f"last_msg:{sid}"


def ingest_one(conn, sid: str, path: str | None, cwd: str | None = None, last: str | None = None) -> int:
    """读一个会话的增量。数据库或文件读取错误原样抛出，由调用方决定 pending 行留不留。"""
    if not path:
        r = conn.execute("SELECT transcript_path FROM sessions WHERE id=?", (sid,)).fetchone()
        path = r["transcript_path"] if r and r["transcript_path"] else ""
    return ingest_session(conn, sid, path or "", cwd=cwd, last_assistant_message=last or None)


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
            total += ingest_one(conn, sid, path, None, last)
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
