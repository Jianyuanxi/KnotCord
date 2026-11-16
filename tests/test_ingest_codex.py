"""Codex 会话采集：用自己构造的、结构和真实 rollout 一致的假文件（不含任何真实对话）。"""

import json
import os
import time
from pathlib import Path

import pytest

from knotcord import work
from knotcord.core import config, db
from knotcord.ingest import claude_transcript as ct
from knotcord.ingest import codex_transcript as cx

TID = "01a1d935-e5e1-7332-a64d-4957130eeb7a"


def J(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Rollout:
    """按真实文件的行结构造：session_meta 在第 0 行，之后是 turn 里的各类行，ordinal 递增。"""

    def __init__(self, cwd: str, tid: str = TID, **meta):
        self.n = 0
        self.t = 1_794_000_000
        self.tid = tid
        self.lines: list[str] = []
        pl = {"session_id": tid, "id": tid, "timestamp": self._iso(), "cwd": cwd, "originator": "Codex Desktop",
              "cli_version": "0.167.0-alpha.4.1", "source": "vscode", "thread_source": "user",
              "base_instructions": {"text": "x" * 50}}
        pl.update(meta)
        self._add("session_meta", pl)

    def _iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t))

    def _add(self, typ: str, payload: dict, **extra) -> str:
        self.t += 3
        d = {"timestamp": self._iso(), "ordinal": self.n, "type": typ, "payload": payload}
        d.update(extra)
        self.n += 1
        ln = J(d)
        self.lines.append(ln)
        return ln

    def item(self, item: dict) -> None:
        self._add("event_msg", {"type": "item_completed", "thread_id": self.tid, "turn_id": "t1", "item": item})

    def noise(self) -> None:  # 各种不该被采集的行
        self._add("world_state", {"full": True, "state": {"agents_md": {"text": "忽略我"}}})
        self._add("turn_context", {"turn_id": "t1", "cwd": "x"})
        self._add("response_item", {"type": "message", "role": "developer",
                                    "content": [{"type": "input_text", "text": "<permissions>忽略我"}]})
        self._add("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "<environment_context>忽略我"}]})
        self._add("event_msg", {"type": "token_count", "info": None})
        self._add("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "忽略我"})

    def user(self, text, iid=None, extra_blocks=()) -> None:
        self.item({"type": "UserMessage", "id": iid or f"0000{self.n:04d}-0000-7000-8000-000000000000",
                   "client_id": "c", "content": [{"type": "text", "text": text, "text_elements": []},
                                                 *extra_blocks]})

    def agent(self, text, phase="final_answer", iid=None) -> None:
        it = {"type": "AgentMessage", "id": iid or f"msg_{self.n:04d}", "content": [{"type": "Text", "text": text}]}
        if phase is not None:
            it["phase"] = phase
        self.item(it)

    def cmd(self, script, out="", code=0, iid=None, argv=None) -> None:
        self.item({"type": "CommandExecution", "id": iid or f"exec-{self.n:04d}",
                   "process_id": "1", "command": argv or ["pwsh.exe", "-NoProfile", "-Command", script],
                   "cwd": "file:///E:/x", "parsed_cmd": [{"type": "unknown", "cmd": script}],
                   "source": "unified_exec_startup", "status": "completed" if code == 0 else "failed",
                   "aggregated_output": out, "exit_code": code, "duration": {"secs": 1, "nanos": 0}})

    def edit(self, path, diff="@@ -1 +1 @@\n-a\n+b\n", status="completed", iid=None) -> None:
        self.item({"type": "FileChange", "id": iid or f"edit-{self.n:04d}",
                   "changes": {path: {"type": "update", "unified_diff": diff, "move_path": None}},
                   "status": status, "stdout": "Success", "stderr": ""})

    def mcp(self, server, tool, args, text="结果", err=False, iid=None) -> None:
        self.item({"type": "McpToolCall", "id": iid or f"mcp-{self.n:04d}", "server": server, "tool": tool,
                   "arguments": args, "status": "completed",
                   "result": {"content": [{"type": "text", "text": text}], "isError": err}})

    def write(self, path: Path, mode="w") -> None:
        with open(path, mode, encoding="utf-8", newline="\n") as f:
            for ln in self.lines:
                f.write(ln + "\n")
        self.lines = []


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "myproj"
    (p / ".git").mkdir(parents=True)
    return str(p)


def rollout_path(root: Path, tid: str = TID, day="2026/11/14") -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    return d / f"rollout-2026-11-14T03-39-10-{tid}.jsonl"


def standard(r: Rollout) -> None:
    r.noise()
    r.user("帮我看看 3x-ui 面板端口")
    r.agent("我先查一下", phase="commentary")
    r.cmd("docker ps", "CONTAINER ID\nabc", code=1)
    r.cmd("Get-Content a.txt", "内容")                        # 命令前缀被过滤
    r.cmd("git status", "clean")                              # 同上
    r.edit("C:\\p\\a.py")
    r.mcp("knotcord", "search", {"query": "x"})                # 自家工具：过滤，防回灌
    r.mcp("context7", "query-docs", {"q": "y"}, text="文档")
    r.item({"type": "Reasoning", "id": "rs_1", "summary_text": [], "raw_content": []})
    r.item({"type": "WebSearch", "id": "ws_1"})
    r.agent("端口是 2053。", phase="final_answer")


def rows(conn, sql="SELECT kind, tool, brief, body, uuid, src_line, project FROM events ORDER BY id"):
    return [dict(r) for r in conn.execute(sql)]


# ---------------------------------------------------------------- 映射

def test_basic_mapping(conn, tmp_path, proj):
    r = Rollout(proj)
    standard(r)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    n = cx.ingest_file(conn, str(p))
    ev = rows(conn)
    assert n == len(ev) == 5
    assert [e["kind"] for e in ev] == ["prompt", "tool", "tool", "tool", "assistant"]
    by = {e["tool"]: e for e in ev if e["tool"]}
    assert set(by) == {"PowerShell", "Edit", "mcp__context7__query-docs"}
    ps = by["PowerShell"]
    assert ps["body"].startswith("$ docker ps") and "[exit 1]" in ps["body"] and "CONTAINER ID" in ps["body"]
    assert "a.py" in by["Edit"]["brief"] and "=> ok" in by["Edit"]["body"] and "+b" in by["Edit"]["body"]
    assert "文档" in by["mcp__context7__query-docs"]["body"]
    texts = " ".join((e["body"] or "") for e in ev)
    assert "忽略我" not in texts and "我先查一下" not in texts          # commentary、系统注入都不进
    assert [e["body"] for e in ev if e["kind"] == "assistant"] == ["端口是 2053。"]
    assert [e["body"] for e in ev if e["kind"] == "prompt"] == ["帮我看看 3x-ui 面板端口"]


def test_session_row_and_project_name_same_as_claude(conn, tmp_path, proj):
    r = Rollout(proj)
    standard(r)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (TID,)).fetchone()
    assert s["client"] == "codex" and s["project"] == "myproj" and s["cwd"] == proj
    assert s["transcript_path"] == str(p) and s["read_offset"] == os.path.getsize(p)
    # 同一个文件夹，Claude 那边的项目键规则给出同一个名字
    from knotcord.core import project
    assert project.project_key(proj) == "myproj"
    assert {e["project"] for e in rows(conn)} == {"myproj"}


def test_claude_project_dir_env_not_applied(conn, tmp_path, proj, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "other-claude-project"))
    r = Rollout(proj)
    r.user("hi")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    assert conn.execute("SELECT project FROM sessions").fetchone()[0] == "myproj"


