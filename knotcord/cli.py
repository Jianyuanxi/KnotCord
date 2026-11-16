"""命令行：python -I -X utf8 <插件根>\\knotcord\\boot.py <子命令>（或 python -m knotcord <子命令>）。

usage / doctor / migrate / reindex / drain / serve / install / uninstall。
install、uninstall 默认只打印步骤和将要生成的内容（--dry-run），--apply 才把插件根下的
hooks/hooks.json 和 mcp.json 重新生成；绝不动 ~/.claude，也不注册插件（那一步要用户在 Claude Code 里做）。
install --codex [--apply | --remove] 单独处理 Codex：只增改/撤销 ~/.codex 下 config.toml 和 hooks.json 里 knotcord 自己的部分
（先备份成 .bak-knotcord），见 codex_setup。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from knotcord import __version__
from knotcord.core import config, db, lease as _lease, paths

ROOT = Path(__file__).resolve().parent.parent
GB = 1024 ** 3


# ---------------------------------------------------------------- 小工具

def _out(s: str = "") -> None:
    print(s)


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


def _path_size(p: Path) -> tuple[int, int]:
    """(总字节, 文件数)；目录递归，不跟链接。"""
    try:
        if p.is_symlink():
            return 0, 0
        if p.is_file():
            return p.stat().st_size, 1
        total = files = 0
        if p.is_dir():
            for dp, dns, fns in os.walk(p):
                dns[:] = [d for d in dns if not os.path.islink(os.path.join(dp, d))]
                for f in fns:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                        files += 1
                    except OSError:
                        pass
        return total, files
    except OSError:
        return 0, 0


def _interpreter() -> str:
    """解释器本体（不是 venv 的启动器）。"""
    return getattr(sys, "_base_executable", None) or sys.executable


# ---------------------------------------------------------------- usage

def cmd_usage(args) -> int:
    from knotcord.extract import budget
    try:
        conn = db.connect(readonly=True)
    except FileNotFoundError:
        _out("尚无数据库：还没有任何记录")
        return 0
    try:
        _out(budget.usage_line(conn, config.load()))
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------- doctor

def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def cmd_doctor(args) -> int:
    from knotcord import procs
    from knotcord.extract import backlog, budget
    cfg = config.load()
    home = paths.home()
    now = time.time()
    _out(f"KnotCord {__version__} · 目录 {home}")
    _out(f"解释器 {sys.executable} · Python {sys.version.split()[0]}")
    warn: list[str] = []

    # 目录和大小（不依赖数据库）
    _out("")
    _out("[文件]")
    dbp = paths.db_path()
    db_bytes = sum(_path_size(Path(str(dbp) + s))[0] for s in ("", "-wal", "-shm"))
    _out(f"  knotcord.db（含 -wal、-shm）: {_mb(db_bytes)}" + ("" if dbp.exists() else "（尚未创建）"))
    for sub in ("logs", "backups", "legacy", "venv", "models"):
        p = home / sub
        if p.exists():
            b, n = _path_size(p)
            _out(f"  {sub}\\: {_mb(b)}（{n} 个文件）")
    tmp = home / "tmp"
    tmp_n = _path_size(tmp)[1] if tmp.exists() else 0
    _out(f"  tmp\\ 里的文件: {tmp_n} 个")
    if db_bytes > GB:
        warn.append(f"数据库超过 1 GB（{_mb(db_bytes)}），请检查是否该清理")
    legacy = home / "legacy" / "claude-mem.db"
    if legacy.exists():
        warn.append(f"迁移快照 legacy\\claude-mem.db（{_mb(legacy.stat().st_size)}）不会自动删；"
                    "停用 claude-mem 满 30 天后请决定是否删除")

    try:
        conn = db.connect(readonly=True)
    except FileNotFoundError:
        _out("")
        _out("数据库不存在：KnotCord 还没有记录过任何东西。")
        _finish_doctor(warn)
        return 0
    try:
        _out("")
        _out("[进程与租约]")
        if db.is_newer_than_code(conn):
            warn.append("库结构版本比本进程认识的新：本进程只做检索。请更新插件")
        rows = procs.list_procs(conn, now)
        if not rows:
            _out("  没有登记的 KnotCord 进程")
        for r in rows:
            state = "活着" if r["alive"] else ("pid 在但心跳过期" if r["pid_exists"] else "已死（待清理）")
            _out(f"  pid {r['pid']} {r.get('role', '?')} 起于 {_fmt_ts(r.get('started'))} "
                 f"心跳 {_fmt_ts(r.get('beat'))} · {state}")
        info = _lease.holder_info(conn)
        if info is None:
            _out("  租约: 无人持有")
        else:
            left = info["expires"] - now
            _out(f"  租约: {info['owner']}，" + ("已过期" if info["expired"] else f"还剩 {left:.0f} 秒"))

        _out("")
        _out("[积压与记账]")
        stats = backlog.unbatched_stats(conn)
        _out(f"  积压 {backlog.backlog_batches(conn, cfg)} 批（{len(stats)} 个会话有未提炼内容）")
        pend = conn.execute("SELECT count(*) FROM pending").fetchone()[0]
        _out(f"  pending 待处理: {pend} 行")
        _out(f"  {budget.usage_line(conn, cfg, now)}")
        for prov in ("codex", "deepseek"):
            until = budget.cooldown_until(conn, prov)
            if until > now:
                _out(f"  {prov} 冷却到 {_fmt_ts(until)}")
        fails = conn.execute(
            "SELECT id, session_id, provider, status, error, created FROM batches "
            "WHERE status IN ('failed','dead') ORDER BY id DESC LIMIT 5").fetchall()
        _out("  最近 5 次失败:" + ("" if fails else " 无"))
        for r in fails:
            _out(f"    批 {r['id']} {_fmt_ts(r['created'])} {r['status']} {r['provider'] or '-'} "
                 f"{(r['error'] or '')[:120]}")

        _out("")
        _out("[索引]")
        n_docs = conn.execute("SELECT count(*) FROM docs WHERE active=1").fetchone()[0]
        from knotcord.search import embed as _embed
        n_miss = conn.execute("SELECT count(*) FROM docs d WHERE d.active=1 AND NOT EXISTS "
                              "(SELECT 1 FROM docs_vec v WHERE v.doc_id=d.id AND v.model=?)",
                              (_embed.MODEL_ID,)).fetchone()[0]
        _out(f"  可检索文档 {n_docs} 条，其中缺向量 {n_miss} 条"
             f"（向量模型{'已就绪' if _embed.model_present() else '未下载，boot.py fetch-model 可下载'}）")
        for key, label in (("janitor:last", "上次清理"), ("janitor:weekly", "上次每周清理")):
            v = db.kv_get(conn, key)
            _out(f"  {label}: {_fmt_ts(v) if v else '从未'}")
        bak = home / "backups" / "knotcord.db"
        _out("  备份: " + (f"{_mb(bak.stat().st_size)}，{_fmt_ts(bak.stat().st_mtime)}" if bak.exists() else "无"))
        corrupt = db.kv_get(conn, "db_corrupt")
        if corrupt:
            warn.append(f"库被标记为损坏，已停止记录: {corrupt}")
    finally:
        conn.close()
    _finish_doctor(warn)
    return 0


def _finish_doctor(warn: list[str]) -> None:
    _out("")
    if warn:
        _out("[需要注意]")
        for w in warn:
            _out(f"  ! {w}")
    else:
        _out("没有发现问题。")


# ---------------------------------------------------------------- migrate / reindex / drain / serve

def cmd_migrate(args) -> int:
    from knotcord.migrate import claude_mem
    src = args.src or str(Path.home() / ".claude-mem" / "claude-mem.db")
    if not Path(src).exists():
        _out(f"源库不存在: {src}")
        return 1
    conn = db.connect()
    try:
        res = claude_mem.run(conn, src, snapshot=not args.no_snapshot, log=_out)
    finally:
        conn.close()
    bad = [k for k, v in res.items() if not v["一致"]]
    _out("全部一致" if not bad else "不一致的表: " + "、".join(bad))
    return 0 if not bad else 2


def cmd_reindex(args) -> int:
    from knotcord.search import memfiles
    conn = db.connect()
    try:
        n = memfiles.reindex(conn, root=args.memroot)
    finally:
        conn.close()
    _out(f"内置记忆文件索引已更新 {n} 个")
    return 0


def cmd_fetch_model(args) -> int:
    """下载向量检索用的嵌入模型到 ~/.knotcord/models（幂等；已就绪只校验）。"""
    from knotcord.search import embed
    _out(f"嵌入模型 {embed.MODEL_ID} -> {embed.model_dir()}")
    return embed.fetch_model(_out)


_PROJECT_TABLES = ("sessions", "events", "notes", "state_cards", "mem_flags", "summaries")


def rename_project(conn, old: str, new: str) -> dict[str, int]:
    """项目文件夹改名后把旧项目名的记录并到新名下。内置记忆文件（memfiles 及其 docs）按目录算项目，不动。"""
    from knotcord import inject
    counts: dict[str, int] = {}
    with db.write_tx(conn):
        for t in _PROJECT_TABLES:
            counts[t] = conn.execute(f"UPDATE {t} SET project=? WHERE project=?", (new, old)).rowcount
        counts["docs"] = conn.execute("UPDATE docs SET project=? WHERE project=? AND src!='memfile'",
                                      (new, old)).rowcount
        conn.execute("DELETE FROM kv WHERE key=?", (f"inject:{old}",))
    inject.render(conn, new)
    return counts


def cmd_rename_project(args) -> int:
    if args.old == args.new:
        _out("新旧项目名相同")
        return 1
    conn = db.connect()
    try:
        counts = rename_project(conn, args.old, args.new)
    finally:
        conn.close()
    _out(f"{args.old} → {args.new}: " + "，".join(f"{k} {v}" for k, v in counts.items()))
    return 0


def cmd_projmap(args) -> int:
    """立即重算项目地图（写 kv）并打印；--project 只打印那一个项目的。"""
    from knotcord import projmap
    cfg = config.load()
    if not cfg.get("projmap.enabled", True):
        _out("项目地图已关闭（projmap.enabled = false）")
        return 0
    conn = db.connect()
    try:
        res = projmap.build(conn, cfg)
    finally:
        conn.close()
    items = {args.project: res.get(args.project, [])} if args.project else res
    shown = {p: v for p, v in items.items() if v}
    if not shown:
        _out("无")
        return 0
    _out(f"{len(res)} 个项目有相关项目（min_score {cfg.get('projmap.min_score')}，每项目最多 {cfg.get('projmap.max')} 个）")
    for p in sorted(shown):
        _out(f"{p}: " + "；".join(f"{x['project']} {x['score']}（{x['why']}）" for x in shown[p]))
    return 0


def cmd_corrections(args) -> int:
    """纠错统计：tags 带 correction 的笔记，按项目分组、标题相近的归类。"""
    from knotcord import corrections
    try:
        conn = db.connect(readonly=True)
    except FileNotFoundError:
        _out("无")
        return 0
    try:
        _out(corrections.render(conn, days=args.days, project=args.project))
    finally:
        conn.close()
    return 0


def cmd_drain(args) -> int:
    from knotcord import drain
    return drain.main()


def cmd_serve(args) -> int:
    from knotcord.server import main as server_main
    return server_main.main()


# ---------------------------------------------------------------- install / uninstall

HOOK_STDLIB_ARGS = ["-I", "-S", "-X", "utf8"]
# 不叫 .mcp.json：在本仓库里开会话时 Claude Code 会把根目录的 .mcp.json 当项目级 server 再加载一遍，
# 那里没有 ${CLAUDE_PLUGIN_ROOT}，必然启动失败。plugin.json 的 mcpServers 指向这个文件。
MCP_FILE = "mcp.json"
SERVER_NAME = "plugin:knotcord:knotcord"


def _hook_cmd(python: str, event_arg: str, timeout: int = 5) -> dict:
    return {"type": "command", "command": python,
            "args": HOOK_STDLIB_ARGS + ["${CLAUDE_PLUGIN_ROOT}/knotcord/hooks/entry.py", event_arg],
            "timeout": timeout}


def _hook_ingest(with_last: bool) -> dict:
    inp = {"session_id": "${session_id}", "transcript_path": "${transcript_path}", "cwd": "${cwd}"}
    if with_last:
        inp["last"] = "${last_assistant_message}"
    return {"type": "mcp_tool", "server": SERVER_NAME, "tool": "ingest", "input": inp, "timeout": 5}


def hooks_json(python: str) -> dict:
    return {
        "description": "KnotCord 记忆插件：开局注入、收尾、增量入库。所有 hook 只写日志，永远 exit 0。",
        "hooks": {
            "SessionStart": [{"matcher": "startup|clear|compact", "hooks": [_hook_cmd(python, "session-start")]}],
            # 提问前召回：默认关（recall.enabled），关着时 hook 只读配置就退出；timeout 比 SessionStart 更紧
            "UserPromptSubmit": [{"hooks": [_hook_cmd(python, "prompt", timeout=3)]}],
            "Stop": [{"hooks": [_hook_ingest(True)]}],
            "PreCompact": [{"hooks": [_hook_ingest(False)]}],
            "SessionEnd": [{"hooks": [_hook_cmd(python, "session-end")]}],
        },
    }


def mcp_json(python: str) -> dict:
    return {"mcpServers": {"knotcord": {
        "type": "stdio", "command": python,
        "args": ["-I", "-X", "utf8", "${CLAUDE_PLUGIN_ROOT}/knotcord/boot.py", "serve"]}}}


def render(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def generated_files(python: str, root: Path = ROOT) -> dict[Path, str]:
    return {root / "hooks" / "hooks.json": render(hooks_json(python)),
            root / MCP_FILE: render(mcp_json(python))}


def codex_steps(python: str) -> list[str]:
    """Codex 客户端的接入步骤（只打印，不改 ~/.codex）。采集不用配：server 里的 worker 会自己扫 Codex 会话目录。
    一步到位用 install --codex --apply（见 codex_setup）；这里列的是等价的手工写法。"""
    from knotcord import codex_setup
    block = codex_setup.mcp_block(python, ROOT, paths.home())
    hooks = {"hooks": codex_setup.hook_groups(python, ROOT)}
    return [
        "",
        "Codex CLI / Desktop 作为第二个客户端（可选；共用同一个库，会话记录由 server 里的 worker 定期扫描采集）:",
        "  一步到位：boot.py install --codex --apply（改 ~/.codex 下的 config.toml 和 hooks.json，先备份，"
        "--remove 撤销，不加 --apply 只预演）。下面是等价的手工写法:",
        "  A. 检索 + 采集：在 ~/.codex/config.toml 末尾加下面这段（或在终端跑 codex mcp add，效果相同），"
        "重开 Codex 生效。Codex 拉起这个 server 时它同时做采集和提炼:",
        *("     " + ln for ln in block.splitlines()),
        "  B. 开局注入近况和会话结束收尾（可选）：把下面的条目并进 ~/.codex/hooks.json 的 hooks 里"
        "（别整份覆盖，里面已有别的工具的条目），然后在 Codex 里 /hooks 点信任；hook 命令一改就要重新信任:",
        *("     " + ln for ln in json.dumps(hooks, indent=2, ensure_ascii=False).splitlines()),
        "  C. 不接 B 也行：让 agent 开头调一次 knotcord 的 search（query 写项目名）。",
        f"  扫描设置（config.toml 的 [codex]）: enabled、scan_s、lookback_days（默认只看最近 "
        f"{config.DEFAULTS['codex.lookback_days']} 天内改过的会话）、sessions_dir",
    ]


def cmd_install(args) -> int:
    python = args.python or _interpreter()
    if getattr(args, "remove", False) and not getattr(args, "codex", False):
        _out("--remove 要和 --codex 一起用")
        return 1
    if getattr(args, "codex", False):  # 只处理 Codex 那边（~/.codex 的 config.toml 和 hooks.json），不生成插件文件
        from knotcord import codex_setup
        return codex_setup.run(python, apply=bool(args.apply) and not args.dry_run,
                               remove=bool(args.remove), out=_out)
    files = generated_files(python)
    apply = bool(args.apply) and not args.dry_run
    _out(("【执行】" if apply else "【预演，不写任何文件；加 --apply 才会写】") + f" 解释器: {python}")
    _out("")
    for p, text in files.items():
        same = p.exists() and p.read_text(encoding="utf-8") == text
        _out(f"{'写入' if apply else '将生成'} {p}" + ("（内容无变化）" if same else ""))
        if apply and not same:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="\n")
    venv = paths.home() / "venv"
    _out("")
    _out("接下来需要你手动做的步骤（本命令不会替你做，也不会改 ~/.claude）:")
    _out(f"  1. 建依赖环境（只用来放第三方包，不用它启动）: \"{python}\" -m venv \"{venv}\"")
    _out(f"     然后: \"{venv}\\Scripts\\python.exe\" -m pip install mcp numpy onnxruntime tokenizers")
    _out(f"  2. 校验插件格式: claude plugin validate \"{ROOT}\"")
    _out(f"  3. 试跑（只在这一次会话里加载，不注册）: claude --plugin-dir \"{ROOT}\"")
    _out("  4. 迁移旧数据（和 claude-mem 并行跑一周对比，不停用它）: "
         f"\"{python}\" -I -X utf8 \"{ROOT}\\knotcord\\boot.py\" migrate")
    _out("  5. 确认没问题再按 Claude Code 的插件安装流程注册；回退见 uninstall")
    for ln in codex_steps(python):
        _out(ln)
    return 0


def cmd_uninstall(args) -> int:
    home = paths.home()
    _out("【只列清单，不删任何东西】")
    _out("")
    _out("从 Claude Code 去掉 KnotCord（需要你在 Claude Code 里做，本命令不碰 ~/.claude）:")
    _out("  - 在 Claude Code 里用 /plugin 管理界面卸载 trace 插件，或结束用 --plugin-dir 加载的会话")
    _out("  - 卸载后 hook 和 MCP 注册随插件一起消失")
    _out("")
    _out(f"{home} 下各项（是否删除由你决定，删就进回收站）:")
    if not home.exists():
        _out("  目录不存在")
        return 0
    total = 0
    for p in sorted(home.iterdir()):
        b, n = _path_size(p)
        total += b
        _out(f"  {p}  {_mb(b)}" + (f"（{n} 个文件）" if p.is_dir() else ""))
    _out(f"  合计 {_mb(total)}")
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="knotcord", description="KnotCord 记忆插件")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("usage", help="今天的提炼次数、花费、积压").set_defaults(fn=cmd_usage)
    sub.add_parser("doctor", help="自查：进程、租约、积压、体积、最近失败").set_defaults(fn=cmd_doctor)
    m = sub.add_parser("migrate", help="从 claude-mem 迁移（源库只读）")
    m.add_argument("--src", help="源库路径，默认 ~/.claude-mem/claude-mem.db")
    m.add_argument("--no-snapshot", action="store_true", help="不另存快照")
    m.set_defaults(fn=cmd_migrate)
    r = sub.add_parser("reindex", help="重建内置记忆文件索引")
    r.add_argument("--memroot", help="默认 ~/.claude/projects（只读）")
    r.set_defaults(fn=cmd_reindex)
    sub.add_parser("fetch-model", help="下载向量检索用的嵌入模型（幂等）").set_defaults(fn=cmd_fetch_model)
    rp = sub.add_parser("rename-project", help="项目文件夹改名后把旧名下的记录并到新名")
    rp.add_argument("old")
    rp.add_argument("new")
    rp.set_defaults(fn=cmd_rename_project)
    pm = sub.add_parser("projmap", help="立即重算项目地图（项目之间的联系）并打印")
    pm.add_argument("--project", help="只打印这个项目的相关项目")
    pm.set_defaults(fn=cmd_projmap)
    co = sub.add_parser("corrections", help="纠错统计：用户纠正过什么、同类错几次")
    co.add_argument("--days", type=int, default=7, help="看最近几天，默认 7；0 = 不限")
    co.add_argument("--project", help="只看这个项目")
    co.set_defaults(fn=cmd_corrections)
    sub.add_parser("drain", help="一次性收尾（SessionEnd hook 拉起）").set_defaults(fn=cmd_drain)
    sub.add_parser("serve", help=f"启动 stdio MCP server（{MCP_FILE} 用）").set_defaults(fn=cmd_serve)
    for name, fn, text in (("install", cmd_install, "生成 hook 和 MCP 配置、打印安装步骤"),
                           ("uninstall", cmd_uninstall, "打印卸载步骤和 ~/.knotcord 各项大小")):
        p = sub.add_parser(name, help=text)
        g = p.add_mutually_exclusive_group()
        g.add_argument("--dry-run", action="store_true", help="只打印（默认）")
        g.add_argument("--apply", action="store_true",
                       help="真的写文件（仅 install；不带 --codex 只写插件根下两个文件，带 --codex 只写 ~/.codex 下两个文件）")
        if name == "install":
            p.add_argument("--python", help="写进配置的解释器绝对路径，默认当前解释器本体")
            p.add_argument("--codex", action="store_true",
                           help="把 KnotCord 接进 Codex：登记 MCP server、挂 SessionStart/SessionEnd hook")
            p.add_argument("--remove", action="store_true", help="和 --codex 一起用：撤销上面的接入")
        p.set_defaults(fn=fn)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)
