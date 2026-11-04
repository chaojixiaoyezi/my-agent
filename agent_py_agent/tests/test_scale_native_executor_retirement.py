"""责任索引：热路径静态守门、真实C来源与精确unknown拒绝。"""
import ast
import importlib
import inspect
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as runtime
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 仅解析本模块函数调用图，不执行被测源码，也不把跨模块未知调用解释成已覆盖。
# 函数用途: 找出指定入口可达的静态调用名，守住热路径禁止全扫的约束。
def reachable(source, start):
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen, pending = set(), [start]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in functions:
            for call in ast.walk(functions[name]):
                if isinstance(call, ast.Call):
                    target = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, 'attr', '')
                    pending.append(target)
    return seen


# LLM: 导入当前源码中的真实责任索引，不提供替代实现或伪造权限。
# 函数用途: 延迟取得原索引模块供各回归独立运行。
def index_module():
    return importlib.import_module('agent_py_agent.agent.gateway_parts.multiprocess_executor_active_index')


# LLM: 正例和明确无效fixture共用同一守门算法；签名检查不代替行为回归。
# 函数用途: 禁止热启动路径重扫历史并保留冻结接口。
def test_owner_hot_path_cannot_reach_full_discovery_and_guard_rejects_fixture():
    source = Path(runtime.__file__).read_text()
    assert 'discover_takeover_targets' not in reachable(source, 'launch_owner_writer')
    invalid = 'def launch_owner_writer():\n    helper()\ndef helper():\n    discover_takeover_targets()\n'
    assert 'discover_takeover_targets' in reachable(invalid, 'launch_owner_writer')
    assert str(inspect.signature(runtime.CoordinatorTakeover.launch_owner_writer)) == '(self, owner_sha, launch)'


# LLM: 原租约和原C均由产品构造，关闭标志只制造撤权反例，不签业务授权。
# 函数用途: 索引只能属于同一原C活期，撤权后不能被旧引用延寿。
def test_index_is_original_c_instance_and_copied_hint_cannot_authorize(context):
    module = index_module()
    controller = context.controller()
    index = module.index_for(controller)
    assert index is module.index_for(controller) is module.current_index(context.paths)
    assert index.active_p3() == ()
    controller._lease._closing = True
    try:
        assert module.current_index(context.paths) is None
        with pytest.raises(ValueError):
            index.active_p3()
    finally:
        controller._lease._closing = False


# LLM: 陌生命名空间作为未知输入保留；禁止发现器只是失败探针，不生成可接受来源。
# 函数用途: 热门遇未知变更须阻塞，不能在锁内回退全扫。
def test_unknown_namespace_change_blocks_without_discovery(context, monkeypatch):
    module = index_module()
    controller = context.controller()
    module.index_for(controller)
    directory = context.paths.root / 'workers' / 'executors'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'unknown').mkdir()
    monkeypatch.setattr(runtime, 'discover_takeover_targets', lambda *_a, **_k: pytest.fail('hot discovery'))
    called = []
    decision = controller.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), lambda: called.append(True))
    assert decision['status'] == 'blocked' and called == []


# LLM: hint仅为非权威历史输入，原controller和实际nofollow读取保持不变。
# 函数用途: 增长无权hint历史不应增加一次真实owner写门的文件预算。
@pytest.mark.parametrize('history', [10, 100, 1000])
def test_history_hints_never_enter_hot_file_budget(context, monkeypatch, history):
    module = index_module()
    controller = context.controller()
    directory = context.paths.root / 'responsibility_index' / 'settlements'
    directory.mkdir(parents=True)
    for n in range(history):
        (directory / f'{n}.json').write_text('{"hint":true}')
    reads = []
    from agent_py_agent.agent.common import nofollow_fs
    original = nofollow_fs.read_bytes_beneath
    # LLM: 观察器总是委托原reader，计数不能改变被测读取结果或权限。
    # 函数用途: 旁观本次热门实际读取的路径与字节。
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append((args[1], len(value or b'')))
        return value
    monkeypatch.setattr(nofollow_fs, 'read_bytes_beneath', read)
    monkeypatch.setattr(runtime, 'discover_takeover_targets', lambda *_a, **_k: pytest.fail('hot discovery'))
    result = controller.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), lambda: None)
    assert result['status'] == 'started'
    assert len(reads) <= 8 and sum(size for _, size in reads) <= 131072
    assert all('settlements' not in parts for parts, _ in reads)
    assert module.index_for(controller).active_p3() == ()


# LLM: 原claim/P3/执行scope由产品产生，不从JSON恢复C来源或构造替身授权。
# 函数用途: 保留真实C执行来源的词法生命周期。
@contextmanager
def original_c_source(c_actual):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
        current_coordinator_execution,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_executor_source import (
        CoordinatorExecutorSource,
    )
    gate, claim, agent = c_actual
    with coordinator_execution_scope(gate, claim, agent):
        source = CoordinatorExecutorSource(current_coordinator_execution())
        yield source, claim


# LLM: 仅预留原域对象和原pin，未启动Linux namespace；物理退出由显式边界fixture另行提供。
# 函数用途: 建立与原C请求准确绑定的canonical P1责任。
def reserved_domain(source, claim, identifier):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
    from agent_py_agent.agent.gateway_parts.multiprocess_native_linux_domain import (
        NativePidNamespaceDomain,
    )
    binding = {'owner_sha': owner_digest(source.owner), 'request_id': claim.request_id,
               'execution_attempt_id': claim.execution_attempt_id, 'lease_epoch': claim.lease_epoch,
               'tool_call_id': 'retirement-call'}
    row = {**_reservation(source, binding, 'shell', identifier),
           'schema_version': 'multiprocess_executor_reservation.v2', 'execution_domain': None}
    domain = NativePidNamespaceDomain(source, row)
    source._domains.append(domain)
    domain.retain_lifetime()
    source.reserve(row)
    return row, domain


# LLM: 此边界明确受控，不能据此声称真实Linux namespace或整树退出。
# 函数用途: 在产品结清流程外层提供可控的同一域退出观察。
def controlled_exit_boundary(module, domain, monkeypatch):
    """只替换内核观察边界；原C、T、P3、pin、P1 CAS和hint写入均走产品。

    不代表真实native整树退出；宿主native回归另验。
    """
    reference = SimpleNamespace()
    receipt = (reference, None, 12345, 'controlled-birth', (('device', 1), ('inode', 2)), 0)
    reference._receipt_origin = receipt
    domain._reference_origin, domain._release_receipt = reference, receipt
    monkeypatch.setattr(module, '_observe_domain', lambda observed: receipt if observed['domain'] is domain else None)
    return receipt


# LLM: 仅内核观察边界受控；原pin/域/P3及索引不得被另一个责任替换。
# 函数用途: 验证错pin不能结清原执行责任。
def test_original_pin_replacement_cannot_retire_another_responsibility(c_actual, monkeypatch):
    module = index_module()
    with original_c_source(c_actual) as (source, claim):
        row, domain = reserved_domain(source, claim, 'pin-original')
        index = module.index_for(source._sources[4])
        key = module._executor_key(row)
        entry = index.executors[key]
        saved = domain.pin
        other = source.begin_execution_domain()
        controlled_exit_boundary(module, domain, monkeypatch)
        domain.pin = other
        assert module._retire_candidate(index, key, entry) is False
        assert saved in source._pins and other in source._pins
        domain.pin = saved
        domain.retire_lifetime()
        source.finish_execution_domain(other)


