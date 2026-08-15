import json
from dataclasses import asdict
from pathlib import Path

from agent_py_agent.agent.subagents import ContextManifest, QualityContract
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_create_run_persists_quality_contract(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")

    task = manager.create_run(
        goal="Ship a report",
        thought="Define quality before execution.",
        plan=["draft", "check evidence"],
        quality_contract=QualityContract(
            user_visible_goal="Readable final report",
            quality_bar="Evidence-backed and ready for review",
            must_check=["real artifact"],
            evidence_required=["test output"],
        ),
        context_manifest=ContextManifest(
            task_pack_refs=["task.md"],
            required_read_paths=["README.md"],
            quality_contract_ref="task.quality_contract",
            token_budget=1200,
        ),
        context_packs=[{"name": "core", "summary": "No fake done"}],
    )

    loaded = manager.load(task.id)

    assert loaded.quality_contract.quality_bar == "Evidence-backed and ready for review"
    assert loaded.context_manifest.required_read_paths == ["README.md"]
    assert loaded.context_packs[0]["name"] == "core"


def _write_task_json(run_dir: Path, payload: dict) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_current_task_json(workspace: Path) -> None:
    _write_task_json(
        workspace / "normalized-run",
        {
            "id": "normalized-run",
            "goal": "Normalized goal",
            "thought": "Normalized thought",
            "plan": ["one"],
            "capability_requests": [],
            "quality_contract": {"quality_bar": "current bar", "must_check": ["sample"]},
            "context_manifest": {"task_pack_refs": ["task-pack.md"], "token_budget": 42},
            "context_packs": [{"name": "current-pack"}],
        },
    )


def _write_minimal_task_json(workspace: Path) -> None:
    _write_task_json(
        workspace / "minimal-run",
        {
            "id": "minimal-run",
            "goal": "Minimal goal",
            "thought": "Minimal thought",
            "plan": ["one"],
        },
    )


def test_load_task_json_normalizes_current_payload(tmp_path):
    workspace = tmp_path / "subs"
    _write_current_task_json(workspace)
    _write_minimal_task_json(workspace)
    manager = SubAgentManager(workspace)

    task = manager.load("normalized-run")
    bare_task = manager.load("minimal-run")

    assert task.quality_contract.quality_bar == "current bar"
    assert task.quality_contract.must_check == ["sample"]
    assert task.context_manifest.task_pack_refs == ["task-pack.md"]
    assert task.context_manifest.token_budget == 42
    assert task.context_packs == [{"name": "current-pack"}]
    assert task.capability_requests == []
    assert bare_task.quality_contract.quality_bar == ""
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

    context = manager.runner_context.write_execution_context(task.id)
    payload = json.loads(Path(context.execution_context_json).read_text(encoding="utf-8"))

    assert context.quality_contract.user_visible_goal == "Working patch"
    assert context.context_manifest.required_read_paths == ["spec.md"]
    assert context.context_packs[0]["name"] == "task-pack"
    assert payload["quality_contract"]["evidence_required"] == ["pytest"]
    assert payload["context_manifest"]["role_pack"] == "producer"


def test_execution_context_markdown_states_result_handoff_rule(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="Review output",
        thought="Worker submits material only.",
        plan=["inspect"],
    )

    context = manager.runner_context.write_execution_context(task.id)
    text = Path(context.execution_context_file).read_text(encoding="utf-8")

    assert "hand results back to the caller" in text
    assert "Quality Contract" in text
    assert "Context Manifest" in text


def test_legacy_workflow_fields_are_ignored_when_loading_task(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="Fix API bug and add tests",
        thought="Use the explicit worker contract.",
        plan=["implement", "test", "review"],
    )
    payload = asdict(task)
    payload.update(
        {
            "workflow_mode": "auto",
            "workflow_template_id": "code_feature_split",
            "workflow_plan": {"ok": True},
            "workflow_child_run_ids": ["legacy-child"],
        }
    )

    loaded = manager.persistence.task_from_payload(payload)

    assert loaded.id == task.id
    assert loaded.goal == task.goal
    assert not hasattr(loaded, "workflow_mode")
