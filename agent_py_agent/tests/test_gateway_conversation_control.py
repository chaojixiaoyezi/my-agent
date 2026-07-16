from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interruptible,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_delivery_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import (
    _GatewayTaskBindingWriter,
    _handle_gateway_request,
)
from agent_py_agent.agent.settings import AgentConfig


def _request(request_id: str, *, user: str = "u-1", conversation_id: str = "c-1") -> dict:
    return {
        "id": request_id,
        "request_id": request_id,
        "kind": "ask",
        "goal": "整理今天的资料",
        "created_at": time.time() - 30,
        "lease_started_at": time.time() - 20,
        "status": "processing",
        "user_id": user,
        "metadata": {"user_id": user, "channel": "feishu"},
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": user,
            "canonical_user_id": user,
        },
    }


def _scope(user: str = "u-1", conversation_id: str = "c-1") -> GatewayControlScope:
    return GatewayControlScope(user, "feishu", conversation_id)


def _command(text: str):
    command = parse_conversation_control(text)
    assert command is not None
    return command


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _bind_durable_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    user: str = "u-1",
    conversation_id: str = "c-1",
    goal: str = "整理持久后台资料",
):
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": user,
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": user,
            "now": time.time() - 40,
        }
    )
    link = agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": goal,
            "status": "active",
            "now": time.time() - 30,
        }
    )
    return thread, link


def test_btw_targets_only_current_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = execute_gateway_conversation_control(agent, paths, _command("/btw 先核对来源"), _scope())

    assert result.ok is True
    assert result.request_id == "req-1"
    assert agent.conversation_store.pending_guidance("request", "req-1")[0].message == "先核对来源"
    assert agent.conversation_store.pending_guidance("request", "req-2") == []


def test_goal_lifecycle_is_persistent_and_conversation_scoped(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)

    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 连续整理七天资料"), _scope()
    )
    viewed = execute_gateway_conversation_control(agent, paths, _command("/goal"), _scope())
    other_user = execute_gateway_conversation_control(
        agent, paths, _command("/goal"), _scope(user="u-2", conversation_id="c-2")
    )

    assert created.ok is True
    assert created.request_id.startswith("goal-task-")
    assert "连续整理七天资料" in viewed.message
    assert "运行中" in viewed.message
    assert other_user.message == "当前没有持续目标。"
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    assert thread is not None
    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert goal is not None and goal.task_id == created.request_id and goal.status == "active"
    assert any(
        wake.reason == "thread_goal_continue" and wake.root_task_id == goal.task_id
        for wake in agent.conversation_store.pending_wake_signals()
    )

    paused = execute_gateway_conversation_control(
        agent, paths, _command("/goal pause"), _scope()
    )
    assert paused.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "paused"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"

    resumed = execute_gateway_conversation_control(
        agent, paths, _command("/goal resume"), _scope()
    )
    assert resumed.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "active"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "active"

    edited = execute_gateway_conversation_control(
        agent, paths, _command("/goal edit 改为连续整理十四天资料"), _scope()
    )
    assert edited.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).objective == "改为连续整理十四天资料"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].goal == "改为连续整理十四天资料"

    cleared = execute_gateway_conversation_control(
        agent, paths, _command("/goal clear"), _scope()
    )
    assert cleared.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "cleared"
    assert execute_gateway_conversation_control(
        agent, paths, _command("/goal"), _scope()
    ).message == "当前没有持续目标。"


def test_goal_rejects_second_unfinished_goal(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    first = execute_gateway_conversation_control(
        agent, paths, _command("/goal 第一件长期工作"), _scope()
    )
    second = execute_gateway_conversation_control(
        agent, paths, _command("/goal 第二件长期工作"), _scope()
    )

    assert first.ok is True
    assert second.ok is False
    assert "已有未结束" in second.message


def test_stop_pauses_active_goal_without_deleting_it(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )

    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert created.ok is True and stopped.ok is True
    assert goal is not None and goal.status == "paused"
    assert goal.task_id == created.request_id


def test_btw_on_goal_keeps_goal_continuation_reason(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )

    steered = execute_gateway_conversation_control(
        agent, paths, _command("/btw 先处理今天新增的数据"), _scope()
    )

    assert created.ok is True and steered.ok is True
    wakes = [
        item
        for item in agent.conversation_store.pending_wake_signals()
        if item.root_task_id == created.request_id
    ]
    assert wakes
    assert all(item.reason == "thread_goal_continue" for item in wakes)
    assert any(item.metadata.get("guidance_id") for item in wakes)


def test_btw_follows_durable_task_after_initial_request_finished(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-background")
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-chat.json", _request("req-chat"))

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 最终预算控制在四百元内"),
        _scope(),
    )

    assert result.ok is True
    assert result.request_id == "req-background"
    assert agent.conversation_store.pending_guidance("request", "req-chat") == []
    guidance = agent.conversation_store.pending_guidance("task", "req-background")
    assert [item.message for item in guidance] == ["最终预算控制在四百元内"]
    wakes = agent.conversation_store.pending_wake_signals()
    assert any(
        item.thread_id == thread.thread_id
        and item.root_task_id == "req-background"
        and item.reason == "user_guidance"
        for item in wakes
    )
    deliver, reason = _background_delivery_decision(
        agent,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="req-background",
            reason="user_guidance",
        ),
        content="我已看到补充要求，接下来继续处理。",
    )
    assert deliver is False
    assert reason == "user_guidance_applied_internal"


