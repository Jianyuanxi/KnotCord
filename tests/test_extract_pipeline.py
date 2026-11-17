"""提炼流水线测试：阈值、切批、落库、失败重试、通道切换、上限、租约丢失、熔断。
模型通道全部用假实现注入（monkeypatch codex.call / deepseek.call），不真调模型。"""

import json
import sys
import time
import types

import pytest

from knotcord.core import config, db, paths
from knotcord.core.lease import Lease
from knotcord.extract import budget, codex, deepseek, pipeline
from knotcord.extract.errors import ChannelUnavailable, ModelError, ModelResult

NOW = 1_795_000_000  # 2026-11 中旬某个时刻


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = [NOW]
    monkeypatch.setattr(pipeline, "_now", lambda: t[0])
    monkeypatch.setattr(budget, "_now", lambda: t[0])
    budget.reset_breaker()
    yield t
    budget.reset_breaker()


@pytest.fixture
def injected(monkeypatch):
    """假的 knotcord.inject：记录 render 调用。"""
    calls = []
    mod = types.ModuleType("knotcord.inject")
    mod.render = lambda conn, project: calls.append(project)
    monkeypatch.setitem(sys.modules, "knotcord.inject", mod)
    return calls


def mkcfg(**over):
    d = config.load().as_dict()
    d.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(d)


@pytest.fixture
def lease(conn):
    le = Lease(conn, owner="me")
    assert le.try_acquire()
    return le


def add_session(conn, sid="s1", project="proj", last_seen=NOW):
    conn.execute("INSERT OR REPLACE INTO sessions(id, client, project, cwd, last_seen) VALUES(?,?,?,?,?)",
                 (sid, "claude", project, "/x", last_seen))


def add_events(conn, sid="s1", turns=2, text_len=100, ts0=NOW - 100, project="proj", tag=""):
    """每个回合 1 条提示 + 1 条工具 + 1 条回答，共 turns*3 条。"""
    i = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    ids = []
    for t in range(turns):
        for kind, tool in (("prompt", None), ("tool", "Bash"), ("assistant", None)):
            i += 1
            cur = conn.execute(
                "INSERT INTO events(session_id, project, ts, uuid, kind, tool, brief, body) VALUES(?,?,?,?,?,?,?,?)",
                (sid, project, ts0 + i, f"{sid}-{tag}{i}", kind, tool,
                 "Edit db.py" if kind == "tool" else f"{kind} {i}", ("字" * text_len) if kind != "tool" else ""))
            ids.append(cur.lastrowid)
    return ids


def good(**over):
    out = {
        "skip": False,
        "observations": [{"type": "decision", "title": "选用 SQLite", "body": "定了用 SQLite 存数据",
                          "facts": ["sqlite 3.51"], "files": ["db.py"], "tags": ["gotcha"], "slot": "",
                          "supersedes": [], "source": [1]}],
        "summary": {"request": "做存储", "completed": "选了 SQLite", "learned": "", "next_steps": "写代码"},
        "state_card": {"goal": "做 KnotCord", "status": "选型完成", "decisions": ["SQLite"], "pitfalls": [],
                       "next": ["写代码"]},
        "memory_conflicts": [], "unfinished_new": ["把迁移脚本写完"], "unfinished_resolved": [],
    }
    out.update(over)
    return json.dumps(out, ensure_ascii=False)


class FakeChannel:
    """假的 call：按顺序吐出预设的结果或异常，记录每次收到的 prompt。"""

    def __init__(self, *script, provider="codex"):
        self.script = list(script)
        self.last = None  # 预设用完后重复最后一个
        self.provider = provider
        self.prompts = []
        self.tmp_dirs = []

    def __call__(self, prompt, cfg, *, tmp_dir, on_tick=None, stop_event=None):
        import os
        self.prompts.append(prompt)
        self.tmp_dirs.append(tmp_dir)
        assert os.path.isdir(tmp_dir)  # 调用时临时目录已存在
        if self.script:
            self.last = self.script.pop(0)
        item = self.last
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(on_tick)
        return ModelResult(text=item, provider=self.provider, tokens_in=1000, tokens_out=200,
                           cost_cny=0.0 if self.provider == "codex" else 0.01, model="m")


