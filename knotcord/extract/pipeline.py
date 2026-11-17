"""提炼流水线：该提炼哪些会话（due_sessions）、提炼一个会话（run_session）。

一批的生命周期（细则 2.3）：
  选事件 → batches 插/认领一行 running → 组 prompt（再脱敏）→ 调模型（不在事务里，等待时续租约）
  → 先分类失败再校验 → 一个 write_tx 落库（第一句确认租约）。
失败：attempts+1，next_try 依次 10 分钟、1 小时；第 3 次失败标 dead，并给这批事件打上批号（走零模型兜底）。
通道不可用（额度、限流、余额、鉴权）不算这批的失败次数。
"""

from __future__ import annotations

import os
import shutil
import time

from knotcord.core import db, log, paths
from knotcord.core.lease import LeaseLost

from . import backlog, budget, codex, commit, deepseek, prompt, validate
from .errors import CallAborted, ChannelUnavailable, ModelError

_log = log.get_logger("extract")
_RETRY_DELAYS = (600, 3600)  # 第 1、2 次失败后的等待；超出的次数沿用最后一个


def _now() -> float:  # 测试里可替换
    return time.time()


# ---------------------------------------------------------------- 阈值和是否该做

def _provider_for_thresholds(conn, now: float) -> str:
    """当前通道：Codex 不在冷却期就是 codex，冷却期内按 deepseek 的阈值。"""
    return budget.CODEX if budget.channel_available(conn, budget.CODEX, now) else budget.DEEPSEEK


def _thresholds(cfg, provider: str) -> tuple[int, int]:
    if provider == budget.DEEPSEEK:
        return int(cfg.get("extract.chars_deepseek")), int(cfg.get("extract.turns_deepseek"))
    return int(cfg.get("extract.chars_codex")), int(cfg.get("extract.turns_codex"))


def _can_work(conn, cfg, now: float) -> bool:
    """预算没用完、没熔断、至少有一条通道不在冷却期。"""
    if not budget.can_call(conn, cfg, now) or budget.breaker_open(now):
        return False
    return (budget.channel_available(conn, budget.CODEX, now)
            or budget.channel_available(conn, budget.DEEPSEEK, now))


def _meets_threshold(stats: dict, last_seen: int, cfg, provider: str, now: float) -> bool:
    chars_t, turns_t = _thresholds(cfg, provider)
    if stats["chars"] >= chars_t or stats["turns"] >= turns_t:
        return True
    idle_s = int(cfg.get("extract.idle_minutes")) * 60
    return now - max(stats["last_ts"], last_seen) >= idle_s  # 有未提炼内容且空闲够久


def _last_seen(conn, session_id: str) -> int:
    r = conn.execute("SELECT last_seen FROM sessions WHERE id=?", (session_id,)).fetchone()
    return backlog.sec(r["last_seen"]) if r else 0


def due_sessions(conn, cfg, *, now: int | None = None) -> list[str]:
    """现在该提炼的会话 id：未提炼内容达到字数阈值，或满 N 个用户回合，或空闲超过 extract.idle_minutes 且有
    未提炼内容，或有 failed 批到了 next_try。阈值按当前通道取。预算用完、熔断、两条通道都在冷却期时返回空。"""
    now = _now() if now is None else now
    if not _can_work(conn, cfg, now):
        return []
    stats = backlog.unbatched_stats(conn)
    if not stats:
        return []
    provider = _provider_for_thresholds(conn, now)
    retry = backlog.retry_rows(conn)
    seen = {r["id"]: backlog.sec(r["last_seen"]) for r in conn.execute("SELECT id, last_seen FROM sessions")}
    out: list[tuple[int, str]] = []
    for sid, s in stats.items():
        state = backlog.retry_state(retry.get(sid), now)
        if state == "wait":
            continue  # 还在退避期，哪怕字数够了也不碰
        if state == "due" or _meets_threshold(s, seen.get(sid, 0), cfg, provider, now):
            out.append((s["last_ts"], sid))
    return [sid for _, sid in sorted(out)]


