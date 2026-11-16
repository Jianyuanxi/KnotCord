"""项目地图：项目之间的关系，只读用户手工维护的地图，自己不猜。只许 import 标准库和 knotcord.core。

数据来源（都只读，绝不写，不调模型、不联网）：
1. 全量地图：默认 ~/CLAUDE.md（projmap.map_file 可改）。Markdown，若干 `## 分类` 小节，每节一张表，
   表头含 名字 / 路径 / 是什么 / 关联。路径栏第一个反引号路径所指的目录就是一个节点，项目键 = 目录名；
   相对路径用小节标题里第一个反引号路径当基准拼起来。
2. 各节点目录下 CLAUDE.local.md（没有这一节就看 CLAUDE.md）里的「## 相关项目」一节。

有向边 A→B 只认三种，同一对只留一条，优先级 a > b > c，不自动补反向边：
  a. A 的「相关项目」某个列表项里的反引号路径，等于或落在 B 目录下（why = 冒号后的说明）
  b. A 那一行「关联」栏里的反引号绝对路径，等于或落在 B 目录下（why = "地图关联栏"）
  c. A 那一行「关联」栏里整词出现了 B 的项目键或 B 名字栏的某个词元（why = 关联栏原文）
另有"子目录"关系：地图里没有、库里有记录的项目 P，工作目录落在地图项目 Q 的目录之下，P⇄Q 双向（kind=subdir）。

写进 kv：键 projmap:<项目键>，值 [{"project": "B", "kind": "declared"|"subdir", "why": "…"}]。读用 related()。
统计信号（互相改文件、库里有记录但地图没有的项目）只在 report() 里给用户看，不进 kv、不影响检索和注入。
不参与"候选"统计的项目名：纯数字、长度小于 3、命中 projmap.exclude（不分大小写，支持 * 通配）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from knotcord.core import db, log
from knotcord.core.project import _true_name

_log = log.get_logger("projmap")

KEY_PREFIX = "projmap:"
KEY_AT = "projmap_at"        # 上次重建时间（不以 projmap: 开头，免得和叫 at 的项目重名）
KEY_FILES = "projmap_files"  # 上次重建读过的文件清单（JSON 列表），用来判断要不要重建
MIN_NAME_LEN = 3
WHY_MAX = 60                 # why 最长字数
UNMAPPED_MIN_NOTES = 20      # report：库里有记录但地图里没有的项目，笔记数门槛
CAND_MIN_FILES = 5           # report：跨项目改文件的候选，不同路径数门槛
_ABS_RE = re.compile(r"^(?:[a-z]:/|/)")
_TICK_RE = re.compile(r"`([^`]+)`")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_SEP_RE = re.compile(r"^:?-{1,}:?$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_SPLIT_NAME_RE = re.compile(r"[\s()（）]+")


# ---------------------------------------------------------------- 项目名是否参与候选统计

def name_ok(name, cfg) -> bool:
    n = str(name or "").strip()
    if len(n) < MIN_NAME_LEN or n.isdigit():
        return False
    low = n.lower()
    for pat in cfg.get("projmap.exclude", []) or []:
        if fnmatch.fnmatchcase(low, str(pat).lower()):
            return False
    return True


# ---------------------------------------------------------------- 路径工具

def _norm_path(p) -> str:
    s = posixpath.normpath(str(p or "").replace("\\", "/").strip().lower())
    return s.rstrip("/")


def _abs_norm(p) -> str | None:
    """小写、正斜杠的绝对路径；不是绝对路径返回 None。"""
    s = _norm_path(p)
    return s if _ABS_RE.match(s) else None


def _home() -> str:
    return _norm_path(os.path.expanduser("~"))


def _owner(path: str, dirs: dict[str, str]) -> str | None:
    """path（已规范化）落在哪个目录下：最深的那个目录对应的值；目录本身也算。"""
    parts = path.split("/")
    for i in range(len(parts), 0, -1):
        hit = dirs.get("/".join(parts[:i]))
        if hit:
            return hit
    return None


def _cut(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()[:WHY_MAX]


# ---------------------------------------------------------------- 解析全量地图

class Row(NamedTuple):
    name: str
    path_cell: str
    assoc: str
    base: str | None   # 所在小节标题里第一个反引号路径


def _cells(line: str) -> list[str] | None:
    s = line.strip()
    if not s.startswith("|"):
        return None
    s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def parse_map(text: str) -> list[Row]:
    """从地图文本里取出所有表格数据行。表头（含"路径"的那行）之后、遇到空行或非表格行之前都是数据行。"""
    rows: list[Row] = []
    base: str | None = None
    cols: dict[str, int] | None = None
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            t = _TICK_RE.search(m.group(2))
            base = t.group(1).strip() if t else None
            cols = None
            continue
        cells = _cells(line)
        if cells is None:
            cols = None
            continue
        if cols is None:
            if any("路径" in c for c in cells):
                def idx(word, default=None):
                    return next((i for i, c in enumerate(cells) if word in c), default)
                pi = idx("路径")
                cols = {"path": pi, "name": idx("名字", 0), "assoc": idx("关联", -1)}
            continue
        if not any(cells) or all(_SEP_RE.match(c) for c in cells if c):
            continue  # 空行或 |---|---| 分隔行
        def at(i):
            return cells[i] if 0 <= i < len(cells) else ""
        rows.append(Row(at(cols["name"]), at(cols["path"]), at(cols["assoc"]) if cols["assoc"] >= 0 else "", base))
    return rows


def _resolve_path(cell: str, base: str | None) -> str | None:
    """路径栏 -> 正斜杠、保留原大小写的绝对路径；解析不出返回 None。"""
    t = _TICK_RE.search(cell)
    raw = (t.group(1) if t else cell).strip()
    if not raw or raw in {"—", "-", "–", "无"}:
        return None
    p = raw.replace("\\", "/")
    if not _ABS_RE.match(p.lower()):
        if not base:
            return None
        bparts = base.replace("\\", "/").rstrip("/").split("/")
        rparts = p.strip("/").split("/")
        if bparts and rparts and bparts[-1].lower() == rparts[0].lower():
            bparts = bparts[:-1]  # 基准已经以这一段结尾：别重复拼
        p = "/".join(bparts + rparts)
        if not _ABS_RE.match(p.lower()):
            return None
    return posixpath.normpath(p).rstrip("/")


class Node(NamedTuple):
    key: str            # 项目键 = 目录名
    name: str           # 名字栏原文
    path: str           # 目录，正斜杠、原大小写
    norm: str           # 目录，小写
    assoc: str          # 关联栏原文
    tokens: tuple       # 能拿来在别人的关联栏里匹配的词（项目键 + 名字栏词元）


def _tok_ok(t: str) -> bool:
    if not any(ch.isalnum() for ch in t):
        return False
    return len(t) >= 3 if t.isascii() else len(t) >= 2


def _tokens(key: str, name: str) -> tuple:
    out: list[str] = []
    for t in [key, *_SPLIT_NAME_RE.split(name)]:
        t = t.strip()
        if t and _tok_ok(t) and t not in out:
            out.append(t)
    return tuple(out)


def _has_word(text: str, tok: str) -> bool:
    if tok.isascii():  # 前后不能是 ASCII 字母数字（汉字、符号、边界都行），大小写敏感
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", text) is not None
    return tok in text


# ---------------------------------------------------------------- 解析项目说明文件里的「相关项目」

def parse_related(text: str) -> list[tuple[list[str], str]] | None:
    """「## 相关项目」一节的列表项：[(反引号路径们, 冒号后的说明)]；没有这一节返回 None。"""
    items: list[str] = []
    in_sec, level = False, 0
    for line in text.splitlines():
        h = _HEAD_RE.match(line)
        if h:
            if in_sec and len(h.group(1)) <= level:
                break
            if not in_sec and h.group(2).strip().startswith("相关项目"):
                in_sec, level = True, len(h.group(1))
            continue
        if not in_sec:
            continue
        b = _BULLET_RE.match(line.strip()) if line.strip() else None
        if b:
            items.append(b.group(1))
        elif line.strip() and line[:1] in " \t" and items:
            items[-1] += " " + line.strip()  # 缩进的续行并进上一项
    if not in_sec:
        return None
    return [([m.group(1) for m in _TICK_RE.finditer(it)], _why_of(it)) for it in items]


