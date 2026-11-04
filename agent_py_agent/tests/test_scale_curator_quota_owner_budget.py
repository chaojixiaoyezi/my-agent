"""策展准入先行回归：真实 P3 入口的用户访问预算与全扫静态门。"""
import ast
import os
from contextvars import ContextVar
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as accounting
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    enqueue_source,
    source_owner,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.tests.test_gateway_cold_request_pump import cold_context, curator_control
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context

_cold_observation = ContextVar('quota_cold_observation', default=None)

# LLM: 只解析本地 helper 的调用关系，不执行模块，也不把跨模块未知调用认作已覆盖。
# 函数用途: 检查准入是否仍间接走全 Owner 日账扫描。
def _quota_scan_paths(source):
    functions = {node.name: node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.FunctionDef)}
    pending, seen, found = [('curator_quota_admission', ('curator_quota_admission',))], set(), []
    while pending:
        name, route = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for call in ast.walk(functions[name]):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            callee = call.func.id
            if callee == '_all_consumed_records':
                found.append((*route, callee))
            elif callee in functions and callee not in seen:
                pending.append((callee, (*route, callee)))
    return found


# LLM: 对真实准入定义的本地调用图做守门，不把静态断言当作运行成本证明。
# 函数用途: 防止准入重新间接调用全Owner日账扫描。
def test_quota_admission_does_not_read_all_registered_owners():
    source = Path(accounting.__file__).read_text(encoding='utf-8')
    assert _quota_scan_paths(source) == []


# LLM: 反例必须触发相同调用图检查，避免只检查入口名称产生假绿。
# 函数用途: 确认AST守门能识别藏在helper后的全量扫描。
def test_quota_scan_guard_rejects_indirect_invalid_fixture():
    source = '''\ndef curator_quota_admission():\n    reconcile()\ndef reconcile():\n    _all_consumed_records()\n'''
    assert _quota_scan_paths(source) == [
        ('curator_quota_admission', 'reconcile', '_all_consumed_records')]


# LLM: 用户目录在实际 C 租约建立前准备；计数器仅旁观原 reader，不替换 quota、source、P3 或 controller 的业务行为。
# 函数用途: 对真实 admit→_commit_created_record 入口统计一次准入实际访问的 Owner，注册历史不应推高预算。
@pytest.mark.parametrize('registered', [5, 100, 1000])
def test_real_p3_admission_owner_visits_are_bounded(tmp_path, monkeypatch, registered):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    for number in range(registered - 2):
        ensure_owner_home(home, OwnerIdentity.provider_user('feishu', f'inactive-{number}'))
    with curator_control(tmp_path, monkeypatch) as (control, refs):
        path, _ = enqueue_source(control.paths, control.home_root, refs[0])
        run = cold_context(control)
        gate = admission.coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        owner = source_owner(refs[0]['owner'])
        payload = accounting._occupied_request(control.paths, path.stem)
        _warm_quota(gate, payload)
        visits = []
        reader = accounting.owner_consumed_records

        # LLM: 每次调用仍执行同一个原 no-follow runlog reader，记录的是打开尝试，不将不存在的文件称为读到正文。
        # 函数用途: 旁观真实 Owner 日账访问量。
        def measured(root, current_owner, *, run_id=None):
            visits.append(owner_digest(current_owner))
            return reader(root, current_owner, run_id=run_id)

        monkeypatch.setattr(accounting, 'owner_consumed_records', measured)
        descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
        before = len(os.listdir(descriptors))
        from agent_py_agent.agent.gateway_parts.io import read_json_file
        payload = read_json_file(path)
        decision = gate.admit(path, (owner, admission.session_scope_for_request(payload)),
                              AdmissionLimits(user_inflight=8, global_inflight=8), snapshot=payload)
        after = len(os.listdir(descriptors))
        print({'registered': registered, 'owner_log_open_attempts': len(visits), 'fd_delta': after - before})
        assert after - before == 0
        assert decision['status'] == 'admitted'
        assert len(visits) <= 65, f'one P3 admission visited {len(visits)} owner logs'


# LLM: 仅通过原审计生产者生成完整final记录；没有C执行上下文时准备隔离测试输入，不补造运行许可。
# 函数用途: 为UTC及重复身份回归创建一条严格的原运行账。
def _append_run(home, owner, run_id, *, finished='2026-11-03T23:59:59+00:00', status='succeeded'):
    from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog, CuratorRunRecord
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    ensure_owner_home(home, owner)
    directory = resolve_owner_home(home, owner).home_dir / 'memory' / 'curator' / 'runs'
    return CuratorRunLog(directory).append(CuratorRunRecord(
        run_id=run_id, lease_id='original-lease', status=status, reason='admin',
        provider='fixture', model='fixture', started_at=finished, finished_at=finished))


