# LLM: 回归原领域控制到规范回执、HTTP轮询和durable投递的接缝；只替换HTTP/provider IO，不伪造accepted或执行身份。
# 模块用途: 确认同步控制结果可回送、claimed只等待，并保留未知/非法状态与身份冲突的隔离门。
from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.adapter.delivery import (
    GatewayControlReceiptResult,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    GatewayReplyReceiptCallbacks,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.manager import (
    ChannelManager,
    GatewayAskSubmission,
    _control_submission_needs_watcher,
    _validate_control_receipt_submission,
)
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
    registered_worker_heartbeat,
)
from agent_py_agent.agent.gateway_parts.multiprocess_control_worker import WorkerControlPump
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
from agent_py_agent.agent.gateway_parts.paths import gateway_paths, gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_adapter_manager import ProviderIdempotentDummyAdapter

pytestmark = pytest.mark.usefixtures("gateway_client_credential")


# LLM: 用现有owner配置/原控制入口生成耐久结果，禁用模型卡仅避免provider网络，不改授权或结果字段。
# 函数用途: 创建真实群owner Agent并执行一次带稳定消息身份的同步控制。
def completed_case(tmp_path, text, *, enabled=True):
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "capability_config.yaml").write_text(
        f"im_model_cards_enabled: false\nim_task_delegation_enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", enable_subagents=True,
        gateway_per_user_owner_scoping=True,
        my_agent_owner_provider="feishu", my_agent_owner_kind="group",
        my_agent_owner_id="oc_group",
    ), workspace)
    scope = GatewayControlScope(
        user_id="ou_actor", channel="feishu", conversation_id="oc_group",
        metadata={"message_id": "om_control", "channel_chat_type": "group",
                  "channel_chat_id": "oc_group"},
    )
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.valid
    paths = gateway_paths(agent)
    receipt = ops.execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
    actual = ops.read_gateway_control_operation(paths, receipt.operation_id, read_only=True)
    assert actual.operation_id == receipt.operation_id
    assert actual.state == receipt.state == "completed" and actual.result
    assert actual.result["kind"] == receipt.result["kind"]
    assert actual.result["ok"] is receipt.result["ok"]
    return actual


# LLM: fake只代替认证HTTP和provider IO，JSON来自真实projection，manager/worker/store和身份核验仍是原实现。
# 函数用途: 组装同operation ID的投递链并记录实际provider发送，调用方可推进规范结果快照。
def delivery_case(tmp_path, monkeypatch, receipt):
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    attempts, messages = [], {}
    adapter = ProviderIdempotentDummyAdapter(messages, attempts)
    adapter.adapter_name = "feishu"
    manager.register_adapter(adapter)
    current = [receipt]
    seen = []

    def open_request(request, *, timeout):
        assert timeout == 5
        assert request.full_url.endswith("/control-status/" + receipt.operation_id)
        assert request.get_header("X-user-id") == receipt.user_id
        assert request.get_header("X-channel") == receipt.channel
        seen.append(request.full_url)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            ops.gateway_control_operation_status_payload(current[0])
        ).encode()
        return response

    monkeypatch.setattr("agent_py_agent.agent.adapter.manager.open_authenticated_request", open_request)
    pending = PendingGatewayReply(
        request_id=receipt.expected_turn_id, operation_id=receipt.operation_id,
        receipt_id=receipt.operation_id, control_kind=receipt.command_kind,
        channel=receipt.channel, user_id=receipt.user_id,
        message_id=receipt.client_message_id, conversation_id=receipt.conversation_id,
        channel_chat_type=receipt.channel_chat_type, channel_chat_id=receipt.channel_chat_id,
        watch_kind="control_receipt", progress_handle="control-placeholder",
    )
    manager._reply_delivery.enqueue(pending)
    return manager._reply_delivery, pending, current, attempts, messages, seen


