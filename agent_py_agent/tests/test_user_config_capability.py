"""LLM: 用户级配置能力的合同单测（R249）。

真机问题：用户问"compact 阈值能不能改"，模型没有任何入口、也不知道用户配置在哪，于是凭空答
"我没有修改 compact 阈值的工具或权限"。本文件守住三条：白名单外一律拒、安全边界结构性拒、
读取必须区分"用户配置"与"随包默认"。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    TUNABLE_KEYS,
    read_config_fact,
    set_tunable_value,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at


def _files(tmp_path: Path) -> tuple[Path, Path]:
    user = tmp_path / "desktop.yaml"
    packaged = tmp_path / "agent_config.yaml"
    user.write_text("my_agent_home: /tmp/home\n", encoding="utf-8")
    packaged.write_text(
        "my_agent_home: /tmp/home\nmemory_compact_auto_trigger_percent: 90\n",
        encoding="utf-8",
    )
    return user, packaged


# LLM: 安全边界必须结构性拒绝，且给出原因——模型不能靠"试试看"绕过。
# 函数用途: 验证权限/凭据类键永远不可写。
def test_boundary_keys_are_never_writable(tmp_path):
    user, _packaged = _files(tmp_path)
    for key in ("access_mode", "api_key", "my_agent_home"):
        assert key in BOUNDARY_KEYS
        report = set_tunable_value(key, "full-access", user_path=user)
        assert report["ok"] is False
        assert "安全边界" in report["error"]


# LLM: 白名单内的键要校验取值区间，越界/非数字都必须拒绝并说明合法范围。
# 函数用途: 验证取值校验。
def test_tunable_value_is_validated(tmp_path):
    user, _packaged = _files(tmp_path)
    assert set_tunable_value("memory_compact_auto_trigger_percent", "150", user_path=user)["ok"] is False
    assert set_tunable_value("memory_compact_auto_trigger_percent", "abc", user_path=user)["ok"] is False
    report = set_tunable_value("memory_compact_auto_trigger_percent", "80", user_path=user)
    assert report["ok"] is True
    assert report["saved"] == "80"
    assert report["written_value_matches"] is True
    assert "生效" in report["effect_text"]
    assert "80" in user.read_text(encoding="utf-8")


# LLM: 生效值来源必须如实区分：用户配置写了就是 user_config，没写才回落 packaged_default。
# 模型把随包默认当用户配置答出来，正是真机里那次错误回答的形态。
# 函数用途: 验证来源标注。
def test_source_distinguishes_user_and_packaged(tmp_path):
    user, packaged = _files(tmp_path)
    fact = read_config_fact(
        "memory_compact_auto_trigger_percent", user_path=user, default_path=packaged
    )
    assert fact["source"] == "packaged_default"
    assert fact["user_value"] is None

    set_tunable_value("memory_compact_auto_trigger_percent", "70", user_path=user)
    fact = read_config_fact(
        "memory_compact_auto_trigger_percent", user_path=user, default_path=packaged
    )
    assert fact["source"] == "user_config"
    assert fact["effective"] == "70"


# LLM: 没有用户配置文件位置时不能退化成"写随包默认"，必须拒绝并告诉人工编辑哪一份。
# 函数用途: 验证缺用户配置路径时拒绝写入。
def test_missing_user_path_refuses_write(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    report = set_tunable_value("memory_compact_auto_trigger_percent", "80", user_path=None)
    assert report["ok"] is False
    assert "MY_AGENT_CONFIG" in report["error"]
    assert "memory_compact_auto_trigger_percent" in TUNABLE_KEYS


def test_original_user_config_decision_actions_share_service_and_cas(tmp_path):
    host = host_at(tmp_path)
    tool = UserConfigTool(host)
    read = tool.execute({"action": "decision_read"})
    assert read.ok
    initial = json.loads(read.output)
    saved = tool.execute({"action": "decision_patch", "expected_revision": initial["revision"], "changes": {"enabled": True, "timeout_seconds": 3}})
    assert saved.ok and json.loads(saved.output)["effective"]["timeout_seconds"] == 3
    conflict = tool.execute({"action": "decision_patch", "expected_revision": initial["revision"], "changes": {"timeout_seconds": 8}})
    assert not conflict.ok and conflict.error_code == "STALE_VERSION"
    assert conflict.reported_error_code == "DECISION_SETTINGS_CONFLICT" and conflict.effect_outcome == "not_started"
    reset = tool.execute({"action": "decision_reset", "expected_revision": json.loads(saved.output)["revision"], "fields": ["timeout_seconds"]})
    assert reset.ok and json.loads(reset.output)["effective"]["timeout_seconds"] == 2


def test_decision_tool_uses_current_runner_thread_and_refuses_explicit_identity(tmp_path):
    host = host_at(tmp_path)
    store = host.conversation_store.threads
    current = store.get_or_create({"canonical_user_id": "alice", "owner_id": "alice", "channel_conversation_id": "current"})
    other = store.get_or_create({"canonical_user_id": "alice", "owner_id": "alice", "channel_conversation_id": "other"})
    host._current_task_attributes = {"conversation_thread_id": other.thread_id}
    previous = set_current_subagent_context(host, run_id="trusted-run", task_attributes={"agent_thread_id": current.thread_id, "conversation_thread_id": other.thread_id})
    try:
        tool = UserConfigTool(host)
        read = json.loads(tool.execute({"action": "decision_read", "scope": "thread"}).output)
        assert read["thread_id"] == current.thread_id
        saved = tool.execute({"action": "decision_patch", "scope": "thread", "expected_revision": read["revision"], "changes": {"enabled": True}})
        assert saved.ok
        assert store.load(current.thread_id).decision_settings["overrides"] == {"enabled": True}
        assert store.load(other.thread_id).decision_settings["overrides"] == {}
        for field, value in (("thread_id", other.thread_id), ("owner_id", "bob"), ("run_id", "other-run")):
            rejected = tool.execute({"action": "decision_read", field: value})
            assert not rejected.ok and rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    finally:
        restore_current_subagent_context(host, previous)


def test_decision_tool_without_trusted_thread_refuses_temporary_settings(tmp_path):
    tool = UserConfigTool(host_at(tmp_path))
    result = tool.execute({"action": "decision_patch", "scope": "thread", "expected_revision": {"owner": 0, "thread": 0}, "changes": {"enabled": True}})
    assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
    assert not UserConfigTool().execute({"action": "decision_read"}).ok


def test_decision_tool_owner_check_and_unavailable_service_can_be_disabled(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    provider_id = data["profiles"][key]["provider_id"]
    execute_model_profile_operation(host, "save_provider", {"provider_id": provider_id, "editing": True, "clear_key": True, "provider": {"enabled": False}})
    tool = UserConfigTool(host)
    first = json.loads(tool.execute({"action": "decision_read"}).output)
    saved = tool.execute({"action": "decision_patch", "expected_revision": first["revision"], "changes": {"enabled": True, "profile_id": key}})
    assert saved.ok and not json.loads(saved.output)["effective"]["points"]["recall"]["connection"]["configured"]
    disabled = tool.execute({"action": "decision_patch", "expected_revision": json.loads(saved.output)["revision"], "changes": {"enabled": False}})
    assert disabled.ok and json.loads(disabled.output)["effective"]["enabled"] is False
    assert "only-private-secret" not in disabled.output
    foreign = host.conversation_store.threads.get_or_create({"canonical_user_id": "bob", "owner_id": "bob", "channel_conversation_id": "foreign"})
    previous = set_current_subagent_context(host, run_id="trusted-run", task_attributes={"agent_thread_id": foreign.thread_id})
    try:
        result = tool.execute({"action": "decision_read"})
        assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
    finally:
        restore_current_subagent_context(host, previous)
