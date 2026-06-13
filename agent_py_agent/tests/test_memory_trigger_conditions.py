"""P5-2 教训触发条件钉子(REFACTORING_BACKLOG 最后一个 Active 暂缓项收口)。

钉死四层契约:
1. 持久化:write_memory_with_type 的 trigger_conditions 随 JSONL 主事实落盘
   (attributes 扩展位),旧行无该键读取兼容,空 attributes 不写键。
2. 匹配语义(全结构化,零自然语言解析):列表=任一命中、min_ 前缀=数值阈值、
   标量=相等;全部声明键满足才 matched;空条件永不 matched。
3. 消费提权:推送时条件匹配的记忆排最前(软提权),不匹配/未声明的不淘汰。
4. 生产端:自省调参后自动写带条件教训(failure_type+min_attempts)。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_push import (
    MemoryType,
    MemoryWriteContext,
    push_relevant_memories,
    trigger_conditions_match,
    write_memory_with_type,
)
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. 持久化
# ---------------------------------------------------------------------------


def test_trigger_conditions_persist_through_jsonl(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    write_memory_with_type(
        memory,
        MemoryWriteContext(
            content="大文件任务超时三次后把超时翻倍才跑通",
            mem_type=MemoryType.LESSON_TASK,
            trigger_type="failure",
            lesson="超时×3 → 调 2x",
            trigger_conditions={"failure_type": "runner_timeout", "min_attempts": 3},
        ),
    )

    lines = [json.loads(line) for line in (tmp_path / "memory.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["attributes"]["trigger_conditions"] == {"failure_type": "runner_timeout", "min_attempts": 3}
    reloaded = memory.all()[0]
    assert reloaded.attributes["trigger_conditions"]["min_attempts"] == 3


def test_records_without_conditions_stay_clean(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "普通记忆一条")
    line = json.loads((tmp_path / "memory.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "attributes" not in line, "无扩展字段的行保持旧格式"
    # 旧格式行(无 attributes 键)读取兼容
    assert memory.all()[0].attributes is None


# ---------------------------------------------------------------------------
# 2. 匹配语义
# ---------------------------------------------------------------------------


def test_match_semantics() -> None:
    conditions = {"trigger_type": ["failure", "timeout"], "failure_type": "runner_timeout", "min_attempts": 3}
    ctx_hit = {"failure_type": "runner_timeout", "attempts": 3}
    assert trigger_conditions_match(conditions, "failure", ctx_hit) is True
    assert trigger_conditions_match(conditions, "planning", ctx_hit) is False, "列表键不命中"
    assert trigger_conditions_match(conditions, "failure", {**ctx_hit, "attempts": 2}) is False, "数值阈值不足"
    assert trigger_conditions_match(conditions, "failure", {"failure_type": "other", "attempts": 5}) is False
    assert trigger_conditions_match({}, "failure", ctx_hit) is False, "空条件永不匹配(不提权)"
    assert trigger_conditions_match(None, "failure", ctx_hit) is False


# ---------------------------------------------------------------------------
# 3. 消费提权
# ---------------------------------------------------------------------------


def test_matched_lesson_is_prioritized(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    # 先写一条无条件的通用教训(文本相关性会命中)
    write_memory_with_type(
        memory,
        MemoryWriteContext(
            content="runner_timeout 失败时先看日志再重试,不要无脑重试同参数",
            mem_type=MemoryType.LESSON_GENERAL,
            trigger_type="failure",
            lesson="失败先看日志",
        ),
    )
    # 再写一条带条件的教训(写在后面,正常顺序靠后)
    write_memory_with_type(
        memory,
        MemoryWriteContext(
            content="runner_timeout 大任务连续超时,把 dynamic_timeout 翻倍后通过",
            mem_type=MemoryType.LESSON_TASK,
            trigger_type="failure",
            lesson="超时×3 调 2x",
            trigger_conditions={"failure_type": "runner_timeout", "min_attempts": 3},
        ),
    )
    agent = SimpleNamespace(memory=memory)

    texts = push_relevant_memories(
        agent,
        "failure",
        {"task_id": "t1", "goal": "处理 runner_timeout 大任务", "failure_type": "runner_timeout", "attempts": 3},
        limit=2,
    )

    assert texts, "应有记忆注入"
    assert "调 2x" in texts[0], "条件匹配的教训必须排第一"


# ---------------------------------------------------------------------------
# 4. 生产端:自省调参自动写教训
# ---------------------------------------------------------------------------


def test_introspection_records_conditioned_lesson(tmp_path: Path) -> None:
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.models import FailureType

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(goal="一个会超时的大任务", thought="t", plan=["p"])
    task.status = "TIMEOUT"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.runner_attempts = 1
    task.attributes["dynamic_timeout_seconds"] = 60.0
    agent.subagents.save(task)
    runner_result = SimpleNamespace(ok=False, status="TIMEOUT", message="runner timed out", failure_type=FailureType.RUNNER_TIMEOUT.value)

    agent._handle_failure_introspection(task.id, task, runner_result)

    lessons = [r for r in agent.memory.all() if isinstance(r.attributes, dict) and r.attributes.get("trigger_conditions")]
    assert lessons, "自省调参后必须留下带触发条件的教训"
    conditions = lessons[0].attributes["trigger_conditions"]
    assert conditions["failure_type"] == FailureType.RUNNER_TIMEOUT.value
    assert conditions["min_attempts"] >= 1


# ---------------------------------------------------------------------------
# 5. 检索教训播种(归因落地的另一半)
# ---------------------------------------------------------------------------


def test_research_lesson_seeded_into_new_home(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_memory_seeds import (
        default_memory_lessons,
        default_memory_route_index_md,
    )

    lessons = default_memory_lessons()
    assert "research.md" in lessons
    body = lessons["research.md"]
    # 2026-06-12 中文化大修(R12 实锤:英文 lesson 对中文任务模型执行率低)
    assert "窄口径" in body and "untried_channels_known" in body
    assert "署名形态" in body and "产品线" in body, "R12c 实锤的两条判定纪律必须在种子里"
    index = default_memory_route_index_md()
    assert "lessons.research" in index and "检索" in index
