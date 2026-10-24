from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.contracts.gates.path_url_command import (
    PathUrlCommandFacts,
    evaluate_path_url_command_gate,
)
from agent_py_agent.agent.contracts.gates.state_event_ledger import (
    StateEventLedgerSnapshot,
    evaluate_state_event_ledger_gate,
)
from agent_py_agent.agent.contracts.gates.tool_approval_binding import (
    ApprovalBindingFacts,
    evaluate_approval_binding_gate,
)


def test_path_url_command_gate_blocks_escape_private_url_and_shell_operators(tmp_path: Path):
    workspace = _workspace_with_symlink_escape(tmp_path)

    path_escape = _path_gate({"tool": "read_file", "path": "link"}, workspace)
    private_url = _path_gate({"tool": "web_fetch", "url": "http://127.1/admin"}, workspace)
    file_url = _path_gate({"tool": "web_fetch", "url": "file:///etc/passwd"}, workspace)
    command_operator = _path_gate({"tool": "run_command", "command": "python build.py && python test.py"}, workspace)
    passed = _path_gate({"tool": "run_command", "command": ["python3", "--version"], "working_dir": "."}, workspace)

    assert path_escape.finding_codes == ("PATH_SYMLINK_ESCAPE_BLOCKED",)
    assert private_url.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert file_url.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert command_operator.finding_codes == ("COMMAND_SHELL_OPERATOR_BLOCKED",)
    assert passed.allowed is True


def test_path_url_command_gate_allows_only_declared_owner_scoped_file_url(tmp_path: Path):
    owner = tmp_path / "owners" / "user-a"
    owner.mkdir(parents=True)
    source = owner / "events.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    allowed = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload={"tool": "watch_stream", "url": source.as_uri()},
            workspace_root=owner,
            workspace_roots=[owner],
            owner_scope_root=str(owner),
            local_file_url_fields=("url",),
        )
    )
    undeclared = _path_gate(
        {"tool": "web_fetch", "url": source.as_uri()},
        owner,
    )
    dangerous = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload={"tool": "watch_stream", "url": "file:///etc/passwd"},
            workspace_root=owner,
            workspace_roots=[owner],
            owner_scope_root=str(owner),
            local_file_url_fields=("url",),
        )
    )

    assert allowed.allowed is True
    assert undeclared.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert dangerous.finding_codes == ("PATH_OWNER_SCOPE_BLOCKED",)


def test_path_url_command_gate_blocks_argv_catastrophic_executable(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "argv": ["mkfs.ext4", "/dev/sda1"]}, tmp_path)

    assert decision.finding_codes == ("COMMAND_DANGEROUS_EXECUTABLE_BLOCKED",)
    assert decision.findings[0].evidence["executable"] == "mkfs.ext4"


def test_path_url_command_gate_blocks_string_dangerous_pattern(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "command": "sudo rm -rf /etc"}, tmp_path)

    assert decision.finding_codes == ("COMMAND_DANGEROUS_PATTERN_BLOCKED",)
    assert decision.findings[0].evidence["pattern"] == "RM_PROTECTED_TARGET"


def test_path_url_command_gate_allows_read_only_command(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "command": "git status --short"}, tmp_path)

    assert decision.allowed is True


def test_approval_binding_gate_rejects_mismatched_args_hash_or_run():
    approved = _approval_record()
    wrong_args = evaluate_approval_binding_gate(_approval_facts(approved, args_hash="sha256:def"))
    wrong_run = evaluate_approval_binding_gate(_approval_facts(approved, run_id="run-2"))
    passed = evaluate_approval_binding_gate(_approval_facts(approved))

    assert wrong_args.finding_codes == ("APPROVAL_BINDING_MISMATCH",)
    assert wrong_run.finding_codes == ("APPROVAL_BINDING_MISMATCH",)
    assert passed.allowed is True
    assert passed.evidence["approval_id"] == "approval-1"


