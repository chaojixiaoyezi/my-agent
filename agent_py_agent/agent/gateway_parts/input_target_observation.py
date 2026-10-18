# LLM: 所有目标来自原owner Store/会话claim与唯一processing，不创建Agent/owner/线程；idle只可证不存在，坏账/过期/中间态一律unknown。
# 模块用途: 给C固定输入运输读取真实前台目标，投影不授业务执行权。
from dataclasses import replace

from ..user_space.home_layout import home_paths
from ..user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
from . import input_delivery_service as inputs
from .execution_target import _claim_is_current, _foreground_target
from .io import read_json_file_report
from .multiprocess_route import validate_published_route
from .owner_conversation_store import owner_conversation_store_from_config
from .request_client import GatewayAskExecutionOptions
from .request_owner_binding import frozen_request_owner


# LLM: 配置只定位原canonical owner库，不授读其它owner/初始化许可；foreign清空原runtime resolver覆盖字段，initialize=False零业务创建。
# 函数用途: 打开已冻结owner的原会话库，缺少正式owner明确拒绝。
def input_owner_store(control, receipt):
    owner = frozen_request_owner(receipt.prepared_request)
    if owner is None:
        raise ValueError('input_original_owner_required')
    base = home_paths(control.home_root)
    scoped = home_paths_with_owner(base, resolve_owner_home(control.home_root, owner))
    config = control.config
    if owner != owner_identity_from_config(config):
        paths = resolve_runtime_paths_for_agent(config, base.owner_home_dir, base).paths
        config = replace(config, **{key: '' for key in paths if hasattr(config, key)})
    return owner_conversation_store_from_config(config, scoped, initialize=False)


# LLM: 显式默认可等价；新媒体/引用、files、inject、workspace、system_task不进入纯文字，当前非等价选项排完整prepared，原native验证留worker。
# 函数用途: 判定两份原请求能否沿同一前台文字安全点接续。
def active_text_options_match(prepared, current=None):
    options = GatewayAskExecutionOptions.from_payload(prepared)
    if (prepared.get('kind') != 'ask' or prepared.get('system_task') or prepared.get('workspace')
            or options.input_media or options.prompt_files or options.inject
            or (prepared.get('metadata') or {}).get('quoted_message_references') is not None):
        return False
    return current is None or options == GatewayAskExecutionOptions.from_payload(current)


# LLM: 非阻塞原JSON读取；锁忙/损坏不能变为空目标，原typed异常不捕获；返回原完整JSON作末CAS观察。
# 函数用途: 读取一个规范目标记录，保持明确存在与不可观察的区别。
def target_record(path):
    report = read_json_file_report(path, context='gateway.input.target.observe', blocking=False)
    inputs._raise_for_input_read_report(report, 'gateway input target is unreadable')
    if not report.payload and path.exists():
        from ..runtime_errors import DataCorruptionError
        raise DataCorruptionError('gateway input target is empty')
    return report.payload


