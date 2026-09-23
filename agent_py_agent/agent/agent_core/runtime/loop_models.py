# LLM: 运行参数只携带当前身份、上下文与宿主回调；本片展示及已评估事实不进序列化结果，新工作片须重置。
# 模块用途: 定义主链执行与收口参数；拒绝记忆和可选展示分别沿原宿主生命周期传递。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...capability.decision_recommendation import CapabilityPresentationSelection


# LLM: 消息/运行/尝试身份独立；展示和已应用Compact视图只由同片宿主传入，不能作为持久权限或状态。
# 类用途: 收拢一次代理调用的配置、上下文和内存回调，不执行模型、工具或写文件。
@dataclass
class RunParams:
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    # Host-generated identity for one concrete invocation of a stable run.
    # It is intentionally separate from request_id because scheduled work can
    # retry the same durable request in a fresh execution attempt.
    attempt_id: str = ""
    run_id: str = ""
    task_id: str = ""
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    source: str = "run"
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None
    compact_auto_continue_depth: int = 0
    compact_auto_no_tool_continue_depth: int = 0
    context_scope: str = "default"
    root_user_prompt: str = ""
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] | None = None
    # 宿主在同一活动回合或子代理 attempt 内共享；不从 task_attributes、提示词或模型历史恢复批准。
    runtime_rejected_actions: list[dict[str, str]] = field(default_factory=list)
    # Gateway injects one exact-turn transition callback. Runtime invokes it at
    # reservation and provider-submission edges; generic/local runs leave it empty.
    active_turn_transition_callback: object = None
    # Gateway/direct 宿主回调发布本消息实际选择的 task/run/attempt；不是 prompt 或持久任务属性，
    # 沿 dataclasses.replace 续轮保留，发布失败必须在模型/工具开始前收口；callable 接口仍确认原 Store 的任务晋升。
    conversation_task_binding_callback: object = None
    # The conversation layer supplies one immutable, already-bounded completed transcript seed.
    # Native protocol maps it to provider messages; text protocol renders it once.
    conversation_history_seed: object = None
    # 本次实际采用的摘要范围和视图只在运行参数链中传递，不写入任务或线程持久态。
    compact_context: object | None = None
    # 宿主绑定本次精确 owner/thread/turn 的异常历史出口；只保存已有事实，不投递回复或续跑。
    partial_turn_callback: object = None
    capability_presentation: CapabilityPresentationSelection | None = None
    capability_presentation_evaluated: bool = False
    capability_presentation_turn_id: str = ""
    capability_presentation_callback: Callable[[CapabilityPresentationSelection | None], object] | None = field(
        default=None, repr=False, compare=False,
    )
    # CLI 自动续跑契约(2026-08-14 根因3 设计 v2): 首轮创建后贯穿所有续跑轮,
    # 保证同一 task/run/thread 链路(不每轮隐式生成新根)。
    # - continuation_seq: 0=首轮, 1..N=续跑轮(事件账本/终态分层用)
    # - continuation_root_task_id / root_run_id / root_thread_id: 首轮稳定 ID,
    #   续跑轮复用(不新建 task/run/thread 根)
    # - parent_attempt_id: 上一轮 attempt_id(树形链, 首轮为空)
    # - continuation_prompt: 本轮续跑提示(带 is_continuation 标记, 不伪装用户请求)
    continuation_seq: int = 0
    continuation_root_task_id: str = ""
    continuation_root_run_id: str = ""
    continuation_root_thread_id: str = ""
    continuation_root_request_id: str = ""
    continuation_parent_attempt_id: str = ""
    continuation_prompt: str = ""
    # 上一轮收口原因(缺口F, 双席复核 seq1835): 续跑消息结构化 metadata
    # 的一部分, 系统内部事件可追溯每轮续跑原因, 不依赖提示文本解析。
    continuation_reason: str = ""


@dataclass(frozen=True)
class RuntimeContextRequest:
    user_prompt: str
    inject: list[str] | None
    resume_context: bool | None
    context_scope: str = "default"
    allowed_tools: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    save: bool | None = None
    task_attributes: dict | None = None


# LLM: 已解析参数保留宿主回调/拒绝列表；展示纯值和已评估事实只供原推荐接缝核对，不送入模型或结果，也不能替代快照。
# 类用途: 将冻结工具、权限、会话上下文及本片展示接到原循环；异常与展示回传分别使用各自宿主出口。
# LLM: Prepared runtime parameters carry the applied Compact view unchanged into the one tool
# loop; this transient view cannot authorize calls or replace the full archive.
# 类用途: 保存准备后的循环输入与本次摘要视图，交给唯一工具循环而不写持久状态。
@dataclass
class RuntimeLoopParams:
    user_prompt: str
    root_user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    allowed_tools: list | None = None
    prompt_files: list | None = None
    write_boundary: dict | None = None
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    on_chunk: object = None
    request_id: str = ""
    attempt_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    context_scope: str = "default"
    save: bool | None = None
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] | None = None
    runtime_rejected_actions: list[dict[str, str]] = field(default_factory=list)
    active_turn_transition_callback: object = None
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None
    conversation_history_seed: object = None
    compact_context: object | None = None
    partial_turn_callback: object = None
    capability_presentation: CapabilityPresentationSelection | None = None
    capability_presentation_evaluated: bool = False
    capability_presentation_turn_id: str = ""
    capability_presentation_callback: Callable[[CapabilityPresentationSelection | None], object] | None = field(
        default=None, repr=False, compare=False,
    )


@dataclass
class FinalizeParams:
    user_prompt: str
    final_prompt: str
    final_response: Any
    memories: list
    executed_tools: list[str]
    archive_tool_calls: list[dict[str, object]]
    routed_context: Any
    resume_context_result: Any
    runtime_injections: list
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    run_params: RunParams
    tool_rounds: int
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""
    active_turn_user_inputs: list[dict[str, object]] | None = None
    tool_runtime_evidence: dict[str, object] | None = None
    canonical_native_messages: list[dict[str, object]] | None = None


@dataclass
class PreparedRuntimeContext:
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_result: Any
    resume_context_section: str
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None


@dataclass
class RuntimeLoopResult:
    final_prompt: str
    final_response: Any
    tool_rounds: int
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    executed_tools: list[str]
    archive_tool_calls: list[dict[str, object]]
    active_turn_user_inputs: list[dict[str, object]]
    tool_runtime_evidence: dict[str, object]
    canonical_native_messages: list[dict[str, object]]




# LLM: 原执行种子只传本工作片展示选择；工具和Skill授权仍来自原快照，None保持旧展示，不写共享Agent状态。
# 类用途: 将准备好的目录和能力名卡选择交给工具循环，不再次请求决策模型。
@dataclass
class RuntimeToolLoopSeed:
    params: RuntimeLoopParams
    memories: list
    tool_catalog_section: str
    tool_recommendations_section: str
    tool_runtime_snapshot: object = None
    tool_protocol_snapshot: object = None
    effective_contract_snapshot: object = None
    selected_skill_ids: tuple[str, ...] | None = None
    required_skill_ids: tuple[str, ...] = ()
