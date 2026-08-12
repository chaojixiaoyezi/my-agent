from __future__ import annotations

"""Policy-only concurrency planning for canonical tool calls.

The planner never authorizes or executes a call.  It only decides whether a
known call may share a read-only execution segment; ``ActionPolicy`` and
``ToolExecutor`` remain the sole safety and execution authorities.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .input_schema import validate_tool_input
from .models import (
    ToolRuntimeSnapshot,
    resource_scopes_for_runtime_policy,
    tool_effect_for_runtime_policy,
)
from .runtime_contracts import ToolCall
from .workspace_scopes import authoritative_workspace_scopes


@dataclass(frozen=True)
class ToolConcurrencyDescriptor:
    """Host-derived scheduling facts for one canonical call.

    ``mode`` is the tool author's declaration. ``resolved_effect`` is derived
    from the same runtime policy used by ``ActionPolicy``. ``resource_scopes``
    are stable identities used for conflict checks; they are not permissions.
    ``barrier_reason`` explains why this call must remain sequential.
    """

    mode: str
    resolved_effect: str
    resource_scopes: tuple[str, ...] = ()
    barrier_reason: str = ""

    @property
    def parallel_eligible(self) -> bool:
        return (
            self.mode == "parallel_safe"
            and self.resolved_effect == "read_only"
            and not self.barrier_reason
        )


def describe_tool_concurrency(
    snapshot: object,
    call: ToolCall,
    *,
    workspace_root: Path | None = None,
    write_boundary: dict[str, Any] | None = None,
) -> ToolConcurrencyDescriptor:
    """Return a conservative scheduling projection from the frozen snapshot.

    ``workspace_root`` / ``write_boundary`` are optional physical-root context
    (the executor layer always holds them). When present, resource scopes are
    resolved by the shared authoritative resolver (seq 245 P5) so concurrency
    projection, ActionPolicy audit records and operation-store locks all agree;
    without them the legacy text projection is used for pure-policy callers.
    """

    if not isinstance(snapshot, ToolRuntimeSnapshot):
        return _barrier("missing_runtime_snapshot")
    runtime = snapshot.runtime(call.tool_name)
    if (
        runtime is None
        or call.tool_name not in snapshot.available_tool_names
        or call.schema_hash != runtime.model_spec.schema_hash
        or not runtime.availability.available
    ):
        return _barrier("unknown_or_unavailable_runtime")
    policy = runtime.runtime_policy
    internal = set(policy.input_policy.internal_parameters)
    if internal.intersection(call.arguments):
        return _barrier("internal_parameter_spoof")
    validation = validate_tool_input(call.arguments, runtime.model_spec.input_schema)
    if not validation.ok:
        return _barrier("invalid_arguments")
    # Completion can change effect- or resource-bearing arguments. Keep those
    # calls sequential until the executor has injected the trusted values.
    completion_fields = {
        *(name for name, _value in policy.input_policy.safe_parameter_defaults),
        *(name for name, _binding in policy.input_policy.trusted_parameter_bindings),
    }
    effect_fields = {
        name for name, _variants in policy.effect_resolver.by_parameter
    }
    if policy.effect_resolver.command_parameter:
        effect_fields.add(policy.effect_resolver.command_parameter)
    resource_fields = set(policy.resource_scopes.parameter_names)
    if completion_fields & (effect_fields | resource_fields):
        return _barrier("trusted_completion_affects_scheduling")
    try:
        effect = tool_effect_for_runtime_policy(policy, call.arguments)
    except Exception:
        return _barrier("effect_unresolved")
    mode = policy.concurrency_policy.mode
    if mode != "parallel_safe":
        return ToolConcurrencyDescriptor(mode, effect, barrier_reason=mode)
    if effect != "read_only":
        return ToolConcurrencyDescriptor(mode, effect, barrier_reason="effect_barrier")
    if policy.approval_policy.mode == "always":
        return ToolConcurrencyDescriptor(mode, effect, barrier_reason="approval_barrier")
    scopes = authoritative_workspace_scopes(
        workspace_root=workspace_root,
        write_boundary=write_boundary,
        policy=policy,
        arguments=call.arguments,
    )
    return ToolConcurrencyDescriptor(mode, effect, scopes)


def concurrency_conflicts(
    left: ToolConcurrencyDescriptor,
    right: ToolConcurrencyDescriptor,
) -> bool:
    """Return whether two descriptors must be separated by a barrier."""

    if not left.parallel_eligible or not right.parallel_eligible:
        return True
    # All eligible calls are read-only. Identical scopes therefore represent
    # concurrent readers, not a read/write conflict. The explicit comparison
    # keeps the rule ready for richer access-typed scopes without a tool-name
    # whitelist.
    overlap = set(left.resource_scopes) & set(right.resource_scopes)
    return bool(overlap and {left.resolved_effect, right.resolved_effect} != {"read_only"})


def _barrier(reason: str) -> ToolConcurrencyDescriptor:
    return ToolConcurrencyDescriptor("barrier", "read_only", barrier_reason=reason)


__all__ = [
    "ToolConcurrencyDescriptor",
    "concurrency_conflicts",
    "describe_tool_concurrency",
]