# LLM: Event固定结清交错，退出观察仍是受控输入，不外推实际Linux内核。
# 函数用途: 延迟原finish及重复CAS不能早清责任或重复退役。
def test_kernel_observation_then_late_finish_and_cas_replay(c_actual, monkeypatch):
    module = index_module()
    with original_c_source(c_actual) as (source, claim):
        row, domain = reserved_domain(source, claim, 'finish-interleaving')
        index = module.index_for(source._sources[4])
        key, pin = module._executor_key(row), domain.pin
        entry = index.executors[key]
        controlled_exit_boundary(module, domain, monkeypatch)
        entered, release = threading.Event(), threading.Event()
        original = domain.retire_lifetime
        # LLM: 仅延迟同一原finish调用，不替换其结果或直接改pin集合。
        # 函数用途: 在精确Event窗口观察未结责任仍然保留。
        def delayed():
            entered.set()
            assert release.wait(5)
            return original()
        monkeypatch.setattr(domain, 'retire_lifetime', delayed)
        result = []
        worker = threading.Thread(target=lambda: result.append(module._retire_candidate(index, key, entry)))
        worker.start()
        assert entered.wait(5)
        assert key in index.executors and pin not in source._finished_pins
        release.set()
        worker.join(5)
        assert not worker.is_alive() and result == [True]
        assert pin in source._finished_pins and key not in index.executors
        assert source.read(row['reservation_id']) == row
        assert index._p3(row['request_id'])['state'] == 'occupied'
        assert module._retire_candidate(index, key, entry) is False


# LLM: 原位修改合法JSON只作为反例，原目录revision不变也不能授信。
# 函数用途: active点读必须识别规范原件内容漂移。
def test_canonical_active_content_change_is_not_hidden_by_revision_cache(c_actual):
    module = index_module()
    with original_c_source(c_actual) as (source, claim):
        row, domain = reserved_domain(source, claim, 'canonical-change')
        index = module.index_for(source._sources[4])
        entry = index.executors[module._executor_key(row)]
        filename = source.root.joinpath(*entry['ledger']._parts(row['reservation_id']))
        # 原目录mtime不变的原位合法JSON修改，同样必须被完整active点读拒绝。
        changed = {**row, 'tool_call_id': 'different-original-call'}
        filename.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match='active_executor_canonical_changed'):
            index.check_sources()
        filename.write_text(json.dumps(row))
        domain.retire_lifetime()


# LLM: 原C/P1历史和active均真实；仅物理退出边界受控，不代表真实namespace结清。
# 函数用途: 历史增长时热门文件/字节只随活跃责任变化，且不泄露FD。
@pytest.mark.parametrize('active', [0, 1, 8])
@pytest.mark.parametrize('history', [10, 100, 1000])
def test_real_canonical_settled_history_leaves_hot_active_budget_flat(c_actual, monkeypatch, history, active):
    module = index_module()
    with original_c_source(c_actual) as (source, claim):
        index = module.index_for(source._sources[4])
        for n in range(history):
            row, domain = reserved_domain(source, claim, f'settled-{n}')
            controlled_exit_boundary(module, domain, monkeypatch)
            key = module._executor_key(row)
            assert module._retire_candidate(index, key, index.executors[key])
        assert not index.executors
        pending = [reserved_domain(source, claim, f'active-{n}') for n in range(active)]
        reads = []
        byte_reads = []
        from agent_py_agent.agent.gateway_parts import multiprocess_executor_ledger as ledger_module
        from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
        original = ExecutorLedger.read
        original_bytes = ledger_module.read_bytes_beneath
        # LLM: 委托同一个原ledger点读，不改变canonical数据或状态。
        # 函数用途: 记录本次热门实际访问的P1标识。
        def read(ledger, identifier):
            reads.append(identifier)
            return original(ledger, identifier)
        # LLM: 原reader结果完整透传，锁观察只计数，不参与被测决策。
        # 函数用途: 记录热门P1读取字节及controller持锁状态。
        def read_bytes(*args, **kwargs):
            content = original_bytes(*args, **kwargs)
            byte_reads.append((len(content or b''), index.controller._mutex._is_owned()))
            return content
        monkeypatch.setattr(ExecutorLedger, 'read', read)
        monkeypatch.setattr(ledger_module, 'read_bytes_beneath', read_bytes)
        fd_root = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
        before_fd = len(os.listdir(fd_root))
        decision = index.controller.launch_owner_writer(row['owner_sha'], lambda: None)
        assert decision['status'] == 'started' and len(reads) == active
        assert len(byte_reads) == active and all(held for _, held in byte_reads)
        assert sum(size for size, _ in byte_reads) <= active * 16384
        assert len(os.listdir(fd_root)) == before_fd
        print({'history': history, 'active': active, 'p1_files': len(reads),
               'p1_bytes': sum(size for size, _ in byte_reads), 'fd_delta': 0})
        assert source.read(row['reservation_id']) == row
        for _, domain in pending:
            domain.retire_lifetime()


# LLM: 冷历史仅提供canonical责任，不补造原对象或退出证明；每拍仍走原游标。
# 函数用途: 验证冷回填固定批、未完阻塞及持久hint不授结清权。
def test_cold_cursor_is_bounded_unknown_and_persistent_hints_cannot_settle(tmp_path):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    module = index_module()
    paths = gateway_paths_from_root(tmp_path / 'cold-gateway')
    owner = owner_digest(OwnerIdentity.local_main())
    ledger = ExecutorLedger(paths.root, WorkerPartition(int(owner, 16) % 2, 2, 'cold-c'))
    binding = {'owner_sha': owner, 'request_id': 'cold-request', 'execution_attempt_id': 'cold-attempt',
               'lease_epoch': 1, 'tool_call_id': 'cold-call'}
    with coordinator_guard(paths.root, 2):
        for n in range(132):
            ledger.reserve(_reservation(ledger, binding, 'shell', f'cold-{n}'))
        controller = runtime.prepare_coordinator_takeover(paths, 2, 'cold-c')
        index = module.index_for(controller)
        assert not index.ready and index._cold_visited == 64
        with pytest.raises(ValueError, match='unknown'):
            index.active_p3()
        assert controller.launch_owner_writer(owner, lambda: pytest.fail('cold authorizes'))['status'] == 'blocked'
        assert controller._all_quarantined is False
        tick = module.maintain_controller_index(controller)
        assert tick['visited'] == 64 and tick['ready'] is False
        tick = module.maintain_controller_index(controller)
        assert tick['visited'] <= 64 and tick['ready'] is True
        assert len(index.executors) == 132
        assert all(entry['descriptor'] is None for entry in index.executors.values())
        cursor = json.loads((paths.root / 'responsibility_index' / 'cold_cursor.json').read_bytes())
        assert cursor['ready'] is True and cursor['generation'] == 'cold-c'
        controller.close()


# LLM: 浅拷贝不是原C责任来源，不能借原lease获得原对象身份。
# 函数用途: 拒绝复制索引作为活跃责任权威。
def test_copied_index_is_not_an_original_responsibility_source(context):
    import copy
    original = index_module().index_for(context.controller())
    with pytest.raises(ValueError, match='original'):
        copy.copy(original).active_p3()


# LLM: 只注入一次hint I/O失败，原不可变写者及原C权限检查保持。
# 函数用途: 验证同hint可重放、不同内容冲突且hint永不授执行权。
def test_immutable_hint_io_replay_is_idempotent_but_never_authorizes(context, monkeypatch):
    module = index_module()
    index = module.index_for(context.controller())
    original = module.write_bytes_atomic_beneath
    calls = []
    # LLM: 首次依赖失败不制造持久成功，其余调用完整交原写者处理。
    # 函数用途: 精确触发hint持久化失败后的重放窗口。
    def fail_once(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            raise OSError('simulated_io_error')
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', fail_once)
    payload = {'record': {'only': 'hint-not-authority'}}
    with pytest.raises(OSError):
        index._persist('executor', 'replay-hint', payload)
    index._persist('executor', 'replay-hint', payload)
    index._persist('executor', 'replay-hint', payload)
    assert len(calls) == 2
    with pytest.raises(ValueError, match='immutable'):
        index._persist('executor', 'replay-hint', {'record': {'only': 'changed'}})
    assert index.active_p3() == () and not index.executors and index.controller._all_quarantined is False


# LLM: 规范终态是明确标注的运输夹具；原claim/P3/lease仍由产品创建，不声称真实模型或native完成。
# 函数用途: 为索引释放重放提供与原请求准确绑定的cancelled终态。
def _cancelled_terminal(c_actual):
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic

    gate, claim, _agent = c_actual
    payload = read_json_file(claim.path)
    terminal = {**payload, 'schema_version': 'gateway_terminal_request.v1',
                'turn_phase': 'closed', 'status': 'cancelled',
                'terminal_response': {'id': claim.request_id, 'ok': False, 'status': 'cancelled'}}
    write_json_file_atomic(gate.paths.terminal / claim.path.name, terminal)
    return terminal


# LLM: 只观察原release；active不是第二P3账，canonical released后必须经原索引结清才移出。
# 函数用途: 规范释放不会留下永久失效的责任索引。
def test_canonical_release_settles_original_index(c_actual):
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission

    gate, claim, _agent = c_actual
    _cancelled_terminal(c_actual)
    index = index_module().index_for(gate._runtime_takeover)
    assert len(index.active_p3()) == 1
    assert admission.release_reservation_if_terminal(gate, claim.request_id)['released'] is True
    assert index.active_p3() == ()
    assert claim.request_id not in index.reservations
    assert gate._runtime_takeover.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), lambda: None)['status'] == 'started'


