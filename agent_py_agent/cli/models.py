from __future__ import annotations

"""LLM: defines small CLI DTOs for chat jobs and daemon options.

给人看的解释：
这个文件只放命令行运行时用的小数据结构。
聊天后台队列用 ChatJob，daemon/gateway 的调度参数用 DaemonOptions。
"""

from dataclasses import dataclass


@dataclass
class ChatJob:

    user: str
    show_prompt: bool
    inject: list[str]
    prompt_files: list[str]


@dataclass
class DaemonOptions:

    apply: bool
    execute_runners: bool
    planner: bool
    interval: float
    max_runners: int
    limit: int
    max_cycles: int
    max_cards: int
    reviewer: str
    instruction: str
    probe: bool