# LLM: 预期键与原v2公开契约一致，按owner/run区分；不调用被测reader生成期望值。
# 函数用途: 独立计算测试期望的规范身份。
def _expected_identity(owner, run_id):
    import hashlib
    import json
    from dataclasses import asdict

    content = json.dumps({'owner': asdict(owner), 'run_id': run_id}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(content.encode()).hexdigest()


# LLM: 时钟只模拟调用期间UTC跨日；真正磁盘分片和严格审计reader不替换。
# 函数用途: 一次读取必须冻结同一天，不能开昨日文件却按今日过滤。
def test_original_owner_reader_freezes_day_across_midnight(tmp_path, monkeypatch):
    owner = OwnerIdentity.provider_user('feishu', 'midnight-owner')
    _append_run(tmp_path, owner, 'memory-curator-run-midnight')
    days = iter(('2026-11-03', '2026-11-04'))
    monkeypatch.setattr(accounting, 'quota_date', lambda: next(days, '2026-11-04'))
    assert accounting.owner_consumed_records(tmp_path, owner) == {
        _expected_identity(owner, 'memory-curator-run-midnight')}


# LLM: 相同run字符串跨owner不是同一计账事实；保留真实schema及UTC分片读取。
# 函数用途: 守住规范Owner参与去重身份。
def test_same_run_id_in_two_owners_is_two_facts(tmp_path, monkeypatch):
    owners = [OwnerIdentity.provider_user('feishu', name) for name in ('left', 'right')]
    for owner in owners:
        _append_run(tmp_path, owner, 'memory-curator-run-shared')
    monkeypatch.setattr(accounting, 'quota_date', lambda: '2026-11-03')
    left, right = (accounting.owner_consumed_records(tmp_path, owner) for owner in owners)
    assert left and right and left.isdisjoint(right)


# LLM: 严格记录允许两个不同final record-id，但同一run对应相冲突final必须被会计拒绝，不能悄悄去重。
# 函数用途: 损坏/歧义运行不能变成可用额度。
def test_conflicting_final_audits_do_not_silently_deduplicate(tmp_path, monkeypatch):
    owner = OwnerIdentity.local_main()
    _append_run(tmp_path, owner, 'memory-curator-run-conflict')
    _append_run(tmp_path, owner, 'memory-curator-run-conflict', status='failed',
                finished='2026-11-03T23:59:59.500000+00:00')
    monkeypatch.setattr(accounting, 'quota_date', lambda: '2026-11-03')
    with pytest.raises(ValueError, match='ambiguous'):
        accounting.owner_consumed_records(tmp_path, owner)


# LLM: 使用原真实curator fixture和供应商事件替身；原_handle及终态提交/P3/claim保持，不声称native或真实LLM端到端。
# 函数用途: 完成一次真正策展并经原终态生产者落规范terminal。
def _finish_real_curator(case):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_transactions import (
        request_transaction_ports_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
    from agent_py_agent.tests.test_gateway_background_curator_transactions import run_original

    agent, service, backend, gate, claim, _close = case
    backend.release.set()
    response = run_original(agent, gate, claim)
    assert response['ok'] is True and response['background_result']['status'] == 'succeeded'
    assert len(service.run_log.list()) == 1
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        _finish_claimed_gateway_request(
            gate.paths, claim.path, claim.request_id, response,
            conversation_store=agent.conversation_store, execution_agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id,
            expected_lease_epoch=claim.lease_epoch)
    terminal = read_json_file(gate.paths.terminal / claim.path.name)
    assert terminal['terminal_response']['background_result']['run_id'] == service.run_log.list()[0].run_id
    return terminal


# LLM: 直接调用原release入口，必须先持久计一次真实完成再清原P3；重复释放不得重扣。
# 函数用途: 原终态释放不会丢掉完成事实。
def test_canonical_release_accounts_actual_run_before_freeing_p3(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as case:
        _finish_real_curator(case)
        _agent, _service, _backend, gate, claim, _close = case
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is True, result
        assert admission._read_reservation(admission.reservation_file(gate.paths, claim.request_id))['state'] == 'released'
        path = tmp_path / 'home' / 'global_index' / 'curator_daily_quota.json'
        assert accounting.read_quota_count(path) == 1
        admission.release_reservation_if_terminal(gate, claim.request_id)
        assert accounting.read_quota_count(path) == 1


# LLM: 只在全局会计写入注入一次EIO；原canonical终态/原P3写者与权限校验不替换。
# 函数用途: count落盘失败保留occupied，恢复后重放不丢不重。
def test_quota_commit_io_failure_keeps_p3_occupied_until_replay(tmp_path, monkeypatch):
    import errno

    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as case:
        _finish_real_curator(case)
        _agent, _service, _backend, gate, claim, _close = case
        writer = accounting.write_json_file_atomic_unlocked

        # LLM: EIO只描述当前持久化依赖失败，不把原业务执行替换为测试结果。
        # 函数用途: 让第一次计账明确不能落盘。
        def fail(path, payload):
            if path.name == 'curator_daily_quota.json':
                raise OSError(errno.EIO, 'injected quota write')
            return writer(path, payload)

        monkeypatch.setattr(accounting, 'write_json_file_atomic_unlocked', fail)
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is False
        assert admission._read_reservation(admission.reservation_file(gate.paths, claim.request_id))['state'] == 'occupied'
        monkeypatch.setattr(accounting, 'write_json_file_atomic_unlocked', writer)
        retry = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert retry['released'] is True, (retry, sorted(p.name for p in admission.reservations_dir(gate.paths).iterdir()))
        path = tmp_path / 'home' / 'global_index' / 'curator_daily_quota.json'
        assert accounting.read_quota_count(path) == 1


# LLM: 只推进原产品的固定批准备，不提前创建P3，也不以reuse冒充首次真实准入。
# 函数用途: 显式区分冷迁移和稳态测量，固定上限内必须完成。
def _warm_quota(gate, payload):
    prepare = getattr(accounting, 'prepare_quota_admission', None)
    if prepare is None:
        return
    for _tick in range(128):
        if prepare(gate, payload):
            return
    pytest.fail('bounded quota migration did not finish')


# LLM: 测试数据全在C出生前沿原Count0仓储生产；真正有来源且随后有occupied P3的owner才计活跃。
# 函数用途: 准备5/20活跃Owner与额外空注册目录，不伪造业务gate。
def _scale_control(tmp_path, monkeypatch, registered, active, *, distributed=False):
    from contextlib import contextmanager

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import freeze_source
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
    from agent_py_agent.agent.settings.config import AgentConfig

    # LLM: context包装只管理原C lease，退出不改业务记录或放宽生命周期。
    # 函数用途: 让规模输入完整覆盖实际coordinator的存续区间。
    @contextmanager
    def opened():
        home = tmp_path / 'home'
        monkeypatch.setenv('MY_AGENT_HOME', str(home))
        config = AgentConfig(my_agent_home=str(home))
        refs = []
        for number in range(registered):
            provider = f'provider-{number:04d}' if distributed else 'feishu'
            owner = OwnerIdentity.provider_user(provider, f'scale-{number:04d}')
            resolved = ensure_owner_home(home, owner)
            if number >= active:
                continue
            store = ConversationStore(resolved.home_dir / 'conversations')
            thread = store.threads.get_or_create({'canonical_user_id': owner.owner_id,
                'channel': 'internal', 'channel_conversation_id': f'scale-{number}'})
            store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '规模输入'})
            state = MemoryCuratorStateStore(resolved.home_dir / 'memory' / 'curator' / 'state.json')
            for run in range(2):
                _append_run(home, owner, f'memory-curator-run-{number}-{run}',
                    finished=accounting.quota_date() + 'T00:00:01+00:00')
            refs.append(freeze_source(home, owner, state.path, 'curator'))
        write_json_file_atomic(home / 'global_index' / 'curator_daily_quota.json',
            {'date': accounting.quota_date(), 'count': active * 2})
        paths = gateway_paths_from_root(tmp_path / 'gateway')
        with coordinator_guard(paths.root, 2):
            controller = prepare_coordinator_takeover(paths, 2, 'scale-curator')
            assert controller.active()
            control = GatewayControlPlane(config, home, paths, 2, 'scale-curator', agent_root=tmp_path,
                workspace_roots=(tmp_path,))
            yield control, refs

    return opened()


# LLM: 只旁观原fd读取与原生成器yield/resume；不换锁、不包controller方法、不注入权限，fd重用需inode同一才计数。
# 函数用途: 记录真实日账文件/字节/行及controller已持锁的决策区间。
def _quota_meter(monkeypatch):
    import dis
    import sys
    import time
    from contextlib import contextmanager

    try:
        from agent_py_agent.agent.gateway_parts import curator_quota_index as index
    except ImportError:
        index = None

    # LLM: 恢复所有观察器，避免测试后污染其它回归；io业务与异常仍交原函数执行。
    # 函数用途: 在一次实际admit周围收集严格入口的成本证据。
    @contextmanager
    def metered():
        metrics = {'owner_log_opens': 0, 'audit_bytes': 0, 'audit_rows': 0,
                   'hot_audit_bytes': 0, 'hot_seconds': 0.0, 'hot_intervals': 0}
        tracked, hot = {}, [None]
        original_open = accounting.open_readonly_file_beneath
        original_read = os.read
        original_accept = index._accept_line if index is not None else None
        previous_profile = sys.getprofile()
        code = admission._runtime_decision_scope.__wrapped__.__code__

        # LLM: 打开失败也计为尝试，inode绑定仅用于随后实际读取，不把fd编号当稳定身份。
        # 函数用途: 旁观原日账fd生产点。
        def opened(root, relative, *args, **kwargs):
            is_audit = tuple(relative[:3]) == ('memory', 'curator', 'runs')
            if is_audit:
                metrics['owner_log_opens'] += 1
            descriptor = original_open(root, relative, *args, **kwargs)
            if is_audit:
                stat = os.fstat(descriptor)
                tracked[descriptor] = (stat.st_dev, stat.st_ino)
            return descriptor

        # LLM: 严格先核fd原inode，再执行同一个原read；不改变短读、EOF或异常。
        # 函数用途: 计量实际读取的原JSONL字节。
        def read(descriptor, count):
            stat = os.fstat(descriptor) if descriptor in tracked else None
            match = stat is not None and tracked[descriptor] == (stat.st_dev, stat.st_ino)
            chunk = original_read(descriptor, count)
            if match:
                metrics['audit_bytes'] += len(chunk)
                if hot[0] is not None:
                    metrics['hot_audit_bytes'] += len(chunk)
            return chunk

        # LLM: 统计原解析器实际接受的行，不把预读buffer里尚未解析的换行算已处理。
        # 函数用途: 观察双预算中的真实行处理量。
        def accept(job, owner, line):
            metrics['audit_rows'] += 1
            return original_accept(job, owner, line)

        # LLM: yield发生在原controller mutex已取得后，resume发生在释放前；只计中间实际决策，不声称包含锁等待。
        # 函数用途: 不替换原锁地观测持锁热门区间。
        def profile(frame, event, arg):
            if frame.f_code is code:
                if event == 'return' and dis.opname[code.co_code[frame.f_lasti]] == 'YIELD_VALUE':
                    hot[0] = time.perf_counter()
                elif event == 'call' and hot[0] is not None:
                    metrics['hot_seconds'] += time.perf_counter() - hot[0]
                    metrics['hot_intervals'] += 1
                    hot[0] = None
            if previous_profile is not None:
                previous_profile(frame, event, arg)

        with monkeypatch.context() as observation:
            observation.setattr(accounting, 'open_readonly_file_beneath', opened)
            if index is not None:
                observation.setattr(index, 'open_readonly_file_beneath', opened)
                observation.setattr(index, '_accept_line', accept)
            observation.setattr(os, 'read', read)
            try:
                sys.setprofile(profile)
                yield metrics
            finally:
                sys.setprofile(previous_profile)

    return metered()


# LLM: 同活跃owner/同日完成事实下只增长空注册目录；测量的是尚未有P3的最后一个原请求，不是重复reuse。
# 函数用途: 保证真实准入成本不随5→1000注册量增长，且本次没有fd泄漏。
@pytest.mark.parametrize(('registered', 'active'), [(5, 5), (100, 5), (1000, 5), (20, 20), (1000, 20)])
def test_active_owner_matrix_measures_real_audit_and_hot_cost(tmp_path, monkeypatch, registered, active):
    with _scale_control(tmp_path, monkeypatch, registered, active) as (control, refs):
        paths = [enqueue_source(control.paths, control.home_root, ref)[0] for ref in refs]
        run = cold_context(control)
        gate = admission.coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        payloads = [accounting._occupied_request(control.paths, path.stem) for path in paths]
        _warm_quota(gate, payloads[0])
        for path, payload, ref in zip(paths[:-1], payloads[:-1], refs[:-1], strict=True):
            decision = gate.admit(path, (source_owner(ref['owner']), admission.session_scope_for_request(payload)),
                AdmissionLimits(64, 64), snapshot=payload)
            assert decision['status'] == 'admitted', decision
        target = paths[-1]
        assert admission._read_reservation(admission.reservation_file(gate.paths, target.stem)) is None
        descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
        before = len(os.listdir(descriptors))
        with _quota_meter(monkeypatch) as costs:
            decision = gate.admit(target,
                (source_owner(refs[-1]['owner']), admission.session_scope_for_request(payloads[-1])),
                AdmissionLimits(64, 64), snapshot=payloads[-1])
        delta = len(os.listdir(descriptors)) - before
        print({'registered': registered, 'active': active, **costs, 'fd_delta': delta})
        assert decision['status'] == 'admitted', decision
        assert gate.occupied_snapshot()['total'] == active
        assert delta == 0
        assert costs['owner_log_opens'] <= 8 and costs['audit_bytes'] <= 65536 and costs['audit_rows'] <= 64
        assert costs['hot_audit_bytes'] == 0 and costs['hot_intervals'] >= 1
        assert costs['hot_seconds'] < 1.0


# LLM: legacy计数是明确的迁移输入，不声称已跑4999次模型；新完成由原curator和规范terminal真正生产。
# 函数用途: 初次迁移不能把刚完成的豁免P3吞进legacy下限。
def test_legacy_floor_does_not_absorb_new_exempt_completion(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as case:
        agent, _service, _backend, gate, claim, _close = case
        path = agent.home_paths.global_index_dir / 'curator_daily_quota.json'

        # LLM: 写fixture也走原当前C短门，不绕过全局写者约束。
        # 函数用途: 安装与原基线一致的legacy 4999输入。
        def seed():
            accounting.write_json_file_atomic_unlocked(path, {'date': accounting.quota_date(), 'count': 4999})

        assert gate._runtime_takeover.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), seed)['status'] == 'started'
        _finish_real_curator(case)
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is True, result
        assert accounting.read_quota_count(path) == 5000
        assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
        assert accounting.read_quota_count(path) == 5000


