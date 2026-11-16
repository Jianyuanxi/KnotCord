import pytest

from knotcord.core.redact import is_fully_private, redact

# 全部是现场拼出来的假密钥，不是真的
A32 = "a1B2c3D4" * 4
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEA1234567890abcdefghij\n"
       "KLMNOPQRSTUVWXYZ0123456789abcdefghij\n"
       "-----END RSA PRIVATE KEY-----")


@pytest.mark.parametrize("secret,kind", [
    ("sk-" + A32, "sk_key"),
    ("sk-proj-" + A32, "sk_key"),
    ("sk-ant-api03-" + A32, "anthropic_key"),
    ("ghp_" + "A1b2C3d4E5" * 4, "github_token"),
    ("github_pat_" + "11ABCDEFG0" + "_" + "x1Y2z3" * 10, "github_token"),
    ("AKIA" + "ABCDEFGH12345678", "aws_key"),
    ("xoxb-1234567890-abcdefghij", "slack_token"),
    ("xoxp-1234567890-abcdefghij", "slack_token"),
    (JWT, "jwt"),
])
def test_token_formats(secret, kind):
    out = redact(f"前面 {secret} 后面")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert out.startswith("前面 ") and out.endswith(" 后面")


def test_pem_multiline():
    out = redact(f"before\n{PEM}\nafter")
    assert "MIIEow" not in out and "KLMNOP" not in out
    assert out == "before\n[REDACTED:pem]\nafter"


def test_pem_truncated_without_end():
    cut = PEM.split("-----END")[0]
    out = redact("x\n" + cut + "\nrest of text")
    assert "MIIEow" not in out and "KLMNOP" not in out
    assert "rest of text" in out


def test_url_password():
    out = redact("clone https://alice:hunter2pw@example.com/repo.git ok")
    assert "hunter2pw" not in out
    assert "alice" in out and "example.com/repo.git" in out
    assert "[REDACTED:url_password]" in out


@pytest.mark.parametrize("line,leaked", [
    ("password=hunter2", "hunter2"),
    ("PASSWORD: hunter2", "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ("passwd = hunter2", "hunter2"),
    ("secret=abcd1234", "abcd1234"),
    ("GITHUB_TOKEN=abcd1234", "abcd1234"),
    ("api_key: abcd1234", "abcd1234"),
    ("apikey=abcd1234", "abcd1234"),
    ("API-KEY=abcd1234", "abcd1234"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("mysql --password hunter2 -u root", "hunter2"),
])
def test_key_value_secrets(line, leaked):
    out = redact(line)
    assert leaked not in out
    assert "REDACTED" in out


def test_bearer():
    out = redact("curl -H 'Authorization: Bearer abc123DEF456ghi789' x")
    assert "abc123DEF456ghi789" not in out
    assert "[REDACTED:" in out
    out2 = redact("header bearer abc123DEF456ghi789")
    assert "abc123DEF456ghi789" not in out2


def test_no_double_redaction_and_no_false_positive():
    out = redact("token=sk-" + A32)
    assert out == "token=[REDACTED:sk_key]"
    assert redact("max_tokens=4096 and tokens: many") == "max_tokens=4096 and tokens: many"
    assert redact("普通文本，没有密钥") == "普通文本，没有密钥"
    assert redact("") == ""


def test_long_alnum_run_is_fast():
    import time
    t0 = time.time()
    # 超长的字母数字串（base64 之类）上不能出现平方级回溯
    for blob in ("a" * 1000000, "ab-" * 300000, "a1." * 300000, "-" * 500000):
        redact(blob + " password=zzzz")
    assert time.time() - t0 < 5


# ---------------------------------------------------------------- 标签

def test_strip_simple_blocks():
    for tag in ("private", "system-reminder", "claude-mem-context", "persisted-output",
                "system_instruction", "system-instruction", "knotcord-context"):
        out = redact(f"keep1 <{tag}>drop me</{tag}> keep2")
        assert "drop me" not in out, tag
        assert out == "keep1  keep2", tag


def test_strip_multiline_and_attrs_and_case():
    out = redact("a<PRIVATE>x\ny\nz</Private>b<system-reminder foo='1'>\nlong\n</system-reminder>c")
    assert out == "abc"


def test_nested_blocks():
    out = redact("a<private>1<private>2</private>3</private>b")
    assert out == "ab"
    out = redact("a<private>1<system-reminder>2</system-reminder>3</private>b")
    assert out == "ab"


def test_unclosed_kept_as_is():
    assert redact("a<private>never closed") == "a<private>never closed"
    assert redact("a</private>b") == "a</private>b"
    # 一个没闭合、一个闭合：闭合的被剥，没闭合的保留
    out = redact("a<private>b<private>c</private>d")
    assert "c" not in out
    assert out == "a<private>bd"


def test_secret_inside_tag_is_gone_and_other_tags_untouched():
    out = redact("<private>sk-" + A32 + "</private> and <b>bold</b>")
    assert "sk-" not in out and "<b>bold</b>" in out


def test_is_fully_private():
    assert is_fully_private("<private>全是秘密</private>")
    assert is_fully_private("  <private>a</private>\n<private>b</private> ")
    assert not is_fully_private("<private>a</private> 还有别的")
    assert not is_fully_private("没有标签")
    assert not is_fully_private("")
    assert not is_fully_private("<private>没闭合")
