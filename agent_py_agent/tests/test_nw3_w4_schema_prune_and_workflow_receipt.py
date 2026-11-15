# LLM: 同时验证 provider Schema 不可达 $defs 裁剪与 WorkflowsTool 失败步骤结构化回执；隔离临时目录运行，不触碰真实宿主状态。
# 模块用途: 锁定 NW3-W4（INIT-CTX Bucket A.1 + LIVE4V-P2-01）在工具裁剪、描述符引导保留与工作流失败诊断上的核心契约。
from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_py_agent.agent.backends.tool_schema import (
    _prune_unreachable_defs,
    tool_model_spec_to_input_schema,
)
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.models import ToolModelSpec
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def _write_skill(owner_home: Path, name: str) -> None:
    path = owner_home / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: fixture\n---\n按规范步骤分析。\n", encoding="utf-8")


def _sample_descriptor(asset_id: str, kind: str) -> dict[str, object]:
    return {
        "id": asset_id,
        "kind": kind,
        "name": f"{asset_id}-名称",
        "access_targets": [],
        "data_sources": [],
        "credential_refs": [],
        "io_contract": {"inputs": ["number"], "outputs": ["value"]},
    }


def _register_script(scripts_tool: ScriptsTool, script_id: str, code: str) -> None:
    outcome = scripts_tool.execute(
        {
            "action": "register",
            "script_id": script_id,
            "descriptor": _sample_descriptor(script_id, "script"),
            "code": code,
        }
    )
    assert outcome.ok, outcome.output


def test_real_workflows_and_scripts_schemas_prune_unreachable_defs_and_preserve_guidance(tmp_path: Path):
    workflows_tool = WorkflowsTool(tmp_path)
    scripts_tool = ScriptsTool(tmp_path)

    wf_spec = workflows_tool.model_spec
    sc_spec = scripts_tool.model_spec

    wf_raw_snapshot = copy.deepcopy(wf_spec.input_schema)
    sc_raw_snapshot = copy.deepcopy(sc_spec.input_schema)

    wf_pruned = tool_model_spec_to_input_schema(wf_spec)
    sc_pruned = tool_model_spec_to_input_schema(sc_spec)

    # canonical input_schema 不得被就地篡改
    assert wf_spec.input_schema == wf_raw_snapshot
    assert sc_spec.input_schema == sc_raw_snapshot
    wf_spec.assert_schema_hash()
    sc_spec.assert_schema_hash()

    assert set(wf_pruned.get("$defs", {})) == {"JsonScalar", "JsonValue"}
    assert set(sc_pruned.get("$defs", {})) == {"JsonScalar", "JsonValue"}

    raw_chars = len(json.dumps(wf_raw_snapshot, ensure_ascii=False)) + len(
        json.dumps(sc_raw_snapshot, ensure_ascii=False)
    )
    pruned_chars = len(json.dumps(wf_pruned, ensure_ascii=False)) + len(
        json.dumps(sc_pruned, ensure_ascii=False)
    )
    assert raw_chars - pruned_chars > 15_000

    descriptor_prop = wf_pruned["properties"]["definition"]["properties"]["descriptor"]
    desc_text = descriptor_prop.get("description", "")
    assert isinstance(desc_text, str) and desc_text.strip()
    assert "inputs" in desc_text

    # 裁剪后的 provider schema 仍可通过正常 save 请求校验
    valid_save = {
        "action": "save",
        "workflow_id": "pruned-check",
        "definition": {
            "descriptor": {
                "name": "裁剪校验",
                "access_targets": [],
                "data_sources": [],
                "credential_refs": [],
                "io_contract": {"inputs": ["n"], "outputs": ["v"]},
            },
            "steps": [],
        },
    }
    assert validate_tool_input(valid_save, wf_pruned).ok


