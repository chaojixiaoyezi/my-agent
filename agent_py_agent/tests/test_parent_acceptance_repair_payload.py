"""LLM: tests for parent acceptance repair advice in dispatch payloads.

模块用途: 验证父级真实验收失败会转换成 refs-first 修复建议，而不是误走通用接管通道。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_dispatch_payload import dispatch_record_payload
from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool


# LLM: test_rejected_child_acceptance_enters_repair_lane protects failed parent-test closeout.
# 函数用途: child 最新验收已 REJECT 时，父 runner 不能把它当成完成，也不该默认接管旧 runner。
def test_rejected_child_acceptance_enters_repair_lane(tmp_path: Path):
    reports_dir = _reports_dir(tmp_path, message="missing child")

    direct = _dispatch_direct_children(_child(reports_dir))

    assert direct["needs_parent_acceptance_repair_wave"] is True
    assert direct["needs_recovery"] is False
    assert direct["rejected_acceptance_run_ids"] == ["child-a"]
    assert direct["ready_for_parent_acceptance"] is False
    assert direct["next_action"] == "create_repair_child_from_parent_acceptance_refs"
    assert direct["parent_acceptance_repair_advice"]["failed_run_ids"] == ["child-a"]


# LLM: test_rejected_parent_tests_include_scoped_repair_child protects actionable repair handoff.
# 函数用途: 父级真实验收 tests 拒绝 child 后，payload 要给修复小傻妞建议和具体失败 refs。
def test_rejected_parent_tests_include_scoped_repair_child(tmp_path: Path):
    reports_dir = _reports_dir(tmp_path, message="父级真实验收测试失败：total=2 failed=1")
    _write_failed_test_refs(reports_dir)

    direct = _dispatch_direct_children(_child(reports_dir))

    advice = direct["parent_acceptance_repair_advice"]
    assert direct["needs_parent_acceptance_repair_wave"] is True
    assert direct["needs_recovery"] is False
    assert direct["next_action"] == "create_repair_child_from_parent_acceptance_refs"
    assert advice["failed_run_ids"] == ["child-a"]
    assert advice["failure_refs"][0]["test_ref"].endswith("test_execution.json")
    repair_child = advice["suggested_tool_call"]["children"][0]
    assert repair_child["role"] == "worker"
    assert "child-a" in repair_child["goal"]
    assert "HTML语法静态检查" in repair_child["goal"]
    assert repair_child["repair_contract"]["schema"] == "subagent_repair_contract.v1"
    assert "execute_generated_scripts_or_commands_if_needed" in repair_child["repair_contract"]["same_run_required_actions"]
    assert repair_child["repair_contract"]["output_tests_required"] is True
    assert "content_check" in repair_child["repair_contract"]["recommended_test_methods"]
    assert any("output.json.tests" in item for item in repair_child["acceptance_checks"])
    assert str(reports_dir / "test_execution.json") in repair_child["required_read_paths"]
    assert repair_child["context_packs"][0]["kind"] == "repair_contract"


# LLM: top-level dispatch rejects should suggest create_subagents, not runner-only schedule_child_subagents.
# 函数用途: 顶层 root 看到父级验收失败时，应拿到可派修复小傻妞的 refs-first 工具建议。
def test_top_level_rejected_parent_tests_include_repair_child_tool_call(tmp_path: Path):
    fixture = _top_level_reject_fixture(tmp_path)

    payload = dispatch_record_payload(fixture.item)

    assert payload["next_action"] == "create_repair_child_from_parent_acceptance_refs"
    advice = payload["parent_acceptance_repair_advice"]
    assert advice["failed_run_ids"] == ["child-a"]
    assert advice["failure_refs"][0]["output_ref"] == str(fixture.output_ref)
    suggested = advice["suggested_tool_call"]
    assert suggested["tool"] == "create_subagents"
    assert suggested["extra_write_roots"] == [str(fixture.product_root)]
    assert str(fixture.test_ref) in suggested["goal"]
    assert suggested["repair_contract"]["kind"] == "parent_acceptance"
    assert str(fixture.test_ref) in suggested["repair_contract"]["required_read_paths"]
    assert str(fixture.missing_xlsx) in suggested["repair_contract"]["target_artifact_refs"]
    assert suggested["agent_name"] == "小傻妞-验收修复"
    assert suggested["context_manifest"]["task_pack_refs"] == ["subagent_repair_contract.v1"]


# LLM: repair waves must not shrink the original child contract to the latest symptom.
# 函数用途: 顶层验收修复建议要继承失败 child 的完整验收条件，避免只补最近一个静态错误就误通过。
def test_top_level_repair_child_inherits_original_acceptance_contract(tmp_path: Path):
    fixture = _top_level_reject_fixture(tmp_path)
    original_check = "示例站必须有完整注册、登录、流程状态、结算和下单成功流程"
    _write_original_task_contract(fixture.run_dir, goal="做一个完整示例网站", checks=[original_check])

    payload = dispatch_record_payload(fixture.item)

    advice = payload["parent_acceptance_repair_advice"]
    failure_ref = advice["failure_refs"][0]
    suggested = advice["suggested_tool_call"]
    assert failure_ref["task_ref"] == str(fixture.run_dir / "task.json")
    assert failure_ref["original_acceptance_checks"] == [original_check]
    assert original_check in suggested["repair_contract"]["full_success_checks"]
    assert any(original_check in item for item in suggested["acceptance_checks"])
    assert original_check in suggested["goal"]


# LLM: runner-context repair suggestions need the same full-contract inheritance as top-level root.
# 函数用途: 父 runner 直接创建修复 child 时，也不能把原始验收要求缩成 failure_refs 的局部错误。
def test_direct_repair_child_inherits_original_acceptance_contract(tmp_path: Path):
    reports_dir = _reports_dir(tmp_path, message="父级真实验收测试失败：total=2 failed=1")
    _write_failed_test_refs(reports_dir)
    original_check = "最终页面必须包含 register、login、cart、checkout 和 order-confirmation 区域"
    _write_original_task_contract(reports_dir.parent, goal="做完整示例站首页", checks=[original_check])

    direct = _dispatch_direct_children(_child(reports_dir))

    repair_child = direct["parent_acceptance_repair_advice"]["suggested_tool_call"]["children"][0]
    assert original_check in repair_child["repair_contract"]["full_success_checks"]
    assert any(original_check in item for item in repair_child["acceptance_checks"])
    assert original_check in repair_child["goal"]


# LLM: _top_level_reject_fixture keeps the repair-contract test below size limits.
# 函数用途: 准备顶层 parent acceptance reject 记录和对应 run/test/output refs。
def _top_level_reject_fixture(tmp_path: Path) -> SimpleNamespace:
    run_dir = tmp_path / "child-a"
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True)
    refs = _top_level_reject_refs(run_dir, tmp_path / "deliverables" / "site-output")
    return SimpleNamespace(
        run_dir=run_dir,
        item=_top_level_reject_item(refs.test_ref, refs.followup_ref),
        **refs.__dict__,
    )


# LLM: _top_level_reject_refs writes machine refs for one rejected child.
# 函数用途: 写 run.json、output.json 和 test_execution.json，让 repair payload 从真实小文件取合同字段。
def _top_level_reject_refs(run_dir: Path, product_root: Path) -> SimpleNamespace:
    test_ref = run_dir / "reports" / "test_execution.json"
    output_ref = run_dir / "output.json"
    missing_xlsx = product_root / "github_weekly_star_growth_short.xlsx"
    _write_parent_reject_run_ref(run_dir, product_root)
    output_ref.write_text(json.dumps({"artifacts": [{"path": "deliverables/site-output/index.html"}]}), encoding="utf-8")
    test_ref.write_text(json.dumps({"records": [{"validation_result": {"ok": False, "path": str(missing_xlsx)}}]}), encoding="utf-8")
    return SimpleNamespace(
        test_ref=test_ref,
        followup_ref=run_dir / "reports" / "parent_acceptance_auto_followup.json",
        output_ref=output_ref,
        product_root=product_root,
        missing_xlsx=missing_xlsx,
    )


# LLM: _write_parent_reject_run_ref records product roots for the top-level repair suggestion.
# 函数用途: 写 run.json，模拟失败 child 已授权的产物根。
def _write_parent_reject_run_ref(run_dir: Path, product_root: Path) -> None:
    (run_dir / "run.json").write_text(
        json.dumps({
            "task_dir": str(run_dir),
            "allowed_write_roots": [str(run_dir), str(product_root)],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: _write_original_task_contract records the failed child contract beside run.json.
# 函数用途: 写 task.json 中的原始 goal/acceptance_checks，模拟真实子代理创建时保留的完整任务合同。
def _write_original_task_contract(run_dir: Path, *, goal: str, checks: list[str]) -> None:
    (run_dir / "task.json").write_text(
        json.dumps({"goal": goal, "acceptance_checks": checks}, ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: _top_level_reject_item mirrors the dispatch acceptance reject record.
# 函数用途: 构造顶层 dispatch_record_payload 需要的 parent acceptance reject 字段。
def _top_level_reject_item(test_ref: Path, followup_ref: Path) -> SimpleNamespace:
    return SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="child-a",
        ok=False,
        dry_run=True,
        applied=False,
        message="父级真实验收测试失败",
        before_status="AWAITING_ACCEPTANCE",
        after_status="AWAITING_ACCEPTANCE",
        runner_child_status_counts={},
        parent_acceptance_auto_execution_test_ref=str(test_ref),
        parent_acceptance_test_failure_summary="inert_control_hits=13",
        parent_acceptance_test_failure_details=["index.html:a:客厅家具 href=#"],
        parent_acceptance_followup_ref=str(followup_ref),
    )


# LLM: Artifact integrity blocks should become scoped repair work instead of generic blocker prose.
# 函数用途: child 产物结构检查失败时，顶层 dispatch payload 要建议创建修复小傻妞读取 output/run refs。
def test_top_level_artifact_integrity_blocker_includes_repair_child_tool_call(tmp_path: Path):
    fixture = _artifact_integrity_blocker_fixture(tmp_path)

    payload = dispatch_record_payload(fixture.item)

    assert payload["next_action"] == "create_repair_child_from_artifact_integrity_refs"
    advice = payload["artifact_integrity_repair_advice"]
    assert advice["failed_run_ids"] == ["child-a"]
    assert advice["failure_refs"][0]["output_ref"] == str(fixture.run_dir / "output.json")
    assert advice["failure_refs"][0]["artifact_refs"] == [str(fixture.artifact)]
    suggested = advice["suggested_tool_call"]
    assert suggested["tool"] == "create_subagents"
    assert suggested["agent_name"] == "小傻妞-产物修复"
    assert suggested["extra_write_roots"] == [str(fixture.product_root)]
    assert "missing_body_close" in suggested["goal"]
    assert suggested["repair_contract"]["kind"] == "artifact_integrity"
    assert suggested["repair_contract"]["target_artifact_refs"] == [str(fixture.artifact)]
    assert "execute_generated_scripts_or_commands_if_needed" in suggested["repair_contract"]["same_run_required_actions"]


# LLM: _artifact_integrity_blocker_fixture writes the compact run/output refs used by repair payload tests.
# 函数用途: 准备一条真实 classify_blocker 记录和对应 output.json，保持测试主体短而聚焦断言。
def _artifact_integrity_blocker_fixture(tmp_path: Path) -> SimpleNamespace:
    run_dir = tmp_path / "child-a"
    run_dir.mkdir()
    product_root = tmp_path / "deliverables" / "site-output"
    product_root.mkdir(parents=True)
    artifact = product_root / "index.html"
    artifact.write_text("<html><body>", encoding="utf-8")
    (run_dir / "WORK_LOG.md").write_text("- blocked", encoding="utf-8")
    _write_artifact_integrity_run_ref(run_dir, product_root)
    _write_artifact_integrity_output_ref(run_dir, artifact)
    return SimpleNamespace(
        run_dir=run_dir,
        product_root=product_root,
        artifact=artifact,
        item=_artifact_integrity_dispatch_item(run_dir),
    )


# LLM: _write_artifact_integrity_run_ref records product roots for suggested repair children.
# 函数用途: 写 run.json，模拟失败 child 已授权的 runtime 根和产物根。
def _write_artifact_integrity_run_ref(run_dir: Path, product_root: Path) -> None:
    (run_dir / "run.json").write_text(
        json.dumps({
            "task_dir": str(run_dir),
            "allowed_write_roots": [str(run_dir), str(product_root)],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: _write_artifact_integrity_output_ref writes the failed artifact integrity output shape.
# 函数用途: 写 output.json，包含 blocker、artifact ref 和 artifact_integrity test。
def _write_artifact_integrity_output_ref(run_dir: Path, artifact: Path) -> None:
    (run_dir / "output.json").write_text(
        json.dumps(_artifact_integrity_output_payload(artifact), ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: _artifact_integrity_output_payload keeps the failed output fixture reusable and compact.
# 函数用途: 返回产物结构失败的 output.json 最小字段。
def _artifact_integrity_output_payload(artifact: Path) -> dict:
    blocker = f"artifact_integrity_failed:{artifact}:missing_body_close"
    return {
        "status": "BLOCKED",
        "structured_output": {"status": "BLOCKED", "blocked_reason": blocker},
        "blockers": [blocker],
        "artifacts": [{"path": str(artifact), "kind": "file"}],
        "tests": [{
            "name": "artifact integrity",
            "validation_method": "artifact_integrity",
            "ok": False,
            "summary": f"{artifact}:missing_body_close",
        }],
    }


# LLM: _artifact_integrity_dispatch_item mirrors the dispatch classify_blocker record from real E2E.
# 函数用途: 构造顶层 dispatch record，evidence_paths 只给 WORK_LOG.md。
def _artifact_integrity_dispatch_item(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        step="action_apply",
        action="classify_blocker",
        run_id="child-a",
        ok=True,
        dry_run=False,
        applied=True,
        message="已记录 classify_blocker 待人工处理。",
        before_status="BLOCKED",
        after_status="BLOCKED",
        evidence_paths=[str(run_dir / "WORK_LOG.md")],
    )


# LLM: _dispatch_direct_children builds a runner-context dispatch payload from simple task records.
# 函数用途: 复用真实 DispatchSubagentsTool payload builder，只替换 manager/list_runs 依赖。
def _dispatch_direct_children(*children: SimpleNamespace) -> dict:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"total": 0}
    mock_report.records = []
    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "root"
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = list(children)
    payload = DispatchSubagentsTool(mock_agent)._report_payload(mock_report)
    return payload["direct_children"]


# LLM: _child returns the minimal child status needed for direct_children progress.
# 函数用途: 构造一个等待父级验收但已被 REJECT 的 child task stub。
def _child(reports_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id="child-a",
        parent_id="root",
        status="AWAITING_ACCEPTANCE",
        verification_status="NEEDS_ACCEPTANCE",
        reports_dir=str(reports_dir),
    )


# LLM: _reports_dir writes the smallest rejected acceptance audit.
# 函数用途: 创建 reports 目录和 acceptance_review.json，供 repair payload 读取。
def _reports_dir(tmp_path: Path, *, message: str) -> Path:
    reports_dir = tmp_path / "child-a" / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT", "message": message}, ensure_ascii=False),
        encoding="utf-8",
    )
    return reports_dir


# LLM: _write_failed_test_refs adds parent test/followup facts to the rejected child fixture.
# 函数用途: 写入 test_execution 和 followup failed_tests，验证 goal 中有可执行修复线索。
def _write_failed_test_refs(reports_dir: Path) -> None:
    (reports_dir / "test_execution.json").write_text(
        json.dumps({
            "total_tests": 2,
            "failed": 1,
            "records": [_failed_test_record()],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (reports_dir / "parent_acceptance_auto_followup.json").write_text(
        json.dumps({
            "followup": {
                "action": "plan_rescue",
                "status": "needs_manual_rescue",
                "test_execution_ref": str(reports_dir / "test_execution.json"),
                "failed_tests": [{"name": "HTML语法静态检查", "error": "内容不相等"}],
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: _failed_test_record keeps the fixture compact and close to real parent test output.
# 函数用途: 返回一条 content_check 失败记录，模拟真实 E2E 中 HTML 不完整的父级测试。
def _failed_test_record() -> dict[str, object]:
    return {
        "test_name": "HTML语法静态检查",
        "executed": True,
        "exit_code": 1,
        "error": "内容不相等",
        "validation_method": "content_check",
        "validation_result": {"ok": False},
    }
