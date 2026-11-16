"""通道、记账、冷却、熔断：codex 报错解析、budget、deepseek（本地假服务器，不联网）。"""

import json
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from knotcord.core import config, db
from knotcord.extract import budget, codex, deepseek
from knotcord.extract.errors import ChannelUnavailable, ModelError

REAL_MSG = ("You’ve hit your usage limit. Upgrade to Plus to continue using Codex "
            "(https://chatgpt.com/explore/plus), or try again at Dec 12th, 2026 5:25 AM.")
FAKE_KEY = "sk-TEST-not-a-real-key-123456"
NOW = 1_795_000_000


def mkcfg(**over):
    d = config.load().as_dict()
    d.update({k.replace("__", "."): v for k, v in over.items()})
    return config.Config(d)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    budget.reset_breaker()
    yield
    budget.reset_breaker()


# ------------------------------------------------------------------ codex 解析

def test_parse_retry_at_real_text_both_apostrophes():
    want = int(datetime(2026, 12, 12, 5, 25).timestamp())
    assert codex.parse_retry_at(REAL_MSG) == want
    assert codex.parse_retry_at(REAL_MSG.replace("’", "'")) == want  # 任务书里写的是 ASCII 撇号


def test_parse_retry_at_variants():
    assert codex.parse_retry_at("try again at Jan 3rd, 2027 11:05 PM") == int(datetime(2027, 1, 3, 23, 5).timestamp())
    assert codex.parse_retry_at("try again at December 1st, 2026 12:00 AM") == int(datetime(2026, 12, 1, 0, 0).timestamp())
    assert codex.parse_retry_at("try again at Nov 22nd, 2026 12:30 PM") == int(datetime(2026, 11, 22, 12, 30).timestamp())
    assert codex.parse_retry_at("try again in 2 hours 30 minutes", now=1000) == 1000 + 9000
    assert codex.parse_retry_at("Please try again in 3 days.", now=0) == 3 * 86400
    assert codex.parse_retry_at("try again in 45 seconds", now=10) == 55


@pytest.mark.parametrize("text", ["", "没有时间", "try again later", "try again at Foo 3rd, 2026 1:00 AM",
                                   "try again at Feb 30th, 2026 1:00 AM"])
def test_parse_retry_at_unparseable_returns_none(text):
    assert codex.parse_retry_at(text) is None


def test_looks_like_quota():
    assert codex.looks_like_quota(REAL_MSG)
    assert codex.looks_like_quota("429 Too Many Requests")
    assert codex.looks_like_quota("Not logged in")
    assert not codex.looks_like_quota("")
    assert not codex.looks_like_quota("segmentation fault")


# ------------------------------------------------------------------ codex.call（假进程）

def fake_codex(tmp_path, monkeypatch, body: str):
    script = tmp_path / "fake_codex.py"
    script.write_text(body, encoding="utf-8")
    monkeypatch.setattr(codex, "_command", lambda: [sys.executable, str(script)])


def test_codex_call_success_reads_out_json_and_tokens(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch,
               "import json,sys\n"
               "a = sys.argv[1:]\n"
               "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
               "out = a[a.index('-o') + 1]\n"
               "open(out, 'w', encoding='utf-8').write(json.dumps({'skip': True, 'echo': prompt, 'args': a}))\n"
               "print(json.dumps({'type': 'thread.started'}))\n"
               "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1234, 'output_tokens': 56}}))\n")
    res = codex.call("你好 prompt", mkcfg(), tmp_dir=str(tmp_path / "w"))
    d = json.loads(res.text)
    assert d["echo"] == "你好 prompt"  # 提示走标准输入
    a = d["args"]
    for flag in ("exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json", "--output-schema"):
        assert flag in a, flag
    assert a[a.index("-s") + 1] == "read-only" and a[a.index("-m") + 1] == codex.MODEL
    assert a[-1] == "-"
    assert (res.provider, res.tokens_in, res.tokens_out, res.cost_cny) == ("codex", 1234, 56, 0.0)


