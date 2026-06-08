
from __future__ import annotations

import time as time_module
from dataclasses import dataclass, replace

from ..delivery_requirement_materializer import (
    build_delivery_requirement_materializer_prompt,
    delivery_contract_from_user_prompt_structure,
    materialized_delivery_contract,
    materializer_repair_feedback,
)
from ..provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .loop_support import RunParams, run_params_from_values

_AUTO_MATERIALIZE_SOURCES: set[str] = set()
_MAX_MATERIALIZER_REPAIR_ATTEMPTS = 3


@dataclass(frozen=True)
class RunKeywordFields:
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    task_attributes: dict | None = None
    delivery_contract: dict | None = None
    system_prompt_override: str | None = None
    source: str | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None
    context_scope: str | None = None


@dataclass(frozen=True)
class StructuralContractRequest:
    agent: object
    contract_prompt: str
    params: RunParams
    structural_contract: dict


def run_params_from_keywords(params: RunParams, fields: RunKeywordFields) -> RunParams:
    return run_params_from_values(
        params,
        inject=fields.inject,
        prompt_files=fields.prompt_files,
        save=fields.save,
        allowed_tools=fields.allowed_tools,
        granted_capabilities=fields.granted_capabilities,
        write_boundary=fields.write_boundary,
        request_id=fields.request_id,
        run_id=fields.run_id,
        task_id=fields.task_id,
        task_attributes=fields.task_attributes,
        delivery_contract=fields.delivery_contract,
        system_prompt_override=fields.system_prompt_override,
        source=fields.source,
        resume_context=fields.resume_context,
        recovery_task_refs=fields.recovery_task_refs,
        recovery_content_paths=fields.recovery_content_paths,
        recovery_next_actions=fields.recovery_next_actions,
        on_chunk=fields.on_chunk,
        context_scope=fields.context_scope,
    )


def run_params_with_request_id(params: RunParams) -> RunParams:
    request_id = params.request_id or f"run-{time_module.time_ns()}"
    run_id = params.run_id or request_id
    task_id = params.task_id or run_id
    if params.request_id == request_id and params.run_id == run_id and params.task_id == task_id:
        return params
    return replace(params, request_id=request_id, run_id=run_id, task_id=task_id)


def run_params_with_materialized_delivery_contract(agent, user_prompt: str, params: RunParams) -> RunParams:
    if params.delivery_contract is not None:
        return params
    if _is_internal_context_scope(params.context_scope):
        return params
    contract_prompt = _delivery_contract_prompt(user_prompt, params)
    structural_contract = delivery_contract_from_user_prompt_structure(
        contract_prompt,
        workspace_root=getattr(agent, "root", None),
    )
    if replacement := _structural_contract_params(
        StructuralContractRequest(agent, contract_prompt, params, structural_contract)
    ):
        return replacement
    if not _should_materialize_delivery_contract(params):
        return params
    contract = _materialized_contract_with_repairs(agent, contract_prompt, params)
    if not _has_materialized_runtime_contract(contract):
        return params
    return replace(params, delivery_contract=contract)


def _structural_contract_params(request: StructuralContractRequest) -> RunParams | None:
    if not _has_materialized_runtime_contract(request.structural_contract):
        return None
    if not _should_repair_structural_contract(request.structural_contract):
        return replace(request.params, delivery_contract=request.structural_contract)
    repaired = _materialized_contract_with_repairs(request.agent, request.contract_prompt, request.params)
    if _has_materialized_runtime_contract(repaired):
        return replace(request.params, delivery_contract=repaired)
    return replace(request.params, delivery_contract=request.structural_contract)


def _materialized_contract_with_repairs(agent, contract_prompt: str, params: RunParams) -> dict:
    contract = _materialize_delivery_contract(agent, contract_prompt, params)
    for _ in range(_MAX_MATERIALIZER_REPAIR_ATTEMPTS):
        repair_feedback = materializer_repair_feedback(contract)
        if not repair_feedback:
            break
        contract = _materialize_delivery_contract(agent, contract_prompt, params, repair_feedback=repair_feedback)
    return contract


def _delivery_contract_prompt(user_prompt: str, params: RunParams) -> str:
    root_prompt = str(getattr(params, "root_user_prompt", "") or "").strip()
    return root_prompt or user_prompt


def _materialize_delivery_contract(agent, user_prompt: str, params: RunParams, *, repair_feedback: str = "") -> dict:
    prompt = build_delivery_requirement_materializer_prompt(user_prompt, repair_feedback=repair_feedback)
    response = run_with_provider_transient_auto_resume(
        lambda: agent.backend.generate(prompt),
        on_chunk=params.on_chunk if callable(params.on_chunk) else None,
        policy=getattr(agent, "runtime_guard_policy", None),
    )
    return materialized_delivery_contract(response.text, workspace_root=agent.root, user_prompt=user_prompt)


def _should_materialize_delivery_contract(params: RunParams) -> bool:
    return str(params.source or "").strip() in _AUTO_MATERIALIZE_SOURCES


def _is_internal_context_scope(value: object) -> bool:
    return str(value or "").strip().lower() in {"task_local", "control_plane"}


def _has_materialized_runtime_contract(contract: dict) -> bool:
    return any(
        (
            bool(contract.get("artifacts")),
            isinstance(contract.get("delivery_quality_contract"), dict),
            isinstance(contract.get("fact_evidence_contract"), dict),
            isinstance(contract.get("target_coverage_contract"), dict),
            isinstance(contract.get("bootstrap_contract"), dict),
        )
    )


def _should_repair_structural_contract(contract: dict) -> bool:
    return bool(materializer_repair_feedback(contract))
