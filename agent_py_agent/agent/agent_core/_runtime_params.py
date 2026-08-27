# LLM: 本模块集中定义一次 Agent run、工具循环和收口阶段的参数合同；新增控制状态必须是宿主注入的结构化字段并随 replace/续跑传播。
# 模块用途: 保存模型循环、工具执行、压缩和归档阶段共享的不可变参数束。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol import RunScope
from ..concurrency.interrupt import is_interrupted
from ..tooling.cancellation import CancellationToken


@dataclass(frozen=True)
class FinalizeContext:
    user_prompt: str
    final_prompt: str
    final_response: Any
    memories: list
    executed_tools: list
    archive_tool_calls: list
    routed_context: Any
    resume_context_result: Any
    runtime_injections: list
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    request_id: str
    run_id: str
    task_id: str
    source: str
    do_save: bool
    task_attributes: dict | None
    recovery_task_refs: list | None
    recovery_content_paths: list | None
    recovery_next_actions: list | None
    tool_rounds: int = 0
    compact_auto_continue_depth: int = 0
    compact_auto_no_tool_continue_depth: int = 0
    main_context_bundle_path: str = ""
    main_context_bundle_markdown_path: str = ""
    delivery_contract: dict | None = None
    # 子代理 task_local 回合的 compact 阈值覆盖依赖这个字段；来源是 run params 的 context_scope。
    context_scope: str = "default"
    active_turn_user_inputs: list[dict[str, object]] = field(default_factory=list)
    tool_runtime_evidence: dict[str, object] = field(default_factory=dict)
    # Typed sink used only to retrieve tool-boundary-confirmed assistant parts.
    on_chunk: object = None


# LLM: ToolLoopExecuteParams 是工具调用的 run 级事实源；临时批准/拒绝只能由当前审批等待链追加，不能来自模型参数。
# 类用途: 汇总一轮工具循环所需的上下文、协议快照、取消令牌和临时审批决定。
@dataclass(frozen=True)
class ToolLoopExecuteParams:
    user_prompt: str
    memories: list
    runtime_injections: list
    prompt_files: list
    tool_catalog_section: str
    tool_recommendations_section: str
    tool_context: list
    effective_on_chunk: Any
    allowed_tools: Any
    write_boundary: Any
    task_attributes: dict | None
    request_id: str
    run_id: str
    task_id: str
    one_shot_tool_calls: set
    executed_tools: list
    archive_tool_calls: list
    # Unique host-owned invocation identity.  Stable run/task ids remain the
    # durable lineage while provider call ids are namespaced by this attempt.
    attempt_id: str = ""
    # 会话运行时 run snapshot: catalog, search, native Schema and execution share one tool universe.
    tool_runtime_snapshot: Any = None
    # The provider capability and native/text decision are fixed once per run.
    tool_protocol_snapshot: Any = None
    # The existing EffectiveContractSnapshot remains the sole task obligation authority.
    effective_contract_snapshot: Any = None
    # One host-owned token follows this run through shell/MCP/HTTP/long-poll boundaries.
    cancellation_token: CancellationToken = field(
        default_factory=lambda: CancellationToken(_external_check=is_interrupted)
    )
    tool_rounds: int = 0
    save: bool | None = None
    live_archive_state: dict[str, object] = field(default_factory=dict)
    # 原生 tool_use（native）下与 tool_context 文本链路并存的结构化 IR 历史
    # （AssistantTurn / ToolResult / UserTurn / CompactionSummary）；text 协议下恒为空，
    # 由 message_adapter 翻成厂商原生 messages。详见 agent_core/tool_ir_history.py。
    tool_ir_history: list = field(default_factory=list)
    system_prompt_override: str | None = None
    context_scope: str = "default"
    delivery_contract: dict | None = None
    run_scope: RunScope | None = None
    root_user_prompt: str = ""
    source: str = "run"
    runtime_guard_policy: object | None = None
    # Auxiliary presentation-only model turns (for example the model-authored
    # background receipt) must not drain user steering or task lifecycle input.
    # Those inputs belong to the real active task turn and remain pending until
    # that turn reaches its next model safe point.
    consume_pending_turn_input: bool = True
    # Real user turns added to this already-running task (currently /btw).
    # These structured packets cross compact continuations; provider text is
    # still emitted through UserTurn IR/tool_context at its chronological point.
    active_turn_user_inputs: list[dict[str, object]] = field(default_factory=list)
    # Gateway-only typed callback serializes reservation/submission with exact-turn close.
    active_turn_transition_callback: object = None
    # Completed conversation history stays separate from the current run's tool IR. The seed is
    # immutable and already bounded/compacted by ConversationStore.
    conversation_history_seed: object = None
    # Progressive disclosure: an explicit tool_search selection makes these
    # already-authorized deferred tools visible to the next model call only.
    loaded_tool_names: set[str] = field(default_factory=set)
    # 会话运行时 turn snapshot: volatile wall-clock fields must not rewrite the
    # first provider message on every tool round and invalidate prompt caching.
    workspace_context_snapshot: str = ""
    # 协议违规修复机会次数：模型输出格式偶发抖动时先给几次结构化重试再 break。
    max_protocol_repairs: int = 2
    # 部署者显式开启 hard_failure_halt 后，同一工具同类失败连续达硬阈值的
    # 收口标记(tool_name, failure_class, count)。默认可恢复失败只返给模型
    # 换策略，不写本字段；同一批后续成功还会撤销早到的旧标记。
    repeated_failure_halt: tuple[str, str, int] | None = None
    # 显式硬门真收口标记:True 时本轮不再自动续跑,等用户提供新思路。
    repeated_failure_halt_exhausted: bool = False
    # 当前 run 内已由交互审批确认的精确 ActionPolicy binding；不持久化、不按工具名泛化。
    runtime_approved_actions: list[dict[str, str]] = field(default_factory=list)
    # 当前 run 内用户已拒绝/取消的精确工具参数指纹；provider 重建 call identity 时仍按
    # tool_name + args_hash 阻止同一调用再次弹框，参数变化后才允许重新请求。
    runtime_rejected_actions: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class CompressionContext:
    user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    request_id: str
    run_id: str
    task_id: str
    source: str


@dataclass(frozen=True)
class ArchiveRunParams:
    do_save: bool
    user_prompt: str
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    run_id: str
    task_id: str
    source: str
    task_attributes: dict | None = None
    context_scope: str = "default"


@dataclass(frozen=True)
class EstimateTokenParams:
    user_prompt: str
    runtime_injections: list
    memories: list
    final_response: Any
    archive_tool_calls: list
    run_request_id: str
    turn_id: str
    final_prompt: str = ""
    context_scope: str = "default"
    task_attributes: dict | None = None
