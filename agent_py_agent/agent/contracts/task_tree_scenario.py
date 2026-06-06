
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_json_file
from .task_tree_ledger_contract import validate_task_tree_ledger


@dataclass(frozen=True)
class TaskTreeScenarioReport:
    ok: bool
    report_ref: str
    tree_ref: str
    events_ref: str
    validation_error_codes: tuple[str, ...]
    tree: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "report_ref": self.report_ref,
            "tree_ref": self.tree_ref,
            "events_ref": self.events_ref,
            "validation_error_codes": list(self.validation_error_codes),
            "tree": dict(self.tree),
        }


def run_task_tree_small_scenario(workspace: Path) -> TaskTreeScenarioReport:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    tree = _tree()
    _materialize_node_refs(root, tree)
    tree_ref = _write_artifact_json_ref(root / "task_tree_scenario" / "tree.json", tree, root)
    events_ref = _write_events(root)
    validation = validate_task_tree_ledger(tree)
    report = TaskTreeScenarioReport(
        ok=validation.ok,
        report_ref="task_tree_scenario/report.json",
        tree_ref=tree_ref,
        events_ref=events_ref,
        validation_error_codes=validation.error_codes,
        tree=tree,
    )
    _write_artifact_json_ref(root / report.report_ref, report.to_dict(), root)
    return report


def _tree() -> dict[str, object]:
    return {
        "tree_id": "tree-small-real-1",
        "root_task_id": "task-root",
        "nodes": [
            _node("task-root", "", "DONE", {"child_ids": ["task-read", "task-report"]}),
            _node("task-read", "task-root", "DONE"),
            _node("task-report", "task-root", "DONE", {"dependency_ids": ["task-read"]}),
        ],
        "edges": [
            {"from_task_id": "task-root", "to_task_id": "task-read", "kind": "parent_child"},
            {"from_task_id": "task-root", "to_task_id": "task-report", "kind": "parent_child"},
            {"from_task_id": "task-read", "to_task_id": "task-report", "kind": "depends_on"},
        ],
    }


def _node(
    task_id: str,
    parent_id: str,
    status: str,
    links: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    links = links or {}
    base = f"task_tree_scenario/{task_id}"
    return {
        "task_id": task_id,
        "parent_id": parent_id,
        "status": status,
        "critical": True,
        "child_ids": links.get("child_ids", []),
        "dependency_ids": links.get("dependency_ids", []),
        "task_contract_ref": f"artifact://{base}/contract.json",
        "artifact_refs": [f"artifact://{base}/output.json"],
        "acceptance_result_ref": f"artifact://{base}/acceptance.json",
        "state_ref": f"artifact://{base}/state.json",
    }


def _materialize_node_refs(root: Path, tree: dict[str, object]) -> None:
    for node in tree["nodes"]:
        task_id = str(node["task_id"])
        node_dir = root / "task_tree_scenario" / task_id
        _write_artifact_json_ref(node_dir / "contract.json", {"task_id": task_id}, root)
        _write_artifact_json_ref(node_dir / "output.json", {"task_id": task_id, "ok": True}, root)
        _write_artifact_json_ref(node_dir / "acceptance.json", {"task_id": task_id, "passed": True}, root)
        _write_artifact_json_ref(node_dir / "state.json", {"task_id": task_id, "status": node["status"]}, root)


def _write_events(root: Path) -> str:
    path = root / "task_tree_scenario" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"event_id": "evt-tree-created", "type": "tree_created", "tree_id": "tree-small-real-1"},
        {"event_id": "evt-tree-verified", "type": "tree_verified", "tree_id": "tree-small-real-1"},
    ]
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    return _rel(path, root)


def _write_artifact_json_ref(path: Path, payload: object, root: Path) -> str:
    write_json_file(path, payload)
    return _rel(path, root)


def _rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


__all__ = ["TaskTreeScenarioReport", "run_task_tree_small_scenario"]
