# LLM: 宿主读取共用原 ActionPolicy、ManagedOperationStore 与活动回合事务；不调用工具执行器，不登记假工具操作或复制权限规则。
# 模块用途: 为一次选包准备提供精确执行轮检查和入口只读准入，取消与换代仍由原任务权威裁决。
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..agent_core.model.call_runtime import max_output_tokens, model_name
from ..agent_core.model.context_window import resolve_model_context_window_tokens
from ..agent_core.tool_loop.recovery import runtime_run_scope
from ..agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from ..common.cancellation import ToolCancelled
from ..path_access_policy import effective_owner_scope_root
from ..runtime_context import current_subagent_run_id
from ..runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ToolOperationAuthorityRequest,
)
from ..runtime_db.operation_store_selector import select_operation_store
from ..tooling.action_policy import ActionPolicy, ActionPolicyRequest
from ..tooling.registry import _execution_workspace_roots
from ..tooling.registry_workspace import effective_registry_cwd
from ..tooling.runtime_contracts import ToolCall


# LLM: 会话链接和执行权分别冻结，后者复用原 runtime_run_scope；锁内 check 不回读 TaskStore，事务顺序是活动回合 T 后任务锁。
# 类用途: 复核当前执行仍可工作，并将任务选择提交和停止串行化。
@dataclass(frozen=True)
class PackageSelectionAuthority:
    agent: object
    params: object
    task_id: str
    thread_id: str
    request_id: str
    run_id: str
    attempt_id: str
    execution_task_id: str = field(init=False)
    declared_task_id: str = field(init=False)

    # LLM: successor 可换会话链接而不换当前 RuntimeDB 执行链；只读原工具 scope，不重新绑定 run 或 attempt。
    # 函数用途: 记住原执行归属，避免把新任务目录的编号当成模型当前执行权限。
    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_task_id", runtime_run_scope(self.agent, self.params).task_id)
        object.__setattr__(self, "declared_task_id", self.params.task_id)

    # LLM: 只检查原取消令牌、当前宿主绑定及原 operation store 的 read-only authority；不领取 operation 或补造运行链。
    # 函数用途: 在读取、pin 和选择提交前拒绝已停止或已换代的执行。
    def check(self) -> None:
        self.params.cancellation_token.raise_if_cancelled()
        current = getattr(self.agent, "_current_run_params", None)
        if current_subagent_run_id(self.agent) or not all(self._matches(value) for value in (current, self.params)):
            raise ToolCancelled("CAPABILITY_SELECTION_EXECUTION_CHANGED")
        scope = runtime_run_scope(self.agent, self.params)
        if (scope.task_id, scope.request_id, scope.run_id, scope.attempt_id) != (
            self.execution_task_id, self.request_id, self.run_id, self.attempt_id,
        ):
            raise ToolCancelled("CAPABILITY_SELECTION_EXECUTION_CHANGED")
        store = select_operation_store(self.agent)
        require = getattr(store, "require_authority", None)
        if callable(require):
            try:
                require(ToolOperationAuthorityRequest(
                    owner_id=str(getattr(self.agent.tools, "operation_owner_id", "") or ""),
                    run_id=self.run_id, task_id=self.execution_task_id, operation_id="", tool_name="skill_search", attempt_id=self.attempt_id,
                ))
            except AuthorityContextMissing as exc:
                raise ToolCancelled("CAPABILITY_SELECTION_EXECUTION_CHANGED") from exc
        self.params.cancellation_token.raise_if_cancelled()

    # LLM: 外层权威和循环载体都须独立匹配冻结身份，不从任一份补另一份，允许它们是不同对象。
    # 函数用途: 阻止准备中参数被换成其它请求或任务后继续交付正文。
    def _matches(self, params: object) -> bool:
        attrs = getattr(params, "task_attributes", None)
        return (isinstance(attrs, dict) and attrs.get("conversation_task_id") == self.task_id
                and attrs.get("conversation_thread_id") == self.thread_id
                and getattr(params, "task_id", "") == self.declared_task_id
                and (getattr(params, "request_id", ""), getattr(params, "run_id", ""), getattr(params, "attempt_id", ""))
                == (self.request_id, self.run_id, self.attempt_id))

    # LLM: TaskStore 已在自身锁内核对 link/marker 身份；此回调仅复核当前 execution，不能再取 task 锁或联网。
    # 函数用途: 把同一只读执行权检查适配到原选择 CAS。
    def is_current(self, link: object, marker: object) -> bool:
        del marker
        self.check()
        return getattr(link, "task_id", "") == self.task_id and getattr(link, "thread_id", "") == self.thread_id

    # LLM: 仅包住有限本地 claim/read/pin/finish/IR 提交；不把模型调用放入活动回合锁。
    # 函数用途: 让原 Gateway 停止操作与本次准备事务保持顺序。
    def atomic(self, operation):
        # LLM: 执行事务前复核当前身份，调用方必须是宿主提供的同步本地操作。
        # 函数用途: 在活动回合锁内检查执行权再执行有限操作。
        def guarded():
            self.check()
            return operation()

        transition = getattr(self.params, "active_turn_transition_callback", None)
        return transition("capability_selection", guarded) if callable(transition) else guarded()


# LLM: 评估值只送原 ActionPolicy，不是模型调用，不进入 history/operation/handler；ask 与 deny 均不给宿主自动读取权。
# 函数用途: 用原工具的 schema、运行边界、owner 模式和守卫检查即将读取的准确入口参数。
def package_entry_policy(agent: object, params: object, tools: object, arguments: dict, *, claim_id: str):
    registry = agent.tools
    runtime = tools.runtime("skill_search")
    if runtime is None:
        raise ValueError("CAPABILITY_SELECTION_READ_UNAVAILABLE")
    call = ToolCall(
        call_id=f"host-read-policy:{claim_id}", tool_name="skill_search", arguments=arguments,
        source_protocol="native", schema_hash=runtime.model_spec.schema_hash,
        run_id=params.run_id, turn_id=params.conversation_turn_id or params.request_id, attempt_id=params.attempt_id,
    )
    boundary = write_boundary_with_runtime_ledger(agent, params)
    root = effective_registry_cwd(registry.workspace_root, boundary)
    mode = registry.approval_mode_reader() if registry.approval_mode_reader else "ask"
    return ActionPolicy().decide(ActionPolicyRequest(
        call=call, runtime_snapshot=tools, workspace_root=root,
        workspace_roots=_execution_workspace_roots(root, registry.workspace_roots, boundary),
        path_access_mode=registry.path_access_mode, path_dangerous_roots=tuple(registry.path_dangerous_roots),
        owner_scope_root=effective_owner_scope_root(write_boundary=boundary, registry_scope=registry.owner_scope_root),
        write_boundary=boundary, runtime_guard_policy=registry.runtime_guard_policy, approval_mode=mode,
    ))


# LLM: 摘要仅观察准备时实际绑定的公开生成字段，不充当 profile/凭据版本或模型采用权；原辅助账仍记录实际调用。
# 函数用途: 识别本次选择使用的后端配置，避免把菜单后来切换的 profile 冒充当前工作片。
def package_selection_model_digest(agent: object) -> str:
    config = agent.config
    fields = {name: getattr(config, name, None) for name in (
        "model_backend", "api_base", "temperature", "top_p", "stream_enabled",
        "model_reasoning_control", "model_structured_output",
    )}
    fields.update(backend=type(agent.backend).__name__, model=model_name(agent),
                  max_output_tokens=max_output_tokens(agent), context_window_tokens=resolve_model_context_window_tokens(agent))
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
