"""project、filters、tokens、config、paths、log 和隔离检查。"""
import os
import subprocess
import sys

import pytest

from knotcord.core import config, filters, log, paths, project, tokens

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- project

def test_project_key_git_dir(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert project.project_key(str(sub), {}) == "myrepo"
    assert project.project_key(str(repo), {}) == "myrepo"


def test_project_key_git_file_worktree(tmp_path):
    wt = tmp_path / "wt-feature"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert project.project_key(str(wt / "src" if (wt / "src").exists() else wt), {}) == "wt-feature"


def test_project_key_no_git_uses_cwd_name(tmp_path):
    d = tmp_path / "plain" / "dir"
    d.mkdir(parents=True)
    # tmp_path 往上不应有 .git；万一有，只验证返回的是某个目录名
    key = project.project_key(str(d), {})
    assert key in ("dir", *[p.name for p in d.parents if (p / ".git").exists()])


def test_project_key_env_wins(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    assert project.project_key(str(repo), {"CLAUDE_PROJECT_DIR": "E:\\Work\\Other"}) == "Other"
    assert project.project_key(str(repo), {"CLAUDE_PROJECT_DIR": "E:\\Work\\Other\\"}) == "Other"
    assert project.project_key(str(repo), {"CLAUDE_PROJECT_DIR": ""}) == "myrepo"


def test_project_key_chinese_and_env_default(tmp_path, monkeypatch):
    repo = tmp_path / "中文项目"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert project.project_key(str(repo)) == "中文项目"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "来自环境"))
    assert project.project_key(str(repo)) == "来自环境"


# ---------------------------------------------------------------- filters

@pytest.fixture
def cfg():
    return config.load()


@pytest.mark.parametrize("name", [
    "Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch", "TodoWrite", "Skill",
    "mcp__ccd_turn__update_plan", "mcp__ccd_turn__wrap_up", "mcp__ccd_session__mark_chapter",
    "mcp__Claude_Browser__computer_screenshot", "mcp__Claude_Browser__read_page",
    "mcp__Claude_Browser__get_page_text", "mcp__Claude_Browser__find",
    "mcp__Claude_Browser__read_console_messages", "mcp__Claude_Browser__read_network_requests",
    "mcp__plugin_claude-mem_mcp-search__search", "mcp__mcp-search__timeline",
    "mcp__plugin_knotcord_knotcord__search",
])
def test_skip_whole_tools(name, cfg):
    assert filters.skip_tool(name, {}, cfg) is True


@pytest.mark.parametrize("name", ["Edit", "Write", "Agent", "Bash", "PowerShell", "NotebookEdit",
                                  "mcp__github__create_pull_request"])
def test_keep_tools(name, cfg):
    assert filters.skip_tool(name, {"command": "python build.py"}, cfg) is False


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", [
    "ls -la", "ls", "dir E:\\x", "cat a.txt", "type a.txt", "head -n 5 f", "tail -f f", "echo hi",
    "pwd", "which python", "where python", "git status", "git log --oneline", "git diff HEAD~1",
    "git show abc", "rtk read file.py", "rtk git status", "Get-ChildItem -Recurse", "get-content x.log",
    "Test-Path C:\\x", "  ls  ",
])
def test_skip_commands(tool, cmd, cfg):
    assert filters.skip_tool(tool, {"command": cmd}, cfg) is True


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", [
    "lsof -i", "typecheck src", "python -m pytest", "git commit -m x", "git push", "npm run build",
    "Remove-Item x", "catalog build", "echoserver start",
])
def test_keep_commands(tool, cmd, cfg):
    assert filters.skip_tool(tool, {"command": cmd}, cfg) is False


def test_filters_robust_inputs(cfg):
    assert filters.skip_tool("Bash", None, cfg) is False
    assert filters.skip_tool("Bash", {"command": 5}, cfg) is False
    assert filters.skip_tool("", None, cfg) is False
    assert filters.skip_tool("Edit", {"command": "ls"}, cfg) is False  # 前缀只对 shell 工具生效


def test_filters_follow_config(tmp_path):
    (paths.home()).mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(
        '[filters]\nskip_tools = ["Edit"]\nskip_cmd_prefixes = ["make"]\n', encoding="utf-8")
    c = config.load()
    assert filters.skip_tool("Edit", {}, c) is True
    assert filters.skip_tool("Read", {}, c) is False
    assert filters.skip_tool("Bash", {"command": "make all"}, c) is True
    assert filters.skip_tool("Bash", {"command": "ls"}, c) is False


# ---------------------------------------------------------------- tokens

