# LLM: Offline subagent contracts validate parent closeout against child run facts.
# 模块用途: 校验父子代理的状态、产物、验收、显式层级限制和 refs-only 输出隔离。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUCCESS_STATUSES = {"SUCCEEDED", "VERIFIED", "DONE"}
ACTIVE_PARENT_CLOSEOUT_STATUSES = {"VERIFYING", "SUCCEEDED", "VERIFIED", "DONE"}


# LLM: OfflineSubagentValidation reports subagent tree findings for fake and replay tests.
# 类用途: 返回子代理父子收口合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineSubagentValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: validate_subagent_contract checks one parent task and its child run records.
# 函数用途: 读取 parent、children、limits 结构字段，验证父任务是否可以安全收口。
def validate_subagent_contract(contract: dict[str, Any]) -> OfflineSubagentValidation:
    findings: list[dict[str, object]] = []
    parent = _section(contract.get("parent"))
    children = _record_list(contract.get("children"))
    children_by_id = {_text(child.get("run_id")): child for child in children if _text(child.get("run_id"))}
    _validate_explicit_limits(parent, children, _section(contract.get("limits")), findings)
    _validate_required_children(parent, children_by_id, findings)
    _validate_successful_children(children, findings)
    _validate_child_exports(children, findings)
    return OfflineSubagentValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_explicit_limits enforces only machine-provided limits.
# 函数用途: 只在 max_depth/max_children 明确传入时检查，不设置隐式默认层级限制。
def _validate_explicit_limits(
    parent: dict[str, Any],
    children: tuple[dict[str, Any], ...],
    limits: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    _validate_explicit_depth_limit(children, _optional_int(limits.get("max_depth")), findings)
    _validate_explicit_child_limit(parent, children, _optional_int(limits.get("max_children")), findings)


# LLM: _validate_explicit_depth_limit enforces max_depth only when it is configured.
# 函数用途: 检查 child.depth 是否超过显式 max_depth。
def _validate_explicit_depth_limit(
    children: tuple[dict[str, Any], ...],
    max_depth: int | None,
    findings: list[dict[str, object]],
) -> None:
    if max_depth is None:
        return
    for child in children:
        if _child_depth_exceeds(child, max_depth):
            findings.append(
                _finding(
                    "SUBAGENT_DEPTH_EXCEEDED",
                    {
                        "run_id": _text(child.get("run_id")),
                        "depth": _optional_int(child.get("depth")),
                        "max_depth": max_depth,
                    },
                )
            )


# LLM: _validate_explicit_child_limit enforces max_children only when it is configured.
# 函数用途: 检查一个 parent 下直接 child 数是否超过显式 max_children。
def _validate_explicit_child_limit(
    parent: dict[str, Any],
    children: tuple[dict[str, Any], ...],
    max_children: int | None,
    findings: list[dict[str, object]],
) -> None:
    if max_children is None:
        return
    parent_id = _text(parent.get("run_id"))
    child_count = sum(1 for child in children if _text(child.get("parent_run_id")) == parent_id)
    if child_count > max_children:
        findings.append(
            _finding(
                "SUBAGENT_CHILD_LIMIT_EXCEEDED",
                {
                    "parent_run_id": parent_id,
                    "child_count": child_count,
                    "max_children": max_children,
                },
            )
        )


# LLM: _child_depth_exceeds checks one child against an explicit depth limit.
# 函数用途: 缺失 depth 不参与限制；存在时必须小于等于 max_depth。
def _child_depth_exceeds(child: dict[str, Any], max_depth: int) -> bool:
    depth = _optional_int(child.get("depth"))
    return depth is not None and depth > max_depth


# LLM: _validate_required_children keeps parents from closing before required child facts settle.
# 函数用途: 父任务进入验收/成功类状态时，required_child_run_ids 必须都存在且成功。
def _validate_required_children(
    parent: dict[str, Any],
    children_by_id: dict[str, dict[str, Any]],
    findings: list[dict[str, object]],
) -> None:
    if _status(parent.get("status")) not in ACTIVE_PARENT_CLOSEOUT_STATUSES:
        return
    for child_id in _string_list(parent.get("required_child_run_ids")):
        child = children_by_id.get(child_id)
        if child is None:
            findings.append(_finding("CHILD_MISSING", {"child_run_id": child_id}))
            continue
        if _status(child.get("status")) in {"TIMEOUT", "TIMED_OUT"}:
            findings.append(_finding("CHILD_TIMEOUT", {"child_run_id": child_id}))
            continue
        if _status(child.get("status")) not in SUCCESS_STATUSES:
            findings.append(
                _finding(
                    "CHILD_NOT_SUCCESSFUL",
                    {
                        "child_run_id": child_id,
                        "child_status": _status(child.get("status")),
                    },
                )
            )


# LLM: _validate_successful_children requires artifacts and acceptance for successful child runs.
# 函数用途: child 处于成功类状态时，必须有 artifact_refs 和 acceptance.ok/evidence_refs。
def _validate_successful_children(
    children: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for child in children:
        if _status(child.get("status")) not in SUCCESS_STATUSES:
            continue
        child_id = _text(child.get("run_id"))
        if not _string_list(child.get("artifact_refs")):
            findings.append(_finding("CHILD_ARTIFACT_MISSING", {"child_run_id": child_id}))
        acceptance = _section(child.get("acceptance"))
        if acceptance.get("ok") is not True or not _string_list(acceptance.get("evidence_refs")):
            findings.append(_finding("CHILD_ACCEPTANCE_MISSING", {"child_run_id": child_id}))


# LLM: _validate_child_exports keeps child-to-parent transfer refs-only when an export is declared.
# 函数用途: 子任务 export.kind 一旦存在，必须为 refs_only，避免父上下文吞入完整子上下文。
def _validate_child_exports(
    children: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for child in children:
        export = _section(child.get("export"))
        if not export:
            continue
        if _text(export.get("kind")).lower() != "refs_only":
            findings.append(
                _finding(
                    "CHILD_CONTEXT_EXPORT_NOT_REFS_ONLY",
                    {
                        "child_run_id": _text(child.get("run_id")),
                        "export_kind": _text(export.get("kind")),
                    },
                )
            )


# LLM: _record_list normalizes child run arrays.
# 函数用途: 只接受 dict 列表，忽略无结构项。
def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: _section normalizes nested dict sections.
# 函数用途: 非 dict 字段按空 section 处理。
def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# LLM: _finding creates compact machine findings without prose parsing.
# 函数用途: 生成 code 和额外结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _optional_int reads optional numeric limits without inventing defaults.
# 函数用途: 将显式传入的数字转为 int，未传或非法时返回 None。
def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# LLM: _status normalizes lifecycle-like status values.
# 函数用途: 把状态字段转为大写字符串。
def _status(value: object) -> str:
    return _text(value).upper()


# LLM: _string_list normalizes list-like fields without parsing embedded prose.
# 函数用途: 把结构化数组规整成去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineSubagentValidation", "validate_subagent_contract"]
