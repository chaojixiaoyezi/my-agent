# LLM: Shared post-create lifecycle for root and nested subagent scheduling.
# 模块用途: 子代理任务创建后统一登记会话、记住 run_id、自动启动 runner。

from __future__ import annotations

from dataclasses import dataclass

from .orchestration_background_dispatch import auto_start_tasks
from .orchestration_run_scope import remember_orchestration_run_ids


# LLM: CreatedSubagentLifecycleRequest carries post-create facts shared by root and nested scheduling.
# 类用途: 把 agent、已创建任务和原始请求参数打包，避免 create/schedule 两条入口各自处理启动副作用。
@dataclass(frozen=True)
class CreatedSubagentLifecycleRequest:
    """Facts needed after a create/schedule path has materialized task records."""

    agent: object
    tasks: list[object]
    request_params: dict[str, object]


# LLM: CreatedSubagentLifecycleResult is the small lifecycle summary returned to payload builders.
# 类用途: 保存后台启动结果和 run_id 列表，供 create_subagents/schedule_child_subagents 使用同一返回语义。
@dataclass(frozen=True)
class CreatedSubagentLifecycleResult:
    """Shared lifecycle output consumed by model-visible create/schedule payloads."""

    auto_start: dict[str, object]
    run_ids: list[str]


# LLM: publish_created_subagents is the shared post-create path for all subagent creation tools.
# 函数用途: 创建后统一绑定会话、登记 run_id、默认后台启动，避免 create 和 schedule 产生两套行为。
def publish_created_subagents(
    request: CreatedSubagentLifecycleRequest,
) -> CreatedSubagentLifecycleResult:
    """Register newly materialized subagents and optionally launch their runners."""
    tasks = [task for task in request.tasks if _task_id(task)]
    run_ids = [_task_id(task) for task in tasks]
    _bind_tasks_to_conversation(request.agent, tasks)
    remember_orchestration_run_ids(request.agent, run_ids)
    auto_start = auto_start_tasks(request.agent, tasks, request.request_params)
    return CreatedSubagentLifecycleResult(auto_start=auto_start, run_ids=run_ids)


# LLM: _bind_tasks_to_conversation keeps child run ids reachable from the parent conversation.
# 函数用途: 如果任务带 conversation_thread_id，就登记 run_id 到同一线程，便于事件和 guidance 找到目标。
def _bind_tasks_to_conversation(agent: object, tasks: list[object]) -> None:
    """Make local subagent run_ids addressable in the parent conversation thread."""
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "bind_task", None)):
        return
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            continue
        thread_id = str(attrs.get("conversation_thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            store.bind_task(
                {
                    "thread_id": thread_id,
                    "task_id": _task_id(task),
                    "goal": str(getattr(task, "goal", "") or ""),
                }
            )
        except Exception:
            continue


# LLM: _task_id filters mocks and malformed task objects before lifecycle registration.
# 函数用途: 只接受真实字符串 run_id，避免测试替身或坏对象污染 run_id 账本。
def _task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""
