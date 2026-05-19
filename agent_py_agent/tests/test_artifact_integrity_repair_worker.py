from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.artifact_integrity_repair_worker import (
    maybe_run_artifact_integrity_repair_worker,
)
from agent_py_agent.agent.subagents.execution_records import TestExecutionRecord
from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_artifact_integrity_repair_worker_fixes_hash_links_and_missing_closers(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text(
        '<html><body><main>Shop</main><a href="#">Buy</a><a href="#missing">Missing</a>',
        encoding="utf-8",
    )
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "repair_source_run_id": "source-run",
            "target_artifact_refs": [
                f"artifact_integrity_failed:{artifact}:missing_body_close,missing_html_close,placeholder_hash_link"
            ],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    text = artifact.read_text(encoding="utf-8")
    saved = manager.load(task.id)
    assert result is not None
    assert result.status == "AWAITING_ACCEPTANCE"
    assert saved.status == "AWAITING_ACCEPTANCE"
    assert 'id="top"' in text
    assert 'href="#top"' in text
    assert text.rstrip().endswith("</html>")
    assert saved.runner_last_error == ""
    assert saved.artifact_refs == [str(artifact)]
    assert saved.evidence
    assert saved.evidence[0].ok is True
    assert "artifact_integrity passed" in saved.evidence[0].summary
    output = json.loads(Path(saved.output_json).read_text(encoding="utf-8"))
    assert output["patches"] == []


def test_artifact_integrity_repair_worker_evidence_can_pass_parent_acceptance(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text("<html><body><main>Shop</main>", encoding="utf-8")
    task = agent.subagents.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = agent.subagents.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        agent.subagents,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )
    saved = agent.subagents.load(task.id)
    write_test_execution_report(
        saved.reports_dir,
        [
            TestExecutionRecord(
                test_name="artifact_integrity",
                executed=True,
                exit_code=0,
                validation_method="artifact_integrity",
                validation_result={"ok": True, "path": str(artifact), "blocker_codes": []},
            )
        ],
        options=TestExecutionReportOptions(executed_at="2026-05-18T12:00:00Z"),
    )

    applied = agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")
    reloaded = agent.subagents.load(task.id)

    assert result is not None
    assert applied.applied is True
    assert applied.acceptance_decision == "ACCEPT"
    assert reloaded.status == "DONE"
    assert reloaded.verification_status == "VERIFIED"


def test_artifact_integrity_repair_worker_rejects_refs_outside_allowed_roots(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    artifact = outside / "index.html"
    artifact.write_text('<html><body><a href="#">Bad</a>', encoding="utf-8")
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(allowed)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    assert result is not None
    assert result.status == "BLOCKED"
    assert "no_target_artifacts" in manager.load(task.id).runner_last_error
    assert artifact.read_text(encoding="utf-8") == '<html><body><a href="#">Bad</a>'


def test_artifact_integrity_repair_worker_balances_extra_style_close(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text(
        (
            "<!doctype html><html><head><style>body{color:#111}</style></head>"
            "<body><header>Shop</header>.hero{display:block}</style><main id='top'>Hi</main></body></html>"
        ),
        encoding="utf-8",
    )
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    text = artifact.read_text(encoding="utf-8").lower()
    assert result is not None
    assert result.status == "AWAITING_ACCEPTANCE"
    assert text.count("<style") == text.count("</style>")


def test_artifact_integrity_repair_worker_closes_missing_style_block(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text(
        (
            "<!doctype html><html><head><style>body{color:#111}</head>"
            "<body><main id='top'>Shop</main></body></html>"
        ),
        encoding="utf-8",
    )
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    text = artifact.read_text(encoding="utf-8").lower()
    assert result is not None
    assert result.status == "AWAITING_ACCEPTANCE"
    assert text.count("<style") == text.count("</style>")
    assert text.find("</style>") < text.find("</head>")


def test_artifact_integrity_repair_worker_closes_missing_script_block(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text(
        (
            "<!doctype html><html><head><title>Shop</title></head><body id='top'>"
            "<main>Shop</main><script>function checkout(){return true;}</body></html>"
        ),
        encoding="utf-8",
    )
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    text = artifact.read_text(encoding="utf-8").lower()
    assert result is not None
    assert result.status == "AWAITING_ACCEPTANCE"
    assert text.count("<script") == text.count("</script>")
    assert text.find("</script>") < text.find("</body>")


def test_artifact_integrity_repair_worker_collapses_duplicate_html_skeleton(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    artifact_root = tmp_path / "site"
    artifact_root.mkdir()
    artifact = artifact_root / "index.html"
    artifact.write_text(
        (
            "<!doctype html><html><head><title>One</title><style>body{color:#111}</head>"
            "<body id='top'><button onclick=\"checkout()\">Checkout</button>"
            "<head><title>Two</title></head><body><main>Two</main></body></html>"
        ),
        encoding="utf-8",
    )
    task = manager.create_run(
        goal="repair artifact integrity",
        thought="repair",
        plan=["repair"],
        extra_write_roots=[str(artifact_root)],
        attributes={
            "repair_kind": "artifact_integrity",
            "target_artifact_refs": [str(artifact)],
        },
    )
    prepared = manager.prepare_runner_attempt(task.id)

    result = maybe_run_artifact_integrity_repair_worker(
        manager,
        task.id,
        attempt_id=prepared.runner_active_attempt_id,
    )

    saved = manager.load(task.id)
    text = artifact.read_text(encoding="utf-8").lower()
    assert result is not None
    assert result.status == "AWAITING_ACCEPTANCE"
    assert saved.status == "AWAITING_ACCEPTANCE"
    assert text.count("<html") == 1
    assert text.count("<head") == 1
    assert text.count("<body") == 1
    assert text.count("</body>") == 1
    assert text.count("</html>") == 1
    assert "checkout" in text
    assert "<main>two</main>" in text
