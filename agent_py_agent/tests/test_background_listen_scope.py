"""后台服务监听范围：默认只允许本机回环，开放局域网要结构化声明并由用户长期授权；host 按真实 socket 表回收越界服务。"""
from __future__ import annotations

import json
import stat
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
    operation_grant_key,
)
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.background_process_host import _read_launch_spec
from agent_py_agent.agent.tooling.listen_scope import normalize_listen_scope, scope_violations
from agent_py_agent.agent.tooling.models import ApprovalPolicy, _validate_runtime_policy
from agent_py_agent.agent.tooling.process_registry import BackgroundProcess
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.tooling.shell import ShellTool, _background_listen_scope
from agent_py_agent.agent.user_space.approval_mode import (
    autonomous_tool_decision,
    execute_approval_mode_operation,
)
from agent_py_agent.agent.user_space.operation_grants import (
    owner_operation_granted,
    record_owner_operation_grant,
)
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools

BIND_ANY = "import socket,time; s=socket.socket(); s.bind(('0.0.0.0',0)); s.listen(); time.sleep(60)"
BIND_LOOP = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); time.sleep(60)"
GRANT_PARAMS = (("background_listen_scope", ("lan",)),)
LAN_KEY = "run_command:background_listen_scope=lan"


def _call(arguments: dict | None = None):
    return SimpleNamespace(call_id="call-1", tool_name="run_command", run_id="run-1", operation_id="op-1",
                           idempotency_key="idem-1", args_hash="sha256:args", turn_id="turn-1", arguments=arguments or {})


def _request(grant_key: str = ""):
    return build_tool_approval_request(_call(), request_id="req-1", round_number=1, call_index=0,
                                       description="run_command", grant_key=grant_key)


def _home(tmp_path):
    return SimpleNamespace(owner_tool_policy_json=tmp_path / "policy" / "tool_policy.json")


