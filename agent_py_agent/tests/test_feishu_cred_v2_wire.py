"""Private control field-specific toast projection, personal discovery tooling & wire tests."""
from __future__ import annotations

import io
import json
from types import ModuleType, SimpleNamespace
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.adapter import private_controls
from agent_py_agent.agent.conversation.input_dependencies import InputDependencyAccess
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    _plugin_input_binding,
    handle_private_interaction,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.gateway_pending import respond_interaction
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.agent.user_space.task_credential_forms import CredentialFormValidationError
from agent_py_agent.tests.test_credential_card_validation import validation_case


@pytest.mark.parametrize(
    "field,reason,expected_keyword",
    [
        ("username", "empty", "账号"),
        ("username", "missing", "账号"),
        ("secret", "empty", "密码"),
        ("secret", "missing", "密码"),
        ("target_url", "missing", "目标链接"),
        ("target_url", "empty", "目标链接"),
        ("target_url", "invalid_url", "目标链接"),
        ("fields", "newline", "换行符"),
        ("secret", "newline", "换行符"),
        ("secret", "too_long", "超出长度上限"),
        ("fields", "empty", "表单内容无效"),
        ("", "", "表单内容无效"),
    ],
)
def test_private_controls_toast_projects_specific_field_and_protects_card(
    tmp_path, monkeypatch, field, reason, expected_keyword
):
    """Ensure HTTP 400 form error projects specific Chinese diagnosis with original card retained."""
    adapter = SimpleNamespace(
        my_agent_home=str(tmp_path),
        _gateway_port=1,
        _gateway_auth_token="synthetic-token",
    )

    def mock_invalid_request(*_args, **_kwargs):
        payload = {
            "ok": False,
            "error_code": "CREDENTIAL_FORM_INVALID",
            "error_stage": "form",
            "message": "synthetic-untrusted-secret-do-not-leak",
        }
        if field:
            payload["error_field"] = field
        if reason:
            payload["error_reason"] = reason
        raw = json.dumps(payload).encode()
        raise HTTPError("http://127.0.0.1/", 400, "Bad Request", {}, io.BytesIO(raw))

    monkeypatch.setattr(private_controls, "open_authenticated_request", mock_invalid_request)

    action_result = private_controls.tool_approval_card_action(
        adapter,
        {
            "value": {
                "kind": "my_agent_task_credential",
                "reference": "ir1.synthetic.1234567890123456789012345678901234567890123456789012345678901234",
                "decision": "submit",
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_msg_1",
            "chat_id": "oc_chat_1",
            "form_value": {"username": "user", "secret": "sec"},
        },
    )

    assert "card" not in action_result
    toast = action_result["toast"]
    assert toast["type"] == "error"
    content = toast["content"]
    assert expected_keyword in content
    assert "原卡" in content
    assert "（CREDENTIAL_FORM_INVALID/form）" in content
    assert "synthetic" not in json.dumps(action_result)


def test_private_interaction_handler_serializes_error_reason_when_present():
    """Verify handle_private_interaction includes error_reason in HTTP 400 response."""
    sent = []

    class DummyHandler:
        headers = {
            "X-Channel": "feishu",
            "X-User-Id": "ou_synthetic",
        }
        _auth_middleware = SimpleNamespace(
            has_gateway_credential=lambda _h: True,
        )

        def _read_json(self):
            return {"reference": "test", "decision": "submit"}

        def _send_json(self, status, payload):
            sent.append((status, payload))

    error = CredentialFormValidationError("target_url")
    error.reason = "invalid_url"

    server = SimpleNamespace(
        control_plane=None,
        agent=SimpleNamespace(),
        paths=None,
    )

    def raise_validation(*_args, **_kwargs):
        raise error

    from agent_py_agent.agent.gateway_parts import private_interaction_service
    original_resolve = private_interaction_service.resolve_private_interaction
    private_interaction_service.resolve_private_interaction = raise_validation
    try:
        handle_private_interaction(DummyHandler(), server)
    finally:
        private_interaction_service.resolve_private_interaction = original_resolve

    assert len(sent) == 1
    status, body = sent[0]
    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "CREDENTIAL_FORM_INVALID"
    assert body["error_stage"] == "form"
    assert body["error_field"] == "target_url"
    assert body["error_reason"] == "invalid_url"


@pytest.mark.parametrize("workers", [0, 2])
def test_coordinator_mode_form_error_parity_end_to_end(tmp_path, monkeypatch, workers):
    """Verify both single-worker and coordinator modes return HTTP 400 with form diagnostic."""
    with validation_case(tmp_path, monkeypatch, workers) as (agent, path, row, body, server, _runtime):
        adapter = SimpleNamespace(
            my_agent_home=str(agent.home_paths.root),
            _gateway_port=server.server.server_address[1],
            _gateway_auth_token=server._local_client_credential,
        )
        norm = {
            "value": {
                "kind": "my_agent_task_credential",
                "reference": row["reference"],
                "decision": "submit",
            },
            "operator_open_id": row["actor_id"],
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "form_value": {**body["fields"], "target_url": "invalid-url"},
        }
        before = path.read_bytes()
        result = private_controls.tool_approval_card_action(adapter, norm)
        assert path.read_bytes() == before
        assert "card" not in result
        toast = result["toast"]
        assert toast["type"] == "error"
        assert "CREDENTIAL_FORM_INVALID/form" in toast["content"]
        assert "原卡" in toast["content"]
        assert "目标链接" in toast["content"]


def test_inactive_task_save_updates_card_and_shows_success_toast(tmp_path, monkeypatch):
    """Verify resume_status == 'task_inactive' presents read-only card confirmation."""
    adapter = SimpleNamespace(
        my_agent_home=str(tmp_path),
        _gateway_port=1,
        _gateway_auth_token="synthetic-token",
    )

    def mock_inactive_save(*_args, **_kwargs):
        return {
            "ok": True,
            "decision": "ready",
            "saved": True,
            "resume_status": "task_inactive",
            "message": "凭据已安全保存（原任务已结束，后续访问该站点可直接复用）。",
        }

    monkeypatch.setattr(private_controls, "_submit_control_request", mock_inactive_save)

    result = private_controls.tool_approval_card_action(
        adapter,
        {
            "value": {
                "kind": "my_agent_task_credential",
                "reference": "ir1.synthetic.1234567890123456789012345678901234567890123456789012345678901234",
                "decision": "submit",
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_msg_1",
            "chat_id": "oc_chat_1",
            "form_value": {"username": "user", "secret": "sec"},
        },
    )

    assert result["toast"]["type"] == "success"
    expected_msg = "凭据已安全保存（原任务已结束，后续访问该站点可直接复用）。"
    assert result["toast"]["content"] == expected_msg
    elements = result["card"]["data"]["elements"]
    assert any(expected_msg in el.get("text", {}).get("content", "") for el in elements)


def test_personal_agent_dispatches_discovery_to_owner_credentials(monkeypatch):
    """Verify personal agent dispatches list/find to discover_owner_credentials when present."""
    owner_calls = []
    group_calls = []

    def mock_discover_owner(agent, *, url="", name="", operation="list"):
        owner_calls.append((agent, url, name, operation))
        return {
            "status": "ok",
            "operation": operation,
            "access_scope": "owner",
            "origin": "https://example.com:443",
            "query_name": name,
            "total_count": 1,
            "usable_count": 1,
            "own_credential_present": True,
            "selected_credential_ref": "cred_personal_1",
            "selection_reason": "own_selected",
            "credentials": [{"credential_ref": "cred_personal_1"}],
        }

    def mock_discover_group(agent, *, url="", name="", operation="list"):
        group_calls.append((agent, url, name, operation))
        return {
            "status": "ok",
            "operation": operation,
            "access_scope": "group",
            "credentials": [],
        }

    module = ModuleType("agent_py_agent.agent.user_space.task_credential_groups")
    module.discover_owner_credentials = mock_discover_owner
    module.discover_group_credentials = mock_discover_group
    monkeypatch.setitem(pytest.importorskip("sys").modules, module.__name__, module)

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent",
        lambda _agent: SimpleNamespace(im_task_credentials_enabled=True),
    )

    personal_agent = SimpleNamespace(home_paths=SimpleNamespace(owner_kind="user"))
    tool_user = RequestCredentialsTool(personal_agent)
    outcome = tool_user.execute({"operation": "find", "url": "https://example.com/login"})
    assert outcome.ok
    assert len(owner_calls) == 1
    assert len(group_calls) == 0
    assert owner_calls[0][1] == "https://example.com/login"
    assert owner_calls[0][3] == "find"
    envelope = outcome.result_envelope["credential_discovery"]
    assert envelope["selected_credential_ref"] == "cred_personal_1"

    group_agent = SimpleNamespace(home_paths=SimpleNamespace(owner_kind="group"))
    tool_group = RequestCredentialsTool(group_agent)
    outcome_grp = tool_group.execute({"operation": "find", "url": "https://example.com/login"})
    assert outcome_grp.ok
    assert len(group_calls) == 1
    assert group_calls[0][1] == "https://example.com/login"


def test_plugin_input_binding_allows_owner_credential_card_on_cancelled_task():
    """Verify detached personal owner credential cards can bind when originating task is cancelled."""
    inputs = SimpleNamespace(
        read=lambda _input_id: {
            "provider": "credential",
            "reference": "cred_ref_1",
            "status": "cancelled",
            "scope": {"thread_id": "th_1", "run_id": "run_1"},
            "conversation_request_id": "req_1",
        },
        current=lambda _dep: False,
    )

    row_owner = {
        "kind": "task_credential",
        "input_id": "inp_1",
        "credential_ref": "cred_ref_1",
        "access_scope": "owner",
        "status": "pending",
        "scope": {"thread_id": "th_1", "run_id": "run_1"},
    }
    binding = _plugin_input_binding(inputs, row_owner)
    assert binding == ("th_1", "run_1", "req_1", "inp_1")

    row_task = {
        "kind": "task_credential",
        "input_id": "inp_1",
        "credential_ref": "cred_ref_1",
        "access_scope": "task",
        "status": "pending",
        "scope": {"thread_id": "th_1", "run_id": "run_1"},
    }
    assert _plugin_input_binding(inputs, row_task) is None


def test_gateway_pending_respond_interaction_maps_validation_error(monkeypatch):
    """Verify respond_interaction translates CredentialFormValidationError into HostAPIError 400."""
    services = SimpleNamespace(
        resources=SimpleNamespace(
            server=SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir="home"))),
            runtime_paths=lambda _ctx: {"subagent_workspace": "ws"},
            store_for=lambda _ctx: SimpleNamespace(),
        ),
        revalidate=lambda _auth: None,
    )
    authorized = SimpleNamespace()
    ticket = SimpleNamespace(
        interaction={"kind": "task_credential", "payload": {}},
        decision_payload={"decision": "submit"},
        human_identity={},
        revision="rev_1",
    )

    monkeypatch.setattr(
        "agent_py_agent.agent.plugin_host.gateway_pending.resolve_read_context",
        lambda _auth: SimpleNamespace(
            owner_home=SimpleNamespace(home_dir="home"),
            identity=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.plugin_host.gateway_pending.loaded_owner_agent",
        lambda _s, _c: SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir="home")),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.user_space.owner_resolver.home_paths_with_owner",
        lambda _a, _o: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.capability.runtime_config_reload.deployment_capability_config",
        lambda _h: SimpleNamespace(),
    )

    class DummyRepo:
        def close(self):
            pass

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.input_dependencies.input_dependency_owner_resources",
        lambda *args, **kwargs: (SimpleNamespace(), DummyRepo()),
    )

    def raise_form_error(*_args, **_kwargs):
        raise CredentialFormValidationError("target_url")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.private_interaction_service.execute_plugin_input",
        raise_form_error,
    )

    with pytest.raises(HostAPIError) as caught:
        respond_interaction(services, authorized, ticket)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.status == 400


def test_input_dependency_access_retires_inactive_task_without_exception(tmp_path):
    """Verify retiring dependencies on missing/terminal tasks does not raise exceptions."""
    nonexistent = tmp_path / "nonexistent.json"
    store = SimpleNamespace(
        tasks=SimpleNamespace(
            load_report=lambda _tid: (None, None),
            storage=SimpleNamespace(task_path=lambda _tid: nonexistent),
        ),
        session_tasks=SimpleNamespace(load=lambda _rid: None),
    )
    access = InputDependencyAccess(
        store=store,
        owner=SimpleNamespace(),
        quota=None,
        load_child=None,
        upsert_wake=None,
        runtime_db=None,
    )

    # Calling update_refs with remove=True on nonexistent task should return None safely
    assert access.update_refs("missing_task_id", "inp_1", remove=True) is None

    # Calling schedule on nonexistent task should complete without raising ValueError
    access.schedule("missing_task_id")