def test_selected_task_is_persisted_on_the_live_gateway_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-turn.json"
    write_json_file(request_path, _request("req-turn"))
    thread, link = _bind_durable_task(agent, "task-existing")
    agent._current_run_params = RunParams(
        request_id="req-turn",
        run_id="req-turn",
        task_id="req-turn",
        task_attributes={"conversation_thread_id": thread.thread_id},
        conversation_task_binding_callback=_GatewayTaskBindingWriter(request_path, "req-turn"),
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            select_current_conversation_task,
        )

        selected = select_current_conversation_task(agent, link.task_id)
    finally:
        del agent._current_run_params

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert selected is not None
    assert payload["conversation_runtime"] == {
        "thread_id": thread.thread_id,
        "task_id": "task-existing",
        "task_path": "",
    }


def test_linked_live_request_controls_exact_task_and_status_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-selected", goal="较早的总任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-unrelated-newer",
            "goal": "不应被本轮控制选中的任务",
            "status": "active",
            "now": time.time() + 30,
        }
    )
    payload = _request("req-current-turn")
    payload["goal"] = "继续完成当前第五步"
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-selected",
        "task_path": "",
    }
    write_json_file(paths.processing / "req-current-turn.json", payload)

    steered = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 先把当前第五步的兼容性补齐"),
        _scope(),
    )
    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert steered.ok is True and steered.request_id == "task-selected"
    assert [
        item.message for item in agent.conversation_store.pending_guidance("task", "task-selected")
    ] == ["先把当前第五步的兼容性补齐"]
    assert agent.conversation_store.pending_guidance("task", "task-unrelated-newer") == []
    assert agent.conversation_store.pending_wake_signals() == []
    assert result.request_id == "task-selected"
    assert result.status is not None
    assert result.status.task == "继续完成当前第五步"


def test_stop_linked_durable_task_also_interrupts_live_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-root")
    request_path = paths.processing / "req-live.json"
    payload = _request("req-live")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-root",
        "task_path": "",
    }
    write_json_file(request_path, payload)
    ready = [threading.Event(), threading.Event()]
    observed = [threading.Event(), threading.Event()]

    def worker(index: int, request_id: str) -> None:
        with register_interruptible(f"conversation-request:{request_id}"):
            ready[index].set()
            while not is_interrupted():
                time.sleep(0.01)
            observed[index].set()

    workers = [
        threading.Thread(target=worker, args=(0, "task-root")),
        threading.Thread(target=worker, args=(1, "req-live")),
    ]
    for item in workers:
        item.start()
    assert all(item.wait(timeout=2) for item in ready)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    for item in workers:
        item.join(timeout=2)

    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    stopped_payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert result.ok is True and result.request_id == "task-root"
    assert all(item.is_set() for item in observed)
    assert links["task-root"].status == "interrupted"
    assert stopped_payload["cancel_requested"] is True


def test_btw_expected_task_check_rejects_task_switch_race(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "task-old")
    append_guidance = agent.conversation_store.append_guidance

    def append_then_switch(request):
        entry = append_guidance(request)
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": "task-new",
                "goal": "用户刚刚启动的新任务",
                "status": "active",
                "now": time.time() + 10,
            }
        )
        return entry

    monkeypatch.setattr(agent.conversation_store, "append_guidance", append_then_switch)

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 只应用到我发送时看到的当前任务"),
        _scope(),
    )

    assert result.ok is False
    assert result.request_id == "task-old"
    assert "已切换" in result.message
    assert agent.conversation_store.pending_guidance("task", "task-old") == []
    assert agent.conversation_store.pending_guidance("task", "task-new") == []
    assert not any(item.reason == "user_guidance" for item in agent.conversation_store.pending_wake_signals())


