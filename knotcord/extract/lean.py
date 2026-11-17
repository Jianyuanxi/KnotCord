"""提炼前的本地去噪：只改喂给模型的文本，库里的原文不动（搜索仍能查到完整内容）。纯规则、不调模型。

做法参照：
- rtk 的压缩方法：ansi（去控制码）、progress（去进度条）、dedup/rle（连续重复行折叠）、cap（头尾截断）。
- Headroom 的思路：截掉的中间部分里，报错、警告、测试结论这类"异常行"单独捞回来。
- 提炼要的是"发生过什么"，不是让模型接着干活，所以比上面两者删得更狠：
  成功命令的输出只留头尾几行；往 scratchpad 写的临时脚本只留文件名；浏览器的开标签、跳转、截图等只留一行。
用户提问和助手回复原样保留（只缩短路径）。

11-17 用 11-16 以来的真实记录试算：4135 条事件 309 万字 → 142 万字（46%）。
"""

from __future__ import annotations

import re

from knotcord.ingest.trim import split_body

# ---------------------------------------------------------------- 路径缩短

_SCRATCH = re.compile(
    r"(?:[A-Za-z]:|/[a-z])[\\/]Users[\\/][^\\/\s\"']+[\\/]AppData[\\/]Local[\\/]Temp[\\/]claude[\\/][0-9a-f]+[\\/]"
    r"[0-9a-f\-]{36}[\\/]scratchpad(?:[\\/]|\b)", re.I)
_HOME = re.compile(r"(?:[A-Za-z]:|/[a-z])[\\/]Users[\\/][^\\/\s\"']+(?=[\\/\s\"']|$)", re.I)


def short_paths(s: str) -> str:
    return _HOME.sub("~", _SCRATCH.sub("<scratch>/", s))


# ---------------------------------------------------------------- 文本压缩（rtk 风格）

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PROGRESS = re.compile(r"^\s*(?:\d{1,3}(?:\.\d+)?%|[#=>\-█░▒▓ ]{10,}\|?)\s*\S{0,40}$")
_NOISE = re.compile(r"^(?:Shell cwd was reset to .*|\(no output\))$")
_IMPORTANT = re.compile(r"error|错误|失败|fail|warn|警告|traceback|exception|denied|refused|not found|"
                        r"\bpassed\b|\bok\b\s*\d|✗|✘", re.I)
_RESCUE = 3  # 中间被截掉的部分最多捞回几条异常行


