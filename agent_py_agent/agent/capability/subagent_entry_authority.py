# LLM: 只承接原 child 创建和首请求权威；typed 初始化材料不进 attrs，原线程标记仍是唯一持久资格。
# 模块用途: 校验宿主创建材料和活动子代理执行轮，让包入口读取不能扩大权限、跨轮提交或另建任务。
from __future__ import annotations

from dataclasses import dataclass

from ..common.cancellation import ToolCancelled
from ..runtime_context import current_subagent_run_id

PACKAGE_ENTRY_PURPOSE = "capability_package_entries"


# LLM: 仅原创建链传递此对象，不接收模型 dict/普通 metadata；只初始化新线程，不保存另一份采用状态。
# 类用途: 将已验证孩子的精确身份交给原 ThreadStore，区别真正创建与恢复 ensure。
@dataclass(frozen=True)
class SubagentEntryInitialization:
    run_id: str
    thread_id: str


# LLM: 原 preparing 标记、canonical RUNNING/attempt、取消及 RuntimeDB 权威共同约束读取；本对象无持久状态。
# 类用途: 在读前、读后和提交上下文前拒绝停止、换代或错误线程的迟到内容。
@dataclass(frozen=True)
class SubagentEntryAuthority:
    agent: object
    params: object
    preparation: object
    thread_id: str
    execution_scope: object

    # LLM: 调用方持原 creation_guard，嵌套 pin 只验证原 child refs；不得在这里写 task/thread 或回填首请求资格。
    # 函数用途: 复核当前调用仍属于领取资格的活动孩子，并通过原操作账执行准入。
    def check(self) -> None:
        from ..agent_core.subagent.model_selection import (
            _require_current_attempt,
            first_request_preparation,
        )
        from ..agent_core.tool_loop.recovery import runtime_run_scope
        from ..conversation.agent_thread_store import SUBAGENT_FIRST_REQUEST_KEY
        from ..runtime_db.managed_operation_store import (
            AuthorityContextMissing,
            ToolOperationAuthorityRequest,
        )
        from ..runtime_db.operation_store_selector import select_operation_store

        params, agent = self.params, self.agent
        params.cancellation_token.raise_if_cancelled()
        current = getattr(agent, "_current_run_params", None)
        if (current_subagent_run_id(agent) != params.run_id
                or first_request_preparation(agent, params) is not self.preparation
                or any(getattr(current, field, None) != getattr(params, field, None)
                       for field in ("run_id", "attempt_id", "request_id", "task_id"))):
            raise ToolCancelled("CAPABILITY_ENTRY_EXECUTION_CHANGED")
        manager = agent.subagents
        _require_current_attempt(manager, params, params.run_id)
        task = manager.load(params.run_id)
        thread = agent.conversation_store.threads.require(self.thread_id)
        marker = thread.metadata.get(SUBAGENT_FIRST_REQUEST_KEY, {})
        if (task.agent_thread_id != self.thread_id or thread.metadata.get("agent_run_id") != task.id
                or not isinstance(marker, dict) or marker.get("schema") != SUBAGENT_FIRST_REQUEST_KEY
                or marker.get("status") != "preparing" or marker.get("attempt_id") != params.attempt_id
                or marker.get("child_run_id") != task.id or marker.get("child_thread_id") != self.thread_id
                or runtime_run_scope(agent, params) != self.execution_scope):
            raise ToolCancelled("CAPABILITY_ENTRY_EXECUTION_CHANGED")
        require = getattr(select_operation_store(agent), "require_authority", None)
        if callable(require):
            try:
                require(ToolOperationAuthorityRequest(
                    owner_id=str(getattr(agent.tools, "operation_owner_id", "") or ""),
                    run_id=params.run_id, task_id=self.execution_scope.task_id, operation_id="",
                    tool_name="skill_search", attempt_id=params.attempt_id,
                ))
            except AuthorityContextMissing as exc:
                raise ToolCancelled("CAPABILITY_ENTRY_EXECUTION_CHANGED") from exc
        params.cancellation_token.raise_if_cancelled()
