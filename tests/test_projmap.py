"""项目地图：互提计数、共用文件、排除规则、kv 读写、开局注入一行、检索加分、janitor 每周项、CLI。"""

import json
import time

import pytest

from knotcord import cli, inject, janitor, projmap
from knotcord.core import config, db
from knotcord.core.index import index_doc
from knotcord.search import api

T0 = 1_762_700_000
P = "proj"


def cfg(**over):
    data = dict(config.DEFAULTS)
    data["projmap.min_score"] = 0.0  # 单项测试看原始分；阈值另有专门的用例
    data.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(data)


def note(conn, project, text, *, files=None, ts=T0, state="active"):
    cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, files, tags, state) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (project, ts, "discovery", text, "", json.dumps(files or [], ensure_ascii=False), "[]", state))
    index_doc(conn, "note", cur.lastrowid, project, ts, text, active=(state == "active"))
    return cur.lastrowid


def summary(conn, project, text, ts=T0):
    cur = conn.execute("INSERT INTO summaries(session_id, project, ts, request, completed) VALUES('s',?,?,?,?)",
                       (project, ts, text, ""))
    index_doc(conn, "summary", cur.lastrowid, project, ts, text)


def prompt(conn, project, text, ts=T0):
    cur = conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, body) VALUES('s',?,?,?, 'prompt', ?)",
                       (project, ts, f"u{time.time_ns()}", text))
    index_doc(conn, "prompt", cur.lastrowid, project, ts, text)


def session(conn, project, cwd, sid=None):
    conn.execute("INSERT INTO sessions(id, client, project, cwd) VALUES(?,?,?,?)",
                 (sid or f"s-{project}-{time.time_ns()}", "claude", project, cwd))


def scores(res):
    return {(p, x["project"]): x["score"] for p, v in res.items() for x in v}


# ---------------------------------------------------------------- 互提计数

def test_mentions_counted_per_document_both_directions(conn):
    note(conn, "ProjA", "排查 ProjB 的问题")
    note(conn, "ProjA", "ProjB 和 ProjB 都要改，一篇只算一次")
    prompt(conn, "ProjA", "看看 ProjB")
    summary(conn, "ProjB", "ProjB 里借用了 ProjA 的写法")
    note(conn, "ProjA", "ProjA 自己提自己不算")
    note(conn, "ProjB", "ProjB 自己提自己不算")
    res = projmap.compute(conn, cfg())
    assert scores(res) == {("ProjA", "ProjB"): 4, ("ProjB", "ProjA"): 4}
    assert res["ProjA"][0]["why"] == "互提 4 次"


def test_superseded_docs_not_counted(conn):
    note(conn, "ProjA", "ProjB 旧结论", state="superseded")
    assert projmap.compute(conn, cfg()) == {}


def test_names_that_contain_another_name_do_not_double_count(conn):
    # 搜 alpha 的短语会命中含 "Alpha-2" 的文档：短名只算没命中长名的
    for _ in range(5):
        note(conn, "ProjX", "改了 Alpha-2 的配置")
    for _ in range(2):
        note(conn, "ProjX", "改了 Alpha 的配置")
    note(conn, "Alpha", "x")
    note(conn, "Alpha-2", "y")
    s = scores(projmap.compute(conn, cfg()))
    assert s[("ProjX", "Alpha-2")] == 5 and s[("ProjX", "Alpha")] == 2


def test_plain_english_word_names_need_exact_case(conn):
    for _ in range(5):
        note(conn, "ProjX", "building a distributed system")  # 普通英语词，不是提到项目
    note(conn, "ProjX", "Building 项目里改了入口")
    note(conn, "Building", "x")
    assert scores(projmap.compute(conn, cfg())) == {("ProjX", "Building"): 1, ("Building", "ProjX"): 1}


# ---------------------------------------------------------------- 排除规则

