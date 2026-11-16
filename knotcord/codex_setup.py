"""把 KnotCord 接进 Codex（CLI 和桌面版共用 ~/.codex）：boot.py install --codex [--apply | --remove]。

做两件事，都只增改 knotcord 自己的那一份，别的内容原样保留：
1. ~/.codex/config.toml：末尾追加一段带标记的受管段 [mcp_servers.knotcord]（让 Codex 能 search / timeline / get）。
   整文件按文本处理，不用会丢注释的"读成字典再写回"的方式。
2. ~/.codex/hooks.json（Codex 官方的 hook 配置文件，里面常有别的工具的条目）：
   往 SessionStart（开局注入）和 SessionEnd（收尾）各并入一条，不碰别的条目。

改文件前先把原文件备份成同目录的 <文件名>.bak-knotcord（只留一份，内容有变才重新备份）；
改完立刻重新解析校验；重复运行不产生重复段；--remove 撤销。解释器、脚本一律写绝对路径。
Codex 的 hook 要用户在 Codex 里用 /hooks 审核并点"信任"之后才会运行；信任记录绑定 hook 内容的哈希，
这是 Codex 的安全设计，本模块不替用户写信任。

Codex 规格（官方文档 https://developers.openai.com/codex/hooks 、/codex/mcp，对照 0.167.0 源码）：
- hook 的 stdin 字段和 Claude Code 一致：session_id、transcript_path、cwd、source（startup/resume/clear/compact/fork）。
- SessionStart 的 stdout 作为开发者上下文注入；SessionEnd 超时默认 1 秒、最长 3 秒。
- Windows 下 hook 命令交给会话所用的 shell（本机是 PowerShell）执行，commandWindows 优先于 command。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from knotcord.core import paths

SERVER_NAME = "knotcord"
BAK_SUFFIX = ".bak-knotcord"
BEGIN_PREFIX = "# >>> knotcord"
BEGIN_LINE = "# >>> knotcord：由 boot.py install --codex 管理，请勿手改；撤销用 install --codex --remove >>>"
END_LINE = "# <<< knotcord <<<"

ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT_S = 30
SESSION_START_TIMEOUT_S = 5
SESSION_END_TIMEOUT_S = 3        # Codex 对 SessionEnd 的上限

_BLOCK_RE = re.compile(r"^# >>> knotcord[^\r\n]*\r?\n.*?^# <<< knotcord <<<[ \t]*(?:\r?\n|\Z)", re.M | re.S)
_ENTRY_RE = re.compile(r"knotcord[\\/]+hooks[\\/]+entry\.py", re.I)
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_:\\/.\-]+$")


class SetupError(Exception):
    """可以直接给用户看的失败原因；出现时一个文件都没动。"""


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


# ---------------------------------------------------------------- 要写进去的内容

def _q(s: str) -> str:
    """TOML 基本字符串（和 JSON 字符串转义规则一致，反斜杠写成 \\\\）。"""
    return json.dumps(s, ensure_ascii=False)


def mcp_block(python: str, root: Path = ROOT, home: Path | None = None, codex: Path | None = None,
              newline: str = "\n") -> str:
    """受管段全文（含首尾标记行，以换行结尾）。"""
    boot = root / "knotcord" / "boot.py"
    env = {"KNOTCORD_HOME": str(home if home is not None else paths.home())}
    if os.environ.get("CODEX_HOME"):          # 用户自定义了 CODEX_HOME：server 里的 worker 要扫同一个目录
        env["CODEX_HOME"] = str(codex if codex is not None else codex_home())
    lines = [
        BEGIN_LINE,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_q(python)}",
        "args = [" + ", ".join(_q(a) for a in ("-I", "-X", "utf8", str(boot), "serve")) + "]",
        "env = { " + ", ".join(f"{k} = {_q(v)}" for k, v in env.items()) + " }",
        f"startup_timeout_sec = {STARTUP_TIMEOUT_S}",
        'disabled_tools = ["ingest"]    # ingest 是给 Claude Code 的 hook 用的，Codex 里的 agent 不需要看到它',
        END_LINE,
    ]
    return newline.join(lines) + newline


def hook_command(python: str, entry: str, event: str) -> str:
    """Codex 在 Windows 里把命令交给 PowerShell。路径里没有空格等特殊字符时写成裸命令（PowerShell 和 cmd 都能跑），
    否则写 PowerShell 的 & '路径' 形式。"""
    parts = [python, "-I", "-S", "-X", "utf8", entry, event, "--codex"]
    if all(_SAFE_ARG.match(p) for p in parts):
        return " ".join(parts)
    return "& " + " ".join(p if _SAFE_ARG.match(p) else "'" + p.replace("'", "''") + "'" for p in parts)


def hook_groups(python: str, root: Path = ROOT) -> dict[str, list[dict]]:
    """要并进 hooks.json 的条目：{事件名: [matcher 组]}。"""
    entry = str(root / "knotcord" / "hooks" / "entry.py")

    def handler(event: str, timeout: int) -> dict:
        cmd = hook_command(python, entry, event)
        return {"type": "command", "command": cmd, "commandWindows": cmd, "timeout": timeout}

    return {
        # resume / fork 不注入（保住提示缓存，和 Claude 那边一致）；compact 之后上下文被压缩，补注一次
        "SessionStart": [{"matcher": "startup|clear|compact",
                          "hooks": [handler("session-start", SESSION_START_TIMEOUT_S)]}],
        "SessionEnd": [{"hooks": [handler("session-end", SESSION_END_TIMEOUT_S)]}],
    }


# ---------------------------------------------------------------- config.toml（文本级编辑）

def _newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def edit_config(old: str, block: str | None) -> str:
    """block 不为 None：替换已有受管段，没有就追加到文件末尾；为 None：删掉受管段。其余字节不动。"""
    m = _BLOCK_RE.search(old)
    nl = _newline_of(old)
    if block is None:
        if not m:
            return old
        before = old[:m.start()]
        if before.endswith(nl + nl):                  # 追加时多加的那个空行一起删
            before = before[:-len(nl)]
        return before + old[m.end():]
    if m:
        return old[:m.start()] + block + old[m.end():]
    out = old
    if out and not out.endswith("\n"):
        out += nl
    if out and not out.endswith(nl + nl):
        out += nl
    return out + block


def _parse_toml(text: str, what: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SetupError(f"{what} 不是合法 TOML（{e}），没有改动任何文件") from e


def _without_server(d: dict) -> dict:
    out = dict(d)
    servers = dict(out.get("mcp_servers") or {})
    servers.pop(SERVER_NAME, None)
    if servers:
        out["mcp_servers"] = servers
    else:
        out.pop("mcp_servers", None)
    return out


def plan_config(old: str | None, block: str | None) -> str:
    """返回新文本；校验：改前改后都能解析，除 knotcord 这一段外语义不变，且新的段确实读得出来。"""
    old_text = old or ""
    old_parsed = _parse_toml(old_text, "现有 config.toml") if old_text.strip() else {}
    if block is None and not _BLOCK_RE.search(old_text):
        return old_text                                   # 撤销时本来就没有受管段：什么都不做
    new_text = edit_config(old_text, block)
    try:
        new_parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        rest = _BLOCK_RE.sub("", old_text)
        if re.search(r"mcp_servers\s*\.\s*[\"']?knotcord", rest) or re.search(r"^\s*knotcord\s*=", rest, re.M):
            raise SetupError("config.toml 里已有手工登记的 [mcp_servers.knotcord]（不在 KnotCord 的受管段内）。"
                             "请先在终端跑 codex mcp remove knotcord 或自己删掉那一段，再重试；没有改动任何文件") from e
        raise SetupError(f"生成的 config.toml 不是合法 TOML（{e}），没有改动任何文件") from e
    if _without_server(new_parsed) != _without_server(old_parsed):
        # 受管段之外的内容读出来不一样：不该发生，拦住
        raise SetupError("校验失败：改动会影响 knotcord 以外的配置，没有改动任何文件")
    got = (new_parsed.get("mcp_servers") or {}).get(SERVER_NAME)
    if block is not None and not isinstance(got, dict):
        raise SetupError("校验失败：写入后读不出 [mcp_servers.knotcord]，没有改动任何文件")
    return new_text


# ---------------------------------------------------------------- hooks.json（按 JSON 合并，只增删自己的条目）

def _is_ours(h) -> bool:
    if not isinstance(h, dict):
        return False
    for k in ("command", "commandWindows", "command_windows"):
        v = h.get(k)
        if isinstance(v, str) and _ENTRY_RE.search(v) and "--codex" in v:
            return True
    return False


def plan_hooks(old: str | None, groups: dict[str, list[dict]] | None) -> str:
    """groups 为 None 表示撤销。返回新文本（保留原文件的换行风格和末尾换行）。"""
    nl = _newline_of(old or "")
    try:
        data = json.loads(old) if old and old.strip() else {}
    except ValueError as e:
        raise SetupError(f"现有 hooks.json 不是合法 JSON（{e}），没有改动任何文件") from e
    if not isinstance(data, dict):
        raise SetupError("现有 hooks.json 顶层不是对象，没有改动任何文件")
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise SetupError("现有 hooks.json 的 hooks 不是对象，没有改动任何文件")
    before = json.loads(json.dumps(data))
    events = list(hooks) + [e for e in (groups or {}) if e not in hooks]
    for ev in events:
        cur = hooks.get(ev, [])
        if not isinstance(cur, list):
            if groups and ev in groups:
                raise SetupError(f"现有 hooks.json 里 {ev} 不是数组，没有改动任何文件")
            continue
        ours_now = [g for g in cur if isinstance(g, dict) and isinstance(g.get("hooks"), list)
                    and any(_is_ours(h) for h in g["hooks"])]
        if groups and ev in groups and ours_now == groups[ev]:
            continue                                  # 已经是想要的样子：不挪位置
        kept: list = []
        for g in cur:
            hs = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hs, list) or not any(_is_ours(h) for h in hs):
                kept.append(g)
                continue
            rest = [h for h in hs if not _is_ours(h)]
            if rest:
                kept.append({**g, "hooks": rest})
        if groups and ev in groups:
            kept.extend(json.loads(json.dumps(g)) for g in groups[ev])
        if kept:
            hooks[ev] = kept
        else:
            hooks.pop(ev, None)
    if old is not None and data == before:
        return old                                    # 语义没变：不重排原文件的格式
    text = json.dumps(data, indent=2, ensure_ascii=False)
    trailing = (old.endswith("\n") if old else True)
    if nl == "\r\n":
        text = text.replace("\n", "\r\n")
    return text + (nl if trailing else "")


# ---------------------------------------------------------------- 读写（只动 config.toml 和 hooks.json 两个文件）

def _read(p: Path) -> str | None:
    try:
        return p.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise SetupError(f"{p} 不是 UTF-8 文本（{e}），没有改动任何文件") from e


def _write(p: Path, text: str) -> None:
    data = text.encode("utf-8")
    tmp = p.with_name(p.name + ".knotcord-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError:
        try:                                  # 目标被别的进程占着（Codex 桌面版）时退回直接写
            p.write_bytes(data)
        finally:
            tmp.unlink(missing_ok=True)


@dataclass
class Change:
    label: str
    path: Path
    old: str | None
    new: str | None          # None：文件本来就不存在、也不用建（撤销时）

    @property
    def changed(self) -> bool:
        return self.old != self.new

    @property
    def backup(self) -> Path:
        return self.path.with_name(self.path.name + BAK_SUFFIX)


def build_plan(python: str, *, remove: bool = False, home: Path | None = None, root: Path = ROOT) -> list[Change]:
    home = home or codex_home()
    if not home.is_dir():
        raise SetupError(f"找不到 Codex 配置目录 {home}（没装 Codex，或用 CODEX_HOME 指到了不存在的位置），没有改动任何文件")
    cfg_path, hooks_path = home / "config.toml", home / "hooks.json"
    old_cfg, old_hooks = _read(cfg_path), _read(hooks_path)
    block = None if remove else mcp_block(python, root, codex=home, newline=_newline_of(old_cfg or ""))
    new_cfg = plan_config(old_cfg, block)
    new_hooks = plan_hooks(old_hooks, None if remove else hook_groups(python, root))
    if remove:
        new_cfg = None if old_cfg is None else new_cfg
        new_hooks = None if old_hooks is None else new_hooks
    return [Change("config.toml（MCP server）", cfg_path, old_cfg, new_cfg),
            Change("hooks.json（SessionStart / SessionEnd）", hooks_path, old_hooks, new_hooks)]


def apply_plan(changes: list[Change]) -> None:
    for c in changes:
        if not c.changed:
            continue
        if c.old is not None:
            c.backup.write_bytes(c.old.encode("utf-8"))          # 只留一份：直接覆盖上一份
        if c.new is not None:
            _write(c.path, c.new)
    # 写完立刻重新读、重新解析，坏了马上报
    for c in changes:
        text = _read(c.path)
        if text is None:
            continue
        if c.path.suffix == ".toml":
            _parse_toml(text, f"写入后的 {c.path.name}")
        else:
            json.loads(text)


# ---------------------------------------------------------------- 命令入口

def _show(c: Change, python: str, remove: bool, out: Callable[[str], None]) -> None:
    if not c.changed:
        out(f"  {c.path}（{c.label}）：内容无变化")
        return
    verb = "将删除" if remove else ("将新建" if c.old is None else "将修改")
    out(f"  {c.path}（{c.label}）：{verb}" + (f"，原文件先备份为 {c.backup.name}" if c.old is not None else ""))
    a = [] if c.old is None else c.old.splitlines()
    b = c.new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for ln in a[i1:i2]:
            out("    - " + ln)
        for ln in b[j1:j2]:
            out("    + " + ln)
    out(f"    （其余 {sum(i2 - i1 for t, i1, i2, _, _ in sm.get_opcodes() if t == 'equal')} 行原样保留）")


def run(python: str, *, apply: bool, remove: bool, out: Callable[[str], None] = print, home: Path | None = None,
        root: Path = ROOT) -> int:
    home = home or codex_home()
    mode = "撤销" if remove else "接入"
    out(("【执行】" if apply else "【预演，不写任何文件；加 --apply 才会写】") + f" {mode} Codex · 目录 {home} · 解释器 {python}")
    if not remove:
        if not os.path.isabs(python):
            out("解释器必须是绝对路径")
            return 1
        if not os.path.exists(python):
            out(f"提醒：{python} 不存在，写进去的配置会起不来")
    try:
        changes = build_plan(python, remove=remove, home=home, root=root)
    except SetupError as e:
        out(str(e))
        return 1
    out("")
    for c in changes:
        _show(c, python, remove, out)
    if apply:
        try:
            apply_plan(changes)
        except SetupError as e:
            out(str(e))
            return 1
        out("")
        out("已完成。" if any(c.changed for c in changes) else "没有需要改的。")
    if not remove:
        out("")
        out("接下来:")
        out("  1. 重开 Codex 会话，MCP server 才会被拉起；终端里可以用 codex mcp list 看到 knotcord。")
        out("  2. Codex 的 hook 要你审核后才运行：在 Codex 里输入 /hooks，把两条 knotcord 的 hook 点“信任”"
            "（只有这一步需要手动；以后改了 hook 内容要重新点一次）。")
        out("  3. 采集不依赖 hook：server 里的 worker 每 60 秒扫一次 ~/.codex/sessions。hook 只管开局注入和会话结束收尾。")
    else:
        out("")
        out("已撤销的是 knotcord 自己的那一段；备份文件 " + BAK_SUFFIX + " 没删，确认没问题后可以自己删掉。")
    return 0
