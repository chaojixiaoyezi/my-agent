from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalRequest,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13_readers as readers_mod
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    resolve_owner_home,
)


def _make_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tag: str) -> SimpleAgent:
    home = tmp_path / f"home-{tag}"
    ws = tmp_path / f"ws-{tag}"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / f"os-home-{tag}"))
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home),
        memory_path="memory.jsonl",
        execution_mode="local_unmanaged",
        enable_tools=True,
        business_spaces_enabled=True,
    )
    return SimpleAgent(config, ws)


def _sample_approval_request(request_id: str, run_id: str, description: str) -> ToolApprovalRequest:
    call = SimpleNamespace(
        call_id=f"call-{request_id}",
        tool_name="run_command",
        run_id=run_id,
        operation_id=f"op-{request_id}",
        idempotency_key=f"idem-{request_id}-0001",
        args_hash=f"hash-{request_id}",
        turn_id=f"turn-{request_id}",
    )
    req = build_tool_approval_request(
        call,
        request_id=request_id,
        round_number=1,
        call_index=0,
        description=description,
    )
    return ToolApprovalRequest(
        permission_id=req.permission_id,
        request_id=req.request_id,
        tool_name=req.tool_name,
        round=req.round,
        call_index=req.call_index,
        title=req.title,
        description=req.description,
        binding=req.binding,
        options=req.options,
        requested_at=time.time(),
        expires_at=time.time() + 600.0,
    )


def _write_processing_request(
    paths,
    *,
    request_id: str,
    channel: str,
    conversation_id: str,
    user_id: str,
    owner_info: dict[str, str],
    status: str = "processing",
    turn_phase: str = "open",
    approval_request: ToolApprovalRequest | None = None,
    thread_id: str = "",
) -> Path:
    now = time.time()
    req_path = paths.processing / f"{request_id}.json"
    request_doc: dict[str, object] = {
        "id": request_id,
        "kind": "ask",
        "status": status,
        "turn_phase": turn_phase,
        "cancel_requested": False,
        "lease_started_at": now - 5.0,
        "user_id": user_id,
        "conversation": {
            "channel": channel,
            "channel_conversation_id": conversation_id,
            "channel_user_id": user_id,
            "canonical_user_id": user_id,
        },
        "host_request_owner.v1": owner_info,
    }
    if thread_id:
        request_doc["conversation_runtime"] = {"request_id": request_id, "thread_id": thread_id}
    write_json_file_atomic(req_path, request_doc)
    if approval_request is not None:
        chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
        event = {
            "kind": "permission_requested",
            "t": now,
            "permission": approval_request.to_dict(),
        }
        chunk_path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    return req_path


