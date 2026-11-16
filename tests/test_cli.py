"""命令行：usage / doctor / migrate / reindex / install / uninstall，以及提交进仓库的插件清单。"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import srvharness as H
from test_migrate_claude_mem import make_src
from knotcord import cli, procs
from knotcord.core import db, lease, paths

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*argv, capsys=None):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out if capsys else ""


def run_boot(home, *argv, timeout=60):
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(H.BOOT), *argv], env=H.child_env(home),
                       capture_output=True, timeout=timeout, creationflags=H.CREATE_NO_WINDOW)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


# ---------------------------------------------------------------- usage / doctor

def test_usage_without_db(trace_home, capsys):
    rc, out = run_cli("usage", capsys=capsys)
    assert rc == 0 and "尚无数据库" in out
    assert not (trace_home / "knotcord.db").exists()  # 只读命令不凭空建库


def test_usage_with_db(trace_home, conn, capsys):
    rc, out = run_cli("usage", capsys=capsys)
    assert rc == 0 and "提炼 0 次" in out and "积压 0 批" in out


def test_doctor_without_db(trace_home, capsys):
    rc, out = run_cli("doctor", capsys=capsys)
    assert rc == 0 and "数据库不存在" in out and "没有发现问题" in out
    assert not (trace_home / "knotcord.db").exists()


def test_doctor_reports_everything(trace_home, conn, capsys):
    now = time.time()
    # 一个活着的进程登记（用测试进程自己）、一个已死的
    procs.register(conn, "server")
    conn.execute("INSERT INTO kv(key, value) VALUES('proc:4000000', ?)",
                 (json.dumps({"pid": 4000000, "role": "drain", "started": now - 99, "beat": now - 99}),))
    assert lease.Lease(conn, owner=f"{os.getpid()}:a:b").try_acquire()
    conn.execute("INSERT INTO sessions(id, client, project, last_seen) VALUES('s1','claude','p',?)", (int(now),))
    conn.execute("INSERT INTO events(session_id, project, ts, uuid, kind, body) VALUES('s1','p',?,'e1','prompt',?)",
                 (int(now), "字" * 100))
    conn.execute("INSERT INTO batches(id, session_id, created, status, provider, error) "
                 "VALUES(7,'s1',?, 'failed','codex','Codex: 额度用完')", (int(now),))
    conn.execute("INSERT INTO docs(src, src_id, project, ts, active) VALUES('note', 1, 'p', 1, 1)")
    conn.execute("INSERT INTO pending(session_id, client, transcript_path, reason, enqueued_at) VALUES('s1','c','x','stop',1)")
    paths.tmp_dir()
    (paths.tmp_dir() / "left.txt").write_text("x")
    rc, out = run_cli("doctor", capsys=capsys)
    assert rc == 0
    assert f"pid {os.getpid()} server" in out and "活着" in out
    assert "pid 4000000 drain" in out and "已死" in out
    assert "租约:" in out and str(os.getpid()) in out and "还剩" in out
    assert "积压 1 批" in out and "pending 待处理: 1 行" in out
    assert "批 7" in out and "额度用完" in out
    assert "缺向量 1 条" in out
    assert "tmp\\ 里的文件: 1 个" in out
    assert "knotcord.db（含 -wal、-shm）" in out


def test_doctor_warns_on_corrupt_flag_and_newer_schema(trace_home, conn, capsys):
    db.kv_set(conn, "db_corrupt", "数据库损坏，已停止记录")
    conn.execute("PRAGMA user_version=999")
    rc, out = run_cli("doctor", capsys=capsys)
    assert rc == 0 and "需要注意" in out and "已停止记录" in out and "版本比本进程认识的新" in out


def test_doctor_warns_when_legacy_snapshot_exists(trace_home, capsys):
    leg = trace_home / "legacy"
    leg.mkdir(parents=True)
    (leg / "claude-mem.db").write_bytes(b"x" * 10)
    rc, out = run_cli("doctor", capsys=capsys)
    assert "迁移快照" in out


def test_boot_doctor_and_usage_real_process(trace_home):
    rc, out, err = run_boot(trace_home, "usage")
    assert rc == 0 and "尚无数据库" in out and err == ""
    rc, out, err = run_boot(trace_home, "doctor")
    assert rc == 0 and "KnotCord 0.1.0" in out and err == ""
    db.connect().close()
    rc, out, err = run_boot(trace_home, "usage")
    assert rc == 0 and "提炼" in out
    rc, out, err = run_boot(trace_home, "doctor")
    assert rc == 0 and "[进程与租约]" in out and "没有发现问题" in out


# ---------------------------------------------------------------- migrate / reindex

def test_migrate_command(trace_home, tmp_path, capsys):
    src = tmp_path / "claude-mem.db"
    make_src(str(src))
    rc, out = run_cli("migrate", "--src", str(src), "--no-snapshot", capsys=capsys)
    assert rc == 0 and "全部一致" in out and "observations: 源 8 / 目标 8" in out
    assert not (trace_home / "legacy").exists()


def test_migrate_missing_source(trace_home, tmp_path, capsys):
    rc, out = run_cli("migrate", "--src", str(tmp_path / "nope.db"), capsys=capsys)
    assert rc == 1 and "源库不存在" in out


def test_reindex_command(trace_home, tmp_path, capsys):
    mem = tmp_path / "mem" / "C--work-Demo" / "memory"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text("---\nname: 甲\ndescription: 描述\n---\n正文\n", encoding="utf-8")
    rc, out = run_cli("reindex", "--memroot", str(tmp_path / "mem"), capsys=capsys)
    assert rc == 0 and "更新 1 个" in out
    rc, out = run_cli("reindex", "--memroot", str(tmp_path / "mem"), capsys=capsys)
    assert "更新 0 个" in out


# ---------------------------------------------------------------- install / uninstall

def snapshot(d: Path) -> dict:
    return {str(p): p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()}


def test_install_default_is_dry_run_and_writes_nothing(trace_home, capsys):
    before = snapshot(ROOT / "hooks"), (ROOT / ".mcp.json").stat().st_mtime_ns
    rc, out = run_cli("install", capsys=capsys)
    assert rc == 0 and "预演" in out and "claude plugin validate" in out and "--plugin-dir" in out
    assert "将生成" in out and "写入 " not in out
    rc, out = run_cli("install", "--dry-run", capsys=capsys)
    assert "预演" in out
    assert (snapshot(ROOT / "hooks"), (ROOT / ".mcp.json").stat().st_mtime_ns) == before
    assert not trace_home.exists() or not any(trace_home.iterdir())  # ~/.knotcord 也没被碰


def test_install_apply_writes_only_target_files(trace_home, tmp_path, monkeypatch, capsys):
    """把生成目标指到临时目录再 --apply：只写那两个文件，内容和提交进仓库的一致（除解释器路径）。"""
    plugin = tmp_path / "plugin"
    real = cli.generated_files
    monkeypatch.setattr(cli, "generated_files", lambda python, root=plugin: real(python, plugin))
    rc, out = run_cli("install", "--apply", "--python", r"X:\py\python.exe", capsys=capsys)
    assert rc == 0 and "【执行】" in out
    written = sorted(str(p.relative_to(plugin)) for p in plugin.rglob("*") if p.is_file())
    assert written == [".mcp.json", "hooks\\hooks.json"] or written == [".mcp.json", "hooks/hooks.json"]
    assert json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["knotcord"]["command"] == r"X:\py\python.exe"
    assert not trace_home.exists() or not any(trace_home.iterdir())  # 除目标文件外什么都没写
    rc, out = run_cli("install", "--apply", "--python", r"X:\py\python.exe", capsys=capsys)  # 再来一次：内容无变化
    assert "内容无变化" in out


def test_uninstall_lists_sizes_and_deletes_nothing(trace_home, capsys):
    (trace_home / "logs").mkdir(parents=True)
    (trace_home / "logs" / "a.log").write_bytes(b"x" * 2048)
    (trace_home / "knotcord.db").write_bytes(b"y" * 10)
    rc, out = run_cli("uninstall", "--apply", capsys=capsys)
    assert rc == 0 and "只列清单" in out and "logs" in out and "knotcord.db" in out and "合计" in out
    assert (trace_home / "logs" / "a.log").exists() and (trace_home / "knotcord.db").exists()


# ---------------------------------------------------------------- 提交进仓库的清单

def _committed():
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    return mcp, mcp["mcpServers"]["knotcord"]["command"]


def test_committed_manifests_match_generator():
    _, python = _committed()
    gen = cli.generated_files(python)
    for path, text in gen.items():
        assert path.read_text(encoding="utf-8") == text, f"{path} 与 install 生成的不一致"


def test_manifest_paths_exist_and_hooks_follow_template():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "PreCompact", "SessionEnd"}
    for ev in ("SessionStart", "SessionEnd"):
        h = hooks[ev][0]["hooks"][0]
        assert h["type"] == "command" and h["timeout"] <= 5
        assert h["args"][:4] == ["-I", "-S", "-X", "utf8"]  # 细则 6.3：只用标准库，不加载 site
        script = h["args"][4].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
        assert Path(script).is_file()
    for ev in ("Stop", "PreCompact"):
        h = hooks[ev][0]["hooks"][0]
        assert h["type"] == "mcp_tool" and h["server"] == "plugin:knotcord:knotcord" and h["tool"] == "ingest"
        assert h["input"]["session_id"] == "${session_id}" and h["timeout"] <= 5
    assert "last" in hooks["Stop"][0]["hooks"][0]["input"] and "last" not in hooks["PreCompact"][0]["hooks"][0]["input"]
    mcp, python = _committed()
    assert Path(python).is_absolute() and python.lower().endswith("python.exe")  # 解释器本体，不是 venv 启动器
    assert "venv" not in python.lower()
    args = mcp["mcpServers"]["knotcord"]["args"]
    assert args[:3] == ["-I", "-X", "utf8"] and args[-1] == "serve"
    assert Path(args[3].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))).is_file()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "knotcord" and re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])


def test_skill_frontmatter_and_content():
    text = (ROOT / "skills" / "knotcord-recall" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", text)
    assert m and m.group(1) == "knotcord-recall" and len(m.group(2)) > 40
    for word in ("search", "timeline", "get", "内置记忆文件", "会话原文"):
        assert word in text
    assert text.index("内置记忆文件") < text.index("KnotCord：") < text.index("会话原文（")  # 顺序：记忆文件 → KnotCord → 原文
    for p in "nuspme":
        assert f"| {p} |" in text
