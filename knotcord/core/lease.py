"""租约（细则 1.1）：同一时刻只有一个进程做提炼、嵌入、清理。

kv 里一行 key='lease'，value 是 JSON {"owner":..., "expires":...}。
抢和续都是一条带条件的 UPDATE，改到 1 行才算数。续租由干活的循环调用，不另开线程。
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time

from . import db as _db

KEY = "lease"
_PROC_START = int(time.time() * 1000)  # 本进程 import 时刻，近似进程启动时间


class LeaseLost(Exception):
    """落库时发现租约已不是自己的。"""


def _now() -> float:  # 测试里可替换
    return time.time()


def default_owner() -> str:
    return f"{os.getpid()}:{_PROC_START}:{secrets.token_hex(4)}"


def _payload(owner: str, expires: float) -> str:
    return json.dumps({"owner": owner, "expires": expires})


def _read(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return None
    try:
        d = json.loads(row[0])
        return {"owner": str(d.get("owner", "")), "expires": float(d.get("expires", 0))}
    except (ValueError, TypeError):
        return None  # 内容坏了当作没人持有，下次抢会覆盖


def holder_info(conn: sqlite3.Connection) -> dict | None:
    """当前持有者：owner、expires（epoch 秒）、expired（是否已过期）。没人持有返回 None。"""
    d = _read(conn)
    if d is None or not d["owner"]:
        return None
    d["expired"] = d["expires"] < _now()
    return d


class Lease:
    def __init__(self, conn: sqlite3.Connection, owner: str | None = None, ttl_s: float = 90) -> None:
        self.conn = conn
        self.owner = owner or default_owner()
        self.ttl_s = ttl_s

    def try_acquire(self) -> bool:
        now = _now()
        with _db.write_tx(self.conn):
            if _read(self.conn) is None:  # 没有这一行，或内容坏了（json_extract 会报错）：重置
                self.conn.execute("DELETE FROM kv WHERE key=?", (KEY,))
            self.conn.execute("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)",
                              (KEY, _payload("", 0)))
            cur = self.conn.execute(
                "UPDATE kv SET value=? WHERE key=? AND "
                "(json_extract(value,'$.expires') < ? OR json_extract(value,'$.owner') = ?)",
                (_payload(self.owner, now + self.ttl_s), KEY, now, self.owner),
            )
            return cur.rowcount == 1

    def renew(self) -> bool:
        """续租。发现不是自己的返回 False。"""
        with _db.write_tx(self.conn):
            cur = self.conn.execute(
                "UPDATE kv SET value=? WHERE key=? AND json_extract(value,'$.owner') = ?",
                (_payload(self.owner, _now() + self.ttl_s), KEY, self.owner),
            )
            return cur.rowcount == 1

    def release(self) -> None:
        with _db.write_tx(self.conn):
            self.conn.execute(
                "UPDATE kv SET value=? WHERE key=? AND json_extract(value,'$.owner') = ?",
                (_payload("", 0), KEY, self.owner),
            )

    def is_mine(self) -> bool:
        """持有者是我（即使已过期，只要没被别人接手仍算我的）。"""
        d = _read(self.conn)
        return d is not None and d["owner"] == self.owner

    def assert_mine_in_tx(self) -> None:
        """落库事务的第一句调用。事务里已持有写锁，这里读到的就是提交时的真相。"""
        if not self.is_mine():
            raise LeaseLost(f"租约已不属于 {self.owner}")
