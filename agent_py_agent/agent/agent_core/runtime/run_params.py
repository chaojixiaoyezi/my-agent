
from __future__ import annotations

import time as time_module
from dataclasses import dataclass, replace

from .loop_support import RunParams, run_params_from_values


@dataclass(frozen=True)
class RunKeywordFields:
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
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


def run_params_from_keywords(params: RunParams, fields: RunKeywordFields) -> RunParams:
    return run_params_from_values(
        params,
        inject=fields.inject,
        prompt_files=fields.prompt_files,
        save=fields.save,
        allowed_tools=fields.allowed_tools,
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
