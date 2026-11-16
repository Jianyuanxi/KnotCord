"""工具记录的精简规则（方案书第 2 节）。顺序：先脱敏、再精简（细则 2.3）。只用标准库和 core。

body 的结构：`<输入部分>` 或 `<输入部分>\\n=> <结果部分>`。输入部分里的 "\\n=> " 会被改写，
所以按第一个 "\\n=> " 切开永远是对的（split_body）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from knotcord.core.redact import redact

MAX_RAW = 1_000_000   # 超过 1 MB 的工具输出，先各取头尾 2 万字再脱敏
RAW_KEEP = 20_000
RESULT_SEP = "\n=> "
_SEP_SAFE = "\n=> "  # 输入部分里出现分隔符时的替身

_SHELL = {"bash", "powershell"}
_AGENT = {"agent", "task"}

# 图片和 base64 整块删
_B64_URI = re.compile(r"data:[\w/+.\-]{1,60};base64,[A-Za-z0-9+/=_\-]{16,}")
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{300,}={0,2}")
_EXIT_RE = re.compile(r"\AExit code (-?\d+)[ \t]*\r?\n?")


def marker(n: int) -> str:
    return f"…[省略{n}字]…"


def precut(text: str) -> str:
    if len(text) <= MAX_RAW:
        return text
    return text[:RAW_KEEP] + "\n…\n" + text[-RAW_KEEP:]


def _blob_or_keep(m: re.Match) -> str:
    s = m.group(0)
    # 真 base64 一定大小写数字混着；"aaaa…"、"====" 这类重复串不算
    if any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() or c in "+/" for c in s):
        return "[base64已删]"
    return s


def strip_blobs(text: str) -> str:
    text = _B64_URI.sub("[base64已删]", text)
    return _B64_RUN.sub(_blob_or_keep, text)


def prepare_text(text: str) -> str:
    """超长先取头尾 → 脱敏 → 删图片/base64。精简（截头尾）放在这之后。"""
    if not text:
        return ""
    return strip_blobs(redact(precut(text)))


def clip(text: str, limit: int) -> str:
    """总长不超过 limit 的头尾截断（头约 2/3）。"""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + marker(len(text) - limit) + (text[-tail:] if tail else "")


def head_tail(text: str, n: int) -> str:
    if len(text) <= 2 * n:
        return text
    return text[:n] + marker(len(text) - 2 * n) + text[-n:]


def oneline(text: str, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 结果的头尾（解析阶段先压成小块）

@dataclass(frozen=True)
class Clipped:
    head: str
    tail: str      # 空串表示 head 就是全文
    total: int     # 脱敏、删图后的全文长度（不含开头的 "Exit code N" 行）
    exit_code: int | None = None


_KEEP = 600  # 各工具规则里最大的头尾长度


def make_clipped(text: str) -> Clipped:
    code = None
    m = _EXIT_RE.match(text)
    if m:  # 先摘掉退出码行，免得它占掉头部的 600 字
        code = int(m.group(1))
        text = text[m.end():]
    total = len(text)
    if total <= 2 * _KEEP:
        return Clipped(text, "", total, code)
    return Clipped(text[:_KEEP], text[-_KEEP:], total, code)


def _render_clipped(c: Clipped, n: int) -> str:
    if c.tail == "":
        return head_tail(c.head, n)
    return c.head[:n] + marker(c.total - 2 * n) + c.tail[-n:]


# ---------------------------------------------------------------- 输入

def _short_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if "__" in name else name


def _path_brief(path: str) -> str:
    path = " ".join(path.split())
    return path if len(path) <= 110 else "…" + path[-109:]


def _safe_input(part: str) -> str:
    return part.replace(RESULT_SEP, _SEP_SAFE)


def input_part(name: str, inp) -> tuple[str, str]:
    """返回 (brief, 输入部分)。brief 一行，≤ 120 字。"""
    name = name or ""
    low = name.lower()
    if not isinstance(inp, dict):
        inp = {}

    def field(key: str) -> str:
        v = inp.get(key)
        return prepare_text(v) if isinstance(v, str) else ""

    if low in _SHELL:
        cmd = field("command")
        desc = field("description")
        brief = oneline(desc or cmd.split("\n", 1)[0] or name, 120)
        return brief, _safe_input("$ " + clip(cmd, 1000))

    if low in ("edit", "write"):
        path = inp.get("file_path") if isinstance(inp.get("file_path"), str) else ""
        brief = _path_brief(path) or name
        if low == "edit":
            old, new = clip(field("old_string"), 300), clip(field("new_string"), 300)
            part = f"{path}\n旧：\n{old}\n新：\n{new}"
        else:
            part = f"{path}\n内容：\n{clip(field('content'), 300)}"
        return brief, _safe_input(part)

    if low in _AGENT:
        prompt = field("prompt")
        desc = field("description")
        sub = inp.get("subagent_type") if isinstance(inp.get("subagent_type"), str) else ""
        brief = oneline(desc or prompt or name, 120)
        shown = prompt if len(prompt) <= 300 else prompt[:300] + "…"
        return brief, _safe_input((f"[{sub}] " if sub else "") + shown)

    # 其他工具：输入的紧凑 JSON 截 400 字
    try:
        dumped = json.dumps(inp, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        dumped = str(inp)
    dumped = prepare_text(dumped)
    shown = dumped if len(dumped) <= 400 else dumped[:400] + "…"
    brief = oneline(f"{_short_name(name)} {dumped}", 120)
    return brief, _safe_input(shown)


# ---------------------------------------------------------------- 结果

def result_part(name: str, c: Clipped, is_error: bool) -> str:
    low = (name or "").lower()
    if low in ("edit", "write"):
        if not is_error:
            return "ok"
        return "[错误] " + clip(_render_clipped(c, 200), 300)
    if low in _SHELL:
        text = _render_clipped(c, _KEEP)
        code = c.exit_code if c.exit_code is not None else (1 if is_error else 0)
        return f"[exit {code}]" + ("\n" + text if text.strip() else "")
    n = 600 if low in _AGENT else 400
    text = _render_clipped(c, n)
    if c.exit_code is not None:
        text = f"Exit code {c.exit_code}\n" + text
    return ("[错误] " if is_error else "") + text


def join_body(part: str, result: str | None) -> str:
    return part if result is None else part + RESULT_SEP + result


def split_body(body: str | None) -> tuple[str, str | None]:
    body = body or ""
    i = body.find(RESULT_SEP)
    if i < 0:
        return body, None
    return body[:i], body[i + len(RESULT_SEP):]
