#!/usr/bin/env python3
# LLM: 只核对合同实现与测试文件是否齐备，不读取尺寸产物或证明业务质量；保留真实缺项失败，并同步matrix gate测试。
# 模块用途: 检查离线验证入口是否缺文件；实际行为由索引中的测试和真实 TUI 分别验收。

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED_AREAS = {
    "concurrency": (
        "agent_py_agent/agent/contracts/offline_concurrency_contract.py",
        "agent_py_agent/tests/test_offline_concurrency_contract.py",
    ),
    "recovery": (
        "agent_py_agent/agent/contracts/offline_recovery_contract.py",
        "agent_py_agent/tests/test_offline_recovery_contract.py",
    ),
    "compact_resume": (
        "agent_py_agent/agent/contracts/offline_compact_resume_contract.py",
        "agent_py_agent/tests/test_offline_compact_resume_contract.py",
    ),
    "memory_skill": (
        "agent_py_agent/agent/contracts/offline_memory_skill_contract.py",
        "agent_py_agent/tests/test_offline_memory_skill_contract.py",
    ),
    "subagent": (
        "agent_py_agent/agent/contracts/offline_subagent_contract.py",
        "agent_py_agent/tests/test_offline_subagent_contract.py",
    ),
    "artifact_quality": (
        "agent_py_agent/agent/contracts/artifact_structured_contracts.py",
        "agent_py_agent/tests/test_offline_artifact_quality_contract.py",
    ),
    "output_format": (
        "agent_py_agent/agent/contracts/offline_output_format_contract.py",
        "agent_py_agent/tests/test_offline_output_format_contract.py",
    ),
    "tool_guardrail": (
        "agent_py_agent/agent/contracts/offline_tool_guardrail_contract.py",
        "agent_py_agent/tests/test_offline_tool_guardrail_contract.py",
    ),
    "security_boundary": (
        "agent_py_agent/agent/contracts/offline_security_boundary_contract.py",
        "agent_py_agent/tests/test_offline_security_boundary_contract.py",
    ),
    "config_idempotency": (
        "agent_py_agent/agent/contracts/runtime_config_contract.py",
        "agent_py_agent/tests/test_offline_security_config_idempotency_contract.py",
    ),
    "contract_doctor": (
        "agent_py_agent/agent/contracts/contract_doctor.py",
        "agent_py_agent/tests/test_contract_doctor.py",
    ),
    "effective_contract_snapshot": (
        "agent_py_agent/agent/contracts/effective_contract_snapshot.py",
        "agent_py_agent/tests/test_effective_contract_snapshot.py",
    ),
    "verification_evidence": (
        "agent_py_agent/agent/verification/runtime.py",
        "agent_py_agent/agent/verification/repository.py",
        "agent_py_agent/tests/test_verification_runtime.py",
        "agent_py_agent/tests/test_verification_repository.py",
    ),
    "plan_contract": (
        "agent_py_agent/agent/contracts/offline_plan_contract.py",
        "agent_py_agent/tests/test_offline_plan_contract.py",
    ),
    "event_order": (
        "agent_py_agent/agent/contracts/offline_event_order_contract.py",
        "agent_py_agent/tests/test_offline_event_order_contract.py",
    ),
    "persistence_consistency": (
        "agent_py_agent/agent/contracts/offline_persistence_contract.py",
        "agent_py_agent/tests/test_offline_persistence_contract.py",
    ),
    "side_effect": (
        "agent_py_agent/agent/contracts/offline_side_effect_contract.py",
        "agent_py_agent/tests/test_offline_side_effect_contract.py",
    ),
    "prompt_context": (
        "agent_py_agent/agent/contracts/offline_prompt_context_contract.py",
        "agent_py_agent/tests/test_offline_prompt_context_contract.py",
    ),
    "model_adapter": (
        "agent_py_agent/agent/contracts/offline_model_adapter_contract.py",
        "agent_py_agent/tests/test_offline_model_adapter_contract.py",
    ),
    "scheduler_resource": (
        "agent_py_agent/agent/contracts/offline_scheduler_resource_contract.py",
        "agent_py_agent/tests/test_offline_scheduler_resource_contract.py",
    ),
    "channel_browser": (
        "agent_py_agent/agent/contracts/offline_channel_browser_contract.py",
        "agent_py_agent/tests/test_offline_channel_browser_contract.py",
    ),
    "defense_audit_observability": (
        "agent_py_agent/agent/contracts/offline_defense_audit_observability_contract.py",
        "agent_py_agent/tests/test_offline_defense_audit_observability_contract.py",
    ),
    "combination_failure": (
        "agent_py_agent/agent/contracts/offline_combination_failure_contract.py",
        "agent_py_agent/tests/test_offline_combination_failure_contract.py",
    ),
    "main_agent_core_entrypoints": (
        "agent_py_agent/agent/contracts/main_agent_core_entrypoints.py",
        "agent_py_agent/tests/test_main_agent_core_entrypoints.py",
    ),
    "dry_run_mainline": (
        "agent_py_agent/agent/contracts/dry_run_mainline_contract.py",
        "agent_py_agent/tests/test_dry_run_mainline_contract.py",
    ),
    "live_llm_fake_tool": (
        "agent_py_agent/agent/contracts/live_llm_fake_tool_contract.py",
        "agent_py_agent/tests/test_live_llm_fake_tool_contract.py",
    ),
    "tool_adapter_readiness": (
        "agent_py_agent/agent/contracts/tool_adapter_readiness_contract.py",
        "agent_py_agent/tests/test_tool_adapter_readiness_contract.py",
    ),
    "real_tool_dry_run": (
        "agent_py_agent/agent/contracts/real_tool_dry_run_contract.py",
        "agent_py_agent/tests/test_real_tool_dry_run_contract.py",
    ),
    "task_tree_ledger": (
        "agent_py_agent/agent/contracts/task_tree_ledger_contract.py",
        "agent_py_agent/tests/test_task_tree_ledger_contract.py",
    ),
    "long_task_recovery": (
        "agent_py_agent/agent/contracts/long_task_recovery_contract.py",
        "agent_py_agent/tests/test_long_task_recovery_contract.py",
    ),
    "failure_sample_library": (
        "agent_py_agent/agent/contracts/failure_sample_library_contract.py",
        "agent_py_agent/tests/test_failure_sample_library_contract.py",
    ),
    "small_real_acceptance_gate": (
        "agent_py_agent/agent/contracts/small_real_acceptance_gate.py",
        "agent_py_agent/tests/test_small_real_acceptance_gate.py",
    ),
    "small_real_acceptance_runner": (
        "agent_py_agent/agent/contracts/small_real_acceptance_runner.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "failure_sample_capture": (
        "agent_py_agent/agent/contracts/failure_sample_capture.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "task_tree_scenario": (
        "agent_py_agent/agent/contracts/task_tree_scenario.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "long_task_recovery_scenario": (
        "agent_py_agent/agent/contracts/long_task_recovery_scenario.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "medium_real_acceptance_runner": (
        "agent_py_agent/agent/contracts/medium_real_acceptance_runner.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "pre_real_task_validation": (
        "agent_py_agent/agent/contracts/pre_real_task_validation.py",
        "agent_py_agent/tests/test_pre_real_task_validation.py",
    ),
    "llm_activation_readiness": (
        "agent_py_agent/agent/contracts/llm_activation_readiness.py",
        "agent_py_agent/tests/test_llm_activation_readiness.py",
    ),
    "real_run_review": (
        "agent_py_agent/agent/contracts/real_run_review.py",
        "agent_py_agent/tests/test_real_run_review_contract.py",
    ),
    "runtime_gate_ledger": (
        "agent_py_agent/agent/local_storage/runtime_gate_ledger.py",
        "agent_py_agent/tests/test_runtime_gate_ledger.py",
    ),
}


