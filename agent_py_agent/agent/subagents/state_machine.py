# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""LLM contract: subagent state machine with structural transitions only.

Human version:
这个模块定义子代理状态机，确保状态转换的合法性和一致性。
状态文件格式必须保持向后兼容。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SubAgentTask


# LLM: SubAgentState 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 封装subagent状态相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentState(str, Enum):
    """Canonical subagent task states."""

    PLANNING = "PLANNING"  # Created, not yet started
    RUNNING = "RUNNING"  # Actively executing
    WAIT_CHILD = "WAIT_CHILD"  # Waiting for child tasks
    PENDING = "PENDING"  # Queued, waiting for resources
    PAUSED = "PAUSED"  # User paused, can resume
    DONE = "DONE"  # Successfully completed
    FAILED = "FAILED"  # Exhausted all strategies
    BLOCKED = "BLOCKED"  # Blocked by external condition


# LLM: State transitions are structural only; quality/artifact checks stay in closeout.
# Each tuple is (from_state, to_state, trigger, description)
_SUBAGENT_TRANSITIONS = [
    # From PLANNING
    ("PLANNING", "RUNNING", "start", "Begin task execution"),
    ("PLANNING", "PAUSED", "pause", "User pauses before start"),
    ("PLANNING", "ABANDONED", "abandon", "User abandons before start"),
    # From RUNNING
    ("RUNNING", "WAIT_CHILD", "wait_child", "Dispatched subagent, waiting"),
    ("RUNNING", "DONE", "complete", "Task execution reached a done state"),
    ("RUNNING", "FAILED", "fail", "All strategies exhausted"),
    ("RUNNING", "BLOCKED", "block", "External dependency not met"),
    ("RUNNING", "PAUSED", "pause", "User pauses mid-execution"),
    # From WAIT_CHILD
    ("WAIT_CHILD", "RUNNING", "child_done", "Child tasks completed"),
    ("WAIT_CHILD", "FAILED", "child_failed", "Child task failed"),
    ("WAIT_CHILD", "BLOCKED", "block", "Child blocked or timeout"),
    # From PENDING
    ("PENDING", "RUNNING", "dispatch", "Resources available, dispatch"),
    # Terminal states
    ("DONE", "RUNNING", "retry", "Retry after done (exceptional)"),
    ("FAILED", "RUNNING", "retry", "Retry after failure"),
    ("BLOCKED", "RUNNING", "unblock", "Blocked condition resolved"),
    ("PAUSED", "RUNNING", "resume", "User resumes task"),
    ("PAUSED", "FAILED", "abandon", "User abandons paused task"),
]


_SUBAGENT_STATE_INDEX = {
    (t[0], t[2]): t for t in _SUBAGENT_TRANSITIONS
}

# Valid state transitions grouped by trigger
_TRANSITIONS_BY_TRIGGER = {
    "start": ["PLANNING"],
    "pause": ["PLANNING", "RUNNING"],
    "abandon": ["PLANNING", "PAUSED"],
    "wait_child": ["RUNNING"],
    "complete": ["RUNNING"],
    "fail": ["RUNNING"],
    "block": ["RUNNING", "WAIT_CHILD"],
    "child_done": ["WAIT_CHILD"],
    "child_failed": ["WAIT_CHILD"],
    "dispatch": ["PENDING"],
    "retry": ["DONE", "FAILED", "BLOCKED"],
    "unblock": ["BLOCKED"],
    "resume": ["PAUSED"],
}


# LLM: SubAgentStateMachine 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 封装subagent状态machine相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentStateMachine:
    """State machine for subagent task lifecycle.

    Enforces valid state transitions and maintains transition audit log.
    """

    # LLM: __init__ 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def __init__(self, manager):
        self.manager = manager

    # LLM: get_valid_transitions 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询validtransitions需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def get_valid_transitions(self, from_state: str) -> list[dict]:
        """Get all valid transitions from a given state."""

        valid = []
        for from_s, to_s, trigger, description in _SUBAGENT_TRANSITIONS:
            if from_s == from_state:
                valid.append({
                    "from": from_s,
                    "to": to_s,
                    "trigger": trigger,
                    "description": description,
                })
        return valid

    # LLM: can_transition 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 判断transition条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def can_transition(self, from_state: str, trigger: str) -> bool:
        """Check if a transition is valid."""

        key = (from_state.upper(), trigger.lower())
        return key in _SUBAGENT_STATE_INDEX

    # LLM: transition 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理transition相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def transition(
        self,
        run_id: str,
        trigger: str,
        *,
        params: object | None = None,
    ) -> SubAgentTask:
        """Execute a state transition on a task.

        Returns the updated task after transition.
        Raises ValueError if transition is invalid.
        """

        task = self.manager.load(run_id)
        current = task.status.upper()
        trigger_lower = trigger.lower()

        # Look up the transition
        key = (current, trigger_lower)
        if key not in _SUBAGENT_STATE_INDEX:
            self._raise_invalid_transition(current, trigger_lower)

        trans = _SUBAGENT_STATE_INDEX[key]
        new_state = trans[1]

        # Keep this machine structural. Quality/artifact acceptance is handled
        # by the shared closeout path so every task depth uses one mechanism.
        _ = params

        old_state = self._apply_transition_state(task, new_state, trigger_lower, run_id)
        self.manager.save(task)
        self._append_transition_log(task, old_state, trigger_lower, new_state)

        return task

    # LLM: _raise_invalid_transition 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理raiseinvalidtransition相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _raise_invalid_transition(self, current: str, trigger_lower: str) -> None:
        valid = [t["trigger"] for t in self.get_valid_transitions(current)]
        raise ValueError(
            f"Invalid transition: {current} --{trigger_lower}--> ? "
            f"(valid triggers: {valid})"
        )

    # LLM: _apply_transition_state 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新transition状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _apply_transition_state(
        self,
        task: SubAgentTask,
        new_state: str,
        trigger_lower: str,
        run_id: str,
    ) -> str:
        from .models import StateTransitionRecord

        old_state = task.status
        task.status = new_state
        task.updated_at = self.manager._clock()
        task.state_transitions.append(
            StateTransitionRecord(
                from_state=old_state,
                to_state=new_state,
                trigger=trigger_lower,
                run_id=run_id,
                created_at=task.updated_at,
            )
        )
        return old_state

    # LLM: _append_transition_log 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入transitionlog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _append_transition_log(
        self,
        task: SubAgentTask,
        old_state: str,
        trigger_lower: str,
        new_state: str,
    ) -> None:
        self.manager._append_task_work_log(
            task,
            f"state_machine: {old_state} --{trigger_lower}--> {new_state}",
        )

    # LLM: get_state_summary 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询状态summary需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def get_state_summary(self, run_id: str) -> dict:
        """Get a summary of task state and valid transitions."""

        task = self.manager.load(run_id)
        current = task.status.upper()
        valid = self.get_valid_transitions(current)

        return {
            "run_id": run_id,
            "current_state": current,
            "valid_transitions": valid,
            "transition_count": len(task.state_transitions),
            "last_transition": task.state_transitions[-1] if task.state_transitions else None,
        }
