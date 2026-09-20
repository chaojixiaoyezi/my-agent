from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemorySubjectConflict
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
    VerifiedToolEvidence,
)


class _ToolVerifier:
    def __init__(
        self,
        successful: bool,
        *,
        terminal_status: str = "",
        effect_outcome: str = "",
        canonical_ref: dict[str, object] | None = None,
        parameters: dict[str, object] | None = None,
    ) -> None:
        self.successful = successful
        self.terminal_status = terminal_status or (
            "succeeded" if successful else "failed"
        )
        self.effect_outcome = effect_outcome or (
            "completed" if successful else "not_started"
        )
        self.canonical_ref = dict(canonical_ref or {})
        self.parameters = dict(parameters or {})

    def verify(self, ref: dict[str, object]):
        return VerifiedToolEvidence(
            successful=self.successful,
            terminal_status=self.terminal_status,
            canonical_ref={**dict(ref), **self.canonical_ref},
            effect_outcome=self.effect_outcome,
            parameters=self.parameters,
        )


def _runtime(
    tmp_path: Path,
    *,
    tool_successful: bool = False,
    tool_verifier: object | None = None,
):
    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    thread = conversations.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    long_term = JsonlMemory(
        owner / "memory" / "long_term" / "memory.jsonl",
        ops_path=owner / "memory" / "ops.jsonl",
    )
    lessons = LessonRepository(
        owner / "memory" / "lessons",
        owner / "memory" / "routing" / "INDEX.md",
    )
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=long_term,
            persona=PersonaRepository(
                owner_home=owner,
                soul_path=owner / "SOUL.md",
                user_path=owner / "USER.md",
                agents_path=owner / "AGENTS.md",
            ),
            lessons=lessons,
            hot=HotRuleRepository(owner / "memory-hot.md"),
            message_verifier=ConversationMessageEvidenceVerifier(conversations),
            tool_verifier=tool_verifier or _ToolVerifier(tool_successful),
        ),
    )
    return service, conversations, thread


def _explicit(
    service: MemoryPromotionService,
    conversations: ConversationStore,
    thread: object,
    *,
    content: str,
    scope_type: str = "personal",
    scope_key: str = "personal",
    candidate_type: str = "long_term_fact",
    subject_key: str = "device.os",
    promotion_target: str = "long_term",
    proposed_action: str = "add",
    target_entry_id: str = "",
    duplicate_generic_evidence: bool = False,
    source_tool_refs: tuple[dict[str, object], ...] = (),
):
    message = conversations.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": content,
            "now": 11.0,
        }
    )
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    message_ref = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": content_hash,
        "quote": content,
    }
    return service.candidates.observe(
        CandidateObservation(
            candidate_type=candidate_type,
            content=content,
            subject_key=subject_key,
            scope=MemoryScope(scope_type, scope_key),
            origin="user_explicit",
            evidence_refs=(message_ref,) if duplicate_generic_evidence else (),
            source_message_refs=(message_ref,),
            source_tool_refs=source_tool_refs,
            proposed_action=proposed_action,
            target_entry_id=target_entry_id,
            promotion_target=promotion_target,
            promotion_mode="auto_eligible",
            confidence=0.99,
        )
    )


