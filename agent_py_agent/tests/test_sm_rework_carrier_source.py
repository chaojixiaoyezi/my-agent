"""候选载体请求的共享格式、来源重验和真实队列往返。"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_store import (
    REQUEST_SCHEMA_VERSION,
    SkillLearningStore,
    request_key,
)
from agent_py_agent.agent.capability.skill_learning_validation import (
    default_machine_promotion_verifier,
)
from agent_py_agent.agent.conversation.learning_source import subagent_projection
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    CandidateStoreCorruptError,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import _FakeBackend, _output
from agent_py_agent.tests.test_sm_w8_promotion_curator import _observe_tool_lesson, _setup_owner


def _observe(candidates: CandidateService, *, trigger: str = "user_dialogue", label: str = "first"):
    return candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=(
                "# Procedure\n\n## Steps\n"
                "1. Read the source file.\n2. Validate the output before publication.\n" + label
            ),
            subject_key="source-check-" + label,
            scope=MemoryScope("project", "project:test", applies_when="Review the completed procedure"),
            origin="subagent_lesson",
            source_task_ids=("task-real",),
            source_run_ids=("run-real",),
            evidence_refs=({"kind": "tool_run", "ref_id": "run-real"},),
            learning_source=subagent_projection(
                trigger_kind=trigger, run_ids=("run-real",), task_ids=("task-real",)
            ),
        )
    )


@pytest.fixture
def carrier(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    store = SkillLearningStore.for_home(home)
    backend = _FakeBackend(_output())
    runtime = SkillLearningRuntime(
        backend=backend,
        snapshot_provider=lambda: SimpleNamespace(entries=()),
        owner_id="local:main",
        deployment_root=tmp_path / "deployment",
    )
    service = SkillLearningService(store, SkillLearningSettings(autonomous_verified=True), runtime)
    return SimpleNamespace(candidates=candidates, store=store, backend=backend, runtime=runtime, service=service)


def test_mixed_intake_refuses_scheduled_source_and_publishes_dialogue(carrier) -> None:
    scheduled = _observe(carrier.candidates, trigger="schedule", label="scheduled")
    dialogue = _observe(carrier.candidates, label="dialogue")

    result = carrier.service.enqueue_lesson_candidates([scheduled, dialogue])

    assert [(row["candidate_id"], row["code"]) for row in result["refused"]] == [
        (scheduled.candidate_id, "SKILL_CARRIER_INELIGIBLE")
    ]
    assert [row["candidate_id"] for row in result["queued"]] == [dialogue.candidate_id]
    assert not result["deferred"]
    assert len(carrier.store.pending_requests()) == 1
    assert carrier.service.run_pending().status == "published"
    assert len(carrier.backend.prompts) == 1
    assert len(carrier.store.load_registry().skills) == 1
    assert not carrier.store.pending_requests()


def test_intake_revalidates_canonical_candidate_instead_of_old_snapshot(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1

    result = carrier.service.enqueue_lesson_candidates([candidate])

    assert result["refused"][0]["code"] == "SKILL_CARRIER_INELIGIBLE"
    assert not result["queued"] and not result["deferred"]
    assert not carrier.store.pending_requests() and not carrier.backend.prompts


@pytest.mark.parametrize("legacy_id", [False, True], ids=["candidate-id", "legacy-id"])
def test_redacted_source_is_rejected_before_model_call(carrier, legacy_id: bool) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    if legacy_id:
        path = carrier.store.pending_requests()[0]
        request = carrier.store.read_request(path)
        request["source_candidate_id"] = request.pop("candidate_id")
        carrier.store.rewrite_request(path, request)
    assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


def test_redacted_during_verifier_is_rejected_at_commit(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    verified = []

    def verify_and_redact(publish, digest):
        proof = default_machine_promotion_verifier(publish, digest)
        verified.append(proof)
        assert carrier.candidates.redact_content(content_hashes={candidate.content_hash}) == 1
        return proof

    carrier.service.runtime = replace(carrier.runtime, promotion_verifier=verify_and_redact)
    result = carrier.service.run_pending()

    assert len(verified) == 1
    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert len(carrier.backend.prompts) == 1
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills
    assert not list(carrier.store.learned_root.glob("*/SKILL.md"))


def test_deleted_canonical_store_is_rejected_before_model_call(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    shutil.rmtree(carrier.candidates.path.parent)

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_PROPOSAL_SOURCE_MISSING")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


@pytest.mark.parametrize(
    "failure",
    [OSError("simulated I/O error"), CandidateStoreCorruptError("simulated source read failure")],
    ids=["io-error", "corrupt-store"],
)
def test_unreadable_source_defers_intake_and_preserves_request_until_recovery(carrier, failure) -> None:
    candidate = _observe(carrier.candidates)
    with patch.object(CandidateService, "get", side_effect=failure):
        intake = carrier.service.enqueue_lesson_candidates([candidate])
    assert intake["deferred"][0]["code"] == "SKILL_PROPOSAL_SOURCE_UNREADABLE"
    assert not intake["queued"] and not intake["refused"]
    assert not carrier.store.pending_requests()

    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    path = carrier.store.pending_requests()[0]
    before = path.read_bytes()
    with patch.object(CandidateService, "get", side_effect=failure):
        result = carrier.service.run_pending()

    assert (result.status, result.code) == ("failed", "SKILL_PROPOSAL_SOURCE_UNREADABLE")
    assert carrier.store.pending_requests() == [path]
    assert path.read_bytes() == before
    assert not carrier.backend.prompts and not carrier.store.load_registry().skills
    recovered = carrier.service.run_pending()
    assert recovered.status == "published"
    assert len(carrier.backend.prompts) == 1
    assert not carrier.store.pending_requests()


def test_candidate_content_hash_is_rechecked_before_model_call(carrier) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    path = carrier.store.pending_requests()[0]
    request = carrier.store.read_request(path)
    request["candidate_content_hash"] = "0" * 64
    carrier.store.rewrite_request(path, request)

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills


def test_promotion_request_preserves_procedure_through_real_consumer(tmp_path: Path) -> None:
    owner, promotion, _conv, _thread, candidates, _lt, lessons, hot = _setup_owner(tmp_path)
    marker = "PROCEDURE_SENTINEL_731942"
    content = (
        "# 发布检查步骤\n## 适用场景\n执行版本发布与静态检查前。\n## 执行步骤\n"
        f"1. 检查 {marker}；\n2. 运行 `ruff check agent_py_agent`；\n3. 提交前确认文件白名单与状态。"
    )
    candidate = _observe_tool_lesson(candidates, content=content, subject_key="deploy.procedure")

    delegated = promotion.promote(candidate.candidate_id, automatic=True)

    assert (delegated.status, delegated.reason_code) == ("withdrawn", "DELEGATED_TO_SKILL_CARRIER")
    store = SkillLearningStore(owner / "data" / "skill_learning", owner / "skills")
    pending = store.pending_requests()
    assert len(pending) == 1
    request = store.read_request(pending[0])
    assert request["schema_version"] == REQUEST_SCHEMA_VERSION
    assert request["candidate_id"] == candidate.candidate_id
    assert request["candidate_content_hash"] == candidate.content_hash
    assert request["request_key"] == request_key(
        json.dumps(["subagent_lesson", candidate.candidate_id, candidate.content_hash], ensure_ascii=False)
    )
    assert request["tool_rounds"] is None
    assert request["tool_calls_total"] == len(candidate.source_tool_refs)
    assert request["tool_trace"] == [{"evidence_summary": "run_command"}]
    assert marker in request["user_prompt"] and marker in request["final_response"]
    assert not {"task", "result", "tool_summary", "source_candidate_id"} & request.keys()

    backend = _FakeBackend(_output())
    learning = SkillLearningService(
        store,
        SkillLearningSettings(autonomous_verified=True),
        SkillLearningRuntime(
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(entries=()),
            owner_id="owner-w8",
            deployment_root=tmp_path / "deployment",
        ),
    )
    result = learning.run_pending()

    assert result.status == "published"
    assert len(backend.prompts) == 1 and marker in backend.prompts[0]
    assert "ruff check agent_py_agent" in backend.prompts[0]
    assert not store.pending_requests() and len(store.load_registry().skills) == 1
    canonical = candidates.get(candidate.candidate_id)
    assert (canonical.status, canonical.review_note) == ("withdrawn", "DELEGATED_TO_SKILL_CARRIER")
    assert lessons.list() == [] and hot.list() == []
    assert not (owner / "data" / "skill_proposals").exists()


@pytest.mark.parametrize("scenario", ["Review the procedure", ""])
def test_shared_builder_is_bounded_and_preserves_recorded_evidence(monkeypatch, scenario: str) -> None:
    from agent_py_agent.agent.capability import skill_learning_request as requests

    monkeypatch.setattr(requests, "utc_now", lambda: "fixed-time")
    candidate = SimpleNamespace(
        candidate_id=" candidate-1 ",
        content_hash="a" * 64,
        scope={"scope_type": "project", "scope_key": "project:test", "applies_when": scenario,
               "password": "fixture-value"},
        source_task_ids=("", "task-first", "task-second"),
        source_run_ids=("", "run-first", "run-second"),
        source_tool_refs=({"tool": "read_file"}, {"operation_id": "op-first"}, {}, "ignored"),
        subject_key=" procedure ",
        content="Procedure details " + "x" * requests.TEXT_LIMIT_CHARS,
        learning_source={"can_learn": True, "password": "fixture-value"},
    )
    with (
        patch.object(Path, "open", side_effect=AssertionError("unexpected file read")),
        patch.object(CandidateService, "get", side_effect=AssertionError("unexpected candidate read")),
    ):
        request = requests.lesson_candidate_request(candidate)

    assert request["candidate_id"] == "candidate-1"
    assert request["request_key"] == request_key(
        json.dumps(["subagent_lesson", "candidate-1", candidate.content_hash], ensure_ascii=False)
    )
    assert (request["run_id"], request["task_id"], request["root_task_id"]) == (
        "run-first", "task-first", "task-first"
    )
    assert request["request_id"] == request["thread_id"] == request["trajectory_digest"] == ""
    assert request["tool_rounds"] is None and request["tool_calls_total"] == 3
    assert request["tool_trace"] == [{"evidence_summary": "read_file, op-first"}]
    assert request["trajectory_fingerprint"] == request["candidate_content_hash"] == candidate.content_hash
    assert request["created_at"] == "fixed-time" and request["attempts"] == 0
    assert request["used_skill_ids"] == [] and request["source"] == "subagent_lesson"
    assert request["schema_version"] == REQUEST_SCHEMA_VERSION
    assert len(request["user_prompt"]) <= requests.TEXT_LIMIT_CHARS
    assert len(request["final_response"]) == requests.TEXT_LIMIT_CHARS
    assert request["final_response"].endswith("…")
    if scenario:
        assert request["user_prompt"] == scenario
    else:
        assert request["user_prompt"].startswith("procedure: Procedure details")
        assert request["user_prompt"].endswith("…")
    assert request["lesson_scope"]["password"] == request["learning_source"]["password"] == "<redacted>"
    assert candidate.scope["password"] == candidate.learning_source["password"] == "fixture-value"
    assert not {"task", "result", "tool_summary", "source_candidate_id"} & request.keys()


def test_shared_builder_defaults_do_not_invent_tool_activity() -> None:
    from agent_py_agent.agent.capability.skill_learning_request import lesson_candidate_request

    request = lesson_candidate_request(SimpleNamespace(candidate_id="candidate-empty"))

    assert request["user_prompt"] == "candidate-empty"
    assert request["final_response"] == request["candidate_content_hash"] == ""
    assert request["tool_rounds"] is None
    assert request["tool_calls_total"] == 0 and request["tool_trace"] == []
    assert request["lesson_scope"] == request["learning_source"] == {}
    assert request["run_id"] == request["task_id"] == request["root_task_id"] == ""


@pytest.mark.parametrize("change", ["excluded", "withdrawn"])
def test_later_exclusion_or_unrelated_withdrawal_is_rejected(carrier, change: str) -> None:
    candidate = _observe(carrier.candidates)
    assert len(carrier.service.enqueue_lesson_candidates([candidate])["queued"]) == 1
    if change == "excluded":
        assert carrier.candidates.mark_promotion_excluded(
            [candidate.candidate_id], reason="source-excluded"
        ) == 1
    else:
        carrier.candidates.transition(
            candidate, "withdrawn", reviewer="test-reviewer", review_note="withdrawn-by-review"
        )

    result = carrier.service.run_pending()

    assert (result.status, result.code) == ("rejected", "SKILL_CARRIER_INELIGIBLE")
    assert not carrier.backend.prompts
    assert not carrier.store.pending_requests() and not carrier.store.load_registry().skills