# LLM: 报告仅含真实合同覆盖结果；不保留已退役的尺寸派生字段或假零值。
# 类用途: 保存缺失领域及对应文件问题，供CLI和离线覆盖检查读取。
@dataclass(frozen=True)
class OfflineContractMatrixReport:
    ok: bool
    missing_areas: tuple[str, ...]
    findings: tuple[dict[str, str], ...]


# LLM: 只读取REQUIRED_AREAS文件存在性，不运行合同测试；任何真实缺项仍失败，不再读取或生成尺寸报告。
# 函数用途: 汇总离线合同实现与测试入口的缺项，不把覆盖索引当行为验收。
def check_offline_contract_matrix(repo_root: Path) -> OfflineContractMatrixReport:
    root = Path(repo_root)
    findings: list[dict[str, str]] = []
    missing = _missing_areas(root, findings)
    return OfflineContractMatrixReport(
        ok=not missing and not findings,
        missing_areas=tuple(missing),
        findings=tuple(findings),
    )


def _missing_areas(root: Path, findings: list[dict[str, str]]) -> list[str]:
    missing: list[str] = []
    for area, paths in REQUIRED_AREAS.items():
        missing_paths = [rel for rel in paths if not (root / rel).is_file()]
        if not missing_paths:
            continue
        missing.append(area)
        for rel in missing_paths:
            findings.append(_finding("OFFLINE_MATRIX_FILE_MISSING", area, rel))
    return missing


def _finding(code: str, area: str, detail: str) -> dict[str, str]:
    return {"code": code, "area": area, "detail": detail}


# LLM: 保留原CLI参数、真实缺项的非零退出和JSON输出模式；输出仅包含合同覆盖字段，不执行产品行为。
# 函数用途: 检查离线合同文件索引并打印真实缺项，供本地和CI调用。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate offline agent contract matrix coverage.")
    parser.add_argument("--repo-root", default=".", help="Repository root to validate.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    args = parser.parse_args(argv)
    report = check_offline_contract_matrix(Path(args.repo_root).resolve())
    payload = {
        "ok": report.ok,
        "missing_areas": list(report.missing_areas),
        "findings": list(report.findings),
    }
    _print_report(payload, json_output=bool(args.json))
    return 0 if report.ok else 1


def _print_report(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"OFFLINE_CONTRACT_MATRIX ok={payload['ok']} missing={payload['missing_areas']}")
    for item in payload["findings"]:
        print(f"- {item['code']}: {item['area']} :: {item['detail']}")


__all__ = ["OfflineContractMatrixReport", "check_offline_contract_matrix", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
