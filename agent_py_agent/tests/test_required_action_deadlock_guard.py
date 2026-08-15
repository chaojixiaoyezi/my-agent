from __future__ import annotations

"""required action 死锁守卫单测。

真机实证（click 复刻，2026-08-07）："继续干活"类任务被评估器生成 read_only ceiling
的 required action 后整体卡死——后续所有 mutating 工具（run_command/write_file）被
REQUIRED_ACTION_EFFECT_CEILING_EXCEEDED 拦截，而 approval_request 没有任何消费端
（无人工审批通道），模型误以为要用户确认，任务永久停滞。

修复两处（配套本测试）：
1. action_policy._required_action_decision：只有 open（待销账）状态的 required action
   才施加工具级执行约束；已 blocked/settled 的动作约束已失效，继续拦截只会卡死任务。
2. required_actions._validated_action_rows：allowed_tools 里任一工具的默认效果超过
   ceiling 即自相矛盾（评估提示语明确禁止 mutating 工具配 read_only ceiling），
   丢弃该动作而非让它以矛盾状态存在。
"""

from pathlib import Path

from agent_py_agent.agent.contracts.required_actions import (
    RequiredAction,
    _validated_action_rows,
)
from agent_py_agent.agent.tooling import BaseTool
from agent_py_agent.agent.tooling.action_policy import (
    ActionPolicy,
    ActionPolicyRequest,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

_RUN = "run-required-action-deadlock-test"


def _CommandTool(default_effect: str = "dangerous"):
    from agent_py_agent.agent.tooling import BaseTool as _BT

    class _FakeCommandTool(_BT):
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
            default_effect,
            strategy="command",
            command_parameter="command",
        )

        def execute(self, params):
            raise AssertionError("decide 阶段不应执行 handler")

    return _FakeCommandTool()


def _snapshot(default_effect: str = "dangerous"):
    return runtime_snapshot_for_tools(
        {"run_command": _CommandTool(default_effect)}, run_id=_RUN
    )


def _decide(
    *,
    required_action: RequiredAction | None,
    default_effect: str = "dangerous",
    tool_name: str = "run_command",
    arguments: dict[str, object] | None = None,
) -> object:
    root = Path("/tmp/my-agent-workspace")
    snapshot = _snapshot(default_effect)
    call = canonical_test_call(
        snapshot,
        tool_name,
        arguments if arguments is not None else {"command": "go build ./..."},
    )
    return ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=root,
            workspace_roots=(root,),
            write_boundary=None,
            runtime_guard_policy=None,
            required_action=required_action,
        )
    )


def _ReadTool():
    from agent_py_agent.agent.tooling import BaseTool as _BT

    class _FakeReadTool(_BT):
        model_spec = make_test_model_spec(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )
        runtime_policy = make_test_runtime_policy(
            "read_only",
            strategy="declared",
        )

        def execute(self, params):
            raise AssertionError("decide 阶段不应执行 handler")

    return _FakeReadTool()


def _decide_read(*, required_action: RequiredAction | None) -> object:
    from agent_py_agent.agent.tooling.action_policy import (
        ActionPolicy,
        ActionPolicyRequest,
    )

    root = Path("/tmp/my-agent-workspace")
    snapshot = runtime_snapshot_for_tools(
        {"read_file": _ReadTool()}, run_id=_RUN
    )
    call = canonical_test_call(
        snapshot,
        "read_file",
        {"path": "main.go"},
    )
    return ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=root,
            workspace_roots=(root,),
            write_boundary=None,
            runtime_guard_policy=None,
            required_action=required_action,
        )
    )


def _action(status: str = "open", *, ceiling: str = "read_only") -> RequiredAction:
    return RequiredAction(
        action_id="act-1",
        source_turn_id="turn-1",
        kind="execute",
        allowed_tools=("run_command",),
        effect_ceiling=ceiling,
        status=status,
    )


# ---------- _required_action_decision：状态闸 ----------


def test_blocked_required_action_does_not_block_mutating_call() -> None:
    # 已 blocked 的动作（如评估器未给任何可用证据工具而自动封死）约束已失效：
    # 继续拦截只会让"继续干活"类任务卡死（approval 无消费端）。go build 是
    # mutating/unknown 命令，必须放行继续走沙箱/边界保护。
    decision = _decide(required_action=_action(status="blocked"))

    assert decision.status == "allow", decision


