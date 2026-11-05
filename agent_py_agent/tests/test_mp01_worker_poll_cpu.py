# LLM: 原C producer/codec/原子写构造临时回执；只计读取与本地poll成本，不以缓存授信或把离线benchmark当线上CPU证明。
# 模块用途: 覆盖控制目录门、五元组判定缓存和外分区pending省读。
import ast
import os
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as controls
from agent_py_agent.agent.gateway_parts import multiprocess_worker_dispatch as dispatch
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case
from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import (  # noqa: F401
    enqueue,
    setup,
)


# LLM: 全部回执由原C producer生成唯一消息身份；完成态保原executor，数量只属测试输入，不修改业务来源。
# 函数用途: 准备已完成且无需对账的真实可解码回执和一个原控制泵。
def completed_case(tmp_path, monkeypatch, count):
    paths, first, scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    identity = controls.control_worker_authority(frozen, claiming=True)
    receipts = []
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, frozen.launch.partition.c_run_id)
        for index in range(count):
            current_scope = replace(scope, metadata={**scope.metadata, 'message_id': f'poll-receipt-{index}'})
            row = first if index == 0 else ops.execute_gateway_control_operation(
                SimpleNamespace(), paths, parse_conversation_control(first.command_text),
                current_scope, command_text=first.command_text)
            row = replace(row, state='completed',
                          result=ConversationControlResult(row.command_kind, True, 'fixture-completed').to_dict(),
                          dispatch={**row.dispatch, 'executor': identity, 'instance_id': frozen.launch.instance_id})
            write_json_file_atomic(ops.gateway_control_operation_path(paths, row.operation_id), row.to_dict())
            receipts.append(row)
    directory = paths.root / 'control_operations'
    old = time.time() - 5
    os.utime(directory, (old, old))
    return controls.WorkerControlPump(frozen, pool), receipts


