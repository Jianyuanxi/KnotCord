"""数据库：连接参数、建表和升级、写事务、kv。

整个项目只有这里能写 sqlite3.connect。规矩见实现细则第 3 节。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from . import paths

SCHEMA_VERSION = 1

# locked/busy 的退避：0.2、1、5 秒（细则第 3 节）。这些都是暂时性错误，绝不当成损坏
_BACKOFF = (0.2, 1.0, 5.0)
_TRANSIENT = ("locked", "busy", "unable to open")


def _is_transient(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in _TRANSIENT)


def _retry(fn):
    for delay in (*_BACKOFF, None):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if delay is None or not _is_transient(e):
                raise
            time.sleep(delay)


# ---------------------------------------------------------------- 表结构（方案书附录 A + 11-15 修订）

_TABLES = [
    """CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, client TEXT, project TEXT, cwd TEXT, transcript_path TEXT,
        read_offset INTEGER DEFAULT 0, started_at INTEGER, last_seen INTEGER,
        legacy_memory_session_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, session_id TEXT, project TEXT, ts INTEGER, uuid TEXT,
        kind TEXT CHECK(kind IN ('prompt','assistant','tool')), tool TEXT, brief TEXT, body TEXT,
        src_line INTEGER, batch_id INTEGER, origin TEXT DEFAULT 'knotcord', legacy_id INTEGER,
        UNIQUE(uuid))""",
    """CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY, project TEXT, session_id TEXT, ts INTEGER, type TEXT, title TEXT, body TEXT,
        facts TEXT, files TEXT, tags TEXT, slot TEXT,
        state TEXT DEFAULT 'active' CHECK(state IN ('active','superseded','resolved')),
        superseded_by INTEGER, superseded_at INTEGER, source_events TEXT, model TEXT,
        origin TEXT DEFAULT 'knotcord', legacy_id INTEGER, content_hash TEXT,
        last_mentioned INTEGER, use_count INTEGER DEFAULT 0, last_used INTEGER)""",
    """CREATE TABLE IF NOT EXISTS state_cards(
        id INTEGER PRIMARY KEY, project TEXT, ts INTEGER, batch_id INTEGER, card TEXT)""",
    """CREATE TABLE IF NOT EXISTS mem_flags(
        file TEXT PRIMARY KEY, project TEXT, why TEXT, flagged_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS summaries(
        id INTEGER PRIMARY KEY, session_id TEXT, project TEXT, ts INTEGER, request TEXT, completed TEXT,
        learned TEXT, next_steps TEXT, extra TEXT, origin TEXT DEFAULT 'knotcord', legacy_id INTEGER)""",
    # docs 的 UNIQUE(src, src_id) 用唯一索引实现（见 _INDEXES），老库也能补上
    """CREATE TABLE IF NOT EXISTS docs(
        id INTEGER PRIMARY KEY, src TEXT NOT NULL, src_id INTEGER NOT NULL, project TEXT, ts INTEGER,
        active INTEGER DEFAULT 1, use_count INTEGER DEFAULT 0, last_used INTEGER)""",
    "CREATE TABLE IF NOT EXISTS docs_vec(doc_id INTEGER PRIMARY KEY, model TEXT, vec BLOB)",
    """CREATE TABLE IF NOT EXISTS batches(
        id INTEGER PRIMARY KEY, session_id TEXT, created INTEGER,
        status TEXT DEFAULT 'running' CHECK(status IN ('running','done','failed','dead')),
        provider TEXT, chars_in INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_cny REAL, error TEXT,
        attempts INTEGER DEFAULT 0, next_try INTEGER)""",
    """CREATE TABLE IF NOT EXISTS pending(
        session_id TEXT, client TEXT, transcript_path TEXT, reason TEXT, enqueued_at INTEGER)""",
    "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)",
    # 内置记忆文件的元数据；正文不存，docs(src='memfile', src_id=memfiles.id) 只建索引
    """CREATE TABLE IF NOT EXISTS memfiles(
        id INTEGER PRIMARY KEY, path TEXT UNIQUE, project TEXT, mtime INTEGER,
        name TEXT, description TEXT)""",
]

# 之后修订才加的列：老库建过表时用 PRAGMA table_info 看真实列，缺了就补
_LATE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [("last_seen", "INTEGER"), ("legacy_memory_session_id", "TEXT")],
    "notes": [("last_mentioned", "INTEGER"), ("use_count", "INTEGER DEFAULT 0"), ("last_used", "INTEGER")],
    "batches": [
        ("status", "TEXT DEFAULT 'running' CHECK(status IN ('running','done','failed','dead'))"),
        ("attempts", "INTEGER DEFAULT 0"),
        ("next_try", "INTEGER"),
    ],
    "docs": [("use_count", "INTEGER DEFAULT 0"), ("last_used", "INTEGER")],
}

# 先升级、后建索引：索引引用的列都已确保存在
_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS docs_src_uq ON docs(src, src_id)",
    "CREATE INDEX IF NOT EXISTS notes_proj_state_ts ON notes(project, state, ts DESC)",
    "CREATE INDEX IF NOT EXISTS notes_proj_slot ON notes(project, slot) WHERE slot IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS notes_proj_hash ON notes(project, content_hash) WHERE content_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS summaries_proj_ts ON summaries(project, ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_session_ts ON events(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS events_unbatched ON events(session_id) WHERE batch_id IS NULL",
    "CREATE INDEX IF NOT EXISTS batches_status ON batches(status)",
    "CREATE INDEX IF NOT EXISTS state_cards_proj_ts ON state_cards(project, ts DESC)",
    "CREATE INDEX IF NOT EXISTS pending_session ON pending(session_id)",
]

# contentless FTS5：不另存正文，rowid 等于 docs.id；contentless_delete 需要 SQLite >= 3.43
_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
        "seg, content='', contentless_delete=1, tokenize='unicode61')")


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _LATE_COLUMNS.items():
        have = set(table_columns(conn, table))
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for ddl in _TABLES:
        conn.execute(ddl)
    _ensure_columns(conn)
    for ddl in _INDEXES:
        conn.execute(ddl)
    conn.execute(_FTS)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """库版本低于代码时，在 BEGIN EXCLUSIVE 里升级。库版本更高就不动。"""
    if _user_version(conn) >= SCHEMA_VERSION:
        return
    _retry(lambda: conn.execute("BEGIN EXCLUSIVE"))
    try:
        ver = _user_version(conn)  # 拿到锁后再看一遍：别的进程可能刚升完
        if ver < SCHEMA_VERSION:
            if ver < 1:
                _migrate_v1(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def is_newer_than_code(conn: sqlite3.Connection) -> bool:
    return _user_version(conn) > SCHEMA_VERSION


# ---------------------------------------------------------------- 连接和事务

def connect(*, readonly: bool = False, busy_timeout_ms: int = 5000, retry: bool = True) -> sqlite3.Connection:
    path = paths.db_path()
    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"数据库不存在: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        else:
            # auto_vacuum 只对空库有效，且必须在切 WAL（写文件头）之前设；已有的库要靠 VACUUM 才能改
            if _user_version(conn) == 0 and conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            if retry:
                _retry(lambda: conn.execute("PRAGMA journal_mode=WAL").fetchall())
            else:  # hook 用：撞锁只等 busy_timeout 一次
                conn.execute("PRAGMA journal_mode=WAL").fetchall()
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=67108864")
            if not is_newer_than_code(conn):
                ensure_schema(conn)
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection, *, retry: bool = True):
    """BEGIN IMMEDIATE；locked/busy 按 0.2、1、5 秒退避；异常回滚。已在事务里就并入外层。

    retry=False：只等 busy_timeout 一次就抛，给有 1 秒硬期限的 hook 用。
    """
    if conn.in_transaction:
        yield conn
        return
    if retry:
        _retry(lambda: conn.execute("BEGIN IMMEDIATE"))
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    """value 存 TEXT，结构化数据调用方自己 json.dumps。"""
    if not isinstance(value, str):
        value = str(value)
    with write_tx(conn):
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