# LLM: 只把会计观察时钟推进到次日，原完成日志、claim、terminal与P3不改；这是完成到释放跨日，不冒称真实等待一天。
# 函数用途: 迟到结清按原UTC完成日计一次，不能误扣次日或永久卡P3。
def test_completion_release_after_midnight_charges_finished_day_once(tmp_path, monkeypatch):
    import json
    from datetime import date, timedelta

    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as case:
        _finish_real_curator(case)
        agent, service, _backend, gate, claim, _close = case
        finished_day = service.run_log.list()[0].finished_at[:10]
        tomorrow = (date.fromisoformat(finished_day) + timedelta(days=1)).isoformat()
        monkeypatch.setattr(accounting, 'quota_date', lambda: tomorrow)
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is True, result
        path = agent.home_paths.global_index_dir / 'curator_daily_quota.json'
        assert accounting.read_quota_count(path) == 0
        history = agent.home_paths.global_index_dir / 'curator_quota_history' / (finished_day + '.json')
        original = json.loads(history.read_text())
        assert original['date'] == finished_day and original['count'] == 1
        assert original['accounted_records'] == [_expected_identity(OwnerIdentity.local_main(), service.run_log.list()[0].run_id)]
        assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
        assert json.loads(history.read_text()) == original


# LLM: 两线程在事件屏障后一同进入原admit，真正P3/quota/controller锁不替换；只有最后一个预算可分配。
# 函数用途: 不同Owner并发不能同时拿走同一个全局剩余单位。
def test_two_owners_compete_for_last_quota_unit(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    with curator_control(tmp_path, monkeypatch) as (control, refs):
        paths = [enqueue_source(control.paths, control.home_root, ref)[0] for ref in refs]
        run = cold_context(control)
        gate = admission.coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        payloads = [accounting._occupied_request(control.paths, path.stem) for path in paths]
        _warm_quota(gate, payloads[0])
        ready = threading.Barrier(2)

        # LLM: 测试事件只确定竞争同时起跑，原准入和拒绝原因由产品裁决。
        # 函数用途: 运行一名Owner的真实首次准入。
        def compete(number):
            ready.wait(5)
            return gate.admit(paths[number], (source_owner(refs[number]['owner']),
                admission.session_scope_for_request(payloads[number])), AdmissionLimits(8, 8), snapshot=payloads[number])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(compete, number) for number in range(2)]
            outcomes = [future.result(10) for future in futures]
        assert sum(row['status'] == 'admitted' for row in outcomes) == 1, outcomes
        assert [row['reason'] for row in outcomes if row['status'] == 'blocked'] == ['curator_daily_quota_exhausted']
        assert gate.occupied_snapshot()['total'] == 1
        assert accounting.read_quota_count(control.home_root / 'global_index' / 'curator_daily_quota.json') == 4999