def _why_of(item: str) -> str:
    """第一个反引号之外的冒号（中文或英文）之后的文字；没有冒号用"相关项目一节"。"""
    inside = False
    for i, ch in enumerate(item):
        if ch == "`":
            inside = not inside
        elif not inside and ch in "：:":
            return _cut(item[i + 1:].replace("`", "")) or "相关项目一节"
    return "相关项目一节"


# ---------------------------------------------------------------- 读文件、算声明的边

class MapData(NamedTuple):
    map_file: Path
    nodes: dict[str, Node]
    edges: dict[str, list[dict]]   # 声明的边（已按 projmap.max 截断）
    files: list[str]               # 这次读到的文件（地图 + 项目说明文件）
    skipped: list[str]             # 解析不了的行，给 CLI 打印


def map_path(cfg) -> Path:
    v = str(cfg.get("projmap.map_file", "") or "").strip()
    return Path(v).expanduser() if v else Path(os.path.expanduser("~")) / "CLAUDE.md"


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def load_map(cfg) -> MapData:
    mp = map_path(cfg)
    nodes: dict[str, Node] = {}
    skipped: list[str] = []
    files: list[str] = []
    text = _read(mp)
    if text is None:
        return MapData(mp, nodes, {}, files, skipped)
    files.append(str(mp))
    for row in parse_map(text):
        p = _resolve_path(row.path_cell, row.base)
        if p is None:
            skipped.append(f"路径解析不出：{row.name or '?'}（{row.path_cell or '空'}）")
            continue
        key = _true_name(Path(p))
        if key in nodes:
            skipped.append(f"项目键重复，只用第一行：{key}（{row.name}）")
            continue
        nodes[key] = Node(key, row.name, p, p.lower(), row.assoc, _tokens(key, row.name))

    node_dirs: dict[str, str] = {}
    for k, n in nodes.items():
        node_dirs.setdefault(n.norm, k)
    # 关联栏里列出、但地图里没有单独一行的目录（如某门课的作业工程）：算作挂在这一行下面的成员项目，
    # 两边互连（行 → 成员走下面的 b；成员 → 行在这里记）。带扩展名的当文件，不算。
    member_of: dict[str, list[str]] = {}
    for a, na in list(nodes.items()):
        for m in _TICK_RE.finditer(na.assoc):
            ap = _abs_norm(m.group(1))
            if not ap or "." in ap.rsplit("/", 1)[-1] or _owner(ap, node_dirs):
                continue
            key = _true_name(Path(ap))
            if key in nodes:
                continue
            nodes[key] = Node(key, "", ap, ap.lower(), "", ())
            node_dirs.setdefault(ap.lower(), key)
            member_of.setdefault(key, []).append(a)
    cap = max(1, int(cfg.get("projmap.max", 6)))
    edges: dict[str, list[dict]] = {}
    for a, na in nodes.items():
        found: dict[str, dict] = {}

        def add(b, why):
            if b and b != a and b not in found:
                found[b] = {"project": b, "kind": "declared", "why": why}

        for owner in member_of.get(a, ()):
            add(owner, f"列在 {owner} 的关联栏")

        notes_files: list[str] = []
        for fn in ("CLAUDE.local.md", "CLAUDE.md"):
            fp = Path(na.path) / fn
            t = _read(fp)
            if t is None:
                continue
            notes_files.append(str(fp))
            items = parse_related(t)
            if items is not None:
                for paths, why in items:
                    for rp in paths:
                        ap = _abs_norm(rp)
                        if ap:
                            add(_owner(ap, node_dirs), why)
                break  # 这个文件有这一节：用它，不再看下一个
        files.extend(notes_files)
        for m in _TICK_RE.finditer(na.assoc):  # b：关联栏里的绝对路径
            ap = _abs_norm(m.group(1))
            if ap:
                add(_owner(ap, node_dirs), "地图关联栏")
        if na.assoc.strip() and na.assoc.strip() not in {"—", "-", "–"}:  # c：关联栏里提到别的项目的名字
            for b, nb in nodes.items():
                if b != a and any(_has_word(na.assoc, t) for t in nb.tokens):
                    add(b, _cut(na.assoc))
        if found:  # 成员项目是地图里明写的清单，不占 projmap.max 的名额
            vals = list(found.values())
            mem = [e for e in vals if a in member_of.get(e["project"], ())]
            edges[a] = [e for e in vals if e not in mem][:cap] + mem
    return MapData(mp, nodes, edges, files, skipped)