# LLM: 在原持久写完成但调用者收到EIO的窗口失败；不替换P3内容、claim或原release_admission验证。
# 函数用途: released已写但未确认的重放不能隔离C或永久保留旧索引原像。
def test_released_write_uncertainty_replays_same_original_index(c_actual, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission

    gate, claim, _agent = c_actual
    _cancelled_terminal(c_actual)
    original = admission._write_gate_record_durable

    # LLM: 保留同一次原写者副作用，仅模拟调用返回前的依赖失败；只在这条released写入触发。
    # 函数用途: 产生已落规范released但外层未确认的真实重放窗口。
    def interrupted(gate, path, record):
        original(gate, path, record)
        if record['state'] == 'released':
            raise OSError('simulated I/O error after original release')

    with monkeypatch.context() as fault:
        fault.setattr(admission, '_write_gate_record_durable', interrupted)
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is False
    index = index_module().index_for(gate._runtime_takeover)
    index.check_sources()
    assert not gate._runtime_takeover._all_quarantined
    assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
    assert claim.request_id not in index.reservations and index.active_p3() == ()


# LLM: hint失败发生在原P3耐久released之后；没有hint不能清索引，但同身份重放应补齐且不重做业务。
# 函数用途: 验证结清hint写失败后的原canonical重放仍能收口。
def test_p3_settlement_hint_failure_keeps_dirty_index_replayable(c_actual, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission

    gate, claim, _agent = c_actual
    _cancelled_terminal(c_actual)
    index = index_module().index_for(gate._runtime_takeover)
    original = index._persist

    # LLM: 仅拒当前P3的hint持久化；不改原quota、P3或终态来源。
    # 函数用途: 确认失败时留下可核原像而不是宣布索引已退役。
    def failed(kind, identifier, value):
        if kind == 'p3':
            raise OSError('simulated I/O error in settlement hint')
        return original(kind, identifier, value)

    with monkeypatch.context() as fault:
        fault.setattr(index, '_persist', failed)
        assert admission.release_reservation_if_terminal(gate, claim.request_id)['released'] is False
        assert claim.request_id in index.reservations
        index.check_sources()
    assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
    assert claim.request_id not in index.reservations and index.active_p3() == ()


# LLM: bound原C的snapshot和quota共用同一个责任索引；只拒这次P3历史枚举，不修改业务输入或投影。
# 函数用途: 保证两个真实消费者不再glob已结历史。
def test_admission_and_quota_consume_same_active_index_without_history_glob(c_actual, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as accounting

    gate, _claim, _agent = c_actual
    directory = admission.reservations_dir(gate.paths)
    original = Path.glob

    # LLM: 其它路径保持原glob，仅把禁止的历史枚举变成确定失败观察点。
    # 函数用途: 验证实际bound分支而非AST或未调用helper。
    def glob(path, pattern, *args, **kwargs):
        assert path != directory, 'P3 historical enumeration entered the active consumer'
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, 'glob', glob)
    assert gate.occupied_snapshot()['total'] == 1
    assert accounting.unsettled_curator_requests(gate.paths) == 0


# LLM: 复用原Count0停止夹具和原C准备者；不修改原来源分类器来容忍私有hint。
# 函数用途: 新索引游标不能污染首次迁移仍要读取的规范workers命名空间。
def test_private_cold_hint_keeps_standalone_source_contract(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.standalone_sources import _source_snapshot
    from agent_py_agent.tests.test_gateway_standalone_migration import stopped_home

    case = stopped_home(tmp_path, monkeypatch)
    with coordinator_guard(case.paths.root, 2):
        controller = runtime.prepare_coordinator_takeover(case.paths, 2, 'new-generation',
            config=case.config, config_path=case.args.config)
        assert not controller._all_quarantined
        _source_snapshot(case.paths.root)


# LLM: 原泵、原gate和规范cancelled终态夹具不变；真实释放后仍须给同一owner后续请求准入。
# 函数用途: 捕获settlement后索引revision漂移导致的错误全局占位。
@pytest.mark.parametrize('prefix', ['', 'index-'])
def test_original_pump_releases_then_indexes_next_same_owner(context, prefix):
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.tests.test_gateway_cold_request_pump import pump, queue_request

    context.config.gateway_user_inflight_limit = 1
    context.config.gateway_global_inflight_limit = 2
    first = queue_request(context, prefix + 'first', 'alice')
    second = queue_request(context, prefix + 'second', 'alice')
    queue_request(context, prefix + 'other', 'bob')
    current = pump(context)
    current.tick()
    gate = current._admission_gate
    assert gate.occupied_snapshot()['total'] == 2
    assert reservation_file(context.paths, first.request_id).exists()
    assert not reservation_file(context.paths, second.request_id).exists()
    payload = read_json_file(context.paths.inbox / (first.request_id + '.json'))
    terminal = {**payload, 'schema_version': 'gateway_terminal_request.v1', 'turn_phase': 'closed',
                'status': 'cancelled', 'terminal_response': {'id': first.request_id, 'ok': False, 'status': 'cancelled'}}
    write_json_file_atomic(context.paths.terminal / (first.request_id + '.json'), terminal)
    current.tick()
    assert read_json_file(reservation_file(context.paths, first.request_id))['state'] == 'released'
    assert reservation_file(context.paths, second.request_id).exists()
    index = index_module().index_for(context.controller())
    result = gate.occupied_snapshot()
    source_error = None
    try:
        index.check_sources()
    except ValueError as error:
        source_error = str(error)
    assert result['total'] == 2, (result, source_error, index.ready, index.failed,
        context.controller()._all_quarantined, tuple(index.reservations),
        read_json_file(context.paths.inbox / (second.request_id + '.json')))


# LLM: 仅原内核退出观察边界受控，其余C/P1/P3/pin及后台实例均真实；不把周期调度器mock成已执行。
# 函数用途: 即便来源发现被节流，同拍仍在T/C门外推进原执行者结清。
def test_original_background_poll_maintains_settlement_even_when_sources_throttled(c_actual, monkeypatch):
    import time

    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )

    module = index_module()
    gate, _claim, _agent = c_actual
    producer = gate._ordinary_admission_origin[0]
    background = CoordinatorBackgroundRequests(producer.control, drain_runtime=producer)
    background._next_poll_at = time.monotonic() + 30
    with original_c_source(c_actual) as (source, claim):
        row, domain = reserved_domain(source, claim, 'background-retirement')
        index = module.index_for(gate._runtime_takeover)
        key = module._executor_key(row)
        receipt = controlled_exit_boundary(module, domain, monkeypatch)
        assert background.poll() == 0
        assert key not in index.executors
        assert source.read(row['reservation_id']) == row and domain._release_receipt is receipt


# LLM: 只等待真实文件/内核观察，deadline不是成功凭据；超时保留最后一份原观察供诊断。
# 函数用途: 给真实C/W跨进程入口提供有界等待，绝不靠固定sleep判就绪或结清。
def _wire_until(observe, label, *, seconds=12):
    import time

    deadline, last = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        last = observe()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f'{label}: last_observation={last!r}')


# LLM: profile只旁观原函数返回，不改I/O、锁、业务回调或结果；逐次短门独立计费，线程不混账。
# 函数用途: 记录真正native消费者持controller锁的文件/字节，以及真实P3创建入口。
@contextmanager
def _wire_meter(controller):
    import sys

    from agent_py_agent.agent.common.nofollow_fs import read_bytes_beneath
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover_sources as sources

    launches, created, pending = [], [], {}
    launch_code = runtime.CoordinatorTakeover.launch_owner_writer.__wrapped__.__code__
    work_modules = {runtime.__name__, sources.__name__, index_module().__name__}
    old, old_threads = sys.getprofile(), threading.getprofile()

    # LLM: 原frame仅提供当次观察点，不调用产品写者、不合成授权或改返回值。
    # 函数用途: 逐线程统计原短门内实际读取，并证明curator经过_commit_created_record。
    def observe(frame, event, result):
        identity = threading.get_ident()
        if frame.f_code is launch_code:
            if event == 'call':
                pending[identity] = {'files': 0, 'bytes': 0, 'role_files': 0, 'work': 0, 'reads': []}
            elif event == 'return':
                measured = pending.pop(identity)
                launches.append({**measured, 'decision': result})
        elif (event == 'return' and frame.f_code is read_bytes_beneath.__code__
              and identity in pending and controller._mutex._is_owned()):
            pending[identity]['files'] += 1
            pending[identity]['bytes'] += len(result or b'')
            caller, stack, role = frame.f_back, [], False
            while caller is not None and caller.f_code is not launch_code:
                if len(stack) < 8:
                    stack.append(caller.f_code.co_name)
                role = role or caller.f_code is sources.indexed_role_coverage.__code__
                caller = caller.f_back
            pending[identity]['role_files'] += int(role)
            pending[identity]['reads'].append({'parts': frame.f_locals['relative_parts'],
                'bytes': len(result or b''), 'role': role, 'stack': stack})
        elif (event == 'call' and identity in pending and controller._mutex._is_owned()
              and frame.f_globals.get('__name__') in work_modules):
            pending[identity]['work'] += 1
        elif event == 'return' and frame.f_code is admission._commit_created_record.__code__:
            request = frame.f_locals['prepared'][0]
            created.append({'kind': request['kind'], 'id': request['id'], 'decision': result,
                            'source_kind': request.get('metadata', {}).get('background_source', {}).get('kind')})

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        yield launches, created
    finally:
        sys.setprofile(old)
        threading.setprofile(old_threads)


# LLM: 预算来自独立fixture的C+W角色集合，保留全部读取/字节；不把P4设施开销藏出计量或按失败值反推阈值。
# 函数用途: 同时约束原角色三读解析上界、全部非角色八读和总预算。
def _wire_assert_budget(launches, worker_count=2):
    role_budget = 3 * (worker_count + 1) + 2
    role_parts = {('gateway_state.json',), ('workers', 'registry.json'),
                  *(('workers', f'w{n}.json') for n in range(worker_count))}
    assert launches and all(row['decision']['status'] == 'started' for row in launches), launches
    for row in launches:
        assert row['files'] == len(row['reads'])
        assert row['bytes'] == sum(read['bytes'] for read in row['reads'])
        assert row['role_files'] == sum(read['role'] for read in row['reads'])
        assert all(tuple(read['parts']) in role_parts for read in row['reads'] if read['role']), row
        assert row['role_files'] <= role_budget, row
        assert row['files'] - row['role_files'] <= 8, row
        assert row['files'] <= role_budget + 8 and row['bytes'] <= 131072, row


# LLM: 仅在隔离home备料；原模型管理入口显式新增/共享离线Echo，不以部署配置或内存注入绕过正式选择。
# 函数用途: 准备正式worker可消费的离线模型档案、规范runlog和真实策展源。
def _wire_prepare(base):
    from dataclasses import replace

    import yaml

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as accounting
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import freeze_source
    from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    from agent_py_agent.tests.test_scale_curator_quota_owner_budget import _append_run

    home, workspace = base / 'home', base / 'workspace'
    options = {'my_agent_home': str(home), 'model_backend': 'echo', 'prompt_files': [],
               'gateway_worker_process_count': 2, 'max_tokens': 65536,
               'memory_curator_turn_threshold': 1}
    config = AgentConfig(**options)
    bootstrap = SimpleAgent(replace(config, gateway_worker_process_count=0), workspace)
    profile_id = '00000000-0000-4000-8000-000000000009'
    execute_model_profile_operation(bootstrap, 'add', {'profile_id': profile_id, 'profile': {
        'transport': 'offline', 'model_backend': 'echo', 'model_name': 'wire-offline-echo',
        'model_context_window_tokens': 262144}})
    execute_model_profile_operation(bootstrap, 'set_default', {'profile_id': profile_id})
    execute_model_profile_operation(bootstrap, 'set_initial', {'profile_id': profile_id})
    owner = OwnerIdentity.provider_user('feishu', 'wire-curator')
    resolved = ensure_owner_home(home, owner)
    store = ConversationStore(resolved.home_dir / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': owner.owner_id,
        'channel': 'internal', 'channel_conversation_id': 'wire-curator'})
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '隔离策展输入'})
    state = MemoryCuratorStateStore(resolved.home_dir / 'memory' / 'curator' / 'state.json')
    # 此规范完成记录是明确的历史输入，不是本轮真实模型执行结果；重启必须保留它且不重计。
    _append_run(home, owner, 'memory-curator-run-wire-history',
                finished=accounting.quota_date() + 'T00:00:01+00:00')
    (base / 'config.yaml').write_text(yaml.safe_dump(options), encoding='utf-8')
    (base / 'curator-source.json').write_text(json.dumps(freeze_source(home, owner, state.path, 'curator')))