# LLM: 仅精确同thread/claim/owner/当前route及完整Gateway fence能冻结；目标竞争二次全JSON重读，未支持BG或不完整发布保unknown。
# 函数用途: 从本次原Store读取前台目标；不申请P3，不把等待processing猜为活动执行。
def observe_input_target(control, receipt, scope):
    store = input_owner_store(control, receipt)
    thread, error = store.threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error:
        return 'unknown', None, None
    if thread is None:
        return 'idle', None, None
    claim_path = store.storage.background_claim_path(thread.thread_id)
    claim = target_record(claim_path)
    if not claim:
        return 'idle', None, None
    if (claim.get('schema_version') == 'background_run_claim.v1'
            and claim.get('thread_id') == claim.get('claim_scope_id') == thread.thread_id
            and type(claim.get('claim_id')) is str and claim['claim_id']
            and claim.get('status') in {'finished', 'failed', 'cancelled'}):
        from .multiprocess_input_reconcile import _identity_bytes

        return ('idle', None, None) if _identity_bytes(target_record(claim_path)) == _identity_bytes(claim) else ('unknown', None, None)
    if not _claim_is_current(claim, thread.thread_id):
        return 'unknown', None, None
    if claim.get('reason') != 'gateway_foreground_turn':
        return _observe_background_input_target(control, receipt, scope, store, thread, claim_path, claim)
    if _closing_input_target(control, receipt, scope, claim, thread.thread_id):
        return 'ineligible', None, None
    target = _foreground_target(control.paths, claim, thread.thread_id)
    if target is None:
        return 'unknown', None, None
    path = control.paths.processing / (target.turn_id + '.json')
    payload = target_record(path)
    if (frozen_request_owner(payload) != frozen_request_owner(receipt.prepared_request)
            or not validate_published_route(payload, control.worker_count, control.c_run_id)['ok']):
        return 'unknown', None, None
    from .control_service import _request_matches_scope

    if not _request_matches_scope(payload, scope):
        return 'ineligible', None, None
    fence = {key: payload.get(key) for key in ('execution_attempt_id', 'lease_epoch', 'lease_owner')}
    fence['request_id'] = target.turn_id
    from .input_transport import validated_input_transport

    try:
        transport = validated_input_transport({'schema_version': 'gateway_input_transport.v1',
            'target': target.to_dict(), 'gateway_claim': fence, 'route': payload['route']})
    except inputs.DataCorruptionError as error:
        inputs._raise_input_transport_uncertain(error)
        return 'unknown', None, None
    if target_record(claim_path) != claim or target_record(path) != payload:
        return 'unknown', None, None
    if not active_text_options_match(receipt.prepared_request, payload):
        return 'ineligible', None, None
    observation = (store, claim_path, claim, path, payload)
    return 'exact', transport, observation


# LLM: 必须在_foreground_target的closing早退前核完整原claim/fence/route/owner/peer；closing不是idle，也不授执行，完整新输入只排原队列。
# 函数用途: 识别同一目标已进入正常封口，避免新输入永久pending或回流该回合。
def _closing_input_target(control, receipt, scope, claim, thread_id):
    from .control_service import _request_matches_scope

    task_id = claim.get('task_id')
    if type(task_id) is not str or not task_id.startswith('gateway:'):
        return False
    request_id = task_id[len('gateway:'):]
    if not request_id or '/' in request_id or '\\' in request_id:
        return False
    path = control.paths.processing / (request_id + '.json')
    payload = target_record(path)
    expected = {'schema_version': 'gateway_conversation_claim.v1', 'request_id': request_id,
                'thread_id': thread_id, 'task_id': task_id}
    if (payload.get('id') != request_id or payload.get('status') != 'processing'
            or payload.get('turn_phase') != 'closing' or payload.get('conversation_claim') != expected
            or type(payload.get('execution_attempt_id')) is not str or not payload['execution_attempt_id']
            or type(payload.get('lease_epoch')) is not int or payload['lease_epoch'] < 1
            or type(payload.get('lease_owner')) is not str or not payload['lease_owner']
            or frozen_request_owner(payload) != frozen_request_owner(receipt.prepared_request)
            or not validate_published_route(payload, control.worker_count, control.c_run_id)['ok']
            or not _request_matches_scope(payload, scope)):
        return False
    return target_record(path) == payload


# LLM: 原TaskStore guard按safe_file_stem同一规范路径，只try不在Gateway T内等task；返回锁不是权限。
# 函数用途: 为BG末核复用原任务锁，避免反向阻塞core的task→T发布。
def input_background_task_guard(store, task_id):
    from ..conversation.store_io import safe_file_stem
    from .io import locked_file_transition
    stem = safe_file_stem(task_id)
    return locked_file_transition(store.storage.tasks_dir / ('.' + stem + '.transition'), blocking=False)


# LLM: 原thread绑定是BG peer事实，Gateway后台envelope没有前台conversation字段；只读同owner canonical thread，不伪造request。
# 函数用途: 比较同一认证channel/user/conversation与已解析线程绑定。
def background_input_peer_matches(store, thread_id, scope):
    row = target_record(store.storage.thread_path(thread_id))
    return any(binding.get('channel') == scope.channel
               and binding.get('channel_conversation_id') == scope.conversation_id
               and binding.get('channel_user_id') == scope.user_id
               for binding in row.get('channel_bindings', ()))


