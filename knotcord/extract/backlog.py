"""待提炼内容的统计：events 里 batch_id 为空的就是没提炼的（细则 6.4：状态存成事实）。

只算本机 KnotCord 自己入库的事件（origin='knotcord'）：从 claude-mem 迁移来的旧提示已经有旧总结，不再提炼。
"""

from __future__ import annotations

import math
import sqlite3

# 字数估算（和 prompt.event_text 的取字规则一致）：提示/回答取 body，没有就取 brief；工具取 工具名+brief+body
_CHARS_SQL = (
    "CASE WHEN kind='tool' "
    "THEN length(coalesce(tool,'')) + length(coalesce(brief,'')) + length(coalesce(body,'')) + 16 "
    "ELSE length(coalesce(nullif(body,''), brief, '')) + 16 END"
)


def sec(ts) -> int:
    """时间戳统一成秒（有的旧数据是毫秒）。"""
    try:
        v = int(ts or 0)
    except (TypeError, ValueError):
        return 0
    return v // 1000 if v > 100_000_000_000 else v


def unbatched_stats(conn: sqlite3.Connection, session_id: str | None = None) -> dict[str, dict]:
    """{会话id: {n, chars, turns, last_ts}}，只含有未提炼事件的会话。turns 是 kind='prompt' 的条数。"""
    sql = (f"SELECT session_id, count(*) AS n, sum({_CHARS_SQL}) AS chars, "
           "sum(CASE WHEN kind='prompt' THEN 1 ELSE 0 END) AS turns, max(ts) AS last_ts "
           "FROM events WHERE batch_id IS NULL AND session_id IS NOT NULL "
           "AND coalesce(origin,'knotcord')='knotcord'")
    args: tuple = ()
    if session_id is not None:
        sql += " AND session_id=?"
        args = (session_id,)
    sql += " GROUP BY session_id"
    return {r["session_id"]: {"n": r["n"], "chars": r["chars"] or 0, "turns": r["turns"] or 0,
                              "last_ts": sec(r["last_ts"])}
            for r in conn.execute(sql, args)}


def retry_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int | None]]]:
    """{会话id: [(状态, next_try)]}：failed 行（等重试）和 running 行（进程死在调模型途中留下的）。"""
    out: dict[str, list[tuple[str, int | None]]] = {}
    for r in conn.execute("SELECT session_id, status, next_try FROM batches "
                          "WHERE status IN ('failed','running') AND session_id IS NOT NULL"):
        out.setdefault(r["session_id"], []).append((r["status"], r["next_try"]))
    return out


def retry_state(rows: list[tuple[str, int | None]] | None, now: float) -> str | None:
    """None=没有重试行；'due'=可以重试；'wait'=还在退避期。running 行在持有租约的进程看来一定是陈旧的。"""
    if not rows:
        return None
    for status, nxt in rows:
        if status == "running" or not nxt or nxt <= now:
            return "due"
    return "wait"


def backlog_batches(conn: sqlite3.Connection, cfg) -> int:
    """积压批数：每个有未提炼内容的会话按 max_chars 折算，至少 1 批。"""
    max_chars = max(1, int(cfg.get("extract.max_chars")))
    return sum(max(1, math.ceil(s["chars"] / max_chars)) for s in unbatched_stats(conn).values())
