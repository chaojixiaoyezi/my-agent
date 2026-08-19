"""2026-08-09 真机 CONVERSATION_TASK_BINDING_FAILED 死锁复现 + 修复验证。

真机链条（requests 复刻任务）：
1. 父代理 wait 无 run_id → watch_run_id fallback 成自身 task_id（self-watch）
2. claim 驱动轮 conversation_task_id=已死子代理 ID（无 active link）
3. promote 走 sticky 回落 bind 父任务 → 父任务 link active + self-watch policy
   enabled → 被 conversation_task_execution_blocker 判 running → bind None → BINDING_FAILED
4. 父代理全部工作工具被拦，连 cancel policy 都救不了自己 → 永久死锁

修复：
- 读侧：subagent_progress_watch policy 用 watch_run_id 判 running，self-watch 不算
- 写侧：wait 无 run_id 时 watch_run_id 置空（不再 fallback 成自身）
- 消费侧：watch 目标已终态的陈旧 policy 到期即禁用
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_support import RunParams
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    _promote_conversation_task_for_work_tool,
)
from agent_py_agent.agent.conversation.task_promotion import (
    conversation_workspace_execution_blocker,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call


def _agent_with_self_watch(tmp_path: Path, *, policy_enabled: bool = True):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _gateway_conversation_context,
        _GatewayConversationLoadRequest,
    )

    store = agent.conversation_store
    conversation = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "session-repro",
                    "channel_user_id": "local-agent",
                }
            },
            "gw-repro",
            "复刻 requests/urllib3",
        )
    )
    thread_id = conversation.thread_id
    task_id = "req_1786250623278_558710_1"
    task_path = Path(agent.home_paths.owner_home_dir) / "tasks" / "existing-task"
    (task_path / "output").mkdir(parents=True)
    (task_path / "work").mkdir()
    # 真机实况 1：父任务 active link
    store.bind_task(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "goal": "requests/urllib3 复刻",
            "status": "active",
            "task_path": str(task_path),
            "work_kind": "",
            "work_name": "",
            "cancellation_scope": "foreground",
        }
    )
    # 真机实况 1.5：thread sticky workspace_task_id=父任务（上轮晋升写入）
    store.select_workspace_task({"thread_id": thread_id, "task_id": task_id})
    # 真机实况 2：父任务自身的 wait policy（wait 无 run_id 的旧 fallback 产物）
    if policy_enabled:
        store.set_progress_policy(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": 600,
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": "sleep",
                    "scope": "own_task_tree",
                    "reason": "",
                    "watch_run_id": task_id,
                },
            }
        )
    return agent, thread_id, task_id


def _promote(agent, params: RunParams, tool_name: str = "list_files"):
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    params.tool_runtime_snapshot = snapshot
    call = canonical_test_call(snapshot, tool_name, {})
    return _promote_conversation_task_for_work_tool(
        ToolCallRuntimeRequest(
            agent=agent,
            request=SimpleNamespace(params=params),
            call=call,
        )
    )


def _params(thread_id, attrs_task_id, task_id):
    return RunParams(
        request_id="bg-main",
        run_id="bg-main-thread-3d9f1fa52eae489c",
        task_id=task_id,
        root_user_prompt="继续 requests/urllib3 复刻",
        task_attributes={
            "conversation_thread_id": thread_id,
            "conversation_task_id": attrs_task_id,
        },
    )


def test_self_watch_policy_does_not_mark_parent_running(tmp_path):
    """修复验证：self-watch policy 不再把父任务判成 running（读侧）。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    blocked = conversation_workspace_execution_blocker(agent)
    assert blocked is None, f"self-watch 不应判 running，blocked={blocked}"


def test_self_watch_policy_no_longer_blocks_sticky_rebind(tmp_path):
    """修复验证：claim 驱动轮（conversation_task_id=子代理 ID）sticky 回落
    bind 父任务不再被 self-watch 拦——BINDING_FAILED 死锁解除。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    params = _params(thread_id, "subagent-1786211892-55685e17", "subagent-1786211892-55685e17")
    agent._current_run_params = params
    outcome = _promote(agent, params)
    assert outcome is None, f"promote 应放行，实际={getattr(outcome, 'error_code', outcome)}"


def test_realistic_watch_of_other_run_still_marks_that_run_running(tmp_path):
    """语义保留：wait 显式 watch 子代理时，子代理仍算 running（H2-2 不重复催促）。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786245734-a462d5b5"
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": child_id,
            },
        }
    )
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    # 父任务 blocker 不因 watch 子代理而拦
    blocked = conversation_workspace_execution_blocker(agent)
    assert blocked is None
    # 但子代理被判 running
    from agent_py_agent.agent.conversation.task_promotion import (
        conversation_task_execution_state,
    )

    state = conversation_task_execution_state(
        agent.conversation_store, thread_id, child_id
    )
    assert state["running"] is True
    assert "progress_policy" in state["sources"]


