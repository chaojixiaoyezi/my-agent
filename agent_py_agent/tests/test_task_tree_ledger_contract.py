from __future__ import annotations


def test_task_tree_ledger_accepts_structured_parent_child_closure() -> None:
    from agent_py_agent.agent.contracts.task_tree_ledger_contract import validate_task_tree_ledger

    result = validate_task_tree_ledger(_valid_task_tree())

    assert result.ok is True
    assert result.error_codes == ()


def test_task_tree_ledger_rejects_parent_success_with_failed_child() -> None:
    from agent_py_agent.agent.contracts.task_tree_ledger_contract import validate_task_tree_ledger

    tree = _valid_task_tree()
    tree["nodes"][1]["status"] = "FAILED"
    tree["nodes"][1]["acceptance_result_ref"] = ""
    tree["nodes"][1]["artifact_refs"] = []

    result = validate_task_tree_ledger(tree)

    assert result.error_codes == ("TASK_TREE_SUCCEEDED_PARENT_HAS_UNFINISHED_CRITICAL_CHILD",)


def _valid_task_tree() -> dict[str, object]:
    return {
        "tree_id": "tree-1",
        "root_task_id": "task-root",
        "nodes": [
            _node("task-root", "", "SUCCEEDED", {"child_ids": ["task-read", "task-report"]}),
            _node("task-read", "task-root", "SUCCEEDED"),
            _node("task-report", "task-root", "VERIFIED", {"dependency_ids": ["task-read"]}),
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
    return {
        "task_id": task_id,
        "parent_id": parent_id,
        "status": status,
        "critical": True,
        "child_ids": links.get("child_ids", []),
        "dependency_ids": links.get("dependency_ids", []),
        "task_contract_ref": f"contract://{task_id}",
        "artifact_refs": [f"artifact://{task_id}/output.json"],
        "acceptance_result_ref": f"artifact://{task_id}/acceptance.json",
        "state_ref": f"artifact://{task_id}/state.json",
    }
