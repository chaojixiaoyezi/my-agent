# LLM: 验证后台任务首拍未发布transport时的G0输入入队、同key控制竞争、单预算快速失败与pending重放恢复合同。
# 模块用途: 覆盖普通BG首拍与同key控制去向的真实临时C/Store/HTTP/claim端到端回归测试。
"""普通BG首拍与同key控制去向；真实临时C/Store/HTTP/claim，模型边界fake，不改存储记录造场景。"""
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import background_claim as claims
from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.gateway_parts import control_service as control
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import multiprocess_input_receipt as producer
from agent_py_agent.agent.gateway_parts.input_guidance_payload import input_scope
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    reservation_file,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    enqueue_source,
    freeze_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_background_claim_transport_reference import gateway_scope
from agent_py_agent.tests.test_background_execution_ports import claimed
from agent_py_agent.tests.test_gateway_active_input_publication import held_lock
from agent_py_agent.tests.test_gateway_background_active_input import model_runner
from agent_py_agent.tests.test_gateway_c_http_wait import c_http, request


# LLM: 原wake公共API→freeze/enqueue→P3/admit/claim，真实Agent/Store/C与HTTP pool不替换；只创建pytest临时普通对象。
# 函数用途: 在BG claim尚未core绑定的准确首拍发送正常本地主用户输入。
@contextmanager
def bg_first_call(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, _close):
        store = agent.conversation_store
        thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
            'channel_conversation_id': 'bg-fresh', 'channel_user_id': 'local-agent'})
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'ports-task', 'status': 'active'})
        wake = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': 'ordinary-test'})
        ref = freeze_source(agent.home_paths.root, OwnerIdentity.local_main(), store.storage.wake_signal_path(wake), 'wake')
        path, _ = enqueue_source(gate.paths, agent.home_paths.root, ref)
        payload = read_json_file(path)
        decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(payload)), AdmissionLimits(8, 8))
        assert decision['status'] == 'admitted'
        gateway_claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
        yield (gate, gateway_claim, agent, store, thread, None), server, backend


# LLM: 构造带固定message_id与会话标识的普通/ask HTTP请求，供首拍入队与同请求重放复用。
# 函数用途: 向测试HTTP网关发送普通用户的首条后台会话输入请求。
def send_first(server, *, message='fresh-bg-message'):
    return request(server, 'POST', '/ask', {'goal': '补充完整的普通说明', 'conversation_id': 'bg-fresh',
        'metadata': {'message_id': message}}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})


# LLM: 仍从原active task resolver选择合法当前任务；调用完整原steer门，结果不能被当作GuidanceEntry处理。
# 函数用途: 模拟已选择当前任务的同一普通输入控制稍晚到达，不直接调用append叶子代算锁链。
def late_control(case, receipt, *, scope=None, message=None):
    gate, _claim, agent, _store, _thread, _ = case
    scope = scope or input_scope(receipt)
    active = control._active_conversation_task(agent, scope, task_id='ports-task')
    assert active is not None
    return control._steer_active_request(agent, gate.paths, active,
        ConversationControlCommand('steer', message if message is not None else receipt.prepared_request['goal']), scope)


