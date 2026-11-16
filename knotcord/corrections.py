"""纠错统计：给周复盘查"用户纠正了什么、同一个错犯了几次"。

提炼时用户明确纠正了助手的做法，会产出一条 tags 带 correction 的笔记（title"错在哪 → 应该怎样"）。
这里只查库、不调模型：按项目分组，标题相近的归一类（相似度用 consolidate.titles_similar）。
superseded 的也算：每周整理把相近的旧笔记标成 superseded，它们正是"同一个错又犯了一次"的证据。
"""

from __future__ import annotations

import json
import time

from knotcord import consolidate
from knotcord.core import config as _config

TAG = "correction"
_DAY = 86400


def _has_tag(raw) -> bool:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return False
    return isinstance(v, list) and TAG in v


def query(conn, *, days: int = 7, project: str | None = None, now: float | None = None) -> list[dict]:
    """tags 含 correction 的笔记，近 days 天（days<=0 不限），按项目、时间排好。每项 {id, project, ts, title}。"""
    now = time.time() if now is None else now
    cutoff = now - days * _DAY if days > 0 else 0
    sql = "SELECT id, project, ts, title, tags FROM notes WHERE tags LIKE ?"
    args: list = [f'%"{TAG}"%']
    if project is not None:
        sql += " AND project=?"
        args.append(project)
    out = []
    for r in conn.execute(sql, args):
        if not _has_tag(r["tags"]) or consolidate._sec(r["ts"]) < cutoff:
            continue
        out.append({"id": r["id"], "project": r["project"] or "-", "ts": consolidate._sec(r["ts"]),
                    "title": " ".join(str(r["title"] or "").split())})
    out.sort(key=lambda x: (x["project"], x["ts"], x["id"]))
    return out


def cluster(rows: list[dict], thr: float) -> list[list[dict]]:
    """同一项目内按标题相似度归类：从旧到新，每条并进第一个和簇首相似的簇，否则自成一簇。
    只和簇首比，不传递（A~B、B~C 不会把 A、C 绑在一起）。返回簇的列表，簇内按时间。"""
    clusters: list[list[dict]] = []
    heads: list[tuple[str, str]] = []  # (项目, 簇首归一化标题)，和 clusters 一一对应
    for r in rows:
        n = consolidate._norm(r["title"])
        for i, (p, hn) in enumerate(heads):
            if p == r["project"] and consolidate.titles_similar(n, hn, thr):
                clusters[i].append(r)
                break
        else:
            clusters.append([r])
            heads.append((r["project"], n))
    return clusters


def _md(ts: float) -> str:
    return time.strftime("%m-%d", time.localtime(ts))


def render(conn, *, days: int = 7, project: str | None = None, now: float | None = None, cfg=None) -> str:
    cfg = cfg or _config.load()
    rows = query(conn, days=days, project=project, now=now)
    if not rows:
        return "无"
    groups = cluster(rows, float(cfg.get("corrections.similarity", 0.7)))
    n_class = len(groups)
    span = f"近 {days} 天" if days > 0 else "全部"
    lines = [f"{span}纠错 {len(rows)} 条，{n_class} 类"]
    by_proj: dict[str, list[list[dict]]] = {}
    for g in groups:
        by_proj.setdefault(g[0]["project"], []).append(g)
    for proj in sorted(by_proj):
        lines.append(f"## {proj}")
        for g in sorted(by_proj[proj], key=lambda g: (-len(g), g[0]["ts"])):  # 犯得多的类在前
            tail = f" [同类 {len(g)} 次]" if len(g) > 1 else ""
            for r in g:
                lines.append(f"n{r['id']} {_md(r['ts'])} {r['title']}{tail}")
    return "\n".join(lines)