# ---------------------------------------------------------------- 选事件、批行

def _session_project(conn, session_id: str) -> str:
    r = conn.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
    if r and r["project"]:
        return r["project"]
    r = conn.execute("SELECT project FROM events WHERE session_id=? AND project IS NOT NULL ORDER BY id LIMIT 1",
                     (session_id,)).fetchone()
    return (r["project"] if r else "") or ""


def _cut(rows, max_chars: int, lean_on: bool = False) -> list:
    """从游标里取一批：不超过 max_chars，超了就切在最近的回合边界（用户提示条）；
    一个回合就超长又没有边界可切时，在当前位置硬切。至少取一条。"""
    batch: list = []
    total = 0
    boundary: int | None = None
    for r in rows:
        n = prompt.event_len(r, lean_on)
        if batch and total + n > max_chars:
            # 当前这条本身就是用户提示 = 已经在回合边界上，直接切；否则退回到最近的回合边界
            if r["kind"] != "prompt" and boundary is not None:
                batch = batch[:boundary]
            break
        if r["kind"] == "prompt" and batch:
            boundary = len(batch)
        batch.append(r)
        total += n
    return batch


def _claim(conn, session_id: str, chars_in: int, now: float) -> tuple[int, int]:
    """认领这个会话已有的 failed/running 行继续重试，没有就插一行新的。返回 (批号, 已失败次数)。
    running 行在持有租约的进程看来一定是陈旧的（上一个进程死在调模型途中），算一次失败。"""
    with db.write_tx(conn):
        rows = conn.execute("SELECT id, status, attempts FROM batches WHERE session_id=? "
                            "AND status IN ('failed','running') ORDER BY id", (session_id,)).fetchall()
        if rows:
            keep = rows[0]
            for extra in rows[1:]:
                conn.execute("UPDATE batches SET status='dead', error='重复的批行' WHERE id=?", (extra["id"],))
            attempts = (keep["attempts"] or 0) + (1 if keep["status"] == "running" else 0)
            if keep["status"] == "running":
                _log.warning("批 %s 上次停在调模型途中（进程被杀或崩溃），记一次失败后重试", keep["id"])
            conn.execute("UPDATE batches SET status='running', attempts=?, chars_in=? WHERE id=?",
                         (attempts, chars_in, keep["id"]))
            return keep["id"], attempts
        cur = conn.execute("INSERT INTO batches(session_id, created, status, chars_in, attempts) "
                           "VALUES(?,?, 'running', ?, 0)", (session_id, int(now), chars_in))
        return cur.lastrowid, 0


def _release(conn, batch_id: int, attempts: int) -> None:
    """放弃这批但不算失败（通道不可用、被中止、租约丢失）：新插的行直接删，重试行回到 failed。"""
    try:
        with db.write_tx(conn):
            if attempts == 0:
                conn.execute("DELETE FROM batches WHERE id=? AND status='running'", (batch_id,))
            else:
                conn.execute("UPDATE batches SET status='failed' WHERE id=? AND status='running'", (batch_id,))
    except Exception as e:
        _log.warning("释放批 %s 失败: %s", batch_id, type(e).__name__)


FAILED_KEEP = 30          # logs/failed 里最多留几份
FAILED_MAX_CHARS = 50_000


def _save_failed_output(batch_id: int, attempt: int, provider: str, reason: str, text: str) -> str:
    """校验没过的模型原始输出存到 logs/failed/b<批>-a<次>.txt，只留最新 FAILED_KEEP 份。返回文件名，失败返回空串。
    输入在发出前已脱敏，模型输出只会复述输入，所以落盘不再脱敏。"""
    try:
        d = paths.logs_dir() / "failed"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"b{batch_id}-a{attempt}.txt"
        p.write_text(f"provider: {provider}\nreason: {reason}\n\n{(text or '')[:FAILED_MAX_CHARS]}", encoding="utf-8")
        for old in sorted(d.glob("*.txt"), key=lambda x: (x.stat().st_mtime, x.name))[:-FAILED_KEEP]:
            old.unlink(missing_ok=True)
        return p.name
    except OSError as e:
        _log.warning("保存失败批的原始输出出错: %s", e)
        return ""