def test_user_explicit_exact_message_can_auto_promote_new_long_term_fact(tmp_path: Path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service,
        conversations,
        thread,
        content="个人电脑使用 macOS。",
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is True
    records = service.long_term.all()
    assert len(records) == 1
    assert records[0].attributes["scope_key"] == "personal"
    assert service.candidates.get(candidate.candidate_id).status == "promoted"


def test_long_term_promotion_deduplicates_overlapping_evidence_refs(tmp_path: Path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service,
        conversations,
        thread,
        content="个人电脑使用 macOS。",
        duplicate_generic_evidence=True,
    )

    assert service.promote(candidate.candidate_id, automatic=True).promoted

    evidence_refs = service.long_term.all()[0].attributes["evidence_refs"]
    assert len(evidence_refs) == 1
    assert evidence_refs[0]["message_id"] == candidate.source_message_refs[0]["message_id"]


def test_newer_same_subject_scope_does_not_silently_replace(tmp_path: Path):
    service, conversations, thread = _runtime(tmp_path)
    first = _explicit(service, conversations, thread, content="个人电脑使用 macOS。")
    assert service.promote(first.candidate_id, automatic=True).promoted
    second = _explicit(service, conversations, thread, content="个人电脑使用 Windows。")

    result = service.promote(second.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.status == "blocked_conflict"
    assert [record.content for record in service.long_term.all()] == ["个人电脑使用 macOS。"]


def test_same_subject_different_scope_can_coexist(tmp_path: Path):
    service, conversations, thread = _runtime(tmp_path)
    personal = _explicit(service, conversations, thread, content="个人电脑使用 macOS。")
    company = _explicit(
        service,
        conversations,
        thread,
        content="公司服务器使用 Linux。",
        scope_type="company",
        scope_key="company:acme",
    )

    assert service.promote(personal.candidate_id, automatic=True).promoted
    assert service.promote(company.candidate_id, automatic=True).promoted
    assert {record.attributes["scope_key"] for record in service.long_term.all()} == {
        "personal",
        "company:acme",
    }


def test_company_requires_typed_id_and_is_recallable(tmp_path: Path):
    """company 合同（方案 A）：必须 company:<id>，写→晋升→search_scoped 闭环可召回。"""
    from agent_py_agent.agent.memory_store.recall import (
        MemoryRecallScope,
        long_term_record_matches_scope,
    )

    service, conversations, thread = _runtime(tmp_path)
    company = _explicit(
        service,
        conversations,
        thread,
        content="公司服务器使用 Linux。",
        scope_type="company",
        scope_key="company:acme",
        subject_key="company.servers",
    )

    assert service.promote(company.candidate_id, automatic=True).promoted
    record = service.long_term.all()[0]
    assert record.attributes["scope_type"] == "company"
    assert record.attributes["scope_key"] == "company:acme"
    with_company = MemoryRecallScope.from_runtime(
        task_attributes={"company_id": "company:acme"},
    )
    assert long_term_record_matches_scope(record, with_company)
    assert not long_term_record_matches_scope(record, MemoryRecallScope.from_runtime())
    # search_scoped 闭环：真实查询按 scope predicate 过滤，有 company_id 命中、无则不命中
    hits = service.long_term.search_scoped(
        "公司服务器",
        top_k=5,
        predicate=lambda item: long_term_record_matches_scope(item, with_company),
    )
    assert hits and hits[0].entry_id == record.entry_id
    without_company = service.long_term.search_scoped(
        "公司服务器",
        top_k=5,
        predicate=lambda item: long_term_record_matches_scope(
            item, MemoryRecallScope.from_runtime()
        ),
    )
    assert not any(item.entry_id == record.entry_id for item in without_company)


def test_bare_company_scope_rejected_at_observe_entry(tmp_path: Path):
    """裸 company/company 可写不可召回 → observe 入口直接拒写。"""
    service, conversations, thread = _runtime(tmp_path)
    with pytest.raises(ValueError):
        _explicit(
            service,
            conversations,
            thread,
            content="公司服务器使用 Linux。",
            scope_type="company",
            scope_key="company",
        )


def test_normalized_equivalent_replay_reuses_original_entry(
    tmp_path: Path,
):
    """规范化等价正文（多空格/全半角）重放必须复用原 entry，不得 blocked_conflict。"""
    service, conversations, thread = _runtime(tmp_path)
    first = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 UTC。",
        scope_type="project",
        scope_key="project:alpha",
        subject_key="environment.timezone",
    )
    replay = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 ​ＵＴＣ。",  # 零宽 + 全角字母，NFKC/casefold 后与原正文等价
        scope_type="project",
        scope_key="project:alpha",
        subject_key="environment.timezone",
    )

    original = service.promote(first.candidate_id, automatic=True)
    result = service.promote(replay.candidate_id, automatic=True)

    assert original.promoted is True
    assert result.promoted is True
    assert result.promotion_ref == original.promotion_ref
    assert result.status != "blocked_conflict"
    assert service.candidates.get(replay.candidate_id).status == "promoted"
    assert len(service.long_term.all()) == 1


def test_same_content_in_different_scope_promotes_as_distinct_fact(tmp_path: Path):
    service, conversations, thread = _runtime(tmp_path)
    alpha = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 UTC。",
        scope_type="project",
        scope_key="project:alpha",
        subject_key="environment.timezone",
    )
    beta = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 UTC。",
        scope_type="project",
        scope_key="project:beta",
        subject_key="environment.timezone",
    )

    first = service.promote(alpha.candidate_id, automatic=True)
    second = service.promote(beta.candidate_id, automatic=True)

    assert first.promoted is True
    assert second.promoted is True
    assert first.promotion_ref != second.promotion_ref
    assert {record.attributes["scope_key"] for record in service.long_term.all()} == {
        "project:alpha",
        "project:beta",
    }
    assert service.candidates.get(beta.candidate_id).status == "promoted"


def test_empty_long_term_add_commit_becomes_conflict_not_stop_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 UTC。",
        scope_type="project",
        scope_key="project:alpha",
        subject_key="environment.timezone",
    )
    monkeypatch.setattr(service.long_term, "apply_batch", lambda _operations: [])

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.status == "blocked_conflict"
    assert result.reason_code == "LONG_TERM_ADD_IDEMPOTENCY_IDENTITY_MISMATCH"
    assert service.candidates.get(candidate.candidate_id).status == "blocked_conflict"


def test_approved_candidate_with_noncanonical_scope_blocks_at_promotion_boundary(
    tmp_path: Path,
):
    """历史落账的脏 scope 候选（如 personal/local）重放必须 blocked，不落任何字节。"""
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service,
        conversations,
        thread,
        content="测试环境使用 UTC。",
        scope_type="personal",
        scope_key="personal",
        subject_key="environment.timezone",
    )
    service.candidates.transition(
        candidate.candidate_id,
        "approved",
        reviewer="test-reviewer",
        review_note="历史审核通过。",
    )
    # 模拟旧版本落账的脏 scope：直接改写候选账本文件（绕过观察入口校验）。
    path = tmp_path / "owner" / "memory" / "candidates.jsonl"
    rewritten: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("candidate_id") == candidate.candidate_id:
            row["scope"]["scope_key"] = "local"
        rewritten.append(row)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in rewritten) + "\n",
        encoding="utf-8",
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.status == "blocked_conflict"
    assert result.reason_code == "SCOPE_NOT_RECALLABLE"
    assert service.candidates.get(candidate.candidate_id).status == "blocked_conflict"
    assert service.long_term.all() == []


