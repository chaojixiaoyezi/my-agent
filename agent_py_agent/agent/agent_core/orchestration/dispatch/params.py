

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DispatchExecutionPlan:
    """Execution intent for one dispatch call."""

    preview_only: bool = True
    mutate_state: bool = False
    start_runners: bool = False
    max_runners: int = 1

    @classmethod
    def from_parts(
        cls,
        *,
        mutate_state: bool,
        start_runners: bool,
        max_runners: int,
    ) -> DispatchExecutionPlan:
        should_mutate = bool(mutate_state)
        should_start = bool(start_runners and should_mutate)
        return cls(
            preview_only=not should_mutate,
            mutate_state=should_mutate,
            start_runners=should_start,
            max_runners=max(0, int(max_runners or 0)),
        )


@dataclass(frozen=True)
class DispatchRuntimePolicy:
    """Frozen dispatch defaults derived from the loaded agent config."""

    max_consecutive_rounds: int = 20
    active_interval: float = 5.0
    idle_interval: float = 30.0
    default_max_runners: int = 1
    default_limit: int = 20
    default_watch_interval: float = 30.0
    source: str = "code-defaults"

    @classmethod
    def from_config(cls, config: object | None) -> DispatchRuntimePolicy:
        if config is None:
            return cls()
        return cls(
            max_consecutive_rounds=_non_negative_int_attr(config, "dispatch_max_consecutive_rounds", 20),
            active_interval=_non_negative_float_attr(config, "dispatch_active_interval", 5.0),
            idle_interval=_non_negative_float_attr(config, "dispatch_idle_interval", 30.0),
            default_max_runners=_non_negative_int_attr(config, "dispatch_default_max_runners", 1),
            default_limit=_non_negative_int_attr(config, "dispatch_default_limit", 20),
            default_watch_interval=_non_negative_float_attr(config, "dispatch_default_watch_interval", 30.0),
            source="agent-config",
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "dispatch_runtime_policy.v1",
            "source": self.source,
            "max_consecutive_rounds": self.max_consecutive_rounds,
            "active_interval": self.active_interval,
            "idle_interval": self.idle_interval,
            "default_max_runners": self.default_max_runners,
            "default_limit": self.default_limit,
            "default_watch_interval": self.default_watch_interval,
        }


@dataclass
class DispatchParams:
    """Dispatch controls after boundary normalization.

    New code should read ``execution_plan`` or the convenience properties below
    so execution intent has one authoritative source.
    """

    apply: bool = False
    start_runners: bool = False
    planner: bool = False
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    recovery_mode: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    parent_run_id: str = ""
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None
    background_launch_id: str = ""
    execution_plan: DispatchExecutionPlan | None = None

    def __post_init__(self) -> None:
        if self.execution_plan is None:
            self.execution_plan = DispatchExecutionPlan.from_parts(
                mutate_state=self.apply,
                start_runners=self.start_runners,
                max_runners=self.max_runners,
            )
            return
        self.apply = bool(self.execution_plan.mutate_state)
        self.start_runners = bool(self.execution_plan.start_runners)
        self.max_runners = int(self.execution_plan.max_runners)

    @property
    def preview_only(self) -> bool:
        return bool(self.execution_plan.preview_only)

    @property
    def mutate_state(self) -> bool:
        return bool(self.execution_plan.mutate_state)

    @property
    def should_start_runners(self) -> bool:
        return bool(self.execution_plan.start_runners)


@dataclass
class WatchParams(DispatchParams):

    interval: float = 30.0
    max_cycles: int = 0
    advance: bool = False
    force_lock: bool = False
    stop_file: str | Path | None = None


DISPATCH_PARAM_KEYS = tuple(field.name for field in fields(DispatchParams))
WATCH_PARAM_KEYS = tuple(field.name for field in fields(WatchParams))


def merge_dispatch_params(
    params: DispatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> DispatchParams:
    if params is None:
        params = DispatchParams()
    elif not isinstance(params, DispatchParams):
        raise TypeError("dispatch_subagents() requires params: DispatchParams keyword argument")
    updates = dict(overrides or {})
    if {"apply", "start_runners", "max_runners"} & set(updates) and "execution_plan" not in updates:
        updates["execution_plan"] = None
    return _replace_bundle(params, DISPATCH_PARAM_KEYS, updates)


def merge_watch_params(
    params: WatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> WatchParams:
    if params is None:
        params = WatchParams()
    elif not isinstance(params, WatchParams):
        raise TypeError("watch_subagents() requires params: WatchParams keyword argument")
    return _replace_bundle(params, WATCH_PARAM_KEYS, overrides or {})


def dispatch_params_from_watch(params: WatchParams) -> DispatchParams:
    return DispatchParams(
        **{key: getattr(params, key) for key in DISPATCH_PARAM_KEYS}
    )


def _replace_bundle(params, allowed_keys: tuple[str, ...], updates: dict[str, Any]):
    selected = {key: updates[key] for key in allowed_keys if key in updates}
    if not selected:
        return params
    return replace(params, **selected)


def _non_negative_int_attr(config: object, field_name: str, default: int) -> int:
    try:
        value = int(getattr(config, field_name, default))
    except (TypeError, ValueError):
        return max(0, default)
    return max(0, value)


def _non_negative_float_attr(config: object, field_name: str, default: float) -> float:
    try:
        value = float(getattr(config, field_name, default))
    except (TypeError, ValueError):
        return max(0.0, default)
    return max(0.0, value)


@dataclass
class DispatchContext:
    """Resolved dispatch state passed through one dispatch cycle."""

    cfg: Any
    planner: bool
    runner_instruction: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    take_over_by: str
    locked_files: list[str] | None
    router: Any  # CapabilityRouter – forward ref to avoid circular import at module level
    recovery_mode: str = ""
    parent_run_id: str = ""
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None
    background_launch_id: str = ""
    records: list = field(default_factory=list)
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)

    @property
    def preview_only(self) -> bool:
        return bool(self.execution_plan.preview_only)

    @property
    def mutate_state(self) -> bool:
        return bool(self.execution_plan.mutate_state)

    @property
    def should_start_runners(self) -> bool:
        return bool(self.execution_plan.start_runners)


@dataclass
class RunnerBatchContext:
    """Runner batch controls after dispatch candidate selection."""

    pending_runner_jobs: list
    runner_concurrency: int
    runner_timeout_seconds: float
    effective_runner_instruction: str
    max_cards: int
    probe: bool
    records: list
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)
