# LLM: Runtime run-param helpers preserve public SimpleAgent.run compatibility while keeping the mixin thin.
# 模块用途: 归一旧 run() 关键字参数、提前生成 run 标识，并按入口物化交付合同。

from __future__ import annotations

import time as time_module
from dataclasses import dataclass, replace

from .delivery_requirement_materializer import (
    build_delivery_requirement_materializer_prompt,
    materialized_delivery_contract,
)
from .runtime_loop_support import RunParams, run_params_from_values

_AUTO_MATERIALIZE_SOURCES = {"chat", "cli_run", "gateway"}


@dataclass(frozen=True)
class RunCompatibilityFields:
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
    recovery_snapshot: bool | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None
    context_scope: str | None = None


def run_params_from_compat(params: RunParams, fields: RunCompatibilityFields) -> RunParams:
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
        recovery_snapshot=fields.recovery_snapshot,
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
    if not _should_materialize_delivery_contract(params):
        return params
    prompt = build_delivery_requirement_materializer_prompt(user_prompt)
    response = agent.backend.generate(prompt)
    contract = materialized_delivery_contract(response.text, workspace_root=agent.root)
    if not _has_materialized_runtime_contract(contract):
        return params
    return replace(params, delivery_contract=contract)


def _should_materialize_delivery_contract(params: RunParams) -> bool:
    return str(params.source or "").strip() in _AUTO_MATERIALIZE_SOURCES


def _has_materialized_runtime_contract(contract: dict) -> bool:
    return any(
        (
            bool(contract.get("artifacts")),
            isinstance(contract.get("delivery_quality_contract"), dict),
            isinstance(contract.get("fact_evidence_contract"), dict),
            isinstance(contract.get("bootstrap_contract"), dict),
        )
    )