# LLM: 原prepared入口冻结真实owner/指纹，再由同一原gate准入；不直接写route/P3/claim或terminal。
# 函数用途: 给指定分区排队一个无媒体的离线Echo请求。
def _wire_queue(control, gate, identifier, worker):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_worker
    from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits

    owner = next(candidate for number in range(100)
                 if owner_worker(candidate := OwnerIdentity.provider_user('feishu', f'wire-{number}'), 2) == worker)
    payload = {'id': identifier, 'kind': 'ask', 'goal': '只回复本地测试文本，不调用工具',
               'user_id': owner.owner_id, 'metadata': {'channel': 'feishu', 'user_id': owner.owner_id,
               'channel_message_id': identifier, 'client_input_digest': identifier}}
    with delivery.gateway_input_transition(control.paths, identifier):
        receipt, _ = delivery.load_or_prepare_gateway_input_locked(control.paths, request_id=identifier,
            client_input_digest=identifier, client_message_id=identifier, guidance_dedupe_key='',
            prepared_request=payload)
        delivery.queue_gateway_input_locked(control.paths, receipt)
    assert frozen_request_owner(receipt.prepared_request) == owner
    decision = gate.admit(control.paths.inbox / (identifier + '.json'),
        (owner, admission.session_scope_for_request(receipt.prepared_request)), AdmissionLimits(8, 8))
    assert decision['status'] == 'admitted', decision
    return identifier


# LLM: 当前C/W均为正式实现且不打patch；只从规范terminal与原session/pin读取已完成事实，不用状态标签替代kernel wire。
# 函数用途: 消费一条实际worker请求并让同一原gate释放已完成P3。
def _wire_finish(control, gate, identifier, *, ask=False):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file

    terminal = _wire_until(lambda: read_json_file(control.paths.terminal / (identifier + '.json')),
                           'actual worker terminal ' + identifier)
    assert terminal['turn_phase'] == 'closed', terminal
    if ask:
        assert terminal['status'] == 'done', terminal
        assert terminal['terminal_response']['ok'] is True, terminal
    gate.release_settled()
    assert read_json_file(reservation_file(control.paths, identifier))['state'] == 'released'
    return terminal


