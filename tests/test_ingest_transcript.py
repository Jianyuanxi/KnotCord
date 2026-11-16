"""采集：用造出来的小 transcript 覆盖细则 2.3 的每一条，再挑真实 transcript 只读跑一遍。"""

import json
import os
import time
from pathlib import Path

import pytest

from knotcord.core import db, index, log
from knotcord.ingest import claude_transcript as ct

SID = "sess-0001"


# ---------------------------------------------------------------- 造数据

def J(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Builder:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.n = 0
        self.t = 1_789_000_000  # 2026-09 前后的 epoch 秒

    def _base(self, typ: str, uuid: str | None = None, **extra) -> dict:
        self.n += 1
        self.t += 5
        d = {"parentUuid": None, "isSidechain": False, "type": typ, "uuid": uuid or f"u{self.n:04d}",
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t)),
             "cwd": self.cwd, "sessionId": SID}
        d.update(extra)
        return d

    def prompt(self, text, uuid=None, **extra) -> str:
        return J(self._base("user", uuid, message={"role": "user", "content": text}, **extra))

    def asst_text(self, text, stop="end_turn", uuid=None, **extra) -> str:
        msg = {"model": "claude-opus-5", "id": f"msg{self.n}", "role": "assistant", "stop_reason": stop,
               "content": [{"type": "text", "text": text}]}
        return J(self._base("assistant", uuid, message=msg, **extra))

    def tool_use(self, tid, name, inp, uuid=None) -> str:
        msg = {"model": "claude-opus-5", "id": f"msg{self.n}", "role": "assistant", "stop_reason": "tool_use",
               "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}
        return J(self._base("assistant", uuid, message=msg))

    def tool_result(self, tid, content, is_error=False, uuid=None) -> str:
        blk = {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}
        return J(self._base("user", uuid, message={"role": "user", "content": [blk]}))


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "myproj"
    (p / ".git").mkdir(parents=True)
    return str(p)


def write_lines(path: Path, lines: list[str], mode="w"):
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        for ln in lines:
            f.write(ln + "\n")


def standard_session(b: Builder) -> list[str]:
    return [
        '{"type":"file-history-snapshot","messageId":"x"}',
        b.prompt("帮我看看 3x-ui 面板端口"),
        b.asst_text("我先查一下", stop="tool_use"),                       # 中间的话：不记
        b.tool_use("tu1", "Bash", {"command": "ls /etc", "description": "列目录"}),   # 命令前缀被过滤
        b.tool_result("tu1", "a\nb"),
        b.tool_use("tu2", "Read", {"file_path": "x"}),                   # 工具被过滤
        b.tool_result("tu2", "文件内容 " * 100),
        b.tool_use("tu3", "Bash", {"command": "docker ps", "description": "看容器"}),
        b.tool_result("tu3", "Exit code 1\nCONTAINER ID", is_error=True),
        b.tool_use("tu4", "Edit", {"file_path": "C:\\p\\a.py", "old_string": "a", "new_string": "b"}),
        b.tool_result("tu4", "The file has been updated successfully."),
        b.asst_text("端口是 2053。", stop="end_turn"),
        '{"type":"attachment","attachment":{"type":"hook","content":"<knotcord-context>old</knotcord-context>"}}',
    ]


def events(conn):
    return [dict(r) for r in conn.execute(
        "SELECT session_id, project, ts, uuid, kind, tool, brief, body, src_line FROM events ORDER BY ts, uuid")]


def kinds(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT kind, count(*) FROM events GROUP BY kind")}


def new_conn(tmp_path, monkeypatch, name):
    monkeypatch.setenv("KNOTCORD_HOME", str(tmp_path / name))
    return db.connect()


# ---------------------------------------------------------------- 基本规则

def test_basic_kinds_and_fields(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    write_lines(p, standard_session(b))
    n = ct.ingest_session(conn, SID, str(p))
    assert n == 4  # prompt + docker ps + Edit + 最终回答
    assert kinds(conn) == {"prompt": 1, "assistant": 1, "tool": 2}
    ev = {r["kind"] + (":" + r["tool"] if r["tool"] else ""): r for r in events(conn)}
    assert ev["prompt"]["body"] == "帮我看看 3x-ui 面板端口"
    assert ev["assistant"]["body"] == "端口是 2053。"
    dk = ev["tool:Bash"]
    assert dk["uuid"] == "tu3" and dk["brief"] == "看容器"
    assert dk["body"].startswith("$ docker ps") and "=> [exit 1]\nCONTAINER ID" in dk["body"]
    assert ev["tool:Edit"]["brief"] == "C:\\p\\a.py" and ev["tool:Edit"]["body"].endswith("=> ok")
    assert all(isinstance(r["ts"], int) for r in events(conn))
    assert all(r["project"] == "myproj" for r in events(conn))
    assert all(isinstance(r["src_line"], int) and r["src_line"] >= 2 for r in events(conn))
    # 中间的话、被过滤的工具、attachment 里的注入都不在库里
    bodies = "\n".join(r["body"] or "" for r in events(conn))
    assert "我先查一下" not in bodies and "文件内容" not in bodies and "knotcord-context" not in bodies


def test_session_row_and_prompt_indexed(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    lines = standard_session(b)
    write_lines(p, lines)
    ct.ingest_session(conn, SID, str(p))
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (SID,)).fetchone()
    assert s["client"] == "claude" and s["project"] == "myproj" and s["cwd"] == proj
    assert s["read_offset"] == os.path.getsize(p) and s["transcript_path"] == str(p)
    assert s["started_at"] <= s["last_seen"] and s["last_seen"] > 1_789_000_000
    pid = conn.execute("SELECT id FROM events WHERE kind='prompt'").fetchone()[0]
    hits = index.fts_search(conn, "面板端口", project="myproj")
    assert [(h["src"], h["src_id"]) for h in hits] == [("prompt", pid)]


def test_cwd_param_overrides_line_cwd_for_project(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    write_lines(p, standard_session(Builder(proj)))
    ct.ingest_session(conn, SID, str(p), cwd=str(other))
    assert {r["project"] for r in events(conn)} == {"other"}


@pytest.mark.parametrize("extra,text", [
    ({"isMeta": True}, "meta 提示"),
    ({"origin": {"kind": "task-notification"}}, "后台任务通知"),
    ({"origin": {"kind": "peer"}}, "peer 消息"),
    ({"isCompactSummary": True}, "This session is being continued from a previous conversation"),
    ({}, "<command-name>/clear</command-name>"),
    ({}, "<local-command-stdout>Set model</local-command-stdout>"),
    ({}, "<task-notification>done</task-notification>"),
    ({}, "<private>只有私密内容</private>"),
    ({}, "[Request interrupted by user]"),
    ({}, "<system-reminder>只有系统提醒</system-reminder>"),
])
def test_non_user_prompts_skipped(conn, tmp_path, proj, extra, text):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    write_lines(p, [b.prompt(text, **extra), b.prompt("真提示")])
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert [r["body"] for r in events(conn)] == ["真提示"]


def test_prompt_with_embedded_system_reminder_and_private_stripped(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    write_lines(p, [b.prompt("改一下登录页<system-reminder>注入的东西</system-reminder> 谢谢<private>密码是 abc</private>")])
    ct.ingest_session(conn, SID, str(p))
    body = events(conn)[0]["body"]
    assert "注入的东西" not in body and "abc" not in body and "改一下登录页" in body


def test_prompt_list_content_text_and_image(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR" * 500}},
              {"type": "text", "text": "看这张图"}]
    only_marker = [{"type": "text", "text": "[Image: source: C:\\x\\1.png]"}]
    write_lines(p, [J(b._base("user", message={"role": "user", "content": blocks})),
                    J(b._base("user", message={"role": "user", "content": only_marker}))])
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert events(conn)[0]["body"] == "看这张图"


def test_sidechain_and_other_types_skipped(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    side = json.loads(b.prompt("子代理里的提示"))
    side["isSidechain"] = True
    write_lines(p, [J(side), '{"type":"queue-operation","operation":"enqueue"}', b.prompt("主线提示")])
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert [r["body"] for r in events(conn)] == ["主线提示"]


def test_synthetic_and_api_error_assistant_skipped(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    err = json.loads(b.asst_text("API Error: 403", stop="stop_sequence"))
    err["isApiErrorMessage"] = True
    err["message"]["model"] = "<synthetic>"
    write_lines(p, [J(err), b.asst_text("正常回答")])
    assert ct.ingest_session(conn, SID, str(p)) == 1


def test_multiple_tool_blocks_in_one_line_and_unanswered_tool(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    line = json.loads(b.tool_use("ta", "Bash", {"command": "make"}))
    line["message"]["content"].append({"type": "tool_use", "id": "tb", "name": "Bash", "input": {"command": "make test"}})
    write_lines(p, [J(line), b.tool_result("ta", "ok A")])  # tb 一直没有结果
    ct.ingest_session(conn, SID, str(p))
    rows = {r["uuid"]: r for r in events(conn)}
    assert rows["ta"]["body"].endswith("=> [exit 0]\nok A")
    assert rows["tb"]["body"] == "$ make test"


# ---------------------------------------------------------------- 幂等

def test_reading_three_times_is_stable(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    write_lines(p, standard_session(Builder(proj)))
    assert ct.ingest_session(conn, SID, str(p)) == 4
    first = events(conn)
    assert ct.ingest_session(conn, SID, str(p)) == 0
    assert ct.ingest_session(conn, SID, str(p)) == 0
    assert events(conn) == first
    assert conn.execute("SELECT count(*) FROM docs WHERE src='prompt'").fetchone()[0] == 1


def test_force_reread_from_zero_does_not_duplicate_or_lose_results(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    write_lines(p, standard_session(Builder(proj)))
    ct.ingest_session(conn, SID, str(p))
    first = events(conn)
    conn.execute("UPDATE sessions SET read_offset=0 WHERE id=?", (SID,))
    assert ct.ingest_session(conn, SID, str(p)) == 0
    assert events(conn) == first


def test_two_halves_equal_single_read(tmp_path, monkeypatch, proj):
    b = Builder(proj)
    lines = standard_session(b) + [b.prompt("第二个问题"), b.tool_use("tu9", "Bash", {"command": "pwd2"}),
                                   b.tool_result("tu9", "D:/x"), b.asst_text("好了")]
    full = tmp_path / "full.jsonl"
    write_lines(full, lines)
    c1 = new_conn(tmp_path, monkeypatch, "h1")
    ct.ingest_session(c1, SID, str(full))
    want = events(c1)
    c1.close()

    # 在 tu3 的 tool_use 和 tool_result 之间切开：result 落在第二次读取里
    cut = next(i for i, ln in enumerate(lines) if '"tu3"' in ln and "tool_use" in ln) + 1
    half = tmp_path / "half.jsonl"
    c2 = new_conn(tmp_path, monkeypatch, "h2")
    write_lines(half, lines[:cut])
    ct.ingest_session(c2, SID, str(half))
    mid = {r["uuid"]: r["body"] for r in events(c2)}
    assert mid["tu3"] == "$ docker ps"  # 此时只有输入部分
    write_lines(half, lines[cut:], mode="a")
    ct.ingest_session(c2, SID, str(half))
    assert events(c2) == want
    c2.close()


def test_incomplete_last_line_left_for_next_time(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    l1, l2 = b.prompt("第一句"), b.prompt("第二句")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(l1 + "\n" + l2[:40])
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert conn.execute("SELECT read_offset FROM sessions").fetchone()[0] == len((l1 + "\n").encode("utf-8"))
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(l2[40:] + "\n")
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert [r["body"] for r in events(conn)] == ["第一句", "第二句"]


def test_crash_before_commit_then_reread_equals_clean_read(tmp_path, monkeypatch, proj):
    b = Builder(proj)
    lines = standard_session(b) + [b.prompt("第二个问题"), b.asst_text("好了")]
    p = tmp_path / "t.jsonl"
    write_lines(p, lines)
    c1 = new_conn(tmp_path, monkeypatch, "clean")
    ct.ingest_session(c1, SID, str(p))
    want = events(c1)
    c1.close()

    c2 = new_conn(tmp_path, monkeypatch, "crash")
    calls = {"n": 0}
    real = index.index_doc

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟崩溃")
        return real(*a, **k)

    monkeypatch.setattr(index, "index_doc", boom)
    with pytest.raises(RuntimeError):
        ct.ingest_session(c2, SID, str(p))
    monkeypatch.setattr(index, "index_doc", real)
    assert events(c2) == [] and c2.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert c2.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
    ct.ingest_session(c2, SID, str(p))
    assert events(c2) == want
    c2.close()


def test_crash_in_second_chunk_keeps_first_chunk_and_resumes(tmp_path, monkeypatch, proj):
    b = Builder(proj)
    lines = []
    for i in range(12):
        lines += [b.prompt(f"问题 {i}"), b.tool_use(f"t{i}", "Bash", {"command": f"cmd{i}"}),
                  b.tool_result(f"t{i}", f"out{i}"), b.asst_text(f"答 {i}")]
    p = tmp_path / "t.jsonl"
    write_lines(p, lines)
    c1 = new_conn(tmp_path, monkeypatch, "clean")
    ct.ingest_session(c1, SID, str(p))
    want = events(c1)
    c1.close()

    monkeypatch.setattr(ct, "CHUNK_BYTES", 1500)
    c2 = new_conn(tmp_path, monkeypatch, "crash")
    calls = {"n": 0}
    real = index.index_doc

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 6:
            raise RuntimeError("模拟崩溃")
        return real(*a, **k)

    monkeypatch.setattr(index, "index_doc", boom)
    with pytest.raises(RuntimeError):
        ct.ingest_session(c2, SID, str(p))
    monkeypatch.setattr(index, "index_doc", real)
    off = c2.execute("SELECT read_offset FROM sessions").fetchone()[0]
    assert 0 < off < os.path.getsize(p)  # 前面的块已提交，位置和事件一致
    ct.ingest_session(c2, SID, str(p))
    assert events(c2) == want
    assert c2.execute("SELECT count(*) FROM docs WHERE src='prompt'").fetchone()[0] == 12
    c2.close()


def test_file_shrink_or_replaced_resets_offset(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.prompt("旧内容一"), b.prompt("旧内容二"), b.prompt("旧内容三")])
    ct.ingest_session(conn, SID, str(p))
    # 文件被换成更短的另一份（开头不同）
    b2 = Builder(proj)
    b2.n = 100
    write_lines(p, [b2.prompt("全新内容")])
    assert os.path.getsize(p) < conn.execute("SELECT read_offset FROM sessions").fetchone()[0]
    assert ct.ingest_session(conn, SID, str(p)) == 1
    assert "全新内容" in [r["body"] for r in events(conn)]
    # 同样大小但开头不同：也要认出来
    b3 = Builder(proj)
    b3.n = 200
    new_line = b3.prompt("第三版内容")
    write_lines(p, [new_line])
    off = conn.execute("SELECT read_offset FROM sessions").fetchone()[0]
    pad = " " * max(0, off - os.path.getsize(p))
    write_lines(p, [new_line + pad])
    assert ct.ingest_session(conn, SID, str(p)) == 1


def test_bad_line_logged_and_skipped(conn, tmp_path, proj):
    p = tmp_path / "t.jsonl"
    b = Builder(proj)
    write_lines(p, [b.prompt("前"), '{"type":"user","uuid":"bad", 坏了', b.prompt("后")])
    log._reset_for_tests()
    assert ct.ingest_session(conn, SID, str(p)) == 2
    text = "".join(f.read_text(encoding="utf-8") for f in (Path(os.environ["KNOTCORD_HOME"]) / "logs").glob("*.log"))
    assert "解析失败" in text


def test_missing_transcript_is_not_an_error(conn, tmp_path):
    assert ct.ingest_session(conn, SID, str(tmp_path / "nope.jsonl"), cwd=str(tmp_path)) == 0


# ---------------------------------------------------------------- 过滤、脱敏、精简

def test_filtered_tools_not_stored_even_when_result_arrives_later(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.tool_use("r1", "Read", {"file_path": "x"}),
                    b.tool_use("g1", "mcp__plugin_claude-mem_mcp-search__search", {"q": "a"}),
                    b.tool_use("p1", "PowerShell", {"command": "Get-ChildItem C:\\"}),
                    b.tool_use("s1", "mcp__x__take_screenshot", {"action": "go"})])
    ct.ingest_session(conn, SID, str(p))
    write_lines(p, [b.tool_result("r1", "x" * 100), b.tool_result("g1", "y"), b.tool_result("p1", "z"),
                    b.tool_result("s1", "w")], mode="a")
    ct.ingest_session(conn, SID, str(p))
    assert events(conn) == []


def test_secret_across_trim_boundary_never_stored(conn, tmp_path, proj):
    secret = "sk-ant-api03-" + "Q" * 8 + "z9" + "R" * 30
    out = "o" * 590 + secret + "p" * 2000
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.tool_use("k1", "Bash", {"command": "cat .env", "description": "x"}),
                    b.tool_result("k1", out),
                    b.prompt("密钥是 " + secret),
                    b.tool_use("k2", "Bash", {"command": "curl -H 'Authorization: Bearer abcdefgh12345678' x"}),
                    b.tool_result("k2", "ok")])
    ct.ingest_session(conn, SID, str(p))
    blob = json.dumps(events(conn), ensure_ascii=False)
    assert "sk-ant" not in blob and "QQQQQQQQ" not in blob and "abcdefgh12345678" not in blob
    assert "REDACTED" in blob


def test_huge_tool_output_over_1mb(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    big = "L1\n" + "中" * 1_200_000 + "\nEND"
    write_lines(p, [b.tool_use("big", "Bash", {"command": "dump"}), b.tool_result("big", big)])
    ct.ingest_session(conn, SID, str(p))
    body = events(conn)[0]["body"]
    assert len(body) < 1500 and body.startswith("$ dump") and body.rstrip().endswith("END")


def test_tool_result_with_image_blocks_dropped(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    content = [{"type": "text", "text": "截图说明"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR" * 1000}}]
    write_lines(p, [b.tool_use("i1", "mcp__x__doit", {"a": 1}), b.tool_result("i1", content)])
    ct.ingest_session(conn, SID, str(p))
    body = events(conn)[0]["body"]
    assert "截图说明" in body and "iVBOR" not in body


# ---------------------------------------------------------------- last_assistant_message

def test_last_message_first_then_transcript_catches_up(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.prompt("问"), b.tool_use("x1", "Bash", {"command": "make"}), b.tool_result("x1", "ok")])
    assert ct.ingest_session(conn, SID, str(p), cwd=proj, last_assistant_message="最后的回答。") == 3
    assert kinds(conn)["assistant"] == 1
    ct.ingest_session(conn, SID, str(p), cwd=proj, last_assistant_message="最后的回答。")  # Stop 重复触发
    assert kinds(conn)["assistant"] == 1
    # transcript 稍后真的写进了这条
    write_lines(p, [b.asst_text("最后的回答。", uuid="real-uuid-1")], mode="a")
    assert ct.ingest_session(conn, SID, str(p), cwd=proj) == 0
    rows = conn.execute("SELECT uuid, src_line FROM events WHERE kind='assistant'").fetchall()
    assert [r["uuid"] for r in rows] == ["real-uuid-1"] and rows[0]["src_line"] == 4
    assert ct.ingest_session(conn, SID, str(p), cwd=proj, last_assistant_message="最后的回答。") == 0
    assert kinds(conn)["assistant"] == 1


def test_transcript_first_then_last_message_not_duplicated(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.prompt("问"), b.asst_text("一模一样的回答")])
    ct.ingest_session(conn, SID, str(p))
    assert ct.ingest_session(conn, SID, str(p), last_assistant_message="一模一样的回答") == 0
    assert kinds(conn)["assistant"] == 1


def test_same_text_in_later_turn_is_still_recorded(conn, tmp_path, proj):
    b = Builder(proj)
    p = tmp_path / "t.jsonl"
    write_lines(p, [b.prompt("问一"), b.asst_text("好的"), b.prompt("问二")])
    ct.ingest_session(conn, SID, str(p))
    assert ct.ingest_session(conn, SID, str(p), last_assistant_message="好的") == 1  # 这是第二轮的"好的"
    assert kinds(conn)["assistant"] == 2


def test_last_message_is_redacted_and_session_created_without_transcript(conn, tmp_path, proj):
    assert ct.ingest_session(conn, "sess-x", "", cwd=proj,
                             last_assistant_message="key=sk-ant-api03-" + "A1b2" * 10) == 1
    body = conn.execute("SELECT body FROM events").fetchone()[0]
    assert "sk-ant" not in body
    assert conn.execute("SELECT project FROM sessions WHERE id='sess-x'").fetchone()[0] == "myproj"


# ---------------------------------------------------------------- 真实 transcript（只读）

def _pick_real():
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    best = None
    for f in root.glob("*/*.jsonl"):
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        if 2_000_000 <= sz <= 40_000_000 and (best is None or sz > best[0]):
            best = (sz, f)
    return best[1] if best else None


def test_real_transcript_readonly(conn):
    f = _pick_real()
    if f is None:
        pytest.skip("本机没有合适大小的真实 transcript")
    before = (f.stat().st_size, f.stat().st_mtime_ns)
    t0 = time.perf_counter()
    n = ct.ingest_session(conn, "real-1", str(f))
    dt = time.perf_counter() - t0
    k = kinds(conn)
    print(f"\n真实 transcript {f.name} {before[0] / 1e6:.1f} MB: {n} 条 {k} {dt:.2f}s")
    assert n > 0 and k.get("prompt", 0) > 0 and k.get("assistant", 0) > 0
    assert dt < 15
    assert ct.ingest_session(conn, "real-1", str(f)) == 0
    assert (f.stat().st_size, f.stat().st_mtime_ns) == before