def test_uuid_prefix_unique_and_src_line(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("a", iid="item-1")                       # 老的外部导入会话 id 就是 item-1
    r.agent("b", iid="item-2")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    ev = rows(conn)
    assert [e["uuid"] for e in ev] == [f"codex:{TID}:item-1", f"codex:{TID}:item-2"]
    assert [e["src_line"] for e in ev] == [2, 3]    # 文件里的行号（第 1 行是 session_meta）
    # 另一个线程里同样叫 item-1，不撞
    tid2 = "01a1d935-e5e1-7332-a64d-4957130eeb7b"
    r2 = Rollout(proj, tid=tid2)
    r2.user("c", iid="item-1")
    p2 = rollout_path(tmp_path / "sessions", tid2)
    r2.write(p2)
    cx.ingest_file(conn, str(p2))
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_filter_redact_and_trim(conn, tmp_path, proj):
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    r = Rollout(proj)
    r.user(f"密钥是 {secret} 记住")
    r.cmd("python run.py", f"token={secret}\n" + "输出行\n" * 800, code=0)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    blob = json.dumps(rows(conn), ensure_ascii=False)
    assert secret not in blob and "REDACTED" in blob.upper()
    tool = [e for e in rows(conn) if e["kind"] == "tool"][0]
    assert len(tool["body"]) < 1800 and "省略" in tool["body"]


def test_system_injected_prompts_and_images_dropped(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("<environment_context>cwd</environment_context>")
    r.user("# AGENTS.md instructions for E:\\x")
    r.user("<turn_aborted>x</turn_aborted>")
    r.user("看这张图", extra_blocks=[{"type": "local_image", "path": "C:/a.png"}])
    r.user("# Files mentioned by the user:\n\n## a.py\n\n## My request\n改一下")  # 用户附带的文件上下文：保留
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    bodies = [e["body"] for e in rows(conn)]
    assert bodies[0] == "看这张图" and bodies[1].startswith("# Files mentioned") and len(bodies) == 2


def test_legacy_agent_message_without_phase_is_final(conn, tmp_path, proj):
    r = Rollout(proj)
    r.agent("旧版回答", phase=None)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    assert cx.ingest_file(conn, str(p)) == 1


def test_failed_edit_and_mcp_error(conn, tmp_path, proj):
    r = Rollout(proj)
    r.edit("a.py", status="failed")
    r.mcp("srv", "do", {"a": 1}, text="boom", err=True)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    ev = rows(conn)
    assert "[错误]" in ev[0]["body"] and "[错误] boom" in ev[1]["body"]


def test_bash_argv_and_unknown_shapes(conn, tmp_path, proj):
    r = Rollout(proj)
    r.cmd("", "ok", argv=["bash", "-lc", "make test"])
    r.item({"type": "FileChange", "id": "x1", "changes": {}, "status": "completed"})      # 空变更：不记
    r.item({"type": "McpToolCall", "id": "x2", "server": None, "tool": 3})                # 坏结构：不记、不报错
    r.item({"type": "Brand-new-kind", "id": "x3"})
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    ev = rows(conn)
    assert len(ev) == 1 and ev[0]["tool"] == "Bash" and ev[0]["body"].startswith("$ make test")


# ---------------------------------------------------------------- 增量

def test_incremental_idempotent_and_partial_line(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("第一句")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    assert cx.ingest_file(conn, str(p)) == 1
    assert cx.ingest_file(conn, str(p)) == 0                 # 重读 0 新增
    r.agent("回答一")
    ln = r.lines[0]
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(ln[:40])                                      # 写了半行
    assert cx.ingest_file(conn, str(p)) == 0                 # 半行不读，位置不动
    off = conn.execute("SELECT read_offset FROM sessions").fetchone()[0]
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(ln[40:] + "\n")
    assert cx.ingest_file(conn, str(p)) == 1
    assert conn.execute("SELECT read_offset FROM sessions").fetchone()[0] > off
    assert cx.ingest_file(conn, str(p)) == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_small_chunks_same_result(conn, tmp_path, proj, monkeypatch):
    r = Rollout(proj)
    standard(r)
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    monkeypatch.setattr(ct, "CHUNK_BYTES", 300)
    n = cx.ingest_file(conn, str(p))
    assert n == 5 and cx.ingest_file(conn, str(p)) == 0


def test_file_replaced_rereads_from_start(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("旧内容")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    r2 = Rollout(proj)
    r2.user("新内容，比旧文件长得多得多得多", iid="new-1")
    r2.agent("再来一条", iid="new-2")
    r2.write(p)
    assert cx.ingest_file(conn, str(p)) == 2
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3


# ---------------------------------------------------------------- 不采集的会话

@pytest.mark.parametrize("meta", [
    {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {}}}},
    {"thread_source": "guardian_review", "source": {"x": 1}},
])
def test_subagent_threads_skipped(conn, tmp_path, proj, meta):
    r = Rollout(proj, **meta)
    r.user("x")
    r.agent("y")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    assert cx.ingest_file(conn, str(p)) == 0
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert db.kv_get(conn, cx.SKIP_KEY + TID)
    assert cx.ingest_file(conn, str(p)) == 0                 # 再来也不读


def test_observer_session_skipped(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("<observed_from_primary_session>\n<what_happened>x</what_happened>")
    r.agent("<observation>y</observation>")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    assert cx.ingest_file(conn, str(p)) == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_thread_id_from_name_and_fallback(tmp_path, proj):
    assert cx.thread_id_of(f"x/rollout-2026-11-14T03-39-10-{TID.upper()}.jsonl") == TID
    r = Rollout(proj)
    r.user("x")
    p = tmp_path / "weird-name.jsonl"
    r.write(p)
    assert cx.thread_id_of(str(p)) == TID


# ---------------------------------------------------------------- 和 Claude 那边共用的部分

def test_last_message_dedupe_with_stop_hook_text(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("q")
    r.agent("最终回答在这里")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    cx.ingest_file(conn, str(p))
    n = cx.ingest_session(conn, TID, str(p), last_assistant_message="最终回答在这里")
    assert n == 0 and conn.execute("SELECT count(*) FROM events WHERE kind='assistant'").fetchone()[0] == 1


def test_work_ingest_one_dispatches_by_client(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("q")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    assert work.ingest_one(conn, TID, str(p), client="codex") == 1
    r.agent("a")
    r.write(p, "a")
    assert work.ingest_one(conn, TID, None) == 1            # 之后按 sessions.client 认出是 codex


def test_process_pending_routes_codex_rows(conn, tmp_path, proj):
    r = Rollout(proj)
    r.user("q")
    p = rollout_path(tmp_path / "sessions")
    r.write(p)
    conn.execute("INSERT INTO pending(session_id, client, transcript_path, reason, enqueued_at) VALUES(?,?,?,?,?)",
                 (TID, "codex", str(p), "stop", int(time.time())))
    n, _ = work.process_pending(conn)
    assert n == 1 and conn.execute("SELECT client FROM sessions").fetchone()[0] == "codex"


# ---------------------------------------------------------------- 扫描

def scan_cfg(**kw):
    c = config.load()
    d = c.as_dict()
    d.update(kw)
    return config.Config(d)


@pytest.fixture(autouse=True)
def _reset_seen():
    work._codex_seen.clear()
    yield
    work._codex_seen.clear()


def test_scan_defaults_in_config():
    c = config.load()
    assert c.get("codex.enabled") is True and c.get("codex.scan_s") == 60
    assert c.get("codex.lookback_days") == 2 and c.get("codex.sessions_dir") == ""


def test_scan_missing_dir_is_zero_cost(conn, tmp_path):
    cfg = scan_cfg(**{"codex.sessions_dir": str(tmp_path / "nope")})
    assert work.scan_codex(conn, cfg) == 0
    assert db.kv_get(conn, work.KEY_CODEX_SCAN) is None      # 连占位的写都没有


def test_scan_default_dir_follows_codex_home(conn, tmp_path, monkeypatch, proj):
    home = tmp_path / "ch"
    monkeypatch.setenv("CODEX_HOME", str(home))
    r = Rollout(proj)
    r.user("q")
    r.write(rollout_path(home / "sessions"))
    assert work.scan_codex(conn, config.load()) == 1


def test_scan_lookback_zst_and_cache(conn, tmp_path, proj):
    root = tmp_path / "sessions"
    r = Rollout(proj)
    r.user("新的")
    new = rollout_path(root, TID, "2026/11/14")
    r.write(new)
    tid_old = "01a1d935-e5e1-7332-a64d-4957130eeb00"
    r2 = Rollout(proj, tid=tid_old)
    r2.user("旧的")
    old = rollout_path(root, tid_old, "2026/10/01")
    r2.write(old)
    z = rollout_path(root, "01a1d935-e5e1-7332-a64d-4957130eeb01", "2026/11/14").with_suffix(".jsonl.zst")
    z.write_bytes(b"\x28\xb5\x2f\xfd not really zstd")       # 压缩文件：不碰
    now = time.time()
    os.utime(old, (now - 10 * 86400,) * 2)
    cfg = scan_cfg(**{"codex.sessions_dir": str(root), "codex.lookback_days": 2})
    assert work.scan_codex(conn, cfg, now=now) == 1
    assert [e["body"] for e in rows(conn)] == ["新的"]
    # 间隔内别的进程（这里是同一个连接）再来：不扫
    assert work.scan_codex(conn, cfg, now=now + 10) == 0
    # 到点再扫：文件没变，不再打开，也没有新增
    assert work.scan_codex(conn, cfg, now=now + 61) == 0
    # 文件增长后被读到，再扫一轮又是 0
    r.agent("追加")
    r.write(new, "a")
    assert work.scan_codex(conn, cfg, now=now + 130) == 1
    assert sorted(e["body"] for e in rows(conn)) == ["新的", "追加"]
    assert work.scan_codex(conn, cfg, now=now + 200) == 0


def test_scan_claim_across_processes(conn, tmp_path, proj):
    """两个连接（两个 server）同一间隔内只有一个真扫。"""
    root = tmp_path / "sessions"
    r = Rollout(proj)
    r.user("q")
    r.write(rollout_path(root))
    cfg = scan_cfg(**{"codex.sessions_dir": str(root)})
    conn2 = db.connect()
    try:
        now = time.time()
        assert work.scan_codex(conn, cfg, now=now) == 1
        work._codex_seen.clear()
        assert work.scan_codex(conn2, cfg, now=now + 5) == 0
        assert work.scan_codex(conn2, cfg, now=now + 61) == 0   # 文件没变，已入库的不重复
    finally:
        conn2.close()


def test_install_prints_codex_steps(capsys, trace_home):
    import tomllib
    from knotcord import cli
    py = "C:\\Python\\python.exe"
    text = "\n".join(cli.codex_steps(py))
    start = text.index("[mcp_servers.knotcord]")
    end = text.index("  B. ")
    block = "\n".join(ln.strip() for ln in text[start:end].splitlines())
    toml = tomllib.loads(block)["mcp_servers"]["knotcord"]            # 打印出来的就是合法 TOML
    assert toml["command"] == py and toml["args"][-1] == "serve" and toml["args"][-2].endswith("boot.py")
    assert toml["env"]["KNOTCORD_HOME"] == str(trace_home)
    hook_json = text[text.index("{", end): text.index("  C. ")]
    h = json.loads("\n".join(ln.strip() for ln in hook_json.splitlines()))     # 合法 JSON，字段名和官方文档一致
    cmd = h["hooks"]["SessionStart"][0]["hooks"][0]
    assert cmd["type"] == "command" and cmd["command"] == cmd["commandWindows"] and "session-start" in cmd["command"]
    assert cli.main(["install", "--dry-run"]) == 0                    # 整条命令能跑，且不写任何文件
    assert "mcp_servers.knotcord" in capsys.readouterr().out


def test_worker_scans_codex_dir(tmp_path, proj):
    """worker 线程启动后第一轮就扫 Codex 目录，不需要任何 hook。"""
    import threading
    from knotcord.server.worker import Worker
    root = tmp_path / "sessions"
    r = Rollout(proj)
    standard(r)
    r.write(rollout_path(root))
    cfg = scan_cfg(**{"codex.sessions_dir": str(root), "codex.scan_s": 3600})
    stop = threading.Event()
    w = Worker(stop, cfg, start_delay_s=9999)
    w.start()
    try:
        c = db.connect()
        try:
            for _ in range(100):
                if c.execute("SELECT count(*) FROM events").fetchone()[0] >= 5:
                    break
                time.sleep(0.1)
            assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 5
            assert c.execute("SELECT client FROM sessions").fetchone()[0] == "codex"
        finally:
            c.close()
    finally:
        stop.set()
        w.wake()
        w.join(5)
    assert not w.is_alive()


def test_scan_disabled(conn, tmp_path, proj):
    root = tmp_path / "sessions"
    r = Rollout(proj)
    r.user("q")
    r.write(rollout_path(root))
    cfg = scan_cfg(**{"codex.sessions_dir": str(root), "codex.enabled": False})
    assert work.scan_codex(conn, cfg) == 0


def test_scan_survives_bad_file(conn, tmp_path, proj, monkeypatch):
    root = tmp_path / "sessions"
    r = Rollout(proj)
    r.user("好的")
    r.write(rollout_path(root))
    bad_tid = "01a1d935-e5e1-7332-a64d-4957130eeb99"
    rollout_path(root, bad_tid).write_text("{not json\n\x00\x01\n", encoding="utf-8")
    cfg = scan_cfg(**{"codex.sessions_dir": str(root)})
    assert work.scan_codex(conn, cfg) == 1