def test_codex_call_quota_exit_raises_channel_unavailable_with_time(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch,
               "import json,sys\n"
               "sys.stdin.read()\n"
               f"m = {REAL_MSG!r}\n"
               "print(json.dumps({'type':'thread.started'}))\n"
               "print(json.dumps({'type':'error','message':m}))\n"
               "print(json.dumps({'type':'turn.failed','error':{'message':m}}))\n"
               "sys.exit(1)\n")
    with pytest.raises(ChannelUnavailable) as ei:
        codex.call("x", mkcfg(), tmp_dir=str(tmp_path / "w"))
    assert ei.value.retry_at == int(datetime(2026, 12, 12, 5, 25).timestamp())
    assert "usage limit" in ei.value.reason and "退出码 1" in ei.value.reason


def test_codex_call_exit_zero_without_output_is_model_error_unless_quota(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, "import sys\nsys.stdin.read()\n")
    with pytest.raises(ModelError):
        codex.call("x", mkcfg(), tmp_dir=str(tmp_path / "w1"))
    fake_codex(tmp_path, monkeypatch,
               "import json,sys\nsys.stdin.read()\n"
               "print(json.dumps({'type':'error','message':'Rate limit reached, try again in 2 hours'}))\n")
    with pytest.raises(ChannelUnavailable) as ei:
        codex.call("x", mkcfg(), tmp_dir=str(tmp_path / "w2"))
    assert ei.value.retry_at and ei.value.retry_at > time.time() + 3600


def test_codex_call_timeout_is_model_error(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, "import time,sys\nsys.stdin.read()\ntime.sleep(30)\n")
    t0 = time.time()
    with pytest.raises(ModelError, match="超时"):
        codex.call("x", mkcfg(extract__timeout_s=1), tmp_dir=str(tmp_path / "w"))
    assert time.time() - t0 < 15


