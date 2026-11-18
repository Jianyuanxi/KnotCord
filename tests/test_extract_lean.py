"""提炼前去噪（extract/lean.py）和提炼告警、故障留证。"""

import time

from knotcord import inject
from knotcord.core import db
from knotcord.extract import budget, lean, pipeline, prompt
from knotcord.ingest.trim import join_body


def ev(kind="tool", tool="Bash", brief="", body="", ts=1_795_000_000):
    return {"kind": kind, "tool": tool, "brief": brief, "body": body, "ts": ts}


SCR = r"C:\Users\someone\AppData\Local\Temp\claude\0a1b2c\12345678-1234-1234-1234-123456789abc\scratchpad"


# ------------------------------------------------------------------ 路径与压缩

def test_short_paths_scratch_and_home():
    s = lean.short_paths(SCR + r"\a.py and C:/Users/someone/proj/x.md and /c/Users/someone/y")
    assert s == "<scratch>/a.py and ~/proj/x.md and ~/y"


def test_squeeze_dedup_ansi_progress_and_rescue():
    text = "\x1b[32mstart\x1b[0m\n\n" + "same\n" * 5 + "45%\n" + "\n".join(f"line{i}" for i in range(30)) \
        + "\nERROR: boom\n" + "\n".join(f"tail{i}" for i in range(5)) + "\nShell cwd was reset to E:\\x"
    out = lean.squeeze(text, 4, 1000)
    assert out.startswith("start\nsame")
    assert "\x1b" not in out and "45%" not in out and "Shell cwd" not in out
    assert "ERROR: boom" in out            # 中间的报错被捞回来
    assert "…[省略" in out and out.endswith("tail4")


def test_squeeze_collapses_repeats():
    assert lean.squeeze("a\na\na\nb", 10, 100) == "a\n(上行重复 2 次)\nb"


# ------------------------------------------------------------------ 按工具

def test_bash_success_output_trimmed_failure_kept_longer():
    out = "\n".join(f"row {i} " + "x" * 40 for i in range(80))
    ok = lean.render(ev(brief="列文件", body=join_body("$ ls -la", "[exit 0]\n" + out)))
    bad = lean.render(ev(brief="跑测试", body=join_body("$ pytest", "[exit 1]\n" + out + "\nFAILED test_x")))
    assert ok.startswith("工具 Bash: 列文件\n    $ ls -la\n    => [exit 0]")
    assert len(ok) < 400 and len(bad) > len(ok)
    assert "FAILED test_x" in bad


def test_bash_brief_equal_to_command_not_repeated():
    r = lean.render(ev(brief="python run.py", body=join_body("$ python run.py", "[exit 0]\nok")))
    assert r.count("python run.py") == 1


def test_write_scratch_only_name():
    body = join_body(SCR + "\\tool.py\n内容：\n" + "print(1)\n" * 50, "ok")
    assert lean.render(ev(tool="Write", body=body)) == "工具 Write: <scratch>/tool.py（临时文件）"


def test_write_project_file_keeps_head():
    body = join_body("E:\\p\\a.md\n内容：\n# 标题\n正文", "ok")
    r = lean.render(ev(tool="Write", body=body))
    assert "E:\\p\\a.md" in r and "# 标题" in r


def test_edit_keeps_old_and_new():
    body = join_body("E:\\p\\a.py\n旧：\nx = 1\n新：\nx = 2", "ok")
    r = lean.render(ev(tool="Edit", body=body))
    assert "旧：x = 1" in r and "新：x = 2" in r


def test_quiet_browser_tool_one_line_but_errors_kept():
    ok = lean.render(ev(tool="mcp__Claude_Browser__navigate", brief="navigate https://a.com",
                        body=join_body('{"url":"https://a.com"}', "页面标题…" * 50)))
    assert ok == "工具 navigate: navigate https://a.com"
    bad = lean.render(ev(tool="mcp__Claude_Browser__navigate", brief="navigate https://a.com",
                         body=join_body('{"url":"https://a.com"}', "[错误] denied")))
    assert "denied" in bad


def test_prompt_and_assistant_kept():
    assert lean.render(ev(kind="prompt", tool=None, body="帮我看看 " + SCR + "\\a.txt")) == "用户: 帮我看看 <scratch>/a.txt"
    assert lean.render(ev(kind="assistant", tool=None, body="好的")) == "助手: 好的"


def test_event_view_switch():
    e = ev(tool="Write", body=join_body(SCR + "\\t.py\n内容：\nprint(1)", "ok"))
    assert "print(1)" in prompt.event_view(e, False)
    assert "print(1)" not in prompt.event_view(e, True)
    assert prompt.event_len(e, True) < prompt.event_len(e, False)


# ------------------------------------------------------------------ 告警

NOW = 1_795_000_000


def _cfg():
    from knotcord.core import config
    return config.load()