# LLM: 包装原codec只计数不替换其结果；诊断、严格校验和锁内重读均照常发生。
# 函数用途: 记录每次真实控制回执读取。
def count_reads(monkeypatch):
    original, calls = ops.read_gateway_control_operation, []

    # LLM: 透传原位置及关键字参数，不跳过codec或修改回执。
    # 函数用途: 在原读取之前记录操作身份。
    def read(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(ops, 'read_gateway_control_operation', read)
    return calls


# LLM: 300条真实inert回执；目录mtime越过粗粒度保护后，稳态不再读取正文。
# 函数用途: 防止每轮重新解码历史回执的CPU回归。
def test_idle_poll_reads_no_receipts_after_first_scan(tmp_path, monkeypatch):
    pump, receipts = completed_case(tmp_path, monkeypatch, 300)
    calls = count_reads(monkeypatch)
    try:
        assert pump.poll() == 0 and len(calls) == 300
        calls.clear()
        for _ in range(10):
            assert pump.poll() == 0
        assert calls == []
        assert len(pump._verdicts) == len(receipts)
        assert {row[2] for row in pump._verdicts.values()} == {'inert'}
    finally:
        pump.close()


# LLM: 用原子替换改变已缓存回执；真实认领和执行仍经过原短锁与authority，而不是缓存直接提交。
# 函数用途: 验证原件变化后只执行一次健康控制。
def test_changed_receipt_is_reread_and_claimed(tmp_path, monkeypatch):
    pump, receipts = completed_case(tmp_path, monkeypatch, 1)
    row, effects = receipts[0], []

    # LLM: 仅替换领域执行边界，不绕过控制认领、登记或回执终态路径。
    # 函数用途: 记录本次原控制实际执行次数。
    def execute(_agent, _paths, command, _scope):
        effects.append(command.kind)
        return ConversationControlResult(command.kind, True, 'fixture-completed')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', execute)
    calls = count_reads(monkeypatch)
    try:
        assert pump.poll() == 0
        prepared = replace(row, state='prepared', result={},
                           dispatch={k: v for k, v in row.dispatch.items() if k not in {'executor', 'instance_id'}})
        write_json_file_atomic(ops.gateway_control_operation_path(pump.paths, row.operation_id), prepared.to_dict())
        calls.clear()
        assert pump.poll() == 1
        assert calls.count(row.operation_id) >= 2
        pump._executor.shutdown(wait=True)
        assert pump.poll() == 0
        assert effects == [row.command_kind]
        assert ops.read_gateway_control_operation(pump.paths, row.operation_id).state == 'completed'
    finally:
        pump.close()


# LLM: 原unknown steer内容保持active，不由目录门压掉逐轮事实对账；替身仅观察offer，不执行对账副作用。
# 函数用途: 验证活跃回执即使原件没改也继续被观察。
def test_reconcilable_completed_receipt_still_offered_each_poll(tmp_path, monkeypatch):
    pump, receipts = completed_case(tmp_path, monkeypatch, 1)
    row = replace(receipts[0], result={**receipts[0].result, 'delivery_status': 'unknown',
                                      'guidance_dedupe_key': 'fixture-guidance'})
    write_json_file_atomic(ops.gateway_control_operation_path(pump.paths, row.operation_id), row.to_dict())
    offers = []
    monkeypatch.setattr(pump, '_offer_reconciliation', lambda value: offers.append(value.operation_id))
    try:
        for _ in range(10):
            assert pump.poll() == 0
        assert offers == [row.operation_id] * 10
        assert pump._verdicts[f'{row.operation_id}.json'][2] == 'active'
        monkeypatch.setattr(pump._scan_gate, 'should_scan', lambda _: False)
        pump.poll()
        assert offers == [row.operation_id] * 11
    finally:
        pump.close()


# LLM: 强制原稳定性判据拒绝命中，同时令目录扫描发生；不得仅以mtime/长度相同跳过racy原件。
# 函数用途: 验证ctime量化危险窗口必须重新读取。
def test_racy_ctime_window_forces_reread(tmp_path, monkeypatch):
    pump, _receipts = completed_case(tmp_path, monkeypatch, 1)
    calls = count_reads(monkeypatch)
    monkeypatch.setattr(controls, 'released_cache_is_stable', lambda *_: False)
    monkeypatch.setattr(pump._scan_gate, 'should_scan', lambda _: True)
    try:
        for _ in range(3):
            assert pump.poll() == 0
        assert len(calls) == 3
    finally:
        pump.close()


# LLM: 使用原pending producer夹具，保原优先级读取；仅外分区严格锁读缓存，未盖章、本分区不缓存。
# 函数用途: 证明多轮pending扫描只对外分区文件严格读取一次。
def test_foreign_pending_requests_not_reread(setup, monkeypatch):  # noqa: F811
    paths = [enqueue(setup, dict(setup.request, id=f'foreign-{index}',
                               route={**setup.request['route'], 'worker': 1 - setup.frozen.launch.partition.index}))
             for index in range(4)]
    pump = dispatch.WorkerRequestPump(setup.frozen, setup.pool, 1)
    original, calls = dispatch._read_locked_request, []

    # LLM: 原锁读照常验证，仅记录调用次数。
    # 函数用途: 计数实际严格pending读取。
    def read(path):
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(dispatch, '_read_locked_request', read)
    try:
        for _ in range(10):
            assert pump.poll() == 0
        assert sorted(calls) == sorted(path.name for path in paths)
        assert setup.owners == []
        paths[0].unlink()
        assert pump.poll() == 0
        assert paths[0].name not in pump._foreign
        write_json_file_atomic(paths[1], dict(setup.request, id=paths[1].stem,
                                             route={**setup.request['route'], 'worker': 1 - setup.frozen.launch.partition.index}))
        assert pump.poll() == 0
        assert calls.count(paths[1].name) == 2
    finally:
        pump.close()


# LLM: 保留一个本分区延迟请求迫使每轮扫描；外分区必须在优先级读取之前过滤，原件变化后缓存失效，不能靠关目录门蒙混通过。
# 函数用途: 覆盖F2排序阶段绕过外分区缓存的正文重复读取。
def test_stable_foreign_pending_skips_priority_reads(setup, monkeypatch):  # noqa: F811
    foreign = enqueue(setup, dict(setup.request, id='sort-foreign',
                                 route={**setup.request['route'], 'worker': 1 - setup.frozen.launch.partition.index}))
    deferred = enqueue(setup, dict(setup.request, id='sort-deferred', not_before_at=time.time() + 3600))
    original, calls = request_worker._pending_request_sort_key, []

    # LLM: 原优先级函数照常读真文件；只记录路径，不替换排序结果或正文codec。
    # 函数用途: 统计实际到达排序读取入口的原件。
    def sort_key(path):
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(request_worker, '_pending_request_sort_key', sort_key)
    pump = dispatch.WorkerRequestPump(setup.frozen, setup.pool, 1)
    try:
        for _ in range(10):
            assert pump.poll() == 0
        assert calls.count(foreign.name) == 1
        assert calls.count(deferred.name) == 10
        write_json_file_atomic(foreign, dict(setup.request, id=foreign.stem,
                                            route={**setup.request['route'], 'worker': 1 - setup.frozen.launch.partition.index}))
        assert pump.poll() == 0
        assert calls.count(foreign.name) == 2
        deferred.unlink()
        old = time.time() - 5
        os.utime(setup.paths.inbox, (old, old))
        for _ in range(3):
            assert pump.poll() == 0
        assert calls.count(foreign.name) == 2
        assert not pump._scan_gate.should_scan(setup.paths.inbox)
        assert setup.owners == []
    finally:
        pump.close()


# LLM: 临时读取失败不改变原件；即便五元组稳定，也必须重开目录扫描并重读fault，成功后仍可正常缓存inert。
# 函数用途: 覆盖F3瞬态IO故障被永久缓存而失去重试的缺陷。
def test_transient_control_read_fault_retried_without_file_change(tmp_path, monkeypatch):
    pump, receipts = completed_case(tmp_path, monkeypatch, 1)
    path = ops.gateway_control_operation_path(pump.paths, receipts[0].operation_id)
    before = path.read_bytes()
    original, calls = ops.read_gateway_control_operation, []

    # LLM: 仅首读抛瞬态OSError，随后委托原codec；原件、mtime和真实控制状态均不改。
    # 函数用途: 注入一次可恢复的读取故障。
    def read(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            raise OSError('fixture-transient-read')
        return original(*args, **kwargs)

    monkeypatch.setattr(ops, 'read_gateway_control_operation', read)
    monkeypatch.setattr(controls, 'released_cache_is_stable', lambda *_: True)
    try:
        assert pump.poll() == 0
        assert len(calls) == 1 and pump._verdicts[path.name][2] == 'fault'
        assert pump.poll() == 0
        assert len(calls) == 2 and pump._verdicts[path.name][2] == 'inert'
        assert pump._read_faults == {}
        assert pump.poll() == 0 and len(calls) == 2
        assert path.read_bytes() == before
    finally:
        pump.close()


# LLM: 直接从任务锚点提取原poll函数；只在临时inert输入上做同进程对照，不改生产源码或构造其它worker。
# 函数用途: 恢复本片修改前的准确扫描入口供本地微基准比较。
def baseline_poll(pump):
    source = subprocess.check_output(['git', 'show',
        '11d2d961a:agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py'], text=True)
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == 'WorkerControlPump')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'poll')
    namespace = dict(vars(controls))
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<anchor-worker-poll>', 'exec'), namespace)
    return namespace['poll'].__get__(pump)


# LLM: 800条真实完成回执、同一输入各20次poll；先暖缓存再计优化后，输出实测秒数/倍率且要求至少20倍，不删断言。
# 函数用途: 量化稳态控制扫描成本的实际降幅。
def test_poll_benchmark_800_completed_receipts(tmp_path, monkeypatch):
    pump, _receipts = completed_case(tmp_path, monkeypatch, 800)
    before_poll = baseline_poll(pump)
    try:
        start = time.perf_counter()
        for _ in range(20):
            assert before_poll() == 0
        before = time.perf_counter() - start
        assert pump.poll() == 0
        start = time.perf_counter()
        for _ in range(20):
            assert pump.poll() == 0
        after = time.perf_counter() - start
        print(f'worker_poll_benchmark receipts=800 polls=20 before_seconds={before:.6f} '
              f'after_seconds={after:.6f} speedup={before / after:.2f}x')
        assert before >= 20 * after
    finally:
        pump.close()
