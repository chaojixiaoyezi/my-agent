"""run 出口合同钉子(任务完成力底座 P2-1/P1-1/P1-2/P5-1,实锤来源 R5 audit)。

钉死四层契约:
1. 纯问答零影响:无任务态的 run 不触发出口检查(不写 closeout、不打回)。
2. 口头放弃被抓(R5b/R5c 形态):存在未收口子代理/open capreq 时,模型想直接收尾
   会先走 closeout → SUBAGENTS gate 阻断 → rework 注入 → 续航打回。
3. 续航双闸:进展签名不变第二次即放行;预算 0 关闭续航。
4. 交付模式分流:派过子代理不等于必须生成文件；无 artifact contract 时可用
   message 收口，显式 artifact contract 仍是硬门。
5. 余留合同(P1-2):REWORK 最终回复带结构化 resume 块。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import (
    _failed_final_closeout_response,
)
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
    unfinished_exit_passthrough,
)
from agent_py_agent.agent.settings.config import AgentConfig

pytestmark = pytest.mark.integration


def _agent(tmp_path: Path, *, continuations: int = 3) -> SimpleNamespace:
    """最小真实 config 的 agent 替身(出口合同只读 config/tools.workspace_root)。"""
    return SimpleNamespace(
        config=AgentConfig(run_repair_max_continuations=continuations),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
    )


def _params(task_root: Path | None, *, archive_tool_calls: list | None = None) -> ToolLoopExecuteParams:
    attrs = {}
    if task_root is not None:
        attrs = {
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        }
    return ToolLoopExecuteParams(
        user_prompt="测试任务",
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
        task_attributes=attrs,
        request_id="req-final-exit",
        run_id="run-final-exit",
        task_id="run-final-exit",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=list(archive_tool_calls or []),
    )


def _final(text: str = "我做完了,这是结论。") -> SimpleNamespace:
    return SimpleNamespace(text=text, backend="echo")


def _write_child_state(task_root: Path, run_id: str, *, status: str, capreqs: list | None = None) -> None:
    agent_dir = task_root / "work" / "agents" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": run_id,
        "status": status,
        "latest_summary": "测试子代理",
        "capability_requests": capreqs or [],
        "attributes": {},
    }
    (agent_dir / "canonical_state.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. 纯问答零影响
# ---------------------------------------------------------------------------


def test_plain_qa_run_exits_untouched(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    decision = final_exit_closeout_decision(FinalExitRequest(agent, _params(None), _final(), FinalExitState()))
    assert decision.should_continue is False
    assert decision.response is None
    assert not (tmp_path / ".agent_delivery").exists()


def _failed_tool_record(code: str, *, request_id: str = "req-final-exit") -> dict[str, object]:
    return {
        "tool": "terminal_session",
        "ok": False,
        "error_code": code,
        "request_id": request_id,
    }


def test_terminal_blocker_with_zero_success_replaces_unverifiable_final_claim(tmp_path: Path) -> None:
    params = _params(None, archive_tool_calls=[_failed_tool_record("SANDBOX_UNAVAILABLE")])
    decision = final_exit_closeout_decision(
        FinalExitRequest(_agent(tmp_path), params, _final("我实际运行得到 42。"), FinalExitState())
    )

    assert decision.should_continue is False
    assert decision.response is not None
    assert "[RUN_TOOL_EVIDENCE_BLOCKED]" in decision.response.text
    assert "42" not in decision.response.text
    assert decision.response.runtime_status == "unfinished"
    assert decision.response.runtime_reason == "ALL_TOOL_ATTEMPTS_BLOCKED"


def test_retryable_failure_only_does_not_replace_honest_final_report(tmp_path: Path) -> None:
    params = _params(None, archive_tool_calls=[_failed_tool_record("PATH_NOT_FOUND")])
    decision = final_exit_closeout_decision(
        FinalExitRequest(_agent(tmp_path), params, _final("文件不存在。"), FinalExitState())
    )

    assert decision.response is None


def test_prior_request_blocker_does_not_contaminate_current_run(tmp_path: Path) -> None:
    params = _params(None, archive_tool_calls=[_failed_tool_record("SANDBOX_UNAVAILABLE", request_id="old")])
    decision = final_exit_closeout_decision(FinalExitRequest(_agent(tmp_path), params, _final(), FinalExitState()))
    assert decision.response is None


def test_tool_limit_exit_uses_same_terminal_blocker_guard(tmp_path: Path) -> None:
    params = _params(None, archive_tool_calls=[_failed_tool_record("SANDBOX_UNAVAILABLE")])
    response = unfinished_exit_passthrough(_agent(tmp_path), params, _final("推测执行成功。"))
    assert response.runtime_status == "unfinished"
    assert "[RUN_TOOL_EVIDENCE_BLOCKED]" in response.text


def test_already_closed_response_passes_through(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t1"
    _write_child_state(task_root, "sub-1", status="RUNNING")
    final = _final("[MAIN_AGENT_DELIVERY_COMPLETE]\n{...}")
    decision = final_exit_closeout_decision(FinalExitRequest(agent, _params(task_root), final, FinalExitState()))
    assert decision.should_continue is False and decision.response is None


# ---------------------------------------------------------------------------
# 2. 口头放弃被抓(R5b/R5c 形态)
# ---------------------------------------------------------------------------


def test_verbal_abandon_with_open_children_is_pulled_back(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-abandon"
    _write_child_state(
        task_root,
        "sub-running",
        status="RUNNING",
        capreqs=[{"id": "capreq-1", "status": "OPEN", "needed_capability": "x"}],
    )
    params = _params(task_root)
    state = FinalExitState()

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("数据不存在,无法完成。"), state))

    assert decision.should_continue is True, "存在未收口子代理时口头收尾必须被打回"
    assert state.continuations == 1
    # closeout 真实落盘且 subagent gate 阻断
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["subagent_aggregation_gate"]["allowed"] is False
    # rework 指令已注入 tool_context(模型下一轮能看到怎么修)
    assert any("SUBAGENTS" in str(item) or "rework" in str(item).lower() or "处理" in str(item) for item in params.tool_context)


# ---------------------------------------------------------------------------
# 3. 续航双闸
# ---------------------------------------------------------------------------


def test_stalled_signature_stops_continuation(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-stall"
    _write_child_state(task_root, "sub-1", status="BLOCKED")
    params = _params(task_root)
    state = FinalExitState()

    first = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), state))
    assert first.should_continue is True
    # 局面零变化的第二次出口 → 签名相同 → 放行退出(防死循环)
    second = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), state))
    assert second.should_continue is False


def test_budget_zero_disables_continuation(tmp_path: Path) -> None:
    agent = _agent(tmp_path, continuations=0)
    task_root = tmp_path / "tasks" / "t-budget"
    _write_child_state(task_root, "sub-1", status="RUNNING")
    decision = final_exit_closeout_decision(FinalExitRequest(agent, _params(task_root), _final(), FinalExitState()))
    assert decision.should_continue is False, "预算 0 = 关闭续航,保持旧行为"
    # 但 closeout 仍然跑了(留档不受预算影响)
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


# ---------------------------------------------------------------------------
# 4. 交付模式分流：消息结果与显式文件要求
# ---------------------------------------------------------------------------


def test_analysis_with_terminal_children_closes_as_message_without_result_file(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-empty"
    _write_child_state(task_root, "sub-done", status="DONE")
    params = _params(task_root)
    state = FinalExitState()

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("分析完成，结论如下。"), state))

    assert decision.should_continue is False
    assert decision.response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in str(decision.response.text)
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["delivery_mode"] == "message"
    assert report["artifacts"] == []
    assert "empty_delivery_gate" not in report


def test_explicit_artifact_contract_still_requires_the_file(tmp_path: Path) -> None:
    import dataclasses

    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-required-file"
    _write_child_state(task_root, "sub-done", status="DONE")
    params = dataclasses.replace(
        _params(task_root),
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "report",
                    "kind": "markdown",
                    "preferred_path": "output/report.md",
                }
            ],
        },
    )

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("文件已完成。"), FinalExitState())
    )

    assert decision.should_continue is True
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert any(item.get("ok") is not True for item in report["artifacts"])


def test_result_file_after_rework_passes_closeout(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-report"
    _write_child_state(task_root, "sub-done", status="CANCELLED")
    output_dir = task_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "不可行报告.md"
    report_file.write_text("# 不可行报告\n\ntried_channels: ...\n", encoding="utf-8")
    archive = [
        {
            "tool": "write_file",
            "call_id": "9-1",
            "ok": True,
            "parameters": {"path": str(report_file)},
            "output": "written",
        }
    ]
    params = _params(task_root, archive_tool_calls=archive)

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("已写不可行报告。"), FinalExitState())
    )

    # 有真实结果文件 → closeout 正常走验收(成功则以 closeout 回复收口,
    # 或因其他 gate 返工;但绝不是"空交付"形态)
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert "empty_delivery_gate" not in report
    if decision.response is not None:
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in str(decision.response.text)


# ---------------------------------------------------------------------------
# 5. 余留合同(P1-2):REWORK 回复带结构化 resume 块
# ---------------------------------------------------------------------------


def test_rework_final_response_carries_resume_block(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks" / "t-resume"
    delivery = task_root / ".agent_delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    (delivery / "closeout.json").write_text(
        json.dumps(
            {
                "ok": False,
                "report_ref": ".agent_delivery/closeout.json",
                "task_progress_closeout_gate": {
                    "allowed": False,
                    "evidence": {"progress_ref": str(task_root / "progress.json"), "open_count": 5},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    agent = _agent(tmp_path)
    params = _params(task_root)

    response = _failed_final_closeout_response(agent, params, backend="echo")

    assert response is not None
    payload = json.loads(
        response.text.split("[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n")[1].split("\n[/MAIN_AGENT_DELIVERY_REWORK_REQUIRED]")[0]
    )
    resume = payload["resume"]
    assert resume["task_root"].endswith("t-resume")
    assert resume["open_count"] == 5
    assert resume["progress_ref"].endswith("progress.json")
    assert any("subagents-dispatch" in item for item in resume["how_to_continue"])


def test_pullback_always_carries_exit_hint_even_without_closeout_injection(tmp_path: Path) -> None:
    """R6a 实锤钉子:contract 路径 closeout 失败不注入指令——出口合同必须兜底注入,
    被打回的模型下一轮必能看到"为什么+该做什么"(幂等不堆叠)。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-hint"
    _write_child_state(task_root, "sub-1", status="RUNNING")
    params = _params(task_root)
    state = FinalExitState()

    first = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), state))
    assert first.should_continue is True
    hints = [item for item in params.tool_context if "[final-exit-contract]" in str(item)]
    assert len(hints) == 1, "打回必有出口指令"
    assert "required_actions" in hints[0] and "open_children" in hints[0]
    # 幂等:不论 closeout 链是否又注入,出口指令不重复堆叠
    final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), state))
    hints_again = [item for item in params.tool_context if "[final-exit-contract]" in str(item)]
    assert len(hints_again) == 1