def install(monkeypatch, codex_ch=None, ds_ch=None):
    if codex_ch is not None:
        monkeypatch.setattr(codex, "call", codex_ch)
    if ds_ch is not None:
        monkeypatch.setattr(deepseek, "call", ds_ch)


# ------------------------------------------------------------------ 阈值

def test_due_below_threshold_not_due(conn, clock):
    add_session(conn)
    add_events(conn, turns=2, text_len=100)  # 约 600 字、2 个回合
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []


def test_due_by_chars(conn):
    add_session(conn)
    add_events(conn, turns=2, text_len=8000)  # 约 3.2 万字
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]


def test_due_by_turns(conn):
    add_session(conn)
    add_events(conn, turns=8, text_len=10)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]
    conn.execute("DELETE FROM events")
    add_events(conn, turns=7, text_len=10)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []


def test_due_by_idle(conn):
    add_session(conn, last_seen=NOW - 16 * 60)
    add_events(conn, turns=1, text_len=10, ts0=NOW - 20 * 60)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]
    # 14 分钟前还有活动：不算空闲
    conn.execute("UPDATE sessions SET last_seen=?", (NOW - 14 * 60,))
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []


def test_idle_without_unextracted_content_not_due(conn):
    add_session(conn, last_seen=NOW - 3600)
    ids = add_events(conn, turns=1, text_len=10, ts0=NOW - 7200)
    conn.execute("INSERT INTO batches(id, session_id, created, status) VALUES(1,'s1',?, 'done')", (NOW,))
    conn.execute("UPDATE events SET batch_id=1")
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []


def test_threshold_follows_current_channel(conn):
    add_session(conn)
    add_events(conn, turns=2, text_len=3500)  # 约 1.4 万字：过 DeepSeek 阈值 1.2 万，没过 Codex 的 3 万
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []
    db.kv_set(conn, "codex_cooldown_until", NOW + 3600)  # Codex 冷却中 -> 用 deepseek 的阈值
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]
    db.kv_set(conn, "codex_cooldown_until", NOW - 1)  # 冷却到期：现比，不需要"恢复"
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []
    # 回合数阈值同理：4 回合
    conn.execute("DELETE FROM events")
    add_events(conn, turns=4, text_len=10)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []
    db.kv_set(conn, "codex_cooldown_until", NOW + 3600)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]


def test_due_empty_when_both_channels_cooling_or_over_budget(conn):
    add_session(conn)
    add_events(conn, turns=8, text_len=10)
    db.kv_set(conn, "codex_cooldown_until", NOW + 3600)
    db.kv_set(conn, "deepseek_cooldown_until", NOW + 3600)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []
    db.kv_set(conn, "deepseek_cooldown_until", 0)
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == ["s1"]
    assert pipeline.due_sessions(conn, mkcfg(budget__daily_calls=0), now=NOW) == []


def test_due_failed_batch_after_next_try(conn):
    add_session(conn)
    add_events(conn, turns=1, text_len=10)  # 远低于任何阈值
    conn.execute("INSERT INTO batches(id, session_id, created, status, attempts, next_try) "
                 "VALUES(1,'s1',?, 'failed', 1, ?)", (NOW - 700, NOW + 300))
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []          # 还在退避
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW + 301) == ["s1"]  # 到点就该重试