def _fail(conn, cfg, batch_id: int, attempts: int, event_ids: list[int], reason: str, provider: str | None,
          now: float) -> None:
    n = attempts + 1
    reason = reason[:300]
    with db.write_tx(conn):
        if n >= int(cfg.get("extract.retries")):
            conn.execute("UPDATE batches SET status='dead', attempts=?, error=?, provider=?, next_try=NULL "
                         "WHERE id=?", (n, reason, provider, batch_id))
            commit.stamp_events(conn, batch_id, event_ids)  # 这批事件走零模型兜底，不再花钱
            _log.warning("批 %s 第 %s 次失败，标 dead: %s", batch_id, n, reason)
        else:
            delay = _RETRY_DELAYS[min(n - 1, len(_RETRY_DELAYS) - 1)]
            conn.execute("UPDATE batches SET status='failed', attempts=?, error=?, provider=?, next_try=? "
                         "WHERE id=?", (n, reason, provider, int(now + delay), batch_id))
            _log.warning("批 %s 第 %s 次失败，%s 秒后重试: %s", batch_id, n, delay, reason)


# ---------------------------------------------------------------- 一批

def _one_batch(conn, sid: str, cfg, lease, stop_event, now: float) -> str:
    """做一批。返回 done / failed / unavailable / aborted / lost / empty。"""
    project = _session_project(conn, sid)
    cur = conn.execute("SELECT id, ts, kind, tool, brief, body FROM events WHERE session_id=? "
                       "AND batch_id IS NULL AND coalesce(origin,'knotcord')='knotcord' ORDER BY id", (sid,))
    try:
        events = _cut(cur, int(cfg.get("extract.max_chars")), bool(cfg.get("extract.lean", True)))
    finally:
        cur.close()
    if not events:
        return "empty"
    text, ctx = prompt.build(conn, cfg, sid, project, events, now=now, max_chars=int(cfg.get("extract.max_chars")))
    batch_id, attempts = _claim(conn, sid, len(text), now)
    _log.info("批 %s 开始: session=%s events=%d chars=%d 第 %d 次", batch_id, sid[:8], len(events), len(text),
              attempts + 1)
    tmp =paths.tmp_dir() / f"b{batch_id}-{os.getpid()}"

    def on_tick() -> None:
        if stop_event is not None and stop_event.is_set():
            raise CallAborted()
        if not lease.renew():
            raise LeaseLost("续租失败，租约已被别人接手")

    try:
        tmp.mkdir(parents=True, exist_ok=True)
        result = None
        for provider in (budget.CODEX, budget.DEEPSEEK):
            if not budget.channel_available(conn, provider, _now()):
                continue
            mod = codex if provider == budget.CODEX else deepseek
            try:
                result = mod.call(text, cfg, tmp_dir=str(tmp), on_tick=on_tick, stop_event=stop_event)
            except ChannelUnavailable as e:
                until = budget.mark_unavailable(conn, provider, e.retry_at, _now(), reason=e.reason)
                budget.breaker_note(False, _now())
                _log.warning("%s 通道不可用，冷却到 %s: %s", provider, time.strftime("%m-%d %H:%M", time.localtime(until)),
                             e.reason[:200])
                continue  # 当场切到另一条通道重做这一批
            except (CallAborted, LeaseLost):
                raise
            except ModelError as e:
                budget.record(conn, provider, tokens_in=e.tokens_in, tokens_out=e.tokens_out,
                              cost_cny=e.cost_cny, now=_now())  # 调用已发出，照样计入次数
                budget.breaker_note(False, _now())
                _fail(conn, cfg, batch_id, attempts, ctx.event_ids, f"{provider}: {e}", provider, _now())
                return "failed"
            except Exception as e:  # 通道代码自己的 bug：当作一次失败，不能让它拖垮整个 worker
                budget.record(conn, provider, now=_now())
                budget.breaker_note(False, _now())
                _fail(conn, cfg, batch_id, attempts, ctx.event_ids,
                      f"{provider}: 意外异常 {type(e).__name__}: {str(e)[:150]}", provider, _now())
                return "failed"
            budget.record(conn, provider, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                          cost_cny=result.cost_cny, now=_now())
            budget.mark_ok(conn, provider)
            break
        if result is None:
            _release(conn, batch_id, attempts)
            return "unavailable"

        # 先分类失败（上面已处理）再校验
        try:
            obj = validate.parse_model_text(result.text)
            v = validate.validate(obj, n_notes=len(ctx.notes), n_unfinished=len(ctx.unfinished),
                                  n_events=len(ctx.event_ids), card_tokens=int(cfg.get("inject.card_tokens")))
        except validate.ValidationError as e:
            budget.breaker_note(False, _now())
            saved = _save_failed_output(batch_id, attempts + 1, result.provider, str(e), result.text)
            _fail(conn, cfg, batch_id, attempts, ctx.event_ids,
                  f"{result.provider}: 输出校验失败: {e}" + (f"（原始输出 {saved}）" if saved else ""),
                  result.provider, _now())
            return "failed"
        for w in v.warnings:
            _log.info("批 %s 校验修正: %s", batch_id, w)

        try:
            stats = commit.commit_batch(
                conn, lease, batch_id=batch_id, session_id=sid, project=project, v=v, ctx=ctx,
                provider=result.provider, model=result.model, tokens_in=result.tokens_in,
                tokens_out=result.tokens_out, cost_cny=result.cost_cny, now=_now())
        except LeaseLost:
            raise
        except Exception as e:
            budget.breaker_note(False, _now())
            _log.error("批 %s 落库失败", batch_id, exc_info=True)  # 带调用栈，方便排查
            _fail(conn, cfg, batch_id, attempts, ctx.event_ids, f"落库失败 {type(e).__name__}: {str(e)[:150]}",
                  result.provider, _now())
            return "failed"
        budget.breaker_note(True, _now())
        _log.info("批 %s 完成: session=%s events=%d chars=%d provider=%s tokens=%d/%d cny=%.4f notes=%d card=%s",
                  batch_id, sid, len(ctx.event_ids), len(text), result.provider, result.tokens_in,
                  result.tokens_out, result.cost_cny, stats["notes"], stats["card"])
        return "done"
    except CallAborted:
        _release(conn, batch_id, attempts)
        return "aborted"
    except LeaseLost as e:
        _release(conn, batch_id, attempts)
        _log.warning("租约丢失，放弃批 %s: %s", batch_id, e)
        return "lost"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_session(conn, session_id: str, cfg, lease, *, flush: bool = False, stop_event=None) -> int:
    """提炼这个会话：flush=True 时把剩下的全部提炼掉（PreCompact、收尾用），否则只在满足阈值时做。
    一批最多 extract.max_chars 字，在回合边界切开，可能连做多批。返回完成的批数。
    预算用完、两条通道都不可用、熔断、stop_event 置位、租约丢失时停下并返回，未提炼的留着。
    还在退避期的 failed 批不会被强行重试（flush 也一样），留给 next_try 之后的下一轮。"""
    done = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        now = _now()
        if not _can_work(conn, cfg, now):
            break
        stats = backlog.unbatched_stats(conn, session_id).get(session_id)
        if not stats:
            break
        state = backlog.retry_state(backlog.retry_rows(conn).get(session_id), now)
        if state == "wait":
            break
        if not flush and state != "due":
            provider = _provider_for_thresholds(conn, now)
            if not _meets_threshold(stats, _last_seen(conn, session_id), cfg, provider, now):
                break
        outcome = _one_batch(conn, session_id, cfg, lease, stop_event, now)
        if outcome != "done":
            break
        done += 1
    return done
