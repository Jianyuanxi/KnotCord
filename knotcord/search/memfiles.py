"""内置记忆文件索引：~/.claude/projects/*/memory/*.md（只读，不改文件）。

每个文件登记成 memfiles 一行 + docs(src='memfile') 一条索引；按 mtime 增量重建。
项目名怎么定（resolve_project）：Claude Code 把工作目录里每个非字母数字的字符都换成 '-' 当目录名
（E:\\ai-guardian-star -> E--ai-guardian-star），这个编码不可逆，所以按三步找回真实目录：
  1. 对照 sessions 表里已知的 cwd：编码后等于这个目录名的，用该会话记下的项目名（和笔记里的项目名天然一致）；
  2. 按盘符和目录名在磁盘上逐级匹配，找到真实路径后用 project_key 的规则（向上找 .git）取项目名；
  3. 都找不到（目录已删、盘不在）才退回 project_from_dir：取最后一个非空的 '-' 段，全是横线就用目录名。
局限：第 3 步会把带连字符的项目名截断；磁盘上同一编码对应多个目录时取第一个匹配的。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from knotcord.core import db
from knotcord.core.index import index_doc, remove_doc

INDEX_CAP = 20000
_FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def project_from_dir(dirname: str) -> str:
    parts = [p for p in dirname.split("-") if p]
    return parts[-1] if parts else dirname


_ENC_RE = re.compile(r"[^A-Za-z0-9]")
_DRIVE_RE = re.compile(r"^([A-Za-z])--(.*)$", re.S)
_WALK_BUDGET = 400  # 一次解析最多列几个目录，防止在大盘上没完没了


def encode_cwd(path: str) -> str:
    """Claude Code 的项目目录名编码：每个非字母数字字符（含 : \\ / . 空格和汉字）换成一个 '-'。"""
    return _ENC_RE.sub("-", path)


def _walk(base: str, rest: str, budget: list[int], out: list[str]) -> None:
    """在 base 下找编码后等于 rest 的目录，把所有命中的完整路径放进 out（凑到 2 个就够判定歧义，停）。"""
    if len(out) >= 2 or budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        entries = [e for e in os.scandir(base) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for e in entries:
        enc = encode_cwd(e.name)
        if rest == enc:
            out.append(e.path)
        elif rest.startswith(enc + "-"):
            _walk(e.path, rest[len(enc) + 1:], budget, out)
        if len(out) >= 2:
            return


def _disk_project(dirname: str) -> str | None:
    """磁盘上唯一匹配的目录 -> 项目名；没有匹配或有歧义（汉字目录名全被编码成 '-'，常有好几个同长的）返回 None。"""
    m = _DRIVE_RE.match(dirname)
    if not m or not m.group(2):
        return None
    root = f"{m.group(1)}:\\"
    if not os.path.isdir(root):
        return None
    found: list[str] = []
    _walk(root, m.group(2), [_WALK_BUDGET], found)
    if len(found) != 1:
        return None
    hit = found[0]
    from knotcord.core.project import project_key
    return project_key(hit, env={})  # env={}：不让环境里的 CLAUDE_PROJECT_DIR 干扰


def session_projects(conn) -> dict[str, str]:
    """sessions 表里已知 cwd 的编码 -> 项目名（同一编码对应多个项目名时取出现最多的）。"""
    counts: dict[tuple[str, str], int] = {}
    for r in conn.execute("SELECT cwd, project, count(*) AS n FROM sessions "
                          "WHERE cwd IS NOT NULL AND cwd<>'' AND project IS NOT NULL AND project<>'' "
                          "GROUP BY cwd, project"):
        k = (encode_cwd(r["cwd"]), r["project"])
        counts[k] = counts.get(k, 0) + r["n"]
    best: dict[str, tuple[int, str]] = {}
    for (enc, proj), n in counts.items():
        if enc not in best or n > best[enc][0]:
            best[enc] = (n, proj)
    return {k: v[1] for k, v in best.items()}


def resolve_project(dirname: str, known: dict[str, str] | None = None) -> str:
    """内置记忆目录名 -> 项目名，规则见模块说明。known 是 session_projects() 的结果。"""
    if known and dirname in known:
        return known[dirname]
    try:
        hit = _disk_project(dirname)
    except Exception:
        hit = None
    return hit or project_from_dir(dirname)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_file(path: Path) -> tuple[str, str, str]:
    """返回 (name, description, 正文)。没有 frontmatter 就用文件名和正文第一行。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    name = desc = ""
    body = text
    m = _FM_RE.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if not sep:
                continue
            k = k.strip().lower()
            if k == "name" and not name:
                name = _unquote(v)
            elif k == "description" and not desc:
                desc = _unquote(v)
    if not name:
        name = path.stem
    if not desc:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                desc = line
                break
    return name, desc, body


def reindex(conn, *, root: str | None = None) -> int:
    """增量重建；返回本次新增或更新的文件数（删掉的不计）。"""
    base = Path(root) if root else default_root()
    found: dict[str, Path] = {}
    if base.is_dir():
        for d in sorted(base.iterdir()):
            mem = d / "memory"
            if not mem.is_dir():
                continue
            for f in sorted(mem.glob("*.md")):
                if f.name.lower() == "memory.md" or not f.is_file():
                    continue
                found[str(f)] = f
    known = {r["path"]: r for r in conn.execute("SELECT id, path, mtime, project FROM memfiles")}
    prefix = str(base)
    updated = 0
    sess = session_projects(conn)
    proj_cache: dict[str, str] = {}

    def project_of(dirname: str) -> str:
        if dirname not in proj_cache:
            proj_cache[dirname] = resolve_project(dirname, sess)
        return proj_cache[dirname]

    for p, f in found.items():
        try:
            mtime = int(f.stat().st_mtime)
            row = known.get(p)
            if row is not None and row["mtime"] == mtime:
                # 内容没变，但项目名规则改过（或 sessions 里多了 cwd）：只改项目名，不重读重建
                project = project_of(f.parent.parent.name)
                if row["project"] != project:
                    with db.write_tx(conn):
                        conn.execute("UPDATE memfiles SET project=? WHERE id=?", (project, row["id"]))
                        conn.execute("UPDATE docs SET project=? WHERE src='memfile' AND src_id=?",
                                     (project, row["id"]))
                    updated += 1
                continue
            name, desc, body = parse_file(f)
        except OSError:
            continue
        project = project_of(f.parent.parent.name)
        text = "\n".join(x for x in (name, desc, body) if x)[:INDEX_CAP]
        with db.write_tx(conn):
            fid = conn.execute(
                "INSERT INTO memfiles(path, project, mtime, name, description) VALUES(?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET project=excluded.project, mtime=excluded.mtime, "
                "name=excluded.name, description=excluded.description RETURNING id",
                (p, project, mtime, name, desc)).fetchone()[0]
            index_doc(conn, "memfile", fid, project, mtime, text)
        updated += 1
    # 文件没了：删 memfiles 行和对应 doc（只动这个 root 之下的）
    for p, row in known.items():
        if p not in found and p.startswith(prefix):
            with db.write_tx(conn):
                remove_doc(conn, "memfile", row["id"])
                conn.execute("DELETE FROM memfiles WHERE id=?", (row["id"],))
    return updated
