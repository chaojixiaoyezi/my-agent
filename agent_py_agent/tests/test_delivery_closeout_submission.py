from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop_completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends import ModelResponse


# LLM: normal tool rounds are work-in-progress and must not trigger final delivery acceptance.
# 函数用途: 验证主代理工具轮结束后只记录进展；没有显式提交时不跑 delivery closeout。
def test_tool_round_without_acceptance_submit_does_not_run_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("read_file")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL read_file]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


# LLM: submit_for_acceptance is the explicit, generic handoff from model work to machine closeout.
# 函数用途: 验证模型显式提交验收时才触发主代理交付验收。
def test_tool_round_with_acceptance_submit_runs_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    assert (tmp_path / ".agent_delivery" / "closeout.json").exists()


# LLM: final text without tool calls must remain ordinary text, not delivery acceptance.
# 函数用途: 验证模型直接说完成不会触发交付验收；只有 submit_for_acceptance 才能提交验收。
def test_no_tool_final_answer_does_not_submit_delivery_acceptance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


# LLM: closeout is the single source of delivery rework guidance.
# 函数用途: 验证验收失败后的返工建议直接由 closeout 上下文提供，不依赖独立 delivery repair 运行门。
def test_failed_closeout_context_includes_unified_repair_guidance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    payload = _last_tool_context_payload(params)

    assert response is None
    assert payload["ok"] is False
    assert payload["repair_guidance"]["mode"] == "closeout_rework"
    assert payload["repair_guidance"]["required_actions"]
    assert "如果还需要读取或搜索" in payload["repair_guidance"]["message_zh"]


# LLM: Work-in-progress narration is indistinguishable from final prose for delivery purposes.
# 函数用途: 覆盖长任务中任何无工具文本都不会触发 closeout，避免系统从字面意思猜验收时机。
def test_no_tool_working_text_does_not_submit_delivery(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="我继续阅读更多项目，然后再汇总写报告。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


# LLM: Existing closeout context should not turn later plain text into another acceptance attempt.
# 函数用途: 验证已有 closeout 返工上下文时，无工具文本仍只是普通回复，不再二次提交验收。
def test_existing_closeout_context_does_not_make_plain_text_submit(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.tool_context.append("[delivery-contract-check]\n{}")
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert params.tool_context == ["[delivery-contract-check]\n{}"]


# LLM: delivery rework should not add a separate tool-loop gate over normal read/list choices.
# 函数用途: 验证已有 closeout 返工事实时，模型的只读工具调用不再被 delivery repair 分支单独拦截。
def test_failed_closeout_does_not_intercept_read_tools_with_delivery_repair_gate(tmp_path: Path):
    _write_builder_ready_closeout(tmp_path)
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent_with_calls(tmp_path, [{"tool": "list_files", "path": "outputs/report"}])

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_LIST", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "list_files", "path": "outputs/report"}]
    assert not any("delivery-required-repair" in item for item in params.tool_context)


# LLM: valid artifacts still require explicit submit_for_acceptance before machine closeout.
# 函数用途: 验证即使产物合格，普通最终回复也不会被系统替换成验收成功回复。
def test_no_tool_final_answer_does_not_close_when_valid_without_submit(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件在 out.txt。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件在 out.txt。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


# LLM: Some legitimate tasks finish in the answer/channel rather than a file.
# 函数用途: 验证显式 message delivery 不会被产物 ref 门误判成缺文件。
def test_message_delivery_contract_closes_without_artifact_ref_failure(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="已经检查完，结论直接回复给你。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["delivery_mode"] == "message"
    assert report["artifacts"] == []
    assert report["runtime_gate"]["allowed"] is True


# LLM: Boolean no-artifact declarations are the preferred open-ended shape.
# 函数用途: 验证无需落盘任务即使没有 artifacts 字段，也不会被合同 doctor 误杀。
def test_requires_artifact_false_contract_closes_without_artifacts_field(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[], contract={"case_id": "answer-only", "requires_artifact": False})
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="检查完成，不需要生成文件。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text


def _agent(root: Path):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: []),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _agent_with_calls(root: Path, calls: list[dict[str, object]]):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: list(calls)),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _write_valid_artifact(root: Path) -> None:
    (root / "out.txt").write_text("finished artifact", encoding="utf-8")


def _write_builder_ready_closeout(root: Path) -> None:
    delivery_dir = root / ".agent_delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    (delivery_dir / "closeout.json").write_text(
        json.dumps(
            {
                "ok": False,
                "delivery_progress": {
                    "recovery_actions": [
                        {
                            "code": "STAGING_BUILDER_READY",
                            "recommended_action": "invoke_builder_tool",
                            "builder_tool": "write_file",
                            "source_ref": "outputs/report/source.json",
                            "output_ref": "outputs/report/report.xlsx",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _last_tool_context_payload(params: ToolLoopExecuteParams) -> dict[str, object]:
    return json.loads(params.tool_context[-1].split("\n", 1)[1])


def _delivery_params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="make artifact",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=archive_tool_calls,
        delivery_contract={
            "case_id": "generic-artifact",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
        },
    )


def _message_delivery_params(
    *,
    archive_tool_calls: list[dict[str, object]],
    contract: dict[str, object] | None = None,
) -> ToolLoopExecuteParams:
    params = _delivery_params(archive_tool_calls=archive_tool_calls)
    return replace(
        params,
        delivery_contract=dict(
            contract
            or {
                "case_id": "message-only",
                "delivery_mode": "message",
                "artifacts": [],
            }
        ),
    )


def _write_file_archive_record() -> dict[str, object]:
    return {
        "tool": "write_file",
        "run_id": "run-1",
        "task_id": "task-1",
        "ok": True,
        "parameters": {"tool": "write_file", "path": "out.txt"},
        "runtime_gate": {
            "allowed": True,
            "status": "ALLOW",
            "evidence": {
                "tool_name": "write_file",
                "operation_id": "op-write-1",
                "idempotency_key": "idem-write-1",
            },
        },
    }
