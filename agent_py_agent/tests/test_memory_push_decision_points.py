"""记忆推模式决策点钉子(开发计划 B1+B2)。

钉死两层契约:
1. B1 planner 注入:父代理 planner 出决策前自动注入 planning 类教训
   (append_planner_memory_hint),不依赖模型主动查;检索失败/无 memory 软容错,
   绝不阻断 planner。注入点覆盖从"仅 runner 失败点"扩展到 planner 决策点
   (memory feedback_memory_push_mode 的核心架构诉求)。
2. B2 幂等:planner 注入与 runner 失败注入都不重复堆叠同一批教训
   (失败重试循环 / 重复构造 prompt 时,同一 hint 只出现一次)。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core.planner_service import (
    _planner_memory_goal,
    append_planner_memory_hint,
)
from agent_py_agent.agent.agent_core.runner.gate import _append_memory_hint
from agent_py_agent.agent.memory_store import JsonlMemory


class _MemoryAgent:
    """最小真实 agent 替身:只带真实 JsonlMemory(被测对象是注入链路本身)。"""

    def __init__(self, memory: JsonlMemory | None) -> None:
        self.memory = memory


def _memory_with_planning_lesson(tmp_path: Path) -> JsonlMemory:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add(
        role="system",
        content="规划拆分大任务时先按模块边界分组,再按文件依赖排序派工",
        kind="lesson_general",
        tags=["planning", "dispatch"],
    )
    return memory


_STATE = {
    "due_issues": [{"run_id": "r1", "goal": "规划拆分大任务", "kind": "stalled"}],
    "active_tasks": [{"id": "r2", "goal": "备用目标"}],
}


# ---------------------------------------------------------------------------
# B1:planner 决策点注入
# ---------------------------------------------------------------------------


def test_planner_prompt_gets_relevant_memory_injected(tmp_path: Path) -> None:
    agent = _MemoryAgent(_memory_with_planning_lesson(tmp_path))
    prompt = append_planner_memory_hint(agent, _STATE, "# Parent Planner Tick\n...")
    assert "[相关记忆提示]" in prompt
    assert "规划拆分大任务" in prompt
    # 原 prompt 内容保留在前,记忆附在尾部
    assert prompt.startswith("# Parent Planner Tick")


def test_planner_injection_is_soft_when_memory_missing(tmp_path: Path) -> None:
    # 无 memory(None)→ 原样返回,不抛、不改 prompt
    assert append_planner_memory_hint(_MemoryAgent(None), _STATE, "P") == "P"
    # 空 memory(真实文件,无相关记录)→ 同样原样返回
    empty_agent = _MemoryAgent(JsonlMemory(tmp_path / "empty.jsonl"))
    assert append_planner_memory_hint(empty_agent, _STATE, "P") == "P"


def test_planner_injection_survives_search_exception(tmp_path: Path) -> None:
    class _BrokenMemory:
        def search(self, query: str, top_k: int = 5):
            raise RuntimeError("索引损坏")

    # 检索抛异常 → 软容错原样返回(绝不阻断 planner 决策)
    assert append_planner_memory_hint(_MemoryAgent(_BrokenMemory()), _STATE, "P") == "P"


def test_planner_memory_goal_prefers_due_issue_goal() -> None:
    assert _planner_memory_goal(_STATE) == "规划拆分大任务"
    assert _planner_memory_goal({"active_tasks": [{"goal": "备用目标"}]}) == "备用目标"
    assert _planner_memory_goal({}) == ""
    # 脏数据容错:非 dict 条目跳过
    assert _planner_memory_goal({"due_issues": ["garbage", {"goal": "g"}]}) == "g"


# ---------------------------------------------------------------------------
# B2:注入幂等(planner + runner 失败路径)
# ---------------------------------------------------------------------------


def test_planner_injection_is_idempotent(tmp_path: Path) -> None:
    agent = _MemoryAgent(_memory_with_planning_lesson(tmp_path))
    once = append_planner_memory_hint(agent, _STATE, "# Tick\n")
    twice = append_planner_memory_hint(agent, _STATE, once)
    assert twice == once, "同一 hint 已在 prompt 里时不得重复追加"
    assert twice.count("[相关记忆提示]") == 1


def test_failure_memory_hint_does_not_stack_on_retry_loop() -> None:
    hint = "[相关记忆提示]\n1. [lesson_general] 超时先调参再重试"
    instruction = _append_memory_hint("继续推进任务", hint)
    assert instruction.count("[相关记忆提示]") == 1
    # 重试循环里同一教训再注入 → 不堆叠
    again = _append_memory_hint(instruction, hint)
    assert again == instruction
    assert again.count("[相关记忆提示]") == 1


def test_failure_memory_hint_handles_empty_inputs() -> None:
    assert _append_memory_hint("指令", "") == "指令"
    assert _append_memory_hint("", "hint") == "hint"
