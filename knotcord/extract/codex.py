"""Codex 通道：codex exec 一次一批，无状态（方案书第 3 节"调用方式"）。

- 起子进程只走 core.proc.run_child（进 Job Object、不弹窗），环境用 child_env()（白名单，带 KNOTCORD_INTERNAL）。
- codex 在本机是 npm 全局装的 .cmd 包装：直接找到旁边的 codex.js 用 node 起，免得经 cmd 转义引号。
- 退出码非 0，或没有输出且输出里有额度/限流字样 → ChannelUnavailable；恢复时间从报错里解析。
  本机 11-15 真实报错（--json 的 error 事件，退出码 1，stderr 为空；注意撇号是 U+2019）：
  You’ve hit your usage limit. Upgrade to Plus to continue using Codex (...), or try again at Dec 12th, 2026 5:25 AM.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import time
from datetime import datetime

from knotcord.core import proc

from .errors import CallAborted, ChannelUnavailable, ModelError, ModelResult

MODEL = "gpt-6-luna"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(PKG_DIR, "schema.json")
INSTR_PATH = os.path.join(PKG_DIR, "instr.md")

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})

# try again at Dec 12th, 2026 5:25 AM
_AT_RE = re.compile(
    r"try again at\s+([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")
# try again in 2 hours 30 minutes / in 3 days
_IN_RE = re.compile(r"try again in\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,and]*)+)",
                    re.IGNORECASE)
_UNIT_RE = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}

_QUOTA_HINT = re.compile(
    r"usage limit|rate[ _-]?limit|quota|too many requests|\b429\b|insufficient|balance|billing|credits?\b"
    r"|limit reached|hit your|upgrade to|unauthori[sz]ed|\b401\b|\b403\b|not logged in|log ?in|authenticat"
    r"|api key|forbidden|try again (?:at|in)",
    re.IGNORECASE)


def parse_retry_at(text: str, now: float | None = None) -> int | None:
    """从报错文字里解析恢复时间（epoch 秒，按本机本地时间理解）；解析不出返回 None。"""
    now = time.time() if now is None else now
    m = _AT_RE.search(text)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            hour = int(m.group(4))
            ap = (m.group(6) or "").lower()
            if ap == "pm" and hour < 12:
                hour += 12
            elif ap == "am" and hour == 12:
                hour = 0
            try:
                return int(datetime(int(m.group(3)), mon, int(m.group(2)), hour, int(m.group(5))).timestamp())
            except ValueError:
                pass
    m = _IN_RE.search(text)
    if m:
        total = sum(int(n) * _UNIT_S[u[0].lower()] for n, u in _UNIT_RE.findall(m.group(1)))
        if total > 0:
            return int(now + total)
    return None


def looks_like_quota(text: str) -> bool:
    return bool(_QUOTA_HINT.search(text or ""))


def _command() -> list[str]:
    """codex 的启动命令前缀。找不到就是"没装"，按通道不可用处理。"""
    exe = shutil.which("codex")
    if not exe:
        raise ChannelUnavailable(None, "没有找到 codex 命令")
    if exe.lower().endswith((".cmd", ".bat")):
        js = os.path.join(os.path.dirname(exe), "node_modules", "@openai", "codex", "bin", "codex.js")
        node = shutil.which("node")
        if node and os.path.isfile(js):
            return [node, js]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe]  # 退路：经 cmd /c（引号转义由 CreateProcess 处理）
    return [exe]


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _scan_events(stdout: str) -> tuple[list[str], int, int]:
    """从 --json 事件里取出错误文字和 token 用量。事件格式不稳定，取不到就算了。"""
    errors: list[str] = []
    tin = tout = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("type", ""))
        if t in ("error", "turn.failed"):
            msg = ev.get("message")
            if not msg and isinstance(ev.get("error"), dict):
                msg = ev["error"].get("message")
            if msg:
                errors.append(str(msg))
        usage = ev.get("usage")
        if isinstance(usage, dict):
            tin = int(usage.get("input_tokens") or tin)
            tout = int(usage.get("output_tokens") or tout)
    return errors, tin, tout


def call(prompt: str, cfg, *, tmp_dir: str, on_tick=None, stop_event=None) -> ModelResult:
    cmd = _command()
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = os.path.join(tmp_dir, "out.json")
    so_path = os.path.join(tmp_dir, "events.jsonl")
    se_path = os.path.join(tmp_dir, "stderr.txt")
    args = cmd + [
        "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
        "-s", "read-only", "-m", MODEL,
        "-c", 'model_reasoning_effort="low"',
        "-c", f"model_instructions_file='{INSTR_PATH}'",  # TOML 单引号字面量，反斜杠不转义
        "--output-schema", SCHEMA_PATH, "-o", out_path, "--json", "-",
    ]
    try:
        rc = proc.run_child(
            args, stdin_text=prompt, cwd=tmp_dir, env=proc.child_env(),
            timeout_s=float(cfg.get("extract.timeout_s")),
            stdout_path=so_path, stderr_path=se_path, stop_event=stop_event, on_tick=on_tick)
    except proc.ChildTimeout as e:
        raise ModelError(f"codex 超时: {e}")  # 超时被杀的调用照样计入次数，由调用方记账
    if rc == -1 and stop_event is not None and stop_event.is_set():
        raise CallAborted()

    stdout, stderr = _read(so_path), _read(se_path)
    errors, tin, tout = _scan_events(stdout)
    err_text = "\n".join(errors + ([stderr.strip()] if stderr.strip() else []))
    out_text = _read(out_path).strip()

    if rc != 0:
        reason = (errors[0] if errors else (stderr.strip() or f"退出码 {rc}"))[:200]
        raise ChannelUnavailable(parse_retry_at(err_text), f"codex 退出码 {rc}: {reason}")
    if not out_text:
        if errors and looks_like_quota(err_text):
            raise ChannelUnavailable(parse_retry_at(err_text), errors[0][:200])
        raise ModelError("codex 没有输出", tokens_in=tin, tokens_out=tout)
    return ModelResult(text=out_text, provider="codex", tokens_in=tin, tokens_out=tout,
                       cost_cny=0.0, model=MODEL)
