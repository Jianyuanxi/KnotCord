"""提问前自动召回：UserPromptSubmit hook（真起子进程 python -I -S）+ recall 模块的单元测试。"""

import json
import os
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from knotcord import recall
from knotcord.core import config, db, index, project

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "knotcord" / "hooks" / "entry.py"
CWD = "C:\\work\\demo"
PROJ = project.project_key(CWD)

RELEVANT = "路由规则迁移到独立配置文件的方案已确定"
PROMPT = "请问路由规则迁移到独立配置文件的方案具体是怎么定的？"
UNRELATED = "今天天气不错我们一起去公园散步吧好不好呀"


def run_hook(event, payload=None, *, env=None, raw=None, timeout=10):
    e = dict(os.environ)
    e.pop("KNOTCORD_INTERNAL", None)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(ENTRY), event], input=data,
                       capture_output=True, env=e, timeout=timeout)
    return r, time.perf_counter() - t0


def prompt_payload(prompt=PROMPT, sid="sess-1", cwd=CWD):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": cwd, "prompt": prompt}


def enable(trace_home, extra=""):
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[recall]\nenabled = true\n" + extra, encoding="utf-8")


def add_note(conn, title, body="正文", *, project_=PROJ, type_="decision", state="active", ts=None):
    ts = ts or int(time.time())
    cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES(?,?,?,?,?,?)",
                       (project_, ts, type_, title, body, state))
    index.index_doc(conn, "note", cur.lastrowid, project_, ts, title + " " + body, active=(state == "active"))
    return cur.lastrowid


def cfg_with(**over):
    data = dict(config.DEFAULTS)
    data.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(data)


# ---------------------------------------------------------------- 默认关闭：不连库、不输出

def test_disabled_by_default_prints_nothing_and_touches_no_db(trace_home):
    r, dt = run_hook("prompt", prompt_payload())
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert not trace_home.exists()  # 连库会顺手建目录；没建说明没连


def test_disabled_does_not_even_import_sqlite(conn, tmp_path):
    add_note(conn, RELEVANT)
    script = tmp_path / "probe_disabled.py"
    script.write_text(textwrap.dedent(f"""
        import sys, io
        sys.path.insert(0, {str(ROOT)!r})
        payload = {json.dumps(prompt_payload(), ensure_ascii=False)!r}
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")))
        from knotcord.hooks import entry
        entry.main(["prompt"])
        print("SQLITE=" + str("sqlite3" in sys.modules) + " RECALL=" + str("knotcord.recall" in sys.modules))
    """), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(script)], capture_output=True,
                       env=dict(os.environ), timeout=10)
    out = r.stdout.decode("utf-8").strip().splitlines()
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert out == ["SQLITE=False RECALL=False"]  # 关着：没有输出、没 import sqlite3、连 recall 模块都没加载


def test_explicit_false_also_disabled(conn, trace_home):
    add_note(conn, RELEVANT)
    (trace_home / "config.toml").write_text("[recall]\nenabled = false\n", encoding="utf-8")
    r, _ = run_hook("prompt", prompt_payload())
    assert r.returncode == 0 and r.stdout == b""


# ---------------------------------------------------------------- 开启：输出格式、耗时、去重

def test_enabled_injects_relevant_note_in_expected_format(conn, trace_home):
    nid = add_note(conn, RELEVANT, "详细说明：把规则拆成独立配置文件，按场景加载。")
    add_note(conn, "完全无关的另一件事：给测试脚本补充日志轮转", "日志按天切分")
    enable(trace_home)
    r, dt = run_hook("prompt", prompt_payload())
    assert r.returncode == 0 and r.stderr == b""
    out = r.stdout.decode("utf-8")
    lines = out.rstrip("\n").split("\n")
    assert lines[0] == f'<knotcord-context kind="recall" project="{PROJ}">'
    assert lines[1] == recall.HEADER
    assert lines[2].startswith(f"n{nid} ") and "decision" in lines[2] and RELEVANT in lines[2]
    assert lines[-1] == "</knotcord-context>" and len(lines) == 4  # 无关的那条没进来
    assert out.endswith("\n") and "\r" not in out
    assert dt < 0.9, dt  # 守住 1 秒的 hook 期限


def test_unrelated_prompt_injects_nothing(conn, trace_home):
    add_note(conn, RELEVANT)
    enable(trace_home)
    r, _ = run_hook("prompt", prompt_payload(UNRELATED))
    assert r.returncode == 0 and r.stdout == b""


def test_same_session_not_repeated_and_session_end_resets(conn, trace_home):
    add_note(conn, RELEVANT)
    enable(trace_home)
    first, _ = run_hook("prompt", prompt_payload(sid="sA"))
    assert b"kind=\"recall\"" in first.stdout
    again, _ = run_hook("prompt", prompt_payload(sid="sA"))
    assert again.stdout == b""  # 同一会话里注入过的不再注入
    other, _ = run_hook("prompt", prompt_payload(sid="sB"))
    assert other.stdout == first.stdout  # 别的会话照常
    state = trace_home / "tmp" / "recall-sA.json"
    assert state.exists()
    run_hook("session-end", {"session_id": "sA", "transcript_path": "p"},
             env={"KNOTCORD_DRAIN_CMD": json.dumps([sys.executable, "-c", "pass"])})
    assert not state.exists()  # 会话结束，状态文件用完即删
    after, _ = run_hook("prompt", prompt_payload(sid="sA"))
    assert after.stdout == first.stdout