def test_stalled_release_appends_structured_unfinished_exit(tmp_path: Path) -> None:
    """R6a 实锤钉子:闸断放行且仍有未收口子代理时,最终回复必须带
    [RUN_UNFINISHED_EXIT] 结构化未完成声明+resume(不依赖 finalize REWORK 路径)。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-unfin"
    _write_child_state(task_root, "sub-1", status="RUNNING")
    params = _params(task_root)
    state = FinalExitState()

    assert final_exit_closeout_decision(FinalExitRequest(agent, params, _final("等子代理跑完。"), state)).should_continue is True
    second = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("我会持续监控,请稍候。"), state))

    assert second.should_continue is False
    assert second.response is not None
    text = str(second.response.text)
    assert "[RUN_UNFINISHED_EXIT]" in text
    payload = json.loads(text.split("[RUN_UNFINISHED_EXIT]\n")[1].split("\n[/RUN_UNFINISHED_EXIT]")[0])
    assert payload["open_children"] == 1
    assert payload["task_root"].endswith("t-unfin")
    assert any("subagents-dispatch" in item for item in payload["how_to_continue"])
    assert "我会持续监控" in text, "模型原文必须保留"


# ---------------------------------------------------------------------------
# 问句型零交付出口守卫(R11b 实锤:单次 run 反过来问用户=白跑)
# ---------------------------------------------------------------------------


def _question_params(task_root: Path, *, source: str = "cli_run") -> ToolLoopExecuteParams:
    import dataclasses

    params = dataclasses.replace(_params(task_root), source=source)
    params.executed_tools.extend(["web_search", "web_fetch"])
    return params


def test_question_exit_in_single_shot_run_is_pulled_back(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _question_params(tmp_path / "task")
    state = FinalExitState()

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("有两个方案,请您选择 A 还是 B?"), state)
    )

    assert decision.should_continue is True, "单次 run 干了活却以问句收尾必须打回"
    assert state.question_guard_fired is True
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "[exit-question-guard]" in joined
    assert "自主决策" in joined and "不要再以提问" in joined


def test_question_exit_guard_fires_only_once(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _question_params(tmp_path / "task")
    state = FinalExitState()

    first = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("请确认是否继续？"), state))
    second = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("我还在等待您的指示。"), state))

    assert first.should_continue is True
    assert second.should_continue is False, "打回一次后仍请示则诚实放行,防无人应答死循环"


def test_plain_qa_question_without_tools_passes(tmp_path: Path) -> None:
    import dataclasses

    agent = _agent(tmp_path)
    params = dataclasses.replace(_params(tmp_path / "task"), source="cli_run")

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("你想了解哪方面?"), FinalExitState())
    )

    assert decision.should_continue is False, "纯问答(零工具)不守卫"


def test_statement_exit_passes_untouched(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _question_params(tmp_path / "task")

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("调研完成,结论如下:数据完整可用。"), FinalExitState())
    )

    assert decision.should_continue is False, "陈述收尾照常放行"


def test_question_exit_in_gateway_scope_passes(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _question_params(tmp_path / "task", source="gateway")

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("请您选择方案 A 还是 B?"), FinalExitState())
    )

    assert decision.should_continue is False, "gateway 多轮有人应答,不守卫"


# ---------------------------------------------------------------------------
# 6. 账本会触发收口核对，但不自动创造文件要求。
# ---------------------------------------------------------------------------


def _write_progress_ledger(root: Path, run_id: str, *, status: str = "done") -> None:
    ledger_dir = root / "memory_archive" / "task_progress" / run_id
    ledger_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "task_progress.v1",
        "run_id": run_id,
        "items": [
            {"id": "build", "title": "建系统", "status": status, "evidence": ["library_system/README.md"]},
        ],
    }
    (ledger_dir / "progress.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_solo_ledger_with_zero_artifacts_closes_as_message(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-solo-ledger"
    task_root.mkdir(parents=True, exist_ok=True)
    _write_progress_ledger(tmp_path, "run-final-exit")
    params = _params(task_root)
    state = FinalExitState()

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("分析做完了，结论如下。"), state))

    assert decision.should_continue is False
    assert decision.response is not None
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["delivery_mode"] == "message"


def test_plain_qa_without_ledger_still_exits_untouched(tmp_path: Path) -> None:
    # 护既有语义:没立账、没子代理、没产物的普通任务态回复照旧不触发 closeout(聊天不受扰)。
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-plain"
    task_root.mkdir(parents=True, exist_ok=True)
    decision = final_exit_closeout_decision(FinalExitRequest(agent, _params(task_root), _final("好的,已说明。"), FinalExitState()))
    assert decision.should_continue is False
    assert not (task_root / ".agent_delivery").exists()