def test_failed_batch_in_backoff_blocks_even_when_over_threshold(conn, lease, monkeypatch):
    add_session(conn)
    add_events(conn, turns=9, text_len=10)
    conn.execute("INSERT INTO batches(id, session_id, created, status, attempts, next_try) "
                 "VALUES(1,'s1',?, 'failed', 1, ?)", (NOW - 700, NOW + 300))
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW) == []
    ch = FakeChannel(good())
    install(monkeypatch, ch)
    assert pipeline.run_session(conn, "s1", mkcfg(), lease, flush=True) == 0  # flush 也不强行重试
    assert ch.prompts == []


# ------------------------------------------------------------------ 切批

def test_cut_at_turn_boundary_multiple_batches(conn, lease, monkeypatch, injected):
    add_session(conn)
    ids = add_events(conn, turns=6, text_len=1000)  # 每回合约 2100 字
    ch = FakeChannel(good(observations=[], unfinished_new=[], state_card={
        "goal": "", "status": "", "decisions": [], "pitfalls": [], "next": []},
        summary={"request": "", "completed": "", "learned": "", "next_steps": ""}))
    install(monkeypatch, ch)
    n = pipeline.run_session(conn, "s1", mkcfg(extract__max_chars=5000), lease, flush=True)
    assert n == 3  # 每批 2 个回合
    assert len(ch.prompts) == 3
    for p in ch.prompts:
        body = p.split("# 本批操作记录\n", 1)[1]
        first = body.splitlines()[0]
        assert "用户:" in first  # 每批都从用户提示开始（回合边界）
        assert body.count("用户:") == 2
    # 全部事件都有批号，三批各自一个号
    batches = {r[0] for r in conn.execute("SELECT batch_id FROM events")}
    assert len(batches) == 3 and None not in batches
    assert conn.execute("SELECT count(*) FROM batches WHERE status='done'").fetchone()[0] == 3


def test_single_huge_turn_is_hard_cut(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=1, text_len=30000)  # 一个回合里有两条各 3 万字的长文本
    ch = FakeChannel(good(observations=[]))
    install(monkeypatch, ch)
    n = pipeline.run_session(conn, "s1", mkcfg(extract__max_chars=40000), lease, flush=True)
    assert n == 2  # 没有回合边界可切，在超长处硬切，剩下的下一批


# ------------------------------------------------------------------ 正常落库

def test_happy_path_commits_everything(conn, lease, monkeypatch, injected):
    add_session(conn)
    ids = add_events(conn, turns=8, text_len=50)
    ch = FakeChannel(good(memory_conflicts=[]))
    install(monkeypatch, ch)
    cfg = mkcfg()
    assert pipeline.run_session(conn, "s1", cfg, lease) == 1
    # notes
    n = conn.execute("SELECT * FROM notes WHERE type='decision'").fetchone()
    assert n["title"] == "选用 SQLite" and n["state"] == "active" and n["project"] == "proj"
    assert json.loads(n["source_events"]) == [ids[0]]
    assert json.loads(n["tags"]) == ["gotcha"] and n["slot"] is None and n["model"] == "m"
    u = conn.execute("SELECT * FROM notes WHERE type='unfinished'").fetchone()
    assert u["state"] == "active" and "迁移脚本" in u["title"]
    # summaries 一个会话一行
    s = conn.execute("SELECT * FROM summaries").fetchall()
    assert len(s) == 1 and s[0]["session_id"] == "s1" and s[0]["completed"] == "选了 SQLite"
    # 现状卡
    card = conn.execute("SELECT * FROM state_cards").fetchall()
    assert len(card) == 1 and json.loads(card[0]["card"])["goal"] == "做 KnotCord"
    # 索引
    srcs = {r[0] for r in conn.execute("SELECT src FROM docs")}
    assert srcs == {"note", "summary"}
    from knotcord.core import index
    assert index.fts_search(conn, "SQLite", project="proj", srcs=("note",))
    # 事件打上批号，批记账
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NULL").fetchone()[0] == 0
    b = conn.execute("SELECT * FROM batches").fetchall()
    assert len(b) == 1 and b[0]["status"] == "done" and b[0]["provider"] == "codex"
    assert b[0]["tokens_in"] == 1000 and b[0]["tokens_out"] == 200 and b[0]["attempts"] == 0
    assert b[0]["id"] == card[0]["batch_id"]
    # 用量、注入、临时目录
    assert budget.usage_get(conn, NOW)["calls"] == 1
    assert injected == ["proj"]
    import os
    assert not os.path.exists(ch.tmp_dirs[0])
    assert list(paths.tmp_dir().iterdir()) == []


