"""清理任务（细则 5.3）。只有持有租约的进程调用（worker 启动 60 秒后、之后每 6 小时、drain 退出前）。

每次都做：扫临时目录（6 小时以上）、轮转日志（14 天 / 50 MB）、截断 WAL、PRAGMA optimize、清死进程的登记行。
每周一次：quick_check、备份（只留最新一份）、FTS optimize、incremental_vacuum、
events 旧工具正文清空（180 天）、内置记忆文件重建索引。
上次运行时间记在 kv：janitor:last、janitor:weekly；不到间隔就跳过。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from knotcord import procs
from knotcord.core import db, log, paths

_log = log.get_logger("janitor")

TMP_MAX_AGE_S = 6 * 3600
WEEK_S = 7 * 86400
KEY_LAST = "janitor:last"
KEY_WEEKLY = "janitor:weekly"
KEY_CORRUPT = "db_corrupt"
_LOG_RE = re.compile(r"^knotcord-(\d{4}-\d{2}-\d{2})\.log$")


def _kv_float(conn, key: str) -> float:
    try:
        return float(db.kv_get(conn, key, "0") or 0)
    except ValueError:
        return 0.0


def due(conn, cfg, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - _kv_float(conn, KEY_LAST) >= float(cfg.get("janitor.hours")) * 3600


def weekly_due(conn, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - _kv_float(conn, KEY_WEEKLY) >= WEEK_S


# ---------------------------------------------------------------- 每次都做

def _newest_mtime(p: Path) -> float:
    try:
        m = p.stat().st_mtime
    except OSError:
        return 0.0
    if p.is_dir():
        for child in p.iterdir():  # 浅扫一层：批目录里的文件才是最近写过的
            try:
                m = max(m, child.stat().st_mtime)
            except OSError:
                pass
    return m


def sweep_tmp(now: float, max_age_s: float = TMP_MAX_AGE_S) -> int:
    """删 tmp 下超过 max_age_s 没动过的文件和目录。返回删除个数。"""
    d = paths.tmp_dir()
    n = 0
    for p in d.iterdir():
        if now - _newest_mtime(p) < max_age_s:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            n += 1
        except OSError as e:
            _log.warning("清理临时项失败 %s: %s", p.name, e)
    return n


def rotate_logs(now: float, days: int, mb: int) -> int:
    """删超过 days 天的日志；剩下的总量超过 mb 就从最旧的删起（当天的不删）。返回删除个数。"""
    d = paths.logs_dir()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    files: list[tuple[float, Path, int]] = []
    removed = 0
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _LOG_RE.match(p.name)
        try:
            st = p.stat()
        except OSError:
            continue
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
            except ValueError:
                ts = st.st_mtime
        else:
            ts = st.st_mtime
        if now - ts > days * 86400 and (not m or m.group(1) != today):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
            continue
        files.append((ts, p, st.st_size))
    total = sum(s for _, _, s in files)
    limit = mb * 1024 * 1024
    for ts, p, size in sorted(files, key=lambda x: x[0]):
        if total <= limit:
            break
        if _LOG_RE.match(p.name) and _LOG_RE.match(p.name).group(1) == today:
            continue
        try:
            p.unlink()
            removed += 1
            total -= size
        except OSError:
            pass
    return removed


def truncate_wal(conn) -> bool:
    """wal_checkpoint(TRUNCATE)；有别的连接在读时可能 busy，返回是否成功截断。"""
    try:
        r = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return bool(r) and r[0] == 0
    except sqlite3.Error as e:
        _log.warning("wal_checkpoint 失败: %s", e)
        return False


# ---------------------------------------------------------------- 每周

def quick_check(conn) -> str:
    """返回 'ok' 或第一条问题。locked/busy 是暂时性错误，不当成损坏：抛出去由调用方跳过。"""
    row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "ok"


def backup(conn) -> Path | None:
    """VACUUM INTO 一份到 tmp，校验通过再替换 backups/knotcord.db（只留最新一份）。失败返回 None。"""
    dst = paths.backups_dir() / "knotcord.db"
    tmp = paths.tmp_dir() / f"knotcord.db.backup-{os.getpid()}"
    try:
        if tmp.exists():
            tmp.unlink()
        conn.execute("VACUUM INTO ?", (str(tmp),))
        chk = sqlite3.connect(str(tmp))
        try:
            res = chk.execute("PRAGMA quick_check").fetchone()
        finally:
            chk.close()
        if not res or res[0] != "ok":
            _log.error("备份校验未通过，保留旧备份: %s", res)
            return None
        os.replace(tmp, dst)
        return dst
    except (sqlite3.Error, OSError) as e:
        _log.warning("备份失败: %s: %s", type(e).__name__, e)
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def fts_optimize(conn) -> None:
    with db.write_tx(conn):
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")


def incremental_vacuum(conn) -> None:
    conn.execute("PRAGMA incremental_vacuum").fetchall()


def clear_old_event_bodies(conn, now: float, days: int, chunk: int = 2000) -> int:
    """已提炼过的工具记录，超过 days 天就清空正文（brief 和 src_line 留着）。分块提交，写事务保持短。"""
    cutoff = int(now - days * 86400)
    total = 0
    sql = ("UPDATE events SET body='' WHERE id IN ("
           "SELECT id FROM events WHERE kind='tool' AND body<>'' AND batch_id IS NOT NULL "
           "AND (CASE WHEN ts>100000000000 THEN ts/1000 ELSE ts END) < ? LIMIT ?)")
    while True:
        with db.write_tx(conn):
            n = conn.execute(sql, (cutoff, chunk)).rowcount
        total += n
        if n < chunk:
            return total


# ---------------------------------------------------------------- 入口

def run(conn, cfg, *, now: float | None = None, force: bool = False, memroot: str | None = None) -> dict:
    """跑一次清理。没到间隔且 force=False 返回 {}。返回各项结果的字典。

    memroot：内置记忆文件的根目录，默认 ~/.claude/projects（只读）；测试里指到临时目录。
    """
    now = time.time() if now is None else now
    if not force and not due(conn, cfg, now):
        return {}
    res: dict = {}
    res["tmp_removed"] = sweep_tmp(now)
    res["logs_removed"] = rotate_logs(now, int(cfg.get("retain.log_days")), int(cfg.get("retain.log_mb")))
    res["procs_pruned"] = procs.prune_dead(conn)
    if force or weekly_due(conn, now):
        res.update(_weekly(conn, cfg, now, memroot))
    res["wal_truncated"] = truncate_wal(conn)
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    db.kv_set(conn, KEY_LAST, int(now))
    _log.info("清理完成: %s", res)
    return res


def _weekly(conn, cfg, now: float, memroot: str | None) -> dict:
    res: dict = {}
    try:
        check = quick_check(conn)
    except sqlite3.OperationalError as e:  # 锁冲突之类：不是损坏，这周先不做，明天（下次清理）再试
        _log.warning("quick_check 遇到暂时性错误，本次跳过每周项: %s", e)
        return {"weekly_skipped": str(e)[:100]}
    res["quick_check"] = check
    if check != "ok":
        bak = paths.backups_dir() / "knotcord.db"
        msg = f"数据库损坏，已停止记录，备份在 {bak}（quick_check: {check[:200]}）"
        db.kv_set(conn, KEY_CORRUPT, msg)
        _log.error(msg)
        return res  # 损坏了就别再写别的、也别用损坏的库覆盖旧备份
    # 能走到这里说明库是好的：清掉过去可能留下的损坏标记
    if db.kv_get(conn, KEY_CORRUPT):
        with db.write_tx(conn):
            conn.execute("DELETE FROM kv WHERE key=?", (KEY_CORRUPT,))
    res["backup"] = str(p) if (p := backup(conn)) else None
    fts_optimize(conn)
    incremental_vacuum(conn)
    res["event_bodies_cleared"] = clear_old_event_bodies(conn, now, int(cfg.get("retain.event_body_days")))
    from knotcord.search import memfiles
    res["memfiles_updated"] = memfiles.reindex(conn, root=memroot)
    db.kv_set(conn, KEY_WEEKLY, int(now))
    return res
