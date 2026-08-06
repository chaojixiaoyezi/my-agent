from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository


def formal_memory_agent(tmp_path: Path):
    owner = tmp_path / "owner"
    memory_root = owner / "memory"
    # 模拟正式 v2 环境(迁移已 complete):正式区内再出现无 marker 的 lesson 文件即损坏,
    # 读路径必须 fail-closed(防模型拿到被破坏的正式记忆);升级前 home 无此 marker,
    # legacy 文件走迁移自愈路径。
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "migration.json").write_text(
        json.dumps(
            {
                "schema_version": "my-agent.memory-migration.v2",
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )
    candidates = CandidateService(memory_root / "candidates.jsonl")
    lessons = LessonRepository(
        memory_root / "lessons",
        memory_root / "routing" / "INDEX.md",
    )
    hot = HotRuleRepository(owner / "memory-hot.md")
    long_term = JsonlMemory(memory_root / "long_term" / "memory.jsonl")
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(
            owner_home_dir=owner,
            owner_memory_routing_index_md=memory_root / "routing" / "INDEX.md",
        ),
        config=SimpleNamespace(
            memory_rule_routing_enabled=True,
            memory_rule_routing_mode="soft",
            memory_rule_auto_read_limit=3,
            home_lesson_stale_caveat_days=7.0,
        ),
        memory=long_term,
        memory_candidates=candidates,
        memory_lessons=lessons,
        memory_hot=hot,
    )
    return agent


def promote_lesson(
    agent,
    *,
    content: str = "规划或失败后先验证真实错误证据，再调整参数。",
    subject_key: str = "planning.failure.validation",
    scope_type: str = "global",
    scope_key: str = "global",
):
    observations = []
    for index in (1, 2):
        observations.append(
            CandidateObservation(
                candidate_type="lesson",
                content=content,
                subject_key=subject_key,
                scope=MemoryScope(scope_type, scope_key),
                origin="subagent_lesson",
                evidence_refs=({"source_ref": f"evidence-{index}"},),
                source_task_ids=(f"task-{index}",),
                source_run_ids=(f"run-{index}",),
                proposed_action="add",
                promotion_target="lesson",
                observation_id=f"lesson-observation:{subject_key}:{index}",
            )
        )
    candidate = agent.memory_candidates.observe_many(observations)[-1]
    candidate = agent.memory_candidates.transition(
        candidate.candidate_id,
        "approved",
        reviewer="test-reviewer",
        review_note="formal lesson fixture",
    )
    return agent.memory_lessons.promote(candidate)


def promote_hot(agent, lesson, *, rule: str = "失败后先核对真实证据。"):
    observations = []
    for index in (1, 2, 3):
        observations.append(
            CandidateObservation(
                candidate_type="hot_rule",
                content=rule,
                subject_key=f"hot.{lesson.subject_key}",
                scope=MemoryScope.from_value(lesson.scope),
                origin="reviewed",
                evidence_refs=({"source_ref": f"hot-evidence-{index}"},),
                source_task_ids=(f"hot-task-{index}",),
                source_run_ids=(f"hot-run-{index}",),
                proposed_action="add",
                promotion_target="hot",
                target_entry_id=lesson.lesson_id,
                observation_id=f"hot-observation:{lesson.lesson_id}:{index}",
            )
        )
    candidate = agent.memory_candidates.observe_many(observations)[-1]
    candidate = agent.memory_candidates.transition(
        candidate.candidate_id,
        "approved",
        reviewer="test-reviewer",
        review_note="formal HOT fixture",
    )
    return agent.memory_hot.promote(candidate, lesson=lesson)