def test_below_threshold_does_nothing_unless_flush(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=2, text_len=50)
    ch = FakeChannel(good())
    install(monkeypatch, ch)
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 0 and ch.prompts == []
    assert pipeline.run_session(conn, "s1", mkcfg(), lease, flush=True) == 1


def test_second_batch_updates_summary_and_does_not_duplicate_notes(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=50, tag="a")
    ch = FakeChannel(good())
    install(monkeypatch, ch)
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 1
    add_events(conn, turns=8, text_len=50, tag="b")
    ch2 = FakeChannel(good(summary={"request": "r2", "completed": "c2", "learned": "", "next_steps": ""}))
    install(monkeypatch, ch2)
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 1
    assert conn.execute("SELECT count(*) FROM summaries").fetchone()[0] == 1
    assert conn.execute("SELECT completed FROM summaries").fetchone()[0] == "c2"
    # 模型又吐了同标题的笔记和同样的没做完的事：不重复
    assert conn.execute("SELECT count(*) FROM notes WHERE type='decision'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notes WHERE type='unfinished'").fetchone()[0] == 1
    # 现状卡没变：不插新行
    assert conn.execute("SELECT count(*) FROM state_cards").fetchone()[0] == 1
    # 第二批的 prompt 里带着上一批的总结、现状卡、相关笔记和没做完的事
    p = ch2.prompts[0]
    assert "选了 SQLite" in p and "做 KnotCord" in p and "选用 SQLite" in p and "把迁移脚本写完" in p


# ------------------------------------------------------------------ 失败与重试

def test_exception_in_call_leaves_no_partial_data_and_rerun_has_no_duplicates(conn, lease, monkeypatch, injected, clock):
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    ch = FakeChannel(RuntimeError("boom"), good())
    install(monkeypatch, ch)
    cfg = mkcfg()
    assert pipeline.run_session(conn, "s1", cfg, lease) == 0
    for t in ("notes", "summaries", "state_cards", "docs"):
        assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NOT NULL").fetchone()[0] == 0
    b = conn.execute("SELECT * FROM batches").fetchone()
    assert b["status"] == "failed" and b["attempts"] == 1 and b["next_try"] == NOW + 600
    # 退避期内不重试
    assert pipeline.run_session(conn, "s1", cfg, lease) == 0 and len(ch.prompts) == 1
    clock[0] = NOW + 601
    assert pipeline.run_session(conn, "s1", cfg, lease) == 1
    assert conn.execute("SELECT count(*) FROM notes WHERE type='decision'").fetchone()[0] == 1
    rows = conn.execute("SELECT * FROM batches").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "done" and rows[0]["attempts"] == 1  # 认领原来的行