def squeeze(text: str, keep_lines: int, keep_chars: int) -> str:
    """去控制码、进度条、空行和噪声行，连续重复行折叠成一行，再按行数和字数留头尾；
    截掉的中间部分里的报错/测试结论最多捞回 _RESCUE 条。"""
    if not text:
        return ""
    lines: list[str] = []
    prev, rep = None, 0
    for ln in _ANSI.sub("", text).replace("\r\n", "\n").split("\n"):
        ln = ln.rsplit("\r", 1)[-1].rstrip()  # \r 覆盖写的进度只留最后一帧
        if not ln.strip() or _NOISE.match(ln.strip()) or _PROGRESS.match(ln):
            continue
        if ln == prev:
            rep += 1
            continue
        if rep:
            lines.append(f"(上行重复 {rep} 次)")
            rep = 0
        lines.append(ln)
        prev = ln
    if rep:
        lines.append(f"(上行重复 {rep} 次)")
    if len(lines) > keep_lines:
        h = max(1, keep_lines // 2)
        t = max(1, keep_lines - h)
        middle = lines[h:-t]
        rescued = [x for x in middle if _IMPORTANT.search(x)][:_RESCUE]
        lines = lines[:h] + [f"…[省略 {len(middle) - len(rescued)} 行]…"] + rescued + lines[-t:]
    out = "\n".join(lines)
    if len(out) > keep_chars:
        h = keep_chars * 2 // 3
        out = out[:h] + f"…[省略 {len(out) - keep_chars} 字]…" + out[-(keep_chars - h):]
    return out


def _indent(text: str) -> str:
    return "\n".join("    " + ln for ln in text.split("\n")) if text else ""


# ---------------------------------------------------------------- 按工具

# 成功时只留一行的工具（名字按 "__" 之后的短名比，忽略大小写，包含即算）
_QUIET = ("tabs_context", "tabs_create", "tabs_close", "tabs_select", "resize_window", "navigate", "screenshot",
          "update_plan", "wrap_up", "toolsearch", "mark_chapter", "read_console", "taskstop", "preview_start",
          "preview_list", "zoom", "hover", "scroll", "wait")
# 读类工具：成功时只留查了什么
_READ = ("read", "grep", "glob", "get_page_text", "read_page", "find", "smart_outline", "smart_unfold",
         "list_directory", "read_file", "get", "search", "timeline", "snapshot", "markdown", "extract")
_AGENT = ("agent", "task", "sendmessage")


def _short(name: str) -> str:
    return (name.rsplit("__", 1)[-1] if "__" in name else name).lower()


def _is_error(res: str) -> bool:
    return res.startswith("[错误]") or (res.startswith("[exit ") and not res.startswith("[exit 0]"))


def _tool(ev) -> str:
    name = ev["tool"] or ""
    st = _short(name)
    disp = name.rsplit("__", 1)[-1] if "__" in name else name
    brief = short_paths(ev["brief"] or "")
    inp, res = split_body(ev["body"])
    inp, res = short_paths(inp), short_paths(res or "")
    err = _is_error(res)

    if st in ("bash", "powershell"):
        cmd = inp[2:] if inp.startswith("$ ") else inp
        head = f"工具 {disp}: {brief}" if brief and not cmd.startswith(brief.rstrip("…")[:40]) else f"工具 {disp}:"
        code, _, out = res.partition("\n")
        out = squeeze(out, 12, 700) if err else squeeze(out, 4, 240)
        return head + "\n" + _indent("$ " + squeeze(cmd, 4, 300) + "\n=> " + (code or "?") + ("\n" + out if out else ""))

    if st == "write":
        path, _, rest = inp.partition("\n")
        if path.startswith("<scratch>"):
            return f"工具 Write: {path}（临时文件）" + ("" if not err else "\n" + _indent(res[:300]))
        body = rest.split("内容：\n", 1)[-1]
        return f"工具 Write: {path}\n" + _indent(squeeze(body, 4, 200)) + ("" if not err else "\n" + _indent(res[:300]))

    if st == "edit":
        path, _, rest = inp.partition("\n")
        old, _, new = rest.partition("\n新：\n")
        old = old.replace("旧：\n", "", 1)
        text = f"工具 Edit: {path}\n" + _indent("旧：" + squeeze(old, 3, 150) + "\n新：" + squeeze(new, 4, 220))
        return text + ("" if not err else "\n" + _indent(res[:300]))

    if not err and any(q in st for q in _QUIET):
        return f"工具 {disp}: {brief[:100]}"
    if not err and st in _READ:
        return f"工具 {disp}: {brief[:140]}"

    big = st in _AGENT
    lim_in = 400 if big else 160
    lim_out = (800 if big else 240) * (2 if err else 1)
    parts = [f"工具 {disp}: {brief[:100]}"]
    si = squeeze(inp, 3, lim_in)
    if si and si not in brief:
        parts.append(_indent(si))
    so = squeeze(res, 10 if err else (6 if big else 3), lim_out)
    if so:
        parts.append(_indent("=> " + so))
    return "\n".join(parts)


def render(ev) -> str:
    """一条事件喂给模型的正文（不含序号和时间）。"""
    kind = ev["kind"]
    if kind == "tool":
        return _tool(ev)
    who = "用户" if kind == "prompt" else "助手"
    return f"{who}: {short_paths(ev['body'] or ev['brief'] or '')}"
