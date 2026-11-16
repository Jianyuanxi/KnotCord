"""内置记忆文件索引：~/.claude/projects/*/memory/*.md（只读，不改文件）。

每个文件登记成 memfiles 一行 + docs(src='memfile') 一条索引；按 mtime 增量重建。
项目名规则：目录名形如 E--Span、C--Users-alice，取最后一个非空的 '-' 段（Span、alice）；
全是横线（中文目录名被清成 '-'）就直接用目录名。含连字符的项目名会被截成最后一段，这是已知限制。
"""

from __future__ import annotations

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
    known = {r["path"]: r for r in conn.execute("SELECT id, path, mtime FROM memfiles")}
    prefix = str(base)
    updated = 0
    for p, f in found.items():
        try:
            mtime = int(f.stat().st_mtime)
            row = known.get(p)
            if row is not None and row["mtime"] == mtime:
                continue
            name, desc, body = parse_file(f)
        except OSError:
            continue
        project = project_from_dir(f.parent.parent.name)
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
