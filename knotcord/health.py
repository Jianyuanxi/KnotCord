"""提炼告警：给开局注入第一行和 doctor 用。只读 kv 和 batches，不导入 knotcord.extract（hook 进程要轻）。
kv 键和 extract/budget.py 共用：budget 从这里导入，别各写一份。"""

from __future__ import annotations

import json
import time

from knotcord.core import db

CODEX = "codex"
DEEPSEEK = "deepseek"
HARD_REASONS = ("余额不足", "鉴权失败", "没有配置")  # 要用户动手才会好的原因


def day_key(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def usage_get(conn, now: float, day: str | None = None) -> dict:
    raw = db.kv_get(conn, "usage:" + (day or day_key(now)))
    base = {"calls": 0, "codex": 0, "deepseek": 0, "cny": 0.0, "tokens_in": 0, "tokens_out": 0}
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                base.update({k: d[k] for k in base if k in d})
        except ValueError:
            pass
    return base


def cd_key(provider: str) -> str:
    return f"{provider}_cooldown_until"


def unavail_key(provider: str) -> str:
    return f"{provider}_unavailable"


def cooldown_until(conn, provider: str) -> float:
    try:
        return float(db.kv_get(conn, cd_key(provider), "0") or 0)
    except ValueError:
        return 0.0


def unavailable_info(conn, provider: str) -> dict | None:
    """最近一次通道不可用的原因：{reason, since, until}；成功调用一次后清掉。"""
    try:
        raw = db.kv_get(conn, unavail_key(provider), "") or ""
        d = json.loads(raw) if raw else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def is_hard_reason(reason: str) -> bool:
    return any(k in (reason or "") for k in HARD_REASONS)


def _md_hm(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def lines(conn, cfg, now: float | None = None, *, min_dead: int = 1) -> list[str]:
    """提炼出问题时给用户看的告警，每条一行；没问题返回空列表。conn 可以是只读连接。
    - DeepSeek 余额不足 / 鉴权失败 / 没配密钥（要用户处理，不管 Codex 能不能用都报）
    - 两条通道都在冷却（其他原因）
    - 今天的次数或花费到上限
    - 近 24 小时有批次失败作废"""
    now = time.time() if now is None else now
    out: list[str] = []
    try:
        ds = unavailable_info(conn, DEEPSEEK)
        ds_down = cooldown_until(conn, DEEPSEEK) > now
        if ds and is_hard_reason(ds.get("reason", "")):
            out.append(f"KnotCord 提炼告警：{ds['reason']}（{_md_hm(ds.get('since') or now)} 起），"
                       "DeepSeek 提炼已暂停；充值或改好密钥后 1 小时内自动恢复，详情 `boot.py doctor`")
        elif ds_down and cooldown_until(conn, CODEX) > now:
            why = (ds or {}).get("reason") or "冷却中"
            out.append(f"KnotCord 提炼暂停：Codex 和 DeepSeek 都不可用（DeepSeek：{why}，"
                       f"{_md_hm(cooldown_until(conn, DEEPSEEK))} 再试）")
        u = usage_get(conn, now)
        cap_calls, cap_cny = int(cfg.get("budget.daily_calls")), float(cfg.get("budget.daily_cny"))
        if u["calls"] >= cap_calls or u["cny"] >= cap_cny:
            out.append(f"KnotCord 今天的提炼已到上限（{u['calls']} 次 / ¥{u['cny']:.2f}，上限 {cap_calls} 次 / "
                       f"¥{cap_cny:g}），剩下的明天再提炼")
        since = int(now - 86400)
        n = conn.execute("SELECT count(*) FROM batches WHERE status='dead' AND created>=?", (since,)).fetchone()[0]
        if n and n >= min_dead:
            last =conn.execute("SELECT error FROM batches WHERE status='dead' AND created>=? ORDER BY id DESC "
                                "LIMIT 1", (since,)).fetchone()
            out.append(f"KnotCord 近 24 小时有 {n} 批提炼失败作废（最近一次：{(last[0] or '')[:80]}），"
                       "日志在 ~/.knotcord/logs")
    except Exception:
        return out
    return out
