"""工作流类型化直接工具调用步骤执行器 (WF-01)。

负责正式 DAG 运行中 mode='tool_call' 步骤的执行：
提取 tool_spec/config、映射并校验参数模式、基于 Engine 注入的工具分派器/注册表
分发调用、生成确定性幂等 effect_id 与耐久工作流工具回执 (workflow-tool-receipt.v1)。
"""
from __future__ import annotations

import inspect
from typing import Any

from .models import output_value, payload, redact_bearer_values

WORKFLOW_TOOL_RECEIPT_SCHEMA = "workflow-tool-receipt.v1"

# 默认禁止递归运行工作流的工具名称
_RECURSIVE_WORKFLOW_TOOL_NAMES = {
    "workflows",
    "workflows.run",
    "workflow_run",
    "run_workflow",
    "workflow.run",
}

# 默认受保护/破坏性工具名称，需要显式授权
_DEFAULT_DESTRUCTIVE_TOOL_NAMES = {
    "delete_file",
    "rm",
    "remove_file",
    "drop_table",
    "truncate_table",
}


# 函数用途: 解析单个参数引用，支持 $inputs、上游 outputs、step.input_snapshot 与字面量。
def _resolve_argument_reference(ref: Any, step: Any, run: Any, outputs: dict[str, Any]) -> Any:
    if isinstance(ref, dict):
        if "step_id" in ref:
            return output_value(ref, outputs, getattr(run, "inputs", None))
        return {k: _resolve_argument_reference(v, step, run, outputs) for k, v in ref.items()}
    if isinstance(ref, list):
        return [_resolve_argument_reference(item, step, run, outputs) for item in ref]
    if isinstance(ref, str):
        if ref == "$inputs":
            return getattr(run, "inputs", {})
        if ref.startswith("$inputs."):
            key = ref[len("$inputs."):]
            inputs = getattr(run, "inputs", {})
            if isinstance(inputs, dict) and key in inputs:
                return inputs[key]
            if isinstance(step.input_snapshot, dict) and key in step.input_snapshot:
                return step.input_snapshot[key]
            raise KeyError(f"Tool input missing from $inputs: {key}")
        if "." in ref:
            step_id, key = ref.split(".", 1)
            if step_id in outputs:
                out = outputs[step_id]
                if isinstance(out, dict) and key in out:
                    return out[key]
        if isinstance(step.input_snapshot, dict) and ref in step.input_snapshot:
            return step.input_snapshot[ref]
        if ref in outputs:
            return outputs[ref]
        return ref
    return ref