def test_native_approvals_list_scans_processing_directory_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent(tmp_path, monkeypatch, tag="scan-once")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    store = agent.conversation_store

    # Create 50 bound threads under the current owner.
    for idx in range(50):
        store.threads.get_or_create({
            "channel": "chat",
            "channel_conversation_id": f"conv-scan-{idx}",
            "channel_user_id": "local-user",
            "canonical_user_id": "local-user",
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
            "title": f"bound-thread-{idx}",
        })

    main_owner_info = {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    pending_req = _sample_approval_request("req-match-00", "run-match-00", "Check release status")
    _write_processing_request(
        paths,
        request_id="req-match-00",
        channel="chat",
        conversation_id="conv-scan-0",
        user_id="local-user",
        owner_info=main_owner_info,
        approval_request=pending_req,
    )

    # Create 79 additional processing records (total = 80 records in processing/).
    for idx in range(1, 80):
        _write_processing_request(
            paths,
            request_id=f"req-other-{idx:02d}",
            channel="chat",
            conversation_id=f"conv-unrelated-{idx}",
            user_id="local-user",
            owner_info=main_owner_info,
            status="done" if idx % 2 == 0 else "processing",
        )

    # Add a pending request belonging to another isolated owner to verify scope validation.
    other_pending = _sample_approval_request("req-other-owner", "run-other-owner", "Other owner action")
    _write_processing_request(
        paths,
        request_id="req-other-owner",
        channel="feishu",
        conversation_id="conv-other-owner",
        user_id="other-user",
        owner_info={"provider": "feishu", "owner_kind": "user", "owner_id": "other-user"},
        approval_request=other_pending,
    )

    readers = readers_mod.native_approval_readers(agent, paths, agent.home_paths.owner_id, store)
    assert {"approvals", "approvals.get"} <= set(readers)

    scandir_calls: list[Path] = []
    read_calls: list[Path] = []
    real_scandir = os.scandir
    from agent_py_agent.agent.gateway_parts import io as gw_io

    real_read_json = gw_io.read_json_file_report

    def counted_scandir(target=None, *args, **kwargs):
        if target is not None and Path(target) == paths.processing:
            scandir_calls.append(Path(target))
        return real_scandir(target, *args, **kwargs)

    def counted_read_json(path: Path, *args, **kwargs):
        if Path(path).parent == paths.processing:
            read_calls.append(Path(path))
        return real_read_json(path, *args, **kwargs)

    monkeypatch.setattr(readers_mod.os, "scandir", counted_scandir)
    monkeypatch.setattr(gw_io, "read_json_file_report", counted_read_json)

    page = readers["approvals"]({})
    assert len(scandir_calls) == 1
    assert len(read_calls) == 81
    assert page.coverage == "complete"
    assert [row.value["approval_id"] for row in page.records] == [pending_req.permission_id]

    # When capped below total processing files, scan stays bounded and surfaces partial coverage.
    scandir_calls.clear()
    read_calls.clear()
    monkeypatch.setattr(readers_mod._NativeApprovals, "_MAX_PROCESSING_SCAN", 10)
    capped_page = readers["approvals"]({})
    assert len(scandir_calls) == 1
    assert len(read_calls) == 10
    assert capped_page.coverage == "partial"

    missing_detail = readers["approvals.get"]({"approval_id": "nonexistent-approval-id"})
    assert missing_detail.records == ()
    assert missing_detail.coverage == "partial"

    # Also verify agent tool approvals path uses bounded list_pending_agent_tool_approvals_report.
    monkeypatch.setattr(readers_mod._NativeApprovals, "_MAX_PROCESSING_SCAN", 2048)
    agent_task_thread = store.threads.get_or_create({
        "channel": "chat",
        "channel_conversation_id": "conv-agent-task-0",
        "channel_user_id": "local-user",
        "canonical_user_id": "local-user",
        "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
        "title": "agent-task-thread",
    })
    store.threads.update_atomic(
        agent_task_thread.thread_id,
        lambda current: current.__class__.from_dict({
            **current.to_dict(),
            "active_task_ids": ["root-task-bounded-0"],
        }),
    )
    from agent_py_agent.agent.conversation import agent_tool_approval

    report_calls: list[dict[str, object]] = []

    def fake_bounded_report(_agent, *, root_task_id, deadline, scan_limit=128, scan_progress=None):
        report_calls.append({"root_task_id": root_task_id, "deadline": deadline, "scan_limit": scan_limit})
        return [], False

    monkeypatch.setattr(agent_tool_approval, "list_pending_agent_tool_approvals_report", fake_bounded_report)
    agent_partial_page = readers["approvals"]({})
    assert len(report_calls) == 1
    assert report_calls[0]["root_task_id"] == "root-task-bounded-0"
    assert agent_partial_page.coverage == "partial"


def test_local_business_services_exposes_readonly_approvals_list_and_get_to_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent(tmp_path, monkeypatch, tag="model-wire")
    space_tool_before = agent.tools.tools["business_space"]
    actions_before = set(space_tool_before.model_spec.input_schema["properties"]["action"]["enum"])
    assert "approvals.list" not in actions_before
    assert "approvals.get" not in actions_before

    # Enable live Gateway paths on the host and create a pending approval for this owner.
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    thread = agent.conversation_store.threads.get_or_create({
        "channel": "chat",
        "channel_conversation_id": "conv-live-0",
        "channel_user_id": "local-user",
        "canonical_user_id": "local-user",
        "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
        "title": "live-approval-thread",
    })
    pending_req = _sample_approval_request("req-live-01", "run-live-01", "Verify package snapshot")
    _write_processing_request(
        paths,
        request_id="req-live-01",
        channel="chat",
        conversation_id="conv-live-0",
        user_id="local-user",
        owner_info={"provider": "local", "owner_kind": "main", "owner_id": "main"},
        approval_request=pending_req,
        thread_id=thread.thread_id,
    )

    api.register_business_tools(agent)
    space_tool = agent.tools.tools["business_space"]
    actions_after = set(space_tool.model_spec.input_schema["properties"]["action"]["enum"])
    assert {"approvals.list", "approvals.get"} <= actions_after
    assert "approvals.decide" not in actions_after

    caps_res = space_tool.execute({"action": "system.capabilities", "request_id": "cap-live-1", "params": {}})
    caps_data = json.loads(caps_res.output)
    assert caps_data["error"] is None
    assert {"approvals.list", "approvals.get"} <= set(caps_data["data"]["supported_actions"])
    assert "approvals.decide" not in caps_data["data"]["supported_actions"]

    list_res = space_tool.execute({"action": "approvals.list", "request_id": "list-live-1", "params": {}})
    list_data = json.loads(list_res.output)
    assert list_data["error"] is None
    assert list_data["data"]["coverage"] == "complete"
    assert [item["approval_id"] for item in list_data["data"]["items"]] == [pending_req.permission_id]
    assert list_data["data"]["items"][0]["allowed_decisions"] == []

    get_res = space_tool.execute({
        "action": "approvals.get",
        "request_id": "get-live-1",
        "params": {"approval_id": pending_req.permission_id},
    })
    get_data = json.loads(get_res.output)
    assert get_data["error"] is None
    assert get_data["data"]["approval"]["approval_id"] == pending_req.permission_id
    assert get_data["data"]["allowed_actions"] == []

    # Verify run-boundary refresh in runtime_mixin via gateway_root_from_host_params for an isolated owner.
    isolated_owner = resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user("feishu", "alice-iso"))
    isolated_owner.home_dir.mkdir(parents=True, exist_ok=True)
    shared_gw = tmp_path / "shared-gw"
    shared_paths = gateway_paths_from_root(shared_gw)
    ensure_gateway_folders(shared_paths)

    monkeypatch.setenv("MY_AGENT_HOME", str(agent.home_paths.root))
    iso_agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(agent.home_paths.root),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="alice-iso",
            memory_path="memory.jsonl",
            enable_tools=True,
            business_spaces_enabled=True,
        ),
        isolated_owner.home_dir / "workspace-no-gw",
    )
    iso_agent.backend = EchoBackend()
    api.register_business_tools(iso_agent)
    iso_before = set(iso_agent.tools.tools["business_space"].model_spec.input_schema["properties"]["action"]["enum"])
    assert "approvals.list" not in iso_before

    iso_thread = iso_agent.conversation_store.threads.get_or_create({
        "channel": "feishu",
        "channel_conversation_id": "conv-iso-1",
        "channel_user_id": "alice-iso",
        "canonical_user_id": "alice-iso",
        "owner_id": isolated_owner.owner_id,
        "owner_home": str(isolated_owner.home_dir),
        "title": "isolated-owner-thread",
    })
    iso_req_id = "req-iso-live-01"
    iso_pending = _sample_approval_request(iso_req_id, "run-iso-live-01", "Approve isolated step")
    claimed_path = _write_processing_request(
        shared_paths,
        request_id=iso_req_id,
        channel="feishu",
        conversation_id="conv-iso-1",
        user_id="alice-iso",
        owner_info={"provider": "feishu", "owner_kind": "user", "owner_id": "alice-iso"},
        approval_request=iso_pending,
        thread_id=iso_thread.thread_id,
    )
    # Write a second owner's approval in the same shared Gateway queue to ensure zero cross-owner visibility.
    bob_pending = _sample_approval_request("req-bob-live-01", "run-bob-live-01", "Bob action")
    _write_processing_request(
        shared_paths,
        request_id="req-bob-live-01",
        channel="feishu",
        conversation_id="conv-bob-1",
        user_id="bob-iso",
        owner_info={"provider": "feishu", "owner_kind": "user", "owner_id": "bob-iso"},
        approval_request=bob_pending,
    )

    observed_during_run: dict[str, object] = {}
    orig_prepare = iso_agent.tools.prepare_for_run

    def capture_on_prepare():
        orig_prepare()
        tool = iso_agent.tools.tools["business_space"]
        observed_during_run["ready_actions"] = set(tool._ready_action_names)
        snap = iso_agent.tools.runtime_snapshot(run_id=iso_agent._current_run_params.run_id)
        observed_during_run["snapshot_actions"] = set(
            snap.runtime("business_space").model_spec.input_schema["properties"]["action"]["enum"]
        )
        res = tool.execute({"action": "approvals.list", "request_id": "iso-list-1", "params": {}})
        observed_during_run["list_body"] = json.loads(res.output)
        get_res = tool.execute({
            "action": "approvals.get",
            "request_id": "iso-get-1",
            "params": {"approval_id": iso_pending.permission_id},
        })
        observed_during_run["get_body"] = json.loads(get_res.output)

    monkeypatch.setattr(iso_agent.tools, "prepare_for_run", capture_on_prepare)
    iso_actor = HostChannelActor(
        owner_id=isolated_owner.owner_id,
        source_thread_id=iso_thread.thread_id,
        entry_request_id=iso_req_id,
        channel="feishu",
        conversation_id="conv-iso-1",
        actor_id="alice-iso",
    )
    iso_agent.run(
        "check approvals",
        params=RunParams(
            run_id="run-iso-live-01",
            source="gateway",
            request_id=iso_req_id,
            host_channel_actor=iso_actor,
            task_attributes={"conversation_thread_id": iso_thread.thread_id},
            recovery_content_paths=[str(claimed_path), str(shared_paths.responses / f"{iso_req_id}.json")],
        ),
    )

    assert {"approvals.list", "approvals.get"} <= observed_during_run["ready_actions"]
    assert {"approvals.list", "approvals.get"} <= observed_during_run["snapshot_actions"]
    assert "approvals.decide" not in observed_during_run["ready_actions"]
    list_body = observed_during_run["list_body"]
    assert list_body.get("error") is None, list_body
    assert [
        item["approval_id"] for item in list_body["data"]["items"]
    ] == [iso_pending.permission_id]
    get_body = observed_during_run["get_body"]
    assert get_body.get("error") is None, get_body
    assert get_body["data"]["approval"]["approval_id"] == iso_pending.permission_id
    assert get_body["data"]["allowed_actions"] == []

    # A subsequent non-gateway run cleans up gateway-only approval readers and keeps schema accurate.
    non_gw_actions: list[set[str]] = []
    monkeypatch.setattr(
        iso_agent.tools,
        "prepare_for_run",
        lambda: (
            orig_prepare(),
            non_gw_actions.append(set(iso_agent.tools.tools["business_space"]._ready_action_names)),
        )[0],
    )
    iso_agent.run("plain turn", params=RunParams(run_id="run-plain-02"))
    assert non_gw_actions and "approvals.list" not in non_gw_actions[0]


