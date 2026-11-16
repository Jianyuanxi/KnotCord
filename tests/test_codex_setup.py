"""Codex 接入：install --codex 的配置编辑、hook 入口的 Codex 分支、两个客户端共用一份记忆。

所有写文件的测试都指向临时的 CODEX_HOME，不碰真实的 ~/.codex。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from knotcord import cli, codex_setup, inject
from knotcord.core import db, project
from knotcord.ingest import claude_transcript as ct
from knotcord.ingest import codex_transcript as cx
from knotcord.search import api

import srvharness
import test_ingest_codex as tic

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "knotcord" / "hooks" / "entry.py"
PY = sys.executable

USER_CFG = '# 我的 Codex 配置\nmodel = "gpt-x"   # 行尾注释\n\n[mcp_servers.other]\ncommand = "node"\nargs = ["a.js"]\n'
FOREIGN_HOOKS = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
    {"type": "command", "command": "other-tool start", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "other-tool stop"}]}]}}


@pytest.fixture
def chome(tmp_path, monkeypatch):
    d = tmp_path / "codex_home"
    d.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(d))
    return d


def _run(*args, capsys=None) -> int:
    return cli.main(["install", "--codex", "--python", PY, *args])


def _hooks(chome) -> dict:
    return json.loads((chome / "hooks.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- config.toml

def test_block_is_valid_toml_and_has_expected_fields():
    block = codex_setup.mcp_block(PY, ROOT, Path("C:/kc home"), newline="\n")
    srv = tomllib.loads(block)["mcp_servers"]["knotcord"]
    assert srv["command"] == PY
    assert srv["args"][:3] == ["-I", "-X", "utf8"] and srv["args"][3].endswith("boot.py") and srv["args"][4] == "serve"
    assert srv["env"]["KNOTCORD_HOME"] == str(Path("C:/kc home"))
    assert srv["startup_timeout_sec"] == 30 and srv["disabled_tools"] == ["ingest"]
    assert os.path.isabs(srv["args"][3])


def test_append_keeps_everything_else_byte_for_byte():
    block = codex_setup.mcp_block(PY, ROOT, Path("C:/h"))
    new = codex_setup.plan_config(USER_CFG, block)
    assert new.startswith(USER_CFG)
    parsed = tomllib.loads(new)
    assert parsed["model"] == "gpt-x" and parsed["mcp_servers"]["other"]["command"] == "node"
    assert "knotcord" in parsed["mcp_servers"]


def test_crlf_file_stays_crlf():
    old = USER_CFG.replace("\n", "\r\n")
    new = codex_setup.plan_config(old, codex_setup.mcp_block(PY, ROOT, Path("C:/h"), newline="\r\n"))
    assert new.startswith(old)
    assert new.count("\n") == new.count("\r\n")


def test_replace_in_place_no_duplicate_and_remove_restores():
    b1 = codex_setup.mcp_block("C:/py/one.exe", ROOT, Path("C:/h"))
    b2 = codex_setup.mcp_block("C:/py/two.exe", ROOT, Path("C:/h"))
    t1 = codex_setup.plan_config(USER_CFG, b1)
    assert codex_setup.plan_config(t1, b1) == t1                      # 幂等
    t2 = codex_setup.plan_config(t1, b2)
    assert t2.count("[mcp_servers.knotcord]") == 1 and "two.exe" in t2 and "one.exe" not in t2
    assert codex_setup.plan_config(t2, None) == USER_CFG              # 撤销后和原文件逐字节一致


def test_remove_without_block_is_noop_and_empty_file_ok():
    assert codex_setup.plan_config(USER_CFG, None) == USER_CFG
    new = codex_setup.plan_config(None, codex_setup.mcp_block(PY, ROOT, Path("C:/h")))
    assert tomllib.loads(new)["mcp_servers"]["knotcord"]["command"] == PY
    assert codex_setup.plan_config(new, None) == ""


def test_manual_registration_conflict_is_rejected():
    manual = USER_CFG + '\n[mcp_servers.knotcord]\ncommand = "x"\n'
    with pytest.raises(codex_setup.SetupError, match="手工登记"):
        codex_setup.plan_config(manual, codex_setup.mcp_block(PY, ROOT, Path("C:/h")))


def test_invalid_toml_is_rejected():
    with pytest.raises(codex_setup.SetupError, match="不是合法 TOML"):
        codex_setup.plan_config("this is [not toml", codex_setup.mcp_block(PY, ROOT, Path("C:/h")))


def test_codex_home_env_is_forwarded_only_when_set(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert "CODEX_HOME" not in tomllib.loads(codex_setup.mcp_block(PY, ROOT, Path("C:/h")))["mcp_servers"]["knotcord"]["env"]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    env = tomllib.loads(codex_setup.mcp_block(PY, ROOT, Path("C:/h")))["mcp_servers"]["knotcord"]["env"]
    assert env["CODEX_HOME"] == str(tmp_path)


# ---------------------------------------------------------------- hooks.json

def test_hook_command_bare_vs_quoted():
    assert codex_setup.hook_command("C:\\py\\python.exe", "C:\\k\\entry.py", "session-start") == \
        "C:\\py\\python.exe -I -S -X utf8 C:\\k\\entry.py session-start --codex"
    q = codex_setup.hook_command("C:\\Program Files\\py\\python.exe", "C:\\k it's\\entry.py", "session-end")
    assert q.startswith("& 'C:\\Program Files\\py\\python.exe' -I -S -X utf8 ")
    assert "'C:\\k it''s\\entry.py'" in q and q.endswith(" session-end --codex")


def test_hook_groups_shape():
    g = codex_setup.hook_groups(PY, ROOT)
    assert set(g) == {"SessionStart", "SessionEnd"}
    start = g["SessionStart"][0]
    assert start["matcher"] == "startup|clear|compact" and "resume" not in start["matcher"]
    for ev, (grp,) in g.items():
        (h,) = grp["hooks"]
        assert h["type"] == "command" and h["command"] == h["commandWindows"] and h["command"].endswith("--codex")
        assert h["timeout"] <= (3 if ev == "SessionEnd" else 10)


def test_hooks_merge_keeps_foreign_idempotent_and_removes_only_ours():
    groups = codex_setup.hook_groups(PY, ROOT)
    old = json.dumps(FOREIGN_HOOKS, indent=2) + "\n"
    new = codex_setup.plan_hooks(old, groups)
    data = json.loads(new)
    assert data["hooks"]["Stop"] == FOREIGN_HOOKS["hooks"]["Stop"]
    assert data["hooks"]["SessionStart"][0] == FOREIGN_HOOKS["hooks"]["SessionStart"][0]
    assert len(data["hooks"]["SessionStart"]) == 2 and len(data["hooks"]["SessionEnd"]) == 1
    assert codex_setup.plan_hooks(new, groups) == new                 # 幂等：文本都不变
    back = json.loads(codex_setup.plan_hooks(new, None))
    assert back == FOREIGN_HOOKS                                       # 只撤自己的；空事件不留


def test_hooks_missing_file_created_and_removed_to_empty():
    new = codex_setup.plan_hooks(None, codex_setup.hook_groups(PY, ROOT))
    assert set(json.loads(new)["hooks"]) == {"SessionStart", "SessionEnd"}
    assert json.loads(codex_setup.plan_hooks(new, None)) == {"hooks": {}}


def test_hooks_changed_python_replaces_our_entry():
    old = codex_setup.plan_hooks(None, codex_setup.hook_groups("C:/a/python.exe", ROOT))
    new = json.loads(codex_setup.plan_hooks(old, codex_setup.hook_groups("C:/b/python.exe", ROOT)))
    cmds = [h["command"] for g in new["hooks"]["SessionStart"] for h in g["hooks"]]
    assert len(cmds) == 1 and "C:/b/python.exe" in cmds[0]


def test_hooks_group_shared_with_foreign_handler_only_ours_removed():
    ours = codex_setup.hook_groups(PY, ROOT)["SessionEnd"][0]["hooks"][0]
    mixed = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "x"}, ours]}]}}
    out = json.loads(codex_setup.plan_hooks(json.dumps(mixed), None))
    assert out == {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "x"}]}]}}


def test_hooks_invalid_json_and_wrong_shape_rejected():
    groups = codex_setup.hook_groups(PY, ROOT)
    for bad in ("{oops", "[]", '{"hooks": []}', '{"hooks": {"SessionStart": {}}}'):
        with pytest.raises(codex_setup.SetupError):
            codex_setup.plan_hooks(bad, groups)


# ---------------------------------------------------------------- install --codex（命令行）

def test_dry_run_writes_nothing(chome, capsys):
    (chome / "config.toml").write_text(USER_CFG, encoding="utf-8")
    assert _run() == 0
    out = capsys.readouterr().out
    assert "预演" in out and "[mcp_servers.knotcord]" in out and "--apply" in out
    assert (chome / "config.toml").read_text(encoding="utf-8") == USER_CFG
    assert not (chome / "hooks.json").exists() and not list(chome.glob("*.bak-knotcord"))
    assert _run("--dry-run") == 0 and not (chome / "hooks.json").exists()


def test_apply_backup_idempotent_and_remove(chome, capsys):
    cfg, hk = chome / "config.toml", chome / "hooks.json"
    cfg.write_text(USER_CFG, encoding="utf-8")
    hk.write_text(json.dumps(FOREIGN_HOOKS, indent=2), encoding="utf-8")
    foreign_text = hk.read_text(encoding="utf-8")
    assert _run("--apply") == 0
    assert (chome / "config.toml.bak-knotcord").read_text(encoding="utf-8") == USER_CFG
    assert (chome / "hooks.json.bak-knotcord").read_text(encoding="utf-8") == foreign_text
    after = cfg.read_text(encoding="utf-8")
    assert after.startswith(USER_CFG) and "[mcp_servers.knotcord]" in after
    assert len(_hooks(chome)["hooks"]["SessionStart"]) == 2
    # 第二次：没有变化，不重写备份（备份里仍是用户原文件，而不是接入后的）
    capsys.readouterr()
    bak_mtime = (chome / "config.toml.bak-knotcord").stat().st_mtime_ns
    assert _run("--apply") == 0
    assert "内容无变化" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == after
    assert (chome / "config.toml.bak-knotcord").stat().st_mtime_ns == bak_mtime
    assert (chome / "config.toml.bak-knotcord").read_text(encoding="utf-8") == USER_CFG
    # 撤销：恢复成接入前
    assert _run("--remove", "--apply") == 0
    assert cfg.read_text(encoding="utf-8") == USER_CFG
    assert _hooks(chome) == FOREIGN_HOOKS


def test_apply_creates_missing_files_and_only_touches_two_files(chome):
    (chome / "sessions").mkdir()
    (chome / "AGENTS.md").write_text("别动我", encoding="utf-8")
    before = {p.name for p in chome.iterdir()}
    assert _run("--apply") == 0
    assert {p.name for p in chome.iterdir()} - before == {"config.toml", "hooks.json"}
    assert (chome / "AGENTS.md").read_text(encoding="utf-8") == "别动我"
    assert "[mcp_servers.knotcord]" in (chome / "config.toml").read_text(encoding="utf-8")


def test_remove_on_clean_home_creates_nothing(chome, capsys):
    assert _run("--remove", "--apply") == 0
    assert list(chome.iterdir()) == []


def test_missing_codex_dir_is_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope"))
    assert _run("--apply") == 1
    assert "找不到 Codex 配置目录" in capsys.readouterr().out
    assert not (tmp_path / "nope").exists()


def test_conflict_aborts_without_touching_any_file(chome, capsys):
    bad = USER_CFG + '\n[mcp_servers.knotcord]\ncommand = "x"\n'
    (chome / "config.toml").write_text(bad, encoding="utf-8")
    assert _run("--apply") == 1
    assert (chome / "config.toml").read_text(encoding="utf-8") == bad
    assert not (chome / "hooks.json").exists() and not list(chome.glob("*.bak-knotcord"))


def test_invalid_hooks_json_aborts_before_writing_config(chome):
    (chome / "config.toml").write_text(USER_CFG, encoding="utf-8")
    (chome / "hooks.json").write_text("{oops", encoding="utf-8")
    assert _run("--apply") == 1
    assert (chome / "config.toml").read_text(encoding="utf-8") == USER_CFG


def test_relative_python_rejected(chome, capsys):
    assert cli.main(["install", "--codex", "--python", "python", "--apply"]) == 1
    assert "绝对路径" in capsys.readouterr().out
    assert not (chome / "config.toml").exists()


def test_remove_without_codex_is_error(chome, capsys):
    assert cli.main(["install", "--remove", "--apply"]) == 1
    assert "--codex" in capsys.readouterr().out


def test_install_without_codex_does_not_touch_codex_home(chome, capsys):
    assert cli.main(["install", "--dry-run"]) == 0
    assert list(chome.iterdir()) == []


# ---------------------------------------------------------------- hook 入口的 Codex 分支

def _hook(event, payload, *args, env=None, shell=None):
    e = {k: v for k, v in os.environ.items() if k != "KNOTCORD_INTERNAL"}
    e.update(env or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    cmd = [PY, "-I", "-S", "-X", "utf8", str(ENTRY), event, *args]
    if shell:  # 模拟 Codex 在 Windows 里经 PowerShell 转发
        cmd = [shell, "-NoProfile", "-NonInteractive", "-Command",
               codex_setup.hook_command(PY, str(ENTRY), event)]
    return subprocess.run(cmd, input=data, capture_output=True, env=e, timeout=30)


def _seed_note(conn, cwd, title):
    proj = project.project_key(cwd, env={})
    conn.execute("INSERT INTO notes(project, ts, type, title, body) VALUES(?,?,?,?,?)",
                 (proj, int(time.time()), "decision", title, "b"))
    conn.commit()
    inject.render(conn, proj)          # 开局注入读的是预拼好存进库里的文本，先拼一次
    return proj


def test_codex_session_start_outputs_ascii_json(conn, tmp_path):
    cwd = tmp_path / "项目甲"
    (cwd / ".git").mkdir(parents=True)
    proj = _seed_note(conn, str(cwd), "选方案乙：中文标题")
    r = _hook("session-start", {"source": "startup", "session_id": "c1", "cwd": str(cwd)}, "--codex")
    assert r.returncode == 0 and r.stderr == b""
    r.stdout.decode("ascii")                                          # 全是 ASCII，GBK 重编码也不会坏
    msg = json.loads(r.stdout)
    assert msg["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert msg["hookSpecificOutput"]["additionalContext"] == inject.render(conn, proj)
    assert "选方案乙：中文标题" in msg["hookSpecificOutput"]["additionalContext"]


def test_codex_session_start_ignores_claude_project_dir(conn, tmp_path):
    cwd = tmp_path / "codexproj"
    (cwd / ".git").mkdir(parents=True)
    _seed_note(conn, str(cwd), "只属于 codexproj")
    r = _hook("session-start", {"source": "startup", "session_id": "c1", "cwd": str(cwd)}, "--codex",
              env={"CLAUDE_PROJECT_DIR": str(tmp_path / "elsewhere")})
    assert "只属于 codexproj" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_claude_session_start_unchanged_plain_text(conn, tmp_path):
    cwd = tmp_path / "plainproj"
    (cwd / ".git").mkdir(parents=True)
    proj = _seed_note(conn, str(cwd), "老路径不变")
    r = _hook("session-start", {"source": "startup", "session_id": "c1", "cwd": str(cwd)},
              env={"CLAUDE_PROJECT_DIR": str(cwd)})
    assert r.stdout.decode("utf-8") == inject.render(conn, proj) + "\n"


def test_codex_resume_injects_nothing(conn, tmp_path):
    cwd = tmp_path / "p"
    (cwd / ".git").mkdir(parents=True)
    _seed_note(conn, str(cwd), "x")
    assert _hook("session-start", {"source": "resume", "session_id": "c", "cwd": str(cwd)}, "--codex").stdout == b""


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="本机没有 pwsh")
def test_codex_hook_through_powershell_keeps_chinese(conn, tmp_path):
    cwd = tmp_path / "中文目录"
    (cwd / ".git").mkdir(parents=True)
    _seed_note(conn, str(cwd), "经 PowerShell 转发后中文不坏")
    r = _hook("session-start", {"source": "startup", "session_id": "c1", "cwd": str(cwd)}, shell="pwsh")
    assert r.returncode == 0, r.stderr
    ctx = json.loads(r.stdout.decode("utf-8", errors="strict"))["hookSpecificOutput"]["additionalContext"]
    assert "经 PowerShell 转发后中文不坏" in ctx


def test_codex_session_end_enqueues_with_codex_client(conn, tmp_path):
    marker = tmp_path / "drain.txt"
    drain = json.dumps([PY, "-c", f"open({str(marker)!r}, 'w').write('x')"])
    r = _hook("session-end", {"session_id": "cs1", "transcript_path": "C:\\r\\rollout.jsonl", "cwd": "C:\\w"},
              "--codex", env={"KNOTCORD_DRAIN_CMD": drain})
    assert r.returncode == 0 and r.stdout == b""
    rows = conn.execute("SELECT session_id, client, transcript_path, reason FROM pending").fetchall()
    assert [tuple(x) for x in rows] == [("cs1", "codex", "C:\\r\\rollout.jsonl", "end")]
    end = time.time() + 8
    while not marker.exists() and time.time() < end:
        time.sleep(0.05)
    assert marker.exists()


def test_claude_session_end_still_claude_client(conn, tmp_path):
    drain = json.dumps([PY, "-c", "pass"])
    _hook("session-end", {"session_id": "cl1", "transcript_path": "C:\\t.jsonl"}, env={"KNOTCORD_DRAIN_CMD": drain})
    assert conn.execute("SELECT client FROM pending").fetchone()[0] == "claude"


# ---------------------------------------------------------------- 项目名：两边对同一个文件夹给同一个名字

def test_project_key_spellings_agree(tmp_path):
    root = tmp_path / "MixedCase"
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    want = "MixedCase"
    assert project.project_key(str(sub), env={}) == want                      # 子目录：向上找到 .git 那层
    assert project.project_key(str(root).upper(), env={}) == want             # 大小写写法不同
    assert project.project_key(str(root).lower(), env={}) == want
    assert project.project_key("\\\\?\\" + str(root), env={}) == want         # \\?\ 前缀
    assert project.project_key(str(root).replace("\\", "/") + "/", env={}) == want


def test_project_key_wsl_path_and_worktree(tmp_path):
    root = tmp_path / "Work"
    root.mkdir()
    (root / ".git").write_text("gitdir: ../.git/worktrees/x", encoding="utf-8")   # worktree：.git 是文件
    drive, rest = str(root).split(":", 1)
    wsl = "/mnt/" + drive.lower() + rest.replace("\\", "/")
    assert project.project_key(wsl, env={}) == "Work"
    assert project.project_key(str(root), env={}) == "Work"


def test_project_key_nonexistent_path_keeps_own_name():
    assert project.project_key("Z:\\no\\such\\Foo", env={}) == "Foo"


# ---------------------------------------------------------------- 两个客户端共用一份记忆

def test_shared_memory_same_project_both_clients_searchable(conn, tmp_path):
    cwd = tmp_path / "SharedProj"
    (cwd / "pkg").mkdir(parents=True)
    (cwd / ".git").mkdir()
    # Claude 那边从子目录启动，Codex 那边写成全小写的路径：两边都应归到同一个项目
    claude_cwd, codex_cwd = str(cwd / "pkg"), str(cwd).lower()

    tp = tmp_path / "claude.jsonl"
    srvharness.make_transcript(tp, "claude-sess-1", claude_cwd, turns=1, text="甲")
    ct.ingest_session(conn, "claude-sess-1", str(tp), cwd=claude_cwd)

    r = tic.Rollout(codex_cwd)
    r.user("独特口令紫罗兰咖啡机")
    r.agent("好的，已处理。")
    rp = tic.rollout_path(tmp_path / "sessions")
    r.write(rp)
    cx.ingest_file(conn, str(rp))

    rows = conn.execute("SELECT id, client, project FROM sessions ORDER BY client").fetchall()
    assert [(x["client"], x["project"]) for x in rows] == [("claude", "SharedProj"), ("codex", "SharedProj")]

    # 按项目检索：两边的提示都找得到
    codex_hit = api.search(conn, "紫罗兰咖啡机", project="SharedProj")
    claude_hit = api.search(conn, "甲甲甲甲甲甲甲甲甲甲", project="SharedProj")
    assert "SharedProj prompt" in codex_hit and "紫罗兰咖啡机" in codex_hit
    assert "SharedProj prompt" in claude_hit

    # 来源区分：events.session_id 连 sessions.client
    src = {x["client"]: x["n"] for x in conn.execute(
        "SELECT s.client AS client, COUNT(*) AS n FROM events e JOIN sessions s ON s.id = e.session_id "
        "WHERE e.project = 'SharedProj' AND e.kind = 'prompt' GROUP BY s.client")}
    assert src == {"claude": 1, "codex": 1}


def test_shared_memory_codex_session_gets_same_injection_as_claude(conn, tmp_path):
    cwd = tmp_path / "InjProj"
    (cwd / ".git").mkdir(parents=True)
    # Claude 会话提炼出的笔记，Codex 开局也要读得到（同一个项目名）
    proj = _seed_note(conn, str(cwd), "Claude 里定下的决定甲")
    assert proj == "InjProj"
    r = _hook("session-start", {"source": "startup", "session_id": "codex-new", "cwd": str(cwd)}, "--codex")
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Claude 里定下的决定甲" in ctx
