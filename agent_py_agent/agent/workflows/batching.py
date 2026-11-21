"""确定性切批、有界调度及聚合；账本写入只由协调线程调用。"""

import json
import keyword
import re
from collections import OrderedDict
from concurrent.futures import Future, wait
from contextvars import copy_context
from functools import partial
from threading import Lock

from ..concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
from ..concurrency.interrupt import set_interrupt
from ..concurrency.work_item_pool import TOOL_WAIT_POOL
from ..settings.config import DEFAULT_WORKFLOW_FOREACH_MAX_PARALLEL_COUNT, AgentConfig


class ForeachInvalid(ValueError):
    """声明或运行时输入违反foreach合同。"""

    error_code = 'WORKFLOW_FOREACH_INVALID'

    # LLM: 错误码与精确路径一起保留，不将非法输入当作空数据。
    # 函数用途: 提供统一的配置及输入错误。
    def __init__(self, message):
        super().__init__(f'{self.error_code}: {message}')


_configured_max_parallel = DEFAULT_WORKFLOW_FOREACH_MAX_PARALLEL_COUNT
_configured_host_parallel = AgentConfig.gateway_http_max_request_workers


def configure_from_config(config: object) -> None:
    global _configured_max_parallel, _configured_host_parallel
    limit = getattr(config, 'workflow_foreach_max_parallel', DEFAULT_WORKFLOW_FOREACH_MAX_PARALLEL_COUNT)
    if type(limit) is not int or limit < 1:
        raise ValueError('workflow_foreach_max_parallel 必须为正整数')
    host_limit = getattr(config, 'gateway_http_max_request_workers', AgentConfig.gateway_http_max_request_workers)
    if type(host_limit) is not int or host_limit < 1:
        raise ValueError('gateway_http_max_request_workers must be a positive integer')
    _configured_max_parallel, _configured_host_parallel = limit, host_limit


def resolved_max_parallel() -> int:
    return _configured_max_parallel


_FOREACH_ALLOWED_KEYS = frozenset({
    'from', 'batch_size', 'max_parallel', 'concurrency', 'as',
    'max_batch_tokens', 'on_batch_error', 'on_item_error', 'mode', 'item_id_key',
})


# LLM: 配置校验不执行步骤、不访问磁盘；支持WF-07公有形式foreach声明。
# 函数用途: 校验foreach声明。
def validate_foreach(value, *, max_parallel_limit: int | None = None):
    limit = resolved_max_parallel() if max_parallel_limit is None else max_parallel_limit
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
        if type(concurrency) is not int or not 1 <= concurrency <= limit:
            raise ForeachInvalid('concurrency超出范围')
        if 'max_parallel' in value:
            max_parallel = value['max_parallel']
            if type(max_parallel) is not int or not 1 <= max_parallel <= limit:
                raise ForeachInvalid('max_parallel超出范围')
            if max_parallel != concurrency:
                raise ForeachInvalid('max_parallel与concurrency冲突')
        else:
            max_parallel = concurrency
    else:
        max_parallel = value.get('max_parallel', 1)
        if type(max_parallel) is not int or not 1 <= max_parallel <= limit:
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