# ---------------------------------------------------------------- 项目目录（库里的 sessions.cwd）

def _project_dir(project: str, cwd: str) -> str | None:
    """sessions.cwd 可能是项目里的子目录：路径里有一段等于项目名就截到那一段。
    家目录本身（及其上层）、磁盘根、AppData 下的临时/暂存目录不当项目目录，否则整个家目录都算它的。"""
    d = _norm_path(cwd)
    if not _ABS_RE.match(d):
        return None
    parts = d.split("/")
    low = project.lower()
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == low:
            d = "/".join(parts[:i + 1])
            break
    home = _home()
    if len([x for x in d.split("/") if x]) < 2 or home == d or home.startswith(d + "/"):
        return None
    if "/appdata/" in d + "/" and home and d.startswith(home + "/"):
        return None
    return d


def _project_dirs(conn, names: set[str]) -> dict[str, set[str]]:
    """{目录: {项目...}}。"""
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT DISTINCT project, cwd FROM sessions WHERE cwd IS NOT NULL AND cwd<>'' "
                          "AND project IS NOT NULL"):
        if r[0] in names:
            d = _project_dir(r[0], r[1])
            if d:
                out.setdefault(d, set()).add(r[0])
    return out


def _owners(path: str, dirs: dict[str, set[str]]) -> set[str]:
    """文件落在哪个项目目录下：最深的那个目录的项目。"""
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):  # 目录本身也算（i = len-1 是文件所在目录）
        owners = dirs.get("/".join(parts[:i]))
        if owners:
            return owners
    return set()