def test_model_inferred_never_auto_promotes(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="模型猜测用户喜欢蓝色。",
            subject_key="preference.color",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    # 权限闸先于权威块:model_inferred 一律 manual_required,automatic 路径直接拒绝。
    assert result.reason_code == "PROMOTION_MANUAL_REQUIRED"
    assert result.status == "pending_review"
    assert service.long_term.all() == []


def test_automatic_promotion_assigns_authority_then_still_verifies_real_evidence(
    tmp_path: Path,
) -> None:
    """宿主自动赋权不等于证据通过；不存在的消息仍会被正式 verifier 阻断。"""
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="个人电脑使用 macOS。",
            subject_key="device.personal.os",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=({"message_id": "missing-message"},),
            promotion_target="long_term",
        )
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert candidate.promotion_mode == "auto_eligible"
    assert result.reason_code == "USER_MESSAGE_EVIDENCE_INVALID"
    assert result.status == "blocked_missing_evidence"
    assert service.candidates.get(candidate.candidate_id).status == "blocked_missing_evidence"
    assert service.long_term.all() == []


def test_model_inferred_cannot_become_formal_fact_even_after_review(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="模型猜测用户喜欢蓝色。",
            subject_key="preference.color",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
            confidence=1.0,
        )
    )
    service.review(candidate.candidate_id, approved=True, reviewer="admin")

    result = service.promote(candidate.candidate_id, reviewer="admin")

    assert result.promoted is False
    assert result.reason_code == "MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN"
    assert service.candidates.get(candidate.candidate_id).status == "approved"
    assert service.long_term.all() == []


def _lesson(
    service: MemoryPromotionService,
    *,
    content: str,
    runs: tuple[str, ...],
    tasks: tuple[str, ...] = (),
    origin: str = "model_inferred",
    proposed_action: str = "add",
    target_entry_id: str = "",
    conflicts_with: tuple[str, ...] = (),
):
    """对同一 lesson 语义观察多次(每次不同 run/task 来源),模拟跨任务重复出现合并。

    occurrence_count 由 CandidateService 按独立 observation_key 累计
    (stable_observation_key 含 candidate_id + run/task/refs),同一候选不同来源
    → 合并为一条 occurrence_count=N 的候选,这正是"跨 run 重复教训"的真实路径。
    """
    candidate = None
    for index, run in enumerate(runs):
        candidate = service.candidates.observe(
            CandidateObservation(
                candidate_type="lesson",
                content=content,
                subject_key="lesson.task_x_backup_first",
                scope=MemoryScope("personal", "personal"),
                origin=origin,
                promotion_target="lesson",
                source_run_ids=(run,),
                source_task_ids=(f"task-{index + 1}",) if tasks else (),
                proposed_action=proposed_action,
                target_entry_id=target_entry_id,
                conflicts_with=conflicts_with,
            )
        )
    assert candidate is not None
    return candidate


def test_model_inferred_lesson_autonomously_promotes_with_repeated_evidence(
    tmp_path: Path,
):
    """跨任务重复证据达到门槛后，lesson 由 owner Agent 自主晋升。"""
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = _lesson(
        service,
        content="做 X 任务应先备份再修改,否则数据会丢。",
        runs=("run-1", "run-2"),
        tasks=("task-1", "task-2"),
    )
    assert candidate.occurrence_count == 2  # 两次独立观察已合并

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is True
    assert result.reason_code == "PROMOTED"
    lessons = service.lessons.list()
    assert len(lessons) == 1
    assert lessons[0].evidence_groups == (
        "run:run-1",
        "run:run-2",
        "task:task-1",
        "task:task-2",
    )


def test_model_inferred_single_occurrence_lesson_waits_for_more_evidence(tmp_path: Path):
    """lesson 单次出现只是不满足客观门槛，不要求用户审核。"""
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = _lesson(service, content="做 X 任务应先备份再修改。", runs=("run-1",))

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "PROMOTION_THRESHOLD_NOT_MET"
    assert service.lessons.list() == []


def test_lesson_automatic_path_blocks_mutation_and_conflict_candidates(tmp_path: Path):
    """lesson 一律 manual:remove 动作/冲突引用的 lesson 候选 automatic 均被权限闸拦。"""
    service, _conversations, _thread = _runtime(tmp_path)
    mutation = _lesson(
        service,
        content="做 X 任务应先备份。",
        runs=("run-1", "run-2"),
        proposed_action="remove",
        target_entry_id="lesson-abc",
    )

    result = service.promote(mutation.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "PROMOTION_MANUAL_REQUIRED"
    assert service.lessons.list() == []


def test_lesson_automatic_path_rejects_conflicting_candidate(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path)
    conflicting = _lesson(
        service,
        content="做 X 任务不应备份。",
        runs=("run-1", "run-2"),
        conflicts_with=("candidate-other",),
    )

    result = service.promote(conflicting.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "AUTO_POLICY_CONFLICT_UNRESOLVED"
    assert service.lessons.list() == []


def test_failed_tool_evidence_is_blocked(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path, tool_successful=False)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="project",
            content="项目构建产物位于 dist/app。",
            subject_key="project.output",
            scope=MemoryScope("project", "project:demo"),
            origin="tool_verified",
            source_tool_refs=(
                {"operation_id": "op-1", "run_id": "run-1", "status": "failed"},
            ),
            promotion_target="long_term",
            promotion_mode="auto_eligible",
        )
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.status == "blocked_missing_evidence"
    assert service.long_term.all() == []


@pytest.mark.parametrize(
    ("successful", "terminal_status", "effect_outcome"),
    [
        (False, "failed", "not_started"),
        (False, "timeout", "unknown"),
        (False, "cancelled", "not_started"),
        (True, "succeeded", "unknown"),
    ],
)
def test_non_success_or_unknown_tool_effect_never_promotes(
    tmp_path: Path,
    successful: bool,
    terminal_status: str,
    effect_outcome: str,
) -> None:
    verifier = _ToolVerifier(
        successful,
        terminal_status=terminal_status,
        effect_outcome=effect_outcome,
    )
    service, _conversations, _thread = _runtime(
        tmp_path,
        tool_verifier=verifier,
    )
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="project",
            content="项目构建产物位于 dist/app。",
            subject_key="project.output",
            scope=MemoryScope("project", "project:demo"),
            origin="tool_verified",
            source_tool_refs=(
                {"owner_id": "local/main", "run_id": "run-1", "operation_id": "op-1"},
            ),
            promotion_target="long_term",
            promotion_mode="auto_eligible",
        )
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.reason_code == "TOOL_EVIDENCE_NOT_SUCCEEDED"
    assert result.status == "blocked_missing_evidence"
    assert service.long_term.all() == []


