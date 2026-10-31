# LLM: 冷 IM 入口只消费可信 owner 路径和已解析配置；测试不创建 Agent、不调用模型，旧回执不会重读偏好。
# 模块用途: 验证只读委派快照的身份门、日界线、策略关闭及无写入合同。
from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent_py_agent.agent.capability import runtime_config_reload
from agent_py_agent.agent.gateway_parts import im_dispatch_policy
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings import im_task_preferences as preferences
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)

_NOW = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
# 仍受严格校验的能力字段写非法值，使能力配置真正加载失败；a12fe40c5 起决策点位已移出能力配置，
# 旧键 decision_subagent_model_mode 只告警忽略，不再构成坏配置。
_BAD_CAPABILITY_YAML = "owner_default_permission_mode: invalid\n"


# LLM: 仅用原规范纯路径工厂和 typed scope 生成合成身份，不初始化用户、Store 或 Agent。
# 函数用途: 为私聊或群创建测试输入，所有后续写入都局限于 pytest 临时目录。
def _case(root, *, owner=None, actor="actor-a", timezone="Asia/Shanghai"):
    owner = owner or OwnerIdentity.provider_user("feishu", "user-a")
    paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner))
    config = AgentConfig(enable_subagents=True, timezone=timezone)
    scope = GatewayControlScope(actor, "feishu", "chat-a", resolved_owner=owner)
    agent = SimpleNamespace(home_paths=paths, config=config, root=root / "constructor")
    return agent, scope


# LLM: 使用原控制写口准备完整记录；删除仅由测试创建的锁，便于检测只读入口是否重新写锁。
# 函数用途: 在合成用户下设置每日偏好，产品入口仍须自行读取并校验它。
def _enable(agent, scope, *, now=_NOW):
    result = preferences.set_delegation_preference(agent, scope.channel, scope.user_id, True, now=now)
    preferences.preference_path(agent).with_suffix(".json.lock").unlink(missing_ok=True)
    return result


# LLM: 快照包含文件字节、模式和修改时刻，不把不影响合同的读取 atime 当写入。
# 函数用途: 比较测试 owner 全树，捕获目录播种、锁、缓存或策略修复等意外副作用。
def _tree(root):
    if not root.exists():
        return None
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


# LLM: 直接调用公开冷入口，不能替它补造身份、缓存或策略。
# 函数用途: 复用合成输入调用产品接口，方便正反例比较原 schema。
def _capture(agent, scope):
    return im_dispatch_policy.capture_im_dispatch_for_owner(
        home_paths=agent.home_paths, config=agent.config, scope=scope,
        capability_root=agent.root, capability_config_path=getattr(agent, "capability_config_path", None),
    )


def test_cold_owner_capture_never_creates_home_or_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.core import SimpleAgent

    def forbidden(*_args, **_kwargs):
        raise AssertionError("只读快照不得构造 Agent 或 Store")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
    monkeypatch.setattr(ConversationStore, "__init__", forbidden)
    agent, scope = _case(tmp_path / "absent")
    before = _tree(agent.home_paths.root)
    captured = _capture(agent, scope)
    assert captured == {
        "mode": "original", "channel": "feishu", "actor_id": "actor-a",
        "conversation_id": "chat-a", "owner_id": agent.home_paths.owner_id,
        "preference_revision": 0, "expires_at": 0.0,
    }
    assert before is None and _tree(agent.home_paths.root) is None


def test_stored_preference_read_is_byte_and_metadata_readonly(tmp_path, monkeypatch):
    agent, scope = _case(tmp_path)
    expected = _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    before = _tree(tmp_path)
    captured = _capture(agent, scope)
    assert captured["mode"] == "delegated"
    assert captured["preference_revision"] == expected["revision"]
    assert captured["expires_at"] == expected["expires_at"]
    assert _tree(tmp_path) == before
    assert not hasattr(agent, "_capability_config_runtime_snapshot")


def test_old_agent_preference_reader_uses_same_readonly_path(tmp_path):
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    before = _tree(tmp_path)
    original = preferences.delegation_preference(agent, scope.channel, scope.user_id, now=_NOW)
    assert original["enabled"] is True
    assert _tree(tmp_path) == before
    direct = preferences.delegation_preference_by_paths(
        agent.home_paths, agent.config.timezone, scope.channel, scope.user_id, now=_NOW,
    )
    assert direct == original


