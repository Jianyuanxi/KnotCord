"""脱敏：全程唯一的出口函数 redact()（方案书第 6 节）。

先剥离标签块，再把密钥换成 [REDACTED:类型]。写库前、发给模型前各调一次。
"""

from __future__ import annotations

import re

# 剥离的标签：private、system-reminder、claude-mem-context、persisted-output、system-instruction、knotcord-context；
# system_instruction 和 system-instruction 都认
_TAG_RE = re.compile(
    r"<(/?)(private|system-reminder|claude-mem-context|persisted-output|system[_-]instruction|knotcord-context)"
    r"(?=[\s>/])[^>]*?(/?)>",
    re.IGNORECASE,
)


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _strip_blocks(text: str, only: str | None = None) -> str:
    """剥离标签块。嵌套按同名计数配对；没闭合的开标签、多余的闭标签保留原样。"""
    toks = []
    for m in _TAG_RE.finditer(text):
        name = _norm(m.group(2))
        if only and name != only:
            continue
        if m.group(3):  # <private/> 自闭合：不构成块
            continue
        toks.append((m.start(), m.end(), name, bool(m.group(1))))
    if not toks:
        return text
    out: list[str] = []
    pos = 0
    i = 0
    n = len(toks)
    while i < n:
        s, e, name, is_close = toks[i]
        if is_close or s < pos:
            i += 1
            continue
        depth = 1
        j = i + 1
        match = -1
        while j < n:
            _, _, nj, cj = toks[j]
            if nj == name:
                depth += -1 if cj else 1
                if depth == 0:
                    match = j
                    break
            j += 1
        if match < 0:
            i += 1  # 没闭合：保留原样，继续看后面的标签
            continue
        out.append(text[pos:s])
        pos = toks[match][1]
        i = match + 1
    out.append(text[pos:])
    return "".join(out)


def is_fully_private(text: str) -> bool:
    """整条都是 <private> 块（块之间只有空白）。"""
    if not text or not text.strip():
        return False
    stripped = _strip_blocks(text, only="private")
    return stripped != text and not stripped.strip()


# ---------------------------------------------------------------- 密钥

def _r(kind: str) -> str:
    return f"[REDACTED:{kind}]"


_SIMPLE = [
    # PEM 块（跨行）；其后是没写到 END 的残块，只吃 base64 行
    (re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----[\s\S]*?-----END \1-----"), _r("pem")),
    (re.compile(r"-----BEGIN [A-Z0-9 ]+-----(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]{16,})+"), _r("pem")),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), _r("anthropic_key")),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), _r("sk_key")),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), _r("github_token")),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), _r("github_token")),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _r("aws_key")),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), _r("slack_token")),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), _r("jwt")),
]

# URL 里的 用户:密码@ ：保留 scheme 和用户名，只去掉密码
_URL_CRED = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]{0,30}://)([^\s/:@]+):([^\s/@]+)@")
_BEARER = re.compile(r"\b(Bearer)(\s+)[A-Za-z0-9._~+/\-]{8,}=*", re.IGNORECASE)

_KV_HINT = re.compile(r"password|passwd|secret|token|api[_-]?key|apikey|authorization", re.IGNORECASE)
# key=value / key: value / "key": "value"。关键词前面的 GITHUB_ 之类前缀不用匹配，原样留在文本里
_KV = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|authorization)"
    r"(\"?'?\s*[:=]\s*\"?'?)"
    r"(?!(?:(?:Bearer|Basic|Token)\s+)?\[REDACTED:)"       # 已被前面的规则处理过的不再处理
    r"((?:(?:Bearer|Basic|Token)\s+)?[^\s\"'`,;&)}\]]{3,})",
    re.IGNORECASE,
)
# 命令行参数：--password xxx / --token xxx
_FLAG = re.compile(
    r"(--?(?:password|passwd|token|secret|api-?key)\s+)(?!-|\[REDACTED:)(\S{3,})", re.IGNORECASE
)


def redact(text: str) -> str:
    if not text:
        return ""
    text = _strip_blocks(text)
    for pat, repl in _SIMPLE:
        text = pat.sub(repl, text)
    if "://" in text:
        text = _URL_CRED.sub(lambda m: f"{m.group(1)}{m.group(2)}:{_r('url_password')}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{_r('bearer')}", text)
    if _KV_HINT.search(text):
        text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{_r('secret')}", text)
        text = _FLAG.sub(lambda m: f"{m.group(1)}{_r('secret')}", text)
    return text