def test_confidence_cannot_replace_user_message_evidence(tmp_path: Path) -> None:
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="用户喜欢蓝色。",
            subject_key="preference.color",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            confidence=1.0,
            promotion_target="long_term",
            promotion_mode="auto_eligible",
        )
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.reason_code == "USER_MESSAGE_EVIDENCE_MISSING"
    assert service.long_term.all() == []


@pytest.mark.parametrize("action", ["replace", "merge"])
def test_reviewed_replace_and_merge_require_exact_target_entry_id(
    tmp_path: Path,
    action: str,
) -> None:
    service, conversations, thread = _runtime(tmp_path)
    original = _explicit(service, conversations, thread, content="个人电脑使用 macOS。")
    assert service.promote(original.candidate_id, automatic=True).promoted
    target = service.long_term.all()[0]
    replacement = _explicit(
        service,
        conversations,
        thread,
        content="个人电脑使用 Windows。",
        proposed_action=action,
        target_entry_id=target.entry_id,
    )
    service.review(replacement.candidate_id, approved=True, reviewer="admin")

    result = service.promote(replacement.candidate_id, reviewer="admin")

    assert result.promoted is True
    assert [(record.entry_id, record.content, record.version) for record in service.long_term.all()] == [
        (target.entry_id, "个人电脑使用 Windows。", 2)
    ]


def test_reviewed_remove_requires_exact_same_subject_scope_target(tmp_path: Path) -> None:
    service, conversations, thread = _runtime(tmp_path)
    original = _explicit(service, conversations, thread, content="个人电脑使用 macOS。")
    assert service.promote(original.candidate_id, automatic=True).promoted
    target = service.long_term.all()[0]
    removal = _explicit(
        service,
        conversations,
        thread,
        content="个人电脑使用 macOS。",
        proposed_action="remove",
        target_entry_id=target.entry_id,
    )
    service.review(removal.candidate_id, approved=True, reviewer="admin")

    result = service.promote(removal.candidate_id, reviewer="admin")

    assert result.promoted is True
    assert result.promotion_ref.endswith(target.entry_id + ":removed")
    assert service.long_term.all() == []


def test_wrong_replace_target_is_blocked_instead_of_using_newer_time(tmp_path: Path) -> None:
    service, conversations, thread = _runtime(tmp_path)
    original = _explicit(service, conversations, thread, content="个人电脑使用 macOS。")
    assert service.promote(original.candidate_id, automatic=True).promoted
    replacement = _explicit(
        service,
        conversations,
        thread,
        content="个人电脑使用 Windows。",
        proposed_action="replace",
        target_entry_id="memory-missing",
    )
    service.review(replacement.candidate_id, approved=True, reviewer="admin")

    result = service.promote(replacement.candidate_id, reviewer="admin")

    assert result.promoted is False
    assert result.status == "blocked_conflict"
    assert result.reason_code == "TARGET_ENTRY_NOT_IN_SAME_SUBJECT_SCOPE"
    assert [record.content for record in service.long_term.all()] == ["个人电脑使用 macOS。"]


def test_user_profile_requires_explicit_message_and_rejects_temporary_scope(
    tmp_path: Path,
) -> None:
    service, conversations, thread = _runtime(tmp_path)
    stable = _explicit(
        service,
        conversations,
        thread,
        content="我通常喜欢简洁回答。",
        candidate_type="user_preference",
        subject_key="preference.response.style",
        promotion_target="user",
    )
    service.review(stable.candidate_id, approved=True, reviewer="admin")
    assert service.promote(stable.candidate_id, reviewer="admin").promoted
    assert "[personal:personal] 我通常喜欢简洁回答。" in service.persona.path_for(
        "user"
    ).read_text(encoding="utf-8")

    temporary = _explicit(
        service,
        conversations,
        thread,
        content="这一次架构问题请详细解释。",
        scope_type="temporary",
        scope_key="temporary:turn-1",
        candidate_type="user_preference",
        subject_key="preference.response.temporary-detail",
        promotion_target="user",
    )
    service.review(temporary.candidate_id, approved=True, reviewer="admin")
    result = service.promote(temporary.candidate_id, reviewer="admin")
    assert result.promoted is False
    assert result.reason_code == "TEMPORARY_USER_PROFILE_FORBIDDEN"
    assert "这一次架构问题请详细解释" not in service.persona.path_for(
        "user"
    ).read_text(encoding="utf-8")


