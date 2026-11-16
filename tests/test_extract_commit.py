"""校验（validate）和落库（commit）的细节：非法值修正、过时标记三道关、记忆文件提示、没做完的事。"""

import json
import sys
import types

import pytest

from knotcord.core import db, index
from knotcord.core.lease import Lease, LeaseLost
from knotcord.extract import commit, validate
from knotcord.extract.prompt import PromptCtx
from knotcord.extract.validate import ValidationError, Validated

T0 = 1_795_000_000


@pytest.fixture(autouse=True)
def fake_inject(monkeypatch):
    mod = types.ModuleType("knotcord.inject")
    mod.render = lambda conn, project: None
    monkeypatch.setitem(sys.modules, "knotcord.inject", mod)


@pytest.fixture
def lease(conn):
    le = Lease(conn, owner="me")
    assert le.try_acquire()
    return le


def obs(**over):
    d = {"type": "change", "title": "标题", "body": "正文", "facts": [], "files": [], "tags": [], "slot": "",
         "supersedes": [], "source": []}
    d.update(over)
    return d


def V(**over):
    base = {"skip": False, "observations": [], "summary": {}, "state_card": {}, "memory_conflicts": [],
            "unfinished_new": [], "unfinished_resolved": []}
    base.update(over)
    return validate.validate(base, n_notes=3, n_unfinished=2, n_events=4)


# ------------------------------------------------------------------ validate

def test_parse_model_text_variants():
    assert validate.parse_model_text('{"skip": true}') == {"skip": True}
    assert validate.parse_model_text('```json\n{"skip": true}\n```') == {"skip": True}
    assert validate.parse_model_text('好的，结果如下：{"skip": false, "x": {"a": 1}} 以上') == {"skip": False, "x": {"a": 1}}
    for bad in ("", "   ", "没有 JSON", "{坏的"):
        with pytest.raises(ValidationError):
            validate.parse_model_text(bad)


def test_validate_rejects_non_object_or_unknown_shape():
    for bad in ([], "x", 3, {"error": "rate limited"}):
        with pytest.raises(ValidationError):
            validate.validate(bad, n_notes=0, n_unfinished=0, n_events=0)
    with pytest.raises(ValidationError):
        validate.validate({"observations": "不是数组"}, n_notes=0, n_unfinished=0, n_events=0)


def test_invalid_type_becomes_change_and_enums_enforced():
    v = V(observations=[obs(type="发现"), obs(type="discovery", title="b", tags=["gotcha", "乱写", "pattern"]),
                        obs(type=None, title="c")])
    assert [n["type"] for n in v.notes] == ["change", "discovery", "change"]
    assert v.notes[1]["tags"] == ["gotcha", "pattern"]
    assert any("非法 type" in w for w in v.warnings)


def test_out_of_range_numbers_dropped():
    v = V(observations=[obs(supersedes=[0, 1, 3, 4, "2", True, 1.0, -1], source=[1, 4, 5])],
          unfinished_resolved=[1, 2, 3])
    assert v.notes[0]["supersedes"] == [1, 3, 2]  # 1..3 之内；0、4、-1、True 丢弃；重复的 1.0 去重
    assert v.notes[0]["source"] == [1, 4]
    assert v.unfinished_resolved == [1, 2]
    assert any("越界" in w for w in v.warnings)


def test_overlong_fields_truncated_and_empty_title_dropped():
    v = V(observations=[obs(title="题" * 500, body="文" * 5000, facts=["f" * 1000] * 20, files=["p"] * 50),
                        obs(title="  ")],
          summary={"request": "r" * 5000, "completed": "", "learned": "", "next_steps": ""})
    assert len(v.notes) == 1
    n = v.notes[0]
    assert len(n["title"]) == validate.TITLE_MAX and len(n["body"]) == validate.BODY_MAX
    assert len(n["facts"]) == 1 and len(n["facts"][0]) == validate.FACT_MAX  # 重复的去重，再截断
    assert len(v.summary["request"]) == validate.SUMMARY_MAX


def test_card_fit_to_token_limit_and_empty_summary_ignored():
    big = {"goal": "目标" * 100, "status": "现状" * 100,
           "decisions": ["决" * 150] * 8, "pitfalls": ["坑" * 150] * 8, "next": ["下" * 150] * 8}
    v = validate.validate({"state_card": big, "summary": {"request": "", "completed": "", "learned": "",
                                                          "next_steps": ""}},
                          n_notes=0, n_unfinished=0, n_events=0, card_tokens=600)
    from knotcord.core import tokens
    assert tokens.estimate(json.dumps(v.card, ensure_ascii=False)) <= 600
    assert v.card["goal"]  # 先砍列表，字符串保住
    assert v.summary is None


