
from __future__ import annotations

import time as time_module
from dataclasses import dataclass, replace

from ...common.logical_reference_ids import RUN_REQUEST_ID_PREFIX
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


# LLM: 运行编号补齐的唯一入口：只在缺 request_id/attempt_id/run_id/task_id 时生成，已有值原样保留；request_id 前缀取
#   common/logical_reference_ids 的共享常量（与“逻辑引用误当写路径”守卫同源），不读模型参数。改动须同步该守卫的用例。
# 函数用途: 给一次运行补齐请求、尝试、运行、任务编号；四个编号都已齐全时返回同一个对象。
def run_params_with_request_id(params: RunParams) -> RunParams:
    request_id = params.request_id or f"{RUN_REQUEST_ID_PREFIX}{time_module.time_ns()}"
    attempt_id = params.attempt_id or f"attempt-{time_module.time_ns()}"
    run_id = params.run_id or request_id
    task_id = params.task_id or run_id
    if (
        params.request_id == request_id
        and params.attempt_id == attempt_id
        and params.run_id == run_id
        and params.task_id == task_id
    ):
        return params
    return replace(
        params,
        request_id=request_id,
        attempt_id=attempt_id,
        run_id=run_id,
        task_id=task_id,
    )
