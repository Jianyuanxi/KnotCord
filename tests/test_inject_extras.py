"""开局注入的两处补充：库损坏提示（细则 5.3）和晋升提名一行提示（每周整理写的 kv）。"""

import json
import time

from knotcord import inject
from knotcord.core import db, tokens

P = "proj"


def _cfg(trace_home, text):
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text(text, encoding="utf-8")


def _seed(conn):
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (P, int(time.time()), "decision", "选 sqlite 做存储", "b"))
    conn.commit()
    return inject.render(conn, P)


def test_corrupt_notice_is_first_content_line(conn):
    text = _seed(conn)
    db.kv_set(conn, "db_corrupt", "数据库损坏，已停止记录，备份在 C:\\k\\backups\\knotcord.db（quick_check: *** Page 5 ***）")
    out = inject.read_for_hook(conn, P)
    lines = out.split("\n")
    assert lines[0].startswith("<knotcord-context")
    assert lines[1] == "! 数据库损坏，已停止记录，备份在 C:\\k\\backups\\knotcord.db"
    assert lines[2] == inject.HEADER
    assert "quick_check" not in out and out.endswith("</knotcord-context>")
    assert "选 sqlite 做存储" in out and len(out) > len(text)


def test_corrupt_notice_even_when_nothing_else_to_inject(conn):
    db.kv_set(conn, "db_corrupt", "数据库损坏，已停止记录，备份在 X")
    out = inject.read_for_hook(conn, P)
    assert out.split("\n")[1] == "! 数据库损坏，已停止记录，备份在 X" and out.endswith("</knotcord-context>")


def test_no_corrupt_flag_means_no_notice(conn):
    text = _seed(conn)
    assert inject.read_for_hook(conn, P) == text


def test_corrupt_notice_ignores_budget(conn, trace_home):
    _seed(conn)
    _cfg(trace_home, "[inject]\nbudget_tokens = 5\nhard_chars = 100\n")
    db.kv_set(conn, "db_corrupt", "数据库损坏，已停止记录，备份在 X")
    assert "数据库损坏，已停止记录，备份在 X" in inject.read_for_hook(conn, P)


def _promote(conn, n, project=P):
    items = [{"id": 100 + i, "title": f"第{i}条常被取用的笔记标题" + "很长" * 30, "uses": 7} for i in range(n)]
    db.kv_set(conn, inject.PROMOTE_PREFIX + project, json.dumps(items, ensure_ascii=False))


def test_promote_hint_line(conn):
    text = _seed(conn)
    _promote(conn, 4)
    out = inject.read_for_hook(conn, P)
    hint = [ln for ln in out.split("\n") if "被反复取用" in ln]
    assert len(hint) == 1
    assert hint[0].startswith("有 4 条笔记被反复取用，可考虑写进记忆文件：n100 第0条")
    assert "n102" in hint[0] and "n103" not in hint[0]  # 只列前 3 条
    assert len(hint[0]) < 200  # 标题被截断
    assert out.startswith(text[:-len("</knotcord-context>")]) and out.endswith("</knotcord-context>")


def test_promote_hint_is_per_project_and_survives_bad_json(conn):
    text = _seed(conn)
    _promote(conn, 2, project="other")
    assert inject.read_for_hook(conn, P) == text
    db.kv_set(conn, inject.PROMOTE_PREFIX + P, "{不是 json")
    assert inject.read_for_hook(conn, P) == text
    db.kv_set(conn, inject.PROMOTE_PREFIX + P, "[]")
    assert inject.read_for_hook(conn, P) == text


def test_promote_hint_bound_by_budget(conn, trace_home):
    text = _seed(conn)
    _promote(conn, 3)
    _cfg(trace_home, f"[inject]\nbudget_tokens = {tokens.estimate(text) + 3}\n")
    assert inject.read_for_hook(conn, P) == text  # 放不下就不加，其余内容照旧
