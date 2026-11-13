"""W8 综合测试：Lesson CAS 演进、Skill 载体分流、Curator 无模型维护与有界保留。

全部使用 tmp_path、测试替身与线程 Event 屏障，不触碰真机 ~/.my-agent 或外部网络。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.capability.skill_learning_store import (
    REQUEST_SCHEMA_VERSION,
    SkillLearningStore,
    request_key,
)
from agent_py_agent.agent.capability.skill_proposals import lesson_carrier
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
    host_promotion_mode,
)
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    CandidateTransitionError,
    candidate_retention_eligible,
)
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorInputBatch,
    CuratorMessageInput,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    CuratorDailyDraft,
    CuratorExtraction,
    MemoryCuratorConfig,
    parse_curator_extraction,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_schema import CuratorOutputViolation
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.curator_validation import validate_extraction
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.generation_manifest import StorageTombstones
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.operations import memory_content_hash
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
    ToolEvidenceVerifier,
    VerifiedToolEvidence,
)
from agent_py_agent.agent.memory_store.security import redact_memory_quote, scan_memory_content


class _PassingToolVerifier(ToolEvidenceVerifier):
    def __init__(self, owner_id: str = "owner-w8") -> None:
        self.owner_id = owner_id

    def verify(self, ref: dict[str, object]) -> VerifiedToolEvidence | None:
        if not ref.get("run_id") or not ref.get("operation_id"):
            return None
        return VerifiedToolEvidence(
            successful=True,
            terminal_status="succeeded",
            canonical_ref=dict(ref),
            effect_outcome="completed",
            parameters={},
        )


def _setup_owner(tmp_path: Path):
    owner = tmp_path / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    thread = conversations.threads.get_or_create(
        {
            "canonical_user_id": "owner-w8",
            "channel": "internal",
            "channel_conversation_id": "chat-w8",
            "channel_user_id": "owner-w8",
            "learning_source": ls.user_dialogue_projection(),
            "now": 10.0,
        }
    )
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    long_term = JsonlMemory(
        owner / "memory" / "long_term" / "memory.jsonl",
        ops_path=owner / "memory" / "ops.jsonl",
        candidate_service=candidates,
    )
    lessons = LessonRepository(
        owner / "memory" / "lessons",
        owner / "memory" / "routing" / "INDEX.md",
    )
    hot = HotRuleRepository(owner / "memory-hot.md")
    persona = PersonaRepository(
        owner_home=owner,
        soul_path=owner / "SOUL.md",
        user_path=owner / "USER.md",
        agents_path=owner / "AGENTS.md",
    )
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=long_term,
            persona=persona,
            lessons=lessons,
            hot=hot,
            message_verifier=ConversationMessageEvidenceVerifier(conversations),
            tool_verifier=_PassingToolVerifier("owner-w8"),
        )
    )
    return owner, service, conversations, thread, candidates, long_term, lessons, hot


def _tool_ref(run_id: str, op_id: str, tool: str = "run_command") -> dict[str, object]:
    return {
        "owner_id": "owner-w8",
        "run_id": run_id,
        "operation_id": op_id,
        "call_id": f"call-{op_id}",
        "tool": tool,
    }


def _observe_tool_lesson(
    candidates: CandidateService,
    *,
    content: str,
    subject_key: str = "workflow.testing",
    proposed_action: str = "add",
    target_entry_id: str = "",
    target_entry_version: int | None = None,
    occurrence_index: int = 1,
) -> object:
    task_id = f"task-{subject_key}-{occurrence_index}"
    run_id = f"run-{subject_key}-{occurrence_index}"
    op_id = f"op-{subject_key}-{occurrence_index}"
    return candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=content,
            subject_key=subject_key,
            scope=MemoryScope("personal", "personal"),
            origin="tool_verified",
            source_tool_refs=(_tool_ref(run_id, op_id),),
            source_task_ids=(task_id,),
            source_run_ids=(run_id,),
            learning_source=ls.user_dialogue_projection(),
            proposed_action=proposed_action,
            target_entry_id=target_entry_id,
            target_entry_version=target_entry_version,
            promotion_target="lesson",
            confidence=0.9,
        )
    )


def _user_message_ref(conversations: ConversationStore, thread: object, content: str) -> dict[str, object]:
    msg = conversations.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": content, "now": 20.0}
    )
    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "message_id": msg.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": digest,
        "quote": content,
    }


def _curator_service(
    owner: Path,
    conversations: ConversationStore,
    candidates: CandidateService,
    long_term: JsonlMemory,
    lessons: LessonRepository,
    hot: HotRuleRepository,
    *,
    backend: object | None = None,
    config: MemoryCuratorConfig | None = None,
    config_provider=None,
) -> MemoryCuratorService:
    formal = FormalMemorySource(long_term=long_term, lessons=lessons, hot=hot)
    return MemoryCuratorService(
        config=config or MemoryCuratorConfig(interval_seconds=60, turn_threshold=10),
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=conversations,
            audit_dir=owner / "audit",
            state_store=MemoryCuratorStateStore(owner / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(owner / "memory" / "daily"),
            candidate_service=candidates,
            run_log=CuratorRunLog(owner / "memory" / "curator" / "runs.jsonl"),
            identity=MemoryCuratorIdentity(provider="", model="", owner_id="owner-w8", timezone_name="UTC"),
            formal_memory_source=formal,
            config_provider=config_provider,
        ),
    )


def test_lesson_cas_supersession_conflict_and_version_validation(tmp_path: Path) -> None:
    _owner, service, conversations, thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)

    # 先自动晋升一条声明式规则和关联 HOT 规则。
    declarative = "禁止直接对生产配置写入未核验默认值。"
    for idx in (1, 2):
        cand1 = _observe_tool_lesson(
            candidates,
            content=declarative,
            subject_key="config.safety",
            occurrence_index=idx,
        )
    assert lesson_carrier(cand1) == "lesson"
    res1 = service.promote(cand1.candidate_id, automatic=True)
    assert res1.promoted is True
    assert res1.reason_code == "PROMOTED"

    active_before = [item for item in lessons.list() if item.status == "active"]
    assert len(active_before) == 1
    lesson1 = active_before[0]
    assert lesson1.revision == 1

    for idx in (1, 2, 3):
        hot_cand = candidates.observe(
            CandidateObservation(
                candidate_type="hot_rule",
                content="禁止写入未核验默认配置。",
                subject_key="config.safety",
                scope=MemoryScope("personal", "personal"),
                origin="reviewed",
                evidence_refs=({"kind": "task", "ref_id": f"ht-{idx}"},),
                source_task_ids=(f"ht-{idx}",),
                source_run_ids=(f"hr-{idx}",),
                learning_source=ls.user_dialogue_projection(),
                proposed_action="add",
                target_entry_id=lesson1.lesson_id,
                promotion_target="hot",
                confidence=0.95,
            )
        )
    service.review(hot_cand.candidate_id, approved=True, reviewer="owner")
    hot_res = service.promote(hot_cand.candidate_id, automatic=False)
    assert hot_res.promoted is True
    assert any(item.lesson_id == lesson1.lesson_id for item in hot.list())

    # FormalMemorySource + validate_extraction 必须冻结 lesson 权威版本。
    formal_items = FormalMemorySource(long_term=long_term, lessons=lessons, hot=hot).read(
        max_items=32, max_chars=4000
    )
    lesson_formal = next(item for item in formal_items if item.authority_type == "lesson")
    assert lesson_formal.authority_id == lesson1.lesson_id
    assert lesson_formal.authority_version == 1

    msg_rep_text = "禁止无校验写入配置，改由注册表统一声明。"
    msg_rep_ref = _user_message_ref(conversations, thread, msg_rep_text)
    msg_input = CuratorMessageInput(
        message_id=str(msg_rep_ref["message_id"]),
        thread_id=thread.thread_id,
        role="user",
        channel="internal",
        created_at=20.0,
        full_content=msg_rep_text,
        content_preview=msg_rep_text,
        content_hash=str(msg_rep_ref["content_hash"]),
        metadata={},
        thread_source=ls.user_dialogue_projection(),
    )
    batch_in = CuratorInputBatch(
        messages=(msg_input,),
        audit_events=(),
        formal_memories=tuple(formal_items),
    )
    extraction = CuratorExtraction(
        daily_events=(),
        candidates=(
            CandidateObservation(
                candidate_type="lesson",
                content=msg_rep_text,
                subject_key="config.safety",
                scope=MemoryScope("personal", "personal"),
                origin="user_explicit",
                source_message_refs=(msg_rep_ref,),
                learning_source=ls.user_dialogue_projection(),
                proposed_action="replace",
                target_entry_id=lesson1.lesson_id,
                promotion_target="lesson",
            ),
        ),
        processed_message_refs=({"thread_id": thread.thread_id, "message_id": msg_input.message_id},),
        processed_audit_refs=(),
        unresolved_refs=(),
        warnings=(),
        next_cursor={},
    )
    validated = validate_extraction(extraction, batch_in, owner_id="owner-w8")
    assert validated.candidates[0].target_entry_version == 1

    # 无 target_entry_version 的 replace/merge 不允许自动提交。
    missing_ver = CandidateObservation(
        candidate_type="lesson",
        content="禁止无校验写入配置（缺版本）。",
        subject_key="config.safety",
        scope=MemoryScope("personal", "personal"),
        origin="user_explicit",
        source_message_refs=(_user_message_ref(conversations, thread, "禁止无校验写入配置（缺版本）。"),),
        proposed_action="replace",
        target_entry_id=lesson1.lesson_id,
        target_entry_version=None,
        promotion_target="lesson",
        learning_source=ls.user_dialogue_projection(),
    )
    missing_cand = candidates.observe(missing_ver)
    assert host_promotion_mode(missing_cand) == "manual_required"
    auto_missing = service.promote(missing_cand.candidate_id, automatic=True)
    assert auto_missing.promoted is False
    assert auto_missing.reason_code == "PROMOTION_MANUAL_REQUIRED"
    service.review(missing_cand.candidate_id, approved=True, reviewer="owner")
    manual_missing = service.promote(missing_cand.candidate_id, automatic=False)
    assert manual_missing.status == "blocked_conflict"
    assert manual_missing.reason_code == "TARGET_ENTRY_OBSERVATION_VERSION_MISSING"

    # 带正确 CAS revision=1 的 replace 自动晋升：旧 lesson 变 superseded，关联 HOT 同步退役。
    msg2_text = "禁止直接写入配置默认值，改为只读引用参数注册表。"
    msg2_ref = _user_message_ref(conversations, thread, msg2_text)
    replace_cand = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=msg2_text,
            subject_key="config.safety",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(msg2_ref,),
            learning_source=ls.user_dialogue_projection(),
            proposed_action="replace",
            target_entry_id=lesson1.lesson_id,
            target_entry_version=1,
            promotion_target="lesson",
        )
    )
    assert host_promotion_mode(replace_cand) == "auto_eligible"
    rep_res = service.promote(replace_cand.candidate_id, automatic=True)
    assert rep_res.promoted is True
    assert lessons.get(lesson1.lesson_id).status == "superseded"
    assert not any(item.lesson_id == lesson1.lesson_id for item in hot.list())

    # 幂等重放仍返回已晋升且不重复改写。
    replay_res = service.promote(replace_cand.candidate_id, automatic=True)
    assert replay_res.promoted is True
    assert replay_res.reason_code == "ALREADY_PROMOTED"
    assert replay_res.promotion_ref == rep_res.promotion_ref

    # 使用过期 revision=1 并发覆盖已被取代的 lesson1：必须进 blocked_conflict 且保住当前活跃 lesson。
    msg3_text = "禁止写入默认值（旧版本并发冲突）。"
    stale_cand = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=msg3_text,
            subject_key="config.safety",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(_user_message_ref(conversations, thread, msg3_text),),
            learning_source=ls.user_dialogue_projection(),
            proposed_action="merge",
            target_entry_id=lesson1.lesson_id,
            target_entry_version=1,
            promotion_target="lesson",
        )
    )
    stale_res = service.promote(stale_cand.candidate_id, automatic=True)
    assert stale_res.promoted is False
    assert stale_res.status == "blocked_conflict"
    assert stale_res.reason_code in {"LESSON_REVISION_CONFLICT", "SUPERSESSION_SCOPE_OR_SUBJECT_MISMATCH"}
    active_after = [item for item in lessons.list() if item.status == "active"]
    assert len(active_after) == 1
    assert active_after[0].content == msg2_text


def test_storage_tombstones_block_lesson_without_threshold_masking(tmp_path: Path) -> None:
    _owner, service, _conv, _thread, candidates, long_term, lessons, _hot = _setup_owner(tmp_path)

    text = "不要绕过只读门禁检查直接返回结果。"
    seed = long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "lesson-deleted-1",
                "role": "system",
                "content": text,
                "kind": "fact",
                "tags": ["lesson"],
            }
        ]
    )[0]
    long_term.remove(seed.entry_id, source="user:forget", expected_version=seed.version)
    assert StorageTombstones(long_term.path).excludes("", memory_content_hash(text))

    for idx in (1, 2):
        cand = _observe_tool_lesson(candidates, content=text, subject_key="tombstone.content", occurrence_index=idx)

    single = service.promote(cand.candidate_id, automatic=True)
    assert single.promoted is False
    assert single.reason_code == "MEMORY_TOMBSTONED_CONTENT"
    assert lessons.list() == []

    # 匹配已删除 ID 的 replace 候选走批量路径同样精确拦截。
    by_id = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="全新文本但目标 ID 已被永久遗忘。",
            subject_key="tombstone.id",
            scope=MemoryScope("personal", "personal"),
            origin="tool_verified",
            source_tool_refs=(_tool_ref("run-tid", "op-tid"),),
            source_task_ids=("task-tid",),
            source_run_ids=("run-tid",),
            learning_source=ls.user_dialogue_projection(),
            proposed_action="replace",
            target_entry_id="lesson-deleted-1",
            target_entry_version=1,
            promotion_target="lesson",
        )
    )
    batch = service.promote_batch([by_id], reviewer="memory-promotion-service", automatic=True)
    assert batch[0].promoted is False
    assert batch[0].reason_code == "MEMORY_TOMBSTONED_ID"
    assert lessons.list() == []


def test_hot_promote_and_lesson_maintenance_serialized_under_lifecycle_lock(tmp_path: Path) -> None:
    owner, service, _conv, _thread, candidates, _long_term, lessons, hot = _setup_owner(tmp_path)
    lesson_text = "执行重要重构前固定先跑核心回归。"
    for idx in (1, 2):
        l_cand = _observe_tool_lesson(candidates, content=lesson_text, subject_key="locking.hot", occurrence_index=idx)
    assert service.promote(l_cand.candidate_id, automatic=True).promoted is True
    lesson = lessons.list()[0]

    for idx in (1, 2, 3):
        h_cand = candidates.observe(
            CandidateObservation(
                candidate_type="hot_rule",
                content="重构前先跑核心回归。",
                subject_key="locking.hot",
                scope=MemoryScope("personal", "personal"),
                origin="reviewed",
                evidence_refs=({"kind": "task", "ref_id": f"ht-{idx}"},),
                source_task_ids=(f"ht-{idx}",),
                source_run_ids=(f"hr-{idx}",),
                learning_source=ls.user_dialogue_projection(),
                proposed_action="add",
                target_entry_id=lesson.lesson_id,
                promotion_target="hot",
                confidence=0.95,
            )
        )
    service.review(h_cand.candidate_id, approved=True, reviewer="owner")

    entered_get = threading.Event()
    release_get = threading.Event()
    maint_started = threading.Event()
    maint_finished = threading.Event()
    orig_get = lessons.get

    def gated_get(target: str):
        entered_get.set()
        assert release_get.wait(timeout=5)
        return orig_get(target)

    lessons.get = gated_get  # type: ignore[method-assign]
    promote_result: list[object] = []
    maint_result: list[object] = []

    t_prom = threading.Thread(target=lambda: promote_result.append(service.promote(h_cand.candidate_id, automatic=False)))
    t_prom.start()
    assert entered_get.wait(timeout=5)

    def run_maint() -> None:
        maint_started.set()
        maint_result.append(
            lessons.maintain(
                now=datetime.now(timezone.utc) + timedelta(days=30),
                archive_days=15,
                pin_loader=lambda: (tuple(r.lesson_id for r in hot.list()), True),
            )
        )
        maint_finished.set()

    t_maint = threading.Thread(target=run_maint)
    t_maint.start()
    assert maint_started.wait(timeout=5)
    # 生命周期锁持有期间 maintain 不得抢先完成。
    assert not maint_finished.wait(timeout=0.05)

    release_get.set()
    t_prom.join(timeout=5)
    t_maint.join(timeout=5)
    assert maint_finished.is_set()
    assert promote_result[0].promoted is True
    assert any(item.lesson_id == lesson.lesson_id for item in hot.list())
    assert not (owner / "memory" / "lessons" / "archive").exists()


def test_procedural_skill_carrier_delegates_without_popups_and_handles_deferred(tmp_path: Path) -> None:
    owner, service, _conv, _thread, candidates, _lt, lessons, hot = _setup_owner(tmp_path)

    procedural = (
        "# 标准发布部署步骤\n"
        "## 适用场景\n"
        "执行版本发布与静态检查前。\n"
        "## 执行步骤\n"
        "1. 运行 `python3 -m pytest -q` 验证单测；\n"
        "2. 运行 `ruff check agent_py_agent`；\n"
        "3. 提交前确认文件白名单与状态。"
    )
    proc_cand = _observe_tool_lesson(
        candidates,
        content=procedural,
        subject_key="deploy.playbook",
        occurrence_index=1,
    )
    assert lesson_carrier(proc_cand) == "skill"

    # 未注入外部回调时走 standalone SkillLearningStore FIFO，不写 lesson/HOT/S1 proposal。
    res = service.promote(proc_cand.candidate_id, automatic=True)
    assert res.promoted is False
    assert res.status == "withdrawn"
    assert res.reason_code == "DELEGATED_TO_SKILL_CARRIER"
    assert res.formal_memory_changed is False
    assert res.promotion_ref == ""
    assert lessons.list() == []
    assert hot.list() == []
    assert not (owner / "data" / "skill_proposals").exists()

    req_dir = owner / "data" / "skill_learning" / "requests"
    files = list(req_dir.glob("*.json"))
    assert len(files) == 1
    req_payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert req_payload["schema_version"] == REQUEST_SCHEMA_VERSION
    assert req_payload["source_candidate_id"] == proc_cand.candidate_id

    # 对已撤回的同一候选重放，保持幂等且不制造多余条目或错误。
    replay = service.promote(proc_cand.candidate_id, automatic=True)
    assert replay.promoted is False
    assert replay.status == "withdrawn"
    assert len(list(req_dir.glob("*.json"))) == 1

    # 测试 deferred（例如 SKILL_LEARNING_DISABLED）时保留状态不撤回，恢复后可成功委托。
    enabled_flag = {"on": False}

    def fake_enqueue(batch):
        out = {}
        for row in batch:
            if not enabled_flag["on"]:
                out[row.candidate_id] = {"status": "deferred", "reason_code": "SKILL_LEARNING_DISABLED"}
            else:
                out[row.candidate_id] = {"status": "queued", "reason_code": "DELEGATED_TO_SKILL_CARRIER"}
        return out

    service2 = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=service.long_term,
            persona=service.persona,
            lessons=lessons,
            hot=hot,
            message_verifier=service.message_verifier,
            tool_verifier=service.tool_verifier,
            skill_carrier_enqueue=fake_enqueue,
        )
    )
    proc_cand2 = _observe_tool_lesson(
        candidates,
        content=(
            "# 构建容器镜像操作清单\n"
            "## 适用场景\n"
            "构建并发布容器镜像时。\n"
            "## 操作步骤\n"
            "1. run_command 执行构建；\n"
            "2. 校验镜像摘要。"
        ),
        subject_key="build.container.steps",
        occurrence_index=2,
    )
    assert lesson_carrier(proc_cand2) == "skill"
    deferred_res = service2.promote(proc_cand2.candidate_id, automatic=True)
    assert deferred_res.promoted is False
    assert deferred_res.status == "pending_review"
    assert deferred_res.reason_code == "SKILL_LEARNING_DISABLED"
    assert candidates.get(proc_cand2.candidate_id).status == "pending_review"
    assert lessons.list() == []
    assert not (owner / "data" / "skill_proposals").exists()

    enabled_flag["on"] = True
    # 混合批次：程序性规则走载体，声明式规则走 LessonRepository。
    decl_cand = _observe_tool_lesson(
        candidates,
        content="禁止在生产热路径执行全表扫描。",
        subject_key="perf.rule",
        occurrence_index=1,
    )
    decl_cand = _observe_tool_lesson(
        candidates,
        content="禁止在生产热路径执行全表扫描。",
        subject_key="perf.rule",
        occurrence_index=2,
    )
    assert lesson_carrier(decl_cand) == "lesson"
    batch_res = service2.promote_batch([proc_cand2, decl_cand], reviewer="memory-promotion-service", automatic=True)
    assert batch_res[0].status == "withdrawn"
    assert batch_res[0].reason_code == "DELEGATED_TO_SKILL_CARRIER"
    assert batch_res[1].promoted is True
    assert batch_res[1].reason_code == "PROMOTED"
    assert len(lessons.list()) == 1


def test_curator_run_maintenance_if_due_independent_of_model_and_live_config(tmp_path: Path) -> None:
    owner, _service, conversations, _thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)

    # 创建一条教训，使之超过 15 天未进路由。
    base_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    for idx in (1, 2):
        c1 = _observe_tool_lesson(
            candidates,
            content="旧规则第一条：始终检查参数边界与单位。",
            subject_key="maint.one",
            occurrence_index=idx,
        )
    assert _service.promote(c1.candidate_id, automatic=True).promoted is True

    # 先放入一条未消费的 SkillLearning FIFO 请求，维护绝对不能消费它。
    skill_store = SkillLearningStore(owner / "data" / "skill_learning", owner / "skills")
    rkey = request_key("maint-fifo-keep")
    assert (
        skill_store.enqueue(
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_key": rkey,
                "created_at": base_time.isoformat(),
                "run_id": "run-keep",
                "task_id": "task-keep",
                "source_candidate_id": "cand-missing-or-held",
                "task": "keep request",
                "result": "keep result",
                "tool_summary": "none",
                "recalled_memory_ids": [],
                "learning_source": ls.user_dialogue_projection(),
            }
        )
        == "queued"
    )

    live = SimpleNamespace(
        memory_curator_enabled=True,
        memory_curator_interval_seconds=60,
        memory_curator_turn_threshold=999,
        memory_lesson_archive_days=0,
    )
    curator = _curator_service(
        owner,
        conversations,
        candidates,
        long_term,
        lessons,
        hot,
        backend=None,
        config_provider=lambda: live,
    )

    # 无模型、turn_threshold=999 仍执行首次维护；archive_days=0 不归档文件，仅刷新路由/热度。
    t0 = base_time
    m0 = curator.run_maintenance_if_due(now=t0)
    assert m0["status"] == "succeeded"
    assert m0["lessons_maintained"] is True
    assert m0["archive_days"] == 0
    assert (owner / "memory" / "routing" / "INDEX.md").is_file()
    assert len(list((owner / "data" / "skill_learning" / "requests").glob("*.json"))) == 1

    # 同周期内节流为 not_due。
    m_skip = curator.run_maintenance_if_due(now=t0 + timedelta(seconds=10))
    assert m_skip["status"] == "not_due"

    # 往 long_term 写一条记录并用多线程模拟并发 access touch，确认刷盘不丢失并发命中。
    rec = long_term.apply_batch(
        [
            {
                "action": "add",
                "entry_id": "mem-touch-1",
                "role": "user",
                "content": "项目测试环境统一使用隔离目录。",
                "kind": "fact",
                "tags": ["fact"],
            }
        ]
    )[0]
    long_term._pending_access[rec.entry_id] = 100.0
    t_future = t0 + timedelta(days=20)
    live.memory_lesson_archive_days = 15
    live.memory_curator_interval_seconds = 10

    m1 = curator.run_maintenance_if_due(now=t_future)
    assert m1["status"] == "succeeded"
    assert m1["archive_days"] == 15
    assert m1["access_flushed"] is True
    assert any(
        p.name.endswith(f"-{rkey}.json")
        for p in (owner / "data" / "skill_learning" / "requests").glob("*.json")
    )

    # 若 memory-hot.md 损坏不可读，必须 fail-closed（coverage=partial），绝不跳过置顶归档。
    hot_path = owner / "memory-hot.md"
    hot_path.write_text("# Memory HOT\n<!-- my-agent-hot-meta:{broken json -->\n", encoding="utf-8")
    m_partial = curator.run_maintenance_if_due(now=t_future + timedelta(seconds=30))
    assert m_partial["status"] == "partial"
    # partial 时不能推进节流时间戳，修复后同刻附近下一拍可继续。
    assert curator._maintenance_now_ts == pytest.approx(t_future.timestamp())

    # owner memory_policy.json 禁用 memory 时停止维护；损坏时 fail-closed 阻断。
    hot_path.unlink()
    policy_path = owner / "memory_policy.json"
    policy_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    m_dis = curator.run_maintenance_if_due(now=t_future + timedelta(seconds=60))
    assert m_dis["status"] == "disabled"
    assert m_dis["reason"] == "memory_policy_disabled"

    policy_path.write_text("{broken", encoding="utf-8")
    m_blk = curator.run_maintenance_if_due(now=t_future + timedelta(seconds=90))
    assert m_blk["status"] == "blocked"
    assert m_blk["reason"] == "memory_policy_unreadable"


def test_candidate_retention_threshold_expiration_and_fifo_pin_protection(tmp_path: Path) -> None:
    owner, service, conversations, thread, candidates, _lt, _lessons, _hot = _setup_owner(tmp_path)

    # 1. 非 user_explicit 且因 PROMOTION_THRESHOLD_NOT_MET 卡在 approved 的 lesson 候选允许按龄清理。
    stuck_tool = _observe_tool_lesson(
        candidates,
        content="仅单次工具调用触发的规则。",
        subject_key="ret.stuck.tool",
        occurrence_index=1,
    )
    stuck_tool = candidates.transition(stuck_tool, "approved", reviewer="admin")
    stuck_tool = candidates.defer_commit(stuck_tool, "PROMOTION_THRESHOLD_NOT_MET")
    assert candidate_retention_eligible(stuck_tool) is True

    # 2. user_explicit 同理由仍受保护，永不因年龄误删。
    explicit_ref = _user_message_ref(conversations, thread, "用户明确教训等待阈值。")
    stuck_user = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="用户明确教训等待阈值。",
            subject_key="ret.stuck.user",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(explicit_ref,),
            learning_source=ls.user_dialogue_projection(),
            promotion_target="lesson",
        )
    )
    stuck_user = candidates.transition(stuck_user, "approved", reviewer="admin")
    stuck_user = candidates.defer_commit(stuck_user, "PROMOTION_THRESHOLD_NOT_MET")
    assert candidate_retention_eligible(stuck_user) is False

    # 3. 其它阻塞原因（如 SKILL_LEARNING_DISABLED）仍受保护。
    other_defer = _observe_tool_lesson(
        candidates,
        content="暂被禁用延后的候选。",
        subject_key="ret.other.defer",
        occurrence_index=1,
    )
    other_defer = candidates.transition(other_defer, "approved", reviewer="admin")
    other_defer = candidates.defer_commit(other_defer, "SKILL_LEARNING_DISABLED")
    assert candidate_retention_eligible(other_defer) is False

    # 4. 已委托给 Skill FIFO（withdrawn）但在 FIFO 尚未被消费期间受 pin 保护；消费后恢复可清理。
    delegated = _observe_tool_lesson(
        candidates,
        content=(
            "# 待总结步骤\n"
            "## 适用场景\n"
            "提交前校验场景。\n"
            "## 操作步骤\n"
            "1. 运行单测；\n"
            "2. 提交校验。"
        ),
        subject_key="ret.delegated",
        occurrence_index=1,
    )
    assert service.promote(delegated.candidate_id, automatic=True).status == "withdrawn"

    future_cutoff = datetime.now(timezone.utc) + timedelta(days=365)
    eligible_ids = candidates.retention_candidates(before=future_cutoff)
    assert stuck_tool.candidate_id in eligible_ids
    assert stuck_user.candidate_id not in eligible_ids
    assert other_defer.candidate_id not in eligible_ids
    assert delegated.candidate_id not in eligible_ids

    with pytest.raises(CandidateTransitionError):
        candidates.delete_terminal([stuck_user.candidate_id])
    with pytest.raises(CandidateTransitionError):
        candidates.delete_terminal([delegated.candidate_id])

    # 清空 FIFO 请求文件后，withdrawn 候选解除 pin 保护，且 delete_terminal 可正常清理。
    for req_file in (owner / "data" / "skill_learning" / "requests").glob("*.json"):
        req_file.unlink()
    eligible_after = candidates.retention_candidates(before=future_cutoff)
    assert delegated.candidate_id in eligible_after

    deleted = candidates.delete_terminal([stuck_tool.candidate_id, delegated.candidate_id])
    assert deleted == 2
    remaining_ids = {item.candidate_id for item in candidates.list()}
    assert stuck_tool.candidate_id not in remaining_ids
    assert delegated.candidate_id not in remaining_ids

    # 验证若 candidate 保留有 target_evidence_bindings 时转 superseded 会清空遗留 commit_obligation。
    stuck_bound = _observe_tool_lesson(
        candidates,
        content="带绑定信息的超期未达标候选。",
        subject_key="ret.stuck.bound",
        proposed_action="replace",
        target_entry_id="lesson-xyz",
        target_entry_version=3,
        occurrence_index=1,
    )
    stuck_bound = candidates.transition(stuck_bound, "approved", reviewer="admin")
    stuck_bound = candidates.defer_commit(stuck_bound, "PROMOTION_THRESHOLD_NOT_MET")
    assert candidates.delete_terminal([stuck_bound.candidate_id]) == 1
    tombstone = candidates.get(stuck_bound.candidate_id)
    assert tombstone.status == "superseded"
    assert tombstone.commit_obligation is False
    assert tombstone.obligation_blocked_reason == ""
    assert tombstone.obligation_retry_after == ""
    # 不使 _assert 误用 time
    assert time.time() > 0


class _FakeStructuredBackend:
    name = "fake-structured-w8"

    def __init__(self, response_data: dict[str, object]) -> None:
        self.response_data = response_data
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        assert "待提炼经历 JSON" in prompt
        assert "tools" not in response_schema
        self.calls += 1
        return ModelResponse(
            text=json.dumps(self.response_data, ensure_ascii=False),
            backend=self.name,
        )


def _conversation_event_response(message_id: str) -> dict[str, object]:
    return {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [
            {
                "event_type": "conversation",
                "summary": "用户要求检查运行期配置热生效。",
                "actor": "user",
                "origin": "user_explicit",
                "message_refs": [{"message_id": message_id}],
                "tool_refs": [],
                "artifact_refs": [],
                "decisions": [],
                "lessons": [],
                "next_actions": [],
            }
        ],
        "candidates": [],
    }


def test_p1_live_effective_config_honored_across_run_backlog_and_status(tmp_path: Path) -> None:
    owner, service, conversations, thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)
    msg = conversations.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "每次切换动态配置后必须立即按新值生效。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-p1-cfg",
                "request_id": "request-p1-cfg",
                "task_id": "task-p1-cfg",
                "run_id": "run-p1-cfg",
            },
            "now": 15.0,
        }
    )
    cand = None
    for idx in (1, 2):
        cand = _observe_tool_lesson(
            candidates,
            content="配置切换后必须立刻在 Curator 运行链路上读取最新参数。",
            subject_key="curator.config.live",
            occurrence_index=idx,
        )
    assert cand is not None
    assert lesson_carrier(cand) == "lesson"

    seen_archive_days: list[int] = []
    orig_maintain = lessons.maintain

    def _wrapped_maintain(*args, **kwargs):
        seen_archive_days.append(int(kwargs.get("archive_days", -1)))
        return orig_maintain(*args, **kwargs)

    lessons.maintain = _wrapped_maintain  # type: ignore[method-assign]

    live_ns = SimpleNamespace(
        memory_curator_enabled=False,
        memory_curator_interval_seconds=9999,
        memory_curator_turn_threshold=99,
        memory_curator_auto_promotion_policy="manual_only",
        memory_lesson_archive_days=15,
    )
    backend = _FakeStructuredBackend(_conversation_event_response(msg.message_id))
    stale_config = MemoryCuratorConfig(
        enabled=False,
        interval_seconds=9999,
        turn_threshold=99,
        auto_promotion_policy="manual_only",
        lesson_archive_days=15,
    )
    curator = MemoryCuratorService(
        config=stale_config,
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=conversations,
            audit_dir=owner / "audit",
            state_store=MemoryCuratorStateStore(owner / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(owner / "memory" / "daily"),
            candidate_service=candidates,
            run_log=CuratorRunLog(owner / "memory" / "curator" / "runs.jsonl"),
            identity=MemoryCuratorIdentity(provider="", model="", owner_id="owner-w8", timezone_name="UTC"),
            formal_memory_source=FormalMemorySource(long_term=long_term, lessons=lessons, hot=hot),
            promotion_callback=lambda cid: service.promote(cid, automatic=True),
            config_provider=lambda: live_ns,
        ),
    )

    now = datetime.now(timezone.utc)
    assert curator.status()["enabled"] is False
    assert curator.run_if_due(now=now).status == "disabled"
    assert backend.calls == 0

    live_ns.memory_curator_enabled = True
    live_ns.memory_curator_interval_seconds = 60
    live_ns.memory_curator_turn_threshold = 1
    live_ns.memory_curator_auto_promotion_policy = "auto"
    live_ns.memory_lesson_archive_days = 0

    status_after = curator.status()
    assert status_after["enabled"] is True
    assert status_after["config"]["interval_seconds"] == 60
    assert status_after["config"]["turn_threshold"] == 1
    assert status_after["config"]["auto_promotion_policy"] == "auto"

    run_res = curator.run_if_due(now=now)
    assert run_res.status == "succeeded"
    assert candidates.get(cand.candidate_id).status == "promoted"
    assert seen_archive_days == [0]

    # backlog cursor 收口后，下一拍立刻按热生效的 turn_threshold=1 走提取与落盘。
    extract_res = curator.run_if_due(now=now + timedelta(seconds=1))
    assert extract_res.status == "succeeded"
    assert backend.calls == 1


def test_p1_maintenance_deterministic_failure_partial_progress_and_uncertain_propagation(
    tmp_path: Path,
) -> None:
    owner, _service, conversations, thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)
    msg = conversations.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "维护步骤部分失败时不得阻塞后续增量策展。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-p1-maint",
                "request_id": "request-p1-maint",
                "task_id": "task-p1-maint",
                "run_id": "run-p1-maint",
            },
            "now": 16.0,
        }
    )
    backend = _FakeStructuredBackend(_conversation_event_response(msg.message_id))
    curator = _curator_service(
        owner,
        conversations,
        candidates,
        long_term,
        lessons,
        hot,
        backend=backend,
        config=MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, lesson_archive_days=15),
    )

    orig_flush = long_term.flush_access_events
    orig_maintain = lessons.maintain

    def _failing_flush(*args, **kwargs):
        raise OSError("simulated I/O error during access flush")

    long_term.flush_access_events = _failing_flush  # type: ignore[method-assign]
    t0 = datetime.now(timezone.utc)
    partial_report = curator.run_maintenance_if_due(now=t0)
    assert partial_report["status"] == "partial"
    assert partial_report["lessons_maintained"] is True
    assert partial_report["access_flushed"] is False
    assert partial_report["candidates_compacted"] is True
    assert partial_report["errors"] == {"access_flush": "CURATOR_MAINTENANCE_IO_ERROR"}
    assert getattr(curator, "_maintenance_at", 0.0) == 0.0

    # 修复 flush 后，因为未推进节流时间戳，下一次立刻允许重试并全量成功。
    long_term.flush_access_events = orig_flush  # type: ignore[method-assign]
    ok_report = curator.run_maintenance_if_due(now=t0)
    assert ok_report["status"] == "succeeded"
    assert ok_report["access_flushed"] is True
    assert getattr(curator, "_maintenance_at", 0.0) > 0.0

    # 确定性维护失败不阻塞 run_if_due() 主提取流程。
    curator._maintenance_at = 0.0
    curator._maintenance_now_ts = 0.0

    def _failing_maintain(*args, **kwargs):
        raise OSError("simulated I/O error during lesson maintenance")

    lessons.maintain = _failing_maintain  # type: ignore[method-assign]
    res = curator.run_if_due(now=t0 + timedelta(seconds=5))
    assert res.status == "succeeded"
    assert backend.calls == 1

    # 未决异常必须原样透传，不允许吞掉。
    curator._maintenance_at = 0.0
    curator._maintenance_now_ts = 0.0

    def _uncertain_maintain(*args, **kwargs):
        raise RequestExecutionOutcomeUncertainError(
            {
                "id": "req-uncertain",
                "execution_attempt_id": "attempt-uncertain",
                "lease_epoch": 1,
                "lease_owner": "owner-w8",
            },
            "lesson_maintain",
        )

    lessons.maintain = _uncertain_maintain  # type: ignore[method-assign]
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        curator.run_maintenance_if_due(now=t0 + timedelta(seconds=10))
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        curator.run_if_due(now=t0 + timedelta(seconds=10))
    lessons.maintain = orig_maintain  # type: ignore[method-assign]


def test_p1_skill_learning_pins_fail_closed_on_corrupt_symlink_and_overflow(tmp_path: Path) -> None:
    owner, _service, _conversations, _thread, candidates, _long_term, _lessons, _hot = _setup_owner(tmp_path)
    stuck = _observe_tool_lesson(
        candidates,
        content="等待验证阈值且用于测试 pin 失败闭合的候选。",
        subject_key="ret.pin.guard",
        occurrence_index=1,
    )
    stuck = candidates.transition(stuck, "approved", reviewer="admin")
    stuck = candidates.defer_commit(stuck, "PROMOTION_THRESHOLD_NOT_MET")
    future_cutoff = datetime.now(timezone.utc) + timedelta(days=365)

    # 1. skill_learning 目录自然不存在时正常返回候选列表。
    assert stuck.candidate_id in candidates.retention_candidates(before=future_cutoff)

    # 2. 队列内出现损坏 JSON 时 fail-closed，不得退化为空 pin 误删。
    req_dir = owner / "data" / "skill_learning" / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    bad_file = req_dir / "corrupt.json"
    bad_file.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(CandidateTransitionError, match="SKILL_LEARNING_PINS_UNREADABLE:CORRUPT_JSON"):
        candidates.retention_candidates(before=future_cutoff)
    with pytest.raises(CandidateTransitionError, match="SKILL_LEARNING_PINS_UNREADABLE:CORRUPT_JSON"):
        candidates.delete_terminal([stuck.candidate_id])

    # 3. 请求文件数超过 512 上限时 fail-closed。
    bad_file.unlink()
    for idx in range(513):
        (req_dir / f"req-{idx:03d}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        CandidateTransitionError,
        match="SKILL_LEARNING_PINS_UNREADABLE:QUEUE_FILE_LIMIT_EXCEEDED",
    ):
        candidates.retention_candidates(before=future_cutoff)

    # 4. skill_learning 或子请求为符号链接时 fail-closed。
    for item in req_dir.iterdir():
        item.unlink()
    outside_target = tmp_path / "outside-target.json"
    outside_target.write_text(json.dumps({"candidate_id": stuck.candidate_id}), encoding="utf-8")
    (req_dir / "linked.json").symlink_to(outside_target)
    with pytest.raises(CandidateTransitionError, match="SKILL_LEARNING_PINS_UNREADABLE:SYMLINK"):
        candidates.delete_terminal([stuck.candidate_id])

    (req_dir / "linked.json").unlink()
    req_dir.rmdir()
    skill_dir = owner / "data" / "skill_learning"
    skill_dir.rmdir()
    outside_dir = tmp_path / "outside-skill-learning"
    outside_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.symlink_to(outside_dir)
    with pytest.raises(CandidateTransitionError, match="SKILL_LEARNING_PINS_UNREADABLE:SYMLINK"):
        candidates.retention_candidates(before=future_cutoff)


def test_p1_lesson_remove_action_rejected_before_skill_delegation(tmp_path: Path) -> None:
    owner, service, _conversations, _thread, candidates, _long_term, _lessons, _hot = _setup_owner(tmp_path)
    procedural_remove = _observe_tool_lesson(
        candidates,
        content=(
            "# 拟删除流程\n"
            "## 适用场景\n"
            "验证 remove 动作不会在 lesson 路径分流成 Skill。\n"
            "## 操作步骤\n"
            "1. 检查动作字段；\n"
            "2. 拒绝非法 remove。"
        ),
        subject_key="workflow.remove.guard",
        proposed_action="remove",
        target_entry_id="lesson-target-1",
        target_entry_version=1,
        occurrence_index=1,
    )
    procedural_remove = candidates.transition(procedural_remove, "approved", reviewer="admin")

    single_res = service.promote(procedural_remove.candidate_id, reviewer="admin", automatic=False)
    assert single_res.promoted is False
    assert single_res.reason_code == "LESSON_ACTION_UNSUPPORTED"
    req_dir = owner / "data" / "skill_learning" / "requests"
    assert not list(req_dir.glob("*.json")) if req_dir.exists() else True

    procedural_remove_batch = _observe_tool_lesson(
        candidates,
        content=(
            "# 拟批量删除流程\n"
            "## 适用场景\n"
            "验证批量 promote_batch 的 remove 动作也不会在 lesson 路径分流成 Skill。\n"
            "## 操作步骤\n"
            "1. 检查批量动作字段；\n"
            "2. 拒绝非法 remove。"
        ),
        subject_key="workflow.remove.batch.guard",
        proposed_action="remove",
        target_entry_id="lesson-target-2",
        target_entry_version=1,
        occurrence_index=2,
    )
    procedural_remove_batch = candidates.transition(procedural_remove_batch, "approved", reviewer="admin")

    batch_res = service.promote_batch([procedural_remove_batch], reviewer="admin", automatic=False)
    assert len(batch_res) == 1
    assert batch_res[0].promoted is False
    assert batch_res[0].reason_code == "LESSON_ACTION_UNSUPPORTED"
    assert not list(req_dir.glob("*.json")) if req_dir.exists() else True


def test_effective_config_honors_explicit_overrides_and_post_init_mutations(tmp_path: Path) -> None:
    from agent_py_agent.agent.settings.config import AgentConfig

    owner, service, conversations, _thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)
    agent_cfg = AgentConfig()
    # 构造与 AgentConfig 默认值不同的显式配置（manual_only + max_retries=0），同时挂真实 config_provider（含 config_sources）。
    explicit_cfg = MemoryCuratorConfig(
        interval_seconds=60,
        turn_threshold=1,
        timeout_seconds=2,
        max_retries=0,
        auto_promotion_policy="manual_only",
    )
    cand = None
    for idx in (1, 2):
        cand = _observe_tool_lesson(
            candidates,
            content="导出报告统一使用 UTF-8 编码。",
            subject_key="export.encoding",
            occurrence_index=idx,
        )
    assert cand is not None
    assert cand.status == "pending_review"
    curator = MemoryCuratorService(
        config=explicit_cfg,
        dependencies=MemoryCuratorDependencies(
            backend=_FakeStructuredBackend({"daily": {}, "candidates": []}),
            conversation_store=conversations,
            audit_dir=owner / "audit",
            state_store=MemoryCuratorStateStore(owner / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(owner / "memory" / "daily"),
            candidate_service=candidates,
            run_log=CuratorRunLog(owner / "memory" / "curator" / "runs.jsonl"),
            identity=MemoryCuratorIdentity(provider="", model="", owner_id="owner-w8", timezone_name="UTC"),
            formal_memory_source=FormalMemorySource(long_term=long_term, lessons=lessons, hot=hot),
            promotion_callback=lambda cid: service.promote(cid, automatic=True),
            config_provider=lambda: agent_cfg,
        ),
    )

    # 1. 初始化覆盖字段保持 manual_only 与 max_retries=0；未固定字段仍可随 live agent_cfg 变更生效。
    eff0 = curator._effective_config()
    assert eff0.auto_promotion_policy == "manual_only"
    assert eff0.max_retries == 0
    assert curator._promote_eligible([cand.candidate_id]) == (0, (), ())
    assert candidates.get(cand.candidate_id).status == "pending_review"

    agent_cfg.memory_curator_enabled = False
    assert curator._effective_config().enabled is False
    agent_cfg.memory_curator_enabled = True
    assert curator._effective_config().enabled is True

    # 对已固定字段若运行期真实发生热变更（不同于初始化时的 provider 值），仍能生效。
    agent_cfg.memory_curator_auto_promotion_policy = "auto"
    assert curator._effective_config().auto_promotion_policy == "auto"

    # 2. 构造后直接修改 curator.config 覆盖 config_provider，还原后恢复跟随 live provider。
    agent_cfg.memory_curator_auto_promotion_policy = "conservative_v1"
    curator2 = MemoryCuratorService(
        config=MemoryCuratorConfig.from_agent_config(AgentConfig()),
        dependencies=replace(curator.dependencies, config_provider=lambda: agent_cfg),
    )
    orig_cfg = curator2.config
    curator2.config = replace(orig_cfg, auto_promotion_policy="manual_only")
    assert curator2._effective_config().auto_promotion_policy == "manual_only"
    assert curator2._promote_eligible([cand.candidate_id]) == (0, (), ())

    curator2.config = orig_cfg
    agent_cfg.memory_curator_auto_promotion_policy = "auto"
    assert curator2._effective_config().auto_promotion_policy == "auto"
    promoted, _warnings, _receipts = curator2._promote_eligible([cand.candidate_id])
    assert promoted == 1
    assert candidates.get(cand.candidate_id).status == "promoted"


def test_auto_promotion_blocks_unlinked_hot_rule_without_keyerror(tmp_path: Path) -> None:
    _owner, service, conversations, thread, candidates, _long_term, _lessons, hot = _setup_owner(tmp_path)
    msg_ref = _user_message_ref(conversations, thread, "必须先跑验证再合并代码。")
    unlinked = candidates.observe(
        CandidateObservation(
            candidate_type="hot_rule",
            content="必须先跑验证再合并代码。",
            subject_key="hot.unlinked",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(msg_ref,),
            learning_source=ls.user_dialogue_projection(),
            proposed_action="add",
            target_entry_id="",
            promotion_target="hot",
            confidence=0.95,
        )
    )
    batch_unlinked = service.promote_batch([unlinked], reviewer="memory-promotion-service", automatic=True)
    assert len(batch_unlinked) == 1
    assert batch_unlinked[0].promoted is False
    assert batch_unlinked[0].reason_code == "AUTO_POLICY_EXACT_TARGET_REQUIRED"

    # 即使携带非空但不存在的 lesson_id 走自动晋升，也不得在 promote_batch 内触发 KeyError / PROMOTION_ITEM_FAILED。
    missing_obs = CandidateObservation(
        candidate_type="hot_rule",
        content="必须先校验目标条目是否存在。",
        subject_key="hot.missing.target",
        scope=MemoryScope("personal", "personal"),
        origin="user_explicit",
        source_message_refs=(msg_ref,),
        source_task_ids=("ht-m-1",),
        source_run_ids=("hr-m-1",),
        learning_source=ls.user_dialogue_projection(),
        proposed_action="add",
        target_entry_id="lesson-nonexistent",
        promotion_target="hot",
        confidence=0.95,
    )
    missing_cand = candidates.observe(missing_obs)
    for idx in (2, 3):
        missing_cand = candidates.observe(
            replace(
                missing_obs,
                source_task_ids=(f"ht-m-{idx}",),
                source_run_ids=(f"hr-m-{idx}",),
                observation_id=f"obs-m-{idx}",
            )
        )
    batch_missing = service.promote_batch([missing_cand], reviewer="memory-promotion-service", automatic=True)
    assert len(batch_missing) == 1
    assert batch_missing[0].promoted is False
    assert batch_missing[0].reason_code == "TARGET_LESSON_NOT_FOUND"
    assert hot.list() == []


def test_curator_and_remember_redact_credentials_in_message_quotes_and_verify(tmp_path: Path) -> None:
    from agent_py_agent.agent.capability.memory_tool import _current_user_message_ref

    synthetic_sk = "sk-test-synthetic00000000000000000000"
    synthetic_bearer = "Bearer eyJsyntheticTokenValue0000123456"
    assert not scan_memory_content(f"key={synthetic_sk}").safe
    assert not scan_memory_content(f"auth={synthetic_bearer}").safe

    owner, service, conversations, thread, candidates, long_term, _lessons, _hot = _setup_owner(tmp_path)
    secret_prose = f"每周三执行灰度发布；凭证 {synthetic_sk} 与 {synthetic_bearer} 不可落盘。"
    msg = conversations.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": secret_prose,
            "now": 25.0,
            "metadata": {"gateway_request_id": "req-redaction-1"},
        }
    )
    digest = "sha256:" + hashlib.sha256(secret_prose.encode("utf-8")).hexdigest()
    msg_input = CuratorMessageInput(
        message_id=msg.message_id,
        thread_id=thread.thread_id,
        role="user",
        channel="internal",
        created_at=25.0,
        full_content=secret_prose,
        content_preview=redact_memory_quote(secret_prose),
        content_hash=digest,
        metadata={"gateway_request_id": "req-redaction-1"},
        thread_source=ls.user_dialogue_projection(),
    )
    batch_in = CuratorInputBatch(messages=(msg_input,), audit_events=(), formal_memories=())
    extraction = CuratorExtraction(
        daily_events=(
            CuratorDailyDraft(
                event_type="preference",
                summary=f"记录发布窗口，忽略 {synthetic_sk}",
                actor="user",
                origin="user_explicit",
                message_refs=({"thread_id": thread.thread_id, "message_id": msg.message_id},),
                decisions=(f"清理 {synthetic_bearer}",),
            ),
        ),
        candidates=(
            CandidateObservation(
                candidate_type="long_term_fact",
                content="每周三执行灰度发布。",
                subject_key="release.cadence",
                scope=MemoryScope("personal", "personal"),
                origin="user_explicit",
                source_message_refs=({"thread_id": thread.thread_id, "message_id": msg.message_id},),
                learning_source=ls.user_dialogue_projection(),
                proposed_action="add",
                promotion_target="long_term",
                confidence=0.95,
            ),
        ),
        processed_message_refs=({"thread_id": thread.thread_id, "message_id": msg.message_id},),
        processed_audit_refs=(),
        unresolved_refs=(),
        warnings=(),
        next_cursor={},
    )
    validated = validate_extraction(extraction, batch_in, owner_id="owner-w8")
    assert synthetic_sk not in validated.daily_events[0].summary
    assert synthetic_bearer not in validated.daily_events[0].decisions[0]
    assert "[REDACTED_CREDENTIAL]" in validated.candidates[0].source_message_refs[0]["quote"]

    cand = candidates.observe(replace(validated.candidates[0], learning_source=ls.user_dialogue_projection()))
    res = service.promote_batch([cand], reviewer="memory-promotion-service", automatic=True)
    assert res[0].promoted is True
    assert len(long_term.all()) == 1

    # Remember 工具回填当前消息引用同样脱敏，且通过 ConversationMessageEvidenceVerifier 校验。
    mock_agent = SimpleNamespace(
        conversation_store=conversations,
        _current_run_params=SimpleNamespace(
            request_id="req-redaction-1",
            task_attributes={"conversation_thread_id": thread.thread_id},
        ),
    )
    tool_ref = _current_user_message_ref(mock_agent, "非原文子串触发截取")
    assert tool_ref is not None
    assert "[REDACTED_CREDENTIAL]" in str(tool_ref["quote"])
    assert synthetic_sk not in str(tool_ref["quote"])
    assert "eyJsyntheticTokenValue0000123456" not in str(tool_ref["quote"])
    assert service.message_verifier.verify(tool_ref) is not None

    # 扫描 owner/memory 下全部持久文件，绝不能落盘任何原始凭据。
    for path in (owner / "memory").rglob("*"):
        if path.is_file():
            raw = path.read_text(encoding="utf-8", errors="replace")
            assert synthetic_sk not in raw, f"raw secret leaked in {path}"
            assert "eyJsyntheticTokenValue0000123456" not in raw, f"raw bearer token leaked in {path}"


def test_parse_curator_extraction_fills_omitted_nullable_scope_conditions() -> None:
    payload = {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [
            {
                "candidate_type": "long_term_fact",
                "content": "Project Alpha deploys after 20:00 UTC.",
                "subject_key": "alpha.release.window",
                "scope": {"scope_type": "project", "scope_key": "project:alpha"},
                "origin": "user_explicit",
                "source_message_refs": [{"message_id": "msg-1"}],
                "source_tool_refs": [],
                "source_artifact_refs": [],
                "confidence": 0.9,
                "proposed_action": "add",
                "conflicts_with": [],
                "promotion_target": "long_term",
            }
        ],
        "processed_message_refs": [{"message_id": "msg-1"}],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {"per_thread_cursors": [{"thread_id": "t-1", "message_id": "msg-1"}], "last_audit_event_id": None},
    }
    parsed = parse_curator_extraction(json.dumps(payload))
    assert len(parsed.candidates) == 1
    assert parsed.candidates[0].scope.scope_type == "project"
    assert parsed.candidates[0].scope.applies_when == ""
    assert parsed.candidates[0].scope.excludes_when == ""

    del payload["candidates"][0]["scope"]
    with pytest.raises(CuratorOutputViolation) as exc_info:
        parse_curator_extraction(json.dumps(payload))
    assert exc_info.value.violation_code == "missing_property"
    assert "$.candidates[0].scope" in exc_info.value.violation_path


def test_curator_classifies_candidate_and_authority_corruption_without_blaming_model(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_store.curator import _failure_code
    from agent_py_agent.agent.memory_store.curator_models import curator_replay_deterministic
    from agent_py_agent.agent.memory_store.jsonl import MemoryAuthorityCorruptError

    owner, _service, conversations, thread, candidates, long_term, lessons, hot = _setup_owner(tmp_path)
    msg = conversations.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "出现本地账本损坏时必须精确暴露真实故障码。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-m2-corrupt",
                "request_id": "request-m2-corrupt",
                "task_id": "task-m2-corrupt",
                "run_id": "run-m2-corrupt",
            },
            "now": 30.0,
        }
    )
    candidates.path.parent.mkdir(parents=True, exist_ok=True)
    with candidates.path.open("a", encoding="utf-8") as fh:
        fh.write("{corrupt jsonl line\n")

    backend = _FakeStructuredBackend(_conversation_event_response(msg.message_id))
    curator = _curator_service(
        owner,
        conversations,
        candidates,
        long_term,
        lessons,
        hot,
        backend=backend,
        config=MemoryCuratorConfig(interval_seconds=60, turn_threshold=1),
    )
    res = curator.run(reason="task_complete", force=True)
    assert res.status == "failed"
    assert res.failure_code == "CURATOR_INPUT_CANDIDATES_CORRUPT"
    assert res.failure_code != "CURATOR_MODEL_FAILED"
    assert backend.calls == 0
    assert curator.state_store.load().per_thread_cursors == {}
    assert curator_replay_deterministic(res.failure_code) is True

    assert _failure_code(MemoryAuthorityCorruptError("corrupt index")) == "CURATOR_INPUT_AUTHORITY_CORRUPT"
    assert curator_replay_deterministic("CURATOR_INPUT_AUTHORITY_CORRUPT") is True