def test_tokens_estimate():
    assert tokens.estimate("") == 0
    assert tokens.estimate("汉" * 100) == 110
    assert tokens.estimate("a" * 350) == 110  # 350/3.5=100，再加 10%
    assert tokens.estimate("汉字abcdefg") == 5  # (2 + 7/3.5) * 1.1 = 4.4 -> 5
    assert tokens.estimate("x") == 1


# ---------------------------------------------------------------- config / paths

def test_config_defaults(cfg):
    assert cfg.get("extract.chars_codex") == 30000
    assert cfg.get("budget.daily_cny") == 3.0
    assert cfg.get("recall.enabled") is False
    assert cfg.get("search.vector") is True
    assert cfg.get("inject.hard_chars") == 9000
    assert "Read" in cfg.get("filters.skip_tools")
    assert cfg.get("nope") is None and cfg.get("nope", 3) == 3


def test_config_override_and_bad_values(trace_home):
    trace_home.mkdir(parents=True)
    paths.config_path().write_text(
        '[extract]\nchars_codex = 1234\nretries = "many"\n[budget]\ndaily_cny = 5\n[x]\ny = 1\n',
        encoding="utf-8")
    c = config.load()
    assert c.get("extract.chars_codex") == 1234
    assert c.get("extract.retries") == 3  # 类型不对：忽略
    assert c.get("budget.daily_cny") == 5
    assert c.get("x.y") == 1  # 未知键保留


def test_config_broken_toml_falls_back(trace_home):
    trace_home.mkdir(parents=True)
    paths.config_path().write_text("this is = = not toml [", encoding="utf-8")
    assert config.load().get("extract.chars_codex") == 30000


def test_defaults_not_shared_between_loads():
    a = config.load()
    a.get("filters.skip_tools").append("X")
    assert "X" not in config.load().get("filters.skip_tools")


def test_paths(trace_home):
    assert paths.home() == trace_home
    assert not trace_home.exists()
    assert paths.db_path() == trace_home / "knotcord.db"
    assert paths.config_path() == trace_home / "config.toml"
    assert not trace_home.exists()  # db_path/config_path 不建目录
    assert paths.tmp_dir().is_dir() and paths.logs_dir().is_dir() and paths.backups_dir().is_dir()


def test_paths_default_home(monkeypatch):
    monkeypatch.delenv("KNOTCORD_HOME")
    assert paths.home() == paths.Path.home() / ".knotcord"


# ---------------------------------------------------------------- log

def read_log():
    files = list(paths.logs_dir().glob("knotcord-*.log"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8").splitlines()


def test_log_writes_utf8_file_not_stdout(capsys):
    log._reset_for_tests()
    lg = log.get_logger("unit")
    lg.info("中文消息 ok")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    lines = read_log()
    assert len(lines) == 1 and "中文消息 ok" in lines[0] and "INFO" in lines[0]
    assert paths.logs_dir().glob("knotcord-????-??-??.log")


def test_log_truncates_and_single_line():
    log._reset_for_tests()
    log.get_logger().error("x" * 5000 + "\nsecond")
    log.get_logger().error("a\nb\r\nc")
    lines = read_log()
    assert len(lines) == 2
    assert len(lines[0]) <= 2000
    assert "a | b | c" in lines[1]


def test_log_dedupe(monkeypatch):
    log._reset_for_tests()
    clock = [10_000.0]
    monkeypatch.setattr(log, "_now", lambda: clock[0])
    lg = log.get_logger("dup")
    for _ in range(5):
        lg.error("same error")
    assert len(read_log()) == 1
    clock[0] += 601
    lg.error("same error")
    lines = read_log()
    assert len(lines) == 2 and "又出现 5 次" in lines[1] and "same error" in lines[1]
    # 被别的消息打断时补记欠的次数
    lg.error("same error")
    lg.error("same error")
    lg.error("different")
    lines = read_log()
    assert "又出现 2 次" in lines[2] and "different" in lines[3]


def test_log_get_logger_idempotent():
    a, b = log.get_logger("z"), log.get_logger("z")
    assert a is b and a.parent.handlers == log.get_logger().handlers
    assert len(log.get_logger().handlers) == 1
    assert log.get_logger().propagate is False


# ---------------------------------------------------------------- 隔离

def test_core_imports_only_stdlib():
    code = (
        "import sys; sys.path.insert(0, r'" + ROOT + "'); "
        "import knotcord.core.db, knotcord.core.index, knotcord.core.redact, knotcord.core.project, "
        "knotcord.core.lease, knotcord.core.proc, knotcord.core.log, knotcord.core.config, "
        "knotcord.core.filters, knotcord.core.tokens, knotcord.core.paths; "
        "assert not ({'numpy','onnxruntime','mcp','tokenizers'} & set(sys.modules)), 'imported third party'; "
        "print('ok')"
    )
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