# LLM: 独立真实C沿正式目录初始化后运行原P0/P4、Mach或SCM、worker CLI/P3/P6；不替换消费链或物理证明。
# 函数用途: 完成真实headless启动和一代结清，保留阶段证据让下一真实进程接班。
def _wire_generation(base, generation, *, initialize=False, stop_one=False, history=0):
    import time

    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as accounting
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
        enqueue_source,
        source_owner,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.gateway_worker_runtime import CoordinatorWorkers
    from agent_py_agent.tests.test_scale_curator_quota_owner_budget import _warm_quota

    base = Path(base).resolve()
    os.environ['MY_AGENT_HOME'] = str(base / 'home')
    if initialize:
        _wire_prepare(base)
    config = load_config(base / 'config.yaml')
    assert config.gateway_worker_process_count == 2
    paths = gateway_paths_from_root(base / 'gateway')
    # 正式setup_control_run同样先准备队列父目录；原claim的rename不会代建processing。
    ensure_gateway_folders(paths)
    quota_path = base / 'home' / 'global_index' / 'curator_daily_quota.json'
    report = {'generation': generation, 'coordinator_pid': os.getpid(), 'stage': 'before_coordinator',
              'quota_entry': accounting.read_quota_count(quota_path)}
    report_path = base / (generation + '.json')

    # LLM: 证据只是旁观投影，不给下代授信；原lease、P3及closure仍由产品独立验证。
    # 函数用途: 在失败前保留最后已到达观察点，避免只留下父pytest超时。
    def save(stage):
        report['stage'] = stage
        report_path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2))

    save('before_coordinator')
    with coordinator_guard(paths.root, 2):
        controller = runtime.prepare_coordinator_takeover(paths, 2, generation)
        index = index_module().index_for(controller)
        report['cold'] = {'ready': index.ready, 'failed': index.failed,
                          'visited': index._cold_visited, 'quarantined': controller._all_quarantined,
                          'p3_names': sorted(p.name for p in admission.reservations_dir(paths).iterdir())}
        save('cold_index')
        # 保留真实.json.lock伴随文件；错误不得靠删除锁或手动ready绕过。
        control = GatewayControlPlane(config, base / 'home', paths, 2, generation,
            agent_root=base / 'workspace', workspace_roots=(base / 'workspace',))
        drain = GatewayDrainRuntime(control)
        gate = admission.coordinator_admission_gate(control, paths, SimpleNamespace(coordinator_worker_count=2))
        drain.retain_admission(gate)
        workers = CoordinatorWorkers(control, base / 'config.yaml', 43123)
        drain.retain_workers(workers)
        from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity

        write_json_file_atomic(paths.root / 'gateway_state.json',
            {'status': 'running', 'pid': os.getpid(), 'c_run_id': generation,
             'process_identity': build_process_identity(os.getpid()), 'http_port': 43123})
        with _wire_meter(controller) as (launches, created):
            try:
                workers.start()
                # 不在测试里补第二批冷维护：正式start必须自行等待已核全量，不能跳过unknown。
                assert index.ready and not index.failed and not controller._all_quarantined, report['cold']
                report['cold_completed'] = {'ready': index.ready, 'failed': index.failed,
                                            'visited': index._cold_visited}
                _wire_until(lambda: all(read_json_file(paths.root / 'workers' / f'w{n}.json').get('ready')
                                        for n in range(2)), 'two real workers ready')
                report['worker_pids'] = [process.pid for process in workers.processes]
                assert len(set(report['worker_pids'])) == 2
                save('workers_ready')
                # 重启入口先核既有额度；在任何新请求之前观察，不能拿后续重算冒充前后相同。
                if not initialize:
                    assert report['quota_entry'] > 0
                for worker in range(2):
                    identifier = _wire_queue(control, gate, f'{generation}-ask-{worker}', worker)
                    _wire_finish(control, gate, identifier, ask=True)
                save('wire_asks_finished')
                if history:
                    _wire_cancel_history(control, gate, generation, history)
                    report['history'] = history
                    save('p3_history_committed')
                    boundary = len(launches)
                    identifier = _wire_queue(control, gate, f'{generation}-new-0', 0)
                    _wire_finish(control, gate, identifier, ask=True)
                    report['history_probe'] = {'baseline': launches[:2], 'after': launches[boundary:]}
                    _wire_assert_history_flat(report['history_probe'], identifier)
                    save('history_new_wire_finished')
                if initialize:
                    ref = json.loads((base / 'curator-source.json').read_text())
                    path, _ = enqueue_source(paths, control.home_root, ref)
                    payload = read_json_file(path)
                    _warm_quota(gate, payload)
                    decision = gate.admit(path, (source_owner(ref['owner']), admission.session_scope_for_request(payload)),
                                          AdmissionLimits(8, 8), snapshot=payload)
                    assert decision['status'] == 'admitted', decision
                    report['curator_terminal'] = _wire_finish(control, gate, path.stem)['status']
                    assert any(row['kind'] == 'background' and row['source_kind'] == 'curator'
                               for row in created), created
                _wire_until(lambda: all(not lane.session._handles and not lane.issuer._pins
                                        for lane in workers.native_lanes), 'actual native finishes')
                report['native'] = [{'last_seq': lane.session._last_seq,
                                     'finished_pins': len(lane.issuer._finished_pins),
                                     'error': repr(lane.last_error)} for lane in workers.native_lanes]
                assert all(row['last_seq'] > 0 and row['finished_pins'] > 0 for row in report['native'])
                assert all(lane.last_error is None for lane in workers.native_lanes), report['native']
                report['launches'], report['created'] = launches, created
                save('native_finished')
                _wire_assert_budget(launches)
                if stop_one:
                    workers.processes[0].terminate()
                    observer = workers._created_origins[0][2]
                    assert observer.wait_until(time.monotonic() + 8) and observer.confirmed
                    assert workers.processes[1].poll() is None
                    report['stopped_one'] = workers.processes[0].pid
                    save('one_worker_stopped')
                report['quota_exit'] = accounting.read_quota_count(quota_path)
                assert report['quota_exit'] > 0
                workers.close(defer_native=True)
                assert all(observer.confirmed for _, _, observer in workers._created_origins)
                drain.stop_producers()
                assessment = drain.assess()
                report['assessments'] = [str(item) for item in assessment]
                save('drain_assessed')
                assert all(item.drained for item in assessment), assessment
                write_json_file_atomic(paths.root / 'gateway_state.json',
                                       {'status': 'stopped', 'pid': os.getpid(), 'c_run_id': generation})
                drain.seal_generation()
                save('sealed')
            finally:
                workers.close()


# LLM: 正式C在独立进程中运行且原父子关系不替换；HOME/PYTHONPATH只隔离数据及锁定当前源码，输出文件仅作证据。
# 函数用途: 运行一代真实组合根，并保留stdout/stderr和原阶段结果供精确复核。
def _wire_child(base, generation, *, initialize=False, stop_one=False, history=0):
    import subprocess
    import sys

    base.mkdir(parents=True, exist_ok=True)
    home = base / 'process-home'
    home.mkdir(exist_ok=True)
    source = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(base / 'home'),
           'PYTHONPATH': os.pathsep.join((str(source), str(source / 'agent_py_agent'))),
           'PYTHONDONTWRITEBYTECODE': '1'}
    module = 'agent_py_agent.tests.test_scale_native_executor_retirement'
    program = (f'from {module} import _wire_generation; '
               f'_wire_generation({str(base)!r}, {generation!r}, initialize={initialize!r}, '
               f'stop_one={stop_one!r}, history={history!r})')
    with (base / (generation + '.stdout')).open('w') as out, (base / (generation + '.stderr')).open('w') as err:
        result = subprocess.run([sys.executable, '-c', program], cwd=source, env=env,
                                stdout=out, stderr=err, timeout=50, check=False)
    evidence = (base / (generation + '.json')).read_text() if (base / (generation + '.json')).exists() else 'no stage'
    assert result.returncode == 0, (result.returncode, evidence, (base / (generation + '.stderr')).read_text())
    return json.loads(evidence)