def test_prune_unreachable_defs_handles_transitive_cycles_and_pops_empty_defs():
    cyclic_spec = ToolModelSpec(
        name="cyclic_probe",
        description="可达闭包测试",
        input_schema={
            "type": "object",
            "properties": {"root": {"$ref": "#/$defs/NodeA"}},
            "$defs": {
                "NodeA": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/$defs/NodeB"}},
                },
                "NodeB": {
                    "type": "object",
                    "properties": {
                        "leaf": {"$ref": "#/$defs/NodeC"},
                        "back": {"$ref": "#/$defs/NodeA"},
                    },
                },
                "NodeC": {"type": "string"},
                "DeadX": {"type": "object", "properties": {"y": {"$ref": "#/$defs/DeadY"}}},
                "DeadY": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    )
    pruned_cyclic = tool_model_spec_to_input_schema(cyclic_spec)
    assert list(pruned_cyclic["$defs"].keys()) == ["NodeA", "NodeB", "NodeC"]

    unused_only_spec = ToolModelSpec(
        name="unused_only",
        description="全不可达测试",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "$defs": {"Unused": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    pruned_unused = tool_model_spec_to_input_schema(unused_only_spec)
    assert "$defs" not in pruned_unused

    empty_defs_spec = ToolModelSpec(
        name="empty_defs",
        description="空defs移除测试",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "$defs": {},
            "additionalProperties": False,
        },
    )
    pruned_empty = tool_model_spec_to_input_schema(empty_defs_spec)
    assert "$defs" not in pruned_empty


def test_prune_unreachable_defs_fails_safe_on_non_plain_or_missing_refs():
    direct_only_cases = [
        {
            "type": "object",
            "properties": {"x": {"$ref": "#"}},
            "$defs": {"Unused": {"type": "string"}},
        },
        {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/MissingDef"}},
            "$defs": {"Unused": {"type": "string"}},
        },
        {
            "type": "object",
            "properties": {"x": {"$ref": 123}},
            "$defs": {"Unused": {"type": "string"}},
        },
    ]
    spec_cases = [
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"$ref": "#/properties/a"}},
            "$defs": {"Unused": {"type": "string"}},
        },
        {
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/Used/properties/inner"}},
            "$defs": {
                "Used": {"type": "object", "properties": {"inner": {"type": "string"}}},
                "Unused": {"type": "integer"},
            },
        },
        {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "$defs": {
                "UnusedWithNonPlain": {
                    "type": "object",
                    "properties": {"self_ref": {"$ref": "#/properties/x"}},
                },
                "OtherUnused": {"type": "string"},
            },
        },
    ]
    for raw in direct_only_cases:
        snapshot = copy.deepcopy(raw)
        assert _prune_unreachable_defs(raw) == snapshot
    for raw in spec_cases:
        snapshot = copy.deepcopy(raw)
        assert _prune_unreachable_defs(raw) == snapshot
        spec = ToolModelSpec(name="failsafe_probe", description="安全退回", input_schema=snapshot)
        rendered = tool_model_spec_to_input_schema(spec)
        assert rendered == snapshot


def test_workflow_run_failure_returns_actionable_partial_steps_receipt(tmp_path: Path):
    _write_skill(tmp_path, "skill-A")
    scripts_tool = ScriptsTool(tmp_path)
    _register_script(scripts_tool, "collect", 'import json; print(json.dumps({"value": 42}))')

    workflows_tool = WorkflowsTool(tmp_path, engine=WorkflowEngine(tmp_path))
    saved = workflows_tool.execute(
        {
            "action": "save",
            "workflow_id": "daily",
            "definition": {
                "descriptor": _sample_descriptor("daily", "workflow"),
                "steps": [
                    {"step_id": "fetch", "name": "采集", "mode": "deterministic_script", "script_id": "collect"},
                    {"step_id": "judge", "name": "分析", "mode": "llm_skill_analysis", "skill_ids": ["skill-A"]},
                ],
            },
        }
    )
    assert saved.ok, saved.output

    outcome = workflows_tool.execute({"action": "run", "workflow_id": "daily", "inputs": {"number": 2}})
    assert not outcome.ok
    assert outcome.error_code == "WORKFLOW_FAILED"
    assert outcome.next_action

    body = json.loads(outcome.output)
    assert body["status"] == "failed"
    assert body["error_code"] == "WORKFLOW_FAILED"
    assert body["side_effect_state"] == "partial_steps_executed"
    assert body["failed_step"] == {
        "failed_step_id": "judge",
        "step_error_code": "ANALYZER_UNAVAILABLE",
        "step_error_summary": outcome.result_envelope["failed_step"]["step_error_summary"],
        "side_effect_state": "partial_steps_executed",
    }
    step_summary = body["failed_step"]["step_error_summary"]
    assert step_summary and "\n" not in step_summary and len(step_summary) <= 240
    assert "judge" in body["error"]
    assert "ANALYZER_UNAVAILABLE" in body["error"]
    assert "partial_steps_executed" in body["error"]
    assert "inspect_run" in outcome.next_action and body["workflow_run_id"] in outcome.next_action
    assert "resume" in outcome.next_action
    assert body["next_action"] == outcome.next_action
    assert outcome.result_envelope["failed_step"] == body["failed_step"]
    assert outcome.result_envelope["next_action"] == outcome.next_action