# LLM: 只旁观原os目录迭代器和原审计解析，不替换来源、CAS或业务裁决。
# 函数用途: 量化冷目录枚举及日账双预算，包含helper内部全量listdir。
def _cold_meter(monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    # LLM: 退出恢复原函数；迭代器关闭仍交标准库。
    # 函数用途: 为真实准备tick收集目录项、字节和行数。
    @contextmanager
    def metered():
        metrics = {'entries': 0, 'bytes': 0, 'rows': 0}
        original_list, original_scan, original_read = os.listdir, os.scandir, os.read
        original_accept = index._accept_line
        original_open, tracked = index.open_readonly_file_beneath, {}

        # LLM: 仅原日账fd参与64KiB预算，state版本点读不混入日账正文统计；复用fd必须同inode。
        # 函数用途: 精确绑定被测原审计的读取观察点。
        def opened(root, relative, *args, **kwargs):
            descriptor = original_open(root, relative, *args, **kwargs)
            if tuple(relative[:3]) == ('memory', 'curator', 'runs'):
                stat = os.fstat(descriptor)
                tracked[descriptor] = (stat.st_dev, stat.st_ino)
            return descriptor

        # LLM: 不截断全量结果，旧helper的隐藏全扫必须计入。
        # 函数用途: 计量原listdir物化条目。
        def listed(*args, **kwargs):
            result = original_list(*args, **kwargs)
            if not metrics.get('_quota_only') or _inside_quota_index():
                metrics['entries'] += len(result)
            return result

        # LLM: 仅代理原next/close，不缓存或预读。
        # 函数用途: 提供同语义的目录观察器。
        class Scan:
            # LLM: 保留原fd迭代器。
            # 函数用途: 初始化观察代理。
            def __init__(self, *args, **kwargs):
                self.source = original_scan(*args, **kwargs)

            # LLM: 迭代不预读。
            # 函数用途: 返回当前观察器。
            def __iter__(self):
                return self

            # LLM: EOF不计为访问。
            # 函数用途: 记录实际返回的条目。
            def __next__(self):
                item = next(self.source)
                current = _cold_observation.get()
                if current is not None and (not current.get('_quota_only') or _inside_quota_index()):
                    current['entries'] += 1
                return item

            # LLM: 句柄由标准库关闭。
            # 函数用途: 释放原迭代器。
            def close(self):
                self.source.close()

            # LLM: 上下文不预读。
            # 函数用途: 保留scandir接口。
            def __enter__(self):
                return self

            # LLM: 异常也释放原句柄。
            # 函数用途: 保留退出语义。
            def __exit__(self, *args):
                self.close()

        # LLM: 只统计同inode的原日账真实read结果，不统计state元数据或请求上限。
        # 函数用途: 观察原fd字节读取。
        def read(*args):
            stat = os.fstat(args[0]) if args[0] in tracked else None
            result = original_read(*args)
            if stat is not None and tracked[args[0]] == (stat.st_dev, stat.st_ino):
                metrics['bytes'] += len(result)
            return result

        # LLM: 行计数包含失败前的访问，不吞异常。
        # 函数用途: 记录真实解析次数。
        def accept(*args):
            metrics['rows'] += 1
            return original_accept(*args)

        with monkeypatch.context() as observation:
            observation.setattr(os, 'listdir', listed)
            observation.setattr(os, 'scandir', Scan)
            observation.setattr(os, 'read', read)
            observation.setattr(index, 'open_readonly_file_beneath', opened)
            observation.setattr(index, '_accept_line', accept)
            token = _cold_observation.set(metrics)
            try:
                yield metrics
            finally:
                _cold_observation.reset(token)

    return metered()


# LLM: terminal只给日期定位提示，不签P3权限。
# 函数用途: 给纯locator回归准确时间范围。
def _locator_terminal():
    from datetime import datetime, timezone
    stamp = datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp()
    return {'id': 'locator-request', 'execution_attempt_id': 'original-attempt', 'lease_epoch': 1,
            'route': {'binding': 'original-route'}, 'lease_started_at': stamp, 'completed_at': stamp + 1}


# LLM: receipt落盘后合法来源变化必须重读，不能以放宽validate通过。
# 函数用途: 覆盖失效receipt永久pending的重放窗口。
@pytest.mark.parametrize('mutation', ['state', 'append', 'replace', 'state_replace'])
def test_persisted_completion_receipt_rebuilds_changed_source(tmp_path, monkeypatch, mutation):
    import json

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index
    from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore

    owner, day, run_id = OwnerIdentity.local_main(), '2026-11-03', 'memory-curator-run-target'
    _append_run(tmp_path, owner, run_id)
    if mutation == 'state_replace':
        MemoryCuratorStateStore(tmp_path / 'owners/local/main/memory/curator/state.json').request('admin')
    for number in range(129):
        _append_run(tmp_path, owner, f'memory-curator-run-history-{number}')
    terminal = _locator_terminal()
    value = {'date': day, 'count': 0}
    for _tick in range(16):
        value, done = index.prepare_completion(value, tmp_path, owner, run_id, terminal)
        value = json.loads(json.dumps(value))
        if done:
            break
    assert done
    binding = index.completion_binding(terminal, owner, run_id)
    previous = value['quota_completion_receipts'][binding]
    if mutation == 'state':
        MemoryCuratorStateStore(tmp_path / 'owners/local/main/memory/curator/state.json').request('admin')
    elif mutation == 'append':
        _append_run(tmp_path, owner, 'memory-curator-run-appended')
    elif mutation == 'replace':
        path = tmp_path / 'owners/local/main/memory/curator/runs' / (day + '.jsonl')
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    else:
        path = tmp_path / 'owners/local/main/memory/curator/state.json'
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    with pytest.raises(ValueError, match='source_changed'):
        index.validate_completion(previous, tmp_path, owner, run_id, terminal)
    with _cold_meter(monkeypatch) as costs:
        value, done = index.prepare_completion(value, tmp_path, owner, run_id, terminal)
    assert done is False
    assert binding not in value['quota_completion_receipts']
    assert costs['rows'] <= 64 and costs['bytes'] <= 65536
    for _tick in range(16):
        value = json.loads(json.dumps(value))
        value, done = index.prepare_completion(value, tmp_path, owner, run_id, terminal)
        if done:
            break
    assert done
    fact = index.validate_completion(value['quota_completion_receipts'][binding], tmp_path, owner, run_id, terminal)
    assert fact['identity'] == _expected_identity(owner, run_id)
    assert value['count'] == 0


# LLM: 不预热旧helper快照，每tick只可访问32个真实目录项。
# 函数用途: 检查固定批不是只截取返回页。
@pytest.mark.parametrize('registered', [100, 1000])
def test_cold_migration_directory_entry_budget(tmp_path, monkeypatch, registered):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    for number in range(registered):
        (tmp_path / 'owners/providers/feishu/users' / f'cold-{number:04d}').mkdir(parents=True)
    value = {'date': '2026-11-03', 'count': 4999}
    try:
        with _cold_meter(monkeypatch) as costs:
            candidate = index.prepare_batch(value, tmp_path, value['date'])
    finally:
        index.close_directory_cursor(home_root=tmp_path)
    print({'registered': registered, **costs})
    assert costs['entries'] <= 32
    assert costs['rows'] <= 64 and costs['bytes'] <= 65536
    assert candidate['quota_index']['status'] == 'scanning'
    assert candidate['count'] == 4999


# LLM: 只提供目录读取资源的生命周期，不替代真实C的权限；纯迁移测试不调用业务写者。
# 函数用途: 让进程重启/CAS回滚回归能显式关闭可丢弃游标。
class _QuotaCursorHost:
    pass


# LLM: 每tick结果按JSON往返模拟原quota提交边界，不复制内存句柄到持久数据。
# 函数用途: 推进完整冷迁移并观察每tick真实固定预算，包括最终tick。
def _drain_cold_index(value, home, host, monkeypatch):
    import json

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    maximum = {'entries': 0, 'bytes': 0, 'rows': 0}
    for ticks in range(1, 257):
        with _cold_meter(monkeypatch) as costs:
            value = index.prepare_batch(value, home, value['date'], controller=host)
        for name in maximum:
            maximum[name] = max(maximum[name], costs[name])
        assert costs['entries'] <= 32 and costs['rows'] <= 64 and costs['bytes'] <= 65536, costs
        value = json.loads(json.dumps(value))
        if index.ready(value, home, value['date']):
            print({'ticks': ticks, 'maximum_per_tick': maximum})
            return value
    pytest.fail('bounded canonical migration did not finish')


# LLM: 覆盖注册新增/整桶inode更换/重启/丢失CAS；旧内存位置绝不能漏读或给未知签ready。
# 函数用途: 全迁移每tick有界且变更后重新canonical核验，legacy下限和事实身份不重扣。
@pytest.mark.parametrize('change', ['register', 'bucket_replace', 'restart', 'partial_restart', 'lost_cas'])
def test_cold_migration_rechecks_canonical_after_change(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    owner = OwnerIdentity.provider_user('feishu', 'cold-0000')
    _append_run(tmp_path, owner, 'memory-curator-run-cold')
    bucket = tmp_path / 'owners/providers/feishu/users'
    for number in range(1000):
        (bucket / f'cold-{number:04d}').mkdir(exist_ok=True)
    host = _QuotaCursorHost()
    descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    before = len(os.listdir(descriptors))
    value = {'date': '2026-11-03', 'count': 4999}
    try:
        if change in {'lost_cas', 'partial_restart'}:
            candidate = index.prepare_batch(value, tmp_path, value['date'], controller=host)
            assert candidate['quota_index']['status'] == 'scanning'
            # 上批候选未提交，下一拍必须从旧原quota重新核验，不能续内存流跳过已经读的owner。
            if change == 'partial_restart':
                import json

                value = json.loads(json.dumps(candidate))
                index.close_directory_cursor(host)
                host = _QuotaCursorHost()
                assert not index.ready(value, tmp_path, value['date'])
        else:
            value = _drain_cold_index(value, tmp_path, host, monkeypatch)
            assert value['count'] == 4999
            assert value['accounted_records'] == [_expected_identity(owner, 'memory-curator-run-cold')]
            if change == 'register':
                added = OwnerIdentity.local_user('new-local')
                _append_run(tmp_path, added, 'memory-curator-run-new-local')
                assert not index.ready(value, tmp_path, value['date'])
            elif change == 'bucket_replace':
                bucket.rename(bucket.with_name('previous-users'))
                bucket.mkdir()
                _append_run(tmp_path, owner, 'memory-curator-run-cold')
                assert not index.ready(value, tmp_path, value['date'])
            else:
                index.close_directory_cursor(host)
                host = _QuotaCursorHost()
                assert not index.ready(value, tmp_path, value['date'])
        value = _drain_cold_index(value, tmp_path, host, monkeypatch)
        assert value['count'] == 4999
        assert _expected_identity(owner, 'memory-curator-run-cold') in value['accounted_records']
        if change == 'register':
            assert _expected_identity(added, 'memory-curator-run-new-local') in value['accounted_records']
        # 原生事件订阅是C存续期资源；结束本轮后仍要求全部真实fd回到扫描前基线。
        index.close_directory_cursor(host)
        assert len(os.listdir(descriptors)) - before == 0
    finally:
        index.close_directory_cursor(host)
    assert len(os.listdir(descriptors)) - before == 0


# LLM: 损坏的绑定不是来源过期；不能删除receipt再借重读自动修复身份冲突。
# 函数用途: 验证prepare与短门都拒绝错误owner/run/fact绑定。
@pytest.mark.parametrize('field', ['owner', 'run_id', 'binding', 'fact'])
def test_completion_binding_conflict_is_not_rebuilt(tmp_path, field):
    import copy

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    owner, run_id = OwnerIdentity.local_main(), 'memory-curator-run-binding'
    _append_run(tmp_path, owner, run_id)
    terminal, value = _locator_terminal(), {'date': '2026-11-03', 'count': 0}
    value, done = index.prepare_completion(value, tmp_path, owner, run_id, terminal)
    assert done
    binding = index.completion_binding(terminal, owner, run_id)
    receipt = value['quota_completion_receipts'][binding]
    receipt[field] = ({'identity': '0' * 64, 'finished_at': receipt['fact']['finished_at']}
                      if field == 'fact' else 'conflicting-original-binding')
    original = copy.deepcopy(value)
    with pytest.raises(ValueError, match='binding_conflict'):
        index.prepare_completion(value, tmp_path, owner, run_id, terminal)
    assert value == original


# LLM: 使用原C写者安装0/1旧计数，之后真实enqueue/admit；不伪造P3或把reuse当首次准入。
# 函数用途: 明确保留小额度真实准入正例，迁移后不能误当unknown或额度耗尽。
@pytest.mark.parametrize('consumed', [0, 1])
def test_zero_one_count_real_admission_positive(tmp_path, monkeypatch, consumed):
    with curator_control(tmp_path, monkeypatch) as (control, refs):
        path, _ = enqueue_source(control.paths, control.home_root, refs[0])
        run = cold_context(control)
        gate = admission.coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        quota_path = control.home_root / 'global_index/curator_daily_quota.json'

        # LLM: fixture写入也由原controller末门生产，不绕过全局写者校验。
        # 函数用途: 保持原0/1 legacy输入。
        def seed():
            accounting.write_json_file_atomic_unlocked(quota_path, {'date': accounting.quota_date(), 'count': consumed})

        assert gate._runtime_takeover.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), seed)['status'] == 'started'
        payload = accounting._occupied_request(control.paths, path.stem)
        _warm_quota(gate, payload)
        result = gate.admit(path, (source_owner(refs[0]['owner']), admission.session_scope_for_request(payload)),
                            AdmissionLimits(8, 8), snapshot=payload)
        assert result['status'] == 'admitted', result
        assert accounting.read_quota_count(quota_path) == consumed
        assert gate.occupied_snapshot()['total'] == 1


