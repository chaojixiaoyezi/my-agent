"""LLM: tests for main-agent machine delivery closeout after tool execution.

给人看的解释：
这个文件验证主代理真实任务的"产物已合格就自动停机"能力，避免产物已经写好还继续跑到超时。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    ArtifactFindingRepairBackend,
    BootstrapMaterializationProgressiveBackend,
    BootstrapMaterializationRedirectBackend,
    DeliveryContractBackend,
    DeliveryRepairRedirectBackend,
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
def _agent(workspace: Path, backend, *, max_tool_rounds: int = 5) -> SimpleAgent:
    cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=max_tool_rounds)
    agent = SimpleAgent(cfg, workspace)
    agent.backend = backend
    return agent


# LLM: Real-task delivery contracts should stop successful runs before extra model turns.
# 函数用途: 验证产物按机器合同验收通过后，主代理工具循环直接收口，不继续读写直到超时。
def test_tool_loop_closes_out_after_delivery_contract_passes():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = DeliveryContractBackend()
        result = _agent(workspace, backend).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/furniture_homepage/index.html").exists()
        assert (workspace / ".agent_delivery/closeout.json").exists()


def test_tool_loop_redirects_repeated_remote_exploration_back_to_local_progress():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        artifact_path = workspace / "memory_archive" / "artifacts" / "tool_outputs" / "demo.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps({"items": [{"name": "demo"}]}, ensure_ascii=False), encoding="utf-8")
        backend = LocalProgressRedirectBackend()

        result = _agent(workspace, backend, max_tool_rounds=8).run(
            "整理 GitHub 项目并生成表格。",
            params=RunParams(delivery_contract=xlsx_delivery_contract(), save=False),
        )

        assert backend.calls == 5
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/github_star_growth/github_star_growth.xlsx").exists()


# LLM: Delivery closeout must use RunParams contracts without requiring prompt markers.
# 函数用途: 验证系统交付合同可以通过结构化运行参数传入，不依赖 user_prompt 中的机器 JSON 标记。
def test_tool_loop_closes_out_from_structured_run_params_delivery_contract():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = DeliveryContractBackend()
        result = _agent(workspace, backend).run(
            "用单文件 html 做一个高端家具品牌首页。",
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert "outputs/furniture_homepage/index.html" in backend.prompts[0]
        assert "[tool-system delivery-contract]" in backend.prompts[0]
        assert "不得引用 http/https 外部" in backend.prompts[0]
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / ".agent_delivery/closeout.json").exists()


def test_tool_loop_reports_malformed_delivery_contract_before_artifact_closeout():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = MalformedDeliveryContractBackend()
        result = _agent(workspace, backend, max_tool_rounds=2).run(
            "生成一个交付文件。",
            params=RunParams(delivery_contract={"schema_version": "delivery_contract.v1", "artifacts": "output.md"}, save=False),
        )

        doctor_report = json.loads((workspace / ".agent_delivery/contract_doctor.json").read_text(encoding="utf-8"))
        assert backend.calls == 2
        assert backend.saw_contract_doctor is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert doctor_report["ok"] is False
        assert doctor_report["repair_actions"][0]["recommended_action"] == "rematerialize_delivery_contract"
        assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in {
            finding["code"] for finding in doctor_report["findings"]
        }


# LLM: Failed delivery contracts must not pretend the task is complete.
# 函数用途: 验证产物验收失败时不会输出完成标记，而是把结构化 finding 传给下一轮模型修复。
def test_tool_loop_does_not_close_out_when_delivery_contract_fails():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = FailedDeliveryContractBackend()
        result = _agent(workspace, backend).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        codes = _closeout_finding_codes(workspace)

        assert backend.calls == 4
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)
        actions = _closeout_report(workspace)["delivery_progress"]["recovery_actions"]
        repair = next(item for item in actions if item["code"] == "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED")
        assert repair["recommended_action"] == "repair_artifact_against_findings"
        assert "write_file" in repair["write_tools"]
        assert "HTML_INCOMPLETE_DOCUMENT" in repair["finding_codes"]
        assert any(str(path).endswith("outputs/furniture_homepage/index.html") for path in repair["repair_targets"])


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

        assert backend.calls == 2
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

        assert backend.calls == 2
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/furniture_homepage/index.html").exists()
        assert _closeout_report(workspace)["ok"] is True


# LLM: Incomplete contracted artifacts must not trigger delivery completion.
# 函数用途: 覆盖真实家具 E2E 中半截 HTML 被误收口的问题，要求 contract findings 进入下一轮。
def test_tool_loop_rejects_incomplete_delivery_contract_artifact():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = IncompleteDeliveryContractBackend()
        result = _agent(workspace, backend).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        codes = _closeout_finding_codes(workspace)

        assert backend.calls == 4
        assert "[DELIVERY_REQUIRED_REPAIR_BLOCKED]" in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)


# LLM: Delivery closeout must not pass while any chunked write session remains open.
# 函数用途: 有 open file_write_session manifest 时，即使目录已存在也不能输出完成标记。
def test_tool_loop_delivery_closeout_blocks_open_file_write_sessions():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_site_index(workspace)
        backend = OpenWriteSessionDeliveryBackend()
        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
            allowed_tools=["file_write_session"],
        )

        assert backend.calls == 3
        assert backend.saw_open_session_context is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == 'console.log("shop ready");'


# LLM: Repeated identical delivery failures must terminate as blocked instead of consuming endless tool rounds.
# 函数用途: 验证 closeout 会识别“同一失败 + 无工作进展”的通用卡死模式，并输出结构化阻塞结果。
def test_tool_loop_blocks_after_repeated_unchanged_delivery_failure():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = NoProgressDeliveryBackend()
        result = _agent(workspace, backend).run(
            delivery_contract_prompt(),
            params=RunParams(delivery_contract=delivery_contract(), save=False),
        )
        report = _closeout_report(workspace)

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["delivery_progress"]["unchanged_failure_count"] >= 4
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
            "做一个购物网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
        )
        report = _closeout_report(workspace)

        assert backend.calls == 3
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" not in result.response
        assert report["ok"] is True
        assert (workspace / "outputs/shopping_site/index.html").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == 'console.log("shop ready");'


# LLM: Startup bootstrap contracts should redirect inspection-only first turns until one target is materialized.
# 函数用途: 验证一个目标都还没出现时，list_files 不会被执行消耗轮次，而是先逼主代理落一个最小有效产物。
def test_tool_loop_redirects_inspection_only_calls_before_any_bootstrap_target_exists():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = BootstrapMaterializationRedirectBackend()
        result = _agent(workspace, backend).run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
            allowed_tools=["list_files", "write_file"],
        )
        report = _closeout_report(workspace)

        assert backend.calls == 3
        assert report["ok"] is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/index.html").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == 'console.log("bootstrapped");'


# LLM: bootstrap startup should allow multiple redirects before escalating to a final block.
# 函数用途: 验证开工阶段连续几轮只检查目录时，系统会持续引导落地目标，而不是过早阻断。
def test_tool_loop_allows_multiple_bootstrap_redirects_before_blocking():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = BootstrapMaterializationProgressiveBackend()
        result = _agent(workspace, backend, max_tool_rounds=7).run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
            allowed_tools=["list_files", "write_file"],
        )

        assert backend.calls == 5
        assert _closeout_report(workspace)["ok"] is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == 'console.log("bootstrapped later");'


# LLM: write-first staged recovery must redirect inspection-only calls before letting the task drift further.
# 函数用途: 验证 closeout 要求先补非空结构化数据时，系统会拦下只读动作，推动主代理先修阶段产物。
def test_tool_loop_redirects_inspection_only_calls_during_required_delivery_repair():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = DeliveryRepairRedirectBackend()
        result = _agent(workspace, backend, max_tool_rounds=6).run(
            "整理 GitHub 周升星项目并生成表格。",
            params=RunParams(delivery_contract=xlsx_delivery_contract(), save=False),
            allowed_tools=["write_file", "read_file", "write_structured_json", "data_to_workbook"],
        )

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert _closeout_report(workspace)["ok"] is True
        assert (workspace / "outputs/github_star_growth/github_star_growth.xlsx").exists()


# LLM: Recovery attempt identity should reset stale no-progress counters before staged repair runs.
# 函数用途: 覆盖真实续跑中旧 local-progress 计数继承到新 attempt，导致阶段修复还没开始就被阻断的问题。
def test_recovery_attempt_uses_repair_contract_before_stale_local_progress_guard():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_stale_xlsx_closeout(workspace)
        _write_stale_local_progress_state(workspace)
        backend = RecoveryAttemptRepairBackend()
        contract = xlsx_delivery_contract()
        contract["recovery"] = {
            "schema_version": "main-agent-real-task-recovery.v1",
            "case_id": "workbook_recovery_case",
            "status": "FAILED",
            "recommended_action": "repair_then_resume_same_case",
            "reason_codes": ["exit_code=2", "artifact_acceptance_failed=2"],
            "packet_ref": "main_agent_task_execution/tasks/workbook_recovery_case/recovery_packet.json",
        }

        result = _agent(workspace, backend, max_tool_rounds=6).run(
            "继续恢复上一轮失败的表格任务。",
            params=RunParams(delivery_contract=contract, save=False),
            allowed_tools=["read_file", "write_structured_json", "data_to_workbook"],
        )

        state = json.loads((workspace / ".agent_delivery/local_progress_guard.json").read_text(encoding="utf-8"))
        assert backend.calls == 3
        assert "[LOCAL_PROGRESS_GUARD_BLOCKED]" not in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert state["exploration_rounds_without_local_progress"] == 0
        assert state["recovery_signature"]
        assert (workspace / "outputs/github_star_growth/github_star_growth.xlsx").exists()


# LLM: New write tools must not run while a file_write_session is still open for the same task.
# 函数用途: 验证 open session 存在时，系统会拒绝新的 write_file，迫使模型先 append/finish 当前 session。
def test_tool_loop_blocks_unrelated_write_tools_while_open_file_write_session_exists():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_site_index(workspace)
        backend = WrongToolDuringOpenSessionBackend()
        result = _agent(workspace, backend, max_tool_rounds=6).run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=web_project_delivery_contract(), save=False),
            allowed_tools=["file_write_session", "write_file"],
        )

        assert backend.calls == 4
        assert not (workspace / "outputs/rogue.txt").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == 'console.log("ok");'
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response


# LLM: _write_site_index creates the already-materialized part of a static site contract.
# 函数用途: 给 open-session 测试准备 index.html 和站点目录。
def _write_site_index(workspace: Path) -> None:
    (workspace / "outputs/shopping_site").mkdir(parents=True)
    (workspace / "outputs/shopping_site/index.html").write_text(
        "<!doctype html><html><body><main>Shop</main></body></html>",
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
                "case_id": "main_direct_web_app",
                "ok": False,
                "delivery_progress": {
                    "recovery_actions": [
                        {
                            "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                            "recommended_action": "repair_artifact_against_findings",
                            "artifact_path": "lab_outputs/main-web-app",
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
        assert "delivery-contract-doctor" in prompt
        assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in prompt
        assert "rematerialize_delivery_contract" in prompt
        self.saw_contract_doctor = True
        return ModelResponse(text="已收到合同结构返工要求。", backend=self.name)


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
                {"workspace_relative_path": "outputs/github_star_growth/github_star_growth.xlsx", "exists": False}
            ],
            "recovery_actions": [
                {
                    "category": "artifact",
                    "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                    "code": "STAGED_JSON_NO_ROWS",
                    "recommended_action": "write_non_empty_structured_rows",
                    "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
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
