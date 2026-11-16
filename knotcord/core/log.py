"""日志：logs/knotcord-YYYY-MM-DD.log，UTF-8，单行截 2000 字，连续重复去重（细则 6.4）。绝不写 stdout。"""

from __future__ import annotations

import logging
import os
import time

from . import paths

MAX_LINE = 2000
REPEAT_EVERY_S = 600  # 同一条消息连续重复时，每 10 分钟写一次"又出现 N 次"


def _now() -> float:  # 单独成函数，测试里可替换
    return time.time()


class _DedupeFileHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._last_key: tuple | None = None
        self._last_text = ""
        self._suppressed = 0
        self._last_written = 0.0

    def _write(self, ts: float, level: str, name: str, msg: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        msg = msg.replace("\r\n", " | ").replace("\n", " | ").replace("\r", " | ")
        line = f"{stamp} {os.getpid()} {level} {name}: {msg}"[:MAX_LINE]
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        path = paths.logs_dir() / f"knotcord-{day}.log"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += " " + logging.Formatter().formatException(record.exc_info)
            now = _now()
            key = (record.levelname, record.name, msg)
            if key == self._last_key:
                self._suppressed += 1
                if now - self._last_written >= REPEAT_EVERY_S:
                    self._write(now, record.levelname, record.name,
                                f"又出现 {self._suppressed} 次: {self._last_text}")
                    self._suppressed = 0
                    self._last_written = now
                return
            if self._suppressed:  # 被别的消息打断，把欠的计数补上
                lv, nm, _ = self._last_key  # type: ignore[misc]
                self._write(now, lv, nm, f"又出现 {self._suppressed} 次: {self._last_text}")
            self._last_key = key
            self._last_text = msg
            self._suppressed = 0
            self._last_written = now
            self._write(now, record.levelname, record.name, msg)
        except Exception:
            pass  # 日志永远不能让主流程出错

    def reset(self) -> None:
        self._last_key = None
        self._last_text = ""
        self._suppressed = 0
        self._last_written = 0.0


_BASE = "knotcord"
_handler: _DedupeFileHandler | None = None


def _ensure_base() -> logging.Logger:
    global _handler
    base = logging.getLogger(_BASE)
    if _handler is None:
        _handler = _DedupeFileHandler()
        base.addHandler(_handler)
        base.setLevel(logging.INFO)
        base.propagate = False  # 不往 root 走，更不会到 stdout
    return base


def get_logger(name: str = "knotcord") -> logging.Logger:
    base = _ensure_base()
    if name == _BASE:
        return base
    return logging.getLogger(name if name.startswith(_BASE + ".") else f"{_BASE}.{name}")


def _reset_for_tests() -> None:
    _ensure_base()
    assert _handler is not None
    _handler.reset()