def test_excluded_names_never_connect(conn):
    bad = ["desktop", "12345", "ab", "scratch-2026-10-09-aaa", ".claude", "New-Chat-2", "D--x--y"]
    for name in bad:
        for _ in range(30):
            note(conn, "ProjA", f"在 {name} 里做了事")
        note(conn, name, "ProjA 的事")  # 它们当来源也不算
    res = projmap.compute(conn, cfg())
    assert res == {}
    assert all(not projmap.name_ok(n, cfg()) for n in bad)
    assert projmap.name_ok("ProjA", cfg()) and projmap.name_ok("图书馆系统", cfg())  # 中文名只要 3 字以上、不在排除表里就参与


def test_exclude_list_is_configurable(conn):
    for _ in range(3):
        note(conn, "ProjA", "ProjB 又出问题")
    note(conn, "ProjB", "x")
    assert projmap.compute(conn, cfg())
    assert projmap.compute(conn, cfg(projmap__exclude=["projb"])) == {}


# ---------------------------------------------------------------- 共用文件

def test_files_under_other_projects_dir(conn):
    session(conn, "ProjB", "E:/ProjB/src")  # cwd 是子目录：截到项目名那一层
    session(conn, "ProjC", "E:\\ProjB\\sub\\ProjC")  # 嵌套：归最深的
    note(conn, "ProjA", "改了几个文件", files=["E:\\ProjB\\x.py", "e:/projb/x.py", "E:/ProjB/lib/y.py", "rel.py",
                                              "E:/ProjB/sub/ProjC/z.py", "E:/ProjA/own.py"])
    note(conn, "ProjB", "ProjB 自己的文件", files=["E:/ProjB/x.py"])
    s = scores(projmap.compute(conn, cfg()))
    # ProjA→ProjB：x.py（大小写、斜杠不同算同一个）和 lib/y.py 共 2 个；z.py 归 ProjC
    assert s[("ProjA", "ProjB")] == 2 * projmap.FILE_WEIGHT
    assert s[("ProjA", "ProjC")] == 1 * projmap.FILE_WEIGHT
    assert ("ProjB", "ProjC") not in s


def test_files_why_text_and_combined_score(conn):
    session(conn, "ProjB", "E:/ProjB")
    note(conn, "ProjA", "ProjB 的事", files=["E:/ProjB/a.py"])
    res = projmap.compute(conn, cfg())
    assert res["ProjA"][0] == {"project": "ProjB", "score": 1 + projmap.FILE_WEIGHT, "why": "互提 1 次，共用文件 1 个"}