def test_persona_promotion_adopts_exact_prior_update_persona_entry(tmp_path: Path) -> None:
    direct_content = "故障复盘先写一句大白话结论，再列证据、原因、修复和影响。"
    target_entry_id = persona_entry_id("user", direct_content)
    verifier = _ToolVerifier(
        True,
        terminal_status="succeeded",
        effect_outcome="confirmed",
        canonical_ref={"tool": "update_persona"},
        parameters={
            "target": "user",
            "content": direct_content,
        },
    )
    service, conversations, thread = _runtime(tmp_path, tool_verifier=verifier)
    service.persona.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content=direct_content,
            confirmed=True,
            source="update_persona:test",
        )
    )
    candidate = _explicit(
        service,
        conversations,
        thread,
        content="用户希望故障复盘先给大白话结论，再展示证据、原因、修复和影响。",
        candidate_type="user_preference",
        subject_key="preference.postmortem.format",
        promotion_target="user",
        target_entry_id=target_entry_id,
        source_tool_refs=(
            {
                "owner_id": "owner-1",
                "run_id": "run-1",
                "tool_call_id": "call-1",
                "tool_name": "update_persona",
            },
        ),
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is True
    assert result.promotion_ref == f"USER.md#{target_entry_id}"
    entries = service.persona.list_entries("user")["entries"]
    assert isinstance(entries, list)
    assert entries == [{"entry_id": target_entry_id, "content": direct_content}]


def test_local_tool_verifier_recovers_exact_legacy_call_from_owner_index(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner"
    parameters = {
        "target": "user",
        "content": "回答先给结论。",
    }
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=owner,
            tool="update_persona",
            call_id="call-legacy-1",
            output='{"ok":true}',
            ok=True,
            run_id="run-legacy-1",
            task_id="task-legacy-1",
            parameters=parameters,
            result_envelope={
                "tool_execution": {"handler_executed": True, "duration_ms": 3},
                "tool_operation": {
                    "operation_id": "operation-legacy-1",
                    "status": "succeeded",
                },
            },
        )
    )
    verifier = LocalStoreToolEvidenceVerifier(
        object(),
        owner_id="owner-1",
        owner_root=owner,
    )

    verified = verifier.verify(
        {
            "owner_id": "owner-1",
            "run_id": "run-legacy-1",
            "tool_call_id": "call-legacy-1",
            "tool_name": "update_persona",
        }
    )

    assert verified is not None
    assert verified.successful is True
    assert verified.terminal_status == "succeeded"
    assert verified.canonical_ref["operation_id"] == "operation-legacy-1"
    assert verified.canonical_ref["source"] == "tool_output_index"
    assert verified.parameters == parameters


def test_local_tool_verifier_migrates_legacy_done_ledger_status() -> None:
    record = SimpleNamespace(
        run_id="run-ledger-1",
        operation_id="operation-ledger-1",
        tool="write_file",
        status="done",
        parameters={"path": "output/report.md"},
    )
    store = SimpleNamespace(
        get_runtime_gate_ledger=lambda **_kwargs: record,
    )
    verifier = LocalStoreToolEvidenceVerifier(store, owner_id="owner-1")

    verified = verifier.verify(
        {
            "owner_id": "owner-1",
            "run_id": "run-ledger-1",
            "operation_id": "operation-ledger-1",
            "tool_call_id": "call-ledger-1",
        }
    )

    assert verified is not None
    assert verified.successful is True
    assert verified.terminal_status == "succeeded"
    assert verified.canonical_ref["status"] == "succeeded"
    assert verified.parameters == {"path": "output/report.md"}


def test_soul_requires_explicit_confirmation(tmp_path: Path) -> None:
    service, conversations, thread = _runtime(tmp_path)
    content = "AI 人格保持耐心和诚实。"
    candidate = _explicit(
        service,
        conversations,
        thread,
        content=content,
        candidate_type="soul_change",
        subject_key="persona.soul.rule",
        promotion_target="soul",
    )
    service.review(candidate.candidate_id, approved=True, reviewer="admin")

    with pytest.raises(ValueError, match="explicit user confirmation"):
        service.promote(candidate.candidate_id, reviewer="admin", confirmed=False)
    result = service.promote(candidate.candidate_id, reviewer="admin", confirmed=True)

    assert result.promoted is True
    target_path = service.persona.path_for("soul")
    assert content in target_path.read_text(encoding="utf-8")


def test_agents_working_agreement_is_autonomous(tmp_path: Path) -> None:
    service, conversations, thread = _runtime(tmp_path)
    content = "长期合作时先核对真实代码。"
    candidate = _explicit(
        service,
        conversations,
        thread,
        content=content,
        candidate_type="working_agreement",
        subject_key="persona.agents.rule",
        promotion_target="agents",
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is True
    assert content in service.persona.path_for("agents").read_text(encoding="utf-8")


@pytest.mark.parametrize("origin", ["subagent_finding", "subagent_lesson"])
def test_subagent_cannot_bypass_persona_repository_authority(
    tmp_path: Path,
    origin: str,
) -> None:
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="user_preference",
            content="用户偏好所有回答都展开十页。",
            subject_key="preference.response.length",
            scope=MemoryScope("personal", "personal"),
            origin=origin,
            evidence_refs=({"ref_id": "subagent-run-1"},),
            source_run_ids=("subagent-run-1",),
            promotion_target="user",
        )
    )
    service.review(candidate.candidate_id, approved=True, reviewer="admin")

    result = service.promote(candidate.candidate_id, reviewer="admin")

    assert result.promoted is False
    assert result.reason_code == "PERSONA_USER_EXPLICIT_REQUIRED"
    assert "十页" not in service.persona.path_for("user").read_text(encoding="utf-8")


