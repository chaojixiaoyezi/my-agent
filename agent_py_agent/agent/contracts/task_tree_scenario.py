# LLM: Task-tree scenario materializes a small parent-child ledger.
# 模块用途: 给真实子代理前置测试提供一个可验收的父子任务账本小场景。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .task_tree_ledger_contract import validate_task_tree_ledger


# LLM: TaskTreeScenarioReport records one deterministic TaskTree validation run.
# 类用途: 保存任务树小场景报告、tree/event refs、错误码和树事实。
@dataclass(frozen=True)
class TaskTreeScenarioReport:
    ok: bool
    report_ref: str
    tree_ref: str
    events_ref: str
    validation_error_codes: tuple[str, ...]
    tree: dict[str, object]

    # LLM: to_dict serializes the task-tree scenario report.
    # 函数用途: 输出任务树报告，保留 refs 和结构化 tree facts。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "report_ref": self.report_ref,
            "tree_ref": self.tree_ref,
            "events_ref": self.events_ref,
            "validation_error_codes": list(self.validation_error_codes),
            "tree": dict(self.tree),
        }


# LLM: run_task_tree_small_scenario writes state/artifact refs before validating the ledger.
# 函数用途: 生成父任务、读文件子任务、报告子任务三节点账本并写事件流水。
def run_task_tree_small_scenario(workspace: Path) -> TaskTreeScenarioReport:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    tree = _tree()
    _materialize_node_refs(root, tree)
    tree_ref = _write_json(root / "task_tree_scenario" / "tree.json", tree, root)
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
    _write_json(root / report.report_ref, report.to_dict(), root)
    return report


# LLM: _tree builds a three-node parent-child dependency ledger.
# 函数用途: 生成 root/read/report 三节点任务树，供任务树合同验收。
def _tree() -> dict[str, object]:
    return {
        "tree_id": "tree-small-real-1",
        "root_task_id": "task-root",
        "nodes": [
            _node("task-root", "", "VERIFIED", {"child_ids": ["task-read", "task-report"]}),
            _node("task-read", "task-root", "SUCCEEDED"),
            _node("task-report", "task-root", "VERIFIED", {"dependency_ids": ["task-read"]}),
        ],
        "edges": [
            {"from_task_id": "task-root", "to_task_id": "task-read", "kind": "parent_child"},
            {"from_task_id": "task-root", "to_task_id": "task-report", "kind": "parent_child"},
            {"from_task_id": "task-read", "to_task_id": "task-report", "kind": "depends_on"},
        ],
    }


# LLM: _node builds one task-tree node from explicit links.
# 函数用途: 生成 task id、父任务、依赖、产物和验收 refs，不解析任务描述。
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


# LLM: _materialize_node_refs writes all node refs referenced by the ledger.
# 函数用途: 为每个节点写 contract/output/acceptance/state JSON，避免空引用。
def _materialize_node_refs(root: Path, tree: dict[str, object]) -> None:
    for node in tree["nodes"]:
        task_id = str(node["task_id"])
        node_dir = root / "task_tree_scenario" / task_id
        _write_json(node_dir / "contract.json", {"task_id": task_id}, root)
        _write_json(node_dir / "output.json", {"task_id": task_id, "ok": True}, root)
        _write_json(node_dir / "acceptance.json", {"task_id": task_id, "passed": True}, root)
        _write_json(node_dir / "state.json", {"task_id": task_id, "status": node["status"]}, root)


# LLM: _write_events records machine events for the TaskTree scenario.
# 函数用途: 写入 JSONL 事件流水，模仿可回放运行记录。
def _write_events(root: Path) -> str:
    path = root / "task_tree_scenario" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"event_id": "evt-tree-created", "type": "tree_created", "tree_id": "tree-small-real-1"},
        {"event_id": "evt-tree-verified", "type": "tree_verified", "tree_id": "tree-small-real-1"},
    ]
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    return _rel(path, root)


# LLM: _write_json persists one structured TaskTree artifact.
# 函数用途: 写入 JSON 并返回 workspace-relative ref。
def _write_json(path: Path, payload: object, root: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return _rel(path, root)


# LLM: _rel converts TaskTree paths to workspace-relative refs.
# 函数用途: 生成报告中使用的稳定相对路径。
def _rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


__all__ = ["TaskTreeScenarioReport", "run_task_tree_small_scenario"]
