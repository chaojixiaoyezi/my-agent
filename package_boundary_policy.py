from __future__ import annotations

"""Authoritative production-distribution boundary for my-agent.

This module is intentionally repository-level: ``setup.py`` uses it while
building the wheel, and boundary checks use the same functions afterwards.
Source checkouts retain developer harnesses for tests, but production wheels
must not contain them.
"""

from pathlib import PurePosixPath

_CONTRACT_PREFIX = "agent_py_agent.agent.contracts."

# Developer-only acceptance matrices, fake/offline contracts, and review
# harnesses. Generic runtime gates (tool/path/approval/state/artifact) are not
# listed here and remain in the production distribution.
DEV_ONLY_CONTRACT_MODULES = frozenset(
    {
        "activity_timeout",
        "contract_layers",
        "dry_run_mainline_contract",
        "e2e_matrix",
        "e2e_matrix_runner",
        "failure_sample_capture",
        "failure_sample_library_contract",
        "live_llm_fake_tool_contract",
        "llm_activation_readiness",
        "long_task_recovery_contract",
        "long_task_recovery_scenario",
        "main_agent_auto_resume",
        "main_agent_core_entrypoints",
        "main_agent_foundation_contract_cases",
        "main_agent_foundation_models",
        "main_agent_foundation_research",
        "main_agent_foundation_runner",
        "medium_real_acceptance_runner",
        "pre_real_task_validation",
        "real_run_review",
        "real_tool_dry_run_contract",
        "run_trace_contract",
        "runtime_config_contract",
        "sample_neutrality",
        "small_real_acceptance_gate",
        "small_real_acceptance_probes",
        "small_real_acceptance_runner",
        "task_tree_ledger_contract",
        "task_tree_scenario",
        "tool_adapter_readiness_contract",
    }
)

DEV_ONLY_MODULES = frozenset({"agent_py_agent.cli.real_e2e_commands"})


def is_dev_only_module(module_name: str) -> bool:
    name = str(module_name or "").strip()
    if name in DEV_ONLY_MODULES:
        return True
    if not name.startswith(_CONTRACT_PREFIX):
        return False
    base = name.removeprefix(_CONTRACT_PREFIX).split(".", 1)[0]
    return base.startswith("offline_") or base in DEV_ONLY_CONTRACT_MODULES


# LLM: Reject unsafe archive paths before release scanners touch a matching source path; the same
# rule also excludes developer modules during builds. Normal package/runtime paths stay unchanged.
# 函数用途: 统一拒绝越界归档路径和开发专用模块，防止验包时沿恶意文件名读取仓库之外的文件。
def forbidden_distribution_member(member_name: str) -> bool:
    """Return True when an archive member cannot belong to a production wheel."""

    path = PurePosixPath(str(member_name or "").replace("\\", "/"))
    parts = path.parts
    if path.is_absolute() or ".." in parts or (parts and ":" in parts[0]):
        return True
    if len(parts) >= 2 and parts[:2] == ("agent_py_agent", "tests"):
        return True
    if path.suffix != ".py" or not parts or parts[0] != "agent_py_agent":
        return False
    module = ".".join((*parts[:-1], path.stem))
    return is_dev_only_module(module)


__all__ = [
    "DEV_ONLY_CONTRACT_MODULES",
    "DEV_ONLY_MODULES",
    "forbidden_distribution_member",
    "is_dev_only_module",
]