def test_skips_short_slash_and_confirmations(conn, trace_home):
    add_note(conn, RELEVANT)
    enable(trace_home)
    for p in ("路由规则", "/compact 路由规则迁移到独立配置文件的方案", "好的，继续吧", "ok", "继续"):
        r, _ = run_hook("prompt", prompt_payload(p))
        assert r.returncode == 0 and r.stdout == b"", p


def test_other_project_and_superseded_not_recalled(conn, trace_home):
    add_note(conn, RELEVANT, project_="elsewhere")
    add_note(conn, RELEVANT + "（旧）", state="superseded")
    enable(trace_home)
    r, _ = run_hook("prompt", prompt_payload())
    assert r.stdout == b""
    enable(trace_home, "cross_project = true\n")
    r, _ = run_hook("prompt", prompt_payload())
    assert RELEVANT.encode("utf-8") in r.stdout and "旧）".encode("utf-8") not in r.stdout


def test_notes_already_in_session_start_injection_not_repeated(conn, trace_home):
    nid = add_note(conn, RELEVANT)
    db.kv_set(conn, f"inject:{PROJ}", f"<knotcord-context>\n## 要点\nn{nid} 11-10 decision {RELEVANT}\n</knotcord-context>")
    enable(trace_home)
    r, _ = run_hook("prompt", prompt_payload())
    assert r.stdout == b""


def test_internal_env_and_garbage_stdin_exit_zero_silently(conn, trace_home):
    add_note(conn, RELEVANT)
    enable(trace_home)
    r, _ = run_hook("prompt", prompt_payload(), env={"KNOTCORD_INTERNAL": "1"})
    assert r.returncode == 0 and r.stdout == b""
    for raw in (b"", b"not json", b"[1,2]", b"\xff\xfe\x00", b'{"prompt": 5}', b'{"prompt": null}'):
        r, _ = run_hook("prompt", raw=raw)
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


def test_without_db_injects_nothing_and_creates_nothing(trace_home):
    enable(trace_home)
    r, _ = run_hook("prompt", prompt_payload())
    assert r.returncode == 0 and r.stdout == b""
    assert not (trace_home / "knotcord.db").exists()


def test_not_blocked_by_write_lock(conn, trace_home):
    add_note(conn, RELEVANT)
    enable(trace_home)
    free, _ = run_hook("prompt", prompt_payload(sid="lock-0"))
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r, dt = run_hook("prompt", prompt_payload(sid="lock-1"))
    finally:
        blocker.rollback()
        blocker.close()
    assert r.returncode == 0 and r.stdout == free.stdout and r.stdout != b""  # WAL：读不被写锁挡住
    assert dt < 0.95


def test_median_latency(conn, trace_home):
    for i in range(300):
        add_note(conn, f"第{i}号笔记：与路由无关的杂项记录条目{i}", "一些别的内容")
    add_note(conn, RELEVANT)
    enable(trace_home)
    # 每次换会话号，避免被"同会话不重复"挡成空输出而测不到查询
    run_hook("prompt", prompt_payload(sid="warm"))
    times = sorted(run_hook("prompt", prompt_payload(sid=f"lat-{i}"))[1] for i in range(15))
    med = statistics.median(times)
    print(f"\nUserPromptSubmit 15 次: 中位 {med * 1000:.0f} ms, 最慢 {times[-1] * 1000:.0f} ms")
    assert med < 0.5 and times[-1] < 0.95


def test_enabled_path_does_not_load_heavy_modules(conn, trace_home, tmp_path):
    add_note(conn, RELEVANT)
    enable(trace_home)
    script = tmp_path / "probe_enabled.py"
    script.write_text(textwrap.dedent(f"""
        import sys, io
        sys.path.insert(0, {str(ROOT)!r})
        payload = {json.dumps(prompt_payload(sid="probe"), ensure_ascii=False)!r}
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")))
        from knotcord.hooks import entry
        entry.main(["prompt"])
        bad = [m for m in ("numpy", "onnxruntime", "mcp", "knotcord.extract", "knotcord.search", "knotcord.migrate")
               if m in sys.modules]
        print("BAD=" + ",".join(bad))
    """), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(script)], capture_output=True,
                       env=dict(os.environ), timeout=10)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "kind=\"recall\"" in out and out.splitlines()[-1] == "BAD="


# ---------------------------------------------------------------- 期限：库慢时放弃，不拖住提问

def test_slow_query_is_interrupted_at_deadline(conn, trace_home, monkeypatch):
    add_note(conn, RELEVANT)

    def slow_find(c, *a, **k):
        c.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<200000000) "
                  "SELECT count(*) FROM c").fetchone()
        return [{"key": "n1", "score": 99, "ts": 1, "line": "n1 x"}]  # 不该走到这里

    monkeypatch.setattr(recall, "find", slow_find)
    t0 = time.perf_counter()
    text = recall.run(prompt_payload(), cfg_with(recall__enabled=True, recall__timeout_ms=150))
    assert text == "" and time.perf_counter() - t0 < 2.0  # 150 ms 的期限被打断，没有跑满


