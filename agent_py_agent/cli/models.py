from __future__ import annotations

"""LLM: defines small CLI DTOs for chat jobs, daemon options, and CLI request bundles.

给人看的解释：
这个文件只放命令行运行时用的小数据结构。
聊天后台队列用 ChatJob，daemon/gateway 的调度参数用 DaemonOptions。
子代理命令先把 argparse namespace 收成这里的 options，再调用业务层。
"""

from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class SubagentsAcceptanceOptions:

    run_ids: list[str] | None
    apply: bool
    reviewer: str | None
    note: str
    limit: int


@dataclass(frozen=True)
class SubagentsPatchOptions:

    action: str
    run_ids: list[str] | None
    reviewer: str | None
    note: str
    limit: int


@dataclass(frozen=True)
class SubagentsMemoryGateOptions:

    run_id: str
    candidate_id: str
    decision: str
    reviewer: str
    note: str
    memory_path: Path | None
    skill_output_dir: str | None
    limit: int
    retention_dry_run: bool
    retention_apply: bool
    export_memory: bool
    export_skill: bool
    verify: bool