# ------------------------------------------------------------------ commit

def mk_ctx(conn, project="p", n_events=3, notes=(), unfinished=(), memfiles=(), text=""):
    ids = []
    for i in range(n_events):
        ids.append(conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind) VALUES('s',?,?,?, 'prompt')",
                                (project, T0 + i * 10, f"u{i}-{T0}-{len(ids)}-{id(ids)}")).lastrowid)
    ctx = PromptCtx(project=project)
    ctx.event_ids = ids
    ctx.event_ts = [T0 + i * 10 for i in range(n_events)]
    ctx.notes = list(notes)
    ctx.unfinished = list(unfinished)
    ctx.memfiles = list(memfiles)
    ctx.batch_text = text
    return ctx


def batch_row(conn):
    return conn.execute("INSERT INTO batches(session_id, created, status) VALUES('s',?, 'running')", (T0,)).lastrowid


def run(conn, lease, v, ctx, project="p", session="s"):
    return commit.commit_batch(conn, lease, batch_id=batch_row(conn), session_id=session, project=project, v=v,
                               ctx=ctx, provider="codex", model="m", tokens_in=10, tokens_out=5, cost_cny=0.0,
                               now=T0 + 1000)


def add_note(conn, title, ts, slot=None, project="p", typ="change", state="active", session="old"):
    nid = conn.execute("INSERT INTO notes(project, session_id, ts, type, title, body, slot, state, content_hash) "
                       "VALUES(?,?,?,?,?, '', ?,?, ?)",
                       (project, session, ts, typ, title, slot, state, commit.content_hash(project, title))).lastrowid
    index.index_doc(conn, "note", nid, project, ts, title)
    return nid


def state_of(conn, nid):
    r = conn.execute("SELECT state, superseded_by, superseded_at FROM notes WHERE id=?", (nid,)).fetchone()
    return r["state"], r["superseded_by"], r["superseded_at"]


def active_flag(conn, nid):
    return conn.execute("SELECT active FROM docs WHERE src='note' AND src_id=?", (nid,)).fetchone()[0]


def test_slot_supersedes_older_active_note_in_same_project(conn, lease):
    old = add_note(conn, "面板端口是 2053", T0 - 5000, slot="3x-ui.面板端口")
    other_slot = add_note(conn, "别的", T0 - 5000, slot="3x-ui.用户名")
    other_proj = add_note(conn, "别的项目的端口", T0 - 5000, slot="3x-ui.面板端口", project="q")
    ctx = mk_ctx(conn)
    run(conn, lease, V(observations=[obs(title="面板端口改成 54321", slot="3x-ui.面板端口", source=[3])]), ctx)
    new = conn.execute("SELECT id, ts FROM notes WHERE title='面板端口改成 54321'").fetchone()
    assert new["ts"] == T0 + 20  # 取依据事件的时间
    assert state_of(conn, old)[:2] == ("superseded", new["id"])
    assert state_of(conn, old)[2] == T0 + 1000
    assert active_flag(conn, old) == 0 and active_flag(conn, new["id"]) == 1
    assert state_of(conn, other_slot)[0] == "active" and state_of(conn, other_proj)[0] == "active"


def test_model_supersedes_by_number(conn, lease):
    a = add_note(conn, "旧结论 A", T0 - 100)
    b = add_note(conn, "旧结论 B", T0 - 100)
    ctx = mk_ctx(conn, notes=[a, b])
    run(conn, lease, V(observations=[obs(title="新结论", supersedes=[2])]), ctx)
    new = conn.execute("SELECT id FROM notes WHERE title='新结论'").fetchone()[0]
    assert state_of(conn, a)[0] == "active"
    assert state_of(conn, b)[:2] == ("superseded", new)


def test_time_rule_older_new_note_does_not_supersede(conn, lease):
    """补录旧数据：新写入的笔记事件时间比库里的旧笔记还早，不能把较新的事实弄失效。"""
    recent = add_note(conn, "较新的事实", T0 + 5000, slot="x.y")
    by_num = add_note(conn, "较新的另一条", T0 + 5000)
    ctx = mk_ctx(conn, notes=[by_num])
    run(conn, lease, V(observations=[obs(title="补录的旧事实", slot="x.y", supersedes=[1], source=[1])]), ctx)
    assert state_of(conn, recent)[0] == "active" and state_of(conn, by_num)[0] == "active"
    assert active_flag(conn, recent) == 1
    assert conn.execute("SELECT count(*) FROM notes WHERE title='补录的旧事实'").fetchone()[0] == 1  # 仍然入库