# LLM: 本地前台输入经原input receipt/admit/claim取得独立P3，再在原执行/事务端口调用业务request；不复用终态claim或伪造端口。
# 函数用途: 在完成receipt已落盘后，合法提交一次同Owner的后续admin状态请求。
def _request_admin_in_fresh_claim(agent, service, gate):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.request_transactions import (
        request_transaction_ports_scope,
    )
    from agent_py_agent.tests.test_gateway_cold_request_pump import queue_request

    receipt = queue_request(gate, identifier='quota-admin-followup', local=True)
    path = gate.paths.inbox / (receipt.request_id + '.json')
    payload = read_json_file(path)
    decision = gate.admit(path, (OwnerIdentity.local_main(), admission.session_scope_for_request(payload)),
                          AdmissionLimits(8, 8), snapshot=payload)
    assert decision['status'] == 'admitted', decision
    claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
    assert claim is not None
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        service.request('admin')


# LLM: 原receipt确已持久化后再合法更新state；fsync变体在原rename后注入一次EIO，P3必须留occupied直到重放。
# 函数用途: 通过同一个真实release入口覆盖来源失效重建和rename后耐久失败，最终准确计一次。
@pytest.mark.parametrize('sync_failure', [False, True])
def test_real_release_rebuilds_persisted_receipt_after_state_change(tmp_path, monkeypatch, sync_failure):
    import errno

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as case:
        terminal = _finish_real_curator(case)
        agent, service, _backend, gate, claim, _close = case
        for _tick in range(128):
            if accounting.prepare_quota_release(gate, terminal):
                break
        else:
            pytest.fail('completion receipt did not become durable')
        path = agent.home_paths.global_index_dir / 'curator_daily_quota.json'
        value = accounting._read_quota(path)
        owner, run_id = OwnerIdentity.local_main(), service.run_log.list()[0].run_id
        binding = index.completion_binding(terminal, owner, run_id)
        assert binding in value['quota_completion_receipts']
        _request_admin_in_fresh_claim(agent, service, gate)
        if sync_failure:
            sync = accounting._sync_quota
            failed = []

            # LLM: 先由原atomic writer完成rename，再让fsync叶失败；不改变来源/P3/原lease。
            # 函数用途: 模拟receipt重建已rename但目录耐久未确认的窗口。
            def fail_once(current):
                if current == path and not failed:
                    persisted = accounting._read_quota(current).get('quota_completion_receipts', {}).get(binding)
                    if persisted is not None and persisted['quiet'] == index._quiet_state(agent.home_paths.root, owner):
                        failed.append(True)
                        raise OSError(errno.EIO, 'simulated post-rename sync error')
                return sync(current)

            monkeypatch.setattr(accounting, '_sync_quota', fail_once)
            result = admission.release_reservation_if_terminal(gate, claim.request_id)
            assert result['released'] is False
            assert failed == [True]
            assert admission._read_reservation(admission.reservation_file(gate.paths, claim.request_id))['state'] == 'occupied'
            monkeypatch.setattr(accounting, '_sync_quota', sync)
        result = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert result['released'] is True, result
        assert accounting.read_quota_count(path) == 1
        assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
        assert accounting.read_quota_count(path) == 1


