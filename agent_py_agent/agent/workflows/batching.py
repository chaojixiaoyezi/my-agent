"""确定性切批、有界调度及聚合；账本写入只由协调线程调用。"""

import json
import keyword
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context


class ForeachInvalid(ValueError):
    """声明或运行时输入违反foreach合同。"""

    error_code = 'WORKFLOW_FOREACH_INVALID'

    # LLM: 错误码与精确路径一起保留，不将非法输入当作空数据。
    # 函数用途: 提供统一的配置及输入错误。
    def __init__(self, message):
        super().__init__(f'{self.error_code}: {message}')


# LLM: 配置校验不执行步骤、不访问磁盘。
# 函数用途: 校验foreach声明。
def validate_foreach(value):
    if not isinstance(value, dict) or set(value) - {
            'from', 'batch_size', 'max_parallel', 'as', 'max_batch_tokens', 'on_batch_error', 'mode'}:
        raise ForeachInvalid('foreach必须是合法配置对象')
    path = value.get('from')
    if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+', path):
        raise ForeachInvalid('from必须是inputs.<name>或<step_id>.<path>')
    normalized = {'batch_size': 50, 'max_parallel': 1, 'as': 'items', 'max_batch_tokens': None,
                  'on_batch_error': 'fail_fast', 'mode': 'map', **value}
    for name, maximum in (('batch_size', 500), ('max_parallel', 8)):
        if type(normalized[name]) is not int or not 1 <= normalized[name] <= maximum:
            raise ForeachInvalid(f'{name}超出范围')
    alias = normalized['as']
    if not isinstance(alias, str) or not alias.isidentifier() or keyword.iskeyword(alias):
        raise ForeachInvalid('as必须是标识符')
    budget = normalized['max_batch_tokens']
    if budget is not None and (type(budget) is not int or budget < 1):
        raise ForeachInvalid('max_batch_tokens必须是正整数或null')
    if normalized['on_batch_error'] not in ('fail_fast', 'continue') or normalized['mode'] not in ('map', 'aggregate'):
        raise ForeachInvalid('未知批错误策略或聚合模式')
    return normalized


# LLM: 只遍历显式JSON路径，缺失和非数组均拒绝，不读取自然语言或磁盘。
# 函数用途: 从原输入或前步输出定位待分批列表。
def resolve_items(path, inputs, outputs):
    parts = path.split('.')
    try:
        value = inputs if parts[0] == 'inputs' else outputs[parts[0]]
        for part in parts[1:]:
            value = value[part]
    except (KeyError, TypeError, IndexError):
        raise ForeachInvalid(f'from路径不存在: {path}') from None
    if not isinstance(value, list):
        raise ForeachInvalid(f'from路径不是列表: {path}')
    return value


# LLM: 输入顺序及预算决定分片，不读取任何运行状态。
# 函数用途: 生成确定性批计划。
def pack_items(items, *, batch_size=50, max_batch_tokens=None):
    validate_foreach({'from': 'inputs.items', 'batch_size': batch_size, 'max_batch_tokens': max_batch_tokens})
    if not isinstance(items, list):
        raise ForeachInvalid('切批输入必须是列表')
    result, current, size, start = [], [], 2, 0
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
        proposed = size + item_size + bool(current)
        if current and (len(current) == batch_size or max_batch_tokens is not None and proposed > max_batch_tokens * 4):
            result.append({'items': current, 'start': start, 'end': start + len(current), 'oversize': False})
            start += len(current)
            current, size = [], 2
        current.append(item)
        size += item_size + (len(current) > 1)
        if max_batch_tokens is not None and size > max_batch_tokens * 4:
            result.append({'items': current, 'start': start, 'end': start + 1, 'oversize': True})
            start += 1
            current, size = [], 2
    if current:
        result.append({'items': current, 'start': start, 'end': start + len(current), 'oversize': False})
    return result


# LLM: 相同键的列表按批顺序拼接，其他字典键按后批覆盖，不改变输入对象。
# 函数用途: 合并有序批输出，供下游步骤直接消费。
def merge_outputs(values):
    if not values:
        return {}
    if all(isinstance(value, list) for value in values):
        return [item for value in values for item in value]
    if not all(isinstance(value, dict) for value in values):
        raise ForeachInvalid('aggregate输出必须同为列表或对象')
    merged = {}
    for value in values:
        for key, item in value.items():
            if key in merged and isinstance(merged[key], list) and isinstance(item, list):
                merged[key] = [*merged[key], *item]
            else:
                merged[key] = item
    return merged


# LLM: 统计只由本次步骤的批账本投影，不访问全局历史。
# 函数用途: 生成C-BATCH-PROGRESS摘要。
def batch_summary(rows):
    counts = dict.fromkeys(('total', 'completed', 'failed', 'blocked', 'skipped'), 0)
    counts['total'] = len(rows)
    names = {'succeeded': 'completed', 'failed': 'failed', 'blocked': 'blocked', 'skipped': 'skipped'}
    for row in rows:
        if row['status'] in names:
            counts[names[row['status']]] += 1
    return counts


# LLM: 在途Future严格不超过max_parallel；所有publish都在协调线程，退出前等待已启动体结束。
# 函数用途: 调度尚未完成批，保留上下文并在每个完成边界发表账本。
def dispatch_batches(indices, execute, publish, *, prepare=None, max_parallel=1, fail_fast=True):
    if max_parallel == 1:
        for index in indices:
            if prepare is not None:
                prepare(index)
            if publish(index, execute(index)) and fail_fast:
                break
        return
    remaining = iter(indices)
    stopped = False
    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix='workflow-batch') as pool:
        active = {}
        while active or not stopped:
            while not stopped and len(active) < max_parallel:
                index = next(remaining, None)
                if index is None:
                    stopped = True
                    break
                if prepare is not None:
                    prepare(index)
                active[pool.submit(copy_context().run, execute, index)] = index
            if not active:
                break
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in sorted(completed, key=lambda item: active[item]):
                index = active.pop(future)
                if publish(index, future.result()) and fail_fast:
                    stopped = True