def test_workflow_run_first_step_failure_reports_not_started_and_sanitizes_long_detail(tmp_path: Path, monkeypatch):
    _write_skill(tmp_path, "skill-A")
    workflows_tool = WorkflowsTool(tmp_path, engine=WorkflowEngine(tmp_path))
    workflows_tool.execute(
        {
            "action": "save",
            "workflow_id": "single-step",
            "definition": {
                "descriptor": _sample_descriptor("single-step", "workflow"),
                "steps": [
                    {"step_id": "first_judge", "name": "首步分析", "mode": "llm_skill_analysis", "skill_ids": ["skill-A"]},
                ],
            },
        }
    )

    outcome = workflows_tool.execute({"action": "run", "workflow_id": "single-step"})
    assert not outcome.ok
    assert outcome.error_code == "WORKFLOW_FAILED"
    body = json.loads(outcome.output)
    assert body["side_effect_state"] == "not_started"
    assert body["failed_step"]["failed_step_id"] == "first_judge"
    assert body["failed_step"]["side_effect_state"] == "not_started"
    assert "inspect_run" in body["next_action"] and "resume" in body["next_action"]

    # 直接核对长多行含敏感值失败消息脱敏并有界截断为单行 <= 240 字符
    synthetic_run = {
        "workflow_id": "single-step",
        "workflow_run_id": "wf-run-sanitize-1",
        "status": "failed",
        "failed_step_id": "s2",
        "steps": [
            {"step_id": "s1", "status": "succeeded"},
            {
                "step_id": "s2",
                "status": "failed",
                "error_code": "SCRIPT_FAILED",
                "error_message": "Bearer sk-secret-token-987654\nline2 " + ("detail-" * 60),
                "escalation_result": {"effect_started": False},
            },
        ],
        "confirmed_step_refs": ["s1"],
    }
    monkeypatch.setattr(
        workflows_tool,
        "_dispatch",
        lambda *_args, **_kwargs: copy.deepcopy(synthetic_run),
    )
    sanitized_outcome = workflows_tool.execute({"action": "run", "workflow_id": "single-step"})
    assert not sanitized_outcome.ok
    assert sanitized_outcome.error_code == "WORKFLOW_FAILED"
    sanitized_body = json.loads(sanitized_outcome.output)
    assert "sk-secret-token-987654" not in sanitized_outcome.output
    summary = sanitized_body["failed_step"]["step_error_summary"]
    assert "\n" not in summary and "\r" not in summary
    assert len(summary) <= 240
    assert sanitized_body["side_effect_state"] == "partial_steps_executed"
    assert sanitized_body["failed_step"]["failed_step_id"] == "s2"
    assert sanitized_body["failed_step"]["step_error_code"] == "SCRIPT_FAILED"