@pytest.mark.parametrize("owner", [OwnerIdentity.local_main(), OwnerIdentity.provider_user("feishu", "user-a"), OwnerIdentity.provider_group("feishu", "group-a")])
def test_exact_owner_is_not_taken_from_config_fields(tmp_path, monkeypatch, owner):
    agent, scope = _case(tmp_path, owner=owner)
    _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    agent.config.my_agent_owner_provider = "foreign"
    agent.config.my_agent_owner_kind = "main"
    agent.config.my_agent_owner_id = "admin"
    assert _capture(agent, scope)["owner_id"] == agent.home_paths.owner_id
    assert _capture(agent, scope)["mode"] == "delegated"


@pytest.mark.parametrize("change", ["missing", "other-owner", "other-kind", "foreign-policy-path", "untyped-home", "raw-owner", "noncanonical-main"])
def test_missing_or_conflicting_owner_rejects_before_reads(tmp_path, monkeypatch, change):
    agent, scope = _case(tmp_path / "absent")
    if change == "missing":
        scope = replace(scope, resolved_owner=None, metadata={"owner_id": "user-a", "role": "admin"})
    elif change == "other-owner":
        scope = replace(scope, resolved_owner=OwnerIdentity.provider_user("feishu", "user-b"))
    elif change == "other-kind":
        scope = replace(scope, resolved_owner=OwnerIdentity.provider_group("feishu", "user-a"))
    elif change == "foreign-policy-path":
        agent.home_paths = replace(agent.home_paths, owner_tool_policy_json=tmp_path / "foreign.json")
    elif change == "untyped-home":
        agent.home_paths = SimpleNamespace(**vars(agent.home_paths))
    elif change == "raw-owner":
        scope = replace(scope, resolved_owner={"provider": "feishu", "owner_kind": "user", "owner_id": "user-a"})
    else:
        scope = replace(scope, resolved_owner=OwnerIdentity("local", "main", "other"))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("身份拒绝前不得读取偏好")
    monkeypatch.setattr(preferences, "delegation_preference_by_paths", forbidden, raising=False)
    with pytest.raises(ValueError, match="owner"):
        _capture(agent, scope)
    assert not (tmp_path / "absent").exists()


def test_same_actor_in_distinct_group_owners_has_independent_preference(tmp_path, monkeypatch):
    first, scope_a = _case(tmp_path, owner=OwnerIdentity.provider_group("feishu", "group-a"))
    second, scope_b = _case(tmp_path, owner=OwnerIdentity.provider_group("feishu", "group-b"))
    scope_b = replace(scope_b, conversation_id="chat-b")
    _enable(first, scope_a)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    assert _capture(first, scope_a)["mode"] == "delegated"
    assert _capture(second, scope_b)["mode"] == "original"
    assert _capture(second, scope_b)["conversation_id"] == "chat-b"
    assert not second.home_paths.owner_home_dir.exists()


@pytest.mark.parametrize("policy", ["disabled", "permissions-bad", "tool-bad", "quota-bad"])
def test_owner_policy_failures_never_enable_delegation(tmp_path, monkeypatch, policy):
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    path = {"disabled": agent.home_paths.owner_tool_policy_json,
            "permissions-bad": agent.home_paths.owner_permissions_json,
            "tool-bad": agent.home_paths.owner_tool_policy_json,
            "quota-bad": agent.home_paths.owner_quota_json}[policy]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disabled_tools": ["create_subagents"]}) if policy == "disabled" else "{broken", encoding="utf-8")
    before = _tree(tmp_path)
    assert _capture(agent, scope)["mode"] == "original"
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("switch", ["agent", "capability", "bad-capability"])
def test_scoped_config_and_capability_are_narrowing_only(tmp_path, monkeypatch, switch):
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    if switch == "agent":
        agent.config.enable_subagents = False
    else:
        path = runtime_config_reload.default_capability_config_path(agent.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_BAD_CAPABILITY_YAML if switch == "bad-capability"
                        else "im_task_delegation_enabled: false\n", encoding="utf-8")
    before = _tree(tmp_path)
    assert _capture(agent, scope)["mode"] == "original"
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("timezone,local,expected_hours", [
    ("Asia/Shanghai", "2026-10-12T23:59:00", 1 / 60),
    ("America/New_York", "2026-03-08T00:00:00", 23),
])
def test_by_paths_preserves_business_midnight_and_dst(tmp_path, timezone, local, expected_hours):
    agent, scope = _case(tmp_path, timezone=timezone)
    current = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(timezone)).timestamp()
    saved = _enable(agent, scope, now=current)
    assert saved["expires_at"] - current == pytest.approx(expected_hours * 3600)
    def read(when):
        return preferences.delegation_preference_by_paths(
            agent.home_paths, timezone, scope.channel, scope.user_id, now=when,
        )
    assert read(current)["enabled"] is True
    assert read(saved["expires_at"])["enabled"] is False
    assert read(current - 86400)["enabled"] is False


