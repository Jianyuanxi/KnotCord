"""DeepSeek 通道：标准库 urllib 调 OpenAI 兼容接口（方案书第 3 节）。

- 请求体必须带 {"thinking":{"type":"disabled"}}，否则默认开思考，又慢又贵。
- 优先 response_format=json_object；服务端因此报 400 就去掉重发一次；返回文本里的 JSON 由 validate 抠。
- 密钥来源：环境变量 DEEPSEEK_API_KEY，其次 config.toml 的 extract.deepseek_api_key。
  密钥只放在 Authorization 头里，不写日志、不写临时文件、不进异常消息。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from knotcord.core import proc

from .errors import CallAborted, ChannelUnavailable, ModelError, ModelResult

DEFAULT_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-flash"
MAX_OUTPUT_TOKENS = 8000

# 价目（美元 / 百万 token，非高峰；方案书第 3 节）。北京工作日 9-12、14-18 点高峰翻倍
PRICE_IN_MISS = 0.15
PRICE_IN_HIT = 0.003
PRICE_OUT = 0.60
# 汇率常量：只用于估算花费和每日上限，不要求精确；换算偏差由 ¥3 的上限留出余量
USD_TO_CNY = 7.2

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_BEIJING = timezone(timedelta(hours=8))


def _beijing_now() -> datetime:  # 测试里可替换
    return datetime.now(_BEIJING)


def _is_peak(dt: datetime) -> bool:
    return dt.weekday() < 5 and (9 <= dt.hour < 12 or 14 <= dt.hour < 18)


def cost_cny(prompt_tokens: int, hit_tokens: int, out_tokens: int, when: datetime | None = None) -> float:
    when = when or _beijing_now()
    miss = max(0, prompt_tokens - hit_tokens)
    usd = (miss * PRICE_IN_MISS + hit_tokens * PRICE_IN_HIT + out_tokens * PRICE_OUT) / 1_000_000
    if _is_peak(when):
        usd *= 2
    return round(usd * USD_TO_CNY, 6)


def _api_key(cfg) -> str:
    return (os.environ.get("DEEPSEEK_API_KEY") or cfg.get("extract.deepseek_api_key") or "").strip()


def _system_text() -> str:
    """指令 + schema：DeepSeek 的 json_object 模式不强制 schema，要把结构写进提示里。"""
    with open(os.path.join(_PKG_DIR, "instr.md"), "r", encoding="utf-8") as f:
        instr = f.read()
    with open(os.path.join(_PKG_DIR, "schema.json"), "r", encoding="utf-8") as f:
        schema = f.read()
    return instr + "\n\n输出必须是符合下面 JSON Schema 的 JSON 对象：\n" + schema


class _RetryNoFormat(Exception):
    pass


def _opener(url: str) -> urllib.request.OpenerDirector:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本地不走系统代理（测试用）
    return urllib.request.build_opener()


def _retry_after(headers) -> int | None:
    try:
        v = headers.get("Retry-After") if headers else None
        return int(time.time() + int(v)) if v and str(v).strip().isdigit() else None
    except Exception:
        return None


def _classify_http(status: int, text: str, headers, with_format: bool) -> Exception:
    snip = text.strip().replace("\n", " ")[:200]
    low = text.lower()
    if status in (401, 403):
        return ChannelUnavailable(None, f"DeepSeek 鉴权失败 (HTTP {status})")
    if status == 402 or "insufficient" in low or "balance" in low:
        return ChannelUnavailable(None, "DeepSeek 余额不足")
    if status == 429 or "rate limit" in low or "quota" in low:
        return ChannelUnavailable(_retry_after(headers), "DeepSeek 限流")
    if status == 400 and with_format and ("response_format" in low or "json" in low):
        return _RetryNoFormat()
    return ModelError(f"DeepSeek HTTP {status}: {snip}")


def _post(url: str, key: str, body: dict, timeout_s: float, with_format: bool) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with _opener(url).open(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            text = e.read(4096).decode("utf-8", "replace")
        except Exception:
            text = ""
        raise _classify_http(e.code, text, e.headers, with_format)
    except urllib.error.URLError as e:
        raise ModelError(f"DeepSeek 网络错误: {type(e.reason).__name__}")
    except (TimeoutError, OSError) as e:
        raise ModelError(f"DeepSeek 网络错误: {type(e).__name__}")
    except ValueError:
        raise ModelError("DeepSeek 返回的不是 JSON")


def _request(url: str, key: str, body: dict, timeout_s: float) -> dict:
    try:
        return _post(url, key, body, timeout_s, True)
    except _RetryNoFormat:
        body = {k: v for k, v in body.items() if k != "response_format"}
        try:
            return _post(url, key, body, timeout_s, False)
        except _RetryNoFormat:  # 不会发生（with_format=False 不抛），保险
            raise ModelError("DeepSeek 请求被拒绝 (HTTP 400)")


def call(prompt: str, cfg, *, tmp_dir: str, on_tick=None, stop_event=None) -> ModelResult:
    key = _api_key(cfg)
    if not key:
        raise ChannelUnavailable(None, "没有配置 DeepSeek 密钥")
    base = (cfg.get("extract.deepseek_base_url") or DEFAULT_BASE).rstrip("/")
    model = cfg.get("extract.deepseek_model") or MODEL
    timeout_s = float(cfg.get("extract.timeout_s"))
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _system_text()}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    holder: dict = {}

    def _work() -> None:
        try:
            holder["resp"] = _request(base + "/chat/completions", key, body, timeout_s)
        except BaseException as e:  # 线程里的异常带回主线程
            holder["err"] = e

    t = threading.Thread(target=_work, daemon=True, name="trace-deepseek")
    t.start()
    deadline = time.monotonic() + timeout_s
    last_tick = time.monotonic()
    while t.is_alive():
        t.join(0.2)
        now = time.monotonic()
        if stop_event is not None and stop_event.is_set():
            raise CallAborted()  # 线程是守护的，请求自己会在 socket 超时时结束
        if now >= deadline:
            raise ModelError(f"DeepSeek 超过 {timeout_s:g} 秒未返回")
        if on_tick is not None and now - last_tick >= proc.TICK_S:
            last_tick = now
            on_tick()
    if "err" in holder:
        raise holder["err"]
    resp = holder["resp"]

    try:
        choice = resp["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ModelError("DeepSeek 返回缺少 choices")
    usage = resp.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    cny = cost_cny(tin, hit, tout)
    if choice.get("finish_reason") == "length":
        raise ModelError("DeepSeek 输出被截断", tokens_in=tin, tokens_out=tout, cost_cny=cny)
    if not text.strip():
        raise ModelError("DeepSeek 返回空文本", tokens_in=tin, tokens_out=tout, cost_cny=cny)
    return ModelResult(text=text, provider="deepseek", tokens_in=tin, tokens_out=tout, cost_cny=cny, model=model)
