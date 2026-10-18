"""真实C写段只尝试原JSON锁；受控普通线程竞争不阻塞关闭，不触发W或模型。"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io as common_json
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import io
from agent_py_agent.tests.test_gateway_c_queued_input_reconcile import module, scan
from agent_py_agent.tests.test_gateway_cold_request_pump import pump, queue_request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 只持测试自己的真实文件锁；watchdog只解除本测试等待以保原红，不伪造锁结果，finally总释放并确认原线程退出。
# 类用途: 用普通Python线程制造可观察的同文件锁竞争，并防原产品阻塞导致测试悬挂。
class HeldResource:
    # LLM: 所有事件/句柄都只属于本测试，不进入产品registry或执行身份。
    # 函数用途: 准备持锁者、原操作结束事件与有限观察窗。
    def __init__(self):
        self.acquired = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.deadline_released = threading.Event()
        self.holder_done = threading.Event()
        self.errors = []
        self.threads = []

    # LLM: 使用原gateway或common锁，真实OS锁及各自线程锁均不mock；只在明确调用位置开始竞争。
    # 函数用途: 从另一个线程持有指定原JSON资源，原操作长等时由观察线程解除本次测试。
    def hold(self, path, family):
        assert not self.threads
        lock = io.locked_file_transition if family == 'gateway' else common_json.locked_json_path

        def holder():
            try:
                with lock(path):
                    self.acquired.set()
                    assert self.release.wait(5), 'test lock release was not requested'
            except BaseException as error:
                self.errors.append(error)
                self.acquired.set()
            finally:
                self.holder_done.set()

        def watchdog():
            if not self.finished.wait(0.3):
                self.deadline_released.set()
                self.release.set()

        self.threads = [threading.Thread(target=holder), threading.Thread(target=watchdog)]
        self.threads[0].start()
        assert self.acquired.wait(2) and not self.errors
        self.threads[1].start()

    # LLM: 原操作返回后只归还本测试资源，不删除目录或清理他人线程；保留已实际发生等待的事实。
    # 函数用途: 确保红绿两种产品版本运行后都无本测试线程残留。
    def close(self):
        self.finished.set()
        self.release.set()
        for thread in self.threads:
            thread.join(3)
            assert not thread.is_alive()
        assert not self.errors


# LLM: 只写显式仓外证据目标；日志不含输入正文、令牌或真实owner，路径均为本次临时夹具。
# 函数用途: 保留原产品实际等待与修后立即busy的标量对照。
def record_fact(fact):
    target = os.environ.get('F3_TRYLOCK_FACTS')
    if target:
        with Path(target).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(fact, sort_keys=True) + '\n')


@pytest.mark.parametrize('family', ['gateway', 'common'])
@pytest.mark.parametrize('where', ['receipt_inner', 'receipt_final', 'candidate', 'queue_write', 'cursor_write'])
def test_c_actual_json_contention_is_busy_and_does_not_hold_root(context, monkeypatch, family, where):
    receipt = queue_request(context, 'b-contended')
    target = context.paths.inbox / (receipt.request_id + '.json')
    if where != 'candidate':
        target.unlink()
    receipt_path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
    cursor = context.paths.root / 'input_receipts/state/reconcile_cursor.json'
    io.write_json_file_atomic(cursor, {'schema_version': 'gateway_input_reconcile_cursor.v1', 'last_name': 'a-prior.json'})
    current = pump(context)
    before_receipt, before_cursor = receipt_path.read_bytes(), cursor.read_bytes()
    held = HeldResource()
    original_reader, original_commit = inputs.read_gateway_input_receipt, inputs._commit_reconcile_cursor
    reads = 0

    def read_original(*args, **kwargs):
        nonlocal reads
        reads += 1
        if where == 'receipt_inner' and reads == 2 or where == 'receipt_final' and reads == 3:
            held.hold(receipt_path, family)
        return original_reader(*args, **kwargs)

    def commit_original(*args, **kwargs):
        if where == 'cursor_write':
            held.hold(cursor, family)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(inputs, 'read_gateway_input_receipt', read_original)
    monkeypatch.setattr(inputs, '_commit_reconcile_cursor', commit_original)
    if where in {'candidate', 'queue_write'}:
        held.hold(target, family)
    error = result = None
    start = time.monotonic()
    try:
        try:
            result = scan(current)
        except Exception as caught:
            error = caught
        held.finished.set()
        elapsed = time.monotonic() - start
        still_held = not held.holder_done.is_set()
        current.runtime.stop_producers()
        closed = current.runtime._closing
        record_fact({'where': where, 'family': family, 'watchdog_released_wait': held.deadline_released.is_set(),
                     'lock_still_held_after_scan': still_held, 'root_closed': closed,
                     'elapsed_seconds': elapsed, 'error_type': type(error).__name__ if error else None,
                     'summary': result, 'cursor_unchanged': cursor.read_bytes() == before_cursor,
                     'receipt_unchanged': receipt_path.read_bytes() == before_receipt})
        assert not held.deadline_released.is_set(), 'C waited for ordinary JSON lock inside original C gates'
        assert still_held and closed, 'C must return and close while the competing resource remains held'
        assert type(error) is BlockingIOError and result is None
        assert cursor.read_bytes() == before_cursor
        assert receipt_path.read_bytes() == before_receipt
        if where in {'receipt_inner', 'receipt_final', 'queue_write'}:
            assert not target.exists()
        else:
            assert json.loads(target.read_bytes()) == receipt.prepared_request
    finally:
        held.close()


@pytest.mark.parametrize('operation', ['read', 'write'])
def test_count0_default_json_helpers_still_wait_for_original_resource(tmp_path, operation):
    path = tmp_path / 'default.json'
    io.write_json_file_atomic(path, {'value': 1})
    held = HeldResource()
    held.hold(path, 'gateway' if operation == 'read' else 'common')
    try:
        if operation == 'read':
            report = io.read_json_file_report(path)
            assert report.payload == {'value': 1} and not report.lock_busy
        else:
            io.write_json_file_atomic(path, {'value': 2})
        assert held.deadline_released.is_set()
    finally:
        held.close()


def test_nonblocking_reader_retains_original_busy_and_corruption_classification(tmp_path):
    path = tmp_path / 'classification.json'
    io.write_json_file_atomic(path, {'value': 1})
    held = HeldResource()
    held.hold(path, 'gateway')
    try:
        report = io.read_json_file_report(path, blocking=False)
        held.finished.set()
        assert report.lock_busy and report.load_error is not None and report.payload == {}
        assert not held.deadline_released.is_set()
    finally:
        held.close()
    path.write_text('broken JSON')
    corrupt = io.read_json_file_report(path, blocking=False)
    assert corrupt.load_error is not None and not corrupt.lock_busy


def test_nonblocking_write_uses_original_bytes_and_private_permissions(tmp_path):
    first, second = tmp_path / 'first.json', tmp_path / 'second.json'
    payload = {'正文': [True, 1, 1.0, -0.0]}
    io.write_json_file_atomic(first, payload)
    io.write_json_file_atomic(second, payload, blocking=False)
    assert first.read_bytes() == second.read_bytes() and not second.read_bytes().endswith(b'\n')
    assert second.stat().st_mode & 0o777 == 0o600


def test_same_code_ordinary_error_does_not_become_resource_busy(context, monkeypatch):
    queue_request(context)
    current = pump(context)

    def ordinary(*_args, **_kwargs):
        raise RuntimeError('gateway input lock is busy')

    monkeypatch.setattr(module()._QueuedInputSource, 'repair_queued', ordinary)
    result = scan(current)
    assert result['errors'] == 1 and result['queued'] == 0
    assert io.read_json_file(context.paths.root / 'input_receipts/state/reconcile_cursor.json')['last_name']


@pytest.mark.parametrize('where', ['queue', 'cursor'])
def test_resource_busy_same_object_stops_middle_row_without_skipping(context, monkeypatch, where):
    for identifier in ('a-first', 'b-busy', 'c-later'):
        receipt = queue_request(context, identifier)
        (context.paths.inbox / (receipt.request_id + '.json')).unlink()
    current = pump(context)
    error = BlockingIOError('ordinary resource is busy')
    source_type = module()._QueuedInputSource
    method = 'repair_queued' if where == 'queue' else 'commit_cursor'
    original = getattr(source_type, method)

    def busy(source, value):
        identifier = value.request_id if where == 'queue' else value.stem
        if identifier == 'b-busy':
            raise error
        return original(source, value)

    monkeypatch.setattr(source_type, method, busy)
    with pytest.raises(BlockingIOError) as caught:
        scan(current)
    assert caught.value is error
    cursor = io.read_json_file(context.paths.root / 'input_receipts/state/reconcile_cursor.json')
    assert cursor['last_name'] == 'a-first.json'
    assert not (context.paths.inbox / 'c-later.json').exists()


def test_real_resource_busy_uses_original_segment_backoff(context):
    from agent_py_agent.cli.gateway_loops import _RECONCILE_SITE

    receipt = queue_request(context, 'backoff-busy')
    target = context.paths.inbox / (receipt.request_id + '.json')
    target.unlink()
    current = pump(context)
    held = HeldResource()
    held.hold(target, 'common')
    try:
        delay = current._run_repair_segment(
            _RECONCILE_SITE, current._reconcile_backoff, current._reconcile_inputs_once)
        held.finished.set()
        assert delay > 0 and current._reconcile_backoff.consecutive_failures == 1
        assert current._last_input_report is None
        assert not held.deadline_released.is_set() and not target.exists()
        assert not (context.paths.root / 'input_receipts/state/reconcile_cursor.json').exists()
    finally:
        held.close()