def test_state_event_ledger_gate_requires_events_and_blocks_late_or_duplicate_actions():
    missing_event = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot("run-1", "RUNNING", transitions=[{"from_status": "PLANNING", "to_status": "RUNNING"}])
    )
    done_dispatch = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot(
            "run-1",
            "DONE",
            next_action="dispatch",
            events=[{"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"}],
        )
    )
    expired_lease_tool_result = evaluate_state_event_ledger_gate(_expired_lease_snapshot())
    passed = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot(
            "run-1",
            "VERIFYING",
            transitions=[{"from_status": "RUNNING", "to_status": "VERIFYING", "event_id": "evt-1"}],
            events=[{"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"}],
        )
    )

    assert missing_event.finding_codes == ("STATE_EVENT_LEDGER_EVENT_MISSING",)
    assert missing_event.recommended_action == "repair"
    assert done_dispatch.finding_codes == ("STATE_EVENT_LEDGER_TERMINAL_ACTION_BLOCKED",)
    assert expired_lease_tool_result.finding_codes == ("STATE_EVENT_LEDGER_LEASE_EXPIRED_RESULT",)
    assert passed.allowed is True


def _workspace_with_symlink_escape(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)
    return workspace


def _path_gate(payload: dict[str, object], workspace: Path):
    return evaluate_path_url_command_gate(PathUrlCommandFacts(payload, workspace, workspace_roots=[workspace]))


def _approval_record() -> dict[str, object]:
    return {
        "approval_id": "approval-1",
        "status": "APPROVED",
        "tool_name": "controlled_exec",
        "run_id": "run-1",
        "operation_id": "op-1",
        "idempotency_key": "idem-1",
        "args_hash": "sha256:abc",
    }


def _approval_facts(
    approved: dict[str, object],
    *,
    run_id: str = "run-1",
    args_hash: str = "sha256:abc",
) -> ApprovalBindingFacts:
    return ApprovalBindingFacts(
        "controlled_exec",
        run_id=run_id,
        operation_id="op-1",
        idempotency_key="idem-1",
        args_hash=args_hash,
        approved_actions=(approved,),
    )


def _expired_lease_snapshot() -> StateEventLedgerSnapshot:
    return StateEventLedgerSnapshot(
        "run-1",
        "WAITING_FOR_TOOL",
        lease_status="EXPIRED",
        events=[
            {"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"},
            {"event_id": "evt-2", "run_id": "run-1", "event_type": "tool_result", "operation_id": "op-1"},
        ],
    )


# LLM: 预检只判字面目标，不做 DNS；格式必须与真正出站门共享。
# 函数用途: 覆盖 CIDR、回环别名与无效容器的预检行为。
@pytest.mark.parametrize(("host", "entries", "allowed"), [
    ("localhost", ["127.0.0.0/8"], True),
    ("localhost", ["127.0.0.1"], True),
    ("10.2.3.4", ["10.0.0.0/8"], True),
    ("192.168.0.50", ["192.168.0.0/24"], True),
    ("[::ffff:192.168.0.50]", ["192.168.0.0/24"], True),
    ("localhost", "localhost", False),
    ("127.0.0.1", ["0.0.0.0/0"], False),
])
def test_path_preflight_private_allowlist_formats(tmp_path, host, entries, allowed):
    decision = evaluate_path_url_command_gate(PathUrlCommandFacts(
        payload={"tool": "web_fetch", "url": f"http://{host}:8000/"},
        workspace_root=tmp_path, allowed_private_hosts=entries,
    ))
    assert decision.allowed is allowed