def test_bad_preference_stays_error_and_bytes_unchanged(tmp_path):
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    path = preferences.preference_path(agent)
    path.write_text("{broken", encoding="utf-8")
    before = _tree(tmp_path)
    with pytest.raises(DataCorruptionError):
        _capture(agent, scope)
    assert _tree(tmp_path) == before


def test_capability_path_priority_and_no_agent_cache(tmp_path):
    agent, _scope = _case(tmp_path)
    legacy = agent.root / "agent_py_agent/config/capability_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("im_task_delegation_enabled: false\n", encoding="utf-8")
    assert runtime_config_reload.capability_config_for_paths(root=agent.root).im_task_delegation_enabled is True
    path = runtime_config_reload.default_capability_config_path(agent.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("im_task_delegation_enabled: false\n", encoding="utf-8")
    before = _tree(tmp_path)
    assert runtime_config_reload.capability_config_for_paths(root=agent.root).im_task_delegation_enabled is False
    assert runtime_config_reload.capability_config_for_agent(agent).im_task_delegation_enabled is False
    assert _tree(tmp_path) == before
    assert agent._capability_config_runtime_snapshot.path == path


@pytest.mark.parametrize("block", ["disabled", "load-errors"])
def test_legacy_agent_keeps_live_policy_upper_bound(tmp_path, monkeypatch, block):
    from agent_py_agent.agent.gateway_parts import control_service
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _agent, _scope: agent)
    assert im_dispatch_policy.capture_im_dispatch(agent, scope) == _capture(agent, scope)
    agent.owner_policy = SimpleNamespace(
        disabled_tools=("create_subagents",) if block == "disabled" else (),
        load_errors=({"context": "owner_policy.tool_policy"},) if block == "load-errors" else (),
    )
    assert im_dispatch_policy.capture_im_dispatch(agent, scope)["mode"] == "original"


def test_legacy_agent_bad_capability_does_not_fall_back_open(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    path = runtime_config_reload.default_capability_config_path(agent.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BAD_CAPABILITY_YAML, encoding="utf-8")
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _agent, _scope: agent)
    assert im_dispatch_policy.capture_im_dispatch(agent, scope)["mode"] == "original"


def test_capture_fake_ingress_freezes_revision_for_serialized_replay(tmp_path, monkeypatch):
    agent, scope = _case(tmp_path)
    enabled = _enable(agent, scope)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    accepted = {im_dispatch_policy.IM_DISPATCH_KEY: _capture(agent, scope)}
    frozen_bytes = json.dumps(accepted, sort_keys=True).encode()
    preferences.set_delegation_preference(agent, scope.channel, scope.user_id, False, now=_NOW + 1)
    monkeypatch.setattr(preferences.time, "time", lambda: enabled["expires_at"] + 1)
    replay = json.loads(frozen_bytes)
    assert replay == accepted
    assert replay[im_dispatch_policy.IM_DISPATCH_KEY]["mode"] == "delegated"
    assert replay[im_dispatch_policy.IM_DISPATCH_KEY]["preference_revision"] == enabled["revision"]
    assert _capture(agent, scope)["mode"] == "original"


def test_tui_and_queue_only_agent_keep_absent_snapshot(tmp_path):
    agent, scope = _case(tmp_path)
    assert _capture(agent, replace(scope, channel="tui")) is None
    assert im_dispatch_policy.capture_im_dispatch(None, scope) is None


