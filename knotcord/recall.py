"""提问前自动召回（第二轮；方案书第 11 节、需求"可以有"第 1 条）。默认关闭（recall.enabled）。

UserPromptSubmit 的 command hook 调这里。hook 进程只许标准库，所以走关键词这条轻量路径：
直接用 knotcord.core.index.fts_search 查 FTS，不 import numpy / onnxruntime / knotcord.search；
向量那一路等常驻 server 里有现成的模型再说。

规则（照 supermemory、OpenViking 的做法，阈值自己标定）：
- 提示太短（recall.min_chars）、斜杠命令、"好的 / 继续"这类短确认：跳过；
- 候选只来自本项目的笔记和会话总结（recall.cross_project 打开才跨项目）；
- 相关度不够就什么都不注入：提示和候选共有的检索词加权后要同时达到 min_score 和 min_coverage；
- 同一会话里注入过的不再注入（状态记在 tmp/recall-<会话>.json，SessionEnd 删，janitor 兜底扫）；
  开局注入里已经有的条目也不重复；
- 单次注入受 recall.budget_tokens 和条数限制，放不下的只给编号；
- 整个查询有 recall.timeout_ms 的期限（sqlite 进度回调打断），超时就当什么都没找到。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata

from knotcord import inject as _inject
from knotcord.core import db as _db
from knotcord.core import index as _index
from knotcord.core import paths as _paths
from knotcord.core import project as _project
from knotcord.core import tokens as _tokens

_CONFIRM = re.compile(
    r"^(好的?|行|可以|继续|接着|嗯+|ok(ay)?|yes|y|是的?|对|没问题|开始(吧)?|go( on)?|continue|proceed|"
    r"就这样|按你说的.{0,4}|确认|同意|谢谢|thanks?)[\s,，.。!！~]*$", re.I)
_ID_IN_INJECT = re.compile(r"\b([nuNSs])(\d+)\b")
_SAFE_SID = re.compile(r"[^A-Za-z0-9_-]")
_SEEN_MAX = 500
_BODY_CHARS = 1500
_LINE_TITLE = 90
_HARD_CHARS = 1500
_CANDIDATES = 30
HEADER = "可能相关的旧记录（knotcord get <编号> 看详情；无关就忽略）"


# ---------------------------------------------------------------- 跳过规则

def should_skip(prompt: str, cfg) -> bool:
    p = (prompt or "").strip()
    if len(p) < int(cfg.get("recall.min_chars", 12)):
        return True
    if p.startswith("/"):
        return True
    return bool(_CONFIRM.match(p))


# ---------------------------------------------------------------- 相关度

_HAN_RUN = re.compile("[㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f]{2,}")
_ASCII_WORD = re.compile(r"[a-z0-9]+(?:[-_./'@:][a-z0-9]+)*")
_EN_STOP = frozenset("the and for with this that you are how what why can not but from have has please help "
                     "make use want need let just its into then when will would could should there here been "
                     "was were your our about like some any all one two new now".split())
_RUN_MAX = 40    # 提示里一段汉字最多看这么长
_SEG_MAX = 8     # 一段连续命中最多按 8 字计


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def _seg_score(n: int) -> float:
    """连续命中 n 个汉字的分：2 字 1 分，之后每多 1 字加 1.5 分。越长的连续命中越不可能是巧合。"""
    return 1.0 + 1.5 * (n - 2) if n >= 2 else 0.0


_FILLER = re.compile("为什么|怎么样|怎么办|是不是|有没有|能不能|可不可以|应该|帮我|请你|麻烦|一下|看看|什么|怎么|"
                     "一个|这个|那个|我们|你们|如何|可以|是否|还是|或者|以及|然后|但是|因为|所以|就是|现在|刚才|"
                     "我想|我要|需要|一份|关于|其中|里面|没有|不是|还有|已经")


_PARTICLE = re.compile("[的了吗呢吧啊]")  # 助词几乎不在词中间：当分隔符
_EDGE_CHARS = "在对把并和与是要到过着里中上下都也又还就才将"


def query_terms(text: str, exclude: str = "") -> tuple[list[str], list[str], int]:
    """提示 -> (汉字段, 英文词/短语, 内容字数)。汉字段是连续的汉字串（先剔掉"为什么、帮我、一下"这类
    虚词，它们会把整句拆成更短、更有区分度的段）；英文词去掉常见虚词、过短的，以及项目名本身
    （exclude：项目内的每条笔记都带项目名，它没有区分度）。"""
    exclude = _norm(exclude)
    t = _PARTICLE.sub(" ", _FILLER.sub(" ", _norm(text)))
    runs = []
    for r in _HAN_RUN.findall(t):
        r = r.strip(_EDGE_CHARS)  # 段首段尾的"在、对、把、并"不是词的一部分
        if len(r) >= 2:
            runs.append(r[:_RUN_MAX])
    words = []
    for w in _ASCII_WORD.findall(t):
        if len(w) >= 3 and w not in _EN_STOP and w != exclude and w not in words:
            words.append(w)
    chars = sum(len(r) for r in runs) + sum(len(w) for w in words)
    return runs, words, chars


def _match_text(runs: list[str], words: list[str], doc: str) -> tuple[float, int, int]:
    """(得分, 命中字数, 最长连续命中)。汉字段从左到右贪心找文档里最长的连续命中；英文词整词/短语命中算 3 分。"""
    score, chars, best = 0.0, 0, 0
    for r in runs:
        i, L = 0, len(r)
        while i < L - 1:
            for k in range(min(L - i, _SEG_MAX), 1, -1):
                if r[i:i + k] in doc:
                    score += _seg_score(k)
                    chars += k
                    best = max(best, k)
                    i += k
                    break
            else:
                i += 1
    for w in words:
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", doc):
            score += 3.0
            chars += len(w)
            best = max(best, len(w))
    return score, chars, best


def relevance(terms: tuple[list[str], list[str], int], title: str, body: str) -> tuple[float, float, int]:
    """(得分, 覆盖率, 最长连续命中)。得分 = 正文+标题里的命中分，标题里命中的再加一半；
    覆盖率 = 命中字数 / 提示的内容字数。"""
    runs, words, total = terms
    if not total:
        return 0.0, 0.0, 0
    s_doc, c_doc, best = _match_text(runs, words, _norm(title + " " + (body or "")[:_BODY_CHARS]))
    s_title, _c, _b = _match_text(runs, words, _norm(title))
    return s_doc + 0.5 * s_title, c_doc / total, best


# ---------------------------------------------------------------- 会话内去重状态

def _seen_path(sid: str):
    return _paths.home() / "tmp" / f"recall-{_SAFE_SID.sub('_', sid)[:80]}.json"


def load_seen(sid: str) -> set[str]:
    if not sid:
        return set()
    try:
        data = json.loads(_seen_path(sid).read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_seen(sid: str, ids) -> None:
    if not sid:
        return
    keep = sorted(ids)[-_SEEN_MAX:]
    p = _seen_path(sid)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    try:
        _paths.tmp_dir()  # 目录不在就建（只有真有东西要记才走到这里）
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def forget(sid: str) -> None:
    """SessionEnd 用：会话结束，状态文件用完即删。"""
    if not sid:
        return
    try:
        _seen_path(sid).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 找和排

def _md(ts) -> str:
    return _inject._md(ts)


def find(conn, prompt: str, project: str | None, skip: set[str], cfg) -> list[dict]:
    """返回按相关度排好的候选：{key, line, score}。key 形如 n123 / u45 / s9，用来去重。"""
    q_text = prompt.strip()[:int(cfg.get("recall.query_chars", 300))]
    terms = query_terms(q_text, project or "")
    if not terms[2]:
        return []
    rows = _index.fts_search(conn, q_text, project=project or None, srcs=("note", "summary"),
                             limit=_CANDIDATES, active_only=True)
    if not rows:
        return []
    note_ids = [r["src_id"] for r in rows if r["src"] == "note"]
    sum_ids = [r["src_id"] for r in rows if r["src"] == "summary"]
    notes = {}
    if note_ids:
        for r in conn.execute("SELECT id, type, title, body, ts FROM notes WHERE state='active' AND id IN (%s)"
                              % ",".join("?" * len(note_ids)), note_ids):
            notes[r["id"]] = r
    sums = {}
    if sum_ids:
        for r in conn.execute("SELECT id, request, completed, learned, ts FROM summaries WHERE id IN (%s)"
                              % ",".join("?" * len(sum_ids)), sum_ids):
            sums[r["id"]] = r
    min_score = float(cfg.get("recall.min_score", 5.0))
    min_cov = float(cfg.get("recall.min_coverage", 0.2))
    out: list[dict] = []
    for r in rows:
        if r["src"] == "note":
            n = notes.get(r["src_id"])
            if n is None:
                continue
            key = ("u" if n["type"] == "unfinished" else "n") + str(n["id"])
            title, body, ts, kind = _inject._one(n["title"] or n["body"]), n["body"], n["ts"], n["type"] or "note"
        else:
            s = sums.get(r["src_id"])
            if s is None:
                continue
            key = "s" + str(s["id"])
            title = _inject._one(s["request"] or s["completed"])
            body = " ".join(str(s[k] or "") for k in ("completed", "learned"))
            ts, kind = s["ts"], "summary"
        if key in skip:
            continue
        score, cov, best = relevance(terms, title, body)
        # 只靠一堆零碎的两字词凑分不算相关：至少要有一处 3 字以上的连续命中或一个英文词命中
        if score < min_score or cov < min_cov or best < 3:
            continue
        out.append({"key": key, "score": score, "ts": ts or 0,
                    "line": f"{key} {_md(ts)} {kind} {title[:_LINE_TITLE]}"})
    out.sort(key=lambda x: (-x["score"], -x["ts"]))
    return out


def render(items: list[dict], project: str, cfg) -> tuple[str, list[str]]:
    """装填：先按完整行装，预算不够的后面几条只给编号。返回 (文本, 实际放进去的 key 列表)；一条都装不下返回 ("", [])。"""
    if not items:
        return "", []
    budget = int(cfg.get("recall.budget_tokens", 400))
    p = str(project or "").replace('"', "'")
    head = f'<knotcord-context kind="recall" project="{p}">\n{HEADER}\n'
    close = "</knotcord-context>"

    def fits(lines: list[str], bare: list[str]) -> bool:
        txt = head + "".join(x + "\n" for x in lines) + (("另有：" + " ".join(bare) + "\n") if bare else "") + close
        return len(txt) <= _HARD_CHARS and _tokens.estimate(txt) <= budget

    lines: list[str] = []
    bare: list[str] = []
    shown: list[str] = []
    for it in items[:int(cfg.get("recall.max_items", 5))]:
        if not bare and fits(lines + [it["line"]], []):
            lines.append(it["line"])
            shown.append(it["key"])
        elif fits(lines, bare + [it["key"]]):
            bare.append(it["key"])
            shown.append(it["key"])
    if not shown:
        return "", []
    text = head + "".join(x + "\n" for x in lines) + (("另有：" + " ".join(bare) + "\n") if bare else "") + close
    return text, shown


# ---------------------------------------------------------------- hook 入口

def run(data: dict, cfg) -> str:
    """UserPromptSubmit 的全部逻辑。返回要写到 stdout 的文本（空串 = 什么都不注入）。"""
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or should_skip(prompt, cfg):
        return ""
    sid = data.get("session_id")
    sid = sid if isinstance(sid, str) else ""
    proj = _project.project_key(data.get("cwd") or "")
    scope = None if cfg.get("recall.cross_project", False) else proj
    deadline = time.monotonic() + int(cfg.get("recall.timeout_ms", 600)) / 1000.0
    try:
        conn = _db.connect(readonly=True, busy_timeout_ms=200)
    except FileNotFoundError:
        return ""  # 库还没建：没有记忆
    seen = load_seen(sid)
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        injected = {m.group(1).lower() + m.group(2)
                    for m in _ID_IN_INJECT.finditer(_db.kv_get(conn, f"inject:{proj}", "") or "")}
        items = find(conn, prompt, scope, seen | injected, cfg)
        text, shown = render(items, proj, cfg)
    except sqlite3.Error:
        return ""  # 超时被打断、库忙、库坏：一律当没找到，不报错
    finally:
        conn.close()
    if text:
        save_seen(sid, seen | set(shown))
    return text
