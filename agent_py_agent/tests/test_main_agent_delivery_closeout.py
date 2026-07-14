"""LLM: tests for main-agent machine delivery closeout after tool execution.

给人看的解释：
这个文件验证主代理真实任务的"产物已合格就自动停机"能力，避免产物已经写好还继续跑到超时。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.agent_core.delivery_closeout.models import DeliveryCloseoutConfig
from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.task_progress import write_task_progress
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    ArtifactFindingRepairBackend,
    CloseoutReworkBackend,
    DeliveryContractBackend,
    FailedDeliveryContractBackend,
    IncompleteDeliveryContractBackend,
    LocalProgressRedirectBackend,
    MissingArtifactRepairBackend,
    NoProgressDeliveryBackend,
    OpenWriteSessionDeliveryBackend,
    PendingTargetsDeliveryBackend,
    RecoveryAttemptRepairBackend,
    WrongToolDuringOpenSessionBackend,
    delivery_contract,
    delivery_contract_prompt,
    web_project_delivery_contract,
    xlsx_delivery_contract,
)


def _agent(
    workspace: Path,
    backend,
    *,
    max_tool_rounds: int = 5,
    delivery_closeout_config: DeliveryCloseoutConfig | None = None,
) -> SimpleAgent:
    cfg = AgentConfig(
        enable_tools=True,
        memory_path="memory.jsonl",
        max_tool_rounds=max_tool_rounds,
        my_agent_home=str(workspace / ".my-agent"),
        run_task_workspace_enabled=False,
    )
    agent = SimpleAgent(cfg, workspace)
    agent.backend = backend
    if delivery_closeout_config is not None:
        agent._delivery_closeout_config = delivery_closeout_config
    return agent


def test_tool_loop_closes_out_after_delivery_contract_passes():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = DeliveryContractBackend()
        result = _agent(
            workspace,
            backend,
            delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=2),
        ).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "output/html_report/index.html").exists()
        assert (workspace / ".agent_delivery/closeout.json").exists()


def test_tool_loop_closes_out_from_structured_run_params_delivery_contract():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = DeliveryContractBackend()
        result = _agent(
            workspace,
            backend,
            delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=2),
        ).run(
            "用单文件 html 做一个高端家具品牌首页。",
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert "output/html_report/index.html" in backend.prompts[0]
        assert "[tool-system delivery-contract]" in backend.prompts[0]
        assert "不得引用 http/https 外部" in backend.prompts[0]
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / ".agent_delivery/closeout.json").exists()


def test_tool_loop_reports_malformed_delivery_contract_without_blocking():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = MalformedDeliveryContractBackend()
        result = _agent(workspace, backend, max_tool_rounds=2).run(
            "生成一个交付文件。",
            params=RunParams(delivery_contract={"schema_version": "delivery_contract.v1", "artifacts": "output.md"}, save=False),
        )

        doctor_report = json.loads((workspace / ".agent_delivery/contract_doctor.json").read_text(encoding="utf-8"))
        # run 出口合同(P1-1)生效后,最终回复会被出口检查多打回一轮续航,
        # 故 backend 比旧行为多调用一次(2 轮工具上限 + doctor 修复轮 + 出口续航轮)。
        assert backend.calls == 4
        assert backend.saw_contract_doctor is True
        assert "[DELIVERY_CONTRACT_DOCTOR_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert doctor_report["ok"] is False
        assert doctor_report["repair_actions"][0]["recommended_action"] == "repair_effective_contract"
        assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in {
            finding["code"] for finding in doctor_report["findings"]
        }


def test_submit_for_acceptance_without_contract_persists_non_terminal_closeout_report():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = NoContractAcceptanceBackend()
        result = _agent(workspace, backend, max_tool_rounds=2).run(
            "我已经完成了，请记录验收尝试。",
            params=RunParams(save=False),
        )
        report = _closeout_report(workspace)

        assert backend.calls == 2
        assert result.tool_rounds == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["non_terminal"] is True
        assert report["reason"] == "delivery_contract_missing"
        assert report["artifacts"] == []


def test_completion_uses_acceptance_summary_from_current_tool_round_only() -> None:
    previous = {
        "tool": "submit_for_acceptance",
        "ok": True,
        "parameters": {"summary": "旧摘要，不应复用。"},
    }
    current = {
        "tool": "submit_for_acceptance",
        "ok": True,
        "parameters": {"summary": "当前摘要，测试 25 项通过。"},
    }
    params = SimpleNamespace(
        executed_tools=["write_file", "submit_for_acceptance"],
        archive_tool_calls=[previous, current],
    )
    completed = ModelResponse(text="closeout", backend="test")

    with patch(
        "agent_py_agent.agent.agent_core.tool_loop.completion.main_agent_delivery_closeout_response",
        return_value=completed,
    ) as closeout:
        result = completion_response_after_tool_round(
            ToolRoundCompletionRequest(
                agent=object(),
                params=params,
                response=ModelResponse(text="tool call", backend="test"),
                before_executed_count=1,
                subagent_output_written=False,
                before_archive_count=1,
            )
        )

    assert result is completed
    assert closeout.call_args.args[0].user_summary == "当前摘要，测试 25 项通过。"


def test_submit_for_acceptance_without_contract_closes_after_current_task_output_report():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "all-agent-架构分析"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        backend = NoContractTaskOutputReportBackend(output_dir / "final_analysis_report.md")

        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "写一份最终分析报告。",
            params=RunParams(
                save=False,
                task_attributes={
                    "run_workspace": {
                        "task_root": str(task_root),
                        "output_dir": str(output_dir),
                        "work_dir": str(work_dir),
                    }
                },
            ),
        )
        report = _closeout_report(task_root)

        # 空转轮宽限(终端应用 对齐,2026-07-02):无合同 run 写完文件的【当轮】不再抢收口
        # (防"写完 SPEC.md 即完成"切断建造),模型下一轮自己 submit_for_acceptance 提交收口
        # ——多一次模型调用,换"不打断建造";交付结果与报告完全一致。
        assert backend.calls == 2
        assert result.tool_rounds == 2
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert not (workspace / ".agent_delivery" / "closeout.json").exists()
        assert report["ok"] is True
        assert report["delivery_mode"] == "uncontracted_task_output"
        assert report["user_summary"] == "最终报告已写入 task output，提交验收。"
        assert "最终报告已写入 task output" in project_user_reply(result.response).content
        assert report["artifacts"][0]["path"] == str((output_dir / "final_analysis_report.md").resolve(strict=False))
        assert report["artifacts"][0]["registry_ref"]["status"] == "ready"
        assert (task_root / "data" / "artifacts" / "registry.jsonl").exists()


def test_uncontracted_closeout_records_open_progress_without_blocking():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "all-agent-架构分析"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        run_id = "run-open-progress"
        backend = NoContractTaskOutputReportBackend(output_dir / "final_analysis_report.md")
        agent = _agent(workspace, backend, max_tool_rounds=3)
        write_task_progress(
            runtime_owner_root(agent),
            run_id,
            {
                "summary": "源码已读，最终报告仍在写",
                "next_action": "继续生成最终报告",
                "items": [
                    {"id": "read-sources", "status": "done", "evidence": ["README.md"]},
                    {"id": "write-final-report", "status": "in_progress"},
                ],
            },
        )

        result = agent.run(
            "写一份最终分析报告。",
            params=RunParams(
                save=False,
                run_id=run_id,
                task_attributes={
                    "run_workspace": {
                        "task_root": str(task_root),
                        "output_dir": str(output_dir),
                        "work_dir": str(work_dir),
                    }
                },
            ),
        )
        report = _closeout_report(task_root)

        # 稳而不管语义裁决(2026-06-12,PLAN-stability-not-control):progress open
        # 是模型自己账本的诚实信号,记录进报告供把关,但不再阻断退出——R9 取证
        # 实锤"打回驱动凑数过门";可恢复性由出口合同 resume 块保证。
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert report["ok"] is True
        assert report["task_progress_closeout_gate"]["allowed"] is False
        assert "TASK_PROGRESS_OPEN_ITEMS" in {
            finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
        }
        assert any(
            item.get("gate") == "task_progress_closeout"
            for item in report.get("quality_advisories", [])
        ), "open 事实进 advisory 投影供把关"


def test_coverage_only_contract_blocks_task_output_closeout_until_sources_are_read():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        source_root = workspace / "sources"
        source_a = source_root / "A-main"
        source_b = source_root / "B-main"
        source_a.mkdir(parents=True)
        source_b.mkdir()
        readme_a = source_a / "README.md"
        readme_b = source_b / "README.md"
        readme_a.write_text("A architecture", encoding="utf-8")
        readme_b.write_text("B architecture", encoding="utf-8")
        task_root = workspace / "tasks" / "2026-06-08" / "source-analysis"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        report_path = output_dir / "final_analysis_report.md"
        backend = CoverageOnlyTaskOutputReportBackend(readme_a, readme_b, report_path)
        contract = {
            "schema_version": "delivery_contract.v1",
            "artifacts": [],
            "target_coverage_contract": {
                "scope_label": "source projects",
                "enforcement": "required",
                "target_items": [
                    {
                        "target_id": "A-main",
                        "source_ref": str(source_a),
                        "coverage_kind": "source_file_under_dir",
                        "min_read_count": 1,
                    },
                    {
                        "target_id": "B-main",
                        "source_ref": str(source_b),
                        "coverage_kind": "source_file_under_dir",
                        "min_read_count": 1,
                    },
                ],
            },
        }

        result = _agent(workspace, backend, max_tool_rounds=6).run(
            "读两个源码目录并写最终分析报告。",
            params=RunParams(
                save=False,
                delivery_contract=contract,
                task_attributes={
                    "run_workspace": {
                        "task_root": str(task_root),
                        "output_dir": str(output_dir),
                        "work_dir": str(work_dir),
                    }
                },
            ),
        )
        report = _closeout_report(task_root)

        assert backend.calls == 4
        assert backend.saw_coverage_repair is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert report["ok"] is True
        assert report["target_coverage_status"]["missing_count"] == 0
        assert "B architecture" in report_path.read_text(encoding="utf-8")


def test_tool_loop_does_not_block_immediately_on_malformed_validation_contract_with_artifact_target():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = ValidationContractStringListBackend()
        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "生成一个表格。",
            params=RunParams(
                delivery_contract={
                    "schema_version": "delivery_contract.v1",
                    "artifacts": [
                        {
                            "artifact_id": "report",
                            "kind": "xlsx",
                            "preferred_path": "output/report.xlsx",
                            "validation_contract": {"required_columns": ""},
                        }
                    ],
                },
                save=False,
            ),
        )

        assert backend.calls == 3
        assert backend.saw_contract_doctor is True
        assert "[DELIVERY_CONTRACT_DOCTOR_BLOCKED]" not in result.response
        doctor_report = json.loads((workspace / ".agent_delivery/contract_doctor.json").read_text(encoding="utf-8"))
        assert "VALIDATION_CONTRACT_STRING_LIST_INVALID" in {
            finding["code"] for finding in doctor_report["findings"]
        }


def test_tool_loop_does_not_close_out_when_delivery_contract_fails():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = FailedDeliveryContractBackend()
        result = _agent(
            workspace,
            backend,
            delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=3),
        ).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        codes = _closeout_finding_codes(workspace)

        assert backend.calls >= 2
        assert any("HTML_INCOMPLETE_DOCUMENT" in prompt for prompt in backend.prompts[1:])
        assert any("HTML_EXTERNAL_RESOURCE_REF" in prompt for prompt in backend.prompts[1:])
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)
        actions = _closeout_report(workspace)["delivery_progress"]["recovery_actions"]
        repair = next(item for item in actions if item["code"] == "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED")
        assert repair["recommended_action"] == "repair_artifact_against_findings"
        assert "write_file" in repair["write_tools"]
        assert "HTML_INCOMPLETE_DOCUMENT" in repair["finding_codes"]
        assert any(str(path).endswith("output/html_report/index.html") for path in repair["repair_targets"])


def test_tool_loop_repairs_failed_artifact_findings_before_closeout():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = ArtifactFindingRepairBackend()
        result = _agent(workspace, backend, max_tool_rounds=4).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 3
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert _closeout_report(workspace)["ok"] is True


def test_tool_loop_repairs_missing_artifact_to_contract_path_before_closeout():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = MissingArtifactRepairBackend()
        result = _agent(workspace, backend, max_tool_rounds=4).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 3
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "output/html_report/index.html").exists()
        assert _closeout_report(workspace)["ok"] is True


def test_tool_loop_rejects_incomplete_delivery_contract_artifact():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = IncompleteDeliveryContractBackend()
        result = _agent(
            workspace,
            backend,
            delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=3),
        ).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        codes = _closeout_finding_codes(workspace)

        assert backend.calls >= 2
        assert any("HTML_INCOMPLETE_DOCUMENT" in prompt for prompt in backend.prompts[1:])
        assert any("HTML_EXTERNAL_RESOURCE_REF" in prompt for prompt in backend.prompts[1:])
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)


def test_tool_loop_repeated_failed_submissions_do_not_emit_rework_marker():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = NoProgressDeliveryBackend()
        result = _agent(
            workspace,
            backend,
            delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=3),
        ).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        report = _closeout_report(workspace)

        assert backend.calls == 7
        assert "已达到最大工具轮数限制" in result.response
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        actions = report["delivery_progress"]["recovery_actions"]
        assert actions[0]["code"] == "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"
        assert "ACCEPTANCE_FAILED" in {item["code"] for item in actions}


def test_delivery_repair_context_does_not_leak_into_uncontracted_later_run():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_stale_delivery_closeout(workspace)
        backend = UncontractedFollowupBackend()

        result = _agent(workspace, backend).run(
            "把当前任务的说明写到 lab_outputs/tool-recovery/report.md。",
            params=RunParams(save=False),
        )

        assert backend.calls == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" not in result.response
        assert (workspace / "lab_outputs/tool-recovery/report.md").exists()


def test_tool_loop_keeps_running_while_bootstrap_targets_are_still_missing():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = PendingTargetsDeliveryBackend()
        result = _agent(workspace, backend, max_tool_rounds=6).run(
            "做一个示例网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
        )
        report = _closeout_report(workspace)

        assert backend.calls == 5
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" not in result.response
        assert report["ok"] is True
        assert (workspace / "output/static_site/index.html").exists()
        assert (workspace / "output/static_site/app.js").read_text(encoding="utf-8") == 'console.log("shop ready");'


def _write_site_index(workspace: Path) -> None:
    (workspace / "output/static_site").mkdir(parents=True)
    (workspace / "output/static_site/index.html").write_text(
        "<!doctype html><html><body><main>Sample</main></body></html>",
        encoding="utf-8",
    )


def _closeout_report(workspace: Path) -> dict[str, object]:
    return json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))


def _write_stale_delivery_closeout(workspace: Path) -> None:
    path = workspace / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "case_id": "sample_stale_delivery_case",
                "ok": False,
                "delivery_progress": {
                    "recovery_actions": [
                        {
                            "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                            "recommended_action": "repair_artifact_against_findings",
                            "artifact_path": "lab_outputs/sample-artifact",
                            "finding_values": ["getElementById:email"],
                            "write_tools": ["write_file", "replace_in_file"],
                        }
                    ],
                    "unchanged_failure_count": 4,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class UncontractedFollowupBackend:
    name = "fake_uncontracted_followup_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        assert "delivery-required-repair" not in prompt
        assert "getElementById:email" not in prompt
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"lab_outputs/tool-recovery/report.md",'
                    '"content":"# 当前任务说明\\n\\n这是后续普通任务的报告，不应继承旧网页修复合同。"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="当前任务已完成。", backend=self.name)


class NoContractAcceptanceBackend:
    name = "fake_no_contract_acceptance_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"提交验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="我会继续按用户目标处理。", backend=self.name)


class NoContractTaskOutputReportBackend:
    name = "fake_no_contract_task_output_report_backend"

    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps(
                        {
                            "tool": "write_file",
                            "path": str(self.report_path),
                            "content": "# 最终分析报告\n\n已完成。",
                        },
                        ensure_ascii=False,
                    )
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"最终报告已写入 task output，提交验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="不应该继续运行。", backend=self.name)


class CoverageOnlyTaskOutputReportBackend:
    name = "fake_coverage_only_task_output_report_backend"

    def __init__(self, readme_a: Path, readme_b: Path, report_path: Path):
        self.readme_a = readme_a
        self.readme_b = readme_b
        self.report_path = report_path
        self.calls = 0
        self.saw_coverage_repair = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps({"tool": "read_file", "path": str(self.readme_a)}, ensure_ascii=False)
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps(
                        {
                            "tool": "write_file",
                            "path": str(self.report_path),
                            "content": "# 最终分析报告\n\nA architecture",
                        },
                        ensure_ascii=False,
                    )
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            self.saw_coverage_repair = "target_coverage_status" in prompt and "B-main" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps({"tool": "read_file", "path": str(self.readme_b)}, ensure_ascii=False)
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 4:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps(
                        {
                            "tool": "write_file",
                            "path": str(self.report_path),
                            "content": "# 最终分析报告\n\nA architecture\n\nB architecture",
                        },
                        ensure_ascii=False,
                    )
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="不应该继续运行。", backend=self.name)


class MalformedDeliveryContractBackend:
    name = "fake_malformed_delivery_contract_backend"

    def __init__(self):
        self.calls = 0
        self.saw_contract_doctor = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","path":"output/draft.md","content":"draft"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"草稿已写入，请系统验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "delivery-contract-doctor" in prompt
        assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in prompt
        assert "repair_effective_contract" in prompt
        self.saw_contract_doctor = True
        return ModelResponse(text="已收到合同结构返工要求。", backend=self.name)


class ValidationContractStringListBackend:
    name = "fake_validation_contract_string_list_backend"

    def __init__(self):
        self.calls = 0
        self.saw_contract_doctor = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","path":"output/report.xlsx","content":"placeholder"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"表格已生成，请系统验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "delivery-contract-doctor" in prompt
        assert "VALIDATION_CONTRACT_STRING_LIST_INVALID" in prompt
        self.saw_contract_doctor = True
        return ModelResponse(text="收到内部合同字段问题，我会继续按用户目标修正。", backend=self.name)


def _closeout_finding_codes(workspace: Path) -> list[str]:
    report = _closeout_report(workspace)
    return [item["code"] for item in report["artifacts"][0]["acceptance_report"]["findings"]]


def _write_stale_xlsx_closeout(workspace: Path) -> None:
    report = {
        "ok": False,
        "delivery_progress": {
            "failure_fingerprint": "failed-xlsx",
            "work_progress_fingerprint": "empty-source",
            "pending_materialization_targets": [
                {"workspace_relative_path": "output/table_report/table_report.xlsx", "exists": False}
            ],
            "recovery_actions": [
                {
                    "category": "artifact",
                    "checkpoint_ref": "output/table_report/source_data.json",
                    "code": "STAGED_JSON_NO_ROWS",
                    "recommended_action": "write_non_empty_structured_rows",
                    "required_columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
                    "retryable": True,
                }
            ],
            "unchanged_failure_count": 1,
            "no_progress_block_threshold": 5,
        },
    }
    path = workspace / ".agent_delivery/closeout.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def _write_stale_local_progress_state(workspace: Path) -> None:
    (workspace / ".agent_delivery/local_progress_guard.json").write_text(
        json.dumps(
            {
                "failure_fingerprint": "failed-xlsx",
                "work_progress_fingerprint": "empty-source",
                "exploration_rounds_without_local_progress": 7,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