# LLM: 真实 X owner 池按原构造 root 建 Echo Agent；local provider 避开飞书姓名读取，不运行模型或服务。
# 函数用途: 构造实际 capability 来源与 owner home 不同的回归对象，防止替身自行把两根合并而掩盖错误。
def _real_pool_case(tmp_path, root_setting, owner_setting):
    root = tmp_path / "constructor"
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_subagents=True,
        memory_curator_enabled=False, timezone="Asia/Shanghai",
    )
    owner = OwnerIdentity.provider_user("local", "user-a")
    agent = OwnerScopedAgentPool(config, root).get(owner)
    actual = runtime_config_reload.capability_config_path_for(agent)
    assert agent.root == root and root != agent.home_paths.owner_home_dir
    assert actual == root / "config/capability_config.yaml"
    actual.parent.mkdir(parents=True, exist_ok=True)
    if root_setting is not None:
        actual.write_text(_BAD_CAPABILITY_YAML if root_setting == "bad"
                          else f"im_task_delegation_enabled: {root_setting}\n", encoding="utf-8")
    wrong = agent.home_paths.owner_home_dir / "config/capability_config.yaml"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text(f"im_task_delegation_enabled: {owner_setting}\n", encoding="utf-8")
    scope = GatewayControlScope("actor-a", "feishu", "chat-a", resolved_owner=owner)
    _enable(agent, scope)
    return agent, scope


@pytest.mark.parametrize("root_setting,owner_setting,expected", [
    ("false", "true", "original"), ("true", "false", "delegated"),
    (None, "false", "delegated"), ("bad", "true", "original"),
])
def test_cold_capture_matches_real_pool_constructor_origin(tmp_path, monkeypatch, root_setting, owner_setting, expected):
    agent, scope = _real_pool_case(tmp_path, root_setting, owner_setting)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    actual = runtime_config_reload.capability_config_for_agent(agent)
    assert ("delegated" if actual is not None and actual.im_task_delegation_enabled else "original") == expected
    cached = agent._capability_config_runtime_snapshot
    before = _tree(tmp_path)
    assert _capture(agent, scope)["mode"] == expected
    assert _tree(tmp_path) == before
    assert agent._capability_config_runtime_snapshot is cached


@pytest.mark.parametrize("override_setting,expected", [("true", "delegated"), ("bad", "original"), (None, "delegated")])
def test_cold_capture_matches_actual_explicit_capability_path(tmp_path, monkeypatch, override_setting, expected):
    agent, scope = _real_pool_case(tmp_path, "false", "false")
    explicit = tmp_path / "explicit/capability.yaml"
    explicit.parent.mkdir()
    if override_setting is not None:
        explicit.write_text(_BAD_CAPABILITY_YAML if override_setting == "bad"
                            else f"im_task_delegation_enabled: {override_setting}\n", encoding="utf-8")
    agent.capability_config_path = explicit
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    assert runtime_config_reload.capability_config_path_for(agent) == explicit
    actual = runtime_config_reload.capability_config_for_agent(agent)
    assert ("delegated" if actual is not None and actual.im_task_delegation_enabled else "original") == expected
    before = _tree(tmp_path)
    assert _capture(agent, scope)["mode"] == expected
    assert _tree(tmp_path) == before


