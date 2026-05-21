# LLM: Contract layer classification keeps runtime gates distinct from lab scenarios.
# 模块用途: 给合同模块做机器可读分层，避免真实任务套件被误当成生产运行时 gate。

from __future__ import annotations

CONTRACT_LAYER_CORE_RUNTIME = "core_runtime"
CONTRACT_LAYER_FORMAT_VALIDATOR = "format_validator"
CONTRACT_LAYER_OFFLINE_TEST = "offline_test"
CONTRACT_LAYER_LIVE_SCENARIO = "live_scenario"
CONTRACT_LAYER_SUPPORT = "support"

_FORMAT_VALIDATOR_PREFIXES = (
    "artifact_html",
    "artifact_static_site",
    "artifact_structured",
    "artifact_xlsx",
)
_FORMAT_VALIDATOR_NAMES = {
    "artifact_acceptance",
    "artifact_acceptance_models",
    "artifact_collection_contract",
    "artifact_collection_evidence",
    "artifact_collection_mapping",
    "artifact_validator_registry",
}
_OFFLINE_PREFIXES = (
    "offline_",
    "failure_sample",
    "live_llm_fake_tool",
    "llm_activation",
    "shadow_mode",
    "dry_run_mainline",
    "real_tool_dry_run",
)
_LIVE_SCENARIO_PREFIXES = (
    "e2e_matrix",
    "main_agent_foundation",
    "main_agent_real_task",
    "main_agent_task",
    "medium_real_acceptance",
    "pre_real_task",
    "real_run_review",
    "small_real_acceptance",
)
_LIVE_SCENARIO_NAMES = {
    "long_task_recovery_scenario",
    "task_tree_scenario",
}
_CORE_PREFIXES = (
    "gates",
    "state_machine",
    "tool_protocol",
)
_CORE_NAMES = {
    "acceptance_contract",
    "activity_timeout",
    "approval_gate",
    "contract_doctor",
    "effective_contract_snapshot",
    "error_taxonomy",
    "evidence_contract",
    "idempotency",
    "model_call_ledger",
    "run_trace_contract",
    "runtime_cards",
    "runtime_config_contract",
    "task_tree_ledger_contract",
    "tool_call_policy",
    "tool_manifest_contract",
}


# LLM: classify_contract_module maps a contract module to its architecture layer.
# 函数用途: 使用模块名的结构化前缀做分层，不读取业务 prompt 或任务自然语言。
def classify_contract_module(module_name: object) -> str:
    name = _module_basename(module_name)
    if name in _LIVE_SCENARIO_NAMES or _starts_with(name, _LIVE_SCENARIO_PREFIXES):
        return CONTRACT_LAYER_LIVE_SCENARIO
    if _starts_with(name, _OFFLINE_PREFIXES):
        return CONTRACT_LAYER_OFFLINE_TEST
    if name in _FORMAT_VALIDATOR_NAMES or _starts_with(name, _FORMAT_VALIDATOR_PREFIXES):
        return CONTRACT_LAYER_FORMAT_VALIDATOR
    if name in _CORE_NAMES or _starts_with(name, _CORE_PREFIXES):
        return CONTRACT_LAYER_CORE_RUNTIME
    return CONTRACT_LAYER_SUPPORT


# LLM: _module_basename normalizes module paths before layer classification.
# 函数用途: 将文件路径或 dotted module 转成基础模块名，保留 gates.* 前缀。
def _module_basename(module_name: object) -> str:
    text = str(module_name or "").strip().replace("\\", "/")
    if text.endswith(".py"):
        text = text[:-3]
    text = text.rsplit("/", 1)[-1]
    return text.rsplit(".", 1)[-1] if "." in text and not text.startswith("gates.") else text


# LLM: _starts_with checks stable module-name prefixes.
# 函数用途: 用结构化前缀集合分类模块，不读取 prompt 或任务描述。
def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix.rstrip("_") or name.startswith(prefix) for prefix in prefixes)


__all__ = [
    "CONTRACT_LAYER_CORE_RUNTIME",
    "CONTRACT_LAYER_FORMAT_VALIDATOR",
    "CONTRACT_LAYER_LIVE_SCENARIO",
    "CONTRACT_LAYER_OFFLINE_TEST",
    "CONTRACT_LAYER_SUPPORT",
    "classify_contract_module",
]
