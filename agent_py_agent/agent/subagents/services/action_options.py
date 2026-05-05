from __future__ import annotations

"""LLM: option objects for subagent action apply.

给人看的解释：
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

    @classmethod
    def from_values(cls, options: "ActionApplyOptions | None" = None, **overrides):
        """Build options while preserving old keyword-call compatibility."""

        base = options or cls()
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(base, **clean)
