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


_FOREACH_ALLOWED_KEYS = frozenset({
    'from', 'batch_size', 'max_parallel', 'concurrency', 'as',
    'max_batch_tokens', 'on_batch_error', 'on_item_error', 'mode', 'item_id_key',
})


# LLM: 配置校验不执行步骤、不访问磁盘；支持WF-07公有形式foreach声明。
# 函数用途: 校验foreach声明。
def validate_foreach(value):
    if not isinstance(value, dict) or set(value) - _FOREACH_ALLOWED_KEYS:
        raise ForeachInvalid('foreach必须是合法配置对象')
    path = value.get('from')
    parts = path.split('.') if isinstance(path, str) else []
    if len(parts) < 2 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', parts[0]) or not all(parts):
        raise ForeachInvalid('from必须是inputs.<name>或<step_id>.<path>')

    batch_size = value.get('batch_size', 50)
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ForeachInvalid('batch_size超出范围')

    if 'concurrency' in value:
        concurrency = value['concurrency']
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ForeachInvalid('concurrency超出范围')
        if 'max_parallel' in value:
            max_parallel = value['max_parallel']
            if type(max_parallel) is not int or not 1 <= max_parallel <= 8:
                raise ForeachInvalid('max_parallel超出范围')
            if max_parallel != concurrency:
                raise ForeachInvalid('max_parallel与concurrency冲突')
        else:
            max_parallel = concurrency
    else:
        max_parallel = value.get('max_parallel', 1)
        if type(max_parallel) is not int or not 1 <= max_parallel <= 8:
            raise ForeachInvalid('max_parallel超出范围')

    alias = value.get('as', 'items')
    if not isinstance(alias, str) or not alias.isidentifier() or keyword.iskeyword(alias):
        raise ForeachInvalid('as必须是标识符')

    budget = value.get('max_batch_tokens')
    if budget is not None and (type(budget) is not int or budget < 1):
        raise ForeachInvalid('max_batch_tokens必须是正整数或null')

    on_item_error = value.get('on_item_error')
    if on_item_error is not None and on_item_error not in ('fail_fast', 'continue', 'collect_errors'):
        raise ForeachInvalid('未知项错误策略')

    on_batch_error = value.get('on_batch_error')
    if on_batch_error is not None:
        if on_batch_error not in ('fail_fast', 'continue', 'collect_errors'):
            raise ForeachInvalid('未知批错误策略或聚合模式')
        normalized_batch_error = 'continue' if on_batch_error in ('continue', 'collect_errors') else 'fail_fast'
    elif on_item_error is not None:
        normalized_batch_error = 'continue' if on_item_error in ('continue', 'collect_errors') else 'fail_fast'
    else:
        normalized_batch_error = 'fail_fast'

    normalized_item_error = on_item_error or ('collect_errors' if normalized_batch_error == 'continue' else 'fail_fast')

    mode = value.get('mode', 'map')
    if mode not in ('map', 'aggregate'):
        raise ForeachInvalid('未知批错误策略或聚合模式')

    item_id_key = value.get('item_id_key')
    if item_id_key is not None:
        if not isinstance(item_id_key, str) or not item_id_key.strip():
            raise ForeachInvalid('item_id_key必须是非空字符串或null')

    return {
        'from': path,
        'batch_size': batch_size,
        'max_parallel': max_parallel,
        'concurrency': max_parallel,
        'as': alias,
        'max_batch_tokens': budget,
        'on_batch_error': normalized_batch_error,
        'on_item_error': normalized_item_error,
        'mode': mode,
        'item_id_key': item_id_key,
    }


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


def _deterministic_item_id(item, index, item_id_key=None):
    if item_id_key and isinstance(item, dict):
        val = item.get(item_id_key)
        if val is not None and str(val).strip():
            return str(val).strip()
    if isinstance(item, dict):
        for candidate in ('id', 'item_id', 'key'):
            val = item.get(candidate)
            if val is not None and str(val).strip():
                return str(val).strip()
    return f"item:{index}"


# LLM: 输入顺序及预算决定分片，支持稳定条目身份，不读取任何运行状态。
# 函数用途: 生成确定性批计划。
def pack_items(items, *, batch_size=50, max_batch_tokens=None, item_id_key=None):
    validate_foreach({'from': 'inputs.items', 'batch_size': batch_size, 'max_batch_tokens': max_batch_tokens,
                      'item_id_key': item_id_key})
    if not isinstance(items, list):
        raise ForeachInvalid('切批输入必须是列表')
    result, current, current_ids, size, start = [], [], [], 2, 0
    for idx, item in enumerate(items):
        item_id = _deterministic_item_id(item, idx, item_id_key)
        item_size = len(json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
        proposed = size + item_size + bool(current)
        if current and (len(current) == batch_size or max_batch_tokens is not None and proposed > max_batch_tokens * 4):
            result.append({'items': current, 'start': start, 'end': start + len(current), 'oversize': False,
                           'item_ids': current_ids})
            start += len(current)
            current, current_ids, size = [], [], 2
        current.append(item)
        current_ids.append(item_id)
        size += item_size + (len(current) > 1)
        if max_batch_tokens is not None and size > max_batch_tokens * 4:
            result.append({'items': current, 'start': start, 'end': start + 1, 'oversize': True,
                           'item_ids': current_ids})
            start += 1
            current, current_ids, size = [], [], 2
    if current:
        result.append({'items': current, 'start': start, 'end': start + len(current), 'oversize': False,
                       'item_ids': current_ids})
    return result


# LLM: 公有形式foreach计划预览，纯函数无副作用。
# 函数用途: 计算条目总数、批数、每批条目身份与并发/错误策略。
def preview_foreach_plan(foreach_spec, inputs, outputs=None):
    if outputs is None:
        outputs = {}
    config = validate_foreach(foreach_spec)
    items = resolve_items(config['from'], inputs, outputs)
    batches = pack_items(
        items,
        batch_size=config['batch_size'],
        max_batch_tokens=config['max_batch_tokens'],
        item_id_key=config.get('item_id_key'),
    )
    return {
        'item_count': len(items),
        'batch_count': len(batches),
        'batches': [
            {
                'index': i,
                'item_count': len(b['items']),
                'start': b['start'],
                'end': b['end'],
                'oversize': b['oversize'],
                'item_ids': list(b.get('item_ids', ())),
            }
            for i, b in enumerate(batches)
        ],
        'max_parallel': config['max_parallel'],
        'concurrency': config['max_parallel'],
        'on_batch_error': config['on_batch_error'],
        'on_item_error': config['on_item_error'],
        'failure_policy': config['on_batch_error'],
        'mode': config['mode'],
        'as': config['as'],
    }


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