# LLM: 使用原C生命周期和真实scandir，关闭只撤销可丢弃读取资源，不改原quota事实。
# 函数用途: 原控制器结束时回收未扫完的额度目录句柄。
def test_original_controller_close_closes_partial_quota_scan(context):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    controller = context.controller()
    for number in range(80):
        (context.home_root / 'owners/providers/feishu/users' / f'close-{number:04d}').mkdir(parents=True)
    descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    before = len(os.listdir(descriptors))
    value = {'date': accounting.quota_date(), 'count': 0}
    candidate = index.prepare_batch(value, context.home_root, value['date'], controller=controller)
    cursor = controller._curator_quota_directory_cursor
    assert candidate['quota_index']['status'] == 'scanning'
    assert cursor.stream is not None
    # 原有一个scandir fd之外，逐个核对新增的明确归属监听fd；关闭后的零差断言不变。
    observer = cursor.observer
    owned = 1 + sum(fd is not None for fd in observer.directories.values())
    assert len(os.listdir(descriptors)) == before + owned + 1
    controller.close()
    assert cursor.stream is None
    assert controller._curator_quota_directory_cursor is None
    assert len(os.listdir(descriptors)) == before


# LLM: 只检查观察时的真实Python栈，不改变任何业务调用或权限。
# 函数用途: 区分quota目录审计与同一admit中其它固定活跃P3目录工作。
def _inside_quota_index():
    import sys

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get('__name__') == index.__name__:
            return True
        frame = frame.f_back
    return False


# LLM: 所有观察器只转发原系统调用，不补造目录版本、事件或业务裁决。
# 函数用途: 同时统计目录打开、元数据系统调用与真实枚举条目。
def _metadata_meter(monkeypatch):
    from contextlib import contextmanager

    # LLM: 恢复观察器后仍保留实测结果，目录流跨拍访问由原冷表计记录。
    # 函数用途: 为准备和真正准入分别建立可比较的元数据观察区间。
    @contextmanager
    def measured():
        import sys

        costs = {'open_calls': 0, 'stat_calls': 0, 'total_open_calls': 0, 'total_stat_calls': 0}
        previous = sys.getprofile()

        # LLM: 旁观C调用而不替换os.open/stat，保持supports_dir_fd能力探针；全部open作为目录open的保守上界。
        # 函数用途: 记录包含no-follow祖先遍历在内的原始open和stat调用。
        def profile(frame, event, arg):
            if event == 'c_call':
                if arg is os.open:
                    costs['total_open_calls'] += 1
                    costs['open_calls'] += int(_inside_quota_index())
                elif arg in (os.stat, os.fstat, os.lstat):
                    costs['total_stat_calls'] += 1
                    costs['stat_calls'] += int(_inside_quota_index())
            if previous is not None:
                previous(frame, event, arg)

        with _cold_meter(monkeypatch) as cold:
            cold['_quota_only'] = True
            try:
                sys.setprofile(profile)
                yield costs
            finally:
                sys.setprofile(previous)
                cold.pop('_quota_only')
                costs.update(cold)

    return measured()


