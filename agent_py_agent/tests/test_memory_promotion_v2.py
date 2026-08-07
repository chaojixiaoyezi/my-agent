from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
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
    ) -> None:
        self.successful = successful
        self.terminal_status = terminal_status or (
            "succeeded" if successful else "failed"
        )
        self.effect_outcome = effect_outcome or (
            "completed" if successful else "not_started"
        )

    def verify(self, ref: dict[str, object]):
        return VerifiedToolEvidence(
            successful=self.successful,
            terminal_status=self.terminal_status,
            canonical_ref=dict(ref),
            effect_outcome=self.effect_outcome,
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
    thread = conversations.get_or_create_thread(
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
):
    message = conversations.append_message(
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
            proposed_action=proposed_action,
            target_entry_id=target_entry_id,
            promotion_target=promotion_target,
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
        scope_key="company",
    )

    assert service.promote(personal.candidate_id, automatic=True).promoted
    assert service.promote(company.candidate_id, automatic=True).promoted
    assert {record.attributes["scope_key"] for record in service.long_term.all()} == {
        "personal",
        "company",
    }


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
    assert result.reason_code == "MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN"
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


def test_model_inferred_lesson_with_repeated_evidence_auto_promotes(tmp_path: Path):
    """教训专线:model_inferred 的 lesson 候选,跨任务重复出现+多证据组 → 自动晋升落库。"""
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
    lessons = service.lessons.list()
    assert len(lessons) == 1
    assert lessons[0].content == "做 X 任务应先备份再修改,否则数据会丢。"
    assert lessons[0].evidence_groups == ("run:run-1", "run:run-2", "task:task-1", "task:task-2")


def test_model_inferred_single_occurrence_lesson_stays_cleanly_rejected(tmp_path: Path):
    """教训专线安全阀:单次出现 → 干净失败(不崩溃),不落库;候选保持 approved 可再审。"""
    service, _conversations, _thread = _runtime(tmp_path)
    candidate = _lesson(service, content="做 X 任务应先备份再修改。", runs=("run-1",))

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "PROMOTION_THRESHOLD_NOT_MET"
    assert service.lessons.list() == []


def test_lesson_auto_promotion_keeps_mutation_and_conflict_blocks(tmp_path: Path):
    """教训专线保留 mutation/conflict 检查:remove 动作与冲突引用仍被拦。"""
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
    assert result.reason_code == "AUTO_POLICY_MUTATION_REQUIRES_REVIEW"
    assert service.lessons.list() == []


def test_lesson_auto_promotion_rejects_conflicting_candidate(tmp_path: Path):
    service, _conversations, _thread = _runtime(tmp_path)
    conflicting = _lesson(
        service,
        content="做 X 任务不应备份。",
        runs=("run-1", "run-2"),
        conflicts_with=("candidate-other",),
    )

    result = service.promote(conflicting.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "AUTO_POLICY_CONFLICT_REQUIRES_REVIEW"
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


@pytest.mark.parametrize(
    ("target", "candidate_type", "content"),
    [
        ("soul", "soul_change", "AI 人格保持耐心和诚实。"),
        ("agents", "working_agreement", "长期合作时先核对真实代码。"),
    ],
)
def test_soul_and_agents_require_explicit_confirmation(
    tmp_path: Path,
    target: str,
    candidate_type: str,
    content: str,
) -> None:
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(
        service,
        conversations,
        thread,
        content=content,
        candidate_type=candidate_type,
        subject_key=f"persona.{target}.rule",
        promotion_target=target,
    )
    service.review(candidate.candidate_id, approved=True, reviewer="admin")

    with pytest.raises(ValueError, match="explicit user confirmation"):
        service.promote(candidate.candidate_id, reviewer="admin", confirmed=False)
    result = service.promote(candidate.candidate_id, reviewer="admin", confirmed=True)

    assert result.promoted is True
    target_path = service.persona.path_for(target)
    assert content in target_path.read_text(encoding="utf-8")


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


def test_near_duplicate_merge_updates_existing_entry_not_add(tmp_path: Path):
    """写路径查重:跨 subject 的逐字近重复合入旧条目,不新增(治重复记录膨胀)。"""
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
    assert len(records) == 1, "近重复必须合并,不新增"
    assert records[0].entry_id == first_ref, "合并保留原条目身份"
    assert records[0].content == "祥子买了两次车"
    assert first_ref in result.promotion_ref


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