class _WorkflowDispatchPool:
    """Round-robin workflow grants. The shared work-item pool retains its own admission rules."""

    def __init__(self):
        self._lock = Lock()
        self._ready = OrderedDict()
        self._last_owner = None
        self._last_runs = {}
        self._active = 0
        self._executor = None
        self._capacity = 0

    def submit(self, key, fn, *args):
        owner, run_id = key
        future = Future()
        item = copy_context(), partial(fn, *args)
        with self._lock:
            runs = self._ready.setdefault(owner, OrderedDict())
            runs.setdefault(run_id, OrderedDict())[future] = item
        future.add_done_callback(partial(self._discard_cancelled, key))
        self._dispatch()
        return future

    def _discard_cancelled(self, key, future):
        if not future.cancelled():
            return
        owner, run_id = key
        with self._lock:
            runs = self._ready.get(owner)
            queue = runs.get(run_id) if runs is not None else None
            if queue is not None:
                queue.pop(future, None)
                if not queue:
                    del runs[run_id]
                if not runs:
                    del self._ready[owner]
                    self._last_runs.pop(owner, None)

    def _next(self):
        with self._lock:
            if self._ready and not self._active and self._capacity != _configured_host_parallel:
                if self._executor is not None:
                    self._executor.shutdown(wait=False)
                self._capacity = _configured_host_parallel
                self._executor = DurableDaemonThreadPoolExecutor(
                    max_workers=self._capacity, thread_name_prefix='workflow-execution',
                )
            while self._ready and self._active < min(self._capacity, _configured_host_parallel):
                if len(self._ready) > 1 and self._last_owner in self._ready:
                    self._ready.move_to_end(self._last_owner)
                owner, runs = self._ready.popitem(last=False)
                last_run = self._last_runs.get(owner)
                if len(runs) > 1 and last_run in runs:
                    runs.move_to_end(last_run)
                run_id, pending = runs.popitem(last=False)
                future, (context, call) = pending.popitem(last=False)
                if pending:
                    runs[run_id] = pending
                if runs:
                    self._ready[owner] = runs
                else:
                    self._last_runs.pop(owner, None)
                if not future.set_running_or_notify_cancel():
                    continue
                self._last_owner = owner
                if runs:
                    self._last_runs[owner] = run_id
                self._active += 1
                return self._executor, future, context, call
        return None

    def _dispatch(self):
        while (item := self._next()) is not None:
            executor, future, context, call = item
            try:
                executor.submit(context.run, self._execute, future, call)
            except Exception as error:
                self._release()
                future.set_exception(error)

    def _release(self):
        with self._lock:
            self._active -= 1

    def _execute(self, future, call):
        set_interrupt(False)
        try:
            result = call()
        except BaseException as error:
            self._release()
            future.set_exception(error)
        else:
            self._release()
            future.set_result(result)
        finally:
            set_interrupt(False)
            self._dispatch()


_WORKFLOW_DISPATCH = _WorkflowDispatchPool()


def submit_workflow_step(engine, run, fn, *args, coordinator=False):
    if not coordinator:
        return _WORKFLOW_DISPATCH.submit((str(engine.owner_home), run.workflow_run_id), fn, *args)
    future = Future()

    def coordinate():
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as error:
                future.set_exception(error)

    TOOL_WAIT_POOL.submit(coordinate, context=copy_context())
    return future


class BatchDispatch:
    """Advance a foreach fan-in on the run coordinator as batch futures settle."""

    def __init__(self, indices, execute, publish, *, submit, prepare, max_parallel, fail_fast, release_unstarted=None):
        self._remaining = iter(indices)
        self._execute, self._publish = execute, publish
        self._submit, self._prepare = submit, prepare
        self._max_parallel, self._fail_fast = max_parallel, fail_fast
        self._release_unstarted = release_unstarted
        self.active = {}
        self._stopped = False

    def advance(self):
        completed = sorted((future for future in self.active if future.done()), key=self.active.__getitem__)
        for future in completed:
            index = self.active.pop(future)
            if self._publish(index, future.result()) and self._fail_fast:
                self._stopped = True
        while not self._stopped and len(self.active) < self._max_parallel:
            index = next(self._remaining, None)
            if index is None:
                self._stopped = True
                break
            if self._prepare is not None:
                self._prepare(index)
            self.active[self._submit(self._execute, index)] = index
        return self._stopped and not self.active

    def drain(self, *, cancel_queued=False):
        self._stopped = True
        if cancel_queued:
            for future in self.active:
                future.cancel()
        wait(future for future in self.active if not future.cancelled())
        for future in sorted(self.active, key=self.active.__getitem__):
            index = self.active.pop(future)
            if future.cancelled():
                if self._release_unstarted is not None:
                    self._release_unstarted(index)
            elif future.exception() is None:
                self._publish(index, future.result())


class BatchContinuation(Exception):
    """Return a prepared foreach to its existing run coordinator."""

    def __init__(self, dispatch, finish, bind_persistence):
        super().__init__()
        self.dispatch, self.finish = dispatch, finish
        self.bind_persistence = bind_persistence


def dispatch_batches(dispatch):
    from .step_retry import wait_for_workflow_completion

    try:
        while not dispatch.advance():
            wait_for_workflow_completion(dispatch.active)
    finally:
        if dispatch.active:
            dispatch.drain(cancel_queued=True)