# 函数用途: 轮询会话记录直到条件成立或超时，避免固定 sleep。
def _wait_record(store: ProcessSessionStore, session_id: str, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    record = store.load(session_id).record
    while not predicate(record) and time.monotonic() < deadline:
        time.sleep(0.2)
        record = store.load(session_id).record
    return record


def _cleanup(hosted) -> None:
    store = ProcessSessionStore(hosted.store_root)
    current = store.load(hosted.record["session_id"]).record
    if current and current.get("status") in {"running", "starting", "unknown"}:
        stop_process_session(store, current, host_process=hosted.process)


def test_operation_grant_key_only_matches_declared_values():
    assert operation_grant_key("run_command", {"background_listen_scope": "lan"}, GRANT_PARAMS) == LAN_KEY
    assert operation_grant_key("run_command", {"background_listen_scope": " LAN "}, GRANT_PARAMS) == LAN_KEY
    assert operation_grant_key("run_command", {"background_listen_scope": "loopback"}, GRANT_PARAMS) == ""
    assert operation_grant_key("run_command", {"command": "ls"}, GRANT_PARAMS) == ""
    assert operation_grant_key("run_command", {"background_listen_scope": "lan"}, ()) == ""
    assert operation_grant_key("run_command", None, GRANT_PARAMS) == ""


def test_approval_request_adds_owner_option_only_with_a_grant_key():
    plain = _request()
    assert [option["decision"] for option in plain.options] == ["approved", "approved_session", "denied"]
    assert "grant_key" not in plain.binding
    granted = _request(LAN_KEY)
    assert granted.binding["grant_key"] == LAN_KEY
    assert [option["decision"] for option in granted.options] == ["approved", "approved_session", "approved_owner", "denied"]
    owner = next(option for option in granted.options if option["decision"] == "approved_owner")
    assert owner["id"] == "allow_owner" and "长期" in owner["label"]
    decision = ToolApprovalDecision(granted.permission_id, "approved_owner")
    assert decision.approved is True
    assert ToolApprovalDecision.from_mapping(decision.to_dict()).decision == "approved_owner"


def test_owner_grant_store_is_owner_policy_file_and_fail_closed(tmp_path):
    home = _home(tmp_path)
    assert owner_operation_granted(home, LAN_KEY) is False
    assert owner_operation_granted(SimpleNamespace(), LAN_KEY) is False, "没有策略路径的替身永远未授权"
    first = record_owner_operation_grant(home, LAN_KEY, source="test")
    assert owner_operation_granted(home, LAN_KEY) is True
    assert owner_operation_granted(home, "run_command:background_listen_scope=loopback") is False
    path = home.owner_tool_policy_json
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "tool-policy.v1" and payload.get("permission_mode", "ask") == "ask"
    assert payload["operation_grants"][LAN_KEY]["source"] == "test"
    again = record_owner_operation_grant(home, LAN_KEY, source="other")
    assert again == first, "重复记录不刷新原授权"
    with pytest.raises(ValueError):
        record_owner_operation_grant(home, "", source="test")


def test_autonomous_mode_does_not_release_an_ungranted_operation(tmp_path):
    home = _home(tmp_path)
    home.owner_tool_policy_json.parent.mkdir(parents=True)
    execute_approval_mode_operation(home, "set", "auto")
    tool = SimpleNamespace(runtime_policy=SimpleNamespace(approval_policy=ApprovalPolicy(owner_grant_parameters=GRANT_PARAMS)))
    agent = SimpleNamespace(tools=SimpleNamespace(tools={"run_command": tool}), home_paths=home)
    assert autonomous_tool_decision(agent, _request()).decision == "approved", "普通危险调用在自主模式照常放行"
    assert autonomous_tool_decision(agent, _request(LAN_KEY)) is None, "开放局域网没授权过：自主模式也要等用户"
    record_owner_operation_grant(home, LAN_KEY, source="test")
    assert autonomous_tool_decision(agent, _request(LAN_KEY)).decision == "approved"
    execute_approval_mode_operation(home, "set", "ask")
    assert autonomous_tool_decision(agent, _request(LAN_KEY)).decision == "approved", "长期授权过的类别在 ask 模式也不再询问"
    assert autonomous_tool_decision(agent, _request()) is None


def test_shell_declares_the_listen_scope_parameter_and_grant(tmp_path):
    tool = ShellTool(tmp_path)
    schema = tool.model_spec.input_schema["properties"]["background_listen_scope"]
    assert schema["enum"] == ["loopback", "lan"]
    assert tool.runtime_policy.approval_policy.owner_grant_parameters == GRANT_PARAMS
    runtime_snapshot_for_tools({tool.model_spec.name: tool}, run_id="run-scope")
    assert _background_listen_scope({}) == "loopback"
    assert _background_listen_scope({"background_listen_scope": "lan"}) == "lan"
    with pytest.raises(ValueError):
        _background_listen_scope({"background_listen_scope": "public"})
    bogus = replace(tool.runtime_policy, approval_policy=ApprovalPolicy(owner_grant_parameters=(("no_such_param", ("x",)),)))
    with pytest.raises(ValueError):
        _validate_runtime_policy(tool.model_spec, bogus)


def test_listen_scope_normalization_and_violation_rules():
    assert normalize_listen_scope(None) == "loopback" and normalize_listen_scope(" LAN ") == "lan"
    with pytest.raises(ValueError):
        normalize_listen_scope("public")
    rows = [{"host": "127.0.0.1", "port": 1, "scope": "loopback"}, {"host": "*", "port": 2, "scope": "non_loopback"}]
    assert scope_violations("loopback", rows) == [rows[1]]
    assert scope_violations("lan", rows) == []


def test_launch_request_and_spec_carry_the_listen_scope(tmp_path):
    request = managed_request(tmp_path, listen_scope=" LAN ", listen_scope_enforce=False)
    assert request.listen_scope == "lan" and request.listen_scope_enforce is False
    with pytest.raises(ValueError):
        managed_request(tmp_path / "bad", listen_scope="public")
    store = ProcessSessionStore(tmp_path / "authority")
    spec_dir = store.root / ".launches"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "bg-test.json"
    base = {"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": "bg-test", "command_argv": ["python"], "max_log_bytes": 10,
            "deadline_monotonic": 0, "stop_on_launcher_exit": False, "io_mode": "log", "activation": None}
    spec_path.write_text(json.dumps(base))
    with pytest.raises(ValueError):
        _read_launch_spec(store, "bg-test", spec_path)
    spec_path.write_text(json.dumps({**base, "listen_scope": "loopback", "listen_scope_enforce": True}))
    assert _read_launch_spec(store, "bg-test", spec_path)["listen_scope"] == "loopback"
    spec_path.write_text(json.dumps({**base, "listen_scope": "public", "listen_scope_enforce": True}))
    with pytest.raises(ValueError):
        _read_launch_spec(store, "bg-test", spec_path)


def test_host_recycles_a_loopback_declared_service_that_binds_all_interfaces(tmp_path):
    hosted = launch.start_background_process(managed_request(tmp_path, BIND_ANY, listen_scope="loopback"))
    try:
        store = ProcessSessionStore(hosted.store_root)
        record = _wait_record(store, hosted.record["session_id"], lambda r: r and r.get("status") not in {"running", "starting"})
        assert record["status"] == "killed" and record["reason"] == "listen_scope_violation"
        evidence = record["listener_violation"]
        assert evidence["enforced"] is True and evidence["listen_scope"] == "loopback"
        assert evidence["bindings"] and all(row["scope"] == "non_loopback" for row in evidence["bindings"])
        summary = BackgroundProcess.from_record(record, store_root=hosted.store_root).to_summary()
        assert summary["listen_scope"] == "loopback" and summary["reason"] == "listen_scope_violation"
        assert summary["listener_violation"]["bindings"][0]["port"] == evidence["bindings"][0]["port"]
    finally:
        _cleanup(hosted)


@pytest.mark.parametrize("code,scope", [(BIND_LOOP, "loopback"), (BIND_ANY, "lan")])
def test_allowed_listeners_keep_running(tmp_path, code, scope):
    hosted = launch.start_background_process(managed_request(tmp_path, code, listen_scope=scope))
    try:
        store = ProcessSessionStore(hosted.store_root)
        time.sleep(4.5)  # 覆盖两次监听核对
        record = store.load(hosted.record["session_id"]).record
        assert record["status"] == "running" and record["listen_scope"] == scope
        assert "listener_violation" not in record and "listener_warning" not in record
    finally:
        _cleanup(hosted)


def test_enforcement_off_only_records_a_warning(tmp_path):
    hosted = launch.start_background_process(managed_request(tmp_path, BIND_ANY, listen_scope="loopback", listen_scope_enforce=False))
    try:
        store = ProcessSessionStore(hosted.store_root)
        record = _wait_record(store, hosted.record["session_id"], lambda r: r and r.get("listener_warning"))
        assert record["status"] == "running"
        assert record["listener_warning"]["enforced"] is False and record["listener_warning"]["bindings"]
        assert "listener_violation" not in record
    finally:
        _cleanup(hosted)
