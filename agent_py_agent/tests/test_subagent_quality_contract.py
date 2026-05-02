import json
from pathlib import Path

from agent_py_agent.agent.subagent import (
    ContextManifest,
    QualityContract,
    SubAgentManager,
)


def test_create_run_persists_quality_contract(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")

    task = manager.create_run(
        goal="Ship a report",
        thought="Define quality before execution.",
        plan=["draft", "check evidence"],
        quality_contract=QualityContract(
            user_visible_goal="Readable final report",
            quality_bar="Evidence-backed and ready for parent review",
            must_check=["real artifact"],
            evidence_required=["test output"],
            final_judge="parent_final_gate",
        ),
        context_manifest=ContextManifest(
            task_pack_refs=["task.md"],
            required_read_paths=["README.md"],
            quality_contract_ref="task.quality_contract",
            token_budget=1200,
        ),
        context_packs=[{"name": "core", "summary": "No fake done"}],
    )

    payload = json.loads((Path(task.task_dir) / "task.json").read_text(encoding="utf-8"))

    assert payload["quality_contract"]["quality_bar"] == "Evidence-backed and ready for parent review"
    assert payload["quality_contract"]["cannot_self_accept"] is True
    assert payload["quality_contract"]["parent_final_gate"] is True
    assert payload["context_manifest"]["required_read_paths"] == ["README.md"]
    assert payload["context_packs"][0]["name"] == "core"


def test_load_legacy_task_json_is_compatible(tmp_path):
    workspace = tmp_path / "subs"
    run_dir = workspace / "legacy-run"
    bare_run_dir = workspace / "bare-legacy-run"
    run_dir.mkdir(parents=True)
    bare_run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps(
            {
                "id": "legacy-run",
                "goal": "Legacy goal",
                "thought": "Legacy thought",
                "plan": ["one"],
                "capability_requests": {"old": "dict-shape"},
                "quality_contract": {"quality_bar": "legacy bar", "must_check": "sample"},
                "context_manifest": {"task_pack_refs": "legacy.md", "token_budget": "42"},
                "context_packs": {"name": "legacy-pack"},
                "future_field": "ignored",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bare_run_dir / "task.json").write_text(
        json.dumps(
            {
                "id": "bare-legacy-run",
                "goal": "Bare legacy goal",
                "thought": "Bare legacy thought",
                "plan": ["one"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = SubAgentManager(workspace)

    task = manager.load("legacy-run")
    bare_task = manager.load("bare-legacy-run")

    assert task.quality_contract.quality_bar == "legacy bar"
    assert task.quality_contract.must_check == ["sample"]
    assert task.context_manifest.task_pack_refs == ["legacy.md"]
    assert task.context_manifest.token_budget == 42
    assert task.context_packs == [{"name": "legacy-pack"}]
    assert task.capability_requests == []
    assert bare_task.quality_contract.final_judge == "parent_final_gate"
    assert bare_task.context_manifest.core_pack_version == "subagent-quality-contract-v1"
    assert bare_task.context_packs == []


def test_execution_context_contains_quality_contract_and_manifest(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="Build a small patch",
        thought="Keep the scope tight.",
        plan=["edit", "test"],
        quality_contract={"user_visible_goal": "Working patch", "evidence_required": ["pytest"]},
        context_manifest={"required_read_paths": ["spec.md"], "role_pack": "producer"},
        context_packs=[{"name": "task-pack", "path": "spec.md"}],
    )

    context = manager.write_execution_context(task.id)
    payload = json.loads(Path(context.execution_context_json).read_text(encoding="utf-8"))

    assert context.quality_contract.user_visible_goal == "Working patch"
    assert context.context_manifest.required_read_paths == ["spec.md"]
    assert context.context_packs[0]["name"] == "task-pack"
    assert payload["quality_contract"]["evidence_required"] == ["pytest"]
    assert payload["context_manifest"]["role_pack"] == "producer"


def test_execution_context_markdown_states_parent_final_gate_rule(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="Review output",
        thought="Worker submits material only.",
        plan=["inspect"],
    )

    context = manager.write_execution_context(task.id)
    text = Path(context.execution_context_file).read_text(encoding="utf-8")

    assert "Subagents cannot self-accept" in text
    assert "final_judge/parent_final_gate" in text
    assert "Quality Contract" in text
    assert "Context Manifest" in text


def test_create_run_workflow_plan_persists_without_raw_json(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")

    task = manager.create_run(
        goal="Fix API bug and add tests",
        thought="Let workflow compiler produce the worker contract first.",
        plan=["route", "compile", "review"],
        workflow_mode="plan",
    )
    payload = json.loads((Path(task.task_dir) / "task.json").read_text(encoding="utf-8"))

    assert task.workflow_mode == "plan"
    assert task.workflow_plan["ok"] is True
    assert task.workflow_template_id == "code_feature_split"
    assert payload["workflow_mode"] == "plan"
    assert payload["workflow_plan"]["selected_template_id"] == "code_feature_split"
    assert "Implementation satisfies the shared contract" in task.acceptance_checks
    assert "Focused verification passes" in task.acceptance_checks