# ---------------------------------------------------------------- 模块单元

def test_should_skip_rules():
    cfg = cfg_with()
    assert recall.should_skip("短", cfg) and recall.should_skip("   ", cfg) and recall.should_skip("", cfg)
    assert recall.should_skip("/compact 帮我压缩一下上下文吧好吗", cfg)
    for p in ("好的", "好的，继续", "OK!", "继续", "yes", "就这样", "按你说的做", "谢谢", "go on"):
        assert recall.should_skip(p + "   " * 4, cfg), p
    assert not recall.should_skip("请问路由规则迁移到独立配置文件的方案", cfg)
    assert not recall.should_skip("好的，那就把路由规则迁移到独立配置文件里去吧", cfg)  # 带实质内容


def test_query_terms_drop_filler_and_project_name():
    runs, words, total = recall.query_terms("帮我看看 Span 的路由规则为什么对国内站不生效，3x-ui 一下", "Span")
    assert runs == ["路由规则", "国内站不生效"] and words == ["3x-ui"] and total == 4 + 6 + 5
    assert recall.query_terms("the sqlite backup fails please help", "")[1] == ["sqlite", "backup", "fails"]


def test_relevance_prefers_long_contiguous_matches_and_title():
    terms = recall.query_terms("路由规则迁移到独立配置文件的方案", "")
    hit = recall.relevance(terms, "路由规则迁移到独立配置文件", "")
    scattered = recall.relevance(terms, "规则 路由 配置 独立 文件 迁移", "")
    assert hit[0] > scattered[0] and hit[2] > scattered[2] and hit[1] > 0.8
    body_only = recall.relevance(terms, "无关标题", "……路由规则迁移到独立配置文件……")
    assert hit[0] > body_only[0] > 0  # 标题里命中加分


def test_render_budget_falls_back_to_bare_ids():
    items = [{"key": f"n{i}", "score": 10 - i, "ts": 1, "line": f"n{i} 11-10 decision " + "很长的标题" * 10}
             for i in range(1, 6)]
    full, keys = recall.render(items, "demo", cfg_with())
    assert keys == ["n1", "n2", "n3", "n4", "n5"] and full.count("很长的标题") == 50
    small, keys = recall.render(items, "demo", cfg_with(recall__budget_tokens=130))
    assert keys[0] == "n1" and "另有：" in small and len(small) < len(full)
    assert small.count("很长的标题") < 50 and all(k in small for k in keys)
    assert recall.render(items, "demo", cfg_with(recall__budget_tokens=10)) == ("", [])
    capped, keys = recall.render(items, "demo", cfg_with(recall__max_items=2))
    assert keys == ["n1", "n2"]
    assert recall.render([], "demo", cfg_with()) == ("", [])


def test_find_threshold_and_skip_set(conn):
    a = add_note(conn, RELEVANT)
    add_note(conn, "路由规则", "太短的标题只有零星命中")
    cfg = cfg_with()
    got = recall.find(conn, PROMPT, PROJ, set(), cfg)
    assert [g["key"] for g in got] == [f"n{a}"]
    assert recall.find(conn, PROMPT, PROJ, {f"n{a}"}, cfg) == []
    assert recall.find(conn, PROMPT, PROJ, set(), cfg_with(recall__min_score=999)) == []
    assert recall.find(conn, UNRELATED, PROJ, set(), cfg) == []


def test_unfinished_gets_u_prefix_and_summaries_are_searched(conn):
    u = add_note(conn, "路由规则迁移到独立配置文件的方案尚未落地", type_="unfinished")
    conn.execute("INSERT INTO summaries(session_id, project, ts, request, completed) VALUES(?,?,?,?,?)",
                 ("s1", PROJ, int(time.time()), "讨论路由规则迁移到独立配置文件的方案", "定了按场景加载"))
    sid = conn.execute("SELECT max(id) FROM summaries").fetchone()[0]
    index.index_doc(conn, "summary", sid, PROJ, int(time.time()), "讨论路由规则迁移到独立配置文件的方案 定了按场景加载")
    keys = [g["key"] for g in recall.find(conn, PROMPT, PROJ, set(), cfg_with())]
    assert set(keys) == {f"u{u}", f"s{sid}"}


def test_seen_state_roundtrip_sanitizes_session_id(trace_home):
    assert recall.load_seen("../../evil") == set()
    recall.save_seen("../../evil", {"n1", "n2"})
    p = recall._seen_path("../../evil")
    assert p.parent == trace_home / "tmp" and p.exists()  # 会话号里的路径符号被替换，写不出 tmp 目录
    assert recall.load_seen("../../evil") == {"n1", "n2"}
    recall.forget("../../evil")
    assert not p.exists()
    recall.forget("never-existed")  # 不存在也不报错
    recall.save_seen("", {"n1"})  # 没有会话号：什么都不写
    assert recall.load_seen("") == set()