@pytest.mark.parametrize("text,expected_ok,enabled", [
    ("/delegate enable", True, True), ("/delegate status", True, True),
    ("/delegate enable", False, False), ("/help", True, True),
    ("/status", True, True), ("/schedule", True, True),
    ("/schedule missing-id", False, True), ("/model", True, True),
    ("/model missing-profile", False, True), ("/group show", True, True),
    ("/group invalid", False, True), ("/stop", False, True),
])
def test_real_completed_control_reaches_provider_once(tmp_path, monkeypatch, text, expected_ok, enabled):
    receipt = completed_case(tmp_path, text, enabled=enabled)
    assert receipt.result["ok"] is expected_ok
    assert "delivery_status" not in receipt.result
    payload = ops.gateway_control_operation_status_payload(receipt)
    assert payload["delivery_status"] == "accepted"
    assert payload["ok"] is expected_ok
    assert payload["operation_id"] == receipt.operation_id
    assert payload["receipt_id"] == receipt.operation_id
    worker, pending, _current, attempts, messages, seen = delivery_case(tmp_path, monkeypatch, receipt)
    assert worker.run_once() == 1
    assert seen and worker.store.pending() == []
    terminal = worker.store.terminal_receipt(receipt.operation_id)
    assert terminal["disposition"] == "sent"
    assert len(attempts) == len(messages) == 1
    # 原通道会脱敏当前群身份；完整相等检查必须使用该已有公开投影。
    from agent_py_agent.agent.conversation.channels import (
        DeliveryContext,
        redact_delivery_context_identifiers,
    )
    expected = redact_delivery_context_identifiers(receipt.result["message"], DeliveryContext(
        channel=receipt.channel, target=receipt.user_id, conversation_id=receipt.conversation_id,
    ))
    assert next(iter(messages.values()))[1] == expected
    worker.enqueue(pending)
    assert worker.run_once() == 0 and len(attempts) == 1


@pytest.mark.parametrize("value", ["", None, "invalid", "unknown", "accepted", "rejected"])
def test_explicit_delivery_value_is_never_normalized(tmp_path, value):
    original = completed_case(tmp_path, "/help")
    receipt = replace(original, result={**original.result, "delivery_status": value})
    assert ops.gateway_control_operation_status_payload(receipt)["delivery_status"] == value
    assert receipt.result["delivery_status"] == value


@pytest.mark.parametrize("kind,state,result", [
    ("steer", "completed", {"kind": "steer", "ok": True, "message": "合成回执", "request_id": ""}),
    ("help", "completed", {}), ("help", "terminal_unknown", {}),
    ("help", "prepared", {}), ("help", "claimed", {}), ("help", "executing", {}),
])
def test_unsettled_or_missing_result_does_not_gain_acceptance(tmp_path, kind, state, result):
    original = completed_case(tmp_path, "/help")
    payload = ops.gateway_control_operation_status_payload(replace(
        original, command_kind=kind, state=state, result=result,
    ))
    assert payload.get("delivery_status") != "accepted"


@pytest.mark.parametrize("value,disposition", [("", "quarantined"), (None, "quarantined"),
    ("invalid", "quarantined"), ("unknown", None)])
def test_explicit_unsafe_result_never_crosses_provider(tmp_path, monkeypatch, value, disposition):
    receipt = completed_case(tmp_path, "/help")
    receipt = replace(receipt, result={**receipt.result, "delivery_status": value})
    worker, _pending, _current, attempts, _messages, _seen = delivery_case(tmp_path, monkeypatch, receipt)
    assert worker.run_once() == 0 and attempts == []
    terminal = worker.store.terminal_receipt(receipt.operation_id)
    if disposition is None:
        assert terminal is None and len(worker.store.pending()) == 1
    else:
        assert terminal["disposition"] == disposition


@pytest.mark.parametrize("field,value", [("operation_id", "other-operation"),
    ("receipt_id", "other-receipt"), ("request_id", "other-target"), ("kind", "stop")])
def test_projection_identity_conflict_is_not_authorized(tmp_path, monkeypatch, field, value):
    receipt = completed_case(tmp_path, "/help")
    if field == "request_id":
        receipt = replace(receipt, expected_turn_id="original-target",
                          result={**receipt.result, "request_id": "original-target"})
    payload = ops.gateway_control_operation_status_payload(receipt)
    worker, _pending, _current, attempts, _messages, _seen = delivery_case(tmp_path, monkeypatch, receipt)
    monkeypatch.setattr(ops, "gateway_control_operation_status_payload", lambda _: {**payload, field: value})
    assert worker.run_once() == 0 and attempts == []
    assert worker.store.terminal_receipt(receipt.operation_id)["disposition"] == "quarantined"


