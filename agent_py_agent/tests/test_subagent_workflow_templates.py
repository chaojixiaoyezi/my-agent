from __future__ import annotations

import json

from agent_py_agent.agent.subagent_workflows import (
    WorkflowTemplate,
    load_template_store,
    validate_template_data,
)


def test_loads_builtin_workflow_templates():
    store = load_template_store()

    assert set(store.templates) >= {
        "single_worker_verified",
        "code_feature_split",
        "producer_critic_repair",
    }
    assert store.get("single_worker_verified").source == "builtin"
    assert store.issues == []


def test_user_template_overrides_builtin_template_id(tmp_path):
    user_dir = tmp_path / "workflow_templates"
    user_dir.mkdir()
    user_template = {
        "id": "single_worker_verified",
        "name": "Custom Single Worker",
        "solves": ["local team policy"],
        "fit_for": ["custom override"],
        "phases": [
            {
                "id": "custom",
                "kind": "worker",
                "task": "Use the local custom workflow.",
            }
        ],
        "parent_acceptance": ["custom acceptance"],
    }
    (user_dir / "single_worker_verified.json").write_text(
        json.dumps(user_template),
        encoding="utf-8",
    )

    store = load_template_store(user_dir)
    template = store.get("single_worker_verified")

    assert isinstance(template, WorkflowTemplate)
    assert template.name == "Custom Single Worker"
    assert template.source == "user"
    assert template.source_path.endswith("single_worker_verified.json")
    assert template.phases[0].id == "custom"


def test_bad_template_reports_validation_issues(tmp_path):
    user_dir = tmp_path / "workflow_templates"
    user_dir.mkdir()
    (user_dir / "bad.json").write_text(
        json.dumps(
            {
                "name": "Bad Template",
                "fit_for": ["tests"],
                "phases": [
                    {
                        "id": "missing_kind_and_task",
                    },
                    {
                        "kind": "worker",
                        "task": "Missing phase id.",
                    },
                ],
                "parent_acceptance": ["reported"],
            }
        ),
        encoding="utf-8",
    )

    store = load_template_store(user_dir)
    messages = [issue.message for issue in store.issues]
    fields = [issue.field for issue in store.issues]

    assert "missing required field: id" in messages
    assert "missing required field: solves" in messages
    assert "phase missing required field: kind" in messages
    assert "phase missing required field: task" in messages
    assert "phases[1].id" in fields
    assert "Bad Template" not in [template.name for template in store.all()]


def test_validate_template_data_reports_required_fields():
    issues = validate_template_data(
        {
            "id": "broken",
            "name": "Broken",
            "solves": ["some problem"],
            "fit_for": ["tests"],
            "phases": [{"kind": "worker", "task": "Missing phase id."}],
            "parent_acceptance": ["reported"],
        },
        source_path="broken.json",
    )

    assert any(issue.field == "phases[0].id" for issue in issues)
    assert all(issue.source_path == "broken.json" for issue in issues)


def test_builtin_templates_include_solves_field():
    store = load_template_store()

    assert store.templates
    assert all(template.solves for template in store.all())
