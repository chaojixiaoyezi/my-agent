# LLM: TaskTree ledger contracts gate parent-child task structure before real subagents run.
# 模块用途: 校验任务树账本的父子关系、依赖、状态收口、产物 refs 和验收 refs。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    string_tuple,
    text,
    validation_report,
)

TERMINAL_OK = {"SUCCEEDED", "VERIFIED"}


# LLM: _TaskRefCheck bundles one task reference validation request.
# 类用途: 保存待校验的 task ref、错误码和来源 task id，避免 helper 参数继续变宽。
@dataclass(frozen=True)
class _TaskRefCheck:
    ref_id: str
    code: str
    task_id: str


# LLM: validate_task_tree_ledger is the public TaskTree gate before real subagent execution.
# 函数用途: 校验任务树节点、边、依赖和父任务收口，返回结构化 findings。
def validate_task_tree_ledger(tree: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    nodes = list(dict_items(tree.get("nodes")))
    by_id = _nodes_by_id(nodes, findings)
    _validate_root(tree, by_id, findings)
    _validate_nodes(nodes, by_id, findings)
    _validate_edges(tree, by_id, findings)
    _validate_parent_closure(nodes, by_id, findings)
    return validation_report(findings)


# LLM: _nodes_by_id builds the machine index used by all TaskTree checks.
# 函数用途: 按 task_id 建索引，并发现缺失或重复 task id。
def _nodes_by_id(nodes: list[dict[str, Any]], findings: list[dict[str, object]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        task_id = text(node.get("task_id"))
        if not task_id:
            findings.append(finding("TASK_TREE_NODE_ID_MISSING"))
            continue
        if task_id in by_id:
            findings.append(finding("TASK_TREE_DUPLICATE_NODE_ID", {"task_id": task_id}))
        by_id[task_id] = node
    return by_id


# LLM: _validate_root requires the declared root to exist in the node index.
# 函数用途: 校验 root_task_id 指向真实节点，避免孤立树进入执行。
def _validate_root(tree: dict[str, Any], by_id: dict[str, dict[str, Any]], findings: list[dict[str, object]]) -> None:
    root_id = text(tree.get("root_task_id"))
    if not root_id or root_id not in by_id:
        findings.append(finding("TASK_TREE_ROOT_MISSING", {"root_task_id": root_id}))


# LLM: _validate_nodes checks per-node parent, dependency, artifact, and acceptance refs.
# 函数用途: 校验节点级结构化引用，不从任务描述文本推断父子关系。
def _validate_nodes(
    nodes: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    findings: list[dict[str, object]],
) -> None:
    for node in nodes:
        task_id = text(node.get("task_id"))
        if parent_id := text(node.get("parent_id")):
            _validate_existing_ref(_TaskRefCheck(parent_id, "TASK_TREE_PARENT_MISSING", task_id), by_id, findings)
        for dependency_id in string_tuple(node.get("dependency_ids")):
            _validate_existing_ref(
                _TaskRefCheck(dependency_id, "TASK_TREE_DEPENDENCY_MISSING", task_id),
                by_id,
                findings,
            )
        if text(node.get("status")) in TERMINAL_OK:
            _validate_terminal_node(node, findings)


# LLM: _validate_existing_ref verifies a single explicit task reference.
# 函数用途: 检查 parent/dependency ref 是否存在，并写入对应机器错误码。
def _validate_existing_ref(
    check: _TaskRefCheck,
    by_id: dict[str, dict[str, Any]],
    findings: list[dict[str, object]],
) -> None:
    if check.ref_id not in by_id:
        findings.append(finding(check.code, {"task_id": check.task_id, "ref_id": check.ref_id}))


# LLM: _validate_terminal_node requires completed nodes to carry deliverable and verification refs.
# 函数用途: 阻断“状态成功但没有产物或验收结果”的假完成。
def _validate_terminal_node(node: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not string_tuple(node.get("artifact_refs")):
        findings.append(finding("TASK_TREE_CHILD_ARTIFACT_REF_MISSING", {"task_id": text(node.get("task_id"))}))
    if not text(node.get("acceptance_result_ref")):
        findings.append(finding("TASK_TREE_ACCEPTANCE_REF_MISSING", {"task_id": text(node.get("task_id"))}))


# LLM: _validate_edges checks declared graph edges against known task ids.
# 函数用途: 校验 edges 中 from/to task id 是否都存在于同一账本。
def _validate_edges(tree: dict[str, Any], by_id: dict[str, dict[str, Any]], findings: list[dict[str, object]]) -> None:
    for edge in dict_items(tree.get("edges")):
        findings.extend(_edge_findings(edge, by_id))


# LLM: _edge_findings returns missing-edge-ref findings without nested control flow.
# 函数用途: 将一条边的 from/to 校验结果转成 finding 列表。
def _edge_findings(edge: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, object]]:
    return [
        finding("TASK_TREE_EDGE_REF_MISSING", {"ref_id": ref_id, "edge_key": key})
        for key in ("from_task_id", "to_task_id")
        for ref_id in (text(edge.get(key)),)
        if not ref_id or ref_id not in by_id
    ]


# LLM: _validate_parent_closure prevents successful parents from hiding unfinished critical children.
# 函数用途: 对已成功父节点检查 child_ids 的关键子任务是否也完成。
def _validate_parent_closure(
    nodes: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    findings: list[dict[str, object]],
) -> None:
    for node in nodes:
        if text(node.get("status")) in TERMINAL_OK:
            findings.extend(_parent_closure_findings(node, by_id))


# LLM: _parent_closure_findings maps one parent node to child closure findings.
# 函数用途: 生成某个父任务的子任务缺失或未完成 findings。
def _parent_closure_findings(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, object]]:
    return [
        finding(code, {"task_id": text(node.get("task_id")), **extra})
        for child_id in string_tuple(node.get("child_ids"))
        for code, extra in (_child_closure_finding(child_id, by_id),)
        if code
    ]


# LLM: _child_closure_finding evaluates one child ref for parent closeout.
# 函数用途: 返回单个 child ref 的缺失或未完成错误码。
def _child_closure_finding(child_id: str, by_id: dict[str, dict[str, Any]]) -> tuple[str, dict[str, object]]:
    child = by_id.get(child_id)
    if not child:
        return "TASK_TREE_CHILD_REF_MISSING", {}
    if child.get("critical") is not False and text(child.get("status")) not in TERMINAL_OK:
        return "TASK_TREE_SUCCEEDED_PARENT_HAS_UNFINISHED_CRITICAL_CHILD", {"child_id": child_id}
    return "", {}


__all__ = ["validate_task_tree_ledger"]