def test_codex_missing_command_is_channel_unavailable(monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    with pytest.raises(ChannelUnavailable):
        codex.call("x", mkcfg(), tmp_dir="unused")


# ------------------------------------------------------------------ budget

def test_usage_record_line_and_limits(conn):
    cfg = mkcfg()
    assert budget.can_call(conn, cfg, NOW)
    budget.record(conn, "codex", tokens_in=100, tokens_out=10, now=NOW)
    budget.record(conn, "deepseek", tokens_in=200, tokens_out=20, cost_cny=0.05, now=NOW)
    u = budget.usage_get(conn, NOW)
    assert (u["calls"], u["codex"], u["deepseek"], u["tokens_in"], u["tokens_out"]) == (2, 1, 1, 300, 30)
    assert u["cny"] == pytest.approx(0.05)
    line = budget.usage_line(conn, cfg, NOW)
    md = time.strftime("%m-%d", time.localtime(NOW))
    assert line.startswith(f"{md} 提炼 2 次（codex 1 / deepseek 1）")
    assert "¥0.05 / 上限 ¥3" in line and "次数上限 150" in line and line.endswith("积压 0 批")
    # 换一天重新计
    assert budget.usage_get(conn, NOW + 86400)["calls"] == 0


def test_can_call_false_when_calls_or_cny_reached(conn):
    budget.record(conn, "codex", now=NOW)
    budget.record(conn, "codex", now=NOW)
    assert not budget.can_call(conn, mkcfg(budget__daily_calls=2), NOW)
    assert budget.can_call(conn, mkcfg(budget__daily_calls=3), NOW)
    budget.record(conn, "deepseek", cost_cny=3.0, now=NOW)
    assert not budget.can_call(conn, mkcfg(budget__daily_calls=100), NOW)


def test_usage_get_tolerates_corrupt_kv(conn):
    db.kv_set(conn, "usage:" + budget.day_key(NOW), "{坏的")
    assert budget.usage_get(conn, NOW)["calls"] == 0


def test_cooldown_ladder_1_6_24_hours_and_reset(conn):
    p = budget.CODEX
    assert budget.channel_available(conn, p, NOW)
    assert budget.mark_unavailable(conn, p, None, NOW) == NOW + 3600
    assert not budget.channel_available(conn, p, NOW + 100)
    assert budget.channel_available(conn, p, NOW + 3600)  # 到期现比，无需"恢复"动作
    assert budget.mark_unavailable(conn, p, None, NOW) == NOW + 6 * 3600
    assert budget.mark_unavailable(conn, p, None, NOW) == NOW + 24 * 3600
    assert budget.mark_unavailable(conn, p, None, NOW) == NOW + 24 * 3600  # 封顶
    budget.mark_ok(conn, p)
    assert budget.mark_unavailable(conn, p, None, NOW) == NOW + 3600  # 成功一次清零


def test_cooldown_uses_parsed_time_and_ignores_past_time(conn):
    until = budget.mark_unavailable(conn, budget.CODEX, NOW + 5000, NOW)
    assert until == NOW + 5000 and db.kv_get(conn, "codex_cooldown_level", "0") == "0"  # 解析出的时间不动阶梯
    assert budget.mark_unavailable(conn, budget.CODEX, NOW - 5, NOW) == NOW + 3600  # 过去的时间当没解析出
    assert budget.mark_unavailable(conn, budget.DEEPSEEK, None, NOW) == NOW + 600


def test_breaker_opens_after_five_consecutive_failures_and_closes_after_six_hours():
    for _ in range(4):
        budget.breaker_note(False, NOW)
    assert not budget.breaker_open(NOW)
    budget.breaker_note(True, NOW)  # 成功一次清零
    for _ in range(4):
        budget.breaker_note(False, NOW)
    assert not budget.breaker_open(NOW)
    budget.breaker_note(False, NOW)
    assert budget.breaker_open(NOW + 3600 * 5)
    assert not budget.breaker_open(NOW + 6 * 3600 + 1)
    budget.breaker_note(False, NOW + 6 * 3600 + 2)  # 重新计数
    assert not budget.breaker_open(NOW + 6 * 3600 + 2)


# ------------------------------------------------------------------ deepseek

def test_cost_peak_vs_offpeak():
    off = datetime(2026, 11, 14, 11, 0, tzinfo=deepseek._BEIJING)   # 周六：不翻倍
    mon_peak = datetime(2026, 11, 16, 10, 0, tzinfo=deepseek._BEIJING)
    mon_night = datetime(2026, 11, 16, 20, 0, tzinfo=deepseek._BEIJING)
    base = (800 * 0.15 + 200 * 0.003 + 100 * 0.60) / 1e6 * 7.2
    assert deepseek.cost_cny(1000, 200, 100, off) == pytest.approx(base, abs=1e-6)  # 结果取 6 位小数
    assert deepseek.cost_cny(1000, 200, 100, mon_night) == pytest.approx(base, abs=1e-6)
    assert deepseek.cost_cny(1000, 200, 100, mon_peak) == pytest.approx(base * 2, abs=1e-6)
    assert deepseek.cost_cny(0, 0, 0, off) == 0.0


def test_deepseek_without_key_is_channel_unavailable():
    with pytest.raises(ChannelUnavailable):
        deepseek.call("x", mkcfg(), tmp_dir="unused")


class _Server:
    """本地假 DeepSeek：按预设依次回应，记录收到的请求。"""

    def __init__(self, *responses):
        self.responses = list(responses)  # (status, body_dict_or_str, headers) 或 callable
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8")
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": json.loads(raw)})
                item = outer.responses.pop(0) if len(outer.responses) > 1 else outer.responses[0]
                if callable(item):
                    item()
                    return
                status, body, headers = item
                data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(*responses):
        s = _Server(*responses)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def ok_body(text='{"skip": true}', finish="stop", usage=None):
    return (200, {"choices": [{"message": {"content": text}, "finish_reason": finish}],
                  "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 100,
                                     "prompt_cache_hit_tokens": 200}}, None)


def dscfg(s, **over):
    return mkcfg(extract__deepseek_base_url=s.url, extract__deepseek_api_key=FAKE_KEY, **over)


def test_deepseek_success_request_shape_and_cost(server):
    s = server(ok_body())
    res = deepseek.call("提示内容", dscfg(s), tmp_dir="unused")
    assert res.text == '{"skip": true}' and res.provider == "deepseek"
    assert (res.tokens_in, res.tokens_out) == (1000, 100) and res.cost_cny > 0
    r = s.requests[0]
    assert r["path"] == "/v1/chat/completions" and r["auth"] == "Bearer " + FAKE_KEY
    b = r["body"]
    assert b["thinking"] == {"type": "disabled"}
    assert b["response_format"] == {"type": "json_object"}
    assert b["model"] == deepseek.MODEL and b["stream"] is False
    assert b["messages"][-1] == {"role": "user", "content": "提示内容"}
    assert "JSON Schema" in b["messages"][0]["content"]