def test_approvals_decide_remains_strictly_forbidden_to_model_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent(tmp_path, monkeypatch, tag="decide-denied")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)

    services = api._local_business_services(agent)
    assert services.ready("approvals.list") is True
    assert services.ready("approvals.get") is True
    assert services.ready("approvals.decide") is False

    # Even if a decider callback is attached to the host, model registration keeps approvals.decide excluded.
    agent.business_approval_decider = lambda req_body, actor=None: {"state": "approved"}
    agent._local_business_services_cache = None
    api.register_business_tools(agent)

    space_tool = agent.tools.tools["business_space"]
    enum_actions = set(space_tool.model_spec.input_schema["properties"]["action"]["enum"])
    assert {"approvals.list", "approvals.get"} <= enum_actions
    assert "approvals.decide" not in enum_actions

    caps_data = json.loads(
        space_tool.execute({"action": "system.capabilities", "request_id": "cap-deny-1", "params": {}}).output
    )
    assert "approvals.decide" not in caps_data["data"]["supported_actions"]

    denied = space_tool.execute({
        "action": "approvals.decide",
        "request_id": "model-decide-1",
        "idempotency_key": "model-decide-idem-0001",
        "expected_revision": 0,
        "params": {"approval_id": "approval-1", "decision": "approved", "response_ticket": "opaque-ticket"},
    })
    assert not denied.ok
    assert denied.reported_error_code == "INVALID_SCHEMA"

    context, _ = api.local_business_context(agent, model=True)
    with pytest.raises(HostAPIError) as exc_info:
        context.check("approvals.decide", {})
    assert exc_info.value.code == "CAPABILITY_DENIED"


