"""纠错统计：提炼提示词和解析认 correction 标签、笔记能查出来、同类归并、CLI 输出。"""

import json
import re
import time
from pathlib import Path


from knotcord import cli, corrections
from knotcord.core import index
from knotcord.core.lease import Lease
from knotcord.extract import codex, commit, deepseek, validate
from knotcord.extract.prompt import PromptCtx

EXTRACT = Path(codex.__file__).resolve().parent
NOW = 1_795_000_000
DAY = 86400


# ---------------------------------------------------------------- 提示词和解析（Codex、DeepSeek 两个通道共用 instr.md + schema.json）

def test_instr_asks_for_correction_notes():
    text = (EXTRACT / "instr.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- correction"))
    for must in ("tags 带 correction", "错在哪 → 应该怎样", "用户原话要点"):
        assert must in line
    assert len(line) <= 150  # 提示词只许多出这么一条（再加上 tags 枚举里的一个词）
    assert "trade-off、correction 里选" in text


def test_both_channels_carry_the_instruction_and_schema():
    assert Path(codex.INSTR_PATH) == EXTRACT / "instr.md" and Path(codex.SCHEMA_PATH) == EXTRACT / "schema.json"
    sys_text = deepseek._system_text()  # DeepSeek：指令 + schema 拼进 system
    assert "tags 带 correction" in sys_text and '"correction"' in sys_text
    schema = json.loads((EXTRACT / "schema.json").read_text(encoding="utf-8"))
    enum = schema["properties"]["observations"]["items"]["properties"]["tags"]["items"]["enum"]
    assert "correction" in enum and set(enum) == set(validate.TAGS)


def test_validate_keeps_correction_tag():
    obs = {"type": "decision", "title": "用 sed 改中文文件 → 只用 Edit", "body": "用户说乱码了", "facts": [], "files": [],
           "tags": ["correction", "乱写", "gotcha", "correction"], "slot": "", "supersedes": [], "source": []}
    v = validate.validate({"skip": False, "observations": [obs], "summary": {}, "state_card": {},
                           "memory_conflicts": [], "unfinished_new": [], "unfinished_resolved": []},
                          n_notes=0, n_unfinished=0, n_events=0)
    assert v.notes[0]["tags"] == ["correction", "gotcha"]  # 非法的丢、重复的合


# ---------------------------------------------------------------- 落库后能查出来（走真实的 提炼输出 -> 校验 -> commit_batch）

def _commit(conn, project, title, ts, tags=("correction",), body="用户原话要点"):
    le = Lease(conn, owner="me")
    assert le.try_acquire()
    out = json.dumps({"skip": False, "observations": [
        {"type": "decision", "title": title, "body": body, "facts": [], "files": [], "tags": list(tags),
         "slot": "", "supersedes": [], "source": [1]}],
        "summary": {}, "state_card": {}, "memory_conflicts": [], "unfinished_new": [], "unfinished_resolved": []})
    v = validate.validate(validate.parse_model_text(out), n_notes=0, n_unfinished=0, n_events=1)
    eid = conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind) VALUES('s',?,?,?, 'prompt')",
                       (project, ts, f"u{time.time_ns()}")).lastrowid
    ctx = PromptCtx(project=project)
    ctx.event_ids, ctx.event_ts = [eid], [ts]
    bid = conn.execute("INSERT INTO batches(session_id, created, status) VALUES('s',?, 'running')", (ts,)).lastrowid
    commit.commit_batch(conn, le, batch_id=bid, session_id="s", project=project, v=v, ctx=ctx, provider="codex",
                        model="m", tokens_in=1, tokens_out=1, cost_cny=0.0, now=ts)
    le.release()


def test_committed_correction_note_is_found(conn):
    _commit(conn, "ProjA", "用 sed 改中文文件 → 只用 Edit 或 Write", NOW - DAY)
    _commit(conn, "ProjA", "普通的发现", NOW - DAY, tags=("gotcha",))
    rows = corrections.query(conn, days=7, now=NOW)
    assert [r["title"] for r in rows] == ["用 sed 改中文文件 → 只用 Edit 或 Write"] and rows[0]["project"] == "ProjA"


# ---------------------------------------------------------------- 查询、归并、输出

def add(conn, project, title, ts, tags='["correction"]', state="active"):
    cur = conn.execute("INSERT INTO notes(project, ts, type, title, body, tags, state) VALUES(?,?,?,?,?,?,?)",
                       (project, ts, "decision", title, "", tags, state))
    index.index_doc(conn, "note", cur.lastrowid, project, ts, title, active=(state == "active"))
    return cur.lastrowid


