"""inject.render / read_for_hook：块顺序、整块跳过、预算、过时标记、第 7 项。"""

import json
import os
import time

from knotcord import inject
from knotcord.core import db, tokens

P = "proj"
DAY = 86400


def _cfg(trace_home, text: str):
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text(text, encoding="utf-8")


def _card(conn, card: dict, project=P, ts=None):
    conn.execute("INSERT INTO state_cards(project, ts, card) VALUES(?,?,?)",
                 (project, ts or int(time.time()), json.dumps(card, ensure_ascii=False)))
    conn.commit()


def _note(conn, type_, title, *, project=P, ts=None, state="active", tags="", last_mentioned=None):
    cur = conn.execute(
        "INSERT INTO notes(project, ts, type, title, body, state, tags, last_mentioned) VALUES(?,?,?,?,?,?,?,?)",
        (project, ts or int(time.time()), type_, title, title, state, tags, last_mentioned))
    conn.commit()
    return cur.lastrowid


def _summary(conn, sid, request, completed, *, project=P, ts=None):
    cur = conn.execute("INSERT INTO summaries(session_id, project, ts, request, completed) VALUES(?,?,?,?,?)",
                       (sid, project, ts or int(time.time()), request, completed))
    conn.commit()
    return cur.lastrowid


def _session(conn, sid, *, project=P, last_seen=None):
    conn.execute("INSERT INTO sessions(id, client, project, last_seen, started_at) VALUES(?,?,?,?,?)",
                 (sid, "claude", project, last_seen or int(time.time()), last_seen or int(time.time())))
    conn.commit()


