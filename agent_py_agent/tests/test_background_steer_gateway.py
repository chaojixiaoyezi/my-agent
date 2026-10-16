"""真实后台 core、会话租约和隔离 HTTP 中的连续插话；供应商仅使用合成响应。"""
from __future__ import annotations

import json

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.conversation import BackgroundMainAgentRuntime
from agent_py_agent.agent.conversation.background_claim import (
    BackgroundClaimDependencies,
    run_claimed,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    read_gateway_input_receipt,
    reconcile_gateway_input_receipts,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.cli.chat_client_context import ActiveTurnInputDelivery, GatewayChatClientAgent
from agent_py_agent.cli.gateway_process import _build_gateway_auth_middleware
from agent_py_agent.tests.test_steer_delivery_recovery import _free_port, _gateway_agent


def test_two_tui_inputs_reach_running_background_before_any_compaction(tmp_path, monkeypatch):
    from types import SimpleNamespace

    agent, paths = _gateway_agent(tmp_path)
    store = agent.conversation_store
    conversation = {"canonical_user_id": "local-agent", "channel_user_id": "local-agent",
                    "channel": "chat", "channel_conversation_id": "sess-steer"}
    thread = store.threads.get_or_create(conversation)
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-a", "goal": "合成任务"})
    waiting = paths.processing / "request-b.json"
    write_json_file_atomic(waiting, {
        "id": "request-b", "kind": "ask", "status": "processing", "turn_phase": "open",
        "conversation": conversation, "user_id": "local-agent",
    })
    GatewayTaskBindingWriter(waiting, "request-b").bind_conversation_claim(thread.thread_id)
    port = _free_port()
    client = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(gateway_port=port), tmp_path / "ws",
                                    [tmp_path / "ws"], agent.home_paths)
    ingress, wires, targets = [], [], []

    # LLM: 仅替换供应商生成；消息经过真 HTTP、原输入账本、后台 core 和提交确认，不改被测运行结果。
    # 函数用途: 在第一次模型调用中模拟两条 TUI 插话，让生产循环自己在下一安全点认领。
    def generate(_self, prompt, *args, **kwargs):
        wires.append(json.dumps(kwargs.get("messages") or prompt, ensure_ascii=False))
        if len(wires) == 1:
            target = client.request_background_notices("sess-steer", after=0)["execution_target"]
            targets.append(target)
            for key, message in (("z-first", "补充甲"), ("a-second", "补充乙")):
                ingress.append(client.request_active_turn_input(
                    "sess-steer", message=message, message_id=key,
                    expected_turn_id=target["turn_id"], expected_execution=target,
                ))
            return ModelResponse(text="第一轮", backend="echo")
        return ModelResponse(text="已接收两条补充。", backend="echo")

    monkeypatch.setattr(EchoBackend, "generate", generate)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    dependencies = BackgroundClaimDependencies(
        claims=store.claims, claim_scope_id=lambda tid, _task: tid, child_owns_task=lambda _task: False,
        terminal_task=lambda _args: False, recovery_block=lambda _task: None, source_admission=lambda _wake: "",
        retire_source=lambda _args: None, run_once=runtime.run_once, runtime_facts=lambda: {},
        record_policy_failure=lambda _args: None, lease_seconds=60, heartbeat_interval_seconds=10,
    )
    server = GatewayHTTPServer(port, paths, params=GatewayHTTPServerParams(
        agent=agent, auth_middleware=_build_gateway_auth_middleware(agent.config),
    ))
    server.start()
    try:
        result = run_claimed(dependencies, {
            "thread_id": thread.thread_id, "task_id": link.task_id, "reason": "scheduled_progress_report",
        })
        # HTTP 服务不包含主循环；推进同一个生产回执对账 tick，不直接改回执。
        reconcile_gateway_input_receipts(paths, agent)
        statuses = [client.request_active_turn_input_status(item.request_id) for item in ingress]
    finally:
        server.stop()
    assert result is not None and targets[0]["kind"] == "task" and targets[0]["task_id"] == link.task_id
    assert targets[0]["runtime_authority"]["attempt_id"]
    assert len(wires) == 2 and all(word not in wires[0] for word in ("补充甲", "补充乙"))
    assert wires[1].index("补充甲") < wires[1].index("补充乙")
    assert all(item.delivery is ActiveTurnInputDelivery.ACCEPTED for item in statuses), [
        (status.delivery, read_gateway_input_receipt(paths, item.request_id).state,
         read_gateway_input_receipt(paths, item.request_id).reconcile_error)
        for item, status in zip(ingress, statuses, strict=True)
    ]
    assert all(read_gateway_input_receipt(paths, item.request_id).state == "consumed" for item in ingress)
    assert all(not (paths.inbox / f"{item.request_id}.json").exists() for item in ingress)
    assert store.guidance.pending("request", "request-b") == []
    assert store.threads.load(thread.thread_id).compact_generation == 0
