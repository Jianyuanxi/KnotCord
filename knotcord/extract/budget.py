"""每日上限、记账、通道冷却、熔断。

- 当天用量存在 kv 的 usage:YYYY-MM-DD（JSON），每次调用后在一个 BEGIN IMMEDIATE 事务里累加。
  超时被杀的调用也走 record（cost 0），所以照样计入当天次数。
- 通道冷却只是 kv 里的一个到期时间，每次用时现比（细则 6.4），没有需要"恢复"的状态。
- 熔断（细则 6.4）：同一进程里连续 5 次失败就停，6 小时后或进程重启才再试。状态只在内存里。
"""

from __future__ import annotations

import json
import time

from knotcord.core import db

from . import backlog

CODEX = "codex"
DEEPSEEK = "deepseek"

# Codex 报错里解析不出恢复时间时，按 1、6、24 小时逐级加长（级数存 kv，成功一次清零）
_LADDER_S = (3600, 6 * 3600, 24 * 3600)
_DEEPSEEK_DEFAULT_COOLDOWN_S = 600  # DeepSeek 没给恢复时间时先歇 10 分钟，别每 30 秒敲一次

BREAKER_FAILS = 5
BREAKER_PAUSE_S = 6 * 3600
_breaker = {"fails": 0, "open_until": 0.0}


def _now() -> float:  # 测试里可替换
    return time.time()


# ---------------------------------------------------------------- 当天用量

def day_key(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def usage_get(conn, now: float | None = None, day: str | None = None) -> dict:
    now = _now() if now is None else now
    key = "usage:" + (day or day_key(now))
    raw = db.kv_get(conn, key)
    base = {"calls": 0, "codex": 0, "deepseek": 0, "cny": 0.0, "tokens_in": 0, "tokens_out": 0}
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                base.update({k: d[k] for k in base if k in d})
        except ValueError:
            pass
    return base


def record(conn, provider: str, *, tokens_in: int = 0, tokens_out: int = 0, cost_cny: float = 0.0,
           now: float | None = None) -> None:
    """记一次调用（成功、失败、超时都记）。"""
    now = _now() if now is None else now
    key = "usage:" + day_key(now)
    with db.write_tx(conn):
        u = usage_get(conn, now)
        u["calls"] += 1
        u[provider] = u.get(provider, 0) + 1
        u["cny"] = round(u["cny"] + float(cost_cny or 0.0), 6)
        u["tokens_in"] += int(tokens_in or 0)
        u["tokens_out"] += int(tokens_out or 0)
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(u)))


def can_call(conn, cfg, now: float | None = None) -> bool:
    """今天的次数和花费都没到上限。"""
    u = usage_get(conn, now)
    return u["calls"] < int(cfg.get("budget.daily_calls")) and u["cny"] < float(cfg.get("budget.daily_cny"))


def usage_line(conn, cfg, now: float | None = None) -> str:
    """例：11-14 提炼 23 次（codex 21 / deepseek 2）· ¥0.08 / 上限 ¥3 · 次数上限 150 · 积压 0 批"""
    now = _now() if now is None else now
    u = usage_get(conn, now)
    md = time.strftime("%m-%d", time.localtime(now))
    return (f"{md} 提炼 {u['calls']} 次（codex {u['codex']} / deepseek {u['deepseek']}）"
            f" · ¥{u['cny']:.2f} / 上限 ¥{float(cfg.get('budget.daily_cny')):g}"
            f" · 次数上限 {int(cfg.get('budget.daily_calls'))}"
            f" · 积压 {backlog.backlog_batches(conn, cfg)} 批")


# ---------------------------------------------------------------- 通道冷却

def _cd_key(provider: str) -> str:
    return f"{provider}_cooldown_until"


def cooldown_until(conn, provider: str) -> float:
    try:
        return float(db.kv_get(conn, _cd_key(provider), "0") or 0)
    except ValueError:
        return 0.0


def channel_available(conn, provider: str, now: float | None = None) -> bool:
    now = _now() if now is None else now
    return cooldown_until(conn, provider) <= now


def mark_unavailable(conn, provider: str, retry_at: float | None, now: float | None = None) -> float:
    """设冷却，返回到期时间。retry_at 有效（在未来）就用它，否则 Codex 按 1/6/24 小时逐级加长。"""
    now = _now() if now is None else now
    if retry_at and retry_at > now:
        until = float(retry_at)
    elif provider == CODEX:
        try:
            level = int(db.kv_get(conn, "codex_cooldown_level", "0") or 0)
        except ValueError:
            level = 0
        until = now + _LADDER_S[min(level, len(_LADDER_S) - 1)]
        db.kv_set(conn, "codex_cooldown_level", level + 1)
    else:
        until = now + _DEEPSEEK_DEFAULT_COOLDOWN_S
    db.kv_set(conn, _cd_key(provider), int(until))
    return until


def mark_ok(conn, provider: str) -> None:
    """通道调用成功：Codex 的逐级加长清零。"""
    if provider == CODEX and db.kv_get(conn, "codex_cooldown_level", "0") != "0":
        db.kv_set(conn, "codex_cooldown_level", 0)


# ---------------------------------------------------------------- 熔断

def breaker_open(now: float | None = None) -> bool:
    now = _now() if now is None else now
    if _breaker["open_until"] and now >= _breaker["open_until"]:
        _breaker["open_until"] = 0.0
        _breaker["fails"] = 0
    return bool(_breaker["open_until"])


def breaker_note(ok: bool, now: float | None = None) -> None:
    now = _now() if now is None else now
    if ok:
        _breaker["fails"] = 0
        return
    _breaker["fails"] += 1
    if _breaker["fails"] >= BREAKER_FAILS and not _breaker["open_until"]:
        _breaker["open_until"] = now + BREAKER_PAUSE_S


def reset_breaker() -> None:
    _breaker["fails"] = 0
    _breaker["open_until"] = 0.0
