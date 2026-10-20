# LLM: background_shutdown默认对象仅运输同代原supervisor/片观察，无I/O、任务状态或执行权限。
# LLM: CLI DTO 只表达边界输入，后台身份与展示备注分离，修改须核对解析和业务适配。
#   可选C/control/drain引用只运输同代原对象，默认Count0；字段和config property自身不授ready、准入或执行权。
# 模块用途: 保存命令行请求的具名字段，方便发现遗漏或错误运输。
from __future__ import annotations

"""defines small CLI DTOs for chat jobs, daemon options, and CLI request bundles.

给人看的解释：
这个文件只放命令行运行时用的小数据结构。
聊天后台队列用 ChatJob，daemon/gateway 的调度参数用 DaemonOptions。
子代理命令先把 argparse namespace 收成这里的 options，再调用业务层。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.conversation.background_shutdown import BackgroundShutdown


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
class GatewayStartOptions:

    config: Path
    workspace_root: str = ""


# LLM: This immutable value follows one exact Gateway process generation from setup through
# cleanup. Identity/start time scope stop markers; the log byte offset scopes diagnostics.
#   数量由公开YAML冻结，默认0；直接private字段只供原内部QA；安全重启仍拒绝未经实现的MP接班。
# LLM: P6可选runtime只持本代原C资源引用，None保持Count0；不从DTO字段授信，原collector核同control/lease。
#   background_shutdown只持原进程内片观察，不成为任务状态/执行许可，cleanup必须核原future和claim。
# 类用途: 保存一次Gateway身份、配置和可选原资源观察，避免清理丢掉实际责任。
@dataclass(frozen=True)
class GatewayRunContext:

    agent: Any
    paths: Any
    config_path: Path
    log_start_offset_bytes: int = 0
    process_identity: dict[str, object] = field(default_factory=dict)
    process_started_at: float = 0.0
    coordinator_worker_count: int = 0
    control_plane: Any = None
    drain_runtime: Any = None
    background_shutdown: BackgroundShutdown = field(default_factory=BackgroundShutdown)

    # LLM: 内部C独立保存配置，agent可以未获P2许可；默认单进程仍直接消费实际agent配置。
    # 函数用途: 返回本次启动的可信配置，不借假Agent填充接口。
    @property
    def config(self):
        return self.control_plane.config if self.control_plane is not None else self.agent.config



# LLM: 只运输原context、HTTP与停止事件；None保持Count0，不从DTO签业务或设施出生权。
# 类用途: 保存启动循环所需的原资源，协调者可复用已经绑定的HTTP实例。
@dataclass(frozen=True)
class GatewayThreadsRequest:

    context: GatewayRunContext
    requeued: int
    failed: int
    http_port: int
    http_server: Any | None = None
    stop_event: Any | None = None


# LLM: 可选workers只保本代真实组合对象，默认None；停止不得从列表、PID或空快照重建退出证明。
# 类用途: 将服务的原线程、HTTP与worker引用交给唯一收尾入口。
@dataclass(frozen=True)
class GatewayRunCleanupRequest:

    context: GatewayRunContext
    pid: int
    stop_event: Any
    heartbeat_thread: Any
    request_thread: Any
    background_thread: Any
    http_server: Any | None
    termination_status: str = "stopped"
    termination_reason: str = ""
    coordinator_workers: Any | None = None


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


# LLM: expected_attempt_ids 运输两种模式的非空原身份，运行模式与最终接纳仍由宿主验证。
# 类用途: 将一次派工的命令行选项收成具名字段，保持排队身份不丢失。
@dataclass(frozen=True)
class SubagentsDispatchOptions:

    mutate_state: bool
    start_runners: bool
    planner: bool
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
    expected_attempt_ids: dict[str, str] | None = None


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
