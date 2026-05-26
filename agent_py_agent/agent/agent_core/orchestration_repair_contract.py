# LLM: Repair contracts keep fix/execute/verify/full-success handoffs machine-readable across repair lanes.
# 模块用途: 为验收失败和产物失败统一生成修复合同，要求同一个 repair run 读 refs、修复、执行必要步骤并重新满足原始验收。

from __future__ import annotations

from dataclasses import dataclass

_REF_KEYS = ("acceptance_ref", "test_ref", "followup_ref", "output_ref", "run_ref", "task_ref")
_CONTRACT_SCHEMA = "subagent_repair_contract.v1"
_FULL_SUCCESS_KEYS = ("full_success_checks", "original_acceptance_checks", "acceptance_checks")
_SAME_RUN_ACTIONS = [
    "read_failure_refs",
    "repair_named_scope",
    "preserve_original_success_contract",
    "execute_generated_scripts_or_commands_if_needed",
    "verify_target_artifacts",
    "emit_executable_tests_for_parent_acceptance",
    "report_artifact_and_test_refs",
]
_RECOMMENDED_TEST_METHODS = ["file_check", "content_check", "static_site_check", "command"]
_MUST_NOT = [
    "create a separate child only to execute the repaired script or command",
    "declare done before target artifacts exist and have been read or tested",
    "repair unrelated healthy branches",
    "shrink the original success contract to only the latest failure symptom",
]
_REPAIR_ALLOWED_TOOLS = [
    "subagent_board",
    "list_files",
    "read_file",
    "read_artifact",
    "search_text",
    "replace_in_file",
    "write_file",
    "append_file",
]


# LLM: RepairContractRequest keeps repair contract assembly bundle-shaped.
# 类用途: 保存修复合同所需的失败信号、run id 和产物路径；自身不执行任何修复动作。
@dataclass(frozen=True)
class RepairContractRequest:
    kind: str
    failed_run_ids: list[str]
    failure_refs: list[dict[str, object]]
    target_artifact_refs: list[str]


# LLM: repair_contract_tool_fields returns create/schedule params that survive into child context.
# 函数用途: 给 suggested_tool_call 补上 context_manifest/context_packs/repair_contract，让修复小傻妞不再拆成“只修不跑”的漂移任务。
def repair_contract_tool_fields(request: RepairContractRequest) -> dict[str, object]:
    required_paths = _required_read_paths(request.failure_refs)
    contract = _repair_contract(request, required_paths)
    return {
        "required_read_paths": required_paths,
        "context_manifest": {
            "required_read_paths": required_paths,
            "task_pack_refs": [_CONTRACT_SCHEMA],
        },
        "context_packs": [_repair_context_pack(request, contract)],
        "repair_contract": contract,
    }


# LLM: repair_contract_acceptance_checks are reusable checks for repair workers.
# 函数用途: 返回修复子代理的通用验收要求；有 failure_refs 时附带原始完整验收，防止修复目标被缩小。
def repair_contract_acceptance_checks(failure_refs: list[dict[str, object]] | None = None) -> list[str]:
    checks = [
        "同一个 repair run 内完成读取 failure refs、修复、必要执行和产物验证",
        "修复后必须重新满足原始完整验收要求，不能只修最近一个症状",
        "如果生成或修改了脚本/命令，必须在本 run 内执行或明确给出不能执行的机器证据",
        "output.json.tests 必须包含可执行父级验收项：优先 file_check/content_check/static_site_check，必要时才用 command",
        "完成前必须报告目标 artifact/test refs，不要只说已经修好",
    ]
    checks.extend(f"原始验收: {item}" for item in repair_contract_full_success_checks(failure_refs or [])[:8])
    return checks


# LLM: repair_contract_allowed_tools grants repair workers the same generic handoff tools across repair lanes.
# 函数用途: 给父级验收修复 worker 统一授权读写和协作账本工具；不按具体业务类型区分。
def repair_contract_allowed_tools() -> list[str]:
    return list(_REPAIR_ALLOWED_TOOLS)