# LLM: 不使用physical/channel受控夹具，两个worker为正式CLI/Popen且消息由真实内核认证；Echo不证明真实provider或Linux整树退出。
# 函数用途: 覆盖W9第6节4的真实C/W短提交及实际curator P3准入入口。
def test_real_two_worker_native_wire_commits_and_curator_admission(tmp_path):
    report = _wire_child(tmp_path / 'wire', 'wire-c', initialize=True)
    assert report['stage'] == 'sealed'
    assert len(report['native']) == 2
    assert any(row['kind'] == 'background' and row['source_kind'] == 'curator' for row in report['created'])


# LLM: 比较同C同W0相同角色/活跃责任的原短门；完整读取仍记录，时间戳文本只给固定64字节编码差额。
# 函数用途: 实际历史增长后次数和锁内工作不增长，非角色字节不能跟历史增长。
def _wire_assert_history_flat(probe, identifier):
    before, after = probe['baseline'], probe['after']
    assert len(before) == len(after) == 2, probe
    _wire_assert_budget(before)
    _wire_assert_budget(after)
    for first, last in zip(before, after):
        assert (last['files'], last['role_files'], last['work']) == (
            first['files'], first['role_files'], first['work']), probe
        old_bytes = sum(read['bytes'] for read in first['reads'] if not read['role'])
        new_bytes = sum(read['bytes'] for read in last['reads'] if not read['role'])
        assert new_bytes <= old_bytes + 64, probe
        for read in last['reads']:
            parts = tuple(read['parts'])
            if parts[:2] == ('admission', 'reservations'):
                assert parts == ('admission', 'reservations', identifier + '.json'), read


# LLM: 真实C+2W与原_commit_created_record均不mock；历史cancelled输入明确是夹具，仍保完整P6收尾。
# 函数用途: 在同一个真实wire入口核对10/100/1000历史后的新原短提交，而不是只测空目录或存在。
@pytest.mark.parametrize('history', [10, 100, 1000])
def test_real_two_worker_wire_budget_stays_flat_after_history(tmp_path, history):
    report = _wire_child(tmp_path / 'wire-history', 'scale-c', initialize=True, history=history)
    assert report['stage'] == 'sealed' and report['history'] == history
    assert report['quota_exit'] > 0 and len(report['native']) == 2
    _wire_assert_history_flat(report['history_probe'], 'scale-c-new-0')


# LLM: 两个C是先后真实退出/出生的不同进程，旧scope仅由原P6封存与后继fresh P2验证；不清锁文件或把同进程当重启。
# 函数用途: 覆盖停止一个真实W、完整停止、接班重启、新wire launch预算和非零quota重启一致性。
def test_real_headless_worker_stop_takeover_restart_preserves_quota_and_launch_budget(tmp_path):
    base = tmp_path / 'continuity'
    before = _wire_child(base, 'before-c', initialize=True, stop_one=True)
    after = _wire_child(base, 'after-c')
    assert before['stopped_one'] in before['worker_pids']
    assert before['coordinator_pid'] != after['coordinator_pid']
    assert set(before['worker_pids']).isdisjoint(after['worker_pids'])
    assert before['quota_exit'] == after['quota_entry'] == after['quota_exit']
    assert after['stage'] == 'sealed'
    _wire_assert_budget(after['launches'])


# LLM: 仅造历史循环暂挂当前profile，finally恢复后实际新wire请求仍完整计量；原P3创建/释放不替换、不称历史为W执行结果。
# 函数用途: 在真实两worker存续期间准备跨首批冷扫描的canonical P3历史及原锁伴随文件。
def _wire_cancel_history(control, gate, generation, count):
    import sys
    import time

    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    owner = OwnerIdentity.provider_user('feishu', 'wire-history')
    old_prof = sys.getprofile()
    sys.setprofile(None)
    try:
        for number in range(count):
            identifier = f'{generation}-history-{number}'
            payload = {'id': identifier, 'kind': 'ask', 'goal': '隔离已取消历史输入',
                       'not_before_at': time.time() + 3600, 'user_id': owner.owner_id,
                       'metadata': {'channel': 'feishu', 'user_id': owner.owner_id,
                                    'client_input_digest': identifier}}
            with delivery.gateway_input_transition(control.paths, identifier):
                receipt, _ = delivery.load_or_prepare_gateway_input_locked(control.paths, request_id=identifier,
                    client_input_digest=identifier, client_message_id='', guidance_dedupe_key='', prepared_request=payload)
                delivery.queue_gateway_input_locked(control.paths, receipt)
            path = control.paths.inbox / (identifier + '.json')
            decision = gate.admit(path, (owner, admission.session_scope_for_request(receipt.prepared_request)),
                                  AdmissionLimits(8, 8))
            assert decision['status'] == 'admitted', decision
            current = read_json_file(path)
            terminal = {**current, 'schema_version': 'gateway_terminal_request.v1',
                        'turn_phase': 'closed', 'status': 'cancelled',
                        'terminal_response': {'id': identifier, 'ok': False, 'status': 'cancelled'}}
            write_json_file_atomic(control.paths.terminal / path.name, terminal)
            assert admission.release_reservation_if_terminal(gate, identifier)['released'] is True
    finally:
        sys.setprofile(old_prof)
    # 不能删除/改名这些真实锁来让P6或下代冷扫描变绿。
    locks = list(admission.reservations_dir(control.paths).glob('*.json.lock'))
    assert len(locks) >= count


# LLM: 全程实际C+2W，历史cancelled输入明确为夹具；重启不由测试主动跑backfill或改ready，生产启动负责完成冷观察。
# 函数用途: 让超过首64条canonical P3的关闭/接班暴露锁分类及缺少冷维护推进，而非小空目录假连续。
def test_real_headless_restart_after_multi_batch_p3_history(tmp_path):
    base = tmp_path / 'multi-batch'
    before = _wire_child(base, 'history-before-c', initialize=True, stop_one=True, history=70)
    after = _wire_child(base, 'history-after-c')
    assert before['history'] == 70
    assert before['quota_exit'] == after['quota_entry'] == after['quota_exit']
    assert after['cold_completed']['ready'] and not after['cold_completed']['failed'], after['cold_completed']
    assert after['stage'] == 'sealed'


# LLM: 使用原C租约、真实规范设施和原spawn门；回调只是出生边界观察，不冒称启动了CLI或通过物理P2。
# 函数用途: 真实启动门必须在短门外完成后续冷批，不能只看prepare的首64项。
def test_production_spawn_waits_for_complete_cold_index(tmp_path):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_mode import WorkerLaunch
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    directory = paths.root / 'admission' / 'reservations'
    directory.mkdir(parents=True)
    for number in range(70):
        (directory / f'original-{number}.json.lock').write_bytes(b'')
    with coordinator_guard(paths.root, 2):
        controller = runtime.prepare_coordinator_takeover(paths, 2, 'cold-spawn')
        index = index_module().index_for(controller)
        assert not index.ready and index._cold_visited == 64
        launch = WorkerLaunch(WorkerPartition(0, 2, 'cold-spawn'), paths.root,
                              tmp_path / 'home', os.getpid(), '1' * 32, (tmp_path,))
        observed = []
        decision = runtime.spawn_coordinator_worker(launch, lambda: observed.append(
            (index.ready, controller._mutex._is_owned(), controller._lease._lifecycle_gate._is_owned())))
        assert decision['status'] == 'started', decision
        assert observed == [(True, True, True)]
        assert index._cold_visited == 70 and index.ready
        thread = controller._recheck_thread_origin
        assert type(thread) is threading.Thread and thread is controller._recheck_thread
        assert not controller._recheck_thread_start_unknown
        controller.close()
        assert not thread.is_alive()


