"""群监控授权明确区分整群未来成员与已发布成员快照，不改变任务所有权。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import group_authorization_control as control
from agent_py_agent.agent.user_space.group_authorization import (
    GroupAuthorizationChange,
    GroupAuthorizationError,
    change_group_authorization,
    read_group_authorization,
    record_group_provider_event,
)
from agent_py_agent.agent.user_space.group_authorization_context import (
    group_capability_guard,
    require_group_mutation_lease,
)
from agent_py_agent.tests.test_group_authorization import _allow, _case, _event, _snapshot


# LLM: 只经原人工授权事务写隔离tool_policy，不代替provider成员证明或关闭开关。
# 函数用途: 创建显式整群监控授权，仍使用原群主与CAS版本。
def _allow_group(agent, raw, actor):
    revision = read_group_authorization(agent.home_paths)[1]["revision"]
    return change_group_authorization(
        agent.home_paths,
        _snapshot(raw),
        actor,
        GroupAuthorizationChange("allow", revision, "monitors.create", group_wide=True),
    )


@pytest.mark.parametrize("topic", [False, True])
def test_explicit_group_grant_covers_current_future_and_rejoined_members(tmp_path, topic):
    agent, source, actor, raw, _ = _case(
        tmp_path, conversation_id="oc_source:thread:opaque_topic" if topic else "oc_source"
    )
    saved = _allow_group(agent, raw, actor)
    assert saved["grants"][0]["scope"] == "group" and saved["grants"][0]["subjects"] == {}
    raw["member_ids"].append("ou_new")
    for subject in ["ou_member", "ou_new"]:
        agent._current_run_params.host_channel_actor = replace(actor, actor_id=subject)
        with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
            pass
    record_group_provider_event(agent.home_paths, _event(event_id="leave", members=("ou_new",)))
    record_group_provider_event(
        agent.home_paths,
        _event(event_id="rejoin", event_type="im.chat.member.user.added_v1", members=("ou_new",)),
    )
    with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
        pass
    assert agent._current_run_params.host_channel_actor.conversation_id == actor.conversation_id


@pytest.mark.parametrize(
    "fault", ["nonmember", "other_app", "other_tenant", "other_owner", "bot_readded", "revoked"]
)
def test_group_scope_preserves_membership_app_owner_epoch_and_revoke(tmp_path, fault):
    agent, source, actor, raw, _ = _case(tmp_path)
    saved = _allow_group(agent, raw, actor)
    current = replace(actor, actor_id="ou_member")
    if fault == "nonmember":
        raw["member_ids"].remove("ou_member")
    elif fault == "other_app":
        raw["app_id"] = "other-app"
    elif fault == "other_tenant":
        raw["tenant_key"] = "other-tenant"
    elif fault == "other_owner":
        current = replace(current, owner_id="providers/feishu/groups/oc_other")
    elif fault == "bot_readded":
        record_group_provider_event(
            agent.home_paths,
            _event(event_id="readd", event_type="im.chat.member.bot.added_v1", operator="ou_owner"),
        )
    elif fault == "revoked":
        change_group_authorization(
            agent.home_paths,
            _snapshot(raw),
            actor,
            GroupAuthorizationChange(
                "revoke", saved["revision"], grant_id=saved["grants"][0]["grant_id"]
            ),
        )
    agent._current_run_params.host_channel_actor = current
    with pytest.raises(ValueError):
        with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
            pytest.fail("失效或异源整群授权不能生效")


def test_legacy_all_current_never_becomes_future_group_permission(tmp_path):
    agent, source, actor, raw, _ = _case(tmp_path)
    before = _allow(agent, raw, actor, all_current=True)
    assert "scope" not in before["grants"][0]
    raw["member_ids"].append("ou_new")
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_new")
    with pytest.raises(GroupAuthorizationError):
        with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
            pass
    assert read_group_authorization(agent.home_paths)[1] == before
    assert "快照" in control._render(before)


@pytest.mark.parametrize(
    "value, group_wide, subject",
    [
        ("监控 全群", True, ""),
        ("监控 member:ou_member", False, "ou_member"),
        ("allow monitors.create", True, ""),
        ("allow monitors.create all-current", False, ""),
    ],
)
def test_simple_command_uses_explicit_scope(value, group_wide, subject):
    change = control._parse_change(value.split(), 0, "ou_owner")
    assert change.group_wide is group_wide and change.subject_id == subject
    assert change.capability == "monitors.create"


@pytest.mark.parametrize(
    "value", ["监控 @张三", "监控 张三", "allow assistant.configure", "监控", "监控 全群 extra"]
)
def test_display_names_and_other_capabilities_cannot_grant(value):
    with pytest.raises(GroupAuthorizationError):
        control._parse_change(value.split(), 0, "ou_owner")


def test_configuration_no_longer_queries_extra_group_authority(tmp_path):
    agent, source, _, raw, calls = _case(tmp_path)
    raw["members_complete"] = False
    with group_capability_guard(agent, "assistant.configure", thread_id=source.thread_id):
        require_group_mutation_lease(agent.home_paths, "assistant.configure")
    assert calls == [] and not read_group_authorization(agent.home_paths)[1]["grants"]


def test_original_group_control_accepts_simple_command_and_new_member(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_profile_service

    agent, source, actor, raw, _ = _case(tmp_path)
    monkeypatch.setattr(model_profile_service, "_scoped_model_host", lambda *_: (agent, source))
    scope = SimpleNamespace(
        channel="feishu", user_id="ou_owner", conversation_id="oc_source", metadata={}
    )
    result = control.execute_group_authorization_control(
        agent, parse_conversation_control("/group 监控 全群"), scope
    )
    assert result.ok and "后续" in result.message
    raw["member_ids"].append("ou_new")
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_new")
    with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
        pass
    assert "assistant.configure" not in result.message


def test_future_member_creates_monitor_with_original_creator_identity(tmp_path):
    import json

    from agent_py_agent.agent.scheduler.tool import ScheduleTool

    agent, source, actor, raw, _ = _case(tmp_path)
    _allow_group(agent, raw, actor)
    raw["member_ids"].append("ou_new")
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_new")
    result = ScheduleTool(agent).execute(
        {
            "action": "create",
            "name": "合成监控",
            "prompt": "合成任务",
            "schedule_kind": "every",
            "every_seconds": 600,
        }
    )
    assert result.ok, result.error_code
    job = agent.scheduler_repository.get_job(json.loads(result.output)["job"]["job_id"])
    assert job["creator"]["actor_id"] == "ou_new" and job["source_thread_id"] == source.thread_id


def test_configuration_identity_lease_cannot_supply_monitor_authority(tmp_path):
    agent, source, actor, raw, calls = _case(tmp_path)
    _allow_group(agent, raw, actor)
    with group_capability_guard(agent, "assistant.configure", thread_id=source.thread_id):
        with pytest.raises(GroupAuthorizationError):
            with group_capability_guard(agent, "monitors.create", thread_id=source.thread_id):
                pytest.fail("配置身份租约不带监控provider授权")
    assert calls == []


def test_provider_failure_has_only_safe_typed_diagnostics_in_control(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.feishu_provider_facts import FeishuProviderFactError
    from agent_py_agent.agent.gateway_parts import model_profile_service

    agent, source, _, _, _ = _case(tmp_path)
    monkeypatch.setattr(model_profile_service, "_scoped_model_host", lambda *_: (agent, source))

    def fail(_):
        raise FeishuProviderFactError("FEISHU_PROVIDER_SCOPE_REQUIRED", 99991663, stage="members")

    agent.delivery_service.registry.adapter_for = lambda _: SimpleNamespace(
        group_authority_snapshot=fail
    )
    scope = SimpleNamespace(
        channel="feishu", user_id="ou_owner", conversation_id="oc_source", metadata={}
    )
    result = control.execute_group_authorization_control(
        agent, parse_conversation_control("/group 监控 全群"), scope
    )
    assert not result.ok and result.error_code == "GROUP_AUTHORITY_UNAVAILABLE"
    assert result.details["provider_diagnostics"] == {
        "reason_code": "FEISHU_PROVIDER_SCOPE_REQUIRED",
        "provider_code": 99991663,
        "stage": "members",
    }
    assert "群成员" in result.message and "99991663" in result.message
    assert "FEISHU_PROVIDER_SCOPE_REQUIRED" in result.message
    assert read_group_authorization(agent.home_paths)[1]["grants"] == []


@pytest.mark.parametrize(
    "stage,label",
    [
        ("chat_info", "群信息"),
        ("bot_membership", "机器人在群"),
        ("members", "群成员"),
        ("auth", "应用认证"),
        ("registry", "适配器注册"),
        ("response_validation", "响应校验"),
    ],
)
def test_group_failure_message_exposes_safe_stage_and_numeric_code(stage, label):
    result = control._result(
        False,
        "通用错误",
        code="GROUP_AUTHORITY_UNAVAILABLE",
        diagnostics={
            "reason_code": "FEISHU_PROVIDER_SCOPE_REQUIRED",
            "provider_code": 99991663,
            "stage": stage,
        },
    )
    assert label in result.message and "99991663" in result.message
    assert "FEISHU_PROVIDER_SCOPE_REQUIRED" in result.message and "群读取权限" in result.message


@pytest.mark.parametrize(
    "reason", ["FEISHU_PROVIDER_TRANSPORT_UNAVAILABLE", "FEISHU_GROUP_MEMBERS_INCOMPLETE"]
)
def test_incomplete_or_transport_failure_does_not_ask_for_new_authorization(reason):
    result = control._result(
        False,
        "请检查飞书权限后重试",
        code="GROUP_AUTHORITY_UNAVAILABLE",
        diagnostics={"reason_code": reason, "provider_code": None, "stage": "members"},
    )
    assert reason in result.message and "群成员" in result.message
    assert "检查飞书权限" not in result.message and "群读取权限" not in result.message


def test_success_control_message_is_unchanged_and_raw_diagnostics_are_sanitized():
    success = control._result(True, "原成功回执")
    assert success.message == "原成功回执"
    failure = control._result(
        False,
        "通用错误",
        diagnostics={
            "reason_code": "secret-free-text",
            "provider_code": "secret-code",
            "stage": "secret-stage",
            "body": "secret-body",
        },
    )
    assert "secret-" not in str(failure.to_dict())


def test_legacy_configuration_record_is_readable_but_not_shown_as_monitor_permission(tmp_path):
    import json

    agent, _, actor, raw, _ = _case(tmp_path)
    old = _allow(agent, raw, actor)
    old["grants"][0]["capability"] = "assistant.configure"
    path = agent.home_paths.owner_tool_policy_json
    data = json.loads(path.read_text())
    data["group_authorization"] = old
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    block = read_group_authorization(agent.home_paths)[1]
    assert block["grants"][0]["capability"] == "assistant.configure"
    assert "assistant.configure" not in control._render(block)
    assert block["grants"][0]["grant_id"] not in control._render(block)
    assert path.read_bytes() == before