def test_lesson_and_hot_have_one_formal_body_and_thresholds(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path)
    lesson_observation = CandidateObservation(
        candidate_type="lesson",
        content="修改共享状态前，先用精确 ID 和版本做 CAS。",
        subject_key="concurrency.cas",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="subagent_lesson",
        source_task_ids=("task-1",),
        promotion_target="lesson",
        observation_id="lesson-task-1",
    )
    lesson = service.candidates.observe(lesson_observation)
    service.candidates.observe(
        CandidateObservation(
            **{
                **lesson_observation.__dict__,
                "source_task_ids": ("task-2",),
                "observation_id": "lesson-task-2",
            }
        )
    )
    service.review(lesson.candidate_id, approved=True, reviewer="admin")
    promoted_lesson = service.promote(lesson.candidate_id, reviewer="admin")
    lesson_id = promoted_lesson.promotion_ref.rsplit("#", 1)[1]

    hot_observation = CandidateObservation(
        candidate_type="hot_rule",
        content="共享状态修改必须使用精确 ID 和版本 CAS。",
        subject_key="hot.concurrency.cas",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="reviewed",
        source_task_ids=("task-1",),
        promotion_target="hot",
        target_entry_id=lesson_id,
        observation_id="hot-task-1",
    )
    hot = service.candidates.observe(hot_observation)
    for task_id in ("task-2", "task-3"):
        service.candidates.observe(
            CandidateObservation(
                **{
                    **hot_observation.__dict__,
                    "source_task_ids": (task_id,),
                    "observation_id": f"hot-{task_id}",
                }
            )
        )
    service.review(hot.candidate_id, approved=True, reviewer="admin")
    promoted_hot = service.promote(hot.candidate_id, reviewer="admin")

    assert promoted_lesson.promoted is True
    assert promoted_hot.promoted is True
    assert len(service.lessons.list()) == 1
    assert service.long_term.all() == []
    hot_text = service.hot.path.read_text(encoding="utf-8")
    assert "共享状态修改必须使用精确 ID 和版本 CAS" in hot_text
    assert "修改共享状态前，先用精确 ID 和版本做 CAS" not in hot_text
    assert lesson_id in hot_text


def test_unreviewed_or_single_subagent_experience_cannot_become_lesson(
    tmp_path: Path,
) -> None:
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="修改共享文件前先检查精确 diff。",
            subject_key="workflow.shared-diff",
            scope=MemoryScope("task_class", "task_class:engineering"),
            origin="subagent_lesson",
            source_task_ids=("task-1",),
            promotion_target="lesson",
            observation_id="lesson-single-task",
        )
    )

    unreviewed = service.promote(candidate.candidate_id, reviewer="admin")
    assert unreviewed.promoted is False
    assert unreviewed.reason_code == "REVIEW_REQUIRED"

    service.review(candidate.candidate_id, approved=True, reviewer="admin")
    result = service.promote(candidate.candidate_id, reviewer="admin")
    assert result.promoted is False
    assert result.reason_code == "PROMOTION_THRESHOLD_NOT_MET"  # 单次出现:干净失败,不崩溃
    assert service.lessons.list() == []
    assert service.long_term.all() == []


def test_single_hot_observation_cannot_promote_and_exact_lesson_id_is_required(
    tmp_path: Path,
) -> None:
    service, _conversations, _thread = _runtime(tmp_path)
    lesson_observation = CandidateObservation(
        candidate_type="lesson",
        content="共享状态更新应使用精确标识和版本校验。",
        subject_key="concurrency.version-check",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="subagent_lesson",
        source_task_ids=("task-1",),
        promotion_target="lesson",
        observation_id="lesson-version-task-1",
    )
    lesson = service.candidates.observe(lesson_observation)
    service.candidates.observe(
        CandidateObservation(
            **{
                **lesson_observation.__dict__,
                "source_task_ids": ("task-2",),
                "observation_id": "lesson-version-task-2",
            }
        )
    )
    service.review(lesson.candidate_id, approved=True, reviewer="admin")
    lesson_id = service.promote(
        lesson.candidate_id,
        reviewer="admin",
    ).promotion_ref.rsplit("#", 1)[1]

    single = service.candidates.observe(
        CandidateObservation(
            candidate_type="hot_rule",
            content="共享状态更新必须做版本校验。",
            subject_key="hot.concurrency.version-check",
            scope=MemoryScope("task_class", "task_class:engineering"),
            origin="reviewed",
            source_task_ids=("task-1",),
            promotion_target="hot",
            target_entry_id=lesson_id,
            observation_id="hot-version-single",
        )
    )
    service.review(single.candidate_id, approved=True, reviewer="admin")
    result = service.promote(single.candidate_id, reviewer="admin")
    assert result.promoted is False
    assert result.reason_code == "PROMOTION_THRESHOLD_NOT_MET"  # HOT 单次观察:干净失败,不崩溃
    assert service.hot.list() == []

    wrong_target_observation = CandidateObservation(
        candidate_type="hot_rule",
        content="共享状态变更必须绑定正确教训。",
        subject_key="hot.concurrency.exact-target",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="reviewed",
        source_task_ids=("task-1",),
        promotion_target="hot",
        target_entry_id="lesson-does-not-exist",
        observation_id="hot-exact-task-1",
    )
    wrong_target = service.candidates.observe(wrong_target_observation)
    for task_id in ("task-2", "task-3"):
        service.candidates.observe(
            CandidateObservation(
                **{
                    **wrong_target_observation.__dict__,
                    "source_task_ids": (task_id,),
                    "observation_id": f"hot-exact-{task_id}",
                }
            )
        )
    service.review(wrong_target.candidate_id, approved=True, reviewer="admin")
    with pytest.raises(KeyError, match="lesson-does-not-exist"):
        service.promote(wrong_target.candidate_id, reviewer="admin")
    assert service.hot.list() == []


