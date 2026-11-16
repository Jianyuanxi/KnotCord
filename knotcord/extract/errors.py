"""提炼模块共用的结果类型和异常。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelResult:
    text: str
    provider: str  # "codex" / "deepseek"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cny: float = 0.0
    model: str = ""  # 具体模型名，写进 notes.model


class ChannelUnavailable(Exception):
    """通道不可用：额度、限流、余额、鉴权、没装、没配密钥。不算这批的失败次数。
    retry_at 是能恢复的 epoch 秒，解析不出来就是 None。"""

    def __init__(self, retry_at: int | None = None, reason: str = "") -> None:
        super().__init__(reason)
        self.retry_at = retry_at
        self.reason = reason


class ModelError(Exception):
    """其他失败（超时、网络、空输出）。调用已经发出，仍计入当天次数；
    tokens/cost 是已知的实际用量（超时被杀时为 0）。"""

    def __init__(self, message: str = "", *, tokens_in: int = 0, tokens_out: int = 0,
                 cost_cny: float = 0.0) -> None:
        super().__init__(message)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_cny = cost_cny


class CallAborted(Exception):
    """stop_event 置位，调用被中止。"""
