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
            tool_protocol="text",
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _GatewayConversationLoadRequest,
        _gateway_conversation_context,
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
                    "tool": "wait",
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
                "tool": "wait",
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


def test_wait_without_run_id_leaves_watch_run_id_empty(tmp_path):
    """修复验证：wait 无显式 run_id 时 watch_run_id 置空（写侧防复发）。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    from agent_py_agent.agent.agent_core.runtime.wait_tool import WaitTool

    outcome = WaitTool(agent).execute({"seconds": 600, "thread_id": thread_id, "task_id": task_id})
    assert outcome.ok
    policies = agent.conversation_store.list_progress_policies_report()
    matches = [
        p
        for p in policies[0]
        if str(getattr(p, "thread_id", "") or "") == thread_id
        and str(getattr(p, "task_id", "") or "") == task_id
    ]
    assert matches
    policy = matches[0]
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    assert str(metadata.get("watch_run_id") or "") == ""
    assert metadata.get("kind") == "subagent_progress_watch"


def _retire_with_child(agent, store, thread_id, child_id, *, task_id):
    store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "wait",
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

    def _watch_policy_ids(enabled_only):
        return {
            str(p.policy_id): str((p.metadata or {}).get("watch_run_id") or "")
            for p in store.list_progress_policies_report(enabled_only=enabled_only)[0]
        }

    before = _watch_policy_ids(True)
    stale_ids = [pid for pid, watched in before.items() if watched == child_id]
    assert stale_ids, "watch 子代理的 policy 应存在且 enabled"
    # 用与既有后台调度测试相同的容器驱动一次 tick：目标终态 → 该 policy 被禁用
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent, store=store, channels=FakeDeliveryService()
            ),
            "store": store,
            "claim_ttl_seconds": 30,
        }
    )
    scheduler.tick(now=__import__("time").time() + 9999)
    after = _watch_policy_ids(True)
    assert not any(pid in after for pid in stale_ids), (
        "watch 目标已终态的陈旧 policy 应被禁用"
    )


def test_stale_watch_policy_retired_when_watch_target_terminal(tmp_path):
    """修复验证：watch 目标已终态（BLOCKED/completed）的陈旧 policy 消费即禁用。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786245734-a462d5b5"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=task_id)


def test_stale_self_task_watch_retired_when_target_terminal(tmp_path):
    """真机实况补测：陈旧 policy 的 task_id 直接就是被 watch 的子代理 ID
    （watch 目标=task_id 自身）时,目标终态同样必须退休——修复 3 的
    watched != task_id_field 条件把这类政策永远排除,300s 一轮空转。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786211892-55685e17"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=child_id)
