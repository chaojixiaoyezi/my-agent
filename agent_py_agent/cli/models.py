
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


@dataclass
class ChatJob:

    user: str
    show_prompt: bool
    inject: list[str]
    prompt_files: list[str]


@dataclass
class DaemonOptions:

    mutate_state: bool
    start_runners: bool
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


@dataclass(frozen=True)
class GatewayRunOptions:

    mutate_state: bool
    start_runners: bool
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
class GatewayStartOptions:

    config: Path
    force_lock: bool


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


@dataclass(frozen=True)
class GatewayThreadsRequest:

    context: GatewayRunContext
    requeued: int
    failed: int
    http_port: int


@dataclass(frozen=True)
class GatewayRunCleanupRequest:

    context: GatewayRunContext
    pid: int
    stop_event: Any
    heartbeat_thread: Any
    request_thread: Any
    background_thread: Any
    http_server: Any | None


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


@dataclass(frozen=True)
class SubagentsDispatchOptions:

    mutate_state: bool
    start_runners: bool
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
    advance: bool
    force_lock: bool
    watch: bool
    run_ids: list[str]
    background_launch_id: str = ""


@dataclass(frozen=True)
class SubagentsDueCheckOptions:

    all: bool
    limit: int
    root_id: str = ""


@dataclass(frozen=True)
class SubagentsPlanActionsOptions:

    all: bool
    limit: int
    root_id: str = ""


@dataclass(frozen=True)
class SubagentsProbeOptions:

    run_ids: list[str] | None
    limit: int


@dataclass(frozen=True)
class SubagentContextOptions:

    run_id: str
    max_cards: int


@dataclass(frozen=True)
class SubagentsCapabilityRouteOptions:

    apply: bool
    run_ids: list[str] | None
    limit: int


@dataclass(frozen=True)
class TimelineOptions:

    limit: int
    source_type: str | None
    event_type: str | None
    json: bool
    details: bool


@dataclass(frozen=True)
class LocalSearchOptions:

    query: str
    limit: int
    source_type: str | None
    visibility: str | None
    preview_chars: int


@dataclass(frozen=True)
class LocalDoctorOptions:

    repair: bool
    limit: int
    json: bool


@dataclass(frozen=True)
class LocalRebuildOptions:

    sources: set[str]
    reset: bool


@dataclass(frozen=True)
class TaskListOptions:

    config: str
    user_id: str | None
    status: str | None
    limit: int


@dataclass(frozen=True)
class TaskIdOptions:

    config: str
    task_id: str


@dataclass(frozen=True)
class TaskSearchOptions:

    config: str
    query: str


@dataclass(frozen=True)
class SubagentsAcceptanceOptions:

    run_ids: list[str] | None
    apply: bool
    reviewer: str | None
    note: str
    limit: int
    execute_tests: bool | None = None
    test_timeout: float | None = None


@dataclass(frozen=True)
class SubagentsPatchOptions:

    action: str
    run_ids: list[str] | None
    reviewer: str | None
    note: str
    limit: int


@dataclass(frozen=True)
class SubagentsTestsOptions:

    run_id: str
    re_run: bool
    timeout: float


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
