"""GPT-P1-04 / W7-I08：真实注册的 scripts/workflows 在 exit=0 且产物核验后经原宿主持久投递受理。

Agent、self-map 注册、ScriptRunner、产物登记、DeliveryService 与 GatewayReplyDeliveryStore 都是生产实现，
不注入 script_delivery_handler；只把飞书 HTTP 上传/发消息替换为进程内 I/O 替身，并禁止一切真实网络。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.self_map_bootstrap import _script_delivery_key
from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.channel_message_target import resolve_message_target
from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.workflows.script_runner import ScriptRunner

_EMPTY = {'pending': [], 'sent': []}


# LLM: 投递测试只允许进程内替身，任何真实 socket 连接都直接判失败。
# 函数用途: 为本文件全部用例禁用网络连接。
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    # LLM: 只拦截连接入口，不改变其它 socket 行为。
    # 函数用途: 把意外网络访问变成显式测试失败。
    def forbidden(*_args, **_kwargs):
        pytest.fail('脚本投递测试禁止真实网络')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


# LLM: 只替换飞书 HTTP 边界；组件回执、uuid 和去重仍由生产 FeishuAdapter/DeliveryService 生成。
# 类用途: 记录每次提供方上传与发消息，并按 msg_type 返回预设的提供方响应。
class _FeishuIO:
    # LLM: 默认每条消息都被提供方确认；用例可按 msg_type 覆盖为未知或明确失败的响应。
    # 函数用途: 初始化空的发送、上传记录与响应表。
    def __init__(self):
        self.sends, self.uploads, self.responses = [], [], {}

    # LLM: 签名与 _feishu_msg_api_with_retry 一致；不重试、不访问网络。
    # 函数用途: 记录一次发消息并返回预设或成功的提供方响应。
    def api(self, _method, path, body, _token):
        self.sends.append((path, dict(body)))
        return self.responses.get(body.get('msg_type'), {'code': 0, 'data': {'message_id': f'om_{len(self.sends)}'}})

    # LLM: 签名与 _upload_feishu_bytes 一致；只记录生产代码交来的冻结字节。
    # 函数用途: 记录一次上传并返回合成 file_key。
    def upload(self, blob, name, *_args):
        self.uploads.append((name, blob))
        return 'file-key-' + name


# LLM: 真实 SimpleAgent 走原 self-map 注册；provider 为空时是本机管理员且没有任何渠道绑定。
# 函数用途: 构造隔离 home 下的 Agent，并给飞书注册只替换 HTTP I/O 的生产 adapter。
def _agent(tmp_path, monkeypatch, io, *, provider='feishu'):
    owner = {'my_agent_owner_provider': 'feishu', 'my_agent_owner_kind': 'user',
             'my_agent_owner_id': 'ou_owner'} if provider == 'feishu' else {}
    (tmp_path / 'project').mkdir(exist_ok=True)
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
                                    orphan_supervision_interval_seconds=0, **owner), tmp_path / 'project')
    if provider == 'feishu':
        adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
        adapter._get_tenant_access_token = lambda: 'synthetic-only'
        registry = agent.channel_registry
        registry.register_adapter('feishu', adapter, capabilities=registry.capabilities_for('feishu'))
        monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', io.api)
        monkeypatch.setattr(feishu, '_upload_feishu_bytes', io.upload)
    return agent


# LLM: 用例共享同一 owner/workspace/持久投递根；脚本与产物都经生产 Store/registry 写入。
# 函数用途: 准备已登记 ready 产物和一条合法投递意向的版本化脚本。
@pytest.fixture
def case(tmp_path, monkeypatch):
    io = _FeishuIO()
    agent = _agent(tmp_path, monkeypatch, io)
    owner = Path(agent.home_paths.owner_home_dir)
    case = SimpleNamespace(agent=agent, io=io, owner=owner, workspace=Path(agent.effective_workspace_root),
                           store_root=owner / 'data' / 'channel-delivery')
    _artifact(case, 'report-1', 'verified weekly report')
    _script(case, [_intent()])
    return case


# LLM: 意向只含脚本可声明的字段；目标、run 与 owner 均不能由意向提供。
# 函数用途: 构造一条投递意向。
def _intent(message='周报已生成', refs=('report-1',), key='weekly'):
    return {'request_key': key, 'message': message, 'output_refs': list(refs)}


# LLM: 产物经原 registry 登记为 ready 并记录 hash/size，投递只承运这份登记事实。
# 函数用途: 在当前工作区写入并登记一个产物文件。
def _artifact(case, artifact_id, text):
    path = case.workspace / 'out' / f'{artifact_id}.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    register_artifact(ArtifactRegistration(case.workspace, path, artifact_id=artifact_id, run_id='run-1', status='ready'))


# LLM: 输出 script-output.v1 信封；result 必须满足 io_contract，intents 由生产解析器校验。
# 函数用途: 经原 ScriptStore 保存或更新一个版本化输出脚本。
def _script(case, intents, *, result=None, exit_code=0, protocol='script-output.v1', script_id='weekly'):
    envelope = {'protocol_version': protocol, 'result': result or {'report': 'report-1'}, 'delivery_intents': intents}
    code = f'import json, sys\nprint(json.dumps({envelope!r}))\nsys.exit({exit_code})\n'
    ScriptStore(case.owner, workspace_root=case.workspace).save_script(
        AssetDescriptor(script_id, 'script', script_id, io_contract=IOContract(outputs=('report',))), code)


# LLM: 只模拟宿主已绑定的活动 run；attempt/request 可变以证明它们不进入稳定键。
# 函数用途: 设置当前活动运行参数。
def _activate(case, *, run_id='run-1', task_id='task-1', attempt_id='attempt-1', request_id='request-1'):
    case.agent._current_run_params = RunParams(request_id=request_id, run_id=run_id, task_id=task_id,
                                               attempt_id=attempt_id, task_attributes={})


# LLM: 每次调用都是一次新的真实 scripts 工具调用，runner 与投递端口由注册时的 provider 当场冻结。
# 函数用途: 在指定活动 run 下执行已登记脚本并返回工具结果和解析后的输出。
def _run(case, script_id='weekly', **run):
    _activate(case, **run)
    outcome = case.agent.tools.tools['scripts'].execute({'action': 'run', 'script_id': script_id})
    return outcome, json.loads(outcome.output)


# LLM: 直接数原持久投递根的 pending/sent 文件，不实例化 store 以免顺带创建目录。
# 函数用途: 返回当前 owner 持久投递账的记录文件清单。
def _durable_records(case):
    return {name: sorted(path.name for path in (case.store_root / name).glob('*.json')) for name in ('pending', 'sent')}


# LLM: 期望值来自可信 owner 与原逻辑 run/task/request_key，不含 attempt、tool call、时间、正文或目标。
# 函数用途: 计算一条意向应命中的原 host-delivery 操作引用。
def _operation_ref(case, *, run_id='run-1', task_id='task-1', request_key='weekly'):
    return 'host-delivery:' + _script_delivery_key(case.agent.home_paths.owner_id, run_id, task_id, request_key)


# LLM: 只在原 _admit_delivery 之前插入宿主状态变化，模拟脚本 exit=0 之后、受理之前发生的切换。
# 函数用途: 包装 ScriptRunner._admit_delivery，先执行一次给定动作。
def _before_admission(monkeypatch, action):
    original = ScriptRunner._admit_delivery

    # LLM: 保持原签名并原样委托，校验与受理逻辑不变。
    # 函数用途: 执行宿主状态变化后进入原受理。
    def admit(self, intents, output, metadata, context, *, receipts=None):
        action()
        return original(self, intents, output, metadata, context, receipts=receipts)

    monkeypatch.setattr(ScriptRunner, '_admit_delivery', admit)


# LLM: 真实注册路径、无注入端口：合法意向必须落原持久操作并以真实回执报告 sent。
# 函数用途: 验证 exit=0 后创建一条原 host-delivery 持久记录并返回同一 operation_ref。
def test_registered_script_exit_zero_admits_one_original_durable_operation(case):
    assert getattr(case.agent, 'script_delivery_handler', None) is None
    outcome, data = _run(case)
    assert outcome.ok, data
    ref = _operation_ref(case)
    assert ref.startswith('host-delivery:script-delivery.v1:')
    assert data['delivery_receipts'] == [{'operation_ref': ref, 'status': 'sent'}]
    records = _durable_records(case)
    assert records['pending'] == [] and len(records['sent']) == 1
    terminal = GatewayReplyDeliveryStore(case.store_root).terminal_receipt(ref)
    assert terminal['disposition'] == 'sent'
    assert (terminal['channel'], terminal['target']) == ('feishu', 'ou_owner')
    assert [body['msg_type'] for _path, body in case.io.sends] == ['text', 'file']
    assert [name for name, _blob in case.io.uploads] == ['report-1.txt']


# LLM: 新 attempt、新 request 与新工具调用都不改变稳定键，原操作已终态时不能再调用提供方。
# 函数用途: 验证同一 (owner, run, task, request_key) 重入复用原持久操作且零重发。
def test_same_key_from_new_attempt_and_tool_call_reuses_operation_without_resend(case):
    first, first_data = _run(case)
    sends, uploads = list(case.io.sends), list(case.io.uploads)
    second, second_data = _run(case, attempt_id='attempt-2', request_id='request-2')
    assert first.ok and second.ok, second_data
    expected = [{'operation_ref': _operation_ref(case), 'status': 'sent'}]
    assert first_data['delivery_receipts'] == second_data['delivery_receipts'] == expected
    assert case.io.sends == sends and case.io.uploads == uploads
    assert _durable_records(case)['pending'] == [] and len(_durable_records(case)['sent']) == 1


@pytest.mark.parametrize('prior', ['sent', 'pending'])
@pytest.mark.parametrize('change', ['message', 'attachment', 'target'])
# LLM: 同键而正文/附件/目标改变只返回原操作引用，不升格为 sent，也不新增/改写持久记录或调用提供方；
#   原操作可能已 sent 终态，也可能仍是待续收的 pending。
# 函数用途: 验证同一业务键的内容或目标漂移不会产生第二个投递操作。
def test_same_key_with_changed_payload_or_target_creates_no_second_operation(case, prior, change):
    if prior == 'pending':
        case.io.responses.update({'text': {}, 'post': {}})
    _run(case)
    case.io.responses.clear()
    pending_bytes = sorted(path.read_bytes() for path in (case.store_root / 'pending').glob('*.json'))
    assert len(pending_bytes) == (prior == 'pending')
    before, sends, uploads = _durable_records(case), list(case.io.sends), list(case.io.uploads)
    if change == 'message':
        _script(case, [_intent(message='周报已更新')])
    elif change == 'attachment':
        _artifact(case, 'report-2', 'another verified report')
        _script(case, [_intent(refs=('report-2',))], result={'report': 'report-2'})
    else:
        case.agent.config.my_agent_owner_id = 'ou_rebound'
    outcome, data = _run(case, attempt_id='attempt-2')
    assert outcome.ok, data
    assert data['delivery_receipts'] == [{'operation_ref': _operation_ref(case), 'status': 'unknown',
                                          'error_code': 'SCRIPT_DELIVERY_REQUEST_INVALID'}]
    assert case.io.sends == sends and case.io.uploads == uploads
    assert _durable_records(case) == before
    assert sorted(path.read_bytes() for path in (case.store_root / 'pending').glob('*.json')) == pending_bytes


@pytest.mark.parametrize('switch', ['active_run', 'active_attempt'])
# LLM: runner 冻结的 run/attempt 才是权威；受理前活动运行已切换必须拒绝，不能借旧 runner 投递。
# 函数用途: 验证活动运行与冻结上下文不一致时零发送、零持久记录。
def test_active_run_switched_after_runner_creation_refuses_admission(case, monkeypatch, switch):
    changed = {'run_id': 'run-2'} if switch == 'active_run' else {'attempt_id': 'attempt-9'}
    _before_admission(monkeypatch, lambda: _activate(case, **changed))
    outcome, data = _run(case)
    assert not outcome.ok and data['error_code'] == 'SCRIPT_CONFIGURATION_ERROR'
    assert 'SCRIPT_DELIVERY_REQUEST_INVALID' in data['stderr_preview']
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


@pytest.mark.parametrize('forged', [{'run_id': 'run-forged'}, {'task_id': 'task-forged'}])
# LLM: 端口取自真实注册的 runner_options_provider；回调实参不能把投递改记到其它 run/task。
# 函数用途: 验证回调 run/task 与冻结上下文不符时拒绝受理。
def test_callback_run_or_task_mismatch_is_refused(case, forged):
    _activate(case)
    handler = case.agent.tools.tools['scripts'].runner_options_provider()['delivery_handler']
    arguments = {'run_id': 'run-1', 'task_id': 'task-1', **forged}
    with pytest.raises(ValueError, match='SCRIPT_DELIVERY_REQUEST_INVALID'):
        handler(**arguments, request_key='weekly', message='forged', attachments=())
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


# LLM: 目标在 runner 创建时冻结；受理前 owner 绑定被改到其它收件人时，同 owner 授权门必须拒绝。
# 函数用途: 验证目标漂移不会被投递到新旧任一收件人。
def test_target_binding_changed_after_runner_creation_refuses_admission(case, monkeypatch):
    _before_admission(monkeypatch, lambda: setattr(case.agent.config, 'my_agent_owner_id', 'ou_rebound'))
    outcome, data = _run(case)
    assert not outcome.ok and 'CHANNEL_TARGET_OUT_OF_SCOPE' in data['stderr_preview']
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


@pytest.mark.parametrize('stop', ['cancelled', 'interrupted', 'interrupted_before_transport'])
# LLM: 取消/中断在 exit=0 之后才到达；入口与持久受理/运输前两道检查都必须让投递零发生。
# 函数用途: 验证已取消或已中断的运行不创建持久记录也不调用提供方。
def test_cancelled_or_interrupted_run_admits_nothing(case, monkeypatch, stop):
    from agent_py_agent.agent.concurrency import interrupt
    from agent_py_agent.agent.scheduler import effect_scope

    token, checks, entered = CancellationToken(), [], []
    original_scope = effect_scope.scheduler_effect_scope

    # LLM: 只记录是否进入原 scheduler 事件资格作用域，再委托原实现。
    # 函数用途: 证明入口检查发生在取得任何 scheduler/持久受理资格之前。
    def scope(agent, task_id, operation_key, *, kind, allow_terminal=False):
        entered.append((task_id, operation_key, kind))
        return original_scope(agent, task_id, operation_key, kind=kind, allow_terminal=allow_terminal)

    monkeypatch.setattr(effect_scope, 'scheduler_effect_scope', scope)

    # LLM: 'interrupted_before_transport' 让入口检查通过，只在持久受理与运输前的第二次检查命中。
    # 函数用途: 按用例返回当前调用的中断事实并计数。
    def interrupted():
        checks.append(True)
        return stop == 'interrupted' or len(checks) > 1

    if stop == 'cancelled':
        _before_admission(monkeypatch, token.cancel)
    else:
        _before_admission(monkeypatch, lambda: monkeypatch.setattr(interrupt, 'is_interrupted', interrupted))
    with bind_cancellation_token(token):
        outcome, _data = _run(case)
    assert not outcome.ok
    if stop == 'interrupted_before_transport':
        assert len(checks) == 2
        assert entered == [('task-1', _operation_ref(case).removeprefix('host-delivery:'), 'delivery')]
    else:
        assert entered == []
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


# LLM: 委派子任务不得抢占原主会话的通知目标；委派身份只经原委派链读取。
# 函数用途: 验证 delegated-origin 阻断在持久受理前生效。
def test_delegated_origin_target_block_admits_nothing(case, monkeypatch):
    from agent_py_agent.agent.conversation import im_delegation_delivery

    monkeypatch.setattr(im_delegation_delivery, 'current_delegated_delivery_task', lambda _agent: SimpleNamespace(task_id='delegated'))
    monkeypatch.setattr(im_delegation_delivery, 'delegated_origin_target', lambda _agent, _task: ('feishu', 'ou_owner'))
    outcome, data = _run(case)
    assert not outcome.ok and 'CHANNEL_TARGET_OUT_OF_SCOPE' in data['stderr_preview']
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


@pytest.mark.parametrize('scope', ['missing_root', 'retired_event_run'])
# LLM: 定时根不可核验或事件运行已终态时，原 scheduler 事件资格以冻结 task 与稳定键拒绝，allow_terminal 保持 False。
# 函数用途: 验证 scheduler 不合格/终态作用域零发送、零持久记录。
def test_ineligible_or_terminal_scheduler_scope_admits_nothing(case, monkeypatch, scope):
    repository, begun = case.agent.scheduler_repository, []
    if scope == 'retired_event_run':
        original = repository.begin_event_effect

        # LLM: 记录原 begin 的实参后原样委托真实实现，由它按 allow_terminal=False 拒绝终态运行。
        # 函数用途: 观察事件副作用准入的 task/键/kind。
        def begin(candidate, operation_key, *, kind, allow_terminal=False):
            begun.append((operation_key, kind, allow_terminal))
            return original(candidate, operation_key, kind=kind, allow_terminal=allow_terminal)

        monkeypatch.setattr(repository, 'effect_run', lambda run_id: {
            'run_id': run_id, 'status': 'done', 'event_source': {'generation': 1}})
        monkeypatch.setattr(repository, 'begin_event_effect', begin)
    outcome, data = _run(case, run_id='srun_synthetic', task_id='srun_synthetic')
    code = 'SCHEDULER_EFFECT_SOURCE_UNAVAILABLE' if scope == 'missing_root' else 'SCHEDULER_EVENT_EFFECT_RETIRED'
    assert not outcome.ok and code in data['stderr_preview']
    if scope == 'retired_event_run':
        key = _script_delivery_key(case.agent.home_paths.owner_id, 'srun_synthetic', 'srun_synthetic', 'weekly')
        assert begun == [(key, 'delivery', False)]
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


# LLM: 原请求执行准入以异常表达拒绝；合法本地执行返回 None 已由其它用例覆盖为可投递。
# 函数用途: 验证执行准入拒绝时零发送、零持久记录。
def test_request_execution_refusal_admits_nothing(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_entry_admission
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError

    # LLM: 模拟原准入对当前执行上下文的拒绝，不伪造任何许可。
    # 函数用途: 以原拒绝类型抛出。
    def refuse(**_kwargs):
        raise WorkerDispatchError('execution_context_required', 'business_entry')

    _before_admission(monkeypatch, lambda: monkeypatch.setattr(request_entry_admission, 'require_request_execution', refuse))
    outcome, _data = _run(case)
    assert not outcome.ok
    assert case.io.sends == [] and _durable_records(case) == _EMPTY


@pytest.mark.parametrize('variant', ['nonzero_exit', 'unregistered_artifact', 'undeclared_ref', 'bad_protocol', 'bad_intent'])
# LLM: 非零退出或任何输出/产物校验失败都发生在受理之前，必须零持久记录、零上传和零发送。
# 函数用途: 验证无效脚本结果不进入宿主投递。
def test_nonzero_exit_or_invalid_output_admits_nothing(case, variant):
    if variant == 'nonzero_exit':
        _script(case, [_intent()], exit_code=3)
    elif variant == 'unregistered_artifact':
        _script(case, [_intent(refs=('ghost',))], result={'report': 'ghost'})
    elif variant == 'undeclared_ref':
        _script(case, [_intent(refs=('report-1',))], result={'report': 'another-output'})
    elif variant == 'bad_protocol':
        _script(case, [_intent()], protocol='script-output.v0')
    else:
        _script(case, [{'request_key': ' ', 'message': 'blank key'}])
    outcome, _data = _run(case)
    assert not outcome.ok
    assert case.io.sends == [] and case.io.uploads == [] and _durable_records(case) == _EMPTY


@pytest.mark.parametrize('responses,expected', [
    ({'text': {}, 'post': {}}, 'unknown'),
    ({'file': {}}, 'partial'),
    ({'text': {'code': 230002}, 'post': {'code': 230002}}, 'failed'),
])
# LLM: 提供方未确认的组件只能是 unknown/partial，明确拒绝且全未开始才是 failed；都不能升格为 sent。
# 函数用途: 验证组件事实原样折叠进脚本回执，且原持久记录不是 sent 终态。
def test_provider_unknown_partial_and_failed_outcomes_are_never_upgraded(case, responses, expected):
    case.io.responses.update(responses)
    _outcome, data = _run(case)
    ref = _operation_ref(case)
    assert data['delivery_receipts'][0]['operation_ref'] == ref
    assert data['delivery_receipts'][0]['status'] == expected
    store = GatewayReplyDeliveryStore(case.store_root)
    terminal = store.terminal_receipt(ref)
    assert terminal is None or terminal.get('disposition') != 'sent'
    assert terminal is not None or any(row.stable_id == ref for row in store.pending())


# LLM: 原持久记录已受理但另一执行者仍持有投递 claim 时只能报 accepted；之后同键重入沿原记录续发一次。
# 函数用途: 验证 accepted 不被当作 sent，且由原操作续收而不是新建操作。
def test_admitted_but_not_dispatched_operation_reports_accepted_then_continues(case, monkeypatch):
    original_claim = GatewayReplyDeliveryStore.claim
    monkeypatch.setattr(GatewayReplyDeliveryStore, 'claim', lambda self, observed, **_kwargs: None)
    _outcome, data = _run(case)
    ref = _operation_ref(case)
    assert data['delivery_receipts'] == [{'operation_ref': ref, 'status': 'accepted'}]
    assert case.io.sends == [] and len(_durable_records(case)['pending']) == 1
    monkeypatch.setattr(GatewayReplyDeliveryStore, 'claim', original_claim)
    _outcome, data = _run(case, attempt_id='attempt-2')
    assert data['delivery_receipts'] == [{'operation_ref': ref, 'status': 'sent'}]
    assert [body['msg_type'] for _path, body in case.io.sends] == ['text', 'file']
    assert _durable_records(case)['pending'] == [] and len(_durable_records(case)['sent']) == 1


# LLM: 渠道 adapter 暂不可用(如凭据缺失)必须在持久受理前拒绝，否则 unknown 终态会永久挡住同键重试。
# 函数用途: 验证不可用时零持久记录，恢复后同一键仍能正常投递一次。
def test_unavailable_channel_is_refused_before_admission_and_key_stays_usable(case):
    registry = case.agent.channel_registry
    adapter, capabilities = registry.adapter_for('feishu'), registry.capabilities_for('feishu')
    registry.register_factory('feishu', lambda: None, capabilities=capabilities)
    outcome, data = _run(case)
    assert not outcome.ok and 'CHANNEL_ADAPTER_UNAVAILABLE' in data['stderr_preview']
    assert case.io.sends == [] and _durable_records(case) == _EMPTY
    registry.register_adapter('feishu', adapter, capabilities=capabilities)
    outcome, data = _run(case, attempt_id='attempt-2')
    assert outcome.ok and data['delivery_receipts'] == [{'operation_ref': _operation_ref(case), 'status': 'sent'}]
    assert len(_durable_records(case)['sent']) == 1


# LLM: 本机管理员未启用渠道身份时没有任何投递目标；不含意向的脚本照常成功，只有真正受理意向时才失败。
# 函数用途: 验证无渠道本地脚本不因投递端口预先失败，且有意向时也零持久记录。
def test_local_script_without_intents_runs_without_channel_target(tmp_path, monkeypatch):
    io = _FeishuIO()
    agent = _agent(tmp_path, monkeypatch, io, provider='')
    owner, workspace = Path(agent.home_paths.owner_home_dir), Path(agent.effective_workspace_root)
    store = ScriptStore(owner, workspace_root=workspace)
    empty = {'protocol_version': 'script-output.v1', 'result': {}, 'delivery_intents': []}
    store.save_script(AssetDescriptor('plain', 'script', 'plain'), 'print(\'{"rows": 3}\')')
    store.save_script(AssetDescriptor('versioned', 'script', 'versioned'), f'import json\nprint(json.dumps({empty!r}))\n')
    tool = agent.tools.tools['scripts']
    for params in (None, RunParams(request_id='request-1', run_id='run-1', task_id='task-1', task_attributes={})):
        agent._current_run_params = params
        for script_id in ('plain', 'versioned'):
            outcome = tool.execute({'action': 'run', 'script_id': script_id})
            assert outcome.ok, outcome.output
    intent = {'protocol_version': 'script-output.v1', 'result': {},
              'delivery_intents': [{'request_key': 'note', 'message': '本地提醒'}]}
    store.save_script(AssetDescriptor('notify', 'script', 'notify'), f'import json\nprint(json.dumps({intent!r}))\n')
    outcome = tool.execute({'action': 'run', 'script_id': 'notify'})
    target_error = resolve_message_target(agent, '')[3]
    assert target_error and not outcome.ok and target_error in json.loads(outcome.output)['stderr_preview']
    assert io.sends == [] and not list((owner / 'data' / 'channel-delivery').glob('*/*.json'))


# LLM: 工作流脚本步骤经同一注册 provider 取得投递端口；同一宿主 run 内再次运行工作流复用原操作。
# 函数用途: 验证 WorkflowsTool 真实注册路径同样落原持久操作且不重发。
def test_registered_workflow_script_step_uses_original_host_delivery(case):
    tool = case.agent.tools.tools['workflows']
    definition = {'descriptor': AssetDescriptor('weekly-flow', 'workflow', 'weekly-flow', io_contract=IOContract()).to_dict(),
                  'steps': [{'step_id': 'deliver', 'name': 'deliver', 'mode': 'deterministic_script',
                             'script_id': 'weekly', 'on_error': 'fail'}]}
    assert tool.execute({'action': 'save', 'workflow_id': 'weekly-flow', 'definition': definition}).ok
    _activate(case)
    first = tool.execute({'action': 'run', 'workflow_id': 'weekly-flow'})
    assert first.ok and json.loads(first.output)['status'] == 'succeeded', first.output
    terminal = GatewayReplyDeliveryStore(case.store_root).terminal_receipt(_operation_ref(case))
    assert terminal['disposition'] == 'sent'
    sends = list(case.io.sends)
    assert [body['msg_type'] for _path, body in sends] == ['text', 'file']
    _activate(case, attempt_id='attempt-2')
    again = tool.execute({'action': 'run', 'workflow_id': 'weekly-flow'})
    assert again.ok, again.output
    assert case.io.sends == sends and len(_durable_records(case)['sent']) == 1


# LLM: 宿主显式注入的端口仍是覆盖入口；它只能来自宿主属性，不来自脚本或工具实参。
# 函数用途: 验证 agent.script_delivery_handler 可调用时 provider 原样交给 runner。
def test_explicit_host_delivery_handler_override_is_kept(case):
    # LLM: 只作身份比较的哨兵，不被调用。
    # 函数用途: 充当宿主显式注入的投递端口。
    def override(*, run_id, task_id, request_key, message, attachments):
        return {'operation_ref': 'host-supplied', 'status': 'accepted'}

    case.agent.script_delivery_handler = override
    assert case.agent.tools.tools['scripts'].runner_options_provider()['delivery_handler'] is override