# LLM: 只把真实文件形状作为冷输入反例；原nofollow分类与原C权限门不patch，成功亦保留原锁文件。
# 函数用途: 放行规范普通锁，但不能借相同后缀藏软链接、硬链接、目录或未知名字。
@pytest.mark.parametrize('shape', ['regular', 'symlink', 'hardlink', 'directory', 'unknown'])
def test_cold_admission_facility_preserves_strict_original_file_checks(tmp_path, shape):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    directory = paths.root / 'admission' / 'reservations'
    directory.mkdir(parents=True)
    leaf = directory / ('untyped.lock' if shape == 'unknown' else 'original.json.lock')
    target = tmp_path / 'outside'
    target.write_bytes(b'not-a-reservation')
    if shape == 'symlink':
        leaf.symlink_to(target)
    elif shape == 'hardlink':
        os.link(target, leaf)
    elif shape == 'directory':
        leaf.mkdir()
    else:
        leaf.write_bytes(b'')
    before = leaf.lstat()
    with coordinator_guard(paths.root, 2):
        controller = runtime.prepare_coordinator_takeover(paths, 2, 'cold-facility')
        index = index_module().index_for(controller)
        observed = []
        decision = controller.launch_owner_writer(owner_digest(OwnerIdentity.local_main()),
                                                  lambda: observed.append(True))
        if shape == 'regular':
            assert index.ready and not index.failed and decision['status'] == 'started'
            assert observed == [True]
        else:
            assert index.failed and controller._all_quarantined
            assert decision['status'] == 'blocked' and observed == []
        after = leaf.lstat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        controller.close()


# LLM: 复用原fixture生成器和清理，不覆盖现有context；物理端点受控，不能声称真实内核通过。
# 函数用途: 为旧P1冷恢复独立保留N=1/2的原producer与租约关闭句柄。
@pytest.fixture(params=[1, 2])
def closed_history_case(tmp_path, monkeypatch, request):
    from agent_py_agent.tests import test_gateway_role_lifecycle_closure as closure
    from agent_py_agent.tests import test_gateway_role_lifecycle_successor as successor

    handles = {}
    with contextmanager(closure.context.__wrapped__)(tmp_path, monkeypatch, request, handles) as control:
        with contextmanager(successor.physical.__wrapped__)(control, monkeypatch) as workers:
            yield workers, handles


# LLM: P3/终态是显式规范组件输入，不代表实际业务执行；原P1与v2闭包由原producer产生。
# 函数用途: 构造准确结清输入及缺P3、缺终态、错attempt/epoch负例。
def _closed_history_input(paths, row, count, fault):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_records import release_admission

    owner = next(OwnerIdentity.provider_user('synthetic', f'domain-{i}') for i in range(100)
                 if owner_digest(OwnerIdentity.provider_user('synthetic', f'domain-{i}')) == row['owner_sha'])
    route = {'worker': int(row['owner_sha'], 16) % count,
             'owner': [owner.provider, owner.owner_kind, owner.owner_id], 'owner_sha': row['owner_sha'],
             'c_run_id': row['c_run_id'], 'stamped_at': 1.0, 'binding': 'b' * 64}
    p3 = {'schema_version': 'multiprocess_admission_reservation.v1', 'reservation_id': 'p3-component',
          'request_id': row['request_id'], 'owner_sha': row['owner_sha'],
          'session_scope': {'channel': 'synthetic', 'thread_id': 'component'}, 'route': route,
          'state': 'occupied', 'terminal_fact': None}
    terminal = {'schema_version': 'gateway_terminal_request.v1', 'id': row['request_id'],
                'turn_phase': 'closed', 'status': 'done', 'route': route,
                'execution_attempt_id': row['execution_attempt_id'], 'lease_epoch': row['lease_epoch'],
                'terminal_response': {'id': row['request_id'], 'ok': True, 'status': 'done'}}
    if fault == 'attempt':
        terminal['execution_attempt_id'] = 'other-attempt'
    if fault == 'epoch':
        terminal['lease_epoch'] += 1
    p3 = release_admission(p3, terminal)
    if fault != 'missing_p3':
        write_json_file_atomic(paths.root / 'admission/reservations' / (row['request_id'] + '.json'), p3)
    if fault != 'missing_terminal':
        write_json_file_atomic(paths.terminal / (row['request_id'] + '.json'), terminal)


# LLM: 原producer→closure→fresh P2完整运行；仅物理枚举受控，不补造holder/descriptor/pin或信hint。
# 函数用途: 齐证旧P1离开热门，缺任一逐请求证据保持active；targets和canonical始终保留。
@pytest.mark.parametrize('fault', ['complete', 'missing_p3', 'missing_terminal', 'attempt', 'epoch'])
def test_cold_closed_p1_projection_keeps_incomplete_active(closed_history_case, monkeypatch, fault):
    from agent_py_agent.agent.gateway_parts import daemon_metadata
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.tests.test_gateway_role_lifecycle_successor import (
        attach_original_domain,
        stop_physical,
    )

    active = index_module()
    physical, handles = closed_history_case
    physical.start()
    row = attach_original_domain(physical, monkeypatch)
    paths, count = physical.control.paths, physical.control.worker_count
    _closed_history_input(paths, row, count, fault)
    drain = GatewayDrainRuntime(physical.control)
    drain.retain_workers(physical)
    stop_physical(drain, physical)
    assert all(item.drained for item in drain.assess())
    drain.seal_generation()
    handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    monkeypatch.setattr(p2, 'observe_processes', lambda: {
        'platform': 'linux', 'host_id': row['process_identity']['host_id'], 'rows': {}, 'failure': ''})
    with coordinator_guard(paths.root, count):
        controller = runtime.prepare_coordinator_takeover(paths, count, 'cold-successor')
        index = active.index_for(controller)
        while not index.ready and not index.failed:
            active.maintain_controller_index(controller)
        for target in controller.targets:
            assert controller.prepare_target(target)['error'] is None
        active.maintain_controller_index(controller)
        key = active._executor_key(row)
        if fault == 'complete':
            assert key not in index.executors, 'closed canonical old P1 returned to hot active set'
            reads = []
            original = active.ExecutorLedger.read
            monkeypatch.setattr(active.ExecutorLedger, 'read', lambda ledger, identifier: (
                reads.append(identifier), original(ledger, identifier))[1])
            index.check_sources()
            assert row['reservation_id'] not in reads
        else:
            assert key in index.executors
            assert index.executors[key]['descriptor'] is None and index.executors[key]['holder'] is None
        assert any(row['reservation_id'] in target.scope['executor_reservation_ids'] for target in controller.targets)
        target = next(t for t in controller.targets if row['reservation_id'] in t.scope['executor_reservation_ids'])
        assert json.loads(paths.root.joinpath(*target.ledger._parts(row['reservation_id'])).read_bytes()) == row
        controller.close()


# LLM: 仅规范P1组件输入，不启动物理域或制造原descriptor；后续物理枚举显式受控。
# 函数用途: 建立需要多拍冷扫描的原ledger目录。
def _cold_driver_controller(paths):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition

    owner = owner_digest(OwnerIdentity.local_main())
    ledger = ExecutorLedger(paths.root, WorkerPartition(int(owner, 16) % 2, 2, 'cold-c'))
    binding = {'owner_sha': owner, 'request_id': 'cold-request', 'execution_attempt_id': 'cold-attempt',
               'lease_epoch': 1, 'tool_call_id': 'cold-call'}
    for n in range(132):
        row = ledger.reserve(_reservation(ledger, binding, 'shell', f'cold-{n}'))
        row = ledger.advance(row, 'registered', {'process_identity': {
            'schema_version': 'multiprocess_process_identity.v1', 'host_id': 'controlled-host',
            'pid': 999900 + n, 'start_time': f'controlled:{n}'}, 'pgid': 999900 + n, 'sid': 999900 + n})
        ledger.advance(row, 'released')
    return runtime.prepare_coordinator_takeover(paths, 2, 'cold-c')