def _event(conn, sid, kind, body, *, brief=None, tool=None, batch_id=None, uuid=None, ts=None):
    conn.execute(
        "INSERT INTO events(session_id, project, ts, uuid, kind, tool, brief, body, batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, P, ts or int(time.time()), uuid or f"{sid}-{kind}-{body[:6]}-{time.time_ns()}", kind, tool, brief, body, batch_id))
    conn.commit()


def _fill_all(conn, tmp_path):
    _card(conn, {"goal": "做完记忆插件", "status": "核心层完成", "decisions": ["用汉字二元组"],
                 "pitfalls": ["936 乱码"], "next": ["写注入"]})
    _note(conn, "unfinished", "迁移脚本没写")
    f = tmp_path / "mem.md"
    f.write_text("x", encoding="utf-8")
    conn.execute("INSERT INTO memfiles(path, name, mtime) VALUES(?,?,?)", (str(f), "mem.md", 1))
    conn.execute("INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?)",
                 ("mem.md", P, "里面写的端口已改", int(time.time()) + 3600))
    conn.commit()
    _summary(conn, "s1", "修登录 bug。顺便看日志", "改了 token 刷新。测试通过")
    _note(conn, "decision", "选 sqlite 做存储")
    return f


def _headings(text):
    return [ln for ln in text.splitlines() if ln.startswith("## ")]


def test_all_blocks_in_order_and_wrapped(conn, tmp_path):
    _fill_all(conn, tmp_path)
    text = inject.render(conn, P)
    assert text.startswith(f'<knotcord-context project="{P}" at="')
    assert text.endswith("</knotcord-context>")
    assert inject.HEADER in text
    assert _headings(text) == ["## 现状", "## 没做完", "## 记忆文件可能过时", "## 最近会话", "## 要点"]
    assert "目标：做完记忆插件" in text and "下一步：写注入" in text
    assert "迁移脚本没写" in text
    assert "mem.md：里面写的端口已改" in text
    assert "修登录 bug → 改了 token 刷新" in text  # 只取第一句
    assert "顺便看日志" not in text
    assert "decision 选 sqlite 做存储" in text
    assert db.kv_get(conn, f"inject:{P}") == text


def test_empty_project_returns_empty_and_kv_empty(conn):
    assert inject.render(conn, "nothing") == ""
    assert db.kv_get(conn, "inject:nothing") == ""
    _note(conn, "decision", "别的项目的", project="other")
    assert inject.render(conn, "nothing") == ""


def test_other_projects_and_inactive_notes_excluded(conn):
    _note(conn, "decision", "旧决定", state="superseded")
    _note(conn, "decision", "老决定", ts=int(time.time()) - 9 * DAY)  # 超过 7 天
    _note(conn, "feature", "普通功能")  # 不是 decision/bugfix/gotcha
    _note(conn, "feature", "有坑", tags="gotcha,win")
    text = inject.render(conn, P)
    assert "有坑" in text
    assert "旧决定" not in text and "老决定" not in text and "普通功能" not in text


def test_over_budget_block_skipped_whole_later_blocks_still_added(conn, trace_home, tmp_path):
    _fill_all(conn, tmp_path)
    for i in range(8):
        _note(conn, "unfinished", f"没做完的事{i}" + "很长的描述" * 20)
    full = inject.render(conn, P)
    assert "## 没做完" in full
    parts = full.split("\n## ")
    without_unf = "\n## ".join(p for p in parts if not p.startswith("没做完"))
    budget = tokens.estimate(without_unf) + 3
    assert budget < tokens.estimate(full)
    _cfg(trace_home, f"[inject]\nbudget_tokens = {budget}\n")
    text = inject.render(conn, P)
    heads = _headings(text)
    assert "## 没做完" not in heads
    assert heads == ["## 现状", "## 记忆文件可能过时", "## 最近会话", "## 要点"]
    assert tokens.estimate(text) <= budget


def test_hard_chars_cap(conn, trace_home, tmp_path):
    _fill_all(conn, tmp_path)
    _cfg(trace_home, "[inject]\nbudget_tokens = 100000\nhard_chars = 260\n")
    text = inject.render(conn, P)
    assert len(text) <= 260
    assert text.startswith("<knotcord-context")
    assert text.endswith("</knotcord-context>")


def test_unfinished_days_and_max(conn, trace_home):
    now = int(time.time())
    _note(conn, "unfinished", "新的", ts=now - DAY)
    _note(conn, "unfinished", "十天前提到过", ts=now - 30 * DAY, last_mentioned=now - 10 * DAY)
    _note(conn, "unfinished", "二十天前", ts=now - 20 * DAY)
    _note(conn, "unfinished", "已解决", ts=now - DAY, state="resolved")
    text = inject.render(conn, P)
    assert "新的" in text and "十天前提到过" in text
    assert "二十天前" not in text and "已解决" not in text
    assert text.index("新的") < text.index("十天前提到过")  # 最近提到的在前

    _cfg(trace_home, "[inject]\nunfinished_max = 1\nunfinished_days = 3\n")
    text = inject.render(conn, P)
    assert "新的" in text and "十天前提到过" not in text


def test_card_over_token_cap_trims_list_tails_then_drops(conn, trace_home):
    _card(conn, {"goal": "目标", "decisions": [f"决定{i}" + "字" * 80 for i in range(10)], "next": ["下一步"]})
    _cfg(trace_home, "[inject]\ncard_tokens = 200\n")
    text = inject.render(conn, P)
    assert "目标：目标" in text
    assert tokens.estimate(text.split("## 现状\n", 1)[1]) <= 220
    assert "决定9" not in text
    _card(conn, {"goal": "字" * 1000}, project="p2")
    _cfg(trace_home, "[inject]\ncard_tokens = 50\n")
    assert inject.render(conn, "p2") == ""  # 砍到头也装不下：整块丢


def test_bad_card_json_skipped(conn):
    conn.execute("INSERT INTO state_cards(project, ts, card) VALUES(?,?,?)", (P, int(time.time()), "{坏"))
    conn.commit()
    _note(conn, "decision", "还有别的")
    text = inject.render(conn, P)
    assert "## 现状" not in text and "还有别的" in text


def test_stale_flag_disappears_when_file_modified_after_flag(conn, tmp_path):
    f = tmp_path / "feedback.md"
    f.write_text("a", encoding="utf-8")
    conn.execute("INSERT INTO memfiles(path, name, mtime) VALUES(?,?,?)", (str(f), "feedback.md", 1))
    now = int(time.time())
    conn.execute("INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?)",
                 ("feedback.md", P, "过时了", now + 100))
    conn.commit()
    assert "过时了" in inject.render(conn, P)
    assert conn.execute("SELECT count(*) FROM mem_flags").fetchone()[0] == 1
    os.utime(f, (now + 500, now + 500))  # 标记之后文件被改过
    text = inject.render(conn, P)
    assert text == ""
    assert conn.execute("SELECT count(*) FROM mem_flags").fetchone()[0] == 0


def test_flag_for_deleted_file_dropped_and_unresolvable_kept(conn, tmp_path):
    gone = tmp_path / "gone.md"
    conn.execute("INSERT INTO memfiles(path, name, mtime) VALUES(?,?,?)", (str(gone), "gone.md", 1))
    conn.execute("INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?)", ("gone.md", P, "x", 5))
    conn.execute("INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?)", ("unknown.md", P, "查不到路径", 5))
    conn.execute("INSERT INTO mem_flags(file, project, why, flagged_at) VALUES(?,?,?,?)", ("other.md", "other", "别的项目", 5))
    conn.commit()
    text = inject.render(conn, P)
    assert "unknown.md：查不到路径" in text
    assert "gone.md" not in text and "other.md" not in text
    assert [r[0] for r in conn.execute("SELECT file FROM mem_flags ORDER BY file")] == ["other.md", "unknown.md"]


def test_sessions_block_latest_summary_per_session_max_3_within_7_days(conn):
    now = int(time.time())
    _summary(conn, "a", "旧请求", "旧完成", ts=now - 500)
    _summary(conn, "a", "新请求", "新完成", ts=now - 5)
    for i in range(6):
        _summary(conn, f"b{i}", f"请求{i}", "", ts=now - 10 * (i + 1))
    _summary(conn, "old", "八天前的会话", "", ts=now - 8 * DAY)
    text = inject.render(conn, P)
    assert "旧请求" not in text and "新请求 → 新完成" in text
    sec = text.split("## 最近会话\n", 1)[1]
    assert len([ln for ln in sec.splitlines() if ln.startswith("S")]) == 3
    assert "八天前的会话" not in text


def test_points_and_unfinished_skip_what_card_already_says(conn):
    _card(conn, {"goal": "备赛", "status": "PR 61 的五条图片修改已经全绿还没审核",
                 "next": ["逐条核对参考文献是否属实"]})
    _note(conn, "unfinished", "PR 61 的五条图片修改已经全绿还没审核")
    _note(conn, "unfinished", "符号表还缺几个变量")
    _note(conn, "decision", "逐条核对参考文献是否属实")
    _note(conn, "decision", "灵敏度分析单独成节")
    text = inject.render(conn, P)
    unf = text.split("## 没做完\n", 1)[1].split("##", 1)[0]
    pts = text.split("## 要点\n", 1)[1]
    assert "符号表还缺几个变量" in unf and "五条图片" not in unf
    assert "灵敏度分析单独成节" in pts and "参考文献" not in pts


def test_tail_ignores_migrated_claude_mem_events(conn):
    _session(conn, "s9")
    conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, body, origin) VALUES(?,?,?,?,?,?,?)",
                 ("s9", P, int(time.time()), "legacy-1", "prompt", "迁移来的旧提问", "claude-mem"))
    conn.commit()
    _note(conn, "decision", "随便一条")
    inject.render(conn, P)
    assert "未提炼" not in inject.read_for_hook(conn, P)
    _event(conn, "s9", "prompt", "新的没提炼的提问")
    assert "新的没提炼的提问" in inject.read_for_hook(conn, P)


