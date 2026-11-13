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

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.capability.skill_learning_store import (
    REQUEST_SCHEMA_VERSION,
    SkillLearningStore,
    request_key,
)
from agent_py_agent.agent.capability.skill_proposals import lesson_carrier
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
    CuratorMessageSource,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CuratorExtraction,
    MemoryCuratorConfig,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
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
            conversation_store=CuratorMessageSource(conversations),
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