def test_routing_index_rebuild_is_byte_deterministic(tmp_path: Path) -> None:
    service, _conversations, _thread = _runtime(tmp_path)
    observation = CandidateObservation(
        candidate_type="lesson",
        content="先核对结构化状态，再判断是否恢复任务。",
        subject_key="recovery.structured-state",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="subagent_lesson",
        source_run_ids=("run-1",),
        promotion_target="lesson",
        observation_id="lesson-recovery-run-1",
    )
    candidate = service.candidates.observe(observation)
    service.candidates.observe(
        CandidateObservation(
            **{
                **observation.__dict__,
                "source_run_ids": ("run-2",),
                "observation_id": "lesson-recovery-run-2",
            }
        )
    )
    service.review(candidate.candidate_id, approved=True, reviewer="admin")
    service.promote(candidate.candidate_id, reviewer="admin")
    first = service.lessons.routing_index_path.read_bytes()

    service.lessons.rebuild_routing_index()
    second = service.lessons.routing_index_path.read_bytes()

    assert second == first
    assert b"my-agent-lesson-meta" not in second


@pytest.mark.parametrize("second_text", [
    "生产服务 prod-b 使用 8081 端口，监听所有网卡，保留访问日志。",
    "生产服务 prod-a 不使用 8080 端口，监听所有网卡，保留访问日志。",
])
def test_similar_add_preserves_distinct_subjects(tmp_path: Path, second_text):
    service, conversations, thread = _runtime(tmp_path)
    original = "生产服务 prod-a 使用 8080 端口，监听所有网卡，保留访问日志。"
    first = _explicit(service, conversations, thread, content=original, subject_key="prod-a")
    assert service.promote(first.candidate_id, automatic=True).promoted
    second = _explicit(service, conversations, thread, content=second_text, subject_key="prod-b")
    assert service.promote(second.candidate_id, automatic=True).promoted
    assert {r.content for r in service.long_term.all()} == {original, second_text}


def test_near_duplicate_add_does_not_rewrite_another_subject(tmp_path: Path):
    """相似措辞不是同一主体的结构化证据；新增不能暗中覆盖旧记忆。"""
    service, conversations, thread = _runtime(tmp_path)
    first = _explicit(
        service, conversations, thread,
        content="祥子买了两次车。",
        subject_key="xiangzi.car",
    )
    assert service.promote(first.candidate_id, automatic=True).promoted
    assert len(service.long_term.all()) == 1
    first_ref = service.long_term.all()[0].entry_id

    second = _explicit(
        service, conversations, thread,
        content="祥子买了两次车",
        subject_key="xiangzi.car-count",  # 不同 subject,模拟模型换主题记同一事实
    )
    result = service.promote(second.candidate_id, automatic=True)
    assert result.promoted
    records = service.long_term.all()
    assert len(records) == 2
    assert next(r for r in records if r.entry_id == first_ref).content == "祥子买了两次车。"
    assert first_ref not in result.promotion_ref


def test_distinct_facts_never_merged_by_content_similarity(tmp_path: Path):
    """写路径查重:不同事实(措辞变体/信息增量)不得误并。"""
    service, conversations, thread = _runtime(tmp_path)
    for content, subject in (
        ("祥子买过两次车", "xiangzi.car.used"),     # 过/了 措辞变体,信息等价但非逐字
        ("祥子买了两辆车", "xiangzi.car.two"),      # 两辆/两次 不同事实
        ("祥子买了车", "xiangzi.car.basic"),        # 信息增量
        ("今天天气很好", "weather.today"),           # 完全不同
    ):
        candidate = _explicit(
            service, conversations, thread,
            content=content, subject_key=subject,
        )
        assert service.promote(candidate.candidate_id, automatic=True).promoted
    assert len(service.long_term.all()) == 4


def test_keywords_en_written_into_long_term_attributes(tmp_path: Path):
    """写路径中英关键词:正文里的英文词面写进 keywords_en,供 BM25 跨语言命中。"""
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service, conversations, thread,
        content="祥子(Xiangzi)在北京买了两次车,花费 96 元。",
        subject_key="xiangzi.beijing.car",
    )
    assert service.promote(candidate.candidate_id, automatic=True).promoted
    record = service.long_term.all()[0]
    assert "xiangzi" in record.attributes["keywords_en"]
    assert "beijing" not in record.attributes["keywords_en"]  # 中文词不进英文关键词


def test_search_scoped_english_keyword_hits_chinese_memory(tmp_path: Path):
    """检索消费点:英文查询词命中含 keywords_en 的中文记忆(BM25 词面臂)。"""
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service, conversations, thread,
        content="祥子(Xiangzi)在北京买了两次车。",
        subject_key="xiangzi.beijing.car",
    )
    assert service.promote(candidate.candidate_id, automatic=True).promoted

    hits = service.long_term.search_scoped(
        "xiangzi",
        top_k=5,
        predicate=lambda record: True,
    )
    assert hits, "英文关键词必须让 BM25 命中中文记忆"
    assert "祥子" in hits[0].content