def test_observed_tool_error_preserves_caller_fallback_message_when_no_review_reason():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    err = readers_mod._observed_tool_error(
        "WORKFLOW_FAILED",
        fallback_message="step_failed_alpha",
    )
    assert err is not None
    assert err["code"] == "WORKFLOW_FAILED"
    assert err["message"] == "step_failed_alpha"
    assert err["details"] == [
        {"key": "recommended_action", "value": error_contract("WORKFLOW_FAILED").recommended_action},
    ]


def test_observed_tool_error_uses_contract_recovery_hint_when_fallback_empty():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    err = readers_mod._observed_tool_error("WORKFLOW_FAILED", fallback_message="")
    contract = error_contract("WORKFLOW_FAILED")
    assert err is not None
    assert err["code"] == "WORKFLOW_FAILED"
    assert err["message"] == contract.recovery_hint
    assert err["details"] == [
        {"key": "recommended_action", "value": contract.recommended_action},
    ]


def test_observed_tool_error_pending_review_reason_overrides_fallback_message():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    workflow = {
        "workflow_run_id": "wf-rev-1",
        "status": "failed",
        "failed_step_id": "step_rev",
        "steps": [
            {
                "step_id": "step_rev",
                "error_code": "WORKFLOW_REVIEW_REQUIRED",
                "escalation_result": {
                    "attempt_id": "att-1",
                    "pending_review": {
                        "schema": "workflow.pending-review.v1",
                        "workflow_run_id": "wf-rev-1",
                        "step_id": "step_rev",
                        "attempt_id": "att-1",
                    },
                },
            },
        ],
    }
    err = readers_mod._observed_tool_error(
        "WORKFLOW_FAILED",
        workflow=workflow,
        fallback_message="step_rev",
    )
    contract = error_contract("WORKFLOW_REVIEW_REQUIRED")
    assert err is not None
    assert err["code"] == "WORKFLOW_REVIEW_REQUIRED"
    assert err["message"] == contract.recovery_hint
    assert err["details"] == [
        {"key": "recommended_action", "value": contract.recommended_action},
    ]
