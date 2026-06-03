from __future__ import annotations

from ....common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...parameters import _bool_param
from ...runner.context import current_subagent_run_id
from ..run_scope import remembered_orchestration_run_ids


def dispatch_include_run_ids_param(params: dict[str, object], *, agent: object | None = None) -> list[str]:
    ids = _explicit_run_ids_from_params(params)
    for run_id in _direct_children_run_ids(params, agent):
        _append_unique_id(ids, run_id)
    return ids


def _explicit_run_ids_from_params(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for run_id in _run_id_list_value(params.get("run_ids")):
        _append_unique_id(ids, run_id)
    return ids


def _run_id_list_value(value: object) -> list[str]:
    if isinstance(value, (bool, int, float)):
        return []
    return string_list(value, TOOL_TEXT_LIST_OPTIONS)


def _direct_children_run_ids(params: dict[str, object], agent: object | None) -> list[str]:
    if not _bool_param(params.get("direct_children"), default=False):
        return []
    if agent is None or current_subagent_run_id(agent):
        return []
    return sorted(remembered_orchestration_run_ids(agent))


def _append_unique_id(ids: list[str], run_id: str) -> None:
    if run_id and run_id not in ids:
        ids.append(run_id)