# LLM: reference仅定位唯一processing，完整task/claim/core/fence/route/owner/peer共同末核；缺/两次变化保unknown，不扫共享库。
# 函数用途: 给C观察真实主BG task，不构造Agent或执行来源。
def _observe_background_input_target(control, receipt, scope, store, thread, claim_path, claim):
    from ..conversation.models import ThreadTaskLink
    from .execution_target import GatewayExecutionTarget
    from .input_transport import validated_input_transport
    ref = claim.get('gateway_transport_reference')
    authority = claim.get('runtime_authority', {})
    # LLM: 未发布仅观察类别，不授queue；保原current claim/task/peer完整raw，漂移或半发布仍unknown，C另核actual fresh与M严格缺席。
    # 函数用途: 给正常BG首拍提供完整原观察，不把无reference等同闲置或物理释放。
    if 'gateway_transport_reference' not in claim and 'runtime_authority' not in claim:
        task_id = claim.get('task_id')
        if type(task_id) is not str or not task_id:
            return 'unknown', None, None
        task_path = store.storage.task_path(task_id)
        task_raw = target_record(task_path)
        link = ThreadTaskLink.from_dict(task_raw) if task_raw else None
        thread_path = store.storage.thread_path(thread.thread_id)
        thread_raw = target_record(thread_path)
        if (link is None or link.task_id != task_id or link.thread_id != thread.thread_id
                or link.status != 'active' or link.work_kind == 'audit'
                or not background_input_peer_matches(store, thread.thread_id, scope)
                or target_record(claim_path) != claim or target_record(task_path) != task_raw
                or target_record(thread_path) != thread_raw):
            return 'unknown', None, None
        return 'unpublished', None, (store, claim_path, claim, task_path, task_raw, thread_path, thread_raw)
    if (type(ref) is not dict or type(ref.get('request_id')) is not str or not ref['request_id']
            or '/' in ref['request_id'] or '\\' in ref['request_id'] or type(authority) is not dict
            or type(authority.get('task_id')) is not str or not authority['task_id']):
        return 'unknown', None, None
    task_path = store.storage.task_path(authority['task_id'])
    task_raw = target_record(task_path)
    link = ThreadTaskLink.from_dict(task_raw) if task_raw else None
    if (link is None or link.task_id != authority['task_id'] or link.thread_id != thread.thread_id
            or link.status != 'active' or link.work_kind == 'audit'):
        return 'unknown', None, None
    path = control.paths.processing / (ref['request_id'] + '.json')
    payload = target_record(path)
    if (frozen_request_owner(payload) != frozen_request_owner(receipt.prepared_request)
            or not validate_published_route(payload, control.worker_count, control.c_run_id)['ok']
            or (payload.get('metadata') or {}).get('background_source', {}).get('thread_id') != thread.thread_id
            or not background_input_peer_matches(store, thread.thread_id, scope)):
        return 'unknown', None, None
    expected = {'schema_version': 'gateway_runtime_authority.v1', 'request_id': ref['request_id'],
                'gateway_execution_attempt_id': ref.get('gateway_execution_attempt_id'),
                **{key: authority.get(key) for key in ('task_id', 'run_id', 'agent_run_id', 'attempt_id')}}
    if payload.get('status') != 'processing' or payload.get('turn_phase') != 'open' or payload.get('runtime_authority') != expected:
        return 'unknown', None, None
    target = GatewayExecutionTarget(thread.thread_id, 'task', authority.get('invocation_run_id', ''), claim['claim_id'],
        authority.get('attempt_id', ''), authority, link.task_id)
    try:
        transport = validated_input_transport({'schema_version': 'gateway_input_transport.v2', 'target': target.to_dict(),
            'gateway_claim': {'request_id': ref['request_id'], **{key: payload.get(key) for key in
                ('execution_attempt_id', 'lease_epoch', 'lease_owner')}},
            'route': payload['route'], 'claim_transport_reference': ref})
    except inputs.DataCorruptionError as error:
        inputs._raise_input_transport_uncertain(error)
        return 'unknown', None, None
    thread_path = store.storage.thread_path(thread.thread_id)
    thread_raw = target_record(thread_path)
    if (target_record(claim_path) != claim or target_record(path) != payload or target_record(task_path) != task_raw
            or not active_text_options_match(receipt.prepared_request, payload)):
        return 'unknown', None, None
    return 'exact', transport, (store, claim_path, claim, path, payload, task_path, task_raw, thread_path, thread_raw)