def test_actual_claimed_waits_then_worker_completed_delivers_once(tmp_path, monkeypatch):
    paths, prepared, _scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch)
    pump = WorkerControlPump(frozen, pool)
    held, calls = [], []
    future = Future()

    def hold_submit(_executor, operation, receipt):
        held.append((operation, receipt))
        return future

    def execute(_agent, _paths, command, _bound):
        calls.append(command.kind)
        return ConversationControlResult(command.kind, False, "合成领域拒绝", error_code="SYNTHETIC_DENIAL")

    monkeypatch.setattr(pump, "_submit", hold_submit)
    monkeypatch.setattr(ops, "execute_gateway_conversation_control", execute)
    try:
        assert pump.poll() == 1 and calls == []
        claimed = ops.read_gateway_control_operation(paths, prepared.operation_id, read_only=True)
        assert claimed.state == "claimed" and claimed.result == {} and claimed.dispatch["executor"]
        payload = ops.gateway_control_operation_status_payload(claimed)
        submission = GatewayAskSubmission(
            request_id="", operation_id=claimed.operation_id, receipt_id=claimed.operation_id,
            control_state=claimed.state, delivery_status="",
        )
        _validate_control_receipt_submission(submission)
        assert _control_submission_needs_watcher(submission)
        assert payload["delivery_status"] == "unknown"
        worker, pending, current, attempts, _messages, _seen = delivery_case(tmp_path, monkeypatch, claimed)
        assert worker.run_once() == 0 and attempts == []
        assert worker.store.terminal_receipt(claimed.operation_id) is None
        assert len(worker.store.pending()) == 1
        held[0][0](held[0][1])
        future.set_result(None)
        completed = ops.read_gateway_control_operation(paths, prepared.operation_id, read_only=True)
        assert completed.state == "completed" and completed.result["ok"] is False
        assert "delivery_status" not in completed.result and calls == ["help"] and owners
        current[0] = completed
        assert worker.run_once() == 1
        assert worker.store.terminal_receipt(completed.operation_id)["disposition"] == "sent"
        worker.enqueue(pending)
        assert worker.run_once() == 0 and len(attempts) == 1
    finally:
        if not future.done():
            future.set_result(None)
        pump.close()


@pytest.mark.parametrize("state", ["prepared", "claimed", "executing", "completed", "terminal_unknown"])
def test_all_canonical_control_states_have_matching_adapter_schema(state):
    result = GatewayControlReceiptResult(control_state=state)
    assert result.control_state == state
    submission = GatewayAskSubmission(request_id="", operation_id="op-original", receipt_id="op-original",
                                      control_state=state)
    _validate_control_receipt_submission(submission)
    assert _control_submission_needs_watcher(submission) is (state != "completed")


# LLM: 沿原C generation/dispatch和W bootstrap/内核出生登记建立claimed；只冻结线程提交时机，不伪造回执认领或权限。
# 函数用途: 构造可由原W认领的合成飞书控制，目标ID遵守实际provider格式。
def prepared_case(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    owner = OwnerIdentity.provider_user("feishu", "ou_actor")
    scope = GatewayControlScope(user_id="ou_actor", channel="feishu", conversation_id="oc_group",
                                metadata={"message_id": "om_control"}, resolved_owner=owner,
                                workspace=str(tmp_path / "workspace"))
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "control-generation")
        receipt = ops.execute_gateway_control_operation(SimpleNamespace(), paths,
                    parse_conversation_control("/help"), scope, command_text="/help")
    launch = mode.WorkerLaunch(WorkerPartition(owner_worker(owner, 2), 2, "control-generation"),
                              paths.root, tmp_path / "home", os.getppid(), "control-instance")
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    heartbeat = registered_worker_heartbeat(launch, launch.instance_id,
                                           {"ready": True, "gateway_ports": [43210]}, time.time())
    registry = {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": launch.partition.c_run_id,
                "worker_count": 2, "process_identity": heartbeat["process_identity"], "http_port": 43210,
                "workers": [{"worker": launch.partition.index, "instance_id": launch.instance_id,
                             "process_identity": heartbeat["process_identity"],
                             "executor_ledger_ref": f"workers/executors/w{launch.partition.index}"}]}
    write_json_file_atomic(paths.root / "workers" / "registry.json", registry)
    write_json_file_atomic(paths.root / "workers" / f"w{launch.partition.index}.json", heartbeat)
    owners = []
    pool = SimpleNamespace(get=lambda selected: (owners.append(selected), SimpleNamespace())[1],
                           pin=lambda _: nullcontext())
    return paths, receipt, scope, freeze_worker_bootstrap(launch), pool, owners


def test_claimed_never_sends_even_with_inconsistent_terminal_delivery_value():
    store, sent = GatewayReplyDeliveryStore(None), []
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: pytest.fail("wrong result route"),
        deliver_response=lambda record, text: sent.append((record, text)) is None,
        receipt_callbacks=GatewayReplyReceiptCallbacks(poll_control=lambda _: GatewayControlReceiptResult(
            control_state="claimed", delivery_status="accepted", message="合成矛盾状态")))
    pending = PendingGatewayReply(request_id="", operation_id="op-original", receipt_id="op-original",
        control_kind="help", channel="feishu", user_id="ou_actor", message_id="om_control",
        conversation_id="oc_group", watch_kind="control_receipt")
    worker.enqueue(pending)
    assert worker.run_once() == 0 and sent == []
    assert store.terminal_receipt(pending.operation_id) is None and len(store.pending()) == 1