def test_query_filters_by_tag_days_project(conn):
    a = add(conn, "ProjA", "把密钥写进了日志 → 入库前先脱敏", NOW - DAY)
    add(conn, "ProjA", "太久以前的纠正 → 随便", NOW - 30 * DAY)
    add(conn, "ProjB", "没问清就动手 → 先确认", NOW - 2 * DAY)
    add(conn, "ProjA", "别的标签", NOW - DAY, tags='["gotcha"]')
    add(conn, "ProjA", "标签里只是带 correction 字样的别的词", NOW - DAY, tags='["correction-ish"]')
    add(conn, "ProjA", "标签坏了", NOW - DAY, tags="correction")  # 不是合法 JSON 数组：不算
    all7 = corrections.query(conn, days=7, now=NOW)
    assert [(r["project"], r["id"]) for r in all7][0] == ("ProjA", a) and len(all7) == 2
    assert [r["project"] for r in corrections.query(conn, days=7, project="ProjB", now=NOW)] == ["ProjB"]
    assert len(corrections.query(conn, days=0, now=NOW)) == 3  # 0 = 不限
    assert corrections.query(conn, days=7, project="Nope", now=NOW) == []


def test_millisecond_timestamps_are_understood(conn):
    add(conn, "ProjA", "迁移来的毫秒时间 → 也要认", (NOW - DAY) * 1000)
    assert len(corrections.query(conn, days=7, now=NOW)) == 1


def test_similar_titles_cluster_within_project_only(conn):
    add(conn, "ProjA", "用 sed 改中文文件导致乱码 → 只用 Edit 或 Write", NOW - 3 * DAY)
    add(conn, "ProjA", "用 sed 改中文文件又乱码 → 只用 Edit 或 Write 改", NOW - 2 * DAY)
    add(conn, "ProjA", "没问清需求就直接动手 → 先复述再确认", NOW - DAY)
    add(conn, "ProjB", "用 sed 改中文文件导致乱码 → 只用 Edit 或 Write", NOW - DAY)  # 别的项目不并
    groups = corrections.cluster(corrections.query(conn, days=7, now=NOW), 0.7)
    sizes = sorted((g[0]["project"], len(g)) for g in groups)
    assert sizes == [("ProjA", 1), ("ProjA", 2), ("ProjB", 1)]


def test_superseded_duplicates_still_count(conn):
    # 每周整理会把相近的旧笔记标成 superseded；它们正是"又犯了一次"的证据
    add(conn, "ProjA", "把密钥写进日志 → 入库前先脱敏", NOW - 3 * DAY, state="superseded")
    add(conn, "ProjA", "把密钥写进日志 → 入库前先脱敏", NOW - DAY)
    out = corrections.render(conn, days=7, now=NOW)
    assert out.count("[同类 2 次]") == 2


def test_render_format(conn):
    n1 = add(conn, "ProjA", "用 sed 改中文文件导致乱码 → 只用 Edit 或 Write", NOW - 3 * DAY)
    n2 = add(conn, "ProjA", "用 sed 改中文文件又乱码 → 只用 Edit 或 Write 改", NOW - 2 * DAY)
    n3 = add(conn, "ProjB", "没问清需求就动手 → 先复述再确认", NOW - DAY)
    lines = corrections.render(conn, days=7, now=NOW).splitlines()
    assert lines[0] == "近 7 天纠错 3 条，2 类"
    assert lines[1] == "## ProjA" and lines[4] == "## ProjB"
    md = lambda t: time.strftime("%m-%d", time.localtime(t))  # noqa: E731
    assert lines[2] == f"n{n1} {md(NOW - 3 * DAY)} 用 sed 改中文文件导致乱码 → 只用 Edit 或 Write [同类 2 次]"
    assert re.fullmatch(rf"n{n2} \d\d-\d\d 用 sed 改中文文件又乱码.* \[同类 2 次\]", lines[3])
    assert lines[5] == f"n{n3} {md(NOW - DAY)} 没问清需求就动手 → 先复述再确认"  # 单条不带"同类"


def test_render_empty(conn):
    assert corrections.render(conn, days=7, now=NOW) == "无"


def test_cli_corrections(conn, capsys):
    now = time.time()
    add(conn, "ProjA", "把密钥写进日志 → 入库前先脱敏", int(now - DAY))
    add(conn, "ProjB", "没问清需求就动手 → 先复述再确认", int(now - 20 * DAY))
    assert cli.main(["corrections"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "近 7 天纠错 1 条，1 类" and "## ProjA" in out and "ProjB" not in out
    assert cli.main(["corrections", "--days", "30"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "近 30 天纠错 2 条，2 类"
    assert cli.main(["corrections", "--days", "30", "--project", "ProjB"]) == 0
    out = capsys.readouterr().out
    assert "## ProjB" in out and "ProjA" not in out
    assert cli.main(["corrections", "--project", "Nope"]) == 0
    assert capsys.readouterr().out.strip() == "无"


def test_cli_corrections_without_db(trace_home, capsys):
    assert cli.main(["corrections"]) == 0
    assert capsys.readouterr().out.strip() == "无"
    assert not (trace_home / "knotcord.db").exists()  # 只读命令不凭空建库
