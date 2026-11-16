"""consolidate：去重合并（slot / hash / 相似标题）、降权打分、晋升提名，以及挂在 janitor 每周项上。"""

import json
import time

from knotcord import consolidate, janitor
from knotcord.core import config, db, index
from knotcord.extract.commit import content_hash

DAY = 86400
P = "proj"


def make_cfg(**over):
    data = dict(config.DEFAULTS)
    data.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(data)


def note(conn, title, *, body="正文", ts=None, project=P, type_="discovery", slot=None, origin="knotcord",
         state="active", use_count=0, last_used=None, last_mentioned=None, chash="auto"):
    ts = ts if ts is not None else int(time.time())
    h = content_hash(project, title) if chash == "auto" else chash
    cur = conn.execute(
        "INSERT INTO notes(project, ts, type, title, body, slot, state, origin, content_hash, use_count, last_used, "
        "last_mentioned) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (project, ts, type_, title, body, slot, state, origin, h, use_count, last_used, last_mentioned))
    nid = cur.lastrowid
    index.index_doc(conn, "note", nid, project, ts, title + " " + body, active=(state == "active"))
    return nid


def row(conn, nid):
    return conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()


def doc_active(conn, nid):
    return conn.execute("SELECT active FROM docs WHERE src='note' AND src_id=?", (nid,)).fetchone()[0]


# ---------------------------------------------------------------- 去重合并

def test_same_slot_keeps_newest(conn):
    now = int(time.time())
    old = note(conn, "VPS 公网 IP 是甲", slot="vps1.ip", ts=now - 3 * DAY)
    mid = note(conn, "VPS 公网 IP 改成乙", slot="vps1.ip", ts=now - 2 * DAY)
    new = note(conn, "VPS 公网 IP 又改成丙", slot="vps1.ip", ts=now - DAY)
    other = note(conn, "另一个项目同 slot", slot="vps1.ip", project="other", ts=now - 5 * DAY)
    res = consolidate.run(conn, make_cfg(), now)
    assert res["superseded"] == 2 and res["by_rule"]["slot"] == 2
    for nid in (old, mid):
        r = row(conn, nid)
        assert r["state"] == "superseded" and r["superseded_by"] == new and r["superseded_at"] == int(now)
        assert doc_active(conn, nid) == 0  # 搜索默认看不到
    assert row(conn, new)["state"] == "active" and doc_active(conn, new) == 1
    assert row(conn, other)["state"] == "active"  # 跨项目不动
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 4  # 只标记，不删除


def test_same_hash_same_type_only(conn):
    now = int(time.time())
    a = note(conn, "同一个标题", ts=now - 2 * DAY, chash="H1", type_="discovery")
    b = note(conn, "同一个标题（重复入库）", ts=now - DAY, chash="H1", type_="discovery")
    c = note(conn, "同哈希不同类型", ts=now - 3 * DAY, chash="H1", type_="decision")
    consolidate.run(conn, make_cfg(), now)
    assert row(conn, a)["state"] == "superseded" and row(conn, a)["superseded_by"] == b
    assert row(conn, b)["state"] == "active" and row(conn, c)["state"] == "active"


def test_similar_titles_need_same_digits_and_similar_body(conn):
    now = int(time.time())
    body = "托盘图标在高 DPI 屏幕下显示模糊，改用多尺寸图标资源后恢复清晰。"
    a = note(conn, "修复托盘图标在高 DPI 下模糊的问题", body=body, ts=now - 2 * DAY)
    b = note(conn, "修复托盘图标在高DPI下模糊的问题（补充）", body=body + "已验证。", ts=now - DAY)
    # 标题几乎一样但数字不同：不同的事实
    v1 = note(conn, "Span 升级到 Xray 26.11.9 并通过虚拟机测试", body="b", ts=now - 4 * DAY)
    v2 = note(conn, "Span 升级到 Xray 26.11.8 并通过虚拟机测试", body="b", ts=now - 3 * DAY)
    # 标题相似但正文完全不同：不是重复
    d1 = note(conn, "修复截图在深色模式下崩溃的问题", body="空指针：渲染线程在窗口销毁后仍访问位图句柄。" * 3, ts=now - 6 * DAY)
    d2 = note(conn, "修复截图在浅色模式下崩溃的问题", body="死锁：保存对话框与剪贴板监听互相等待，调整加锁顺序。" * 3, ts=now - 5 * DAY)
    # 太短的标题不参与
    s1 = note(conn, "修复 bug", ts=now - 8 * DAY)
    s2 = note(conn, "修复 bug。", ts=now - 7 * DAY, chash="x")
    res = consolidate.run(conn, make_cfg(), now)
    assert res["by_rule"]["title"] == 1
    assert row(conn, a)["state"] == "superseded" and row(conn, a)["superseded_by"] == b
    for nid in (b, v1, v2, d1, d2, s1, s2):
        assert row(conn, nid)["state"] == "active", nid


def test_title_cluster_is_star_not_transitive(conn):
    now = int(time.time())
    # A~B、B~C 但 A 与 C 差得远：簇首 C（最新）只吃直接相似的 B，A 留着
    base = "配置托盘图标高分屏缩放策略的详细说明与完整验证记录整理和归档备查资料汇总"
    assert len(base) >= 30
    t = [base, "甲乙" + base[2:], "甲乙丙丁" + base[4:]]  # A~B、B~C 各差 2 字（0.93），A~C 差 4 字（0.87）
    sm = consolidate.difflib.SequenceMatcher
    assert sm(None, t[0], t[1]).ratio() >= 0.9 and sm(None, t[1], t[2]).ratio() >= 0.9
    assert sm(None, t[0], t[2]).ratio() < 0.9
    a = note(conn, t[0], ts=now - 3 * DAY, body="同")
    b = note(conn, t[1], ts=now - 2 * DAY, body="同")
    c = note(conn, t[2], ts=now - DAY, body="同")
    groups = consolidate.plan_dedupe(conn, make_cfg())
    assert [(g[1]["id"], [d["id"] for d in g[2]]) for g in groups] == [(c, [b])]
    consolidate.run(conn, make_cfg(), now)
    assert row(conn, a)["state"] == "active" and row(conn, b)["state"] == "superseded"


def test_legacy_untouched_by_default_and_opt_in(conn):
    now = int(time.time())
    a = note(conn, "旧库来的重复", origin="claude-mem", ts=now - 5 * DAY, chash="L1")
    b = note(conn, "旧库来的重复", origin="claude-mem", ts=now - 4 * DAY, chash="L1")
    res = consolidate.run(conn, make_cfg(), now)
    assert res["superseded"] == 0 and row(conn, a)["state"] == "active" and row(conn, b)["state"] == "active"
    res = consolidate.run(conn, make_cfg(consolidate__include_legacy=True), now)
    assert res["superseded"] == 1 and row(conn, a)["state"] == "superseded" and row(conn, b)["state"] == "active"


def test_unfinished_and_non_active_are_left_alone(conn):
    now = int(time.time())
    u1 = note(conn, "没做完的迁移", type_="unfinished", chash="U", ts=now - 2 * DAY)
    u2 = note(conn, "没做完的迁移", type_="unfinished", chash="U", ts=now - DAY)
    done = note(conn, "已解决的", state="resolved", chash="R", ts=now - 2 * DAY)
    again = note(conn, "已解决的", chash="R", ts=now - DAY)
    consolidate.run(conn, make_cfg(), now)
    assert row(conn, u1)["state"] == "active" and row(conn, u2)["state"] == "active"
    assert row(conn, done)["state"] == "resolved" and row(conn, again)["state"] == "active"


def test_use_count_moves_to_kept_note_and_second_run_is_noop(conn):
    now = int(time.time())
    old = note(conn, "重复项", chash="D", ts=now - 2 * DAY, use_count=3, last_used=now - 100)
    new = note(conn, "重复项", chash="D", ts=now - DAY, use_count=1)
    conn.execute("UPDATE docs SET use_count=3 WHERE src='note' AND src_id=?", (old,))
    conn.execute("UPDATE docs SET use_count=1 WHERE src='note' AND src_id=?", (new,))
    assert consolidate.run(conn, make_cfg(), now)["superseded"] == 1
    r = row(conn, new)
    assert r["use_count"] == 4 and r["last_used"] == now - 100
    assert conn.execute("SELECT use_count FROM docs WHERE src='note' AND src_id=?", (new,)).fetchone()[0] == 4
    assert consolidate.run(conn, make_cfg(), now)["superseded"] == 0  # 幂等


def test_rule_order_each_note_marked_once(conn):
    now = int(time.time())
    a = note(conn, "同 slot 又同哈希", slot="s", chash="X", ts=now - 2 * DAY)
    b = note(conn, "同 slot 又同哈希", slot="s", chash="X", ts=now - DAY)
    res = consolidate.run(conn, make_cfg(), now)
    assert res["superseded"] == 1 and res["by_rule"] == {"slot": 1, "hash": 0, "title": 0}
    assert row(conn, a)["superseded_by"] == b


def test_disabled_does_nothing(conn):
    now = int(time.time())
    a = note(conn, "x重复项目标题", chash="D", ts=now - 2 * DAY)
    note(conn, "x重复项目标题", chash="D", ts=now - DAY)
    assert consolidate.run(conn, make_cfg(consolidate__enabled=False), now) == {"enabled": False}
    assert row(conn, a)["state"] == "active"


# ---------------------------------------------------------------- 降权

def test_demotion_factor_uses_latest_of_three_times():
    now = 1_800_000_000
    cfg = make_cfg()
    old = now - 200 * DAY
    f = lambda ts, lm=None, lu=None: consolidate.demotion_factor(ts, lm, lu, now=now, cfg=cfg)
    assert f(old) == 0.8
    assert f(old, lm=now - 10 * DAY) == 1.0        # 最近被提及
    assert f(old, lu=now - 10 * DAY) == 1.0        # 最近被取用
    assert f(now - 30 * DAY) == 1.0                # 本来就新
    assert f(old * 1000) == 0.8                    # 毫秒时间戳也按秒算
    assert f(None) == 1.0                          # 没有时间信息不降权
    assert consolidate.demotion_factor(old, None, None, now=now, cfg=make_cfg(consolidate__stale_factor=0.5)) == 0.5


def test_note_factors_batch_only_returns_demoted(conn):
    now = time.time()
    stale = note(conn, "老笔记", ts=int(now - 200 * DAY))
    fresh = note(conn, "新笔记", ts=int(now - DAY))
    used = note(conn, "老但常用", ts=int(now - 200 * DAY), last_used=int(now - 2 * DAY), use_count=2)
    assert consolidate.note_factors(conn, [stale, fresh, used, 99999], now=now) == {stale: 0.8}
    assert consolidate.note_factors(conn, []) == {}
    assert consolidate.stale_count(conn, config.load(), now) == 1


# ---------------------------------------------------------------- 晋升提名

def test_nominate_threshold_cooldown_and_per_project(conn):
    now = int(time.time())
    hot = note(conn, "常被取用的坑", use_count=6)
    warm = note(conn, "只用了四次", use_count=4)
    other = note(conn, "别的项目常用", project="other", use_count=9)
    unf = note(conn, "常用但是没做完", type_="unfinished", use_count=9)
    sup = note(conn, "已取代的常用", state="superseded", use_count=9)
    out = consolidate.nominate(conn, make_cfg(), now)
    assert [i["id"] for i in out[P]] == [hot] and [i["id"] for i in out["other"]] == [other]
    assert json.loads(db.kv_get(conn, "promote:" + P))[0] == {"id": hot, "title": "常被取用的坑", "uses": 6}
    assert warm not in [i["id"] for v in out.values() for i in v]
    assert unf not in [i["id"] for v in out.values() for i in v] and sup not in [i["id"] for v in out.values() for i in v]
    # 冷却期内不再提名：上周的清单被换成空
    out2 = consolidate.nominate(conn, make_cfg(), now + 7 * DAY)
    assert out2 == {} and db.kv_get(conn, "promote:" + P) is None
    # 冷却期过后又能提名
    out3 = consolidate.nominate(conn, make_cfg(), now + 31 * DAY)
    assert [i["id"] for i in out3[P]] == [hot]


def test_nominate_respects_max_per_project(conn):
    ids = [note(conn, f"常用笔记第{i}号", use_count=10 + i) for i in range(4)]
    out = consolidate.nominate(conn, make_cfg(consolidate__promote_max=2), int(time.time()))
    assert [i["id"] for i in out[P]] == [ids[3], ids[2]]  # 取用次数多的优先


# ---------------------------------------------------------------- 挂在 janitor 每周项上

def test_janitor_weekly_runs_consolidate(trace_home, conn):
    now = time.time()
    a = note(conn, "每周去重用的重复项", chash="W", ts=int(now - 2 * DAY))
    b = note(conn, "每周去重用的重复项", chash="W", ts=int(now - DAY))
    res = janitor.run(conn, config.load(), now=now, memroot=str(trace_home / "nomem"))
    assert res["consolidate"]["superseded"] == 1
    assert row(conn, a)["state"] == "superseded" and row(conn, b)["state"] == "active"
    assert json.loads(db.kv_get(conn, consolidate.KEY_LAST))["ids"] == [a]
    # 一周内再跑：每周项（含整理）不重复
    res2 = janitor.run(conn, config.load(), now=now + 7 * 3600, memroot=str(trace_home / "nomem"))
    assert "tmp_removed" in res2 and "consolidate" not in res2
    assert (trace_home / "backups" / "knotcord.db").exists()  # 备份在整理之前完成


def test_janitor_weekly_survives_consolidate_failure(trace_home, conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("整理炸了")
    monkeypatch.setattr(consolidate, "run", boom)
    res = janitor.run(conn, config.load(), now=time.time(), memroot=str(trace_home / "nomem"))
    assert "整理炸了" in res["consolidate_error"] and res["quick_check"] == "ok"
    assert db.kv_get(conn, janitor.KEY_WEEKLY) is not None  # 每周项照常收尾