def test_same_session_same_second_later_note_wins(conn, lease):
    ctx = mk_ctx(conn)
    run(conn, lease, V(observations=[obs(title="第一版", slot="a.b", source=[1]),
                                     obs(title="第二版", slot="a.b", source=[1])]), ctx, session="s")
    states = {r["title"]: r["state"] for r in conn.execute("SELECT title, state FROM notes")}
    assert states == {"第一版": "superseded", "第二版": "active"}


def test_content_hash_dedupe_same_project_only(conn, lease):
    add_note(conn, "同名笔记", T0 - 10)
    ctx = mk_ctx(conn)
    s = run(conn, lease, V(observations=[obs(title="同名 笔记！")]), ctx)  # 归一化后相同
    assert s["notes"] == 0 and conn.execute("SELECT count(*) FROM notes WHERE project='p'").fetchone()[0] == 1
    run(conn, lease, V(observations=[obs(title="同名笔记")]), mk_ctx(conn, project="q"), project="q")
    assert conn.execute("SELECT count(*) FROM notes WHERE project='q'").fetchone()[0] == 1


def test_notes_indexed_with_title_body_facts_files_tags(conn, lease):
    ctx = mk_ctx(conn)
    run(conn, lease, V(observations=[obs(title="宝塔面板", body="端口 8888", facts=["nginx 1.27"],
                                         files=["conf.toml"], tags=["gotcha"])]), ctx)
    for q in ("宝塔", "8888", "nginx", "conf.toml", "gotcha"):
        assert index.fts_search(conn, q, project="p", srcs=("note",)), q


def test_unfinished_new_resolved_and_mention_refresh(conn, lease):
    u_done = add_note(conn, "把迁移脚本写完", T0 - 9999, typ="unfinished")
    u_seen = add_note(conn, "修复 pipeline 切批 bug", T0 - 9999, typ="unfinished")
    u_other = add_note(conn, "整理 claude-mem 旧数据", T0 - 9999, typ="unfinished")
    for u in (u_done, u_seen, u_other):
        conn.execute("UPDATE notes SET last_mentioned=? WHERE id=?", (T0 - 9999, u))
    ctx = mk_ctx(conn, unfinished=[{"id": u_done, "text": "把迁移脚本写完"},
                                   {"id": u_seen, "text": "修复 pipeline 切批 bug"},
                                   {"id": u_other, "text": "整理 claude-mem 旧数据"}],
                 text="[1] 11-15 用户: 继续修复 pipeline 切批 bug，迁移脚本也写完了")
    s = run(conn, lease, V(unfinished_new=["给 search 加缓存", "给 search 加缓存"], unfinished_resolved=[1]), ctx)
    assert state_of(conn, u_done)[0] == "resolved"
    lm = {r["id"]: r["last_mentioned"] for r in conn.execute("SELECT id, last_mentioned FROM notes")}
    assert lm[u_seen] == T0 + 20          # 本批原文里有它的关键词：刷新为本批最后事件时间
    assert lm[u_other] == T0 - 9999       # 没提到：不动
    assert lm[u_done] == T0 - 9999        # 已解决的不刷新
    new = conn.execute("SELECT * FROM notes WHERE type='unfinished' AND title='给 search 加缓存'").fetchall()
    assert len(new) == 1 and new[0]["state"] == "active" and new[0]["last_mentioned"] == T0 + 20
    assert s["unfinished_new"] == 1 and s["resolved"] == 1
    assert active_flag(conn, new[0]["id"]) == 1


def test_state_cards_keep_latest_five_and_skip_unchanged(conn, lease):
    for i in range(7):
        v = V(state_card={"goal": f"目标{i}", "status": "s", "decisions": [], "pitfalls": [], "next": []})
        commit_ts = T0 + i
        commit.commit_batch(conn, lease, batch_id=batch_row(conn), session_id="s", project="p", v=v,
                            ctx=mk_ctx(conn), provider="codex", model="m", tokens_in=0, tokens_out=0,
                            cost_cny=0, now=commit_ts)
    rows = conn.execute("SELECT card FROM state_cards WHERE project='p' ORDER BY ts DESC, id DESC").fetchall()
    assert len(rows) == 5
    assert [json.loads(r["card"])["goal"] for r in rows] == ["目标6", "目标5", "目标4", "目标3", "目标2"]
    # 内容没变：不再插新行
    run(conn, lease, V(state_card={"goal": "目标6", "status": "s", "decisions": [], "pitfalls": [], "next": []}),
        mk_ctx(conn))
    assert conn.execute("SELECT count(*) FROM state_cards WHERE project='p'").fetchone()[0] == 5
    # 别的项目互不影响
    run(conn, lease, V(state_card={"goal": "q", "status": "", "decisions": [], "pitfalls": [], "next": []}),
        mk_ctx(conn, project="q"), project="q")
    assert conn.execute("SELECT count(*) FROM state_cards WHERE project='q'").fetchone()[0] == 1


