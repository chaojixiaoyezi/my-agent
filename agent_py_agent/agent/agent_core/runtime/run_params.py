# LLM: 参数规范化与身份补齐各有一个真实实现；只从定义处依赖RunParams，不反向装配工具循环。
# 模块用途: 合并显式调用参数并补齐原运行编号，保留None、子类和宿主对象的原语义。
from __future__ import annotations

import time as time_module
from dataclasses import fields, replace

from ...common.logical_reference_ids import RUN_REQUEST_ID_PREFIX
from .loop_models import RunParams

_RUN_PARAM_FIELD_NAMES = tuple(field.name for field in fields(RunParams))


# LLM: None不覆盖，显式空值/False优先；无更新返回原对象，replace保留子类及本工作片宿主对象，不新增复制或授权。
# 函数用途: 在唯一定义处合并公开run关键字，兼容loop_support的原导入入口。
def run_params_from_values(
    params: RunParams | None = None,
    *,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool | None = None,
    allowed_tools: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    task_attributes: dict | None = None,
    delivery_contract: dict | None = None,
    system_prompt_override: str | None = None,
    source: str | None = None,
    resume_context: bool | None = None,
    recovery_task_refs: list[str] | None = None,
    recovery_content_paths: list[str] | None = None,
    recovery_next_actions: list[str] | None = None,
    on_chunk: object = None,
    context_scope: str | None = None,
) -> RunParams:
    if params is None:
        params = RunParams()
    elif not isinstance(params, RunParams):
        raise TypeError("run() requires params: RunParams keyword argument")

    values = {key: value for key, value in locals().items() if key != "params"}
    updates = {key: values[key] for key in _RUN_PARAM_FIELD_NAMES if values.get(key) is not None}
    if not updates:
        return params
    return replace(params, **updates)


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