def _file_pairs(conn, names: set[str], dirs: dict[str, set[str]]) -> dict[tuple[str, str], int]:
    """{(写笔记的 A, 目录属于的 B): 不同路径数}。"""
    seen: set[tuple[str, str, str]] = set()
    out: dict[tuple[str, str], int] = {}
    if not dirs:
        return out
    for r in conn.execute("SELECT project, files FROM notes WHERE files IS NOT NULL AND files NOT IN ('', '[]') "
                          "AND project IS NOT NULL"):
        a = r[0]
        if a not in names:
            continue
        try:
            files = json.loads(r[1])
        except (TypeError, ValueError):
            continue
        if not isinstance(files, list):
            continue
        for f in files:
            p = _norm_path(f)
            if not _ABS_RE.match(p):
                continue
            for b in _owners(p, dirs):
                if b != a and (a, b, p) not in seen:
                    seen.add((a, b, p))
                    out[(a, b)] = out.get((a, b), 0) + 1
    return out


def _db_projects(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT project FROM notes WHERE project IS NOT NULL AND project<>'' "
        "UNION SELECT project FROM summaries WHERE project IS NOT NULL AND project<>'' "
        "UNION SELECT project FROM sessions WHERE project IS NOT NULL AND project<>''")}


# ---------------------------------------------------------------- 汇总

class Analysis(NamedTuple):
    data: MapData
    merged: dict[str, set[str]]       # 子目录项目 P -> {它所在的地图项目 Q}
    result: dict[str, list[dict]]     # 要写进 kv 的内容


def analyze(conn, cfg) -> Analysis:
    """只算不写。"""
    data = load_map(cfg)
    if not data.nodes:
        return Analysis(data, {}, {})
    node_dirs: dict[str, str] = {}
    for k, n in data.nodes.items():
        node_dirs.setdefault(n.norm, k)
    others = {p for p in _db_projects(conn) if p not in data.nodes}
    merged: dict[str, set[str]] = {}
    for d, ps in sorted(_project_dirs(conn, others).items()):
        q = _owner(d, node_dirs)
        if q:
            for p in ps:
                merged.setdefault(p, set()).add(q)
    result: dict[str, list[dict]] = {a: list(v) for a, v in data.edges.items()}
    sub: dict[str, list[dict]] = {}
    for p, qs in merged.items():
        for q in qs:
            sub.setdefault(q, []).append({"project": p, "kind": "subdir", "why": "子目录"})
            sub.setdefault(p, []).append({"project": q, "kind": "subdir", "why": "子目录"})
    for k, items in sub.items():
        result.setdefault(k, []).extend(sorted(items, key=lambda x: x["project"]))
    return Analysis(data, merged, result)


def compute(conn, cfg) -> dict[str, list[dict]]:
    """只算不写：{项目键: [{project, kind, why}]}。"""
    return analyze(conn, cfg).result