def test_commit_failure_rolls_back_whole_batch(conn, lease, monkeypatch, injected, clock):
    from knotcord.extract import commit
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    install(monkeypatch, FakeChannel(good()))
    real = commit._summary
    monkeypatch.setattr(commit, "_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("提交途中炸了")))
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 0
    # 笔记已经 INSERT 过但事务回滚：库里什么都没有
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "failed"
    monkeypatch.setattr(commit, "_summary", real)
    clock[0] = NOW + 601
    install(monkeypatch, FakeChannel(good()))
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 1
    assert conn.execute("SELECT count(*) FROM notes WHERE type='decision'").fetchone()[0] == 1


def test_invalid_json_counts_one_failure_then_dead_after_three(conn, lease, monkeypatch, injected, clock):
    add_session(conn)
    ids = add_events(conn, turns=8, text_len=50)
    ch = FakeChannel("这不是 JSON，抱歉")
    install(monkeypatch, ch)
    cfg = mkcfg()
    assert pipeline.run_session(conn, "s1", cfg, lease) == 0
    b = conn.execute("SELECT * FROM batches").fetchone()
    assert (b["status"], b["attempts"], b["next_try"]) == ("failed", 1, NOW + 600)
    clock[0] = NOW + 601
    assert pipeline.run_session(conn, "s1", cfg, lease) == 0
    b = conn.execute("SELECT * FROM batches").fetchone()
    assert (b["status"], b["attempts"], b["next_try"]) == ("failed", 2, NOW + 601 + 3600)
    clock[0] = NOW + 601 + 3601
    assert pipeline.run_session(conn, "s1", cfg, lease) == 0
    b = conn.execute("SELECT * FROM batches").fetchone()
    assert b["status"] == "dead" and b["attempts"] == 3
    # dead：事件带上批号，不再被选中，也不再调模型
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id=?", (b["id"],)).fetchone()[0] == len(ids)
    n_calls = len(ch.prompts)
    assert pipeline.due_sessions(conn, cfg, now=clock[0] + 10**6) == []
    assert pipeline.run_session(conn, "s1", cfg, lease, flush=True) == 0
    assert len(ch.prompts) == n_calls == 3
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_timeout_call_still_counts_toward_daily_calls(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    install(monkeypatch, FakeChannel(ModelError("codex 超时")))
    pipeline.run_session(conn, "s1", mkcfg(), lease)
    u = budget.usage_get(conn, NOW)
    assert u["calls"] == 1 and u["codex"] == 1 and u["cny"] == 0


# ------------------------------------------------------------------ 通道

def test_quota_error_switches_to_deepseek_without_counting_failure(conn, lease, monkeypatch, injected, tmp_path):
    """用真实的 codex.call（假的 codex 进程吐出 11-15 的真实报错），确认解析、冷却、切换。"""
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json,sys\n"
        "sys.stdin.read()\n"
        "m = 'You\\u2019ve hit your usage limit. Upgrade to Plus to continue using Codex "
        "(https://chatgpt.com/explore/plus), or try again at Dec 12th, 2026 5:25 AM.'\n"
        "print(json.dumps({'type':'thread.started','thread_id':'x'}))\n"
        "print(json.dumps({'type':'error','message':m}))\n"
        "print(json.dumps({'type':'turn.failed','error':{'message':m}}))\n"
        "sys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(codex, "_command", lambda: [sys.executable, str(script)])
    ds = FakeChannel(good(), provider="deepseek")
    install(monkeypatch, None, ds)
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    cfg = mkcfg()
    assert pipeline.run_session(conn, "s1", cfg, lease) == 1
    b = conn.execute("SELECT * FROM batches").fetchone()
    assert b["status"] == "done" and b["provider"] == "deepseek" and b["attempts"] == 0
    until = float(db.kv_get(conn, "codex_cooldown_until"))
    assert until == time.mktime((2026, 12, 12, 5, 25, 0, 0, 0, -1))  # 12-12 05:25
    assert len(ds.prompts) == 1
    u = budget.usage_get(conn, NOW)
    assert u["calls"] == 1 and u["deepseek"] == 1 and u["codex"] == 0  # 额度报错不占当天次数
    # 冷却期内再来一批：不再碰 Codex，直接 DeepSeek（codex 脚本被换成会失败的假实现来证明）
    monkeypatch.setattr(codex, "call", lambda *a, **k: pytest.fail("冷却期内不该调 codex"))
    add_events(conn, turns=8, text_len=50, tag="z")
    ds.script = [good(observations=[], unfinished_new=[])]
    assert pipeline.run_session(conn, "s1", cfg, lease) == 1


def test_both_channels_unavailable_stops_and_keeps_events(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    install(monkeypatch,
            FakeChannel(ChannelUnavailable(None, "额度")),
            FakeChannel(ChannelUnavailable(None, "DeepSeek 限流"), provider="deepseek"))
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 0  # 没失败过，不留批行
    # Codex 没解析出时间：冷却 1 小时；DeepSeek 默认 10 分钟
    assert float(db.kv_get(conn, "codex_cooldown_until")) == NOW + 3600
    assert float(db.kv_get(conn, "deepseek_cooldown_until")) == NOW + 600
    assert budget.usage_get(conn, NOW)["calls"] == 0
    assert pipeline.due_sessions(conn, mkcfg(), now=NOW + 1) == []


# ------------------------------------------------------------------ 上限、租约、脱敏、熔断

def test_daily_limit_blocks_model_calls(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    ch = FakeChannel(good())
    install(monkeypatch, ch)
    cfg = mkcfg(budget__daily_calls=2)
    budget.record(conn, "codex", now=NOW)
    budget.record(conn, "codex", now=NOW)
    assert not budget.can_call(conn, cfg, NOW)
    assert pipeline.run_session(conn, "s1", cfg, lease, flush=True) == 0
    assert ch.prompts == []
    assert pipeline.due_sessions(conn, cfg, now=NOW) == []
    # 花费上限同理
    cfg2 = mkcfg(budget__daily_cny=0.5)
    budget.record(conn, "deepseek", cost_cny=0.6, now=NOW)
    assert pipeline.run_session(conn, "s1", cfg2, lease, flush=True) == 0 and ch.prompts == []


def test_lease_lost_aborts_without_commit(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    monkeypatch.setattr(lease, "renew", lambda: False)  # 别人接手了

    def slow(on_tick):
        on_tick()  # run_child 等待期间每 5 秒会调；这里模拟调一次
        pytest.fail("on_tick 应该已经抛出 LeaseLost")

    install(monkeypatch, FakeChannel(slow))
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 0
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 0  # 新插的行已撤销，不算失败
    assert injected == []


def test_lease_lost_between_call_and_commit(conn, lease, monkeypatch, injected):
    """调完模型、落库前租约被别人接手：落库事务第一句发现，整批放弃。"""
    add_session(conn)
    add_events(conn, turns=8, text_len=50)

    def steal(on_tick):
        conn.execute("UPDATE kv SET value=? WHERE key='lease'",
                     (json.dumps({"owner": "other", "expires": time.time() + 60}),))
        return ModelResult(text=good(), provider="codex", tokens_in=1, tokens_out=1)

    install(monkeypatch, FakeChannel(steal))
    assert pipeline.run_session(conn, "s1", mkcfg(), lease) == 0
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE batch_id IS NOT NULL").fetchone()[0] == 0


def test_stop_event_stops_before_calling(conn, lease, monkeypatch, injected):
    import threading
    add_session(conn)
    add_events(conn, turns=8, text_len=50)
    ch = FakeChannel(good())
    install(monkeypatch, ch)
    ev = threading.Event()
    ev.set()
    assert pipeline.run_session(conn, "s1", mkcfg(), lease, flush=True, stop_event=ev) == 0
    assert ch.prompts == []


def test_secrets_never_reach_the_prompt(conn, lease, monkeypatch, injected):
    add_session(conn)
    add_events(conn, turns=8, text_len=20)
    conn.execute("UPDATE events SET body='my key sk-abcdefghijklmnopqrstuvwxyz0123456789 and password=hunter2xyz' "
                 "WHERE kind='prompt' AND id=(SELECT min(id) FROM events)")
    ch = FakeChannel(good(observations=[]))
    install(monkeypatch, ch)
    pipeline.run_session(conn, "s1", mkcfg(), lease)
    p = ch.prompts[0]
    assert "sk-abcdefghijklmnop" not in p and "hunter2xyz" not in p
    assert "[REDACTED:sk_key]" in p


def test_circuit_breaker_stops_after_five_consecutive_failures(conn, lease, monkeypatch, injected):
    ch = FakeChannel(ModelError("坏了"))
    install(monkeypatch, ch)
    cfg = mkcfg()
    for i in range(1, 8):
        add_session(conn, f"s{i}")
        add_events(conn, f"s{i}", turns=8, text_len=10)
    for i in range(1, 8):
        pipeline.run_session(conn, f"s{i}", cfg, lease)
    assert len(ch.prompts) == 5  # 第 6、7 个会话没有再调
    assert budget.breaker_open(NOW)
    assert pipeline.due_sessions(conn, cfg, now=NOW) == []
    # 6 小时后恢复
    assert not budget.breaker_open(NOW + 6 * 3600 + 1)


def test_success_resets_breaker_counter(conn, lease, monkeypatch, injected):
    for i in range(1, 5):
        add_session(conn, f"s{i}")
        add_events(conn, f"s{i}", turns=8, text_len=10)
    install(monkeypatch, FakeChannel(ModelError("x"), ModelError("x"), good(observations=[]), ModelError("x")))
    cfg = mkcfg()
    for i in range(1, 5):
        pipeline.run_session(conn, f"s{i}", cfg, lease)
    assert not budget.breaker_open(NOW)


def test_related_notes_and_unfinished_are_numbered_in_prompt(conn, lease, monkeypatch, injected):
    from knotcord.core import index
    add_session(conn)
    ids = add_events(conn, turns=8, text_len=20)
    conn.execute("UPDATE events SET brief='Edit pipeline.py' WHERE kind='tool'")
    n1 = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES('proj',?, 'discovery', "
                      "'pipeline.py 的切批逻辑', '按回合切', 'active')", (NOW - 5000,)).lastrowid
    index.index_doc(conn, "note", n1, "proj", NOW - 5000, "pipeline.py 的切批逻辑 按回合切")
    u1 = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES('proj',?, 'unfinished', "
                      "'写完 pipeline.py 的测试', '', 'active')", (NOW - 5000,)).lastrowid
    index.index_doc(conn, "note", u1, "proj", NOW - 5000, "写完 pipeline.py 的测试")
    # 别的项目的笔记不该出现
    n2 = conn.execute("INSERT INTO notes(project, ts, type, title, body, state) VALUES('other',?, 'discovery', "
                      "'pipeline.py 别的项目', '', 'active')", (NOW - 5000,)).lastrowid
    index.index_doc(conn, "note", n2, "other", NOW - 5000, "pipeline.py 别的项目")
    ch = FakeChannel(good(observations=[{"type": "change", "title": "新", "body": "", "facts": [], "files": [],
                                         "tags": [], "slot": "", "supersedes": [1, 5], "source": [1, 99]}],
                          unfinished_resolved=[1, 3], unfinished_new=[]))
    install(monkeypatch, ch)
    pipeline.run_session(conn, "s1", mkcfg(), lease)
    p = ch.prompts[0]
    assert "[1] discovery" in p and "pipeline.py 的切批逻辑" in p and "别的项目" not in p
    assert "[1] " in p.split("# 没做完的事")[1] and "写完 pipeline.py 的测试" in p
    # 编号 1 -> n1 被取代；5 越界丢弃；unfinished 1 -> u1 resolved；3 越界丢弃
    assert conn.execute("SELECT state, superseded_by FROM notes WHERE id=?", (n1,)).fetchone()[0] == "superseded"
    assert conn.execute("SELECT state FROM notes WHERE id=?", (u1,)).fetchone()[0] == "resolved"
    new = conn.execute("SELECT * FROM notes WHERE title='新'").fetchone()
    assert json.loads(new["source_events"]) == [ids[0]]  # 99 越界被丢，只剩序号 1
    assert conn.execute("SELECT active FROM docs WHERE src='note' AND src_id=?", (n1,)).fetchone()[0] == 0
