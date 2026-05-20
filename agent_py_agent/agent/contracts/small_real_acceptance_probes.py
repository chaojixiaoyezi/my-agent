# LLM: Small real probe helpers keep the runner thin and schema-focused.
# 模块用途: 构造小真实验收需要的 ToolRegistry、tool probe 和 Shadow runtime facts。

from __future__ import annotations

import json
from pathlib import Path

from ..tooling.models import ToolExecutionResult
from ..tooling.registry import ToolRegistry, ToolRegistryParams


# LLM: small_real_registry builds a bounded ToolRegistry for probe execution.
# 函数用途: 创建只指向当前小真实工作区的工具注册表，避免 probe 越界读写。
def small_real_registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=200,
        )
    )


# LLM: read_file_probe wraps a real read_file result in the phase-5 probe schema.
# 函数用途: 生成只读工具 probe，供 real_tool_dry_run_contract 验收。
def read_file_probe(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "read-file-success",
        "operation_id": "op-read-file-success",
        "tool": result.tool,
        "effect": "read_only",
        "mode": "read_only",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/read_file/result",
        "executed_actions": [],
        "result": {"ok": result.ok, "payload": {"output": result.output}},
    }


# LLM: controlled_exec_probe wraps a dry-run shell plan in the phase-5 probe schema.
# 函数用途: 生成 controlled_exec dry-run probe，记录幂等键和参数 hash。
def controlled_exec_probe(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "controlled-exec-dry-run",
        "operation_id": "op-controlled-exec-dry-run",
        "tool": result.tool,
        "effect": "dangerous",
        "mode": "dry_run",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/controlled_exec/result",
        "idempotency_key": "idem-controlled-exec-pwd",
        "args_hash": "sha256:controlled-exec-pwd",
        "executed_actions": [],
        "result": {"ok": result.ok, "payload": json_payload(result.output)},
    }


# LLM: shadow_runtime_facts builds a Shadow runtime ledger from validated probe refs.
# 函数用途: 生成影子模式运行事实，明确引用 phase-5 probe 和人工对比 artifact。
def shadow_runtime_facts(
    probe_refs: list[dict[str, object]],
    comparison_ref: str,
) -> dict[str, object]:
    return {
        "run_id": "shadow-run-small-real",
        "stage": "shadow_mode",
        "mode": "shadow",
        "real_tool_probe_refs": list(probe_refs),
        "comparison_artifacts": [{"artifact_ref": f"artifact://{comparison_ref}", "exists": True}],
        "shadow_facts": _shadow_facts(comparison_ref),
        "executed_actions": [],
    }


# LLM: controlled_exec_grant builds the parent-injected dry-run grant.
# 函数用途: 给 controlled_exec probe 提供命令、路径和输出预算边界。
def controlled_exec_grant(root: Path) -> dict[str, object]:
    return {
        "grant_id": "grant-small-real",
        "request_id": "req-small-real",
        "run_id": "run-small-real",
        "command_allowlist": ["pwd"],
        "path_scope": [str(root)],
        "network_scope": [],
        "output_budget": {"stdout_bytes": 200, "stderr_bytes": 200},
        "risk_level": "low",
    }


# LLM: _shadow_facts returns the nested static Shadow ledger.
# 函数用途: 构造 Shadow 合同需要的风险、证据、建议动作、dry-run 和人工复核字段。
def _shadow_facts(comparison_ref: str) -> dict[str, object]:
    return {
        "mode": "shadow",
        "risk": {"score": 50},
        "evidence_refs": [{"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": "tool://read-file-success"}],
        "recommended_actions": [_recommended_action(comparison_ref)],
        "dry_run_results": [{"action_id": "ACT-1", "mode": "dry_run", "ok": True, "result_ref": "artifact://shadow/dry-run.json"}],
        "executed_actions": [],
        "human_review": {
            "review_id": "HR-1",
            "review_ref": f"artifact://{comparison_ref}",
            "decision": "agree",
            "agreement": True,
        },
    }


# LLM: _recommended_action builds one non-executing dangerous action recommendation.
# 函数用途: 生成需要人工复核和审批草稿的 dry-run 建议动作。
def _recommended_action(comparison_ref: str) -> dict[str, object]:
    return {
        "action_id": "ACT-1",
        "effect": "dangerous",
        "mode": "dry_run",
        "operator_review_ref": f"artifact://{comparison_ref}",
        "approval_draft_ref": "artifact://shadow/approval.json",
    }


# LLM: json_payload parses tool output into a bounded structured object.
# 函数用途: 将 controlled_exec 输出 JSON 转成 dict；解析失败时返回结构化错误字段。
def json_payload(value: str) -> dict[str, object]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {"mode": "", "raw_parse_failed": True}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


__all__ = [
    "controlled_exec_grant",
    "controlled_exec_probe",
    "read_file_probe",
    "shadow_runtime_facts",
    "small_real_registry",
]
