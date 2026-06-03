
from __future__ import annotations

"""Inheritance manifest helpers for subagent task creation.

这里不负责把父级上下文塞给子代理，只记录“当前 child 与 parent 的字段关系”。
真正执行时仍以 child task 上的 allowed/context/quality 字段为准。
"""

import time
from dataclasses import asdict, is_dataclass
from typing import Any

from ..model_task import InheritanceManifest, SubAgentTask

_LIST_FIELDS = ("allowed_skills", "allowed_tools", "acceptance_checks", "context_packs")
_EMPTY_VALUES = ({}, [], "", None)


def build_inheritance_manifest(parent: SubAgentTask | None, child: SubAgentTask) -> InheritanceManifest:
    if parent is None:
        return InheritanceManifest()
    inherited, overridden, dropped = _compare_list_fields(parent, child)
    buckets = (inherited, overridden, dropped)
    _compare_object_field(parent, child, "quality_contract", buckets)
    _compare_object_field(parent, child, "context_manifest", buckets)
    _compare_object_field(parent, child, "effective_permissions", buckets)
    return InheritanceManifest(
        source_run_id=parent.id,
        target_run_id=child.id,
        root_task_id=child.root_id or parent.root_id or parent.id,
        inherited=inherited,
        overridden=overridden,
        dropped=dropped,
        policy=_default_policy(),
        created_at=child.created_at or time.time(),
        reserved={
            "schema_name": "subagent_inheritance_manifest",
            "schema_version": 1,
            "future": ["requester_scope", "visibility", "takeover_query", "explicit_drop_rules"],
        },
    )


def _compare_list_fields(
    parent: SubAgentTask,
    child: SubAgentTask,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    inherited: dict[str, object] = {}
    overridden: dict[str, object] = {}
    dropped: dict[str, object] = {}
    for field_name in _LIST_FIELDS:
        parent_items = _list_value(getattr(parent, field_name, []))
        child_items = _list_value(getattr(child, field_name, []))
        common = [item for item in child_items if item in parent_items]
        removed = [item for item in parent_items if item not in child_items]
        added = [item for item in child_items if item not in parent_items]
        if common:
            inherited[field_name] = common
        if removed:
            dropped[field_name] = removed
        if added or (parent_items and child_items != parent_items):
            overridden[field_name] = {"parent": parent_items, "child": child_items, "added": added}
    return inherited, overridden, dropped


def _compare_object_field(
    parent: SubAgentTask,
    child: SubAgentTask,
    field_name: str,
    buckets: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    inherited, overridden, dropped = buckets
    parent_value = _plain_value(getattr(parent, field_name, None))
    child_value = _plain_value(getattr(child, field_name, None))
    if parent_value == child_value:
        _record_equal_object(field_name, parent_value, inherited)
        return
    if parent_value in _EMPTY_VALUES:
        return
    if child_value in _EMPTY_VALUES:
        dropped[field_name] = parent_value
        return
    overridden[field_name] = {"parent": parent_value, "child": child_value}


def _record_equal_object(field_name: str, value: object, inherited: dict[str, object]) -> None:
    if value not in _EMPTY_VALUES:
        inherited[field_name] = value


def _plain_value(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    return value


def _list_value(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return [] if value in (None, "") else [value]


def _default_policy() -> dict[str, object]:
    return {
        "auto_expand_parent_context": False,
        "inheritance_is_audit_only": True,
        "child_fields_are_runtime_source": True,
    }
