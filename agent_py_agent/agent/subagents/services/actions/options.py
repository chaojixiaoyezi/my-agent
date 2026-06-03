
from __future__ import annotations

"""option objects for subagent action apply.

动作执行入口保留原有关键字兼容，但内部统一收成 options，避免长参数列表继续扩散。
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ActionApplyOptions:
    """User-selected filters and apply flags for action execution."""

    apply: bool = False
    action_filter: str = ""
    run_id: str = ""
    take_over_by: str = ""
    locked_files: list[str] | None = None
    limit: int = 0
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None

    @classmethod
    def from_values(
        cls,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
        root_id: str | None = None,
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ):
        """Build the options bundle from explicit legacy fields."""

        base = options or cls()
        updates = {
            "apply": apply,
            "action_filter": action_filter,
            "run_id": run_id,
            "take_over_by": take_over_by,
            "locked_files": locked_files,
            "limit": limit,
            "root_id": root_id,
            "include_run_ids": include_run_ids,
            "exclude_run_ids": exclude_run_ids,
        }
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)
