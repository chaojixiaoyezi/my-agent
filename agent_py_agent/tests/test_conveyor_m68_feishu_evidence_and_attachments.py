"""M68 的输入证据链与现有任务文件投递回归。"""

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore
from agent_py_agent.agent.agent_core.runtime.minimal_tool_audit import archive_minimal_tool_audit
from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
from agent_py_agent.agent.artifacts import registry
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.common.nofollow_fs import _supports_dir_fd
from agent_py_agent.agent.conversation import input_dependencies
from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
from agent_py_agent.agent.conversation.store_wake_publication import published_signal
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_channel_message_tool import _tool
from agent_py_agent.tests.test_fc1_credential_continuation import _dm_case
from agent_py_agent.tests.test_web_fetch_ssrf import _tool as _web_tool


def _simulated_feishu(monkeypatch, root):
    sends = []
    adapter = feishu.FeishuAdapter({"feishu_session_lock_enabled": False}, workspace_root=root.parent)
    monkeypatch.setattr(adapter, "_get_tenant_access_token", lambda: "fixture-token")

    def post(method, path, body, token):
        assert method == "POST" and token == "fixture-token"
        sends.append((path, body))
        return {"code": 0, "data": {"message_id": "om_m68_fixture"}}

    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry", post)
    channels = ChannelAdapterRegistry(delivery_state_root=root)
    channels.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(
        text=True, reply=True, proactive=True, files=True, frozen_attachment_content=True,
        component_receipts=True, reply_policy=True,
    ))
    return DeliveryService(channels), sends


