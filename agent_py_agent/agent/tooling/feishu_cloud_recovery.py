# LLM: 核对沿原operation调用，只读同身份准确范围；没有供应商位置/幂等证据的append始终UNKNOWN，不能换编号重做。
# 模块用途: 用目标状态证明可核验更新已达到预期，保留真正未知写入和既有分析产物。
from __future__ import annotations

import hashlib
import json

from ..capability.runtime_config_reload import capability_config_for_agent
from ..user_space.feishu_sheet_verification import _compare_cells, _verification_range
from .models import ToolHandlerOutcome, ToolOperationReconciliation


# LLM: 当前runner必须仍代表原run/task，读取继续经过原云工具owner/身份/网络/协作者门，不授予新读写权限。
# 函数用途: 仅对字面值sheet_update做一次有界读回；一致则结算原操作，不一致仍未知且不执行写入。
def reconcile_cloud_operation(tool, params, context):
    current = getattr(tool.agent, '_current_run_params', None)
    if (current is None or current.run_id != context.run_id or current.task_id != context.task_id
            or context.tool_name != 'feishu_cloud'):
        return ToolOperationReconciliation(reason='original_operation_scope_unavailable')
    if params.get('action') != 'sheet_update':
        return ToolOperationReconciliation(reason='append_destination_or_provider_receipt_unproven')
    config = capability_config_for_agent(tool.agent)
    if config is None or not config.feishu_cloud_verify_sheet_writes_enabled:
        return ToolOperationReconciliation(reason='readback_disabled')
    read_range = _verification_range(params, {})
    if not read_range or not _literal_values(params.get('values')):
        return ToolOperationReconciliation(reason='exact_literal_range_unavailable')
    read_params = {key:value for key, value in params.items() if key not in {'values', 'children', 'block_id', 'page_token'}}
    read_params.update(action='sheet_read', range=read_range)
    result = tool.execute(read_params)
    if not result.ok:
        return ToolOperationReconciliation(reason='authorized_readback_unavailable')
    return _reconcile_readback(params, context, result, read_range)


# LLM: 公式、空值和复杂对象可能被供应商变换，不得用文本相似或模型推断解除未知门。
# 函数用途: 只接受原精确比较器能证明的字面二维矩形。
def _literal_values(values):
    return (isinstance(values, list) and bool(values) and all(isinstance(row, list) and bool(row) for row in values)
        and all(type(value) in {str, int, float, bool} and not (isinstance(value, str) and (value.startswith('=') or '[private]' in value or '<redacted' in value))
                for row in values for value in row))


# LLM: 响应范围和形状必须确切；只把读回digest和operation引用带回原账，不复制任何单元格正文或重做分析。
# 函数用途: 将准确目标状态核验转为原协调器可CAS结算的成功证明，其他结果保留UNKNOWN。
def _reconcile_readback(params, context, result, read_range):
    try:
        payload = json.loads(result.output)
        block = payload.get('data', {}).get('valueRange', {})
        actual = block.get('values')
        if (block.get('range') != read_range or not isinstance(actual, list)
                or len(actual) != len(params['values']) or any(not isinstance(row, list) for row in actual)):
            return ToolOperationReconciliation(reason='readback_range_or_shape_unconfirmed')
        comparison = _compare_cells(params['values'], actual)
    except (TypeError, ValueError, AttributeError):
        return ToolOperationReconciliation(reason='readback_response_unverifiable')
    if comparison['status'] != 'verified':
        return ToolOperationReconciliation(reason='target_state_unconfirmed')
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    source_ref = 'feishu-readback://' + context.operation_id
    facts = {'status':'verified', 'reason':'exact_target_state_confirmed', 'source_ref':source_ref,
        'operation_id':context.operation_id, 'readback_digest':digest}
    outcome = ToolHandlerOutcome('feishu_cloud', True, '原操作的准确目标范围已核对一致；已结算原操作，没有重复写入。',
        effect_source_ref=source_ref, result_envelope={'cloud_recovery':facts})
    return ToolOperationReconciliation(outcome='succeeded', source_ref=source_ref, result=outcome,
        reason='exact_target_state_confirmed')