def test_home_dir_and_appdata_are_not_project_dirs(conn, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    session(conn, "ProjH", str(home).replace("\\", "/"))
    session(conn, "ProjT", str(home / "AppData" / "Local" / "Temp" / "x").replace("\\", "/"))
    session(conn, "ProjD", "E:/")  # 磁盘根
    note(conn, "ProjA", "x", files=[str(home / "any.txt"), str(home / "AppData" / "Local" / "Temp" / "x" / "a"),
                                    "E:/b.txt"])
    session(conn, "ProjK", "E:/ProjK")  # 对照：普通项目目录下的文件照常算
    note(conn, "ProjA", "y", files=["E:/ProjK/k.py"])
    assert scores(projmap.compute(conn, cfg())) == {("ProjA", "ProjK"): projmap.FILE_WEIGHT,
                                                    ("ProjK", "ProjA"): projmap.FILE_WEIGHT}


# ---------------------------------------------------------------- 阈值、条数、kv

def _pair(conn, a, b, n):
    for i in range(n):  # 带序号：免得每周整理把一模一样的笔记当重复标成 superseded
        note(conn, a, f"{b} 相关的事 {i}")
        note(conn, b, f"x{i}")


def test_min_score_and_max_and_order(conn):
    _pair(conn, "ProjA", "ProjB", 30)
    _pair(conn, "ProjA", "ProjC", 20)
    _pair(conn, "ProjA", "ProjD", 40)
    _pair(conn, "ProjA", "ProjE", 5)
    top = projmap.compute(conn, cfg(projmap__max=3))["ProjA"]
    assert [x["project"] for x in top] == ["ProjD", "ProjB", "ProjC"]  # 从高到低，最多 3 个
    # 阈值：低于 min_score 不算
    kept = projmap.compute(conn, cfg(projmap__min_score=25.0, projmap__max=10))["ProjA"]
    assert [x["project"] for x in kept] == ["ProjD", "ProjB"]
    assert float(config.DEFAULTS["projmap.min_score"]) > 0


def test_build_writes_kv_and_clears_stale(conn):
    _pair(conn, "ProjA", "ProjB", 3)
    res = projmap.build(conn, cfg())
    assert set(res) == {"ProjA", "ProjB"}
    got = projmap.related(conn, "ProjA")
    assert got == [{"project": "ProjB", "score": 3, "why": "互提 3 次"}]
    assert db.kv_get(conn, "projmap_at")
    conn.execute("DELETE FROM notes WHERE project='ProjA'")
    conn.execute("DELETE FROM docs WHERE project='ProjA'")
    projmap.build(conn, cfg())  # 关系没了：旧的 kv 不能留着
    assert projmap.related(conn, "ProjA") == [] and projmap.related(conn, "ProjB") == []


def test_related_tolerates_missing_or_bad_values(conn):
    assert projmap.related(conn, "nope") == []
    db.kv_set(conn, "projmap:bad", "{不是 json")
    assert projmap.related(conn, "bad") == []
    db.kv_set(conn, "projmap:odd", json.dumps({"a": 1}))
    assert projmap.related(conn, "odd") == []


def test_disabled_does_nothing(conn):
    _pair(conn, "ProjA", "ProjB", 3)
    assert projmap.build(conn, cfg(projmap__enabled=False)) == {}
    assert projmap.related(conn, "ProjA") == []


# ---------------------------------------------------------------- 开局注入

def _set_rel(conn, project, items):
    db.kv_set(conn, projmap.KEY_PREFIX + project, json.dumps(items, ensure_ascii=False))


def test_inject_has_related_line(conn):
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (P, int(time.time()), "decision", "选 sqlite 做存储", "b"))
    inject.render(conn, P)
    _set_rel(conn, P, [{"project": "ProjB", "score": 45, "why": "互提 12 次，共用文件 11 个"},
                       {"project": "ProjC", "score": 30, "why": "共用文件 10 个"}])
    out = inject.read_for_hook(conn, P)
    assert "相关项目：ProjB（互提 12 次，共用文件 11 个）、ProjC（共用文件 10 个）\n" in out
    assert out.endswith("</knotcord-context>") and out.count("<knotcord-context") == 1


def test_inject_without_related_has_no_line(conn):
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (P, int(time.time()), "decision", "选 sqlite 做存储", "b"))
    text = inject.render(conn, P)
    assert inject.read_for_hook(conn, P) == text and "相关项目" not in text
    _set_rel(conn, P, [])
    assert "相关项目" not in inject.read_for_hook(conn, P)


def test_inject_related_line_obeys_budget_and_switch(conn, trace_home):
    _set_rel(conn, P, [{"project": "ProjB", "score": 45, "why": "互提 12 次"}])
    assert "相关项目：ProjB（互提 12 次）" in inject.read_for_hook(conn, P)  # 没有别的内容时也能单独出现
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[inject]\nbudget_tokens = 5\nhard_chars = 100\n", encoding="utf-8")
    assert "相关项目" not in inject.read_for_hook(conn, P)  # 装不下就整行跳过
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    assert "相关项目" not in inject.read_for_hook(conn, P)


# ---------------------------------------------------------------- 检索加分

def _same_text_docs(conn):
    """三个项目各一条同样的笔记；ts 越新排得越前（bm25 打平，按 ts 倒序）。"""
    ids = {}
    for i, proj in enumerate(("Other", "Rel", "Cur")):
        cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES(?,?,?,?,?, 'active')",
                           (proj, T0 + i, "discovery", "蓝色鲸鱼配置", "", ))
        index_doc(conn, "note", cur.lastrowid, proj, T0 + i, "蓝色鲸鱼配置")
        ids[proj] = cur.lastrowid
    return ids


