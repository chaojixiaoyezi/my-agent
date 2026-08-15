from pathlib import Path

from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidence,
    VerificationEvidenceRepository,
)


def _evidence(root: Path, *, status: str = "passed", scope: str = "full") -> VerificationEvidence:
    return VerificationEvidence(
        command="pytest -q",
        canonical_command="pytest",
        kind="test",
        scope=scope,
        status=status,
        exit_code=0 if status == "passed" else 1,
        cwd=str(root),
        root=str(root),
        output_summary="24 passed" if status == "passed" else "1 failed",
    )


def test_latest_evidence_becomes_stale_after_a_successful_edit(tmp_path: Path):
    owner = tmp_path / "owner-a"
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("providers/feishu/users/a", "thread-1", "task-1")
    repository = VerificationEvidenceRepository(owner)

    repository.record(context, _evidence(project))
    passed = repository.status(context, root=project)
    repository.mark_edited(context, root=project, paths=[str(project / "app.py")])
    stale = repository.status(context, root=project)

    assert passed["status"] == "passed"
    assert passed["evidence"]["scope"] == "full"
    assert stale["status"] == "stale"
    assert stale["changed_paths"] == [str(project / "app.py")]


def test_targeted_and_failed_facts_are_never_upgraded(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("owner-a", "thread-1", "task-1")
    repository = VerificationEvidenceRepository(tmp_path / "owner-a")

    repository.record(context, _evidence(project, status="failed", scope="targeted"))
    status = repository.status(context, root=project)

    assert status["status"] == "failed"
    assert status["evidence"]["scope"] == "targeted"
    assert status["evidence"]["exit_code"] == 1


def test_owner_databases_and_task_streams_are_isolated(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    context_a = VerificationContext("owner-a", "thread-a", "task-a")
    context_b = VerificationContext("owner-b", "thread-b", "task-b")
    repository_a = VerificationEvidenceRepository(tmp_path / "owner-a")
    repository_b = VerificationEvidenceRepository(tmp_path / "owner-b")

    repository_a.record(context_a, _evidence(project))

    assert repository_a.status(context_a, root=project)["status"] == "passed"
    assert repository_a.status(context_b, root=project)["status"] == "unverified"
    assert repository_b.status(context_b, root=project)["status"] == "unverified"
    assert repository_a.db_path != repository_b.db_path
