"""trim.py：各类工具的精简规则、分隔符、脱敏先于截断。"""

from knotcord.ingest import trim


def _res(name, text, err=False):
    prepared = trim.prepare_text(text)
    return trim.result_part(name, trim.make_clipped(prepared), err)


def test_shell_keeps_command_exit_and_head_tail():
    cmd = "echo " + "c" * 2000
    brief, part = trim.input_part("Bash", {"command": cmd, "description": "跑个命令"})
    assert brief == "跑个命令"
    assert part.startswith("$ echo ") and len(part) < 1100
    out = "H" * 700 + "M" * 3000 + "T" * 700
    r = _res("Bash", "Exit code 2\n" + out, err=True)
    assert r.startswith("[exit 2]\n")
    body = r.split("\n", 1)[1]
    assert body.startswith("H" * 600) and body.endswith("T" * 600)
    assert "M" not in body and "省略" in body
    assert _res("PowerShell", "hello") == "[exit 0]\nhello"


def test_powershell_same_as_bash():
    _, part = trim.input_part("PowerShell", {"command": "Get-Date"})
    assert part == "$ Get-Date"
    assert _res("PowerShell", "x" * 5000, err=True).startswith("[exit 1]")


def test_edit_and_write_keep_path_and_300_chars():
    old, new = "O" * 1000, "N" * 1000
    brief, part = trim.input_part("Edit", {"file_path": "C:\\a\\b.py", "old_string": old, "new_string": new})
    assert brief == "C:\\a\\b.py"
    assert part.count("O") <= 300 and part.count("N") <= 300 and part.count("O") >= 200
    assert _res("Edit", "The file has been updated successfully.") == "ok"
    _, wpart = trim.input_part("Write", {"file_path": "x.txt", "content": "W" * 1000})
    assert 200 <= wpart.count("W") <= 300
    assert _res("Write", "<tool_use_error>boom</tool_use_error>", err=True).startswith("[错误]")


def test_agent_prompt_head_300_report_600_each():
    brief, part = trim.input_part("Agent", {"prompt": "P" * 1000, "description": "查东西", "subagent_type": "Explore"})
    assert brief == "查东西"
    assert part.count("P") == 300 and part.startswith("[Explore]")
    r = _res("Agent", "A" * 700 + "z" * 3000 + "B" * 700)
    assert r.startswith("A" * 600) and r.endswith("B" * 600) and "z" not in r


def test_other_tool_compact_json_400_and_400_each():
    brief, part = trim.input_part("mcp__x__navigate", {"url": "u" * 1000})
    assert part.startswith('{"url":"u') and len(part) <= 401
    assert brief.startswith("navigate ")
    r = _res("mcp__x__navigate", "A" * 500 + "m" * 2000 + "B" * 500)
    assert r.startswith("A" * 400) and r.endswith("B" * 400) and "m" not in r


def test_base64_and_data_uri_removed():
    img = "data:image/png;base64," + "QUJD" * 400
    run = "aB3+" * 300
    out = _res("Bash", f"before {img} mid {run} after")
    assert "QUJD" not in out and "aB3+aB3+" not in out
    assert "before" in out and "after" in out


def test_redact_runs_before_clip_secret_across_cut_point():
    secret = "sk-ant-api03-" + "K" * 40
    text = "x" * 590 + secret + "y" * 3000
    out = _res("Bash", text)
    assert "sk-ant" not in out and "KKKKKKKK" not in out


def test_huge_output_is_precut_then_redacted():
    text = "a" * 2_000_000
    prepared = trim.prepare_text(text)
    assert len(prepared) < 50_000


def test_body_split_roundtrip_even_if_input_contains_separator():
    _, part = trim.input_part("Write", {"file_path": "p", "content": "x\n=> y"})
    body = trim.join_body(part, "ok")
    p2, r2 = trim.split_body(body)
    assert p2 == part and r2 == "ok"
    assert trim.split_body("no result") == ("no result", None)
