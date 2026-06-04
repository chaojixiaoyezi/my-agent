
"""LLM contract: subagent state machine with structural transitions only.

Human version:
这个模块定义子代理状态机，确保状态转换的合法性和一致性。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SubAgentTask


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


class SubAgentStateMachine:
    """State machine for subagent task lifecycle.

    Enforces valid state transitions and maintains transition audit log.
    """

    def __init__(self, manager):
        self.manager = manager

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

    def can_transition(self, from_state: str, trigger: str) -> bool:
        """Check if a transition is valid."""

        key = (from_state.upper(), trigger.lower())
        return key in _SUBAGENT_STATE_INDEX

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

    def _raise_invalid_transition(self, current: str, trigger_lower: str) -> None:
        valid = [t["trigger"] for t in self.get_valid_transitions(current)]
        raise ValueError(
            f"Invalid transition: {current} --{trigger_lower}--> ? "
            f"(valid triggers: {valid})"
        )

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