# LLM: 固定五个活跃Owner及十个完成事实，只增长分布在不同合法provider的空注册目录。
# 函数用途: 实测一拍prepare和真正热门admit的目录open/stat/枚举预算，不用JSONL预算替代。
@pytest.mark.parametrize('registered', [100, 1000])
def test_distributed_provider_metadata_budget_is_flat(tmp_path, monkeypatch, registered):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    with _scale_control(tmp_path, monkeypatch, registered, 5, distributed=True) as (control, refs):
        paths = [enqueue_source(control.paths, control.home_root, ref)[0] for ref in refs]
        run = cold_context(control)
        gate = admission.coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        payloads = [accounting._occupied_request(control.paths, path.stem) for path in paths]
        _warm_quota(gate, payloads[0])
        for path, payload, ref in zip(paths[:-1], payloads[:-1], refs[:-1], strict=True):
            result = gate.admit(path, (source_owner(ref['owner']), admission.session_scope_for_request(payload)),
                                AdmissionLimits(64, 64), snapshot=payload)
            assert result['status'] == 'admitted', result
        value = accounting._read_quota(control.home_root / 'global_index/curator_daily_quota.json')
        assert value['count'] == 10 and len(value['accounted_records']) == 10
        with _metadata_meter(monkeypatch) as prepared:
            candidate = index.prepare_batch(value, control.home_root, value['date'],
                owners=(source_owner(refs[-1]['owner']),), controller=gate._runtime_takeover)
        assert index.ready(candidate, control.home_root, value['date'])
        with _metadata_meter(monkeypatch) as hot:
            result = gate.admit(paths[-1], (source_owner(refs[-1]['owner']),
                admission.session_scope_for_request(payloads[-1])), AdmissionLimits(64, 64), snapshot=payloads[-1])
        print({'registered_providers': registered, 'prepare': prepared, 'hot_admit': hot})
        assert result['status'] == 'admitted', result
        for costs in (prepared, hot):
            assert costs['open_calls'] <= 192, costs
            assert costs['stat_calls'] <= 256, costs
            assert costs['entries'] <= 32 and costs['rows'] <= 64 and costs['bytes'] <= 65536, costs
            assert costs['total_open_calls'] <= 512 and costs['total_stat_calls'] <= 2048, costs


# LLM: 全部变更发生在旧ready之后，使用真实目录/内核事件；不让mtime或根目录检查替代bucket订阅。
# 函数用途: 验证已有provider新增Owner、替换、重启、事件来源丢失及合并事件后fail-closed有界重审。
@pytest.mark.parametrize('change', ['owner_add', 'bucket_replace', 'provider_replace',
                                  'watch_lost', 'event_burst', 'restart', 'missing_bucket'])
def test_native_directory_change_revokes_ready_and_reaudits(tmp_path, monkeypatch, change):
    import json

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    day = '2026-11-03'
    owner = OwnerIdentity.provider_user('provider-0000', 'original')
    _append_run(tmp_path, owner, 'memory-curator-run-original')
    for number in range(100):
        ensure_owner_home(tmp_path, OwnerIdentity.provider_user(f'provider-{number:04d}', 'empty'))
    host = _QuotaCursorHost()
    descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    before = len(os.listdir(descriptors))
    value = {'date': day, 'count': 0}
    added = OwnerIdentity.provider_user('provider-0050', 'new-owner')
    try:
        value = _drain_cold_index(value, tmp_path, host, monkeypatch)
        assert value['count'] == 1 and index.ready(value, tmp_path, day)
        original_token = value['quota_index']['inventory']
        if change in {'bucket_replace', 'provider_replace'}:
            target = tmp_path / 'owners/providers/provider-0050'
            if change == 'bucket_replace':
                target /= 'users'
            target.rename(target.with_name('.previous-' + target.name))
            _append_run(tmp_path, added, 'memory-curator-run-added')
        elif change == 'missing_bucket':
            added = OwnerIdentity.provider_group('provider-0050', 'new-group')
            _append_run(tmp_path, added, 'memory-curator-run-added')
        elif change == 'restart':
            index.close_directory_cursor(host)
            host = _QuotaCursorHost()
            value = json.loads(json.dumps(value))
        elif change == 'watch_lost':
            observer = host._curator_quota_directory_cursor.observer
            if observer.queue is not None:
                observer.queue.close()
            else:
                os.close(observer.descriptor)
                observer.descriptor = None
        else:
            if change == 'event_burst':
                bucket = tmp_path / 'owners/providers/provider-0050/users'
                for number in range(512):
                    path = bucket / f'transient-{number}'
                    path.mkdir()
                    path.rmdir()
            _append_run(tmp_path, added, 'memory-curator-run-added')
        assert not index.ready(value, tmp_path, day)
        maxima = {'open_calls': 0, 'stat_calls': 0, 'entries': 0, 'rows': 0, 'bytes': 0}
        for tick in range(256):
            with _metadata_meter(monkeypatch) as costs:
                value = index.prepare_batch(value, tmp_path, day, controller=host)
            for name in maxima:
                maxima[name] = max(maxima[name], costs[name])
            assert costs['open_calls'] <= 2048 and costs['stat_calls'] <= 4096, costs
            assert costs['entries'] <= 32 and costs['rows'] <= 64 and costs['bytes'] <= 65536, costs
            value = json.loads(json.dumps(value))
            if index.ready(value, tmp_path, day):
                break
        else:
            pytest.fail('native invalidation did not finish fixed-batch canonical reaudit')
        assert value['quota_index']['inventory'] != original_token
        assert _expected_identity(owner, 'memory-curator-run-original') in value['accounted_records']
        if change in {'restart', 'watch_lost'}:
            assert value['count'] == 1
        else:
            assert value['count'] == 2
            assert _expected_identity(added, 'memory-curator-run-added') in value['accounted_records']
        print({'native_change': change, 'reaudit_ticks': tick + 1, 'maximum_per_tick': maxima})
    finally:
        index.close_directory_cursor(host)
    assert len(os.listdir(descriptors)) - before == 0