def test_memory_conflicts_only_for_fed_files(conn, lease):
    mf = [{"path": r"C:\m\project_span-xray.md", "name": "span-xray", "label": "project_span-xray.md", "mtime": T0 - 50},
          {"path": r"C:\m\fixed.md", "name": "fixed", "label": "fixed.md", "mtime": T0 + 5000}]  # 这批之后改过
    ctx = mk_ctx(conn, memfiles=mf)
    s = run(conn, lease, V(memory_conflicts=[
        {"file": "project_span-xray.md", "why": "内核版本已升到 26.11.9"},
        {"file": "没喂进去的.md", "why": "乱报"},
        {"file": "fixed.md", "why": "文件已经被改过"},
    ]), ctx)
    rows = conn.execute("SELECT * FROM mem_flags").fetchall()
    assert len(rows) == 1 and s["conflicts"] == 1
    assert rows[0]["file"] == r"C:\m\project_span-xray.md" and rows[0]["why"].startswith("内核版本")
    assert rows[0]["project"] == "p" and rows[0]["flagged_at"] == T0 + 1000
    # 再报一次：upsert 覆盖，不重复
    run(conn, lease, V(memory_conflicts=[{"file": "span-xray", "why": "新原因"}]), ctx)
    rows = conn.execute("SELECT * FROM mem_flags").fetchall()
    assert len(rows) == 1 and rows[0]["why"] == "新原因"


def test_skip_true_stamps_events_but_writes_nothing_else(conn, lease):
    ctx = mk_ctx(conn)
    run(conn, lease, V(skip=True, observations=[obs(title="不该入库")], unfinished_new=["也不该"]), ctx)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "done"


def test_summary_per_session_updates_not_duplicates_and_ignores_legacy_row(conn, lease):
    conn.execute("INSERT INTO summaries(session_id, project, ts, request, origin) VALUES('s','p',1,'旧总结','claude-mem')")
    for i in range(3):
        run(conn, lease, V(summary={"request": f"r{i}", "completed": "c", "learned": "", "next_steps": ""}),
            mk_ctx(conn))
    rows = conn.execute("SELECT origin, request FROM summaries ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("claude-mem", "旧总结"), ("knotcord", "r2")]  # 迁移来的旧行不动
    assert conn.execute("SELECT count(*) FROM docs WHERE src='summary'").fetchone()[0] == 1


def test_lease_lost_inside_tx_writes_nothing(conn, lease):
    conn.execute("UPDATE kv SET value=? WHERE key='lease'", (json.dumps({"owner": "other", "expires": 9e12}),))
    ctx = mk_ctx(conn)
    bid = batch_row(conn)
    with pytest.raises(LeaseLost):
        commit.commit_batch(conn, lease, batch_id=bid, session_id="s", project="p",
                            v=V(observations=[obs(title="x")], summary={"request": "r", "completed": "", "learned": "",
                                                                         "next_steps": ""}),
                            ctx=ctx, provider="codex", model="m", tokens_in=0, tokens_out=0, cost_cny=0, now=T0)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM summaries").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()[0] == "running"
    assert not conn.in_transaction


def test_inject_render_failure_does_not_undo_commit(conn, lease, monkeypatch):
    mod = types.ModuleType("knotcord.inject")

    def boom(conn, project):
        raise RuntimeError("render 炸了")

    mod.render = boom
    monkeypatch.setitem(sys.modules, "knotcord.inject", mod)
    run(conn, lease, V(observations=[obs(title="照样入库")]), mk_ctx(conn))
    assert conn.execute("SELECT count(*) FROM notes WHERE title='照样入库'").fetchone()[0] == 1


def test_missing_inject_module_is_tolerated(conn, lease, monkeypatch):
    monkeypatch.setitem(sys.modules, "knotcord.inject", None)  # import 会抛 ImportError
    run(conn, lease, V(observations=[obs(title="没有 inject 也行")]), mk_ctx(conn))
    assert conn.execute("SELECT count(*) FROM notes WHERE title='没有 inject 也行'").fetchone()[0] == 1