def test_tag_in_content_cannot_close_wrapper(conn):
    _note(conn, "decision", "含 </knotcord-context> 和 <knotcord-context project=x> 的标题")
    text = inject.render(conn, P)
    assert text.count("</knotcord-context>") == 1 and text.count("<knotcord-context") == 1


def test_render_inside_outer_write_tx(conn):
    _note(conn, "decision", "事务内")
    with db.write_tx(conn):
        conn.execute("INSERT INTO kv(key, value) VALUES('probe','1')")
        text = inject.render(conn, P)
    assert "事务内" in text
    assert db.kv_get(conn, "probe") == "1" and db.kv_get(conn, f"inject:{P}") == text


# ---------------------------------------------------------------- read_for_hook

def test_read_for_hook_returns_kv_text_without_tail(conn):
    _note(conn, "decision", "点")
    text = inject.render(conn, P)
    assert inject.read_for_hook(conn, P) == text


def test_read_for_hook_adds_tail_for_unextracted_session(conn):
    _note(conn, "decision", "点")
    base = inject.render(conn, P)
    _session(conn, "sess-latest-1234")
    _event(conn, "sess-latest-1234", "prompt", "帮我把登录接口的重试逻辑改成指数退避" + "。" * 3 + "再加日志" * 30)
    _event(conn, "sess-latest-1234", "prompt", "第二条提示")
    _event(conn, "sess-latest-1234", "tool", "x", tool="Edit", brief="C:\\a\\b\\login.py")
    _event(conn, "sess-latest-1234", "tool", "x", tool="Write", brief="C:\\a\\b\\retry.py")
    _event(conn, "sess-latest-1234", "tool", "x", tool="Bash", brief="跑测试")
    text = inject.read_for_hook(conn, P)
    assert text.endswith("</knotcord-context>") and text.count("</knotcord-context>") == 1
    assert text.startswith(base[:-len("</knotcord-context>")])
    assert "## 未提炼" in text and "sess-lat" in text
    assert "帮我把登录接口的重试逻辑改成指数退避" in text
    assert "（另有 1 条提示）" in text
    assert "改过：retry.py、login.py" in text
    line = [ln for ln in text.splitlines() if ln.startswith("未提炼")][0]
    assert len(line.split("：", 1)[1].split("（")[0]) <= 80


