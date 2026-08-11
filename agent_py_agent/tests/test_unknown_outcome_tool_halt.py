from __future__ import annotations

"""T-USER-001 阻断项回归单测：informational 软约束 + UNKNOWN 后停止调用。

真机铁证(2026-08-11, deepseek-v4-flash@工具运行时, 原始 prompt=「已经联系印度方
进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)」):
  评估层判 requires_action=False(source=model_structured, informational) 正确,
  但执行层无任何约束:主循环模型把告警陈述当任务执行,9 次工具调用,其中写文件
  命令 reconcile 成 TOOL_OPERATION_OUTCOME_UNKNOWN(「工具已执行但副作用是否
  完成不确定,系统已阻止自动重做」),模型仍继续 5 次新调用。

两层修复(均结构化信号,无自然语言判定):
  L1 informational 软约束:render_required_action_guidance 在 assessment
     requires_action=False 且非 failed 时注入模型可见结构化信号,不硬禁工具
     (tool_choice 保留 auto,防 2026-08-08 TOOL_CHOICE_VIOLATION 卡死铁证)。
  L2 UNKNOWN 硬收口:_mark_unknown_outcome_halt 对连续 2 次 unknown 设
     params.unknown_outcome_halt + 注入停止提示;收口轮走
     _final_response_after_unknown_outcome_halt(without_tool_call_after_limit
     剥掉模型工具调用, runtime_status=unfinished 保持任务活跃不终端)。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._finalization_service import (
    _conversation_turn_is_terminal,
)
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _UNKNOWN_OUTCOME_HALT_THRESHOLD,
    _UNKNOWN_OUTCOME_STREAK_ATTR,
    _mark_unknown_outcome_halt,
)
from agent_py_agent.agent.contracts.required_actions import (
    render_required_action_guidance,
)

_UNKNOWN_CODE = "TOOL_OPERATION_OUTCOME_UNKNOWN"


def _result(effect_outcome: str):
    return SimpleNamespace(effect_outcome=effect_outcome)


def _params(tool_context=None, repeated_failure_halt=None, unknown_outcome_halt=None):
    return SimpleNamespace(
        repeated_failure_halt=repeated_failure_halt,
        unknown_outcome_halt=unknown_outcome_halt,
        tool_context=tool_context if tool_context is not None else [],
        **{_UNKNOWN_OUTCOME_STREAK_ATTR: 0},
    )


def _record(
    tool_name,
    result,
    params=None,
):
    return SimpleNamespace(
        params=params if params is not None else _params(),
        call=SimpleNamespace(tool_name=tool_name),
        result=result,
    )


def _streak_of(params) -> int:
    return int(getattr(params, _UNKNOWN_OUTCOME_STREAK_ATTR, 0) or 0)


class TestMarkUnknownOutcomeHalt:
    def test_halt_after_two_consecutive_unknown(self):
        params = _params()
        _mark_unknown_outcome_halt(
            None, _record("write_file", _result("unknown"), params)
        )
        assert params.unknown_outcome_halt is None
        assert _streak_of(params) == 1
        _mark_unknown_outcome_halt(
            None, _record("write_file", _result("unknown"), params)
        )
        assert params.unknown_outcome_halt == (
            "write_file",
            _UNKNOWN_CODE,
            _UNKNOWN_OUTCOME_HALT_THRESHOLD,
        )
        joined = "\n".join(params.tool_context)
        assert "副作用结果不确定" in joined
        assert "停止发起任何新的工具调用" in joined

    def test_no_halt_after_single_unknown(self):
        params = _params()
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        assert params.unknown_outcome_halt is None
        assert _streak_of(params) == 1
        assert params.tool_context == [], "单次 unknown 只累计,不注入提示"

    def test_confirmed_interrupts_streak(self):
        params = _params()
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        _mark_unknown_outcome_halt(None, _record("read_file", _result("confirmed"), params))
        assert _streak_of(params) == 0, "confirmed 清零连续段"
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        assert params.unknown_outcome_halt is None, "中断后重新从 1 累计"
        assert _streak_of(params) == 1

    def test_success_resets_streak(self):
        params = _params()
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        _mark_unknown_outcome_halt(None, _record("read_file", _result(""), params))
        assert _streak_of(params) == 0
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        assert params.unknown_outcome_halt is None

    def test_halt_is_idempotent(self):
        params = _params(
            unknown_outcome_halt=("write_file", _UNKNOWN_CODE, 2),
            tool_context=["已有提示"],
        )
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        assert params.unknown_outcome_halt == ("write_file", _UNKNOWN_CODE, 2)
        assert params.tool_context == ["已有提示"], "已收口不得重复追加提示"

    def test_repeated_failure_halt_short_circuits(self):
        params = _params(repeated_failure_halt=("write_file", "SOME_ERROR", 8))
        _mark_unknown_outcome_halt(None, _record("write_file", _result("unknown"), params))
        assert params.unknown_outcome_halt is None
        assert _streak_of(params) == 0, "已有失败收口时 unknown 不参与累计"

    def test_non_unknown_effect_never_halt(self):
        for effect in ("confirmed", "none", "", None, "rejected"):
            params = _params()
            _mark_unknown_outcome_halt(
                None,
                _record("write_file", _result(effect), params),
            )
            assert params.unknown_outcome_halt is None
            assert _streak_of(params) == 0


class TestRenderRequiredActionGuidance:
    def _snapshot(self, assessment=None, actions=()):
        return SimpleNamespace(
            required_actions=actions,
            required_action_assessment=assessment,
        )

    def test_informational_assessment_injects_guidance(self):
        snapshot = self._snapshot(
            assessment={"requires_action": False, "source": "model_structured"}
        )
        text = render_required_action_guidance(snapshot)
        assert "requires_action" in text
        assert "信息性陈述" in text
        assert "不要求执行任何操作" in text
        assert text.startswith("[tool-system:required-action-assessment]")

    def test_failed_assessment_no_injection(self):
        # assessment 本身失败(error 非空)说明判据不可信 → 不注入 informational 文案
        snapshot = self._snapshot(
            assessment={
                "requires_action": False,
                "source": "model_structured",
                "error": "boom",
            }
        )
        assert render_required_action_guidance(snapshot) == ""

    def test_non_model_source_no_injection(self):
        snapshot = self._snapshot(
            assessment={"requires_action": False, "source": "tool_operation"}
        )
        assert render_required_action_guidance(snapshot) == ""

    def test_no_assessment_empty(self):
        assert render_required_action_guidance(self._snapshot(assessment=None)) == ""

    def test_actions_present_keeps_original_behavior(self):
        action = SimpleNamespace(
            action_id="a1",
            kind="execution",
            allowed_tools=("run_command",),
            effect_ceiling="full",
            status="open",
            success_criteria=("c1",),
            blocked_reason=None,
        )
        snapshot = self._snapshot(
            assessment={"requires_action": False, "source": "model_structured"},
            actions=(action,),
        )
        text = render_required_action_guidance(snapshot)
        assert text.startswith("[tool-system:required-actions]")
        assert "开放动作必须以真实 ToolResult 销账" in text
        assert "信息性陈述" not in text, "有开放动作时走原渲染,不注入 informational"

    def test_assessment_not_dict_no_injection(self):
        snapshot = SimpleNamespace(
            required_actions=(), required_action_assessment="not-a-dict"
        )
        assert render_required_action_guidance(snapshot) == ""


class TestUnknownOutcomeTurnNotTerminal:
    def test_unfinished_status_keeps_task_active(self):
        # runtime_status 非 ok → 任务不终端:收口后保持活跃,不自动收掉任务
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(
                runtime_status="unfinished", runtime_reason=_UNKNOWN_CODE
            )
        )
        assert _conversation_turn_is_terminal(ctx) is False
