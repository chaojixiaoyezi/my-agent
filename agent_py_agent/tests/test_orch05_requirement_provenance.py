from __future__ import annotations

"""Comprehensive tests for ORCH-05 Requirement Provenance & Targeted Invalidation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
    RequirementProvenanceTool,
    build_manage_requirement_provenance_model_spec,
)
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.verification.project_facts import ProjectFacts
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidence,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.requirement_provenance import (
    AcceptanceRecord,
    IndependentReviewRecord,
    ObservationScopeHonestyError,
    RequirementProvenanceRecord,
    SelfReviewForbiddenError,
    StaleInputVersionError,
    normalize_path,
    normalize_paths,
    path_intersects,
    provenance_closeout_summary,
    validate_independent_review,
    validate_scope_upgrade,
)


def _make_evidence(
    root: Path,
    *,
    status: str = "passed",
    scope: str = "full",
    command: str = "pytest",
) -> VerificationEvidence:
    return VerificationEvidence(
        command=command,
        canonical_command="pytest",
        kind="test",
        scope=scope,
        status=status,
        exit_code=0 if status == "passed" else 1,
        cwd=str(root),
        root=str(root),
        output_summary="test summary",
    )


# ---------------------------------------------------------------------------
# 1. Path helpers and normalization tests
# ---------------------------------------------------------------------------


def test_path_normalization_and_intersection(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    file_a = root / "src" / "app.py"
    file_b = root / "src" / "utils.py"

    norm_a = normalize_path("src/app.py", root)
    norm_b = normalize_path("src/utils.py", root)
    norm_dir = normalize_path("src", root)

    assert norm_a == str(file_a.resolve(strict=False)).replace("\\", "/")
    assert path_intersects(norm_a, norm_a)
    assert not path_intersects(norm_a, norm_b)
    # Directory prefix match
    assert path_intersects(norm_dir, norm_a)
    assert path_intersects(norm_a, norm_dir)

    all_norm = normalize_paths(["src/app.py", "src/utils.py", "src/app.py"], root)
    assert len(all_norm) == 2
    assert norm_a in all_norm
    assert norm_b in all_norm


# ---------------------------------------------------------------------------
# 2. Requirement binding & version change invalidation
# ---------------------------------------------------------------------------


def test_requirement_binding_and_version_drift(tmp_path: Path):
    owner = tmp_path / "owner-test"
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("owner-test", "thread-1", "task-1")
    repo = VerificationEvidenceRepository(owner)

    rec1 = RequirementProvenanceRecord(
        requirement_id="ORCH-05",
        owner_ref="owner-test",
        task_id="task-1",
        root=str(project),
        input_version="commit-abc",
        watched_paths=("src/core.py", "src/repo.py"),
        author_principal_id="principal-alice",
    )
    saved = repo.bind_requirement(context, rec1)
    assert saved.requirement_id == "ORCH-05"
    assert saved.input_version == "commit-abc"
    assert saved.review_status == "missing"
    assert saved.acceptance_status == "unaccepted"
    assert saved.observation_scope == "unverified"

    # Idempotent re-bind with identical version preserves status
    rec_same = repo.bind_requirement(context, rec1)
    assert rec_same.review_status == "missing"
    assert rec_same.acceptance_status == "unaccepted"

    # Re-bind with updated input_version invalidates previous status
    rec2 = RequirementProvenanceRecord(
        requirement_id="ORCH-05",
        owner_ref="owner-test",
        task_id="task-1",
        root=str(project),
        input_version="commit-def",
        watched_paths=("src/core.py", "src/repo.py"),
        author_principal_id="principal-alice",
    )
    saved2 = repo.bind_requirement(context, rec2)
    assert saved2.input_version == "commit-def"
    assert saved2.review_status == "stale"
    assert saved2.acceptance_status == "invalidated"
    assert saved2.invalidation_reason == "INPUT_VERSION_CHANGED"


# ---------------------------------------------------------------------------
# 3. SELF_REVIEW_FORBIDDEN invariant
# ---------------------------------------------------------------------------


def test_self_review_forbidden_fails_closed(tmp_path: Path):
    owner = tmp_path / "owner-test"
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("owner-test", "thread-1", "task-1")
    repo = VerificationEvidenceRepository(owner)

    rec = RequirementProvenanceRecord(
        requirement_id="ORCH-05",
        owner_ref="owner-test",
        task_id="task-1",
        root=str(project),
        input_version="commit-100",
        watched_paths=("src/core.py",),
        author_principal_id="principal-alice",
    )
    repo.bind_requirement(context, rec)

    # 1. Author reviewing themselves raises SelfReviewForbiddenError
    with pytest.raises(SelfReviewForbiddenError) as exc_info:
        repo.record_independent_review(
            context,
            IndependentReviewRecord(
                review_id="rev-1",
                requirement_id="ORCH-05",
                reviewer_principal_id="principal-alice",
                author_principal_id="principal-alice",
                reviewed_input_version="commit-100",
                verdict="pass",
                scope="code_review",
            ),
        )
    assert "SELF_REVIEW_FORBIDDEN" in str(exc_info.value)

    # 2. Empty reviewer principal fails closed
    with pytest.raises(SelfReviewForbiddenError):
        repo.record_independent_review(
            context,
            IndependentReviewRecord(
                review_id="rev-2",
                requirement_id="ORCH-05",
                reviewer_principal_id="  ",
                author_principal_id="principal-alice",
                reviewed_input_version="commit-100",
                verdict="pass",
                scope="code_review",
            ),
        )

    # 3. Independent reviewer (Bob != Alice) succeeds
    bob_review = repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-3",
            requirement_id="ORCH-05",
            reviewer_principal_id="principal-bob",
            author_principal_id="principal-alice",
            reviewed_input_version="commit-100",
            verdict="pass",
            scope="code_review",
        ),
    )
    assert bob_review.reviewer_principal_id == "principal-bob"
    updated_req = repo.get_requirement("owner-test", "ORCH-05")
    assert updated_req is not None
    assert updated_req.review_status == "approved"

    # 4. Changes requested sets status to changes_requested
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-4",
            requirement_id="ORCH-05",
            reviewer_principal_id="principal-charlie",
            author_principal_id="principal-alice",
            reviewed_input_version="commit-100",
            verdict="changes_requested",
            scope="code_review",
        ),
    )
    req_cr = repo.get_requirement("owner-test", "ORCH-05")
    assert req_cr is not None
    assert req_cr.review_status == "changes_requested"


# ---------------------------------------------------------------------------
# 4. Observation scope honesty: module_only vs full_candidate
# ---------------------------------------------------------------------------


def test_observation_scope_honesty_and_unrelated_requirements(tmp_path: Path):
    owner = tmp_path / "owner-test"
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("owner-test", "thread-1", "task-1")
    repo = VerificationEvidenceRepository(owner)

    # Bind two requirements: Req A and Req B
    repo.bind_requirement(
        context,
        RequirementProvenanceRecord(
            requirement_id="REQ-A",
            owner_ref="owner-test",
            task_id="task-1",
            root=str(project),
            input_version="v1",
            watched_paths=("src/module_a.py",),
            author_principal_id="author-1",
        ),
    )
    repo.bind_requirement(
        context,
        RequirementProvenanceRecord(
            requirement_id="REQ-B",
            owner_ref="owner-test",
            task_id="task-1",
            root=str(project),
            input_version="v1",
            watched_paths=("src/module_b.py",),
            author_principal_id="author-2",
        ),
    )

    # Record a targeted test event (module_only scope)
    ev_targeted = repo.record(context, _make_evidence(project, status="passed", scope="targeted"))

    # Link targeted evidence to REQ-A
    updated_a = repo.link_verification(
        context,
        requirement_id="REQ-A",
        event_ids=[ev_targeted["id"]],
        observation_scope="module_only",
    )
    assert updated_a.observation_scope == "module_only"
    assert updated_a.verification_event_ids == (ev_targeted["id"],)

    # Unrelated requirement REQ-B remains completely unverified!
    req_b = repo.get_requirement("owner-test", "REQ-B")
    assert req_b is not None
    assert req_b.observation_scope == "unverified"
    assert req_b.verification_event_ids == ()

    # Attempting to upgrade REQ-A to 'full_candidate' with only targeted evidence fails closed!
    with pytest.raises(ObservationScopeHonestyError) as exc_info:
        repo.link_verification(
            context,
            requirement_id="REQ-A",
            event_ids=[ev_targeted["id"]],
            observation_scope="full_candidate",
        )
    assert "CANNOT_UPGRADE_TO_FULL_CANDIDATE" in str(exc_info.value)

    # Record integrated test evidence (scope='full')
    ev_full = repo.record(context, _make_evidence(project, status="passed", scope="full"))

    # Still fails if independent review has not been recorded or approved
    with pytest.raises(ObservationScopeHonestyError) as exc_info2:
        repo.link_verification(
            context,
            requirement_id="REQ-A",
            event_ids=[ev_full["id"]],
            observation_scope="full_candidate",
        )
    assert "CANNOT_UPGRADE_TO_FULL_CANDIDATE_WITHOUT_APPROVED_REVIEW" in str(exc_info2.value)

    # Independent review on an OLDER version (v0) does not satisfy requirement v1
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-old",
            requirement_id="REQ-A",
            reviewer_principal_id="reviewer-ext",
            author_principal_id="author-1",
            reviewed_input_version="v0-stale",
            verdict="pass",
            scope="architecture",
        ),
    )
    with pytest.raises(ObservationScopeHonestyError):
        repo.link_verification(
            context,
            requirement_id="REQ-A",
            event_ids=[ev_full["id"]],
            observation_scope="full_candidate",
        )

    # Approved review matching exact current version (v1)
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-v1",
            requirement_id="REQ-A",
            reviewer_principal_id="reviewer-ext",
            author_principal_id="author-1",
            reviewed_input_version="v1",
            verdict="pass",
            scope="architecture",
        ),
    )

    # Now upgrading to full_candidate succeeds honestly!
    full_a = repo.link_verification(
        context,
        requirement_id="REQ-A",
        event_ids=[ev_full["id"]],
        observation_scope="full_candidate",
    )
    assert full_a.observation_scope == "full_candidate"
    assert ev_full["id"] in full_a.verification_event_ids


# ---------------------------------------------------------------------------
# 5. Targeted change invalidation & disjoint unchanged evidence reuse
# ---------------------------------------------------------------------------


def test_targeted_invalidation_and_disjoint_evidence_reuse(tmp_path: Path):
    owner = tmp_path / "owner-test"
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "docs").mkdir()

    file_a = project / "src" / "core.py"
    file_b = project / "docs" / "readme.md"
    file_a.write_text("print('core')\n")
    file_b.write_text("# Readme\n")

    context = VerificationContext("owner-test", "thread-1", "task-1")
    repo = VerificationEvidenceRepository(owner)

    # Bind REQ-1 (watches src/core.py) and REQ-2 (watches docs/readme.md)
    repo.bind_requirement(
        context,
        RequirementProvenanceRecord(
            requirement_id="REQ-1",
            owner_ref="owner-test",
            task_id="task-1",
            root=str(project),
            input_version="commit-1",
            watched_paths=(str(file_a),),
            author_principal_id="author-1",
        ),
    )
    repo.bind_requirement(
        context,
        RequirementProvenanceRecord(
            requirement_id="REQ-2",
            owner_ref="owner-test",
            task_id="task-1",
            root=str(project),
            input_version="commit-1",
            watched_paths=(str(file_b),),
            author_principal_id="author-2",
        ),
    )

    # Full tests + independent reviews for both
    ev = repo.record(context, _make_evidence(project, status="passed", scope="full"))
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-req1",
            requirement_id="REQ-1",
            reviewer_principal_id="reviewer-1",
            author_principal_id="author-1",
            reviewed_input_version="commit-1",
            verdict="pass",
            scope="review",
        ),
    )
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-req2",
            requirement_id="REQ-2",
            reviewer_principal_id="reviewer-2",
            author_principal_id="author-2",
            reviewed_input_version="commit-1",
            verdict="pass",
            scope="review",
        ),
    )

    repo.link_verification(context, requirement_id="REQ-1", event_ids=[ev["id"]], observation_scope="full_candidate")
    repo.link_verification(context, requirement_id="REQ-2", event_ids=[ev["id"]], observation_scope="full_candidate")

    # Both accepted
    repo.record_acceptance(
        context,
        AcceptanceRecord(
            acceptance_id="acc-1",
            requirement_id="REQ-1",
            accepted_by_principal_id="coordinator",
            accepted_input_version="commit-1",
        ),
    )
    repo.record_acceptance(
        context,
        AcceptanceRecord(
            acceptance_id="acc-2",
            requirement_id="REQ-2",
            accepted_by_principal_id="coordinator",
            accepted_input_version="commit-1",
        ),
    )

    req1_before = repo.get_requirement("owner-test", "REQ-1")
    req2_before = repo.get_requirement("owner-test", "REQ-2")
    assert req1_before.review_status == "approved" and req1_before.acceptance_status == "accepted"
    assert req2_before.review_status == "approved" and req2_before.acceptance_status == "accepted"

    # Now edit ONLY src/core.py via mark_edited
    edit_result = repo.mark_edited(context, root=project, paths=[str(file_a)])
    assert "REQ-1" in edit_result.get("invalidated_requirements", [])
    assert "REQ-2" not in edit_result.get("invalidated_requirements", [])

    # Check REQ-1 is invalidated
    req1_after = repo.get_requirement("owner-test", "REQ-1")
    assert req1_after is not None
    assert req1_after.review_status == "stale"
    assert req1_after.acceptance_status == "invalidated"
    assert req1_after.invalidation_reason == "WATCHED_PATH_MODIFIED"

    # Check REQ-2 with DISJOINT watched paths RETAINS its valid review and acceptance!
    req2_after = repo.get_requirement("owner-test", "REQ-2")
    assert req2_after is not None
    assert req2_after.review_status == "approved"
    assert req2_after.acceptance_status == "accepted"
    assert req2_after.invalidation_reason == ""
    assert req2_after.observation_scope == "full_candidate"


# ---------------------------------------------------------------------------
# 6. Acceptance version validation & rejection of stale input_version
# ---------------------------------------------------------------------------


def test_record_acceptance_stale_rejection(tmp_path: Path):
    owner = tmp_path / "owner-test"
    project = tmp_path / "project"
    project.mkdir()
    context = VerificationContext("owner-test", "thread-1", "task-1")
    repo = VerificationEvidenceRepository(owner)

    repo.bind_requirement(
        context,
        RequirementProvenanceRecord(
            requirement_id="ORCH-05",
            owner_ref="owner-test",
            task_id="task-1",
            root=str(project),
            input_version="v2.0",
            watched_paths=("src/app.py",),
            author_principal_id="alice",
        ),
    )

    # 1. Acceptance fails if review is not approved
    with pytest.raises(ValueError) as exc_info:
        repo.record_acceptance(
            context,
            AcceptanceRecord(
                acceptance_id="acc-fail",
                requirement_id="ORCH-05",
                accepted_by_principal_id="lead",
                accepted_input_version="v2.0",
            ),
        )
    assert "REQUIREMENT_NOT_APPROVED" in str(exc_info.value)

    # Approve review on v2.0
    repo.record_independent_review(
        context,
        IndependentReviewRecord(
            review_id="rev-ok",
            requirement_id="ORCH-05",
            reviewer_principal_id="bob",
            author_principal_id="alice",
            reviewed_input_version="v2.0",
            verdict="pass",
            scope="full",
        ),
    )

    # 2. Acceptance fails with StaleInputVersionError if version differs (e.g. accepted on v1.0)
    with pytest.raises(StaleInputVersionError) as exc_info2:
        repo.record_acceptance(
            context,
            AcceptanceRecord(
                acceptance_id="acc-stale",
                requirement_id="ORCH-05",
                accepted_by_principal_id="lead",
                accepted_input_version="v1.0",
            ),
        )
    assert "STALE_INPUT_VERSION" in str(exc_info2.value)

    # 3. Acceptance on current version v2.0 succeeds
    acc = repo.record_acceptance(
        context,
        AcceptanceRecord(
            acceptance_id="acc-good",
            requirement_id="ORCH-05",
            accepted_by_principal_id="lead",
            accepted_input_version="v2.0",
        ),
    )
    assert acc.accepted_input_version == "v2.0"
    req_done = repo.get_requirement("owner-test", "ORCH-05")
    assert req_done is not None
    assert req_done.acceptance_status == "accepted"


# ---------------------------------------------------------------------------
# 7. Closeout summary structure & soft advisory notes (never a prose gate)
# ---------------------------------------------------------------------------


def test_provenance_closeout_summary_advisories():
    rec_stale = RequirementProvenanceRecord(
        requirement_id="REQ-1",
        owner_ref="owner",
        task_id="t1",
        root="/proj",
        input_version="v1",
        watched_paths=("a.py",),
        review_status="stale",
        acceptance_status="invalidated",
        observation_scope="module_only",
        invalidation_reason="WATCHED_PATH_MODIFIED",
    )
    rec_ready = RequirementProvenanceRecord(
        requirement_id="REQ-2",
        owner_ref="owner",
        task_id="t1",
        root="/proj",
        input_version="v1",
        watched_paths=("b.py",),
        review_status="approved",
        acceptance_status="accepted",
        observation_scope="full_candidate",
    )

    summary = provenance_closeout_summary([rec_stale, rec_ready])
    assert summary["total_requirements"] == 2
    assert summary["verified_count"] == 2
    assert summary["full_candidate_count"] == 1
    assert summary["approved_count"] == 1
    assert summary["stale_review_count"] == 1
    assert summary["invalidated_count"] == 1
    assert summary["is_closeout_ready"] is False
    assert any("需重新独立评审" in note for note in summary["advisory_notes"])
    assert any("验收已失效" in note for note in summary["advisory_notes"])


# ---------------------------------------------------------------------------
# 8. RequirementProvenanceTool execute_scoped REQUESTER_IDENTITY_UNPROVEN Gate
# ---------------------------------------------------------------------------


def test_tool_execute_scoped_requester_identity_unproven_gate(tmp_path: Path):
    owner_home = tmp_path / "home"
    owner_home.mkdir()

    # Create dummy agent
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="owner-tool"),
        root=str(tmp_path),
        thread_id="thread-main",
        _current_run_params=None,
    )

    tool = RequirementProvenanceTool(agent)
    spec = tool.model_spec
    assert spec.name == "manage_requirement_provenance"
    assert spec.hints.default_deferred is True

    # 1. Missing context (None) fails closed with REQUESTER_IDENTITY_UNPROVEN
    outcome_no_ctx = tool.execute_scoped(
        {"action": "query_provenance"},
        None,  # type: ignore[arg-type]
    )
    assert outcome_no_ctx.ok is False
    assert outcome_no_ctx.error_code == "TOOL_PERMISSION_DENIED"
    assert outcome_no_ctx.effect_outcome == "not_started"
    payload = json.loads(outcome_no_ctx.output)
    assert payload["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    assert payload["details"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    assert outcome_no_ctx.result_envelope["details"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"

    # 2. Context with empty run_params fails closed with REQUESTER_IDENTITY_UNPROVEN
    dummy_snapshot = SimpleNamespace(runtimes=())
    inv_ctx = ToolInvocationContext(runtime_snapshot=dummy_snapshot)

    agent._current_run_params = SimpleNamespace(
        run_id="",
        task_id="",
        request_id="   ",
    )
    outcome_empty_params = tool.execute_scoped(
        {"action": "query_provenance"},
        inv_ctx,
    )
    assert outcome_empty_params.ok is False
    assert outcome_empty_params.error_code == "TOOL_PERMISSION_DENIED"
    assert outcome_empty_params.effect_outcome == "not_started"
    payload_empty = json.loads(outcome_empty_params.output)
    assert payload_empty["reason"] == "REQUESTER_IDENTITY_UNPROVEN"

    # 3. Proven requester identity allows execution
    agent._current_run_params = SimpleNamespace(
        run_id="run-proven-123",
        task_id="task-proven-123",
        request_id="req-123",
    )
    outcome_proven = tool.execute_scoped(
        {"action": "query_provenance"},
        inv_ctx,
    )
    assert outcome_proven.ok is True
    data = json.loads(outcome_proven.output)
    assert data["ok"] is True
    assert data["action"] == "query_provenance"
    assert data["records"] == []


# ---------------------------------------------------------------------------
# 9. Tool End-to-End Orchestration Actions via Tool
# ---------------------------------------------------------------------------


def test_tool_actions_end_to_end(tmp_path: Path):
    owner_home = tmp_path / "owner-e2e"
    owner_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="owner-e2e"),
        root=str(project),
        thread_id="thread-e2e",
        _current_run_params=SimpleNamespace(run_id="runner-1", task_id="task-e2e", request_id="req-e2e"),
    )
    tool = RequirementProvenanceTool(agent)
    repo = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("owner-e2e", "thread-e2e", "task-e2e")

    # 1. bind_requirement
    res_bind = tool.execute({
        "action": "bind_requirement",
        "requirement_id": "ORCH-05",
        "input_version": "rev-1",
        "watched_paths": ["src/app.py"],
        "author_principal_id": "worker-1",
    })
    assert res_bind.ok is True
    data_bind = json.loads(res_bind.output)
    assert data_bind["requirement"]["requirement_id"] == "ORCH-05"
    assert data_bind["requirement"]["author_principal_id"] == "worker-1"

    # 2. self-review via tool fails closed with SELF_REVIEW_FORBIDDEN
    res_self_rev = tool.execute({
        "action": "record_independent_review",
        "requirement_id": "ORCH-05",
        "reviewer_principal_id": "worker-1",
        "author_principal_id": "worker-1",
        "reviewed_input_version": "rev-1",
        "review_verdict": "pass",
    })
    assert res_self_rev.ok is False
    assert res_self_rev.error_code == "TOOL_PERMISSION_DENIED"
    assert res_self_rev.effect_outcome == "not_started"
    data_self = json.loads(res_self_rev.output)
    assert data_self["reason"] == "SELF_REVIEW_FORBIDDEN"

    # 3. Independent review by reviewer-2 passes
    res_rev = tool.execute({
        "action": "record_independent_review",
        "requirement_id": "ORCH-05",
        "reviewer_principal_id": "reviewer-2",
        "author_principal_id": "worker-1",
        "reviewed_input_version": "rev-1",
        "review_verdict": "pass",
        "review_scope": "code_review",
    })
    assert res_rev.ok is True

    # 4. Record integrated verification evidence and link it
    ev = repo.record(context, _make_evidence(project, status="passed", scope="full"))
    res_link = tool.execute({
        "action": "link_verification",
        "requirement_id": "ORCH-05",
        "verification_event_ids": [ev["id"]],
        "observation_scope": "full_candidate",
        "input_version": "rev-1",
    })
    assert res_link.ok is True
    data_link = json.loads(res_link.output)
    assert data_link["requirement"]["observation_scope"] == "full_candidate"

    # 5. Stale acceptance rejection (version rev-0)
    res_acc_stale = tool.execute({
        "action": "record_acceptance",
        "requirement_id": "ORCH-05",
        "accepted_by_principal_id": "coord-lead",
        "accepted_input_version": "rev-0",
    })
    assert res_acc_stale.ok is False
    data_acc_stale = json.loads(res_acc_stale.output)
    assert data_acc_stale["reason"] == "STALE_INPUT_VERSION"

    # 6. Valid acceptance
    res_acc = tool.execute({
        "action": "record_acceptance",
        "requirement_id": "ORCH-05",
        "accepted_by_principal_id": "coord-lead",
        "accepted_input_version": "rev-1",
    })
    assert res_acc.ok is True

    # 7. query_provenance shows closeout ready
    res_query = tool.execute({"action": "query_provenance", "requirement_id": "ORCH-05"})
    assert res_query.ok is True
    data_query = json.loads(res_query.output)
    assert len(data_query["records"]) == 1
    assert data_query["records"][0]["acceptance_status"] == "accepted"
    assert data_query["summary"]["is_closeout_ready"] is True
