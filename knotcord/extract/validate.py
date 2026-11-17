"""模型输出的解析和校验（细则 2.3）。

整体解析不了、顶层不是对象，抛 ValidationError（算这批一次失败）；
单个字段的问题只修不抛：非法 type 改 change、越界编号丢弃、超长截断。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from knotcord.core import tokens

TYPES = ("bugfix", "feature", "refactor", "change", "discovery", "decision")
TAGS = ("gotcha", "how-it-works", "problem-solution", "pattern", "trade-off", "correction")  # correction：用户纠正，见 corrections.py

# 上限比提示里要求的（标题 40、正文 300）略宽，只拦明显失控的输出
TITLE_MAX = 60
BODY_MAX = 400
FACT_MAX, FACTS_N = 120, 8
FILE_MAX, FILES_N = 200, 10
SLOT_MAX = 80
SUMMARY_MAX = 500
CARD_STR_MAX, CARD_LIST_N, CARD_ITEM_MAX = 300, 8, 200
WHY_MAX = 120
UNFINISHED_MAX, UNFINISHED_N = 200, 5
OBS_N = 8  # 指令要求每批不超过 5 条，这里是硬上限
_KNOWN_KEYS = ("skip", "observations", "summary", "state_card", "memory_conflicts",
               "unfinished_new", "unfinished_resolved")


class ValidationError(Exception):
    pass


@dataclass
class Validated:
    skip: bool = False
    notes: list[dict] = field(default_factory=list)
    summary: dict | None = None          # 四个字段至少一个非空才有
    card: dict | None = None
    conflicts: list[dict] = field(default_factory=list)
    unfinished_new: list[str] = field(default_factory=list)
    unfinished_resolved: list[int] = field(default_factory=list)  # 喂进去的"没做完"列表里的编号（1..n）
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 从文本里取 JSON

def parse_model_text(text: str):
    """先整段 json.loads；不行就去掉 ``` 围栏，再从每个 { 开始试 raw_decode，取第一个解出对象的。"""
    if not text or not text.strip():
        raise ValidationError("模型输出为空")
    s = text.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    i = s.find("{")
    while i >= 0:
        try:
            obj, _ = dec.raw_decode(s, i)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        i = s.find("{", i + 1)
    raise ValidationError("模型输出里没有可解析的 JSON 对象")


# ---------------------------------------------------------------- 字段清洗

def _s(v, cap: int) -> str:
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str):
        return ""
    return v.strip()[:cap]


def _str_list(v, n: int, cap: int) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v:
        s = _s(x, cap)
        if s and s not in out:
            out.append(s)
        if len(out) >= n:
            break
    return out


def _int_list(v, lo: int, hi: int) -> list[int]:
    """只留 lo..hi 范围内的整数（越界的丢弃），去重保序。bool 不算整数。"""
    if not isinstance(v, list):
        return []
    out: list[int] = []
    for x in v:
        if isinstance(x, bool):
            continue
        if isinstance(x, float) and x.is_integer():
            x = int(x)
        if isinstance(x, str) and x.strip().isdigit():
            x = int(x.strip())
        if isinstance(x, int) and lo <= x <= hi and x not in out:
            out.append(x)
    return out


def _fit_card(card: dict, limit_tokens: int) -> dict:
    """现状卡超过 token 上限时，从最长的列表末尾一项项砍，列表砍空了再截字符串。"""
    for _ in range(200):
        if tokens.estimate(json.dumps(card, ensure_ascii=False)) <= limit_tokens:
            break
        lists = [k for k in ("decisions", "pitfalls", "next") if card[k]]
        if lists:
            longest = max(lists, key=lambda k: sum(len(x) for x in card[k]))
            card[longest].pop()
            continue
        k = "status" if len(card["status"]) >= len(card["goal"]) else "goal"
        card[k] = card[k][: max(0, len(card[k]) // 2)]
        if not card["status"] and not card["goal"]:
            break
    return card


def validate(obj, *, n_notes: int, n_unfinished: int, n_events: int, card_tokens: int = 600) -> Validated:
    """n_notes：喂进去的相关笔记条数（supersedes 的合法范围 1..n_notes）；
    n_unfinished：喂进去的没做完条数；n_events：本批事件数（source 的合法范围）。"""
    if not isinstance(obj, dict):
        raise ValidationError("模型输出的顶层不是 JSON 对象")
    if not any(k in obj for k in _KNOWN_KEYS):
        raise ValidationError("模型输出里没有任何已知字段")
    v = Validated()
    v.skip = obj.get("skip") is True

    obs = obj.get("observations", [])
    if obs is not None and not isinstance(obs, list):
        raise ValidationError("observations 不是数组")
    for raw in (obs or [])[:OBS_N]:
        if not isinstance(raw, dict):
            continue
        title = _s(raw.get("title"), TITLE_MAX)
        if not title:
            v.warnings.append("丢弃没有标题的笔记")
            continue
        typ = raw.get("type")
        if typ not in TYPES:
            v.warnings.append(f"非法 type {str(typ)[:20]!r} 改成 change")
            typ = "change"
        sup_raw, src_raw = raw.get("supersedes"), raw.get("source")
        sup = _int_list(sup_raw, 1, n_notes)
        if isinstance(sup_raw, list) and len(sup) < len(sup_raw):
            v.warnings.append("supersedes 有越界编号，已丢弃")
        tags = [t for t in (raw.get("tags") if isinstance(raw.get("tags"), list) else []) if t in TAGS]
        v.notes.append({
            "type": typ,
            "title": title,
            "body": _s(raw.get("body"), BODY_MAX),
            "facts": _str_list(raw.get("facts"), FACTS_N, FACT_MAX),
            "files": _str_list(raw.get("files"), FILES_N, FILE_MAX),
            "tags": list(dict.fromkeys(tags)),
            "slot": _s(raw.get("slot"), SLOT_MAX),
            "supersedes": sup,
            "source": _int_list(src_raw, 1, n_events),
        })

    summ = obj.get("summary")
    if isinstance(summ, dict):
        s = {k: _s(summ.get(k), SUMMARY_MAX) for k in ("request", "completed", "learned", "next_steps")}
        if any(s.values()):
            v.summary = s

    card = obj.get("state_card")
    if isinstance(card, dict):
        c = {
            "goal": _s(card.get("goal"), CARD_STR_MAX),
            "status": _s(card.get("status"), CARD_STR_MAX),
            "decisions": _str_list(card.get("decisions"), CARD_LIST_N, CARD_ITEM_MAX),
            "pitfalls": _str_list(card.get("pitfalls"), CARD_LIST_N, CARD_ITEM_MAX),
            "next": _str_list(card.get("next"), CARD_LIST_N, CARD_ITEM_MAX),
        }
        if c["goal"] or c["status"] or c["decisions"] or c["pitfalls"] or c["next"]:
            v.card = _fit_card(c, card_tokens)

    conf = obj.get("memory_conflicts")
    if isinstance(conf, list):
        for x in conf[:10]:
            if isinstance(x, dict):
                f, why = _s(x.get("file"), 300), _s(x.get("why"), WHY_MAX)
                if f and why:
                    v.conflicts.append({"file": f, "why": why})

    v.unfinished_new = _str_list(obj.get("unfinished_new"), UNFINISHED_N, UNFINISHED_MAX)
    res_raw = obj.get("unfinished_resolved")
    v.unfinished_resolved = _int_list(res_raw, 1, n_unfinished)
    if isinstance(res_raw, list) and len(v.unfinished_resolved) < len(res_raw):
        v.warnings.append("unfinished_resolved 有越界编号，已丢弃")
    return v
