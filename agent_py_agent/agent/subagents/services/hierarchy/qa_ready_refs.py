
from __future__ import annotations

"""Ready-child refs derived from current subagent status protocol facts."""

from pathlib import Path
from typing import Any

from ...models import TaskStatus, task_has_status
from ...role_templates import role_template_snapshot_for_task

_READY_SCAN_MAX_NODES = 64


def ready_implementation_refs(manager: Any, parent: object, *, max_nodes: int = _READY_SCAN_MAX_NODES) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    queue = [str(item) for item in _list_attr(parent, "child_ids") if str(item or "").strip()]
    seen: set[str] = set()
    scanned = 0
    while queue and scanned < max_nodes:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        scanned += 1
        child = _load_child(manager, run_id)
        if child is None:
            continue
        if _is_ready_implementation_child(child):
            refs.append(_ready_ref_payload(child))
        queue.extend(child_id for child_id in _list_attr(child, "child_ids") if child_id not in seen)
    return refs


def has_ready_implementation_child(manager: Any, parent: object) -> bool:
    return bool(ready_implementation_refs(manager, parent, max_nodes=_READY_SCAN_MAX_NODES))


def flatten_ready_ref_values(ready_refs: list[dict[str, object]], key: str, *, max_refs: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for text in _iter_ready_ref_values(ready_refs, key):
        if text in seen:
            continue
        seen.add(text)
        refs.append(text)
    return refs[:max_refs]


def _ready_ref_payload(child: object) -> dict[str, object]:
    return {
        "run_id": _text_attr(child, "id"),
        "role": _text_attr(child, "role"),
        "agent_name": _text_attr(child, "agent_name"),
        "status": _text_attr(child, "status"),
        "verification_status": _text_attr(child, "verification_status"),
        "artifact_refs": _existing_refs(_list_attr(child, "artifact_refs")),
        "output_refs": _existing_refs(
            [
                _text_attr(child, "output_json"),
                _text_attr(child, "agent_run_final_report_md"),
                _text_attr(child, "final_report_md"),
                _text_attr(child, "runner_result_json"),
            ]
        ),
    }


def _is_ready_implementation_child(child: object) -> bool:
    if not task_has_status(child, TaskStatus.DONE):
        return False
    if not _has_ready_ref(child):
        return False
    snapshot = role_template_snapshot_for_task(child)
    if not snapshot:
        return False
    if bool(snapshot.get("depends_on_outputs")) or bool(snapshot.get("can_run_tests")):
        return False
    if bool(snapshot.get("can_spawn_children")):
        return False
    return bool(snapshot.get("can_write"))


def _has_ready_ref(child: object) -> bool:
    payload = _ready_ref_payload(child)
    return bool(payload["artifact_refs"] or payload["output_refs"])


def _load_child(manager: Any, run_id: str) -> object | None:
    try:
        return manager.load(run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return None


def _existing_refs(values: list[object]) -> list[str]:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in refs:
            continue
        if _looks_like_path(text) and not Path(text).exists():
            continue
        refs.append(text)
    return refs[:12]


def _iter_ready_ref_values(ready_refs: list[dict[str, object]], key: str):
    for item in ready_refs:
        values = item.get(key)
        if isinstance(values, list):
            yield from (str(ref or "").strip() for ref in values if str(ref or "").strip())


def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", "~", ".")) or "/" in text or "\\" in text


def _list_attr(item: object, name: str) -> list[object]:
    value = getattr(item, name, [])
    return value if isinstance(value, list) else []


def _text_attr(item: object, name: str) -> str:
    value = getattr(item, name, "")
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


__all__ = ["flatten_ready_ref_values", "has_ready_implementation_child", "ready_implementation_refs"]