def test_workflow_unknown_effect_still_triggers_reconciliation_without_advising_resume(
    tmp_path: Path, monkeypatch
):
    _write_skill(tmp_path, "skill-A")

    def failing_analyzer(*_args, **_kwargs):
        raise TimeoutError("analysis timed out after side effect")

    workflows_tool = WorkflowsTool(
        tmp_path,
        engine=WorkflowEngine(tmp_path, analysis_handler=failing_analyzer),
    )
    workflows_tool.execute(
        {
            "action": "save",
            "workflow_id": "unknown-flow",
            "definition": {
                "descriptor": _sample_descriptor("unknown-flow", "workflow"),
                "steps": [
                    {"step_id": "judge", "name": "分析", "mode": "llm_skill_analysis", "skill_ids": ["skill-A"]},
                ],
            },
        }
    )
    failed = workflows_tool.execute({"action": "run", "workflow_id": "unknown-flow"})
    assert not failed.ok
    assert failed.error_code == "WORKFLOW_FAILED"
    failed_body = json.loads(failed.output)
    assert failed_body["side_effect_state"] == "partial_steps_executed"

    # 未知副作用态再尝试原始 resume 必须维持 WORKFLOW_RECONCILIATION_REQUIRED
    resumed = workflows_tool.execute(
        {"action": "resume", "workflow_run_id": failed_body["workflow_run_id"]}
    )
    assert not resumed.ok
    assert resumed.error_code == "WORKFLOW_RECONCILIATION_REQUIRED"

    # 当步骤带有 effect_outcome='unknown' 且包含 workflow_run_id 时，不应在 next_action 中误导模型 resume
    monkeypatch.setattr(
        workflows_tool,
        "_dispatch",
        lambda *_args, **_kwargs: {
            "workflow_id": "unknown-flow",
            "workflow_run_id": failed_body["workflow_run_id"],
            "status": "failed",
            "failed_step_id": "judge",
            "effect_outcome": "unknown",
            "steps": [
                {
                    "step_id": "judge",
                    "status": "failed",
                    "error_code": "STEP_EFFECT_UNKNOWN",
                    "error_message": "receipt uncertain",
                    "escalation_result": {"effect_started": True, "effect_outcome": "unknown"},
                }
            ],
        },
    )
    reconcile = workflows_tool.execute({"action": "run", "workflow_id": "unknown-flow"})
    assert not reconcile.ok
    assert reconcile.error_code == "WORKFLOW_RECONCILIATION_REQUIRED"
    assert reconcile.result_envelope["recovery_query"]["resume_permitted"] is False
    assert reconcile.next_action == reconcile.result_envelope["recovery_query"]["instruction"]


def test_business_workflow_run_failure_enriches_failed_step_from_run_store(tmp_path: Path, monkeypatch):
    run_store = WorkflowRunStore(tmp_path)
    run_id = "biz-run-001"
    run_store.save_run(
        WorkflowRunRecord(
            workflow_run_id=run_id,
            workflow_id="biz-flow",
            owner_id="owner-1",
            trigger_kind="manual",
            status="failed",
            started_at=10.0,
            ended_at=12.0,
            duration_ms=2000.0,
            failed_step_id="step_b",
            confirmed_step_refs=("step_a",),
            steps=(
                StepExecutionRecord(
                    step_id="step_a",
                    name="第一步",
                    mode="deterministic_script",
                    status="succeeded",
                    started_at=10.0,
                    ended_at=11.0,
                    duration_ms=1000.0,
                    llm_involved=False,
                ),
                StepExecutionRecord(
                    step_id="step_b",
                    name="第二步",
                    mode="llm_skill_analysis",
                    status="failed",
                    started_at=11.0,
                    ended_at=12.0,
                    duration_ms=1000.0,
                    llm_involved=False,
                    error_code="DOWNSTREAM_TIMEOUT",
                    error_message="service timed out on step_b",
                ),
            ),
        )
    )
    workflows_tool = WorkflowsTool(tmp_path)
    monkeypatch.setattr(
        workflows_tool,
        "_dispatch_business",
        lambda _params: {"workflow_run_id": run_id, "operation_id": run_id, "state": "failed"},
    )
    outcome = workflows_tool.execute(
        {"action": "workflows.run", "workflow_ref": "biz-flow", "idempotency_key": "k-1"}
    )
    assert not outcome.ok
    assert outcome.error_code == "WORKFLOW_FAILED"
    body = json.loads(outcome.output)
    assert body["failed_step"]["failed_step_id"] == "step_b"
    assert body["failed_step"]["step_error_code"] == "DOWNSTREAM_TIMEOUT"
    assert "service timed out on step_b" in body["failed_step"]["step_error_summary"]
    assert body["side_effect_state"] == "partial_steps_executed"
    assert "inspect_run" in outcome.next_action and run_id in outcome.next_action
