"""LLM: tests for main-agent machine delivery closeout after tool execution.

给人看的解释：
这个文件验证主代理真实任务的"产物已合格就自动停机"能力，避免产物已经写好还继续跑到超时。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_closeout_config import DeliveryCloseoutConfig
from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
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


# LLM: _agent builds a SimpleAgent test harness with one fake backend.
# 函数用途: 统一创建临时工作区、工具开启配置和测试后端，减少每个测试的样板代码。
def _agent(
    workspace: Path,
    backend,
    *,
    max_tool_rounds: int = 5,
    delivery_closeout_config: DeliveryCloseoutConfig | None = None,
) -> SimpleAgent:
    cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=max_tool_rounds)
    agent = SimpleAgent(cfg, workspace)
    agent.backend = backend
    if delivery_closeout_config is not None:
        agent._delivery_closeout_config = delivery_closeout_config
    return agent


# LLM: Real-task delivery contracts should stop successful runs before extra model turns.
# 函数用途: 验证产物按机器合同验收通过后，主代理工具循环直接收口，不继续读写直到超时。
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

        assert backend.calls == 2
        assert result.tool_rounds == 2
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/html_report/index.html").exists()
        assert (workspace / ".agent_delivery/closeout.json").exists()


# LLM: Delivery closeout must use RunParams contracts without requiring prompt markers.
# 函数用途: 验证系统交付合同可以通过结构化运行参数传入，不依赖 user_prompt 中的机器 JSON 标记。
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

        assert backend.calls == 2
        assert "outputs/html_report/index.html" in backend.prompts[0]
        assert "[tool-system delivery-contract]" in backend.prompts[0]
        assert "不得引用 http/https 外部" in backend.prompts[0]
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / ".agent_delivery/closeout.json").exists()


# LLM: Malformed delivery contracts should become model-visible hints, not terminal blocks.
# 函数用途: 验证合同 Doctor 只提示坏合同字段，不再用 BLOCKED 终止普通任务。
def test_tool_loop_reports_malformed_delivery_contract_without_blocking():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = MalformedDeliveryContractBackend()
        result = _agent(workspace, backend, max_tool_rounds=2).run(
            "生成一个交付文件。",
            params=RunParams(delivery_contract={"schema_version": "delivery_contract.v1", "artifacts": "output.md"}, save=False),
        )

        doctor_report = json.loads((workspace / ".agent_delivery/contract_doctor.json").read_text(encoding="utf-8"))
        assert backend.calls == 3
        assert backend.saw_contract_doctor is True
        assert "[DELIVERY_CONTRACT_DOCTOR_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert doctor_report["ok"] is False
        assert doctor_report["repair_actions"][0]["recommended_action"] == "rematerialize_delivery_contract"
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
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["non_terminal"] is True
        assert report["reason"] == "delivery_contract_missing"
        assert report["artifacts"] == []


# LLM: Malformed validation details should not terminally block a task that still has an artifact target.
# 函数用途: 验证内部 validation_contract 字段写坏时只返还 doctor 上下文，不直接用 BLOCKED 否定已有产物目标。
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
                            "preferred_path": "outputs/report.xlsx",
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


# LLM: Failed delivery contracts must not pretend the task is complete.
# 函数用途: 验证产物验收失败时不会输出完成标记，而是把结构化 finding 传给下一轮模型修复。
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
        assert any(str(path).endswith("outputs/html_report/index.html") for path in repair["repair_targets"])


# LLM: Failed artifact findings should repair in the next model turn and then close out.
# 函数用途: 覆盖“合同失败 -> recovery 注入 -> 模型返工 -> 再验收通过”的完整闭环。
def test_tool_loop_repairs_failed_artifact_findings_before_closeout():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = ArtifactFindingRepairBackend()
        result = _agent(workspace, backend, max_tool_rounds=4).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert _closeout_report(workspace)["ok"] is True


# LLM: Missing artifact refs should repair via the contracted path rather than stop after one failure.
# 函数用途: 覆盖模型写错路径时，delivery closeout 通过结构化 recovery 打回到正确产物路径。
def test_tool_loop_repairs_missing_artifact_to_contract_path_before_closeout():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = MissingArtifactRepairBackend()
        result = _agent(workspace, backend, max_tool_rounds=4).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/html_report/index.html").exists()
        assert _closeout_report(workspace)["ok"] is True


# LLM: Incomplete contracted artifacts must not trigger delivery completion.
# 函数用途: 覆盖真实家具 E2E 中半截 HTML 被误收口的问题，要求 contract findings 进入下一轮。
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


# LLM: Delivery closeout must not pass while any chunked write session remains open.
# 函数用途: 有 open file_write_session manifest 时，即使目录已存在也不能输出完成标记。
# LLM: Repeated explicit failed submissions must not reintroduce the old delivery rework marker.
# 函数用途: 验证模型反复提交同一个坏产物时，只按普通工具轮预算停住，不输出旧的隐式返工硬标记。
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


# LLM: A failed delivery contract is scoped to the run that carried that contract, not every later run.
# 函数用途: 防止 workspace 里的旧 closeout.json 把后续无 delivery_contract 的普通任务强制拉回旧产物修复。
def test_delivery_repair_context_does_not_leak_into_uncontracted_later_run():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_stale_delivery_closeout(workspace)
        backend = UncontractedFollowupBackend()

        result = _agent(workspace, backend).run(
            "把当前任务的说明写到 lab_outputs/tool-recovery/report.md。",
            params=RunParams(save=False),
        )

        assert backend.calls == 2
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" not in result.response
        assert (workspace / "lab_outputs/tool-recovery/report.md").exists()


# LLM: Missing bootstrap targets should delay no-progress blocking for multi-file artifacts.
# 函数用途: 覆盖真实多文件任务中“先有 index、后补 app.js/source_data”的中段阶段。
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
        assert (workspace / "outputs/static_site/index.html").exists()
        assert (workspace / "outputs/static_site/app.js").read_text(encoding="utf-8") == 'console.log("shop ready");'


# LLM: failed closeout should guide repair without blocking useful inspection reads.
# 函数用途: 验证 closeout 返工单能推动主代理修产物，同时不再用 delivery repair 独立门拦截只读动作。
# LLM: Recovery attempt identity should reset stale no-progress counters before staged repair runs.
# 函数用途: 覆盖真实续跑中旧 local-progress 计数继承到新 attempt，导致阶段修复还没开始就被阻断的问题。
# LLM: _write_site_index creates the already-materialized part of a static site contract.
# 函数用途: 给 open-session 测试准备 index.html 和站点目录。
def _write_site_index(workspace: Path) -> None:
    (workspace / "outputs/static_site").mkdir(parents=True)
    (workspace / "outputs/static_site/index.html").write_text(
        "<!doctype html><html><body><main>Sample</main></body></html>",
        encoding="utf-8",
    )


# LLM: _closeout_report loads the latest structured delivery report.
# 函数用途: 测试只读取 closeout.json 里的机器字段，不解析模型自然语言回复。
def _closeout_report(workspace: Path) -> dict[str, object]:
    return json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))


# LLM: _write_stale_delivery_closeout creates an old failed contract report from a different task.
# 函数用途: 构造 workspace 里已经存在的旧交付失败报告，用来测试新 run 的范围隔离。
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


# LLM: UncontractedFollowupBackend fails fast if old delivery repair context is injected.
# 类用途: 模拟同一 workspace 后续普通任务；它不带 delivery_contract，因此不应看到旧修复合同。
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


# LLM: MalformedDeliveryContractBackend proves contract Doctor findings reach the next turn.
# 类用途: 模拟外部结构化合同本身写坏；第二轮必须看到 Doctor 的机器 finding。
class MalformedDeliveryContractBackend:
    name = "fake_malformed_delivery_contract_backend"

    def __init__(self):
        self.calls = 0
        self.saw_contract_doctor = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","path":"outputs/draft.md","content":"draft"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"草稿已写入，请系统验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "delivery-contract-doctor" in prompt
        assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in prompt
        assert "rematerialize_delivery_contract" in prompt
        self.saw_contract_doctor = True
        return ModelResponse(text="已收到合同结构返工要求。", backend=self.name)


# LLM: ValidationContractStringListBackend proves malformed validation fields re-enter the model loop.
# 类用途: 模拟产物目标存在但 validation_contract 字段类型写坏，确认不再直接终止任务。
class ValidationContractStringListBackend:
    name = "fake_validation_contract_string_list_backend"

    def __init__(self):
        self.calls = 0
        self.saw_contract_doctor = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","path":"outputs/report.xlsx","content":"placeholder"}\n[/TOOL_CALL]',
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


# LLM: _closeout_finding_codes returns validator finding codes from the first artifact.
# 函数用途: 让失败验收断言集中检查结构化 code 字段。
def _closeout_finding_codes(workspace: Path) -> list[str]:
    report = _closeout_report(workspace)
    return [item["code"] for item in report["artifacts"][0]["acceptance_report"]["findings"]]


# LLM: _write_stale_xlsx_closeout creates machine recovery facts without depending on prompt prose.
# 函数用途: 构造“缺 workbook、source_data 为空”的通用阶段修复状态。
def _write_stale_xlsx_closeout(workspace: Path) -> None:
    report = {
        "ok": False,
        "delivery_progress": {
            "failure_fingerprint": "failed-xlsx",
            "work_progress_fingerprint": "empty-source",
            "pending_materialization_targets": [
                {"workspace_relative_path": "outputs/table_report/table_report.xlsx", "exists": False}
            ],
            "recovery_actions": [
                {
                    "category": "artifact",
                    "checkpoint_ref": "outputs/table_report/source_data.json",
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


# LLM: _write_stale_local_progress_state simulates a previous failed attempt's accumulated exploration debt.
# 函数用途: 构造续跑前已经到达阻断阈值的 local-progress 状态文件。
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