def _order(conn, **kw):
    out = api.search(conn, "蓝色鲸鱼", current_project="Cur", **kw)
    return [ln.split()[2] for ln in out.splitlines()]  # 行格式：n<id> 日期 项目 ...


def test_related_projects_get_a_small_boost(conn):
    _same_text_docs(conn)
    cfgd = config.load()
    assert float(cfgd.get("search.related_boost")) == 1.1 < float(cfgd.get("search.project_boost"))

    def order(cur):
        out = api.search(conn, "蓝色鲸鱼", current_project=cur)
        return [ln.split()[2] for ln in out.splitlines()]

    # 没有地图：三条一模一样，按 ts 新旧排（RRF 名次 1/61、1/62、1/63）
    assert order("Nobody") == ["Cur", "Rel", "Other"]
    # Other 是相关项目：垫底的 1/63 乘 1.1 反超前两名，说明确实加了分
    _set_rel(conn, "Nobody", [{"project": "Other", "score": 50, "why": "互提 50 次"}])
    assert order("Nobody") == ["Other", "Cur", "Rel"]
    # 本项目自己仍按 project_boost（1.2）加分
    assert order("Cur")[0] == "Cur"


def test_related_boost_never_exceeds_project_boost(conn, trace_home):
    _same_text_docs(conn)
    _set_rel(conn, "Cur", [{"project": "Rel", "score": 50, "why": "互提 50 次"}])
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[search]\nrelated_boost = 5.0\n", encoding="utf-8")
    # 当前项目 Cur 排第 1（1/61*1.2），Rel 排第 2（1/62）；就算配了 5.0，也只按 project_boost 算，追不上
    assert _order(conn)[0] == "Cur"


def test_related_boost_off_when_projmap_disabled(conn, trace_home):
    _same_text_docs(conn)
    _set_rel(conn, "Nobody", [{"project": "Rel", "score": 50, "why": "x"}])
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    out = api.search(conn, "蓝色鲸鱼", current_project="Nobody")
    assert [ln.split()[2] for ln in out.splitlines()][0] == "Cur"


# ---------------------------------------------------------------- janitor 每周项、CLI

def test_janitor_weekly_builds_map_and_isolates_errors(conn, trace_home, monkeypatch):
    _pair(conn, "ProjA", "ProjB", 30)
    res = janitor.run(conn, config.load(), force=True, memroot=str(trace_home / "nomem"))
    assert res["projmap"] == 2 and projmap.related(conn, "ProjA")[0]["project"] == "ProjB"

    def boom(*a, **k):
        raise RuntimeError("坏了")
    monkeypatch.setattr(projmap, "build", boom)
    res = janitor.run(conn, config.load(), force=True, memroot=str(trace_home / "nomem"))
    assert "坏了" in res["projmap_error"] and "projmap" not in res
    assert res["quick_check"] == "ok" and db.kv_get(conn, janitor.KEY_WEEKLY)  # 其他每周项和记录照常


def test_cli_projmap(conn, capsys):
    _pair(conn, "ProjA", "ProjB", 30)
    _pair(conn, "ProjA", "ProjC", 3)  # 低于默认阈值
    assert cli.main(["projmap"]) == 0
    out = capsys.readouterr().out
    assert "ProjA: ProjB 30（互提 30 次）" in out and "ProjC" not in out.split("ProjA:")[1].split("\n")[0]
    assert projmap.related(conn, "ProjB")  # 真的写了 kv
    assert cli.main(["projmap", "--project", "ProjB"]) == 0
    out = capsys.readouterr().out
    assert "ProjB: ProjA 30" in out and "ProjC" not in out
    assert cli.main(["projmap", "--project", "Nope"]) == 0
    assert capsys.readouterr().out.strip() == "无"


def test_cli_projmap_disabled(conn, trace_home, capsys):
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text("[projmap]\nenabled = false\n", encoding="utf-8")
    assert cli.main(["projmap"]) == 0
    assert "已关闭" in capsys.readouterr().out