def test_satisfied_required_action_does_not_block_mutating_call() -> None:
    # satisfied（已销账）动作同样不拦截。
    decision = _decide(required_action=_action(status="satisfied"))

    assert decision.status == "allow", decision


def test_open_read_only_ceiling_still_blocks_mutating_call() -> None:
    # 状态闸只豁免非 open 动作；open + read_only ceiling 的拦截语义不变。
    decision = _decide(required_action=_action(status="open", ceiling="read_only"))

    assert decision.status != "allow"
    assert "REQUIRED_ACTION_EFFECT_CEILING_EXCEEDED" in decision.reason_codes


def test_open_mutating_ceiling_allows_mutating_call() -> None:
    # mutating ceiling 与 mutating 命令同级，不再拦截。
    decision = _decide(required_action=_action(status="open", ceiling="mutating"))

    assert decision.status == "allow", decision


def test_open_action_tool_not_allowed_still_denies() -> None:
    # 工具名单外拦截语义不受状态闸影响(mutating 调用仍受名单约束)。
    action = _action(status="open", ceiling="mutating")
    action.allowed_tools = ("other_tool",)
    decision = _decide(required_action=action)

    assert decision.status != "allow"
    assert "REQUIRED_ACTION_TOOL_NOT_ALLOWED" in decision.reason_codes


def test_open_action_read_call_not_blocked_by_tool_allowlist() -> None:
    # "先读后改"前置链:action allowed_tools 只列写工具(edit_file/run_command)时,
    # 只读调用(read_file)必须放行,否则模型无法先读文件再编辑,任务卡死
    # (真机铁证 2026-08-08: 修 main.go 的 action allowed_tools=[edit_file],
    # 模型 read_file 被连拦 3 轮 break)。只读零副作用,effect_ceiling 已覆盖其上限。
    action = RequiredAction(
        action_id="act-1",
        source_turn_id="turn-1",
        kind="execute",
        allowed_tools=("edit_file",),
        effect_ceiling="mutating",
        status="open",
    )
    decision = _decide_read(required_action=action)

    assert decision.status == "allow", decision
    assert "REQUIRED_ACTION_TOOL_NOT_ALLOWED" not in (decision.reason_codes or ())


# ---------- _validated_action_rows：评估一致性校验 ----------


def test_self_contradictory_action_discarded() -> None:
    # mutating 工具配 read_only ceiling 是自相矛盾（评估提示语明确禁止）；
    # 丢弃而非保留，否则后续所有 mutating 工具被 ceiling 拦截且无审批消费端。
    rows = _validated_action_rows(
        [
            {
                "kind": "execute",
                "description": "run tests",
                "success_criteria": "tests pass",
                "allowed_tools": ["run_command"],
                "effect_ceiling": "read_only",
                "acceptable_exits": ["unfinished"],
            }
        ],
        runtime_snapshot=_snapshot(),
        run_id=_RUN,
        source_turn_id="turn-1",
    )

    assert rows == ()


def test_consistent_dangerous_ceiling_kept() -> None:
    # run_command 默认 dangerous（rank 2）配 dangerous ceiling（rank 2）不越界，保留。
    rows = _validated_action_rows(
        [
            {
                "kind": "execute",
                "description": "run tests",
                "success_criteria": "tests pass",
                "allowed_tools": ["run_command"],
                "effect_ceiling": "dangerous",
                "acceptable_exits": ["unfinished"],
            }
        ],
        runtime_snapshot=_snapshot(),
        run_id=_RUN,
        source_turn_id="turn-1",
    )

    assert len(rows) == 1
    assert rows[0].effect_ceiling == "dangerous"
    assert rows[0].status == "open"


def test_empty_allowed_tools_still_typed_blocked() -> None:
    # 无可用工具的动作保留为 blocked（typed blocked，供上层展示），不被丢弃。
    rows = _validated_action_rows(
        [
            {
                "kind": "execute",
                "description": "run tests",
                "success_criteria": "tests pass",
                "allowed_tools": [],
                "effect_ceiling": "read_only",
                "acceptable_exits": ["blocked"],
            }
        ],
        runtime_snapshot=_snapshot(),
        run_id=_RUN,
        source_turn_id="turn-1",
    )

    assert len(rows) == 1
    assert rows[0].status == "blocked"
    assert rows[0].blocked_reason == "REQUIRED_ACTION_HAS_NO_ALLOWED_TOOL"