def _expected_conservative_identity(owner, run_id, binding):
    import hashlib
    import json

    content = json.dumps({'owner_sha': owner_digest(owner), 'run_id': run_id, 'binding': binding},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def test_failed_completion_without_audit_persists_bounded_conservative_receipt(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    owner, run_id = OwnerIdentity.local_main(), 'memory-curator-run-no-audit'
    terminal = _locator_terminal()
    terminal['completed_at'] += 8 * 86400
    terminal['terminal_response'] = {'background_result': {'status': 'failed', 'run_id': run_id}}
    for number in range(129):
        _append_run(tmp_path, owner, f'memory-curator-run-unrelated-{number}')
    value = {'date': '2026-11-03', 'count': 0}
    binding = index.completion_binding(terminal, owner, run_id)
    for tick in range(16):
        with _cold_meter(monkeypatch) as costs:
            value, done = index.prepare_completion(value, tmp_path, owner, run_id, terminal)
        assert costs['rows'] <= 64 and costs['bytes'] <= 65536, costs
        value = json.loads(json.dumps(value))
        if done:
            break
    else:
        pytest.fail('failed completion without audit did not finish within bounded rounds')
    receipt = value['quota_completion_receipts'][binding]
    assert receipt['day'] == '2026-11-11'
    assert receipt['source'] == 'conservative' and receipt['quiet'] is True
    assert binding not in value['quota_pending_completions']
    assert receipt['fact']['identity'] == _expected_conservative_identity(owner, run_id, binding)
    assert value['count'] == 0

    def forbidden(*args, **kwargs):
        pytest.fail('conservative validation must not read audit or state')

    monkeypatch.setattr(index, 'audit_version', forbidden)
    monkeypatch.setattr(index, '_quiet_state', forbidden)
    assert index.validate_completion(receipt, tmp_path, owner, run_id, terminal) == receipt['fact']
    assert index.prepare_completion(value, tmp_path, owner, run_id, terminal) == (value, True)
    print({'conservative_prepare_rounds': tick + 1, 'receipt_day': receipt['day']})


@pytest.mark.parametrize('field', ['binding', 'owner', 'run_id', 'fact', 'status', 'attempt', 'terminal_run_id'])
def test_conservative_completion_rejects_changed_binding_or_nonfailed_terminal(tmp_path, field):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    owner, run_id = OwnerIdentity.local_main(), 'memory-curator-run-conservative-binding'
    terminal = _locator_terminal()
    terminal['terminal_response'] = {'background_result': {'status': 'failed', 'run_id': run_id}}
    value, done = index.prepare_completion({'date': '2026-11-03', 'count': 0}, tmp_path, owner, run_id, terminal)
    assert done
    binding = index.completion_binding(terminal, owner, run_id)
    receipt = value['quota_completion_receipts'][binding]
    if field == 'status':
        terminal['terminal_response']['background_result']['status'] = 'succeeded'
    elif field == 'attempt':
        terminal['execution_attempt_id'] = 'different-attempt'
    elif field == 'terminal_run_id':
        terminal['terminal_response']['background_result']['run_id'] = 'different-run'
    elif field == 'fact':
        receipt['fact']['identity'] = '0' * 64
    else:
        receipt[field] = 'different-binding'
    with pytest.raises(ValueError, match='binding_conflict'):
        index.validate_completion(receipt, tmp_path, owner, run_id, terminal)


def test_succeeded_completion_without_audit_remains_pending(tmp_path):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index

    owner, run_id = OwnerIdentity.local_main(), 'memory-curator-run-success-no-audit'
    terminal = _locator_terminal()
    terminal['terminal_response'] = {'background_result': {'status': 'succeeded', 'run_id': run_id}}
    value = {'date': '2026-11-03', 'count': 0}
    for _retry in range(3):
        with pytest.raises(ValueError, match='curator_quota_actual_audit_required'):
            index.prepare_completion(value, tmp_path, owner, run_id, terminal)
    assert value == {'date': '2026-11-03', 'count': 0}


def _finish_real_failed_curator_without_audit(case, monkeypatch):
    import errno

    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_transactions import (
        request_transaction_ports_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
    from agent_py_agent.tests.test_gateway_background_curator_transactions import run_original

    agent, service, backend, gate, claim, _close = case
    backend.payload = {}  # 原模型解析器拒绝无效输出，仍走真实租约和失败收口。
    backend.release.set()

    def unavailable(*args, **kwargs):
        raise OSError(errno.EIO, 'simulated unavailable audit writer')

    with monkeypatch.context() as failure:
        failure.setattr(service.run_log, 'append', unavailable)
        response = run_original(agent, gate, claim)
    result = response['background_result']
    assert result['status'] == 'failed' and result['run_id']
    assert backend.calls > 0 and service.run_log.list() == []
    assert not service.state_store.load().active_lease
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        _finish_claimed_gateway_request(gate.paths, claim.path, claim.request_id, response,
            conversation_store=agent.conversation_store, execution_agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    return read_json_file(gate.paths.terminal / claim.path.name)


@pytest.mark.parametrize('late_release', [False, True])
def test_real_failed_curator_without_audit_releases_and_accounts_once(tmp_path, monkeypatch, late_release):
    import json
    from datetime import datetime, timedelta, timezone

    from agent_py_agent.agent.gateway_parts import curator_quota_index as index
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    descriptors = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    before_fds = len(os.listdir(descriptors))
    with curator_case(tmp_path, monkeypatch) as case:
        terminal = _finish_real_failed_curator_without_audit(case, monkeypatch)
        agent, _service, _backend, gate, claim, _close = case
        owner = OwnerIdentity.local_main()
        run_id = terminal['terminal_response']['background_result']['run_id']
        binding = index.completion_binding(terminal, owner, run_id)
        occupied = gate.occupied_snapshot()
        assert occupied['total'] == 1 and occupied['per_owner'][owner_digest(owner)] == 1
        finished_day = datetime.fromtimestamp(terminal['completed_at'], timezone.utc).date()
        if late_release:
            tomorrow = (finished_day + timedelta(days=1)).isoformat()
            monkeypatch.setattr(accounting, 'quota_date', lambda: tomorrow)
        path = agent.home_paths.global_index_dir / 'curator_daily_quota.json'
        for tick in range(128):
            if accounting.prepare_quota_release(gate, terminal):
                break
        else:
            pytest.fail('failed no-audit quota release did not converge')
        prepared = json.loads(path.read_text())
        receipt = prepared['quota_completion_receipts'][binding]
        assert receipt['source'] == 'conservative' and receipt['day'] == finished_day.isoformat()
        assert binding not in prepared['quota_pending_completions']
        assert prepared['count'] == 0 and prepared['accounted_records'] == []
        released = admission.release_reservation_if_terminal(gate, claim.request_id)
        assert released['released'] is True, released
        after = gate.occupied_snapshot()
        assert after['total'] == occupied['total'] - 1
        assert after['per_owner'].get(owner_digest(owner), 0) == 0
        reservation = admission._read_reservation(admission.reservation_file(gate.paths, claim.request_id))
        assert reservation['state'] == 'released'
        settled = json.loads(path.read_text())
        assert binding not in settled['quota_completion_receipts']
        usage_path = (path.parent / 'curator_quota_history' / (receipt['day'] + '.json')
                      if late_release else path)
        usage = json.loads(usage_path.read_text())
        assert usage['count'] == 1
        assert usage['accounted_records'] == [_expected_conservative_identity(owner, run_id, binding)]
        assert settled['count'] == (0 if late_release else 1)
        assert admission.release_reservation_if_terminal(gate, claim.request_id)['reason'] == 'already_released'
        assert json.loads(usage_path.read_text()) == usage
        assert gate.occupied_snapshot() == after
        print({'late_release': late_release, 'prepare_rounds': tick + 1,
               'occupied_before': occupied['total'], 'occupied_after': after['total'],
               'owner_usage': usage['count']})
    assert len(os.listdir(descriptors)) - before_fds == 0