def _store(conn, an: Analysis, now: float) -> None:
    with db.write_tx(conn):
        conn.execute("DELETE FROM kv WHERE key LIKE ?", (KEY_PREFIX + "%",))
        for p, items in an.result.items():
            conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (KEY_PREFIX + p, json.dumps(items, ensure_ascii=False)))
        for k, v in ((KEY_AT, f"{now:.3f}"), (KEY_FILES, json.dumps(sorted(an.data.files), ensure_ascii=False))):
            conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, v))


def build(conn, cfg, now: float | None = None, analysis: Analysis | None = None) -> dict[str, list[dict]]:
    """重建并写 kv（先清掉旧的 projmap:*）。关着返回 {}，也不动 kv。地图文件不存在或解析不出行：清空旧的，返回 {}。"""
    now = time.time() if now is None else now
    if not cfg.get("projmap.enabled", True):
        return {}
    an = analysis or analyze(conn, cfg)
    _store(conn, an, now)
    _log.info("项目地图已重建: %d 个节点，%d 个项目有相关项目", len(an.data.nodes), len(an.result))
    return an.result


def stale(conn, cfg) -> bool:
    """地图文件或任何一个读过的项目说明文件的 mtime 晚于上次重建，或者读到的文件清单变了（新建、删除）。"""
    try:
        at = float(db.kv_get(conn, KEY_AT, "0") or 0)
        stored = json.loads(db.kv_get(conn, KEY_FILES, "[]") or "[]")
    except (ValueError, sqlite3.Error):
        return True
    files = load_map(cfg).files
    if not at:
        return bool(files)
    if not isinstance(stored, list) or sorted(files) != sorted(map(str, stored)):
        return True
    for f in files:
        try:
            if os.stat(f).st_mtime > at:
                return True
        except OSError:
            return True
    return False


def refresh_if_stale(conn, cfg, now: float | None = None) -> int | None:
    """janitor 每次常规运行调用：过期才重建，返回有相关项目的项目数；没动返回 None。"""
    if not cfg.get("projmap.enabled", True) or not stale(conn, cfg):
        return None
    return len(build(conn, cfg, now))


def related(conn, project: str) -> list[dict]:
    """读 kv；没算过、读不到、内容不对都返回空列表。kind 为 declared 或 subdir（旧数据没有 kind 的按 declared 用）。"""
    try:
        raw = db.kv_get(conn, KEY_PREFIX + str(project), "") or ""
        items = json.loads(raw) if raw else []
    except (sqlite3.Error, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict) and x.get("project")]


# ---------------------------------------------------------------- 给用户看的统计（不进 kv）

class Report(NamedTuple):
    unmapped: list[tuple[str, int]]          # (项目, 笔记数)：库里有记录、地图里没有
    candidates: list[tuple[str, str, int]]   # (A, B, 不同路径数)：A 的笔记改过 B 目录下的文件，地图里没连


def report(conn, cfg, an: Analysis) -> Report:
    nodes, merged = an.data.nodes, an.merged
    unmapped = []
    for p, n in conn.execute("SELECT project, COUNT(*) FROM notes WHERE project IS NOT NULL AND project<>'' "
                             "GROUP BY project HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC, project",
                             (UNMAPPED_MIN_NOTES,)):
        if p not in nodes and p not in merged and name_ok(p, cfg):
            unmapped.append((p, n))

    names = {p for p in _db_projects(conn) if name_ok(p, cfg)} | set(nodes)
    dirs = _project_dirs(conn, names)
    for k, n in nodes.items():
        dirs.setdefault(n.norm, set()).add(k)

    def canon(p: str) -> str:
        return sorted(merged[p])[0] if p in merged else p

    linked = {(a, x["project"]) for a, v in an.result.items() for x in v}
    agg: dict[tuple[str, str], int] = {}
    for (a, b), n in _file_pairs(conn, names, dirs).items():
        ca, cb = canon(a), canon(b)
        if ca != cb:
            agg[(ca, cb)] = agg.get((ca, cb), 0) + n
    cands = sorted(((a, b, n) for (a, b), n in agg.items() if n >= CAND_MIN_FILES and (a, b) not in linked),
                   key=lambda x: (-x[2], x[0], x[1]))
    return Report(unmapped, cands)