def test_status_uses_typed_facts_without_guidance_history(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    chunk_path = gateway_chunk_path(paths, "req-1")
    chunk_path.write_text(
        '{"kind":"tool_progress","progress":{"tool":"run_command","status":"完成","detail":"secret"}}\n',
        encoding="utf-8",
    )
    agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert "状态：运行中" in result.message
    assert "最近进展：刚完成一个执行步骤" in result.message
    assert "MiniMax-M2.7" in result.message
    assert "run_command" not in result.message
    assert "secret" not in result.message
    assert "引导" not in result.message


def test_status_follows_durable_task_after_initial_request_finished(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    _bind_durable_task(agent, "req-background", goal="整理一周入职方案")
    agent.subagents.create_run(
        goal="整理每日安排",
        thought="先列清单",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-background"},
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert result.request_id == "req-background"
    assert result.status is not None
    assert result.status.state == "running"
    assert result.status.task == "整理一周入职方案"
    assert result.status.subagent_total == 1
    assert result.status.subagent_running == 1
    assert "状态：运行中" in result.message
    assert "子代理 1" in result.message


def test_stop_persists_and_signals_only_matching_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-1.json"
    write_json_file(request_path, _request("req-1"))
    ready = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-1"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            observed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    thread.join(timeout=2)

    assert result.ok is True
    assert observed.is_set()
    payload = request_path.read_text(encoding="utf-8")
    assert '"cancel_requested": true' in payload
    assert '"control_status": "stopping"' in payload


def test_stop_interrupts_durable_task_and_cancels_only_current_children(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-background")
    child = agent.subagents.create_run(
        goal="整理子目录",
        thought="先检查",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-background"},
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
        }
    )
    agent.local_store.task_registry.register_task(
        "req-background",
        status="running",
        goal="整理持久后台资料",
    )
    ready = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-background"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            observed.set()

    thread_worker = threading.Thread(target=worker)
    thread_worker.start()
    assert ready.wait(timeout=2)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    thread_worker.join(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and agent.subagents.load(child.id).status != "CANCELLED":
        time.sleep(0.01)

    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert result.ok is True
    assert result.request_id == "req-background"
    assert observed.is_set()
    assert links["req-background"].status == "interrupted"
    assert links[child.id].status == "cancelled"
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.local_store.task_registry.lookup_task("req-background")["status"] == "interrupted"
    deliver, reason = _background_delivery_decision(
        agent,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="req-background"),
        content="这是一条迟到的旧完成回复",
    )
    assert deliver is False
    assert reason == "task_interrupted"


def test_status_and_stop_follow_typed_request_lineage_to_subagents(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    child = agent.subagents.create_run(
        goal="整理子目录",
        thought="先检查",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-1"},
    )

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and agent.subagents.load(child.id).status != "CANCELLED":
        time.sleep(0.01)

    assert status.status is not None
    assert status.status.subagent_total == 1
    assert status.status.subagent_running == 1
    assert stopped.ok is True
    assert agent.subagents.load(child.id).status == "CANCELLED"


def test_stop_does_not_recreate_request_that_finished_during_control(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic as real_update

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-finished.json"
    write_json_file(request_path, _request("req-finished"))

    def finish_then_update(path, updater, **kwargs):
        path.unlink()
        return real_update(path, updater, **kwargs)

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_service.update_json_file_atomic",
        finish_then_update,
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    assert result.ok is False
    assert "刚刚结束" in result.message
    assert not request_path.exists()


def test_interrupted_request_finishes_without_model_execution(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-cancelled.json"
    payload = _request("req-cancelled")
    payload["cancel_requested"] = True
    write_json_file(request_path, payload)
    monkeypatch.setattr(agent, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")))

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is True
    assert response["status"] == "interrupted"
    assert response["error_code"] == "INTERRUPTED"
    assert response["response"] == "当前任务已停止。"


def test_control_fails_closed_for_other_user(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope(user="u-1"))

    assert result.ok is False
    assert result.request_id == ""


def test_http_control_endpoint_returns_immediate_conversation_status(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "command": "/status",
                "user_id": "u-1",
                "channel": "feishu",
                "conversation_id": "c-1",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["kind"] == "status"
    assert payload["request_id"] == "req-1"
    assert "状态：运行中" in payload["message"]
