# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""defines small CLI DTOs for chat jobs, daemon options, and CLI request bundles.

给人看的解释：
这个文件只放命令行运行时用的小数据结构。
聊天后台队列用 ChatJob，daemon/gateway 的调度参数用 DaemonOptions。
子代理命令先把 argparse namespace 收成这里的 options，再调用业务层。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: ChatJob 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class ChatJob:

    user: str
    show_prompt: bool
    inject: list[str]
    prompt_files: list[str]


# LLM: DaemonOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
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
    note: str
    take_over_by: str
    locked_files: list[str]
    force_lock: bool


# LLM: GatewayRunOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayRunOptions:

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


# LLM: GatewayStartOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayStartOptions:

    config: Path
    force_lock: bool


# LLM: GatewayRunContext 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass(frozen=True)
class GatewayRunContext:

    agent: Any
    paths: Any
    options: GatewayRunOptions
    config_path: Path
    note: str
    take_over_by: str
    locked_files: list[str]
    force_lock: bool
    router: Any | None = None
    capability_config: Any | None = None


# LLM: GatewayThreadsRequest 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayThreadsRequest:

    context: GatewayRunContext
    requeued: int
    failed: int
    http_port: int


# LLM: GatewayRunCleanupRequest 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayRunCleanupRequest:

    context: GatewayRunContext
    pid: int
    stop_event: Any
    heartbeat_thread: Any
    request_thread: Any
    http_server: Any | None


# LLM: AdapterOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class AdapterOptions:

    root: Path | None
    inbox: Path | None
    outbox: Path | None
    timeout: float | None
    limit: int
    once: bool
    watch: bool
    poll_interval: float
    no_start_gateway: bool
    channel: str
    pid_file: Path | None
    daemon: bool
    stop_timeout: float


# LLM: SubagentsDispatchOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentsDispatchOptions:

    apply: bool
    execute_runners: bool
    execute_acceptance_tests: bool
    planner: bool
    workflow_mode: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    instruction: str
    max_cards: int
    probe: bool
    take_over_by: str
    locked_files: list[str]
    interval: float
    max_cycles: int
    force_lock: bool
    watch: bool


# LLM: SubagentsDueCheckOptions keeps display limit and root scope together for due-check CLI.
# 类用途: 保存 due-check 展示参数；root_id 用来只看一棵 subagent 任务树。
@dataclass(frozen=True)
class SubagentsDueCheckOptions:

    all: bool
    limit: int
    root_id: str = ""


# LLM: SubagentsPlanActionsOptions keeps action-plan display and root scope as one CLI bundle.
# 类用途: 保存 subagents-plan-actions 展示参数；root_id 用来只为指定任务树生成动作建议。
@dataclass(frozen=True)
class SubagentsPlanActionsOptions:

    all: bool
    limit: int
    root_id: str = ""


# LLM: SubagentsProbeOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentsProbeOptions:

    run_ids: list[str] | None
    limit: int


# LLM: SubagentContextOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentContextOptions:

    run_id: str
    max_cards: int


# LLM: SubagentsCapabilityRouteOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentsCapabilityRouteOptions:

    apply: bool
    run_ids: list[str] | None
    limit: int


# LLM: TimelineOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class TimelineOptions:

    limit: int
    source_type: str | None
    event_type: str | None
    json: bool
    details: bool


# LLM: LocalSearchOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class LocalSearchOptions:

    query: str
    limit: int
    source_type: str | None
    visibility: str | None
    preview_chars: int


# LLM: LocalDoctorOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class LocalDoctorOptions:

    repair: bool
    limit: int
    json: bool


# LLM: LocalRebuildOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class LocalRebuildOptions:

    sources: set[str]
    reset: bool


# LLM: TaskListOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class TaskListOptions:

    config: str
    user_id: str | None
    status: str | None
    limit: int


# LLM: TaskIdOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class TaskIdOptions:

    config: str
    task_id: str


# LLM: TaskSearchOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class TaskSearchOptions:

    config: str
    query: str


# LLM: SubagentsAcceptanceOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentsAcceptanceOptions:

    run_ids: list[str] | None
    apply: bool
    reviewer: str | None
    note: str
    limit: int
    execute_tests: bool | None = None
    test_timeout: float | None = None


# LLM: SubagentsPatchOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class SubagentsPatchOptions:

    action: str
    run_ids: list[str] | None
    reviewer: str | None
    note: str
    limit: int


# LLM: SubagentsTestsOptions keeps the test-report CLI request explicit and small.
# 类用途: 保存 subagents-tests 的查看/重跑参数，避免命令层散传 argparse 字段。
@dataclass(frozen=True)
class SubagentsTestsOptions:

    run_id: str
    re_run: bool
    timeout: float


# LLM: SubagentsMemoryGateOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
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
