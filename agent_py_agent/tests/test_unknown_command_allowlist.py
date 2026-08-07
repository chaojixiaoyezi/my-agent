from __future__ import annotations

"""unknown 命令分类门与额度制测试。

根因（真机 2026-08-07）：go/echo 等开发命令不在内置分类表 → 分类 unknown →
COMMAND_CLASSIFICATION_UNKNOWN 进入人工审批 → 但 approval_request 无任何消费端
（无人审批通道），ask 让任务永久卡死。

修复（用户设计：普通用户零负担，不做人工审批；额度按代理实例分桶）：
- 决策层（action_policy）：unknown 命令不做审批；白名单内放行，白名单外也放行，
  沙箱/边界/灾难命令保护兜底，额度由前置 runtime guard 层管理
- 守卫层（agent_budget_stage / agent_budget）：unknown 命令按【代理实例】滚动
  窗口额度（默认 200 次/600 秒，0=关闭），超限暂时拒绝（TOOL_RATE_LIMIT_EXCEEDED），
  窗口滚动过自动恢复；每个代理（主/子）独立一桶，用户间不共用
- 配置：unknown_command_allowlist 白名单内免额度；不配任何东西也自动生效
"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_guard.agent_budget import (
    check_unknown_command_budget,
)
from agent_py_agent.agent.contracts.gates.command_policy import analyze_command
from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy
from agent_py_agent.agent.tooling.action_policy import (
    ActionPolicy,
    ActionPolicyRequest,
    _unknown_command_allowlist,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

_RUN = "run-unknown-budget-test"


def _runtime_guard(values: dict[str, object] | None) -> RuntimeGuardPolicy:
    return RuntimeGuardPolicy(values=dict(values or {}))


def _agent(values: dict[str, object] | None):
    return SimpleNamespace(runtime_guard_policy=_runtime_guard(values))


def _CommandTool():
    from agent_py_agent.agent.tooling import BaseTool

    class _FakeCommandTool(BaseTool):
        model_spec = make_test_model_spec(
            "run_command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        )
        runtime_policy = make_test_runtime_policy(
            "dangerous",
            strategy="command",
            command_parameter="command",
        )

        def execute(self, params):
            raise AssertionError("decide 阶段不应执行 handler")

    return _FakeCommandTool()


def _decision(values: dict[str, object], command: str, *, run_id: str = _RUN):
    root = Path("/tmp/my-agent-workspace")
    snapshot = runtime_snapshot_for_tools(
        {"run_command": _CommandTool()}, run_id=run_id
    )
    call = canonical_test_call(
        snapshot,
        "run_command",
        {"command": command},
    )
    return ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=root,
            workspace_roots=(root,),
            write_boundary=None,
            runtime_guard_policy=_runtime_guard(values),
        )
    )


# ---------- 决策层：unknown 不审批、白名单放行 ----------


def test_unknown_command_passes_decision_layer() -> None:
    # 决策层不拦 unknown（审批死路已移除；额度在前置守卫层按 agent 管）
    decision = _decision({}, "go version")
    assert decision.status == "allow", decision


def test_dangerous_command_never_bypasses_allowlist() -> None:
    # rm 是 dangerous 分类（非 unknown），白名单/额度都不能放行
    decision = _decision(
        {"unknown_command_allowlist": ["rm", "go"], "unknown_command_max_calls": 1000},
        "rm -rf /tmp/x",
    )
    assert decision.status != "allow"
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.reason_codes


def test_allowlist_helper_parses_values() -> None:
    assert _unknown_command_allowlist(None) == frozenset()
    assert _unknown_command_allowlist(RuntimeGuardPolicy(values=None)) == frozenset()
    assert _unknown_command_allowlist(
        RuntimeGuardPolicy(values={"unknown_command_allowlist": [" go ", "", "go test"]})
    ) == frozenset({"go", "go test"})
    # 非 list/tuple 类型一律视为未配置
    assert _unknown_command_allowlist(
        RuntimeGuardPolicy(values={"unknown_command_allowlist": "go"})
    ) == frozenset()


def test_command_still_classified_unknown_by_analyzer() -> None:
    # 基线：确认 go 确实不被内置分类表识别（unknown 走额度制的前提）
    analysis = analyze_command("go version")
    assert analysis.classification == "unknown"


# ---------- 守卫层：unknown 命令按代理实例滚动额度 ----------


def test_unknown_command_budget_allows_known_classifications() -> None:
    # 非 unknown（ls=read_only、mkdir=mutating、rm=危险）不走 unknown 额度
    agent = _agent({"unknown_command_max_calls": 1})
    assert check_unknown_command_budget(agent, "run_command", "ls -la", now=10.0) is None
    assert check_unknown_command_budget(agent, "run_command", "mkdir -p x", now=11.0) is None


def test_unknown_command_budget_allowlist_exempt() -> None:
    # 白名单内命令免额度
    agent = _agent(
        {
            "unknown_command_allowlist": ["go", "cd"],
            "unknown_command_max_calls": 1,
            "unknown_command_window_seconds": 600,
        }
    )
    assert check_unknown_command_budget(agent, "run_command", "go version", now=10.0) is None
    assert check_unknown_command_budget(agent, "run_command", "cd x && go env GOOS", now=11.0) is None
    assert check_unknown_command_budget(agent, "run_command", "go env GOARCH", now=12.0) is None


def test_unknown_command_budget_blocks_after_window_limit() -> None:
    agent = _agent(
        {"unknown_command_max_calls": 2, "unknown_command_window_seconds": 600}
    )
    assert check_unknown_command_budget(agent, "run_command", "go version", now=10.0) is None
    assert check_unknown_command_budget(agent, "run_command", "go env GOOS", now=11.0) is None
    blocked = check_unknown_command_budget(agent, "run_command", "go env GOARCH", now=12.0)
    assert blocked is not None
    assert blocked.ok is False
    assert blocked.error_code == "TOOL_RATE_LIMIT_EXCEEDED"
    assert "unknown 命令窗口额度已用完" in blocked.output


def test_unknown_command_budget_scoped_per_agent_instance() -> None:
    # 用户规格：主代理与子代理各独立额度（不同 agent 实例不共用）
    agent_a = _agent({"unknown_command_max_calls": 1, "unknown_command_window_seconds": 600})
    agent_b = _agent({"unknown_command_max_calls": 1, "unknown_command_window_seconds": 600})
    assert check_unknown_command_budget(agent_a, "run_command", "go version", now=10.0) is None
    # agent_b 是独立实例，不受 agent_a 已用额度影响
    assert check_unknown_command_budget(agent_b, "run_command", "go version", now=10.0) is None
    # agent_a 再次调用超限
    blocked = check_unknown_command_budget(agent_a, "run_command", "go env GOOS", now=11.0)
    assert blocked is not None


def test_unknown_command_budget_rolls_window() -> None:
    agent = _agent(
        {"unknown_command_max_calls": 1, "unknown_command_window_seconds": 60}
    )
    assert check_unknown_command_budget(agent, "run_command", "go version", now=100.0) is None
    blocked = check_unknown_command_budget(agent, "run_command", "go env GOOS", now=101.0)
    assert blocked is not None
    # 窗口滚动过后自动恢复
    assert check_unknown_command_budget(agent, "run_command", "go env GOARCH", now=200.0) is None


def test_unknown_command_budget_zero_disables() -> None:
    agent = _agent({"unknown_command_max_calls": 0})
    assert check_unknown_command_budget(agent, "run_command", "go version", now=10.0) is None
    assert check_unknown_command_budget(agent, "run_command", "go version", now=11.0) is None


def test_unknown_command_budget_ignores_non_command_tools() -> None:
    agent = _agent({"unknown_command_max_calls": 1})
    assert check_unknown_command_budget(agent, "write_file", "go version", now=10.0) is None
