"""批量提炼：把攒下的操作记录成批交给模型，产出笔记、会话总结、项目现状卡，并落库记账。

入口：pipeline.due_sessions / pipeline.run_session；每日上限和记账在 budget。
只许 import 标准库和 knotcord.core；knotcord.inject 在 commit 里延迟 import。
"""

from .errors import CallAborted, ChannelUnavailable, ModelError, ModelResult  # noqa: F401
