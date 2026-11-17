# LLM: 只用真实工具、策略与临时宿主目录，不连接模型、网络或生产配置；对照用例证明原有放行/中断路径不变。
# 模块用途: 回归 G1 工具审批与回执：SOUL 自主模式确认、查无用户的结局、请求时间线 owner home、派工闸门原因与命名工作自停。
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_payload import _auto_start_payload
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
from agent_py_agent.agent.contracts.model_call_ledger import MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE
from agent_py_agent.agent.conversation.control_commands import conversation_request_interrupt_name
from agent_py_agent.agent.conversation.named_work import stop_named_conversation_work
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import gateway_status
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    runtime_snapshot_for_tools,
)


# LLM: 走统一 ActionPolicy 的真实工具声明；自主模式只免可选确认，SOUL 写入属于必须本人确认的危险动作。
# 函数用途: R01 —— auto 与 ask 下 SOUL 写入都要确认，USER 写入与只读列表照常放行。
@pytest.mark.parametrize("approval_mode", ["auto", "ask"])
def test_soul_write_requires_confirmation_even_in_auto_mode(tmp_path, approval_mode) -> None:
    tool = UpdatePersonaTool(SimpleNamespace(home_paths=SimpleNamespace()))
    snapshot = runtime_snapshot_for_tools({"update_persona": tool})

    def decide(arguments):
        call = canonical_test_call(snapshot, "update_persona", arguments,
                                   operation_id="op-g1-r01", idempotency_key="key-g1-r01")
        return ActionPolicy().decide(ActionPolicyRequest(call, snapshot, tmp_path, approval_mode=approval_mode))

    soul = decide({"target": "soul", "content": "语气偏活泼"})
    assert soul.status == "ask" and "APPROVAL_REQUIRED" in soul.reason_codes
    assert decide({"target": "user", "content": "称呼:小王"}).status == "allow"
    listed = decide({"action": "list", "target": "soul"})
    assert listed.status == "allow" and listed.resolved_effect == "read_only"


# LLM: 冷宿主管理员身份与 test_users_slash_and_admin_controls 同源；不创建任何普通账号。
# 函数用途: 构造一个真实的本机管理员宿主。
def _admin(tmp_path):
    paths = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, OwnerIdentity.local_main()))
    paths.owner_home_dir.mkdir(parents=True)
    return SimpleNamespace(home_paths=paths)


# LLM: 查无用户在任何写入前被宿主拒绝，回执须如实为 not_started 并给出可执行下一步。
# 函数用途: R03 —— get_user 查无此人返回 USER_NOT_FOUND/not_started、宿主错误投影和 list_users 提示。
def test_admin_get_missing_user_is_not_started_with_next_step(tmp_path) -> None:
    outcome = AdminControlsTool(_admin(tmp_path)).execute({"action": "get_user", "user_id": "ghost"})

    assert outcome.ok is False and outcome.error_code == "USER_NOT_FOUND"
    assert outcome.effect_outcome == "not_started"
    report = json.loads(outcome.output)
    assert report["error"]["code"] == "USER_NOT_FOUND" and report["error"]["outcome"] == "not_started"
    assert "list_users" in report["next_action"] and "list_users" in outcome.next_action