def _retire_with_child(agent, store, thread_id, child_id, *, task_id):
    store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": child_id,
            },
        }
    )
    # 子代理 BLOCKED 终态 link
    store.bind_task(
        {
            "thread_id": thread_id,
            "task_id": child_id,
            "goal": "fixer-2",
            "status": "blocked",
            "cancellation_scope": "foreground",
        }
    )
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        ConversationStore,
        FakeDeliveryService,
    )

    enabled, _ = store.list_progress_policies_report(enabled_only=True)
    stale_policies = [p for p in enabled if str((p.metadata or {}).get("watch_run_id") or "") == child_id]
    assert stale_policies, "watch 子代理的 policy 应存在且 enabled"
    # tick 落在「已 due 但未 stale」窗口(next_due+100s,catchup 是 2h):
    # 只验证「watch 目标终态 → 退休」路径,不靠 stale 假绿。
    due_at = max(float(p.next_due_at or 0) for p in stale_policies)
    stale_ids = {str(p.policy_id) for p in stale_policies}
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent, store=store, channels=FakeDeliveryService()
            ),
            "store": store,
            "claim_ttl_seconds": 30,
        }
    )
    scheduler.tick(now=due_at + 100)
    after_enabled, _ = store.list_progress_policies_report(enabled_only=True)
    after = {str(p.policy_id) for p in after_enabled}
    assert not (stale_ids & after), "watch 目标已终态的陈旧 policy 应被禁用"


def test_stale_watch_policy_retired_when_watch_target_terminal(tmp_path):
    """修复验证：watch 目标已终态（BLOCKED/completed）的陈旧 policy 消费即禁用。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786245734-a462d5b5"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=task_id)


def test_executor_running_blocks_writing_tools_only(tmp_path):
    """问题6契约:执行锁豁免从 ToolRuntimePolicy.mutates_workspace 声明推导,
    不再手写工具名名单。executor running 时:写 workspace 工具被拦,读/编排
    工具(wait/cancel 等)必须始终可用——2026-08-09 真机死锁里连 cancel 都
    救不了自己,根源就是豁免名单与模型行为脱节。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    # blocker 只拦「当前 conversation_task_id 自身 running」:用第三者任务 watch
    # 当前任务,让 task_id 在 execution_state 里 running(而非 child running)。
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": "other-task-9",
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": task_id,
            },
        }
    )
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    params.tool_runtime_snapshot = snapshot

    # run_command 在无 bwrap 测试环境被 availability 隐藏,不在快照中;契约
    # 测试验证声明驱动机制本身,写类集合取快照内必在的文件系统写工具。
    writing = {"write_file", "edit_file", "apply_patch"}
    exempt = {"read_file", "list_files", "cancel_subagents", "inspect_agent_tree"}
    # 先写工具后豁免工具:豁免工具 promote 成功后同轮 TURN_ACTIVE 置位,后续
    # 写工具放行——「首轮绑定后同轮放行」是既有语义,与声明无关,避免串扰。
    for tool_name in sorted(writing) + sorted(exempt):
        runtime = snapshot.runtime(tool_name)
        assert runtime is not None, f"{tool_name} 应在 run snapshot 中"
        call = canonical_test_call(snapshot, tool_name, {})
        outcome = _promote_conversation_task_for_work_tool(
            ToolCallRuntimeRequest(
                agent=agent,
                request=SimpleNamespace(params=params),
                call=call,
            )
        )
        if tool_name in writing:
            assert outcome is not None, (
                f"{tool_name} 声明写 workspace,executor running 时必须被拦"
            )
            assert getattr(outcome, "error_code", "") in {
                "CONVERSATION_TASK_ALREADY_RUNNING",
                "CONVERSATION_TASK_BINDING_FAILED",
            }, (
                f"{tool_name} 应报绑定门错误,"
                f"实际={getattr(outcome, 'error_code', outcome)}"
            )
        else:
            assert outcome is None, (
                f"{tool_name} 不写 workspace,executor running 时必须豁免,"
                f"实际={getattr(outcome, 'error_code', outcome)}"
            )


def test_stale_self_task_watch_retired_when_target_terminal(tmp_path):
    """真机实况补测：陈旧 policy 的 task_id 直接就是被 watch 的子代理 ID
    （watch 目标=task_id 自身）时,目标终态同样必须退休——修复 3 的
    watched != task_id_field 条件把这类政策永远排除,300s 一轮空转。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786211892-55685e17"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=child_id)