# LLM: 原run_with_heartbeat持真实claim binding；首拍动作在原core/binder前，随后仍运行原模型attempt/末结算，不直调binder代替链路。
# 函数用途: 把输入竞态放在真实BG首次执行片内，fake仅模型响应。
def run_first(case, tmp_path, monkeypatch, before):
    _gate, _claim, agent, store, thread, _ = case
    params = model_runner(case, tmp_path, monkeypatch, lambda bound: 'ordinary-fake-model')
    # LLM: 在真实后台attempt执行前先触发调用方注入的输入与控制竞争断言回调。
    # 函数用途: 包装单次后台模型尝试并在进入模型前执行测试前置钩子。
    def once(_):
        before()
        return execution._run_background_model_attempt(agent, '继续普通后台任务', params)[0]
    deps, args, row, _policy = claimed(case, once)
    store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': row['claim_scope_id'],
        'claim_id': row['claim_id'], 'lease_seconds': 30})
    with gateway_scope('C', case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'ordinary-fake-model'


# LLM: 验证未发布transport时首条输入进入queued且全部锁释放后迟到控制与重放均保持queued幂等。
# 函数用途: 测试BG首拍未发布状态下正常入队、迟到同key控制收敛与同请求重放。
def test_fresh_unpublished_queue_then_late_control_after_all_locks_and_replay(tmp_path, monkeypatch):
    with bg_first_call(tmp_path, monkeypatch) as (case, server, backend):
        gate, gateway_claim, _agent, store, thread, _ = case
        p3 = reservation_file(gate.paths, gateway_claim.request_id).read_bytes()
        # LLM: 在BG首拍内依次执行首次入队、迟到控制和重放断言。
        # 函数用途: 验证首次请求入队后释放全部锁且后续迟到控制与重放不破坏回执。
        def first():
            current = store.claims.load(thread.thread_id)
            assert 'gateway_transport_reference' not in current and 'runtime_authority' not in current
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'queued', result
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert receipt.transport is None and read_json_file(gate.paths.inbox / (receipt.request_id + '.json')) == receipt.prepared_request
            before = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
            # HTTP已返回，G0所有input/claim/task/thread/M/Root锁均已释放。
            late = late_control(case, receipt)
            assert late.ok and late.delivery_status == 'accepted'
            assert late.details == {'input_request_id': receipt.request_id, 'input_state': 'queued', 'active_appended': False}
            assert store.guidance.receipt(receipt.guidance_dedupe_key) is None
            assert send_first(server)[1]['input_state'] == 'queued'
            assert inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes() != b''
            assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).prepared_request == receipt.prepared_request
            assert p3 == reservation_file(gate.paths, gateway_claim.request_id).read_bytes() and backend.calls == 0
            assert before
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证同key控制先写入guidance回执时G0新输入自动收敛为pending且不写inbox。
# 函数用途: 测试控制在route_fresh前先落盘时输入回执保持pending延迟态。
def test_control_append_first_makes_fresh_g0_deferred(tmp_path, monkeypatch):
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _gateway_claim, _agent, store, _thread, _ = case
        original = producer.CoordinatorInputReceiptSource.route_fresh
        observed = []
        # LLM: 在进入原route_fresh前先执行迟到控制写入并记录guidance_id。
        # 函数用途: 模拟控制通道抢先写入同dedupe key的guidance记录。
        def route(source, expected, scope, **options):
            late = late_control(case, expected)
            assert late.ok and late.delivery_status == 'unknown', late
            observed.append(store.guidance.receipt(expected.guidance_dedupe_key).entry.guidance_id)
            return original(source, expected, scope, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', route)
        # LLM: 触发首拍请求并核验返回pending且未生成inbox文件。
        # 函数用途: 断言控制在先时首拍请求保持pending且无残留队列文件。
        def first():
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'pending', result
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert receipt.transport is None and not (gate.paths.inbox / (receipt.request_id + '.json')).exists()
            assert len(observed) == 1
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证input/dedupe/canonical_json任一锁忙时迟到控制立即返回BlockingIOError而不阻塞等待或半写。
# 函数用途: 参数化测试三类底层资源锁被占用时迟到控制的非阻塞失败边界。
@pytest.mark.parametrize('resource', ['input', 'dedupe', 'canonical_json'])
def test_late_control_resource_busy_never_waits_or_appends(tmp_path, monkeypatch, resource):
    from agent_py_agent.agent.gateway_parts.io import (
        _locked_json_path,
        gateway_turn_transition,
        locked_file_transition,
    )
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _gateway_claim, _agent, store, _thread, _ = case
        original = producer.CoordinatorInputReceiptSource.route_fresh
        # LLM: 持有指定资源锁期间调用late_control并断言快速失败且无脏落盘。
        # 函数用途: 在route_fresh切面内构造指定资源忙锁并校验late_control立即返回。
        def route(source, expected, scope, **options):
            canonical = store.storage.guidance_dedupe_path(expected.guidance_dedupe_key)
            lock = (gateway_turn_transition(gate.paths, expected.request_id) if resource == 'input'
                else locked_file_transition(canonical.with_name('.' + canonical.name + '.transition')) if resource == 'dedupe'
                else _locked_json_path(canonical))
            with held_lock(lock):
                started = time.monotonic()
                result = late_control(case, expected)
                assert time.monotonic() - started < 1
                assert not result.ok and result.error_code == 'BlockingIOError', result
                assert not canonical.exists() and not (gate.paths.inbox / (expected.request_id + '.json')).exists()
            return original(source, expected, scope, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', route)
        # LLM: 发起首次请求并确认锁释放后原route_fresh仍正常完成queued。
        # 函数用途: 执行首拍输入并断言最终入队状态。
        def first():
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'queued', result
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证owner/digest/goal任一要素不匹配的普通控制不会误写入guidance或篡改已queued回执。
# 函数用途: 参数化测试非同源或非同内容控制命令对已入队请求的隔离性。
@pytest.mark.parametrize('field', ['owner', 'digest', 'goal'])
def test_nonmatching_ordinary_control_does_not_append_or_change_queue(tmp_path, monkeypatch, field):
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _gateway_claim, _agent, store, _thread, _ = case
        # LLM: 先将首拍输入入队，再构造指定维度不匹配的控制调用并断言拒绝。
        # 函数用途: 校验不匹配控制返回unknown且原queued回执完好。
        def first():
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'queued'
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            scope = input_scope(receipt)
            if field == 'owner':
                # 合法另一个owner作为调用参数；不改任何canonical或权限来源。
                scope = replace(scope, resolved_owner=OwnerIdentity.provider_user('chat', 'another'))
                # 从原匹配的store直接进已持原任务门所调用的叶子，仅验证不同完整消息不能append。
                from agent_py_agent.agent.gateway_parts.control_guidance_payload import (
                    control_guidance_payload,
                )
                candidate = control_guidance_payload('task', 'ports-task', ConversationControlCommand('steer', receipt.prepared_request['goal']), scope, case[4].thread_id, 'ports-task')
                answer = control._append_ordinary_input_guidance(store, gate.paths, scope, candidate, 'ports-task', time.monotonic()+1)
            else:
                if field == 'digest':
                    scope = replace(scope, metadata=scope.metadata | {'client_input_digest': 'another-legitimate-input'})
                answer = late_control(case, receipt, scope=scope, message='另一条完整说明' if field == 'goal' else None)
            assert not answer.ok and answer.delivery_status == 'unknown'
            assert store.guidance.receipt(receipt.guidance_dedupe_key) is None
            assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'queued'
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证observe_input_target观察到unpublished后若claim心跳续租变更了原始文件，G0保持pending不强行入队。
# 函数用途: 测试首拍观察期间合法心跳续租触发文件变更时的保守pending回退。
def test_unpublished_legal_heartbeat_raw_change_stays_deferred(tmp_path, monkeypatch):
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _claim, _agent, store, thread, _ = case
        observe = producer.observe_input_target
        seen = []
        # LLM: 在observe_input_target返回unpublished后立即执行合法claim续租以改变底层字节。
        # 函数用途: 模拟观察窗口与路由窗口之间的合法心跳续租写盘。
        def renewed(*args, **options):
            result = observe(*args, **options)
            assert result[0] == 'unpublished'
            row = store.claims.load(thread.thread_id)
            store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': row['claim_scope_id'],
                'claim_id': row['claim_id'], 'lease_seconds': 31})
            seen.append(True)
            return result
        monkeypatch.setattr(producer, 'observe_input_target', renewed)
        # LLM: 发送首拍请求并断言因claim字节变化保持pending且不写inbox。
        # 函数用途: 核验并发心跳更新下输入回执留在pending状态。
        def first():
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'pending' and seen == [True]
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert receipt.transport is None and not (gate.paths.inbox / (receipt.request_id + '.json')).exists()
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证dedupe transition锁忙时route_fresh只消费一次短deadline返回503，随后恢复正常预算重放pending回执收敛为queued。
# 函数用途: 测试G0首拍遇到dedupe锁竞争时的单次超时边界及后续pending重放恢复。
def test_g0_dedupe_busy_uses_one_deadline_then_pending_replay_recovers(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.request_scope import provider_request_budget
    from agent_py_agent.agent.gateway_parts.io import locked_file_transition
    original_deadline = producer.input_resource_deadline
    budget_calls = []
    # LLM: 仅在首次请求验证dedupe忙锁超时时注入0.12秒短预算并记录deadline读取次数。
    # 函数用途: 为首次锁竞争调用提供短超时上限以验证单deadline快速失败。
    def short_budget():
        budget_calls.append(True)
        with provider_request_budget(0.12):
            return original_deadline()
    monkeypatch.setattr(producer, 'input_resource_deadline', short_budget)
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _claim, _agent, store, _thread, _ = case
        route = producer.CoordinatorInputReceiptSource.route_fresh
        seen = []
        # LLM: 占用guidance_dedupe transition锁后调用原route_fresh以复现锁忙503。
        # 函数用途: 在首次route_fresh期间持有dedupe过渡锁制造资源竞争。
        def busy(source, expected, scope, **options):
            seen.append(expected.request_id)
            path = store.storage.guidance_dedupe_path(expected.guidance_dedupe_key)
            with held_lock(locked_file_transition(path.with_name('.' + path.name + '.transition'))):
                return route(source, expected, scope, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', busy)
        # LLM: 先断言首次请求单deadline快速返回503且保留pending，再恢复正常预算验证重放收敛为queued。
        # 函数用途: 顺序验证首次dedupe忙锁快速失败与二次重放恢复入队。
        def first():
            started = time.monotonic()
            code, result = send_first(server)
            assert code == 503 and result['terminal'] is False and time.monotonic()-started < 1, result
            assert len(budget_calls) == 1
            assert result['error_code'] == 'EXECUTION_CONTEXT_REQUIRED'
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            before = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
            assert receipt.state == 'pending' and not (gate.paths.inbox / (receipt.request_id + '.json')).exists()
            monkeypatch.setattr(producer, 'input_resource_deadline', original_deadline)
            replay_code, replay = send_first(server)
            assert replay_code == 202 and replay['input_state'] == 'queued' and len(seen) == 1
            assert inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes() != before
            assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).prepared_request == receipt.prepared_request
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证迟到控制在provider预算已耗尽（0秒）时立即返回BlockingIOError且不写入guidance。
# 函数用途: 测试过期预算下late_control不追加guidance且不影响原请求入队。
def test_late_control_expired_provider_budget_never_appends(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.request_scope import provider_request_budget
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _claim, _agent, store, _thread, _ = case
        original = producer.CoordinatorInputReceiptSource.route_fresh
        # LLM: 在0秒预算内执行late_control并断言立即因BlockingIOError拒绝。
        # 函数用途: 在route_fresh前验证零预算迟到控制不产生任何guidance副作用。
        def route(source, expected, scope, **options):
            with provider_request_budget(0):
                result = late_control(case, expected)
            assert not result.ok and result.error_code == 'BlockingIOError', result
            assert store.guidance.receipt(expected.guidance_dedupe_key) is None
            return original(source, expected, scope, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', route)
        # LLM: 发送首拍请求并验证最终queued且task下无pending guidance。
        # 函数用途: 断言零预算控制失败后原首拍输入仍正常入队。
        def first():
            code, result = send_first(server)
            assert code == 202 and result['input_state'] == 'queued', result
            assert not store.guidance.pending('task', 'ports-task')
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证写最终queued回执JSON遇锁忙返回503后保留已写inbox，恢复正常锁与预算后同ID重放收敛为queued。
# 函数用途: 测试末步input_receipts写锁竞争时的半程inbox保留与同请求重放恢复。
def test_g0_final_receipt_json_busy_keeps_partial_inbox_and_same_id_replay(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.request_scope import provider_request_budget
    from agent_py_agent.agent.common.json_io import locked_json_path
    original_deadline = producer.input_resource_deadline
    # LLM: 为首次末步回执锁竞争设置0.1秒短预算以快速触发超时。
    # 函数用途: 注入短资源预算供首次写回执锁冲突测试使用。
    def short_budget():
        with provider_request_budget(0.1):
            return original_deadline()
    monkeypatch.setattr(producer, 'input_resource_deadline', short_budget)
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, _claim, _agent, store, _thread, _ = case
        writer = producer.write_json_file_atomic
        seen = []
        # LLM: 当写input_receipts且目标状态为queued时占用对应JSON锁制造写超时。
        # 函数用途: 拦截末步queued回执写入并模拟目标JSON文件锁被占用。
        def busy_final(path, payload, **options):
            if path.parent.name == 'input_receipts' and payload.get('state') == 'queued':
                seen.append(options['deadline'])
                with held_lock(locked_json_path(path)):
                    return writer(path, payload, **options)
            return writer(path, payload, **options)
        monkeypatch.setattr(producer, 'write_json_file_atomic', busy_final)
        # LLM: 验证首次请求503且保留inbox与pending回执，再恢复正常writer和预算验证同ID重放queued。
        # 函数用途: 顺序断言末步回执忙锁失败后的状态一致性与重放恢复。
        def first():
            code, result = send_first(server)
            assert code == 503 and result['terminal'] is False, result
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert receipt.state == 'pending' and receipt.transport is None and seen
            assert len(set(seen)) == 1
            assert read_json_file(gate.paths.inbox / (receipt.request_id+'.json')) == receipt.prepared_request
            assert store.guidance.receipt(receipt.guidance_dedupe_key) is None
            # 实际末JSON短忙已经结束；原prepare恢复仍created=False，复用已存在的完整inbox。
            monkeypatch.setattr(producer, 'write_json_file_atomic', writer)
            monkeypatch.setattr(producer, 'input_resource_deadline', original_deadline)
            code, replay = send_first(server)
            assert code == 202 and replay['input_state'] == 'queued'
        run_first(case, tmp_path, monkeypatch, first)


# LLM: 验证缺少message_id或Count0探测时_ordinary_input_control_deadline直接返回None且不触发任何输入回执磁盘读取。
# 函数用途: 测试无message_id控制探测的零I/O边界。
def test_count0_or_missing_message_id_has_no_new_input_io(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    paths = gateway_paths_from_root(tmp_path/'unused')
    scope = control.GatewayControlScope('local-agent', 'chat', 'plain',
        metadata={'gateway_input_request_id': 'ordinary-id'})
    # LLM: 一旦被调用即触发pytest.fail，用于证明Count0路径绝不读取磁盘回执。
    # 函数用途: 充当read_json_file_report的禁读哨兵。
    def forbidden(*_args, **_options):
        pytest.fail('ordinary Count0 role probe must not read input')
    monkeypatch.setattr(control, 'read_json_file_report', forbidden)
    assert control._ordinary_input_control_deadline(paths, scope) is None
    with bg_first_call(tmp_path/'positive', monkeypatch) as (case, _server, _backend):
        assert control._ordinary_input_control_deadline(case[0].paths,
            replace(scope, metadata={'gateway_input_request_id': 'ordinary-id'})) is None