# LLM: 5xx 存储错误可能发生在部分写入之后（建号物化失败会先保存失败记录），不能被报成已证明未执行。
# 函数用途: R03 对照 —— 宿主 503 仍保持 unknown。
def test_admin_store_unavailable_keeps_unknown_outcome(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts import users_control_service

    def unavailable(*_args, **_kwargs):
        raise HostAPIError("STORE_UNAVAILABLE", 503)

    monkeypatch.setattr(users_control_service, "manage_local_users", unavailable)
    outcome = AdminControlsTool(_admin(tmp_path)).execute({"action": "get_user", "user_id": "ghost"})

    assert outcome.ok is False and outcome.error_code == "STORE_UNAVAILABLE"
    assert outcome.effect_outcome == "unknown"


# LLM: 真实 message_stage_timeline 读 Gateway 请求记录与 owner home 下的轮次遥测；工作区根故意与 owner home 不同。
# 函数用途: R05 —— 状态面请求时间线从 owner home 取模型轮次，而不是从 agent.root。
def test_request_timeline_reads_rounds_from_owner_home(tmp_path, monkeypatch) -> None:
    request_id = "req-g1-r05"
    gateway_root = tmp_path / "gateway"
    (gateway_root / "requests" / "done").mkdir(parents=True)
    (gateway_root / "requests" / "done" / f"{request_id}.json").write_text(json.dumps(
        {"id": request_id, "created_at": 1_800_000_000.0, "completed_at": 1_800_000_003.0}), encoding="utf-8")
    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    rounds = owner_home / "data" / "telemetry" / "task_calls" / f"{request_id}.rounds.jsonl"
    rounds.parent.mkdir(parents=True)
    rounds.write_text(json.dumps({"row": {"started_at": 1_800_000_001.0, "duration_ms": 1500.0,
                                          "model": "probe"}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(gateway_status, "gateway_paths", lambda _agent: SimpleNamespace(root=gateway_root))
    agent = SimpleNamespace(root=tmp_path / "workspace", home_paths=SimpleNamespace(owner_home_dir=owner_home))

    timeline = gateway_status._request_timeline(agent, (None, request_id))

    assert timeline is not None and timeline["ok"] is True and timeline["request_id"] == request_id
    assert "rounds" not in timeline["coverage"]
    assert timeline["summary"]["model_rounds"] == 1 and timeline["summary"]["model_total_ms"] == 1500.0


# LLM: 三类生产者形状取自 background.dispatch 的 host_shutdown、会话生命周期闸门与启动失败分支。
# 函数用途: R08 —— create_subagents 回执保留 blocked/failed/延期的确切原因字段。
def test_auto_start_payload_keeps_exact_gate_reason() -> None:
    shutdown = {"status": "blocked", "run_ids": [], "skipped_run_ids": ["run-a"], "reason": "host_shutdown",
                "error_code": MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE, "reason_code": "host_shutdown"}
    gated = {"status": "blocked", "run_ids": [], "deferred_run_ids": [], "skipped_run_ids": [],
             "reason": "conversation_lifecycle_gate",
             "conversation_gate": [{"run_id": "run-b", "reason_code": "conversation_task_inactive"}]}
    started = {"status": "started", "run_ids": ["run-c"], "deferred_run_ids": ["run-d"],
               "deferred_reason": "item.defer_start=true"}
    failed = {"status": "failed", "run_ids": ["run-e"], "error": "RuntimeError: runner start reservation failed"}

    assert _auto_start_payload(shutdown) == shutdown
    assert _auto_start_payload(gated) == gated
    assert _auto_start_payload(started) == {**started, "start_status": "accepted"}
    assert _auto_start_payload(failed) == failed


# LLM: 与 test_conversation_goal_tools 同源的 echo 宿主；命名 Goal 及其活动 TaskLink 都是真实会话存储记录。
# 函数用途: 准备一个可被 stop_named_work 精确停止的命名 Goal。
def _named_goal_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "u-1", "channel": "feishu", "channel_conversation_id": "c-1", "channel_user_id": "u-1",
    })
    goal = agent.conversation_store.goals.create(
        {"thread_id": thread.thread_id, "objective": "持续整理周报", "name": "周报整理"})
    agent.conversation_store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective,
        "status": "active", "work_kind": "goal", "work_name": goal.name,
    })
    return agent, thread, goal


# 函数用途: 读出命名 Goal 对应 TaskLink 的当前状态。
def _link_status(agent, thread, goal):
    return next(item.status for item in agent.conversation_store.tasks.list(thread.thread_id)
                if item.task_id == goal.task_id)


# LLM: 后台 Goal 轮的请求号即其 task_id，执行线程登记 conversation-request:{task_id}；工具在本轮内停止自身。
# 函数用途: O01 —— 目标在自己的回合里停止自己时，本轮回复不被自身中断，持久状态仍被取消。
def test_goal_stopping_itself_does_not_interrupt_its_own_reply(tmp_path) -> None:
    agent, thread, goal = _named_goal_agent(tmp_path)
    agent._current_run_params = RunParams(request_id=goal.task_id, task_id=goal.task_id, task_attributes={
        "conversation_thread_id": thread.thread_id, "conversation_task_id": goal.task_id,
    })

    with register_interruptible(conversation_request_interrupt_name(goal.task_id)):
        result = agent.tools.tools["stop_named_work"].execute({"name": "周报整理"})
        interrupted = is_interrupted()

    assert result.ok is True and interrupted is False
    assert agent.conversation_store.goals.list(thread.thread_id) == []
    assert _link_status(agent, thread, goal) == "cancelled"


# LLM: 前台普通回合有自己的请求号；Goal 的后台轮在另一线程登记原名，必须照常收到停止。
# 函数用途: O01 对照 —— 从别的回合停止命名 Goal 仍中断它正在运行的回合。
def test_foreground_stop_still_interrupts_the_goal_turn(tmp_path) -> None:
    agent, thread, goal = _named_goal_agent(tmp_path)
    registered, release, observed = threading.Event(), threading.Event(), {}

    def goal_turn() -> None:
        with register_interruptible(conversation_request_interrupt_name(goal.task_id)):
            registered.set()
            release.wait(10)
            observed["interrupted"] = is_interrupted()

    worker = threading.Thread(target=goal_turn, name="g1-o01-goal-turn", daemon=True)
    worker.start()
    assert registered.wait(10)
    agent._current_run_params = RunParams(request_id="foreground-request", task_attributes={
        "conversation_thread_id": thread.thread_id, "conversation_task_id": "ordinary-turn",
    })
    try:
        result = agent.tools.tools["stop_named_work"].execute({"name": "周报整理"})
    finally:
        release.set()
        worker.join(10)

    assert result.ok is True and observed["interrupted"] is True
    assert _link_status(agent, thread, goal) == "cancelled"


# LLM: /stop 控制路径（audit_control_service）不传 spare 名；即使调用线程恰好持有该名也照常中断。
# 函数用途: O01 对照 —— 控制路径的中断语义不变。
def test_control_stop_path_still_interrupts_the_named_turn(tmp_path) -> None:
    agent, thread, goal = _named_goal_agent(tmp_path)

    with register_interruptible(conversation_request_interrupt_name(goal.task_id)):
        stopped = stop_named_conversation_work(agent, thread_id=thread.thread_id, kind="goal", name="周报整理")
        interrupted = is_interrupted()

    assert stopped.ok is True and stopped.task_id == goal.task_id
    assert interrupted is True