def test_deepseek_env_key_wins_over_config(server, monkeypatch):
    s = server(ok_body())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    deepseek.call("x", dscfg(s), tmp_dir="unused")
    assert s.requests[0]["auth"] == "Bearer sk-from-env"


@pytest.mark.parametrize("status,headers,has_time", [(402, None, False), (401, None, False), (403, None, False),
                                                      (429, {"Retry-After": "120"}, True), (429, None, False)])
def test_deepseek_http_errors_are_channel_unavailable(server, status, headers, has_time):
    s = server((status, {"error": {"message": "nope"}}, headers))
    with pytest.raises(ChannelUnavailable) as ei:
        deepseek.call("x", dscfg(s), tmp_dir="unused")
    assert bool(ei.value.retry_at) == has_time
    if has_time:
        assert ei.value.retry_at > time.time() + 60
    assert FAKE_KEY not in str(ei.value) and FAKE_KEY not in ei.value.reason


def test_deepseek_400_response_format_retries_once_without_it(server):
    s = server((400, {"error": {"message": "response_format is not supported"}}, None), ok_body())
    res = deepseek.call("x", dscfg(s), tmp_dir="unused")
    assert res.text and len(s.requests) == 2
    assert "response_format" in s.requests[0]["body"] and "response_format" not in s.requests[1]["body"]
    assert s.requests[1]["body"]["thinking"] == {"type": "disabled"}


def test_deepseek_500_and_garbage_are_model_errors_without_key(server):
    for resp in ((500, "boom", None), (200, "不是 JSON", None), (200, {"choices": []}, None)):
        s = server(resp)
        with pytest.raises(ModelError) as ei:
            deepseek.call("x", dscfg(s), tmp_dir="unused")
        assert FAKE_KEY not in str(ei.value)


def test_deepseek_truncated_or_empty_output_carries_cost(server):
    for body in (ok_body(finish="length"), ok_body(text="  ")):
        s = server(body)
        with pytest.raises(ModelError) as ei:
            deepseek.call("x", dscfg(s), tmp_dir="unused")
        assert ei.value.tokens_in == 1000 and ei.value.cost_cny > 0  # 花了钱就要记账


def test_deepseek_timeout_is_model_error(server):
    s = server(lambda: time.sleep(3))
    t0 = time.time()
    with pytest.raises(ModelError, match="未返回|网络错误"):
        deepseek.call("x", dscfg(s, extract__timeout_s=1), tmp_dir="unused")
    assert time.time() - t0 < 10


def test_deepseek_stop_event_aborts(server):
    from knotcord.extract.errors import CallAborted
    s = server(lambda: time.sleep(3))
    ev = threading.Event()
    threading.Timer(0.4, ev.set).start()
    with pytest.raises(CallAborted):
        deepseek.call("x", dscfg(s, extract__timeout_s=30), tmp_dir="unused", stop_event=ev)


def test_deepseek_key_added_after_start_is_picked_up(trace_home, monkeypatch):
    """server 启动时读的 cfg 没有密钥，之后才写 config.toml：不用重启就能用上。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    stale = mkcfg()
    assert deepseek._api_key(stale) == ""
    trace_home.mkdir(parents=True, exist_ok=True)
    (trace_home / "config.toml").write_text(f'[extract]\ndeepseek_api_key = "{FAKE_KEY}"\n', encoding="utf-8")
    assert deepseek._api_key(stale) == FAKE_KEY


def test_deepseek_network_error_does_not_leak_key():
    cfg = mkcfg(extract__deepseek_base_url="http://127.0.0.1:1/v1", extract__deepseek_api_key=FAKE_KEY)
    with pytest.raises(ModelError) as ei:
        deepseek.call("x", cfg, tmp_dir="unused")
    assert FAKE_KEY not in str(ei.value)
