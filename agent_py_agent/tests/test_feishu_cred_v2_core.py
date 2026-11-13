"""Tests for Feishu site-level credential vault v2 core logic:
- Minimal v2 card rendering (no target_url / name / note inputs, save button text, scope=owner)
- Field-specific CredentialFormValidationError diagnostics
- Detached save for stopped/cancelled task (resume_status="task_inactive")
- discover_owner_credentials public metadata and cross-task resolution
- Rejection flow on 401 setting verification_status="rejected"
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.common.json_io import read_json_object_report, write_json_file_atomic
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.task_credential_forms import (
    CREDENTIAL_FORM_ERROR_FIELDS,
    CREDENTIAL_FORM_ERROR_REASONS,
    CredentialFormValidationError,
    credential_submission_values,
)
from agent_py_agent.agent.user_space.task_credential_groups import discover_owner_credentials
from agent_py_agent.agent.user_space.task_credential_storage import CredentialInputError
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_card,
    credential_headers,
    credential_origin,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_ingestion_secret_scope import _agent
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active


def _user_case(tmp_path, *, kind="user", url="https://example.com/api", authentication="basic", options=None):
    agent = _agent(tmp_path, kind=kind)
    agent.config.feishu_session_lock_enabled = False
    thread, actor, claim = _active(agent, actor_id="ou_synthetic")
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_synthetic")
    sent = []

    def send(target, card, **kwargs):
        sent.append(card)
        return {"message_id": "om_" + uuid.uuid4().hex, "chat_id": "oc_private"}

    agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(send_control_card=send),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    reference, _ = request_credential_card(
        agent, url=url, authentication=authentication, options=options
    )
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context="test").payload
    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {
            "username": "synthetic-user",
            "secret": "Synthetic-API-Secret-123",
        },
    }
    return SimpleNamespace(
        agent=agent,
        thread=thread,
        actor=actor,
        claim=claim,
        reference=reference,
        path=path,
        body=body,
        sent=sent,
        row=row,
    )


def test_personal_owner_defaults_to_owner_scope_and_v2_minimal_card(tmp_path):
    case = _user_case(tmp_path, url="https://api.example.com/v1/items")
    row = case.row
    expected_origin = credential_origin("https://api.example.com/v1/items")
    assert row["access_scope"] == "owner"
    assert row["schema_version"] == "task_credential.v2"
    assert row["ttl_seconds"] == 0
    assert row["origin"] == expected_origin

    # Verify rendered card structure
    assert len(case.sent) == 1
    card = case.sent[0]
    elements = card["elements"]
    assert len(elements) == 3
    div_elem, form_elem, action_elem = elements

    # Explanatory div mentions site target and security notice
    assert f"站点凭据安全填写：{expected_origin}" in div_elem["text"]["content"]
    assert "保存后仅绑定到你的个人账号与该站点" in div_elem["text"]["content"]

    # Form inputs: only username and secret; NO target_url, credential_name, or credential_note
    form_elements = form_elem["elements"]
    field_names = [elem.get("name") for elem in form_elements if elem.get("tag") == "input"]
    assert field_names == ["username", "secret"]
    assert "target_url" not in field_names
    assert "credential_name" not in field_names
    assert "credential_note" not in field_names

    for elem in form_elements:
        if elem.get("tag") == "input":
            assert elem.get("required") is True

    # Submit button
    submit_btn = next(elem for elem in form_elements if elem.get("action_type") == "form_submit")
    assert submit_btn["text"]["content"] == "保存凭据"


def test_personal_owner_replacement_card_button_text(tmp_path):
    case = _user_case(tmp_path, url="https://api.example.com/v1/items")
    # Simulate replacement row
    replacement_row = {**case.row, "replaces_ref": "cred_original_reference"}
    card = credential_card(replacement_row)
    form_elements = card["elements"][1]["elements"]
    submit_btn = next(elem for elem in form_elements if elem.get("action_type") == "form_submit")
    assert submit_btn["text"]["content"] == "更新并保存凭据"


def test_personal_owner_without_url_falls_back_to_task_scope(tmp_path):
    case = _user_case(tmp_path, url="")
    row = case.row
    assert row["access_scope"] == "task"
    assert row["schema_version"] == "task_credential.v1"

    # Card includes target_url and optional metadata fields
    card = case.sent[0]
    form_elements = card["elements"][1]["elements"]
    field_names = [elem.get("name") for elem in form_elements if elem.get("tag") == "input"]
    assert "target_url" in field_names
    assert "credential_name" in field_names
    assert "credential_note" in field_names

    submit_btn = next(elem for elem in form_elements if elem.get("action_type") == "form_submit")
    assert submit_btn["text"]["content"] == "提交"


def test_explicit_owner_scope_without_url_raises_binding_error(tmp_path):
    agent = _agent(tmp_path, kind="user")
    _active(agent)
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_synthetic")
    options = CredentialCardOptions(access_scope="owner")
    with pytest.raises(CredentialInputError) as exc_info:
        request_credential_card(agent, url="", authentication="basic", options=options)
    assert exc_info.value.code == "CREDENTIAL_BINDING_INVALID"


def test_group_owner_cannot_request_owner_scope(tmp_path):
    agent = _agent(tmp_path, kind="group")
    _active(agent)
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "oc_synthetic")
    options = CredentialCardOptions(access_scope="owner")
    with pytest.raises(ValueError, match="群来源不能登记个人站点凭据"):
        request_credential_card(
            agent, url="https://example.com/api", authentication="basic", options=options
        )


def test_field_specific_credential_form_validation_error():
    assert "username" in CREDENTIAL_FORM_ERROR_FIELDS
    assert "secret" in CREDENTIAL_FORM_ERROR_FIELDS
    assert "target_url" in CREDENTIAL_FORM_ERROR_FIELDS
    assert "missing" in CREDENTIAL_FORM_ERROR_REASONS
    assert "empty" in CREDENTIAL_FORM_ERROR_REASONS

    e_user = CredentialFormValidationError("username", "empty")
    assert e_user.field == "username"
    assert e_user.reason == "empty"
    assert e_user.code == "CREDENTIAL_FORM_INVALID"
    assert "账号" in e_user.message
    assert "原卡" in e_user.message

    e_secret = CredentialFormValidationError("secret", "missing")
    assert e_secret.field == "secret"
    assert e_secret.reason == "missing"
    assert "密码" in e_secret.message
    assert "原卡" in e_secret.message

    e_url = CredentialFormValidationError("target_url", "invalid_url")
    assert e_url.field == "target_url"
    assert e_url.reason == "invalid_url"
    assert "目标链接" in e_url.message
    assert "原卡" in e_url.message

    e_newline = CredentialFormValidationError("username", "newline")
    assert "换行符" in e_newline.message
    assert "原卡" in e_newline.message

    e_length = CredentialFormValidationError("username", "too_long")
    assert "长度上限" in e_length.message
    assert "原卡" in e_length.message

    # Test single-argument target_url normalization
    e_default_url = CredentialFormValidationError("target_url")
    assert e_default_url.field == "target_url"
    assert e_default_url.reason == "invalid_url"


def test_credential_submission_values_diagnostics():
    row = {
        "fields": [{"name": "username"}, {"name": "secret"}],
        "target_url": "https://example.com/api",
        "origin": credential_origin("https://example.com/api"),
    }

    # Non-dict fields
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(row, "not_a_dict", credential_origin)
    assert exc.value.field == "fields" and exc.value.reason == "type"

    # Missing username
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(row, {"secret": "s"}, credential_origin)
    assert exc.value.field == "username" and exc.value.reason == "missing"

    # Empty username
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(row, {"username": "   ", "secret": "s"}, credential_origin)
    assert exc.value.field == "username" and exc.value.reason == "empty"

    # Empty secret
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(row, {"username": "user", "secret": ""}, credential_origin)
    assert exc.value.field == "secret" and exc.value.reason == "empty"

    # Newline in field
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(row, {"username": "user\nname", "secret": "s"}, credential_origin)
    assert exc.value.field == "username" and exc.value.reason == "newline"

    # Unexpected keys
    with pytest.raises(CredentialFormValidationError) as exc:
        credential_submission_values(
            row, {"username": "user", "secret": "s", "unknown_key": "val"}, credential_origin
        )
    assert exc.value.field == "fields" and exc.value.reason == "unexpected"

    # Valid submission: strips _credential_submit and ignores None for optional fields
    values, metadata = credential_submission_values(
        row,
        {
            "username": "user1",
            "secret": "secret123",
            "_credential_submit": "click",
            "credential_name": None,
            "credential_note": None,
        },
        credential_origin,
    )
    assert values == {"username": "user1", "secret": "secret123"}
    assert metadata["target_url"] == "https://example.com/api"
    assert metadata["origin"] == credential_origin("https://example.com/api")


def test_detached_save_for_inactive_task_and_replay_idempotency(tmp_path):
    case = _user_case(tmp_path, url="https://example.com/api")
    assert case.row["access_scope"] == "owner"

    # Simulate originating task becoming cancelled / inactive before card submission
    task_id = case.agent._current_run_params.task_id
    case.agent.conversation_store.tasks.update_status(
        {"task_id": task_id, "status": "cancelled"}
    )

    # Submission should succeed via detached path without throwing task-expired error
    result = resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )
    assert result["ok"] is True
    assert result["saved"] is True
    assert result["resume_status"] == "task_inactive"

    # Credential row is persisted in ready state on disk
    saved_row = read_json_object_report(case.path, context="test").payload
    assert saved_row["status"] == "ready"
    assert "ciphertext" in saved_row
    assert saved_row["ready_expires_at"] == 0

    # Duplicate callback replay from Feishu should succeed idempotently
    replay_result = resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )
    assert replay_result["ok"] is True


def test_discover_owner_credentials_and_cross_task_reuse(tmp_path):
    case = _user_case(tmp_path, url="https://example.com/api")
    resolve_result = resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )
    assert resolve_result["ok"] is True

    # 1. Discover owner credentials for the site
    discovery = discover_owner_credentials(
        case.agent, url="https://example.com/api", operation="find"
    )
    assert discovery["status"] == "ok"
    assert discovery["total_count"] >= 1
    credentials = discovery["credentials"]
    match = next((c for c in credentials if c["credential_ref"] == case.reference), None)
    assert match is not None
    assert match["origin"] == credential_origin("https://example.com/api")
    assert match["usable"] is True
    assert match["verification_status"] == "unverified"
    assert discovery["selection_reason"] == "own_selected"
    assert discovery["selected_credential_ref"] == case.reference
    assert "secret" not in match
    assert "ciphertext" not in match

    # 2. Querying a different site should yield no matches
    other_discovery = discover_owner_credentials(
        case.agent, url="https://other.example.com", operation="find"
    )
    assert other_discovery["credentials"] == []
    assert other_discovery["selected_credential_ref"] == ""

    # 3. Simulate another task under the same personal user owner attempting to reuse the credential
    case.agent.conversation_store.claims.finish(
        {"thread_id": case.thread.thread_id, "claim_id": case.claim["claim_id"], "status": "finished"}
    )
    new_task_id = "task-distinct-456"
    _active(case.agent, root_id=new_task_id)

    headers, secrets = credential_headers(
        case.agent, case.reference, "https://example.com/other_endpoint"
    )
    assert headers["Authorization"].startswith("Basic ")
    assert "Synthetic-API-Secret-123" in secrets


def test_owner_credential_rejection_marks_verification_status(tmp_path):
    case = _user_case(tmp_path, url="https://example.com/api")
    assert resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )["ok"]

    # Simulate HTTP 401 response marking verification_status as rejected
    current = read_json_object_report(case.path, context="test").payload
    current["verification_status"] = "rejected"
    write_json_file_atomic(case.path, current)

    discovery = discover_owner_credentials(
        case.agent, url="https://example.com/api", operation="find"
    )
    assert discovery["status"] == "ok"
    match = next(c for c in discovery["credentials"] if c["credential_ref"] == case.reference)
    assert match["usable"] is False
    assert match["verification_status"] == "rejected"
    assert discovery["selection_reason"] == "own_unavailable"
    assert discovery.get("selected_credential_ref") == ""

    # Using rejected credential raises error
    with pytest.raises(ValueError, match="凭据未就绪、已过期或已撤销"):
        credential_headers(case.agent, case.reference, "https://example.com/api")


def test_bound_admin_feishu_dm_defaults_to_owner_scope_and_cross_task_reuse(tmp_path):
    case = _user_case(tmp_path, kind="main", url="https://example.com/api")
    row = case.row
    assert row["access_scope"] == "owner"
    assert row["schema_version"] == "task_credential.v2"
    assert row["ttl_seconds"] == 0
    assert row["origin"] == credential_origin("https://example.com/api")

    # Complete credential submission
    resolve_result = resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )
    assert resolve_result["ok"] is True

    # 1. Discover owner credentials for the site
    discovery = discover_owner_credentials(
        case.agent, url="https://example.com/api", operation="find"
    )
    assert discovery["status"] == "ok"
    assert discovery["total_count"] >= 1
    assert discovery["selected_credential_ref"] == case.reference

    # 2. Simulate another task under the bound-admin attempting to reuse the credential
    case.agent.conversation_store.claims.finish(
        {"thread_id": case.thread.thread_id, "claim_id": case.claim["claim_id"], "status": "finished"}
    )
    new_task_id = "task-bound-admin-789"
    _active(case.agent, actor_id="ou_synthetic", root_id=new_task_id)

    headers, secrets = credential_headers(
        case.agent, case.reference, "https://example.com/api/v2"
    )
    assert headers["Authorization"].startswith("Basic ")
    assert "Synthetic-API-Secret-123" in secrets


def test_local_main_chat_channel_defaults_to_task_scope(tmp_path):
    agent = _agent(tmp_path, kind="main")
    agent.config.feishu_session_lock_enabled = False
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "channel": "chat",
        "channel_conversation_id": "conv-cli",
        "channel_user_id": "user-cli",
        "canonical_user_id": "user-cli",
        "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
    })
    scope = SimpleNamespace(
        active=True,
        thread_id=thread.thread_id,
        run_id="task-cli",
        root_task_id="task-cli",
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-cli", "goal": "CLI task", "status": "active"}
    )
    from agent_py_agent.agent.user_space.task_credential_groups import new_credential_access_scope

    access_scope = new_credential_access_scope(agent, scope, url="https://example.com/api")
    assert access_scope == "task"


def test_v2_signature_and_schema_version_invariants_and_drift_rejection(tmp_path):
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference
    from agent_py_agent.agent.user_space.task_credential_forms import (
        assert_credential_record_signature,
    )
    from agent_py_agent.agent.user_space.task_credential_replay import (
        assert_credential_callback_identity,
    )
    from agent_py_agent.agent.user_space.task_credentials import _assert_credential_scope

    case = _user_case(tmp_path, url="https://example.com/api")
    row = read_json_object_report(case.path, context="test").payload
    scope = tool_approval_scope(case.agent, str(case.claim["task_id"]))

    claims = verify_interaction_reference(
        case.agent.home_paths.root,
        row["reference"],
        channel="feishu",
        actor_id="ou_synthetic",
        decision="submit",
    )
    assert claims.get("schema_version") == "task_credential.v2"
    assert claims.get("origin") == credential_origin("https://example.com/api")
    assert claims.get("collection_mode") == "username_password"

    # 1. Mismatched origin in record
    mismatched_origin_row = dict(row, origin="https://unrelated.example.com")
    with pytest.raises(ValueError, match="原卡凭据绑定不匹配"):
        assert_credential_callback_identity(mismatched_origin_row, claims, case.body)
    with pytest.raises(ValueError, match="原凭据卡身份无法核验"):
        assert_credential_record_signature(case.agent, mismatched_origin_row)
    with pytest.raises(ValueError, match="凭据不属于此调用或接口"):
        _assert_credential_scope(case.agent, mismatched_origin_row, scope, "https://example.com/api")

    # 2. Mismatched collection_mode in record
    mismatched_mode_row = dict(row, collection_mode="alternative_mode")
    with pytest.raises(ValueError, match="原卡凭据绑定不匹配"):
        assert_credential_callback_identity(mismatched_mode_row, claims, case.body)
    with pytest.raises(ValueError, match="原凭据卡身份无法核验"):
        assert_credential_record_signature(case.agent, mismatched_mode_row)

    # 3. Unknown schema_version in record
    unknown_schema_row = dict(row, schema_version="task_credential.v999")
    with pytest.raises(ValueError, match="原卡凭据绑定不匹配"):
        assert_credential_callback_identity(unknown_schema_row, claims, case.body)
    with pytest.raises(ValueError, match="原凭据卡身份无法核验"):
        assert_credential_record_signature(case.agent, unknown_schema_row)
    with pytest.raises(ValueError, match="凭据不属于此调用或接口"):
        _assert_credential_scope(case.agent, unknown_schema_row, scope, "https://example.com/api")

    # 4. v1 schema with owner access_scope rejected by _assert_credential_scope
    v1_owner_row = dict(row, schema_version="task_credential.v1")
    with pytest.raises(ValueError, match="凭据不属于此调用或接口"):
        _assert_credential_scope(case.agent, v1_owner_row, scope, "https://example.com/api", require_ready=False)

    # 5. discover_owner_credentials skips tampered/unknown records cleanly
    assert resolve_private_interaction(
        case.agent, case.body, channel="feishu", actor_id="ou_synthetic"
    )["ok"]

    tampered_ref = "cred_" + ("f" * 32)
    tampered_path = credential_path(case.agent.home_paths.owner_home_dir, tampered_ref)
    tampered_record = dict(
        row,
        credential_ref=tampered_ref,
        status="ready",
        origin="https://modified.example.com",
    )
    write_json_file_atomic(tampered_path, tampered_record)

    discovery = discover_owner_credentials(
        case.agent, url="https://example.com/api", operation="find"
    )
    assert discovery["status"] == "ok"
    discovered_refs = [c["credential_ref"] for c in discovery["credentials"]]
    assert case.reference in discovered_refs
    assert tampered_ref not in discovered_refs