def test_real_agent_cached_stricter_capability_remains_upper_bound(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service
    agent, scope = _real_pool_case(tmp_path, "false", "true")
    cached = runtime_config_reload.capability_config_for_agent(agent)
    assert cached.im_task_delegation_enabled is False
    runtime_config_reload.capability_config_path_for(agent).write_text("im_task_delegation_enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _agent, _scope: agent)
    assert _capture(agent, scope)["mode"] == "delegated"
    assert im_dispatch_policy.capture_im_dispatch(agent, scope)["mode"] == "original"
    assert runtime_config_reload.capability_config_for_agent(agent) is cached


def test_cold_api_requires_explicit_host_origin_and_no_positional_owner_default(tmp_path):
    agent, scope = _case(tmp_path / "absent")
    with pytest.raises(TypeError):
        im_dispatch_policy.capture_im_dispatch_for_owner(home_paths=agent.home_paths, config=agent.config, scope=scope)
    with pytest.raises(TypeError):
        runtime_config_reload.capability_config_for_paths(agent.home_paths)
    assert not agent.home_paths.root.exists()


@pytest.mark.parametrize("root", [None, "", 123])
def test_missing_host_origin_cannot_be_replaced_by_an_explicit_path(tmp_path, root):
    agent, scope = _case(tmp_path / "absent")
    with pytest.raises(ValueError, match="root"):
        im_dispatch_policy.capture_im_dispatch_for_owner(
            home_paths=agent.home_paths, config=agent.config, scope=scope,
            capability_root=root, capability_config_path=tmp_path / "not-read.yaml",
        )
    assert not agent.home_paths.root.exists()


@pytest.mark.parametrize("entry", ["agent-loader", "cold-loader", "agent-dispatch", "cold-dispatch"])
@pytest.mark.parametrize("failure", [ValueError, OSError])
def test_selected_user_read_failure_cannot_be_reset_by_later_removal(tmp_path, monkeypatch, entry, failure):
    from agent_py_agent.agent.gateway_parts import control_service
    agent, scope = _case(tmp_path)
    _enable(agent, scope)
    path = runtime_config_reload.default_capability_config_path(agent.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("im_task_delegation_enabled: false\n", encoding="utf-8")
    selected = []

    def fail_selected(config_path):
        assert config_path == path
        selected.append(config_path)
        path.unlink()
        raise failure("合成选定文件读取失败")

    monkeypatch.setattr(runtime_config_reload, "load_capability_config_snapshot", fail_selected)
    monkeypatch.setattr(preferences.time, "time", lambda: _NOW)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _agent, _scope: agent)
    if entry == "agent-loader":
        assert runtime_config_reload.capability_config_for_agent(agent) is None
    elif entry == "cold-loader":
        assert runtime_config_reload.capability_config_for_paths(root=agent.root) is None
    elif entry == "agent-dispatch":
        assert im_dispatch_policy.capture_im_dispatch(agent, scope)["mode"] == "original"
    else:
        assert _capture(agent, scope)["mode"] == "original"
    assert selected == [path]
    assert not path.exists()
    assert not hasattr(agent, "_capability_config_runtime_snapshot")


@pytest.mark.parametrize("entry", ["agent", "cold"])
@pytest.mark.parametrize("appear", [False, True])
def test_confirmed_missing_user_preserves_original_bundled_failure_contract(tmp_path, monkeypatch, entry, appear):
    agent, _scope = _case(tmp_path / "absent")
    path = runtime_config_reload.default_capability_config_path(agent.root)
    bundled = runtime_config_reload.bundled_capability_config_path()
    selected = []

    def fail_bundled(config_path):
        assert config_path == bundled
        selected.append(config_path)
        if appear:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("im_task_delegation_enabled: false\n", encoding="utf-8")
        raise OSError("合成随包配置读取失败")

    with monkeypatch.context() as patch:
        patch.setattr(runtime_config_reload, "load_capability_config", fail_bundled)
        if entry == "agent":
            result = runtime_config_reload.capability_config_for_agent(agent)
            assert result.im_task_delegation_enabled is True
        else:
            assert runtime_config_reload.capability_config_for_paths(root=agent.root) is None
    assert selected == [bundled]
    assert not hasattr(agent, "_capability_config_runtime_snapshot")
    if appear:
        # 本次读不重选，下一次读仍须看到新文件，不能把缺失默认值缓存成长期授权。
        result = (runtime_config_reload.capability_config_for_agent(agent) if entry == "agent"
                  else runtime_config_reload.capability_config_for_paths(root=agent.root))
        assert result.im_task_delegation_enabled is False
    else:
        assert not agent.home_paths.root.exists()


@pytest.mark.parametrize("entry", ["agent", "cold"])
def test_unconfirmed_user_presence_failure_does_not_become_default(tmp_path, monkeypatch, entry):
    from pathlib import Path
    agent, _scope = _case(tmp_path / "absent")
    path = runtime_config_reload.default_capability_config_path(agent.root)
    exists = Path.exists
    checked = []

    def fail_presence(candidate):
        if candidate == path:
            checked.append(candidate)
            raise PermissionError("合成文件存在性不可核实")
        return exists(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", fail_presence)
        result = (runtime_config_reload.capability_config_for_agent(agent) if entry == "agent"
                  else runtime_config_reload.capability_config_for_paths(root=agent.root))
        assert result is None
    assert checked == [path]
    assert not agent.home_paths.root.exists()