def test_health_balance_exhausted_then_cleared(conn):
    budget.mark_unavailable(conn, budget.DEEPSEEK, None, NOW, reason="DeepSeek 余额不足")
    assert budget.cooldown_until(conn, budget.DEEPSEEK) == NOW + 3600  # 要用户处理：1 小时后再试
    lines = budget.health_lines(conn, _cfg(), NOW + 5)
    assert len(lines) == 1 and "余额不足" in lines[0] and "已暂停" in lines[0]
    # 同一原因再报一次，起始时间不变
    budget.mark_unavailable(conn, budget.DEEPSEEK, None, NOW + 4000, reason="DeepSeek 余额不足")
    assert budget.unavailable_info(conn, budget.DEEPSEEK)["since"] == NOW
    budget.mark_ok(conn, budget.DEEPSEEK)
    assert budget.health_lines(conn, _cfg(), NOW + 5000) == []


def test_health_soft_reason_quiet_when_codex_ok(conn):
    budget.mark_unavailable(conn, budget.DEEPSEEK, None, NOW, reason="DeepSeek 限流")
    assert budget.cooldown_until(conn, budget.DEEPSEEK) == NOW + 600
    assert budget.health_lines(conn, _cfg(), NOW + 1) == []
    budget.mark_unavailable(conn, budget.CODEX, None, NOW, reason="额度")
    lines = budget.health_lines(conn, _cfg(), NOW + 1)
    assert len(lines) == 1 and "都不可用" in lines[0]


def test_health_daily_cap_and_dead_batches(conn):
    cfg = _cfg()
    budget.record(conn, budget.DEEPSEEK, cost_cny=float(cfg.get("budget.daily_cny")), now=NOW)
    conn.execute("INSERT INTO batches(session_id, created, status, error) VALUES('s', ?, 'dead', '输出校验失败')",
                 (NOW - 60,))
    lines = budget.health_lines(conn, cfg, NOW)
    assert any("到上限" in x for x in lines)
    assert any("1 批提炼失败作废" in x and "输出校验失败" in x for x in lines)


def test_health_line_first_in_injection(conn):
    budget.mark_unavailable(conn, budget.DEEPSEEK, None, time.time(), reason="DeepSeek 余额不足")
    out = inject.read_for_hook(conn, "proj")
    assert out.startswith("<knotcord-context") and out.split("\n")[1].startswith("! KnotCord 提炼告警")


# ------------------------------------------------------------------ 故障留证

def test_save_failed_output_keeps_newest(trace_home, monkeypatch):
    monkeypatch.setattr(pipeline, "FAILED_KEEP", 3)
    names = [pipeline._save_failed_output(i, 1, "deepseek", "校验失败", f"raw {i}") for i in range(5)]
    d = trace_home / "logs" / "failed"
    files = sorted(p.name for p in d.glob("*.txt"))
    assert names[-1] == "b4-a1.txt" and len(files) == 3 and "b4-a1.txt" in files
    assert "raw 4" in (d / "b4-a1.txt").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 提炼输出格式

def test_state_card_null_means_unchanged():
    from knotcord.extract import validate
    v = validate.validate({"skip": False, "observations": [], "state_card": None},
                          n_notes=0, n_unfinished=0, n_events=1)
    assert v.card is None
    many = [{"type": "change", "title": f"t{i}"} for i in range(20)]
    v = validate.validate({"observations": many}, n_notes=0, n_unfinished=0, n_events=1)
    assert len(v.notes) == validate.OBS_N


# ------------------------------------------------------------------ 坏 JSON 自动修复（11-17 DeepSeek 实测的几类）

def _parse(text):
    from knotcord.extract import validate
    fixes = []
    return validate.parse_model_text(text, fixes), fixes


def test_repair_truncated_after_observations():
    obj, fixes = _parse('{"skip": false, "observations": [{"type": "change", "title": "t", "source": [1, 2]}]')
    assert obj["observations"][0]["title"] == "t" and fixes == ["补齐中途截断的结尾"]
    obj, _ = _parse('{"skip": false, "observations": [{"type": "change", "title": "半句')
    assert obj["observations"][0]["title"] == "半句"


def test_repair_bad_escape_keeps_valid_ones():
    obj, fixes = _parse(r'{"skip": false, "observations": [], "summary": {"learned": "写 \small 和 \\x 路径 E:\\a\b"}}')
    assert obj["summary"]["learned"] == "写 \\small 和 \\x 路径 E:\\a\b"
    assert fixes == ["补全非法反斜杠转义"]


def test_repair_chinese_separator_and_dsml():
    obj, fixes = _parse('{"skip":false,"observations":[]。"unfinished_new":["a"]}')
    assert obj["unfinished_new"] == ["a"] and fixes == ["字段间中文标点改成逗号"]
    obj, fixes = _parse('{"skip":false,"observations":[{"title":"t"}]</｜｜DSML｜｜ parameter>\n<｜｜DSML｜｜ parameter name="x">[1]')
    assert obj["observations"] == [{"title": "t"}] and "截掉混入的工具调用标记" in fixes


def test_valid_json_reports_no_fixes():
    obj, fixes = _parse('{"skip": true, "observations": []}')
    assert obj["skip"] is True and fixes == []