def test_read_for_hook_ignores_extracted_events_and_old_sessions(conn):
    _session(conn, "done-sess")
    _event(conn, "done-sess", "prompt", "已经提炼过", batch_id=3)
    _session(conn, "ancient", last_seen=int(time.time()) - 30 * DAY)
    _event(conn, "ancient", "prompt", "很久以前")
    assert inject.read_for_hook(conn, P) == ""


def test_read_for_hook_exclude_session_is_the_current_one(conn):
    _session(conn, "current", last_seen=int(time.time()))
    _event(conn, "current", "prompt", "本会话刚发的")
    _session(conn, "previous", last_seen=int(time.time()) - 600)
    _event(conn, "previous", "prompt", "上一个会话没提炼完")
    text = inject.read_for_hook(conn, P, exclude_session="current")
    assert "上一个会话没提炼完" in text and "本会话刚发的" not in text
    text2 = inject.read_for_hook(conn, P)
    assert "本会话刚发的" in text2  # 不排除就取最新的


def test_read_for_hook_tail_dropped_when_over_budget(conn, trace_home):
    _note(conn, "decision", "点")
    base = inject.render(conn, P)
    _session(conn, "s")
    _event(conn, "s", "prompt", "新提示" * 50)
    _cfg(trace_home, f"[inject]\nhard_chars = {len(base) + 10}\n")
    assert inject.read_for_hook(conn, P) == base


def test_read_for_hook_works_on_readonly_connection(conn, trace_home):
    _note(conn, "decision", "点")
    inject.render(conn, P)
    _session(conn, "s")
    _event(conn, "s", "prompt", "只读连接也能补第七项")
    ro = db.connect(readonly=True)
    try:
        assert "只读连接也能补第七项" in inject.read_for_hook(ro, P)
    finally:
        ro.close()