# 函数用途: 验证 JSON 类型是否与声明的 schema 类型匹配。
def _is_json_type_match(val: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(_is_json_type_match(val, t) for t in expected_type)
    if expected_type == "string":
        return isinstance(val, str)
    if expected_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected_type == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected_type == "boolean":
        return isinstance(val, bool)
    if expected_type == "array":
        return isinstance(val, (list, tuple))
    if expected_type == "object":
        return isinstance(val, dict)
    if expected_type == "null":
        return val is None
    return True


# 函数用途: 根据工具提供的参数 JSON Schema 校验已合并的实参。
def _validate_arguments_against_schema(schema: dict[str, Any] | None, arguments: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(schema, dict):
        return True, ""
    # 必填字段检查
    required_fields = schema.get("required")
    if isinstance(required_fields, (list, tuple)):
        for field in required_fields:
            if field not in arguments:
                return False, f"Missing required parameter: {field}"
    # 属性类型检查
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for param_name, param_val in arguments.items():
            if param_name in properties and isinstance(properties[param_name], dict):
                prop_spec = properties[param_name]
                expected_type = prop_spec.get("type")
                if expected_type is not None and param_val is not None:
                    if not _is_json_type_match(param_val, expected_type):
                        return False, (
                            f"Parameter '{param_name}' type mismatch: expected {expected_type}, "
                            f"got {type(param_val).__name__}"
                        )
    # additionalProperties 检查
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        unexpected = set(arguments.keys()) - set(properties.keys())
        if unexpected:
            return False, f"Unexpected additional parameters not allowed by schema: {sorted(unexpected)}"
    return True, ""


# 函数用途: 提取工具对象的参数 Schema 定义。
def _extract_tool_schema(tool_obj: Any, tool_spec: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(tool_spec.get("parameters_schema"), dict):
        return tool_spec["parameters_schema"]
    if tool_obj is None:
        return None
    for attr in ("parameters", "input_schema", "schema"):
        val = getattr(tool_obj, attr, None)
        if isinstance(val, dict):
            return val
    model_spec = getattr(tool_obj, "model_spec", None)
    if model_spec is not None:
        spec_schema = getattr(model_spec, "input_schema", None)
        if isinstance(spec_schema, dict):
            return spec_schema
    return None


# 函数用途: 执行正式 Typed Direct-Tool 单步调用 (WF-01)。
def execute_tool_step(
    *,
    engine: Any,
    run: Any,
    workflow: dict[str, Any],
    step: Any,
    spec: dict[str, Any],
    context: Any,
    outputs: dict[str, Any],
) -> None:
    if not isinstance(getattr(step, "escalation_result", None), dict):
        step.escalation_result = {}

    # 1. 提取并校验 tool_spec
    tool_spec = spec.get("tool_spec")
    if tool_spec is None:
        tool_spec = spec.get("config")
    if not isinstance(tool_spec, dict):
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_SPEC_INVALID"
        step.error_message = "tool_spec or config must be a dictionary"
        step.needs_repair = True
        return

    raw_tool_name = tool_spec.get("tool_name")
    if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_SPEC_INVALID"
        step.error_message = "tool_name must be a non-empty string"
        step.needs_repair = True
        return
    tool_name = raw_tool_name.strip()

    raw_arguments = tool_spec.get("arguments")
    raw_arguments_map = tool_spec.get("arguments_map")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_SPEC_INVALID"
        step.error_message = "tool arguments must be a dictionary"
        step.needs_repair = True
        return
    if raw_arguments_map is not None and not isinstance(raw_arguments_map, dict):
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_SPEC_INVALID"
        step.error_message = "tool arguments_map must be a dictionary"
        step.needs_repair = True
        return

    # 2. 合并实参与映射参数
    resolved_arguments: dict[str, Any] = {}
    if isinstance(step.input_snapshot, dict):
        resolved_arguments.update(payload(step.input_snapshot))
    if isinstance(raw_arguments, dict):
        resolved_arguments.update(payload(raw_arguments))
    if isinstance(raw_arguments_map, dict):
        try:
            for arg_key, arg_ref in raw_arguments_map.items():
                resolved_arguments[arg_key] = _resolve_argument_reference(arg_ref, step, run, outputs)
        except Exception as exc:
            step.status = "failed"
            step.error_code = "WORKFLOW_TOOL_SPEC_INVALID"
            step.error_message = f"Failed to resolve arguments_map reference: {exc}"
            step.needs_repair = True
            return

    # 3. 检查调度器/注册表可用性 (严格禁止 eval 或任意全局函数调用)
    dispatcher = getattr(engine, "tool_dispatcher", None)
    registry = getattr(engine, "tool_registry", None)
    coord_dispatcher = getattr(engine, "tool_operation_coordinator", None)

    if dispatcher is None and registry is None and coord_dispatcher is None:
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_EXECUTOR_UNAVAILABLE"
        step.error_message = "No tool dispatcher, registry, or coordinator wired on workflow engine"
        step.needs_repair = True
        return

    # 4. 权限与安全策略检查 (黑白名单、递归防护、破坏性工具防护)
    # (a) 禁止未显式授权的工作流递归调用
    if tool_name in _RECURSIVE_WORKFLOW_TOOL_NAMES:
        allow_recursive = bool(spec.get("allow_recursive_workflow") or tool_spec.get("allow_recursion"))
        if not allow_recursive:
            step.status = "failed"
            step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
            step.error_message = f"Recursive workflow execution via tool '{tool_name}' is forbidden"
            step.needs_repair = True
            return

    # (b) 显式黑名单检查
    denied_tools = set(spec.get("denied_tools") or ())
    if isinstance(getattr(run, "business_context", None), dict):
        denied_tools.update(run.business_context.get("denied_tools") or ())
    if getattr(engine, "tool_denylist", None):
        denied_tools.update(engine.tool_denylist)
    if tool_name in denied_tools:
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
        step.error_message = f"Tool '{tool_name}' is in the forbidden tool denylist"
        step.needs_repair = True
        return

    # (c) 显式白名单检查
    allowed_tools = None
    if spec.get("allowed_tools") is not None:
        allowed_tools = set(spec["allowed_tools"])
    elif isinstance(getattr(run, "business_context", None), dict) and run.business_context.get("allowed_tools") is not None:
        allowed_tools = set(run.business_context["allowed_tools"])
    elif getattr(engine, "tool_allowlist", None) is not None:
        allowed_tools = set(engine.tool_allowlist)

    if allowed_tools is not None and tool_name not in allowed_tools:
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
        step.error_message = f"Tool '{tool_name}' is not in the authorized tool allowlist"
        step.needs_repair = True
        return

    # (d) 破坏性/特权工具策略检查
    b_context = getattr(run, "business_context", {}) if isinstance(getattr(run, "business_context", None), dict) else {}
    is_read_only_mode = bool(spec.get("read_only") or b_context.get("read_only"))
    is_destructive = tool_name in _DEFAULT_DESTRUCTIVE_TOOL_NAMES or bool(tool_spec.get("destructive"))
    if is_destructive and (is_read_only_mode or not spec.get("allow_destructive", False)):
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
        step.error_message = f"Destructive tool '{tool_name}' is not permitted under current workflow policy"
        step.needs_repair = True
        return

    # (e) 外部自定义授权钩子检查
    authorizer = getattr(engine, "tool_authorizer", None)
    if callable(authorizer):
        try:
            authorized = authorizer(tool_name, resolved_arguments, run=run, step=step)
            if not authorized:
                step.status = "failed"
                step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
                step.error_message = f"Tool '{tool_name}' authorization rejected by tool authorizer"
                step.needs_repair = True
                return
        except Exception as exc:
            step.status = "failed"
            step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
            step.error_message = f"Tool authorization error: {exc}"
            step.needs_repair = True
            return

    # 5. 查找工具实例并进行参数 Schema 校验
    tool_obj = None
    if registry is not None:
        if hasattr(registry, "get_tool") and callable(registry.get_tool):
            tool_obj = registry.get_tool(tool_name)
        elif hasattr(registry, "tools") and isinstance(registry.tools, dict):
            tool_obj = registry.tools.get(tool_name)

    if tool_obj is None and registry is not None and dispatcher is None and coord_dispatcher is None:
        step.status = "failed"
        step.error_code = "WORKFLOW_TOOL_PERMISSION_DENIED"
        step.error_message = f"Tool '{tool_name}' is not registered in the tool registry"
        step.needs_repair = True
        return

    schema = _extract_tool_schema(tool_obj, tool_spec)
    if schema is not None:
        schema_ok, schema_err = _validate_arguments_against_schema(schema, resolved_arguments)
        if not schema_ok:
            step.status = "failed"
            step.error_code = "WORKFLOW_TOOL_SCHEMA_INVALID"
            step.error_message = schema_err
            step.needs_repair = True
            return

    # 6. 生成确定性幂等身份
    attempt_token = step.escalation_result.get("attempt_id") or str(getattr(step, "attempt_count", 1))
    effect_id = f"workflow:{run.workflow_run_id}:{step.step_id}:{attempt_token}"
    logical_step_key = f"workflow-step:{run.workflow_run_id}:{step.step_id}"

    owner_id = getattr(run, "owner_id", "")
    space_id = b_context.get("space_id", "")

    # 7. 调用执行分派
    raw_result = None
    try:
        # 优先级 A: 明确的 tool_dispatcher
        if dispatcher is not None:
            scoped_kwargs = {
                "owner_id": owner_id,
                "space_id": space_id,
                "idempotency_key": effect_id,
                "logical_step_key": logical_step_key,
                "run": run,
                "step": step,
            }
            if hasattr(dispatcher, "execute_scoped") and callable(dispatcher.execute_scoped):
                try:
                    raw_result = dispatcher.execute_scoped(tool_name, resolved_arguments, **scoped_kwargs)
                except TypeError:
                    raw_result = dispatcher.execute_scoped(tool_name, resolved_arguments)
            elif hasattr(dispatcher, "execute") and callable(dispatcher.execute):
                try:
                    raw_result = dispatcher.execute(tool_name, resolved_arguments, **scoped_kwargs)
                except TypeError:
                    raw_result = dispatcher.execute(tool_name, resolved_arguments)
            elif hasattr(dispatcher, "dispatch") and callable(dispatcher.dispatch):
                try:
                    raw_result = dispatcher.dispatch(tool_name, resolved_arguments, **scoped_kwargs)
                except TypeError:
                    raw_result = dispatcher.dispatch(tool_name, resolved_arguments)
            elif callable(dispatcher):
                try:
                    raw_result = dispatcher(tool_name, resolved_arguments, **scoped_kwargs)
                except TypeError:
                    try:
                        raw_result = dispatcher(tool_name, resolved_arguments)
                    except TypeError:
                        raw_result = dispatcher(resolved_arguments)
            else:
                raise ValueError("Configured tool_dispatcher does not provide executable interface")

        # 优先级 B: 基于 ToolOperationCoordinator 包装的分派器
        elif coord_dispatcher is not None:
            raw_result = coord_dispatcher(
                tool_name=tool_name,
                arguments=resolved_arguments,
                owner_id=owner_id,
                space_id=space_id,
                idempotency_key=effect_id,
                logical_step_key=logical_step_key,
            )

        # 优先级 C: 注册表中的工具对象
        elif tool_obj is not None:
            call_kwargs = {
                "owner_id": owner_id,
                "space_id": space_id,
                "idempotency_key": effect_id,
            }
            if hasattr(tool_obj, "execute_scoped") and callable(tool_obj.execute_scoped):
                try:
                    raw_result = tool_obj.execute_scoped(resolved_arguments, **call_kwargs)
                except TypeError:
                    raw_result = tool_obj.execute_scoped(resolved_arguments)
            elif hasattr(tool_obj, "execute") and callable(tool_obj.execute):
                try:
                    raw_result = tool_obj.execute(resolved_arguments, **call_kwargs)
                except TypeError:
                    raw_result = tool_obj.execute(resolved_arguments)
            elif hasattr(tool_obj, "run") and callable(tool_obj.run):
                raw_result = tool_obj.run(resolved_arguments)
            elif callable(tool_obj):
                raw_result = tool_obj(resolved_arguments)
            else:
                raise ValueError(f"Tool '{tool_name}' in registry is not callable")

    except (KeyboardInterrupt, SystemExit, InterruptedError):
        raise
    except Exception as exc:
        if exc.__class__.__name__ == "ToolCancelled":
            raise
        step.status = "failed"
        step.effect_id = effect_id
        step.error_code = getattr(exc, "error_code", "") or "WORKFLOW_TOOL_EXECUTION_FAILED"
        step.error_message = str(exc)
        step.needs_repair = True
        step.escalation_result["tool_receipt"] = {
            "schema": WORKFLOW_TOOL_RECEIPT_SCHEMA,
            "tool_name": tool_name,
            "effect_id": effect_id,
            "operation_id": effect_id,
            "effect_outcome": getattr(exc, "effect_outcome", "failed"),
            "status": "failed",
            "error_code": step.error_code,
            "output_summary": {"error": str(exc)},
        }
        return

    # 8. 解析执行结果与持久化回执
    operation_id = effect_id
    effect_outcome = "succeeded"
    parsed_result: Any = raw_result
    is_success = True
    err_code = ""
    err_msg = ""

    # (a) ToolHandlerOutcome 协议
    if hasattr(raw_result, "ok"):
        is_success = bool(raw_result.ok)
        operation_id = str(getattr(raw_result, "operation_id", "") or effect_id)
        effect_outcome = str(getattr(raw_result, "effect_outcome", "") or ("succeeded" if is_success else "failed"))
        if hasattr(raw_result, "result"):
            parsed_result = raw_result.result
        elif hasattr(raw_result, "to_dict"):
            parsed_result = raw_result.to_dict()
        if not is_success:
            err_code = str(getattr(raw_result, "error_code", "") or "WORKFLOW_TOOL_EXECUTION_FAILED")
            err_msg = str(getattr(raw_result, "error_message", "") or getattr(raw_result, "message", "") or "Tool failed")

    # (b) 字典结果协议
    elif isinstance(raw_result, dict):
        if raw_result.get("ok") is False or raw_result.get("status") == "failed":
            is_success = False
            err_code = str(raw_result.get("error_code") or "WORKFLOW_TOOL_EXECUTION_FAILED")
            err_msg = str(raw_result.get("error_message") or raw_result.get("message") or raw_result.get("error") or "Tool failed")
            effect_outcome = str(raw_result.get("effect_outcome") or "failed")
            operation_id = str(raw_result.get("operation_id") or effect_id)
        else:
            is_success = True
            operation_id = str(raw_result.get("operation_id") or effect_id)
            effect_outcome = str(raw_result.get("effect_outcome") or "succeeded")

    # 9. 状态与回执落地
    step.effect_id = effect_id
    if is_success:
        output_payload = payload(parsed_result if isinstance(parsed_result, dict) else {"result": parsed_result})
        step.status = "succeeded"
        step.output_snapshot = output_payload
        summary = redact_bearer_values(output_payload)
        step.escalation_result["tool_receipt"] = {
            "schema": WORKFLOW_TOOL_RECEIPT_SCHEMA,
            "tool_name": tool_name,
            "effect_id": effect_id,
            "operation_id": operation_id,
            "effect_outcome": effect_outcome,
            "status": "succeeded",
            "error_code": "",
            "output_summary": summary,
        }
    else:
        step.status = "failed"
        step.error_code = err_code or "WORKFLOW_TOOL_EXECUTION_FAILED"
        step.error_message = err_msg or "Tool execution failed"
        step.needs_repair = True
        summary = redact_bearer_values(payload(parsed_result if isinstance(parsed_result, dict) else {"error": err_msg}))
        step.escalation_result["tool_receipt"] = {
            "schema": WORKFLOW_TOOL_RECEIPT_SCHEMA,
            "tool_name": tool_name,
            "effect_id": effect_id,
            "operation_id": operation_id,
            "effect_outcome": effect_outcome or "failed",
            "status": "failed",
            "error_code": step.error_code,
            "output_summary": summary,
        }
