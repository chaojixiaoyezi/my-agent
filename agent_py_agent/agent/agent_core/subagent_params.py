from __future__ import annotations

from dataclasses import dataclass


# LLM: shared subagent runner bundles live outside mixins to avoid core import cycles.
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
    kwargs: dict[str, object],
) -> SubagentRunParams:
    if params is not None:
        if not isinstance(params, SubagentRunParams):
            raise TypeError("run_subagent() requires params: SubagentRunParams")
        return params
    # LLM: run_subagent keeps legacy kwargs but core code consumes a single run bundle.
    return SubagentRunParams(
        run_id=str(run_id or kwargs["run_id"]),
        instruction=str(kwargs.get("instruction", "")),
        dry_run=bool(kwargs.get("dry_run", True)),
        max_cards=int(kwargs.get("max_cards", 0)),
        probe=bool(kwargs.get("probe", True)),
        retry_reason=str(kwargs.get("retry_reason", "")),
        attempt_id=str(kwargs.get("attempt_id", "")),
    )


def spawn_subagents_params(
    params: SpawnSubagentsParams | None,
    *,
    goal: str | None,
    count: int | None,
) -> SpawnSubagentsParams:
    if params is not None:
        if not isinstance(params, SpawnSubagentsParams):
            raise TypeError("spawn_subagents() requires params: SpawnSubagentsParams")
        return params
    # LLM: spawn_subagents keeps the old goal/count shape but immediately normalizes it.
    return SpawnSubagentsParams(goal=str(goal or ""), count=count)