# LLM: 每次调用授权与构造配置取并集；异常退出也不能把临时授权留在 handler。
# 函数用途: 覆盖调用上下文授权并集和 finally 恢复。
@pytest.mark.parametrize("raises", [False, True])
def test_invocation_private_grant_unions_and_restores(raises):
    from agent_py_agent.agent.tooling.registry_invoke import _execute_with_temporary_tool_context
    from agent_py_agent.agent.tooling.web import WebFetchTool

    tool = WebFetchTool(max_chars=1000, timeout=5, allowed_private_hosts=("git.corp.local",), allow_private_resolution=False)
    original = tool.allowed_private_hosts
    seen = []

    # LLM: 记录真正 callback 执行期间的授权，不以调用发生代替正确授权断言。
    # 函数用途: 观察临时并集并可选模拟 handler 异常。
    def callback():
        seen.append(tool.allowed_private_hosts)
        if raises:
            raise RuntimeError("probe")
        return "ok"

    kwargs = dict(workspace_roots=None, allowed_private_hosts=("10.0.0.0/8", "git.corp.local"),
                  allow_private_resolution=True, callback=callback)
    if raises:
        with pytest.raises(RuntimeError, match="probe"):
            _execute_with_temporary_tool_context(tool, **kwargs)
    else:
        assert _execute_with_temporary_tool_context(tool, **kwargs) == "ok"
    assert seen == [("git.corp.local", "10.0.0.0/8")]
    assert tool.allowed_private_hosts is original
    assert tool.allow_private_resolution is False


# LLM: 配置白名单须同时进入预检和 handler，边界额外授权不能取代配置。
# 函数用途: 测动作策略的配置与逐调用主机并集。
@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.0.50"])
def test_action_policy_preflight_unions_configured_hosts(tmp_path, host):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.action_policy import _path_url_command_decision
    from agent_py_agent.agent.tooling.web import WebFetchTool

    tool = WebFetchTool(max_chars=1000, timeout=5, allowed_private_hosts=("127.0.0.0/8",))
    request = SimpleNamespace(
        call=SimpleNamespace(tool_name="web_fetch", arguments={"url": f"http://{host}:8000/"}),
        write_boundary={"allowed_private_hosts": ["192.168.0.0/24"], "allow_private_resolution": False},
        workspace_root=tmp_path, workspace_roots=[tmp_path], path_access_mode="full-access",
        path_dangerous_roots=(), owner_scope_root="",
    )
    runtime = SimpleNamespace(handler=tool, runtime_policy=tool.runtime_policy)
    assert _path_url_command_decision(request, runtime, "read_only") is None


# LLM: YAML/字典或直接构造都得到字符串列表，裸字符串不拆成单字授权。
# 函数用途: 测配置加载和直接构造的白名单类型约束与默认独立性。
@pytest.mark.parametrize(("value", "expected"), [
    ([" git.corp.local ", "10.0.0.0/8", 1, None], ["git.corp.local", "10.0.0.0/8"]),
    ("localhost", []), (None, []), ({"localhost": True}, []),
])
def test_private_allowlist_config_normalizes_input(tmp_path, value, expected):
    from agent_py_agent.agent.settings.config import AgentConfig, load_config

    assert "allowed_private_hosts" in AgentConfig.__dataclass_fields__
    direct = AgentConfig(allowed_private_hosts=value)
    assert direct.allowed_private_hosts == expected
    path = tmp_path / "agent.yaml"
    path.write_text("allowed_private_hosts: " + repr(value) + "\n", encoding="utf-8")
    assert load_config(path).allowed_private_hosts == expected
    direct.allowed_private_hosts.append("not-shared.test")
    assert AgentConfig().allowed_private_hosts == []


# LLM: 真实 Registry 构造必须把配置交给抓取 handler；仅定义配置键不代表已接线。
# 函数用途: 覆盖配置到 Registry bootstrap 的允许集合传递。
def test_registry_bootstrap_preserves_private_allowlist(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    assert "allowed_private_hosts" in ToolRegistryParams.__dataclass_fields__
    hosts = ("git.corp.local", "*.corp.local", "10.0.0.0/8")
    params = ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=10, max_matches=10,
        web_max_chars=1000, http_timeout=5, catalog_limit=30, retrieval_limit=10,
        vector_search_enabled=False, allowed_private_hosts=hosts,
    )
    assert ToolRegistry(params).tools["web_fetch"].allowed_private_hosts == hosts