# LLM: repair_contract_goal_suffix is short text for model-visible goals.
# 函数用途: 给 repair goal 附加一段稳定闭环要求，避免父级再派一个只负责执行的漂移 child。
def repair_contract_goal_suffix() -> str:
    return (
        "修复、必要执行和验证必须在同一个 repair run 内闭环；"
        "不要再创建一个只负责执行脚本/命令的子代理；"
        "不要把原始完整验收要求缩小成只修最近一个失败症状。"
    )


# LLM: repair_contract_full_success_checks extracts inherited full-success gates from failure refs.
# 函数用途: 从失败信号中收集原始 acceptance_checks；供 repair_contract、goal 和 suggested_tool_call 统一复用。
def repair_contract_full_success_checks(failure_refs: list[dict[str, object]]) -> list[str]:
    values: list[str] = []
    for item in failure_refs:
        for key in _FULL_SUCCESS_KEYS:
            values.extend(_text_values(item.get(key)))
    return _unique_text(values)


# LLM: _text_values normalizes inherited acceptance fields without deep nesting.
# 函数用途: 把 failure_refs 里的字符串或字符串列表统一成文本列表，供完整成功合同复用。
def _text_values(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(value) for value in raw]
    if isinstance(raw, str):
        return [raw]
    return []


# LLM: _repair_contract produces the stable machine payload for UI, docs, and future tool runners.
# 函数用途: 组装修复合同 JSON；只包含 refs 和短动作词，不展开业务文件正文。
def _repair_contract(request: RepairContractRequest, required_paths: list[str]) -> dict[str, object]:
    return {
        "schema": _CONTRACT_SCHEMA,
        "kind": request.kind,
        "failed_run_ids": _unique_text(request.failed_run_ids),
        "required_read_paths": required_paths,
        "target_artifact_refs": _unique_text(request.target_artifact_refs),
        "full_success_checks": repair_contract_full_success_checks(request.failure_refs),
        "same_run_required_actions": list(_SAME_RUN_ACTIONS),
        "output_tests_required": True,
        "recommended_test_methods": list(_RECOMMENDED_TEST_METHODS),
        "must_not": list(_MUST_NOT),
    }


# LLM: _repair_context_pack gives runners a compact human-readable view of the contract.
# 函数用途: 把合同摘要放入 Context Packs；runner prompt 会展示它，执行上下文 JSON 也保留完整字段。
def _repair_context_pack(request: RepairContractRequest, contract: dict[str, object]) -> dict[str, object]:
    targets = contract.get("target_artifact_refs") or []
    full_checks = contract.get("full_success_checks") or []
    return {
        "kind": "repair_contract",
        "ref": _CONTRACT_SCHEMA,
        "role": "same-run-fix-execute-verify",
        "summary": (
            f"{request.kind}: 同一个 repair run 必须读取失败 refs、修复、执行必要脚本/命令、"
            f"验证目标产物并报告 refs；目标产物数={len(targets)}，原始验收数={len(full_checks)}。"
        ),
        "contract": contract,
    }


# LLM: _required_read_paths collects small machine refs from all failure signals.
# 函数用途: 提取 acceptance/test/followup/output/run/task 引用，交给修复子代理自己读取。
def _required_read_paths(failure_refs: list[dict[str, object]]) -> list[str]:
    refs: list[str] = []
    for item in failure_refs:
        refs.extend(str(item.get(key) or "") for key in _REF_KEYS)
    return _unique_text(refs)


# LLM: _unique_text keeps contract lists compact and stable.
# 函数用途: 去空、去重并保留首次出现顺序，避免重复 refs 放大 prompt。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique


__all__ = [
    "RepairContractRequest",
    "repair_contract_acceptance_checks",
    "repair_contract_allowed_tools",
    "repair_contract_full_success_checks",
    "repair_contract_goal_suffix",
    "repair_contract_tool_fields",
]