def test_old_task_alias_ledger_reused_by_new_project_candidate(tmp_path: Path):
    """1197 单一权威：旧 task:<id> 账本 + 新 project:<id> 候选同内容同 subject →
    复用原 entry_id，不生成第二条正式事实。"""
    service, conversations, thread = _runtime(tmp_path)
    content = "公司服务器统一使用 HTTPS 部署。"
    # 模拟旧账本：task:<id> scope 已有一条同内容事实（旧代码 era 持久化）
    old = service.long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "memory-old-task-ledger",
                "role": "system",
                "content": content,
                "kind": "fact",
                "tags": ["long_term_fact"],
                "attributes": {
                    "scope_type": "project",
                    "scope_key": "task:alpha",
                    "subject_key": "ops.deploy",
                },
                "source": "legacy",
            }
        ]
    )[0]
    assert old.entry_id == "memory-old-task-ledger"

    # 新候选：project:<id>（写侧合同归一后的唯一正式键）
    candidate = _explicit(
        service,
        conversations,
        thread,
        content=content,
        scope_type="project",
        scope_key="project:alpha",
        subject_key="ops.deploy",
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is True
    records = service.long_term.all()
    # 仍只有旧账本一条，新候选晋升复用它而非新增
    assert [record.entry_id for record in records] == ["memory-old-task-ledger"]
    assert records[0].attributes["scope_key"] == "task:alpha"
    assert service.candidates.get(candidate.candidate_id).promotion_ref.endswith(
        "memory-old-task-ledger"
    )


def test_storage_task_alias_ledger_deduped_by_project_add_same_content(tmp_path: Path):
    """1202 存储级：旧 task:<id> 账本 + 同 subject 同内容新 project:<id> add →
    复用旧 entry_id 不新增（_memory_identity_key 走 canonical scope 合同）。"""
    service, conversations, thread = _runtime(tmp_path)
    content = "公司服务器统一使用 HTTPS 部署。"
    old = service.long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "memory-old-task-ledger",
                "role": "system",
                "content": content,
                "kind": "fact",
                "tags": ["long_term_fact"],
                "attributes": {
                    "scope_type": "project",
                    "scope_key": "task:alpha",
                    "subject_key": "ops.deploy",
                },
                "source": "legacy",
            }
        ]
    )[0]
    assert old.entry_id == "memory-old-task-ledger"

    # 新写入入口已归一为 project:alpha（单一权威键）
    added = service.long_term.apply_batch(
        [
            {
                "action": "add",
                "role": "system",
                "content": content,
                "kind": "fact",
                "tags": ["long_term_fact"],
                "attributes": {
                    "scope_type": "project",
                    "scope_key": "project:alpha",
                    "subject_key": "ops.deploy",
                },
                "source": "candidate:test",
            }
        ]
    )

    assert added == []
    records = service.long_term.all()
    assert [record.entry_id for record in records] == ["memory-old-task-ledger"]


def test_storage_task_alias_ledger_conflicts_with_project_add_diff_content(tmp_path: Path):
    """1202 存储级：旧 task:<id> 账本 + 同 subject 不同内容新 project:<id> add →
    MemorySubjectConflict，不能并存（写时双权威被拒）。"""
    service, conversations, thread = _runtime(tmp_path)
    service.long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "memory-old-task-ledger",
                "role": "system",
                "content": "公司服务器统一使用 HTTPS 部署。",
                "kind": "fact",
                "tags": ["long_term_fact"],
                "attributes": {
                    "scope_type": "project",
                    "scope_key": "task:alpha",
                    "subject_key": "ops.deploy",
                },
                "source": "legacy",
            }
        ]
    )

    with pytest.raises(MemorySubjectConflict):
        service.long_term.apply_batch(
            [
                {
                    "action": "add",
                    "role": "system",
                    "content": "公司服务器统一使用 HTTP 部署。",
                    "kind": "fact",
                    "tags": ["long_term_fact"],
                    "attributes": {
                        "scope_type": "project",
                        "scope_key": "project:alpha",
                        "subject_key": "ops.deploy",
                    },
                    "source": "candidate:test",
                }
            ]
        )
    records = service.long_term.all()
    assert [record.entry_id for record in records] == ["memory-old-task-ledger"]


def test_old_task_alias_ledger_conflicts_with_new_project_candidate(tmp_path: Path):
    """1202 晋升层：旧 task:<id> 账本 + 同 subject 不同内容新 project:<id> 候选 →
    SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE，不能并存。"""
    service, conversations, thread = _runtime(tmp_path)
    service.long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "memory-old-task-ledger",
                "role": "system",
                "content": "公司服务器统一使用 HTTPS 部署。",
                "kind": "fact",
                "tags": ["long_term_fact"],
                "attributes": {
                    "scope_type": "project",
                    "scope_key": "task:alpha",
                    "subject_key": "ops.deploy",
                },
                "source": "legacy",
            }
        ]
    )

    candidate = _explicit(
        service,
        conversations,
        thread,
        content="公司服务器统一使用 HTTP 部署。",
        scope_type="project",
        scope_key="project:alpha",
        subject_key="ops.deploy",
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.status == "blocked_conflict"
    assert result.reason_code == "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE"
    records = service.long_term.all()
    assert [record.entry_id for record in records] == ["memory-old-task-ledger"]
    assert service.candidates.get(candidate.candidate_id).status == "blocked_conflict"
