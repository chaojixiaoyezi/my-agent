# LLM: Dispatch run-id parsing stays structural and avoids guessing from prose.
# 模块用途: 归一化 dispatch_subagents 的显式 run_id 别名和 direct_children 范围。

from __future__ import annotations

from .orchestration_run_scope import remembered_orchestration_run_ids
from .parameters import _bool_param, _string_list
from .runner_context import current_subagent_run_id


# LLM: dispatch_include_run_ids_param accepts common model aliases for explicit dispatch targets.
# 函数用途: 真实模型常把 run_ids 写成 subagent_ids/dispatch_run_ids；这里统一归一。
def dispatch_include_run_ids_param(params: dict[str, object], *, agent: object | None = None) -> list[str]:
    ids = _explicit_run_ids_from_params(params)
    for run_id in _run_ids_from_items(params.get("items")):
        _append_unique_id(ids, run_id)
    for run_id in _direct_children_run_ids(params, agent):
        _append_unique_id(ids, run_id)
    return ids


def _explicit_run_ids_from_params(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for key in _RUN_ID_KEYS:
        for run_id in _run_id_list_value(params.get(key)):
            _append_unique_id(ids, run_id)
    return ids


_RUN_ID_KEYS = (
    "run_ids",
    "include_run_ids",
    "dispatch_run_ids",
    "subagent_run_ids",
    "subagent_ids",
    "dispatch_subagent_run_ids",
    "dispatch_subagent_ids",
    "target_subagent_ids",
    "target_run_ids",
    "agent_ids",
    "direct_children",
    "child_run_ids",
    "children",
)


def _run_id_list_value(value: object) -> list[str]:
    if isinstance(value, (bool, int, float)):
        return []
    return _string_list(value)


def _direct_children_run_ids(params: dict[str, object], agent: object | None) -> list[str]:
    if not _bool_param(params.get("direct_children"), default=False):
        return []
    if agent is None or current_subagent_run_id(agent):
        return []
    return sorted(remembered_orchestration_run_ids(agent))


def _run_ids_from_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        _append_unique_id(ids, _run_id_from_item(item))
    return ids


def _run_id_from_item(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("run_id")
        or item.get("target_run_id")
        or item.get("id")
        or item.get("subagent_id")
        or item.get("target_subagent_id")
        or ""
    ).strip()


def _append_unique_id(ids: list[str], run_id: str) -> None:
    if run_id and run_id not in ids:
        ids.append(run_id)
