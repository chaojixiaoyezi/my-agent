
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentRunParams:
    run_id: str
    instruction: str = ""
    dry_run: bool = True
    max_cards: int = 0
    probe: bool = True
    retry_reason: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class SpawnSubagentsParams:
    goal: str
    count: int | None = None
    role: str = "worker"
    agent_name: str = ""
    extra_write_roots: list[str] | None = None


@dataclass(frozen=True)
class SubagentProbeParams:
    run_id: str
    active_attempt_id: str
    max_cards: int
    instruction: str
    probe: bool


@dataclass(frozen=True)
class SubagentRunFailureParams:
    run_id: str
    active_attempt_id: str
    exc: Exception
    context: object
    prompt: str


@dataclass(frozen=True)
class SubagentFinalizeParams:
    run_id: str
    active_attempt_id: str
    result: object
    context: object
    prompt: str


def subagent_run_params(
    params: SubagentRunParams | None,
    *,
    run_id: str | None,
    instruction: str = "",
    dry_run: bool = True,
    max_cards: int = 0,
    probe: bool = True,
    retry_reason: str = "",
    attempt_id: str = "",
) -> SubagentRunParams:
    if params is not None:
        if not isinstance(params, SubagentRunParams):
            raise TypeError("run_subagent() requires params: SubagentRunParams")
        return params
    return SubagentRunParams(
        run_id=str(run_id or ""),
        instruction=str(instruction),
        dry_run=bool(dry_run),
        max_cards=int(max_cards),
        probe=bool(probe),
        retry_reason=str(retry_reason),
        attempt_id=str(attempt_id),
    )


def spawn_subagents_params(
    params: SpawnSubagentsParams | None,
    *,
    goal: str | None,
    count: int | None,
    role: str = "worker",
    agent_name: str = "",
    extra_write_roots: list[str] | None = None,
) -> SpawnSubagentsParams:
    if params is not None:
        if not isinstance(params, SpawnSubagentsParams):
            raise TypeError("spawn_subagents() requires params: SpawnSubagentsParams")
        return params
    return SpawnSubagentsParams(
        goal=str(goal or ""),
        count=count,
        role=str(role or "worker"),
        agent_name=str(agent_name or ""),
        extra_write_roots=list(extra_write_roots or []),
    )