@pytest.mark.skipif(not _supports_dir_fd(), reason="本平台不支持严格目录 FD 核验")
def test_shell_created_attachment_reaches_delivery(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    task = owner / "tasks" / "current"
    path = task / "output" / "report.bin"
    path.parent.mkdir(parents=True)
    subprocess.run(["dd", "if=/dev/zero", "of=output/report.bin", "bs=1048576", "count=2"],
                   cwd=task, capture_output=True, check=True)
    prototype, _adapter = _tool(owner)
    prototype.agent.delivery_service, sends = _simulated_feishu(monkeypatch, tmp_path / "delivery")
    tool = SendMessageTool(prototype.agent)
    uploads = []

    def upload(content, name, kind, fields, token):
        assert name == "report.bin" and kind == "file" and token == "fixture-token"
        uploads.append((hashlib.sha256(content).hexdigest(), len(content)))
        return "file_m68_fixture"

    monkeypatch.setattr(feishu, "_upload_feishu_bytes", upload)
    result = tool.execute({
        "attachments": ["output/report.bin"],
        "__artifact_task_root": str(task),
        "__artifact_registry_root": str(task),
    })
    assert result.ok, (result.error_code, result.output)
    assert len(sends) == 1 and sends[0][1]["receive_id"] == "ou_current_user"
    record, = registry.latest_artifact_records(task).values()
    assert uploads == [(record.sha256, 2 * 1024 * 1024)]
    component, = json.loads(result.output)["component_receipts"]
    assert (component["phase"], component["upload_ref"], component["provider_message_id"]) == (
        "send_confirmed", "file_m68_fixture", "om_m68_fixture",
    )
    # The evidence names the full spool record; the 20-character receipt_id is display only.
    delivery_ref = result.result_envelope["delivery_evidence"]["delivery_ref"]
    assert delivery_ref.startswith("host-delivery:") and delivery_ref[len("host-delivery:"):][:20] == (
        json.loads(result.output)["receipt_id"])
    spool = GatewayReplyDeliveryStore(owner / "data" / "channel-delivery").terminal_receipt(delivery_ref)
    assert spool["disposition"] == "sent"
    path.write_bytes(b"file changed after registration")
    again = tool.execute({
        "message": "attempt new delivery",
        "attachments": ["output/report.bin"],
        "__artifact_task_root": str(task), "__artifact_registry_root": str(task),
    })
    assert again.error_code == "ARTIFACT_VALIDATION_FAILED" and len(sends) == len(uploads) == 1
    assert registry.latest_artifact_records(task)[record.artifact_id] == record


@pytest.mark.skipif(not _supports_dir_fd(), reason="本平台不支持严格目录 FD 核验")
def test_registration_resamples_versions_and_refuses_other_sources(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    task = owner / "task"
    path = task / "output" / "report.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first version")
    descriptor = registry.ExistingFileSource(registry.ArtifactRegistration(task, path))
    original_sha256 = hashlib.sha256
    updates = []

    class ChangeOnRead:
        def __init__(self):
            self.digest = original_sha256()

        def update(self, chunk):
            if not updates:
                path.write_bytes(b"second version")
            updates.append(chunk)
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    with monkeypatch.context() as patch:
        patch.setattr(registry.hashlib, "sha256", lambda data=None: (
            ChangeOnRead() if data is None else original_sha256(data)
        ))
        record = registry.register_existing_file(owner, descriptor, authorized_source=str(path.parent))
    assert updates == [b"first version", b"second version"]
    assert record.sha256 == original_sha256(b"second version").hexdigest()

    outside = owner / "unauthorized.bin"
    outside.write_bytes(b"other source")
    link = path.with_name("alias.bin")
    link.symlink_to(outside)
    for rejected in (outside, link):
        descriptor = registry.ExistingFileSource(registry.ArtifactRegistration(task, rejected))
        with pytest.raises((ValueError, OSError)):
            registry.register_existing_file(owner, descriptor, authorized_source=str(path.parent))
    assert list(registry.latest_artifact_records(task)) == [record.artifact_id]


def _verified_credential_case(tmp_path, monkeypatch):
    query = getattr(input_dependencies, "credential_evidence_chain", None)
    assert callable(query), "credential evidence chain query is absent"
    ctx = _dm_case(tmp_path)
    inputs = input_dependencies.input_dependency_access(ctx.agent)
    url = "https://m68.example.com/protected"
    ref, _ = request_credential_card(ctx.agent, url=url, authentication="basic",
                                     options=CredentialCardOptions(access_scope="task"))
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context="test").payload
    input_id = row["input_id"]
    input_dependencies.arm_input_dependency(ctx.agent, input_id)
    with monkeypatch.context() as patch:
        patch.setattr(input_dependencies.InputDependencyAccess, "publish", lambda self, row, **_: row)
        assert resolve_private_interaction(ctx.agent, {
            "reference": row["reference"], "decision": "submit",
            "message_id": row["message_id"], "chat_id": row["chat_id"],
            "fields": {"username": "synthetic-m68-user", "secret": "SyntheticM68Password"},
        }, channel="feishu", actor_id=ctx.actor.actor_id)["ok"]

    saved = query(inputs, input_id)
    assert saved == {"state": "saved", "input_id": input_id, "generation": row["generation"]}
    inputs.publish(inputs.read(input_id))
    woken = query(inputs, input_id)
    assert woken == {**saved, "state": "woken", "wake_id": woken["wake_id"]}
    wake = published_signal(inputs.store.storage, ctx.thread.thread_id, "task-input:" + input_id)
    params = replace(ctx.agent._current_run_params,
                     turn_trigger=lifecycle_wake_turn_trigger(wake.reason, wake.to_dict(), ()))
    ctx.agent._current_run_params = params

    def protected_transport(request, **_kwargs):
        assert request.authorization_origin == "https://m68.example.com:443"
        assert request.headers["Authorization"].startswith("Basic ")
        return RawResponseParts(200, {"Content-Type": "application/json"}, b'{"status":"sample"}', request.url)

    web = _web_tool()
    web.raw_fetcher = protected_transport
    web.credential_authenticator = lambda ref, request, send: credential_authenticated_request(
        ctx.agent, ref, request, send=send,
    )
    outcome = web.execute({"url": url, "credential_ref": ref})
    assert outcome.ok, outcome.output
    result = ToolResult("m68-call", "web_fetch", "succeeded", handler_executed=True,
                        metadata={"handler_details": outcome.result_envelope})
    record = {"tool": result.tool_name, "id": result.call_id, "ok": result.ok,
              "handler_executed": result.handler_executed, "tool_result_envelope": _compact_result_envelope(result)}
    archive_minimal_tool_audit(ctx.agent, params, record)
    with Path(record["raw_archive_path"]).open(encoding="utf-8") as handle:
        event = next(row for row in map(json.loads, handle) if row["event_id"] == record["raw_archive_event_id"])
    verified = query(inputs, input_id, tool_event=event)
    assert verified["state"] == "verified"
    assert verified["model_request_id"] == params.request_id
    assert verified["protected_resource_check"]["http_status"] == 200
    assert "SyntheticM68Password" not in json.dumps(event)
    assert query(inputs, input_id, tool_event={**event, "request_id": "other-request"}) == woken
    stale = json.loads(event["content_preview"])
    stale["protected_resource_check"]["generation"] += 1
    assert query(inputs, input_id, tool_event={**event, "content_preview": json.dumps(stale)}) == woken
    return SimpleNamespace(ctx=ctx, inputs=inputs, input_id=input_id, wake=wake, event=event,
                           verified=verified, query=query)


