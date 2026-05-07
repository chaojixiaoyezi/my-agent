
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


@dataclass
class DispatchParams:

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None


@dataclass
class WatchParams:

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    interval: float = 30.0
    max_cycles: int = 0
    force_lock: bool = False
    stop_file: str | Path | None = None


DISPATCH_PARAM_KEYS = tuple(field.name for field in fields(DispatchParams))
WATCH_PARAM_KEYS = tuple(field.name for field in fields(WatchParams))


def merge_dispatch_params(
    params: DispatchParams | None = None,
    kwargs: dict[str, Any] | None = None,
) -> DispatchParams:
    if params is None:
        params = DispatchParams()
    elif not isinstance(params, DispatchParams):
        raise TypeError("dispatch_subagents() requires params: DispatchParams keyword argument")
    return _replace_bundle(params, DISPATCH_PARAM_KEYS, kwargs or {})


def merge_watch_params(
    params: WatchParams | None = None,
    kwargs: dict[str, Any] | None = None,
) -> WatchParams:
    if params is None:
        params = WatchParams()
    elif not isinstance(params, WatchParams):
        raise TypeError("watch_subagents() requires params: WatchParams keyword argument")
    return _replace_bundle(params, WATCH_PARAM_KEYS, kwargs or {})


def dispatch_params_from_watch(params: WatchParams) -> DispatchParams:
    return DispatchParams(
        **{key: getattr(params, key) for key in DISPATCH_PARAM_KEYS}
    )


def _replace_bundle(params, allowed_keys: tuple[str, ...], kwargs: dict[str, Any]):
    overrides = {key: kwargs[key] for key in allowed_keys if key in kwargs}
    if not overrides:
        return params
    return replace(params, **overrides)


@dataclass
class DispatchContext:

    cfg: Any
    normalized_workflow_mode: str
    apply: bool
    planner: bool
    runner_instruction: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    take_over_by: str
    locked_files: list[str] | None
    router: Any  # CapabilityRouter – forward ref to avoid circular import at module level
    records: list = field(default_factory=list)


@dataclass
class RunnerBatchContext:

    pending_runner_jobs: list
    runner_concurrency: int
    runner_timeout_seconds: float
    effective_runner_instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    records: list