# LLM: 只替换P2物理枚举输入，首轮核实、Thread、ACK及P6生命周期槽均保留原实现。
# 函数用途: 首gate在短门外等待冷完成，失败/stop/closing只能失败关闭。
@pytest.mark.parametrize('ending', ['ready', 'failure', 'stop', 'closing'])
def test_first_gate_waits_outside_short_gate_on_original_thread(tmp_path, monkeypatch, ending):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    active = index_module()
    monkeypatch.setattr(p2, 'observe_processes', lambda: {
        'platform': 'linux', 'host_id': 'controlled-host', 'rows': {}, 'failure': ''})
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    with coordinator_guard(paths.root, 2):
        controller = _cold_driver_controller(paths)
        index = active.index_for(controller)
        assert not index.ready and index._cold_visited == 64 and controller._recheck_thread is None
        entered, release = threading.Event(), threading.Event()
        observed, result = [], []
        original = active.maintain_controller_index

        # LLM: 仅延迟原维护入口，batch/权限检查不变；错误注入明确记录为模拟I/O。
        # 函数用途: 固定冷等待、撤权和关闭窗口。
        def delayed(c, limit=64):
            assert not c._mutex._is_owned() and not c._lease._lifecycle_gate._is_owned()
            entered.set()
            assert release.wait(5)
            if ending == 'failure':
                raise OSError('simulated_io_error')
            progress = original(c, limit=limit)
            observed.append(progress)
            assert progress['visited'] <= 64
            return progress

        monkeypatch.setattr(active, 'maintain_controller_index', delayed)
        gate = SimpleNamespace()
        waiter = threading.Thread(target=lambda: result.append(runtime.bind_admission_takeover(gate, controller)))
        waiter.start()
        assert entered.wait(5)
        with controller._mutex:
            origin = controller._recheck_thread_origin
            assert origin is controller._recheck_thread and origin is not waiter
            assert not controller._recheck_thread_start_unknown and waiter.is_alive()
        assert controller._mutex.acquire(blocking=False)
        controller._mutex.release()
        assert not index.ready
        if ending == 'stop':
            controller._recheck_stop.set()
        if ending == 'closing':
            controller._lease._closing = True
        release.set()
        waiter.join(10)
        assert not waiter.is_alive() and result == [None]
        assert gate._runtime_takeover_required and gate._runtime_takeover is controller
        if ending == 'ready':
            assert index.ready and not index.failed and observed
            assert not controller._all_quarantined and not controller._quarantined
        else:
            assert not runtime._await_cold_index(controller)
        controller._lease._closing = False
        assert controller.close()
        assert controller._recheck_thread is origin and not origin.is_alive()
        assert controller._recheck_joined and index._cold_closed


# LLM: Event只安排next与close交错；实际冷锁、生成器和controller.close均不替换。
# 函数用途: 证明关闭不会与同一个冷生成器并发执行。
def test_close_serializes_with_inflight_cold_cursor(tmp_path):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    active = index_module()
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    with coordinator_guard(paths.root, 2):
        controller = _cold_driver_controller(paths)
        index = active.index_for(controller)
        original = index._cold
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        outcomes, failures = [], []

        # LLM: 只在原next前放Event，不改变原游标产出。
        # 函数用途: 保持生成器执行中以复现close竞争窗口。
        def delayed():
            entered.set()
            assert release.wait(5)
            try:
                yield from original
            finally:
                original.close()

        # LLM: 捕获实际异常，不能以线程退出冒充成功。
        # 函数用途: 推进一次原冷维护。
        def tick():
            try:
                outcomes.append(active.maintain_controller_index(controller, limit=1))
            except Exception as error:
                failures.append(error)

        # LLM: 关闭仍沿原controller，不跳过stop或冷锁。
        # 函数用途: 与在途next争同一个游标关闭权。
        def close():
            closing.set()
            try:
                outcomes.append(controller.close())
            except Exception as error:
                failures.append(error)

        index._cold = delayed()
        worker = threading.Thread(target=tick)
        worker.start()
        assert entered.wait(5)
        closer = threading.Thread(target=close)
        closer.start()
        assert closing.wait(5)
        assert not index._cold_lock.acquire(blocking=False)
        release.set()
        worker.join(5)
        closer.join(5)
        assert not worker.is_alive() and not closer.is_alive()
        assert failures == [] and len(outcomes) == 2 and True in outcomes
        assert index._cold_closed and index.failed and not index.ready


# LLM: 仅计量器的显式反例数据，不代表运行时读取或业务链路；无效分类/预算必须被原校验拒绝。
# 函数用途: 防止角色分类藏掉P1/P3读取，或丢失总次数和总字节保护。
@pytest.mark.parametrize('fault', ['role_path', 'role_count', 'non_role_count', 'bytes'])
def test_wire_meter_rejects_budget_misclassification(fault):
    parts = [('gateway_state.json',), ('workers', 'w0.json'), ('workers', 'w1.json')]
    reads = [{'parts': path, 'bytes': 15, 'role': True, 'stack': []} for path in parts for _ in range(3)]
    reads.extend({'parts': ('workers', 'registry.json'), 'bytes': 15, 'role': True, 'stack': []}
                 for _ in range(2))
    reads.append({'parts': ('admission', 'reservations', 'current.json'), 'bytes': 40, 'role': False, 'stack': []})
    if fault == 'role_path':
        reads[0]['parts'] = ('admission', 'reservations', 'history.json')
    elif fault == 'role_count':
        reads.append(dict(reads[0]))
    elif fault == 'non_role_count':
        reads.extend(dict(reads[-1]) for _ in range(8))
    else:
        reads[0]['bytes'] = 131073
    row = {'reads': reads, 'files': len(reads), 'bytes': sum(read['bytes'] for read in reads),
           'role_files': sum(read['role'] for read in reads), 'work': 10, 'decision': {'status': 'started'}}
    with pytest.raises(AssertionError):
        _wire_assert_budget([row])


# LLM: 原cold pump/gate/Root/P3，不制造W或终态权限；cancelled终态仅为显式组件输入，不能当真实业务完成。
# 函数用途: 当前occupied W责任缺原W来源时拒绝关闭，保canonical且不停止复核；原规范释放后允许再观察。
@pytest.mark.parametrize('worker', [0, 1])
def test_uncovered_current_p3_blocks_drain_until_original_terminal_release(context, worker):
    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.tests.test_gateway_admission_closing import admit
    from agent_py_agent.tests.test_gateway_cold_request_pump import pump, queue_request

    user = next(f'uncovered-{number}' for number in range(100)
                if int(owner_digest(OwnerIdentity.provider_user('feishu', f'uncovered-{number}')), 16) % 2 == worker)
    receipt, current = queue_request(context, user=user), pump(context)
    assert admit(current, receipt)['status'] == 'admitted'
    gate, controller = current._admission_gate, current._controller_origin
    path = admission.reservation_file(current.paths, receipt.request_id)
    before = path.read_bytes()
    assert read_json_file(path)['route']['worker'] == worker
    assert controller._current_worker_origins == {}
    with pytest.raises(ValueError, match='drain_original_p3_worker_source_missing'):
        runtime.original_drain_sources(controller)
    current.runtime.stop_producers()
    assert gate.release_if_terminal(receipt.request_id) == {'released': False, 'reason': 'terminal_missing'}
    assert path.read_bytes() == before and read_json_file(path)['state'] == 'occupied'
    assert not all(result.drained for result in current.runtime.assess())
    assert current.runtime._generation_observation is None and current.runtime._closed_generation_ref is None
    assert not controller._recheck_stop.is_set() and not controller._all_quarantined
    payload = read_json_file(current.paths.inbox / (receipt.request_id + '.json'))
    terminal = {**payload, 'schema_version': 'gateway_terminal_request.v1', 'turn_phase': 'closed',
                'status': 'cancelled', 'terminal_response': {'id': receipt.request_id, 'ok': False, 'status': 'cancelled'}}
    write_json_file_atomic(current.paths.terminal / (receipt.request_id + '.json'), terminal)
    assert gate.release_if_terminal(receipt.request_id)['released'] is True
    assert read_json_file(path)['state'] == 'released'
    assert runtime.original_drain_sources(controller)[0] == ()
    assert all(result.drained for result in current.runtime.assess())