def test_credential_evidence_chain_stops_at_last_proven_stage(tmp_path, monkeypatch):
    case = _verified_credential_case(tmp_path, monkeypatch)
    ctx, inputs, input_id, wake, event, verified, query = (
        case.ctx, case.inputs, case.input_id, case.wake, case.event, case.verified, case.query)
    delivery_root = tmp_path / "delivery"
    store = GatewayReplyDeliveryStore(delivery_root)
    assert query(inputs, input_id, tool_event=event, delivery_store=store) == verified
    context = DeliveryContext(
        channel="feishu", target=ctx.actor.actor_id, mode="proactive",
        thread_id=ctx.thread.thread_id, task_id=wake.root_task_id,
        idempotency_key="background-wake:" + wake.wake_signal_id,
    )
    reply = ReplyEnvelope(content="M68 synthetic reply")
    other_root = tmp_path / "other-delivery"
    other_service, _ = _simulated_feishu(monkeypatch, other_root)
    assert other_service.deliver(replace(context, target="ou_unrelated"), reply).delivery_status == "sent"
    assert query(inputs, input_id, tool_event=event, delivery_store=GatewayReplyDeliveryStore(other_root)) == verified

    service, sends = _simulated_feishu(monkeypatch, delivery_root)
    receipt = service.deliver(context, reply)
    assert receipt.delivery_status == "sent" and sends
    inputs.store.wakes.mark_handled(wake.wake_signal_id)
    assert query(inputs, input_id, tool_event=event, delivery_store=store) == {
        **verified, "state": "delivered", "delivery_id": "host-delivery:" + context.idempotency_key,
    }


def test_explicit_send_message_reply_completes_the_credential_chain(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.delivery import deliver_host_envelope
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        mirror_message_tool_deliveries,
    )
    from agent_py_agent.agent.conversation.background_im_progress import background_delivery_context

    case = _verified_credential_case(tmp_path, monkeypatch)
    ctx, inputs, input_id, wake, event, verified, query = (
        case.ctx, case.inputs, case.input_id, case.wake, case.event, case.verified, case.query)
    delivery_root = tmp_path / "delivery"
    store = GatewayReplyDeliveryStore(delivery_root)
    service, sends = _simulated_feishu(monkeypatch, delivery_root)
    # The tool sends through the host spool under its business key; no wake-keyed host receipt exists.
    key = "send_message:" + hashlib.sha256(b"m68-explicit-reply").hexdigest()
    send_context = DeliveryContext(channel="feishu", target=ctx.actor.actor_id, mode="proactive",
                                   task_id=wake.root_task_id, idempotency_key=key)
    reply = ReplyEnvelope(content="M68 explicit reply")
    receipt = deliver_host_envelope(service, store, send_context, reply)
    assert receipt.delivery_status == "sent" and len(sends) == 1
    assert query(inputs, input_id, tool_event=event, delivery_store=store) == verified

    request = SimpleNamespace(thread_id=wake.thread_id, task_id=wake.root_task_id, reason=wake.reason,
                              conversation_turn_id="", wake_signal=wake.to_dict())
    view = SimpleNamespace(conversation_store=inputs.store, home_paths=ctx.agent.home_paths)
    context = background_delivery_context(view, inputs.store.threads.require(wake.thread_id),
                                          channel="feishu", target=ctx.actor.actor_id, request=request)
    binding = {"request_id": "m68-request", "run_id": "m68-run", "task_id": wake.root_task_id,
               "attempt_id": "m68-attempt", "thread_id": wake.thread_id, "source_thread_id": wake.thread_id,
               "conversation_id": ctx.actor.conversation_id, "actor_id": ctx.actor.actor_id,
               "channel": "feishu", "target": ctx.actor.actor_id, "operation_id": "op-m68"}
    delivery = {"schema_version": "message_tool_delivery.v1", "delivery_status": "sent",
                "source_owner_delivery": True, "reply_binding": binding, "delivery_ref": "host-delivery:" + key,
                "channel": "feishu", "content": reply.content, "receipt_id": key[:20], "evidence_refs": [],
                "deduplicated": False, "attachments": [],
                "component_receipts": list(receipt.component_receipts)}
    deps = BackgroundDeliveryDependencies(ctx.agent, inputs.store, service, lambda _request: "running")
    commit = mirror_message_tool_deliveries(deps, request, context, (delivery,),
                                            delivery_reason="message_tool_delivery")
    assert commit.persisted
    # Mirroring and auditing never issue a second provider send.
    assert len(sends) == 1
    inputs.store.wakes.mark_handled(wake.wake_signal_id)
    assert query(inputs, input_id, tool_event=event, delivery_store=store) == {
        **verified, "state": "delivered", "delivery_id": "host-delivery:" + key,
    }
    assert len(sends) == 1

    # The same spool key confirmed for another target is not evidence for the original route.
    other_root = tmp_path / "other-delivery"
    other_service, _ = _simulated_feishu(monkeypatch, other_root)
    other_store = GatewayReplyDeliveryStore(other_root)
    assert deliver_host_envelope(other_service, other_store, replace(send_context, target="ou_unrelated"),
                                 reply).delivery_status == "sent"
    assert query(inputs, input_id, tool_event=event, delivery_store=other_store) == verified
