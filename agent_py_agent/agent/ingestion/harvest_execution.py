# LLM: 仅原Gateway harvester source/P3/P5同步consumer执行一拍；原heldC/受限host缺scope仍拒legacy daemon回落，Count0原线程不经此入口。
# 模块用途: 把原watch快照、来源租约和一次收割接到新请求，长网络/扫描在T外，实际提交沿原短写门。
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..gateway_parts.daemon_metadata import process_host_id, process_identity_is_live
from ..gateway_parts.multiprocess_background_execution import background_short_operation
from ..gateway_parts.multiprocess_background_request import BackgroundSourceChanged
from ..gateway_parts.multiprocess_executor_identity import process_birth
from ..gateway_parts.request_errors import RequestExecutionOutcomeUncertainError
from . import harvester as harvest
from .watch_state import load_state, registry


# LLM: 原冻结W/current execution/heldC/不可逆host都只选择更严来源请求路径，不能从缺current scope回落Count0或授执行权；原ControlPlane正数仍同源拒daemon。
# 函数用途: 判断watch是否必须交原Gateway独立请求，默认只读原内存事实，不创建线程或账本。
def requires_request_harvest(agent=None):
    from ..gateway_parts.request_entry_admission import request_execution_required

    control = getattr(agent, '_gateway_control_plane', None)
    return (request_execution_required()
            or control is not None and type(control.worker_count) is int and control.worker_count > 0)


# LLM: 原source字段开放保留；只核身份、schema、有限机械数字及bool，不从prose/name/URL内容推工作类型。
# 函数用途: 为C/W桥读取同一份原watch快照，坏字段不得变成可执行默认值。
def validate_harvester_payload(payload, *, owner_id, watch_id):
    if (type(payload) is not dict or payload.get('schema_version') != 'watch-stream-state.v1'
            or payload.get('watch_id') != watch_id or not re.fullmatch(r'ws-[0-9a-f]{10}', watch_id)
            or payload.get('owner_id') != owner_id or type(payload.get('source_id')) is not str
            or not payload['source_id'] or type(payload.get('source_url')) is not str or not payload['source_url']
            or type(payload.get('tuning')) is not dict or type(payload.get('source_envelope')) is not dict):
        raise ValueError('background_harvester_identity_invalid')
    for name in ('closed', 'audit_guarantee'):
        if type(payload.get(name)) is not bool:
            raise ValueError('background_harvester_state_invalid')
    for name in ('cursor', 'line_cursor', 'spool_seq', 'spool_generation', 'audit_run_epoch', 'watch_window_seconds'):
        if type(payload.get(name)) is not int or payload[name] < 0:
            raise ValueError('background_harvester_state_invalid')
    for name in ('opened_at', 'window_finalized_at', 'last_pull_at', 'last_poll_at', 'saved_at'):
        if type(payload.get(name)) not in (int, float) or not math.isfinite(payload[name]) or payload[name] < 0:
            raise ValueError('background_harvester_state_invalid')
    for name in ('source_config_version', 'source_profile_ref', 'audit_root_task_id', 'source_mode'):
        if type(payload.get(name)) is not str:
            raise ValueError('background_harvester_state_invalid')
    if (type(payload.get('document_refs')) is not list
            or any(type(item) is not str for item in payload['document_refs'])):
        raise ValueError('background_harvester_state_invalid')
    return payload


# LLM: revision只绑定原来源和Audit代次，rawSHA另保所有进度；不能当权限或冻结有限source业务枚举。
# 函数用途: 对原watch的运输/版本/父任务事实求摘要，供同schema source引用复核。
def harvester_input_revision(payload):
    fields = ('source_id', 'source_config_version', 'source_url', 'source_mode', 'source_envelope',
              'source_profile_ref', 'document_refs', 'audit_root_task_id', 'audit_run_epoch')
    return hashlib.sha256(json.dumps({key: payload[key] for key in fields}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 只读原metadata与canonical next_due_at预筛；到点不能绕过已持久化退避，父Audit/背压/claim仍由consumer重核。
# 函数用途: 为原C发现循环筛选仍需收割的持久watch。
def harvester_has_due_work(payload, now):
    if payload['closed'] or payload['window_finalized_at'] > 0:
        return False
    tuning = payload['tuning']
    if not payload['audit_guarantee'] and tuning.get('background_harvest', 1) != 1:
        return False
    control = (payload.get('source_checkpoint') or {}).get('_host_harvest', {})
    due = control.get('next_due_at') if isinstance(control, dict) else None
    if type(due) in (int, float) and math.isfinite(due) and due > now:
        return False
    window = payload['watch_window_seconds']
    # saved_at 是上一次原收割/工具快照时间；不生成持久timer或每轮新请求号。
    return now >= payload['saved_at'] + 1.5 or window > 0 and now >= payload['opened_at'] + window


# LLM: 原T/P5和同metadata引用才允许捕获；返回原scope而非序列化写许可，复制/退出对象不能延寿。
# 函数用途: 拒绝从裸ref、role bool或另一个请求调用watch consumer。
def _original_scope(agent, ref):
    from ..gateway_parts.multiprocess_background_execution import _BACKGROUND_REQUEST
    from ..gateway_parts.multiprocess_execution import current_worker_execution

    scope = _BACKGROUND_REQUEST.get()
    if (scope is None or not scope.lifetime.active or scope.lifetime.creator_pid != os.getpid()
            or scope.execution.agent is not agent or current_worker_execution() is not scope.execution
            or scope.context['request']['metadata'].get('background_source') != ref or ref['kind'] != 'harvester'):
        raise ValueError('background_original_harvester_execution_required')
    background_short_operation('harvester_source_admission', lambda: _check_request_ref(scope, ref))
    return scope


# LLM: shadow context/ref不是权威；原T内再读同一原request的metadata，不能改内存引用后借旧P3消费另一watch。
# 函数用途: 确认本次真实请求仍运输这一个精确来源。
def _check_request_ref(scope, ref):
    from ..gateway_parts.io import read_json_file

    request = read_json_file(scope.context['request_path'])
    if request.get('metadata', {}).get('background_source') != ref:
        raise BackgroundSourceChanged()


# LLM: 只读完整no-follow watch字节；任何原source/epoch/配置换代先于副作用拒绝，不能载入较新source替旧请求执行。
# 函数用途: 在原短门内确认本次冻结的实际watch快照。
def _check_source(state, ref):
    raw = read_bytes_beneath(state.owner_home, Path(ref['source_ref']).parts,
                             max_bytes=1_048_576, require_dir_fd=True)
    if raw is None or hashlib.sha256(raw).hexdigest() != ref['source_sha256']:
        raise BackgroundSourceChanged()


# LLM: 原父全列表只在T外observe；最终T内核同一个已捕获single link/epoch/thread，不能跟随新父给旧plan授权。
# 函数用途: 在开始和最终提交时复核本拍的唯一原父快照，普通watch保持无会话I/O。
def _parent_active(agent, state, snapshot):
    from .harvest_parent import check_harvest_parent

    return check_harvest_parent(agent, state, snapshot)


# LLM: 观察只保存原lease与父metadata事实，不拥有执行权；prepare最终仍在原T/P5内逐项比较。
# 类用途: 保持来源收割入口显式参数，将T外观察组合给原准备步骤。
@dataclass(frozen=True)
class HarvestObservations:
    lease: tuple
    parent: object


# LLM: 原进程身份观测可能调用平台observer，必须在T外；捕获整份原lease，最终短锁比较同一记录，False只证明该原身份已死。
# 函数用途: 读取旧收割者身份并在短事务前准备原kernel出生事实，不启动ps或借当前事实补旧字段。
def _observe_lease(state):
    report = read_json_object_report(harvest._lease_path(state), context='watch_harvester.original_request_observe')
    current = report.payload
    identity = current.get('process_identity') if current else None
    valid = (not current or type(identity) is dict and type(identity.get('pid')) is int
             and identity['pid'] > 0 and type(identity.get('start_time')) in (int, str) and identity['start_time'] != ''
             and current.get('owner') == f"pid:{identity['pid']}" and current.get('watch_id') == state.watch_id
             and type(current.get('lease_id')) is str and bool(current['lease_id']))
    dead = report.load_error is None and valid and (not current or process_identity_is_live(identity) is False)
    identity = {'host_id': process_host_id(), 'pid': os.getpid(), 'start_time': process_birth(os.getpid())}
    return deepcopy(current), dead, identity


# LLM: TTL不能证明死亡，缺identity/损坏/跨host均保原lease；T内只比较T外观测的同一旧记录，不等待平台探针。
# 函数用途: 在原租约文件锁内领取一次收割，只有确切原身份已死才能替旧者。
def _claim_request_lease(state, scope, observed):
    path = harvest._lease_path(state)
    with locked_json_path(path):
        report = read_json_object_report(path, context='watch_harvester.original_request')
        if report.load_error is not None:
            return None
        current = report.payload
        old, dead, identity = observed
        if current != old or not dead:
            return None
        if identity.get('start_time') is None:
            return None
        request = scope.context['request']
        payload = {**harvest._lease_payload(state, uuid4().hex), 'process_identity': identity,
                   'request_id': request['id'], 'execution_attempt_id': request['execution_attempt_id']}
        write_json_file_atomic_unlocked(path, payload, sort_keys=False)
        return payload


# LLM: 每次写只核原lease全部捕获字段与原creator PID，不续TTL或跟随新nonce；同一进程的出生事实不会在仍执行时变代。
# 函数用途: 防止覆盖或清理替代者；原lease不可核时保同一原P3未知，不产生普通失败终态。
def _check_lease(state, lease):
    report = read_json_object_report(harvest._lease_path(state), context='watch_harvester.original_request_fence')
    if report.load_error is not None or report.payload != lease or lease['process_identity']['pid'] != os.getpid():
        from ..gateway_parts.multiprocess_background_execution import (
            _BACKGROUND_REQUEST,
            _uncertain_execution,
        )

        scope = _BACKGROUND_REQUEST.get()
        if scope is not None:
            raise _uncertain_execution(scope.transition, 'harvester_original_lease')
        raise harvest._HarvestTransactionUnavailable('original harvest lease changed')


# LLM: 仅本次同步plan保存暂存状态，不是权限/账；原snapshot/intent/lease与transport仍是唯一事实。
# 类用途: 将T内准入与T外网络/引擎计算结果交给最终原事务。
@dataclass
class HarvestRequestPlan:
    state: object
    working: object
    transaction: dict
    lease: dict
    finalize_window: bool
    scope: object
    ref: dict
    creator_thread: int
    parent: object


# LLM: plan不跨scope/PID/线程重新签权；所有原写仍由当前短门核，捕获对象只增加限制。
# 函数用途: 阻止另一请求或晚到线程消费这份私有收割计算。
def _check_plan(plan):
    from ..gateway_parts.multiprocess_background_execution import (
        _BACKGROUND_REQUEST,
        _uncertain_execution,
    )

    if (_BACKGROUND_REQUEST.get() is not plan.scope or not plan.scope.lifetime.active
            or plan.scope.lifetime.creator_pid != os.getpid() or plan.creator_thread != threading.get_ident()):
        raise _uncertain_execution(plan.scope.transition, 'harvester_original_plan')
    _check_request_ref(plan.scope, plan.ref)


# LLM: 原T先于state.lock/intent；重核持久退避并冻结一次上界，背压只延后读取不能延后冻结；不持锁等待网络。
# 函数用途: 领取原收割记录并建立一份本次私有计算状态。
def _prepare(agent, state, ref, scope, observed):
    _check_request_ref(scope, ref)
    with state.lock:
        _check_source(state, ref)
        working = load_state(state.owner_home, state.watch_id)
        if working is None or harvest._should_stop(working):
            return 'stopped'
        parent_active = _parent_active(agent, working, observed.parent)
        if parent_active is None:
            return 'busy'
        if not parent_active:
            return 'stopped'
        control = (working.source_checkpoint or {}).get('_host_harvest', {})
        due = control.get('next_due_at') if isinstance(control, dict) else None
        if type(due) in (int, float) and math.isfinite(due) and due > time.time():
            return 'busy'
        lease = _claim_request_lease(state, scope, observed.lease)
        if lease is None:
            return 'busy'
        try:
            transaction = harvest._begin_harvest_transaction(working)
        except Exception:
            _check_lease(state, lease)
            harvest._lease_path(state).unlink()
            raise
        return HarvestRequestPlan(state, working, transaction, lease, harvest._window_deadline_reached(working),
                                  scope, deepcopy(ref), threading.get_ident(), observed.parent)


# LLM: 物理写仅在原T/state.lock门；末次源和父任务变化不覆盖新记录，已进入异常由原intent保存未知恢复事实。
# 函数用途: 将本次计算的候选、审计、片段和游标提交到原事务。
def _commit(agent, plan, ref, drain, chunks):
    from .harvest_request_plan import commit_harvest_plan

    _check_plan(plan)
    with plan.state.lock:
        _check_lease(plan.state, plan.lease)
        _check_source(plan.state, ref)
        if not _parent_active(agent, plan.working, plan.parent):
            raise BackgroundSourceChanged()
        result = commit_harvest_plan(plan, drain, chunks)
        harvest._copy_watch_state(plan.state, plan.working)
        return result


# LLM: 清理也是原最终短写；取消/撤代后绝不裸删lease或回滚，未知保留原记录给确实死者接手。
# 函数用途: 正常一拍及其通知完成后释放唯一原租约。
def _finish(plan):
    _check_plan(plan)
    with plan.state.lock:
        _check_lease(plan.state, plan.lease)
        harvest._lease_path(plan.state).unlink()


# LLM: 只在活的原运输中按相同intent恢复自己的已进入异常；恢复门失效时保旧记录和P3未知，不能假失败重送新id。
# 函数用途: 原文件提交失败时按唯一事务编号回滚，不清除别人的意图。
def _recover(plan):
    _check_plan(plan)
    with plan.state.lock:
        _check_lease(plan.state, plan.lease)
        _recover_original_intent(plan)
        harvest._copy_watch_state(plan.state, plan.working)
        _finish(plan)


# LLM: 恢复只允许同一原输入与同一intent；自己的ready/state可以核完成，陌生/坏/缺记录不删除、不回滚。
# 函数用途: 在原短门内恢复本次实际意图，来源变代或恢复不可核保原请求未知。
def _recover_original_intent(plan):
    from ..gateway_parts.multiprocess_background_execution import _uncertain_execution

    try:
        raw = read_bytes_beneath(plan.state.owner_home, Path(plan.ref['source_ref']).parts,
                                 max_bytes=1_048_576, require_dir_fd=True)
        if raw is None:
            raise ValueError('original recovery source absent')
        payload = validate_harvester_payload(load_strict_json(raw), owner_id=plan.state.owner_id, watch_id=plan.state.watch_id)
        if (harvester_input_revision(payload) != plan.ref['input_revision']
                or payload['closed'] != plan.working.closed or payload['audit_guarantee'] != plan.working.audit_guarantee):
            raise ValueError('original recovery source changed')
        current = harvest._read_harvest_transaction(plan.working)
        if current is None:
            if not harvest._transaction_is_committed(plan.working, plan.transaction):
                raise ValueError('original recovery intent absent')
            harvest._copy_watch_state(plan.working, harvest._state_snapshot(plan.working))
        else:
            if current['transaction_id'] != plan.transaction['transaction_id']:
                raise ValueError('original recovery intent changed')
            if not harvest._recover_pending_harvest(plan.working, expected_transaction_id=plan.transaction['transaction_id']):
                raise ValueError('original recovery unresolved')
    except (OSError, ValueError, harvest._HarvestTransactionUnavailable) as error:
        raise _uncertain_execution(plan.scope.transition, 'harvester_original_recovery') from error


# LLM: 网络/引擎在T与state.lock外；冻结前先核文件授权，partial只唤醒待处理工作，只有耐久finalized才能通知收尾。
# 函数用途: 执行一拍原harvester，通知的任务/日志扫描在T外，各实际写者单独短提交，返回原桥结构结果。
def execute_harvester_source(agent, ref, payload):
    from .harvest_parent import observe_harvest_parent
    from .harvest_request_plan import prepare_harvest_plan
    from .sources import file_path_of
    from .watch_tool import WatchStreamTool, _file_access_policy

    scope = _original_scope(agent, ref)
    home = agent.home_paths.owner_home_dir
    validate_harvester_payload(payload, owner_id=agent.home_paths.owner_id, watch_id=ref['source_id'])
    if harvester_input_revision(payload) != ref['input_revision'] or ref['source_ref'] != f"watch_state/{ref['source_id']}.json":
        raise BackgroundSourceChanged()
    state = registry.get_or_load(home, ref['source_id'])
    if state is None or state.owner_home != home:
        raise BackgroundSourceChanged()
    observed_state = load_state(home, ref['source_id'])
    if observed_state is None:
        raise BackgroundSourceChanged()
    tool = WatchStreamTool(agent)
    if observed_state.source_url.startswith('file://') and not _file_access_policy(tool).check(file_path_of(observed_state.source_url)).allowed:
        raise ValueError('background_harvester_source_path_denied')
    observed = HarvestObservations(_observe_lease(state), observe_harvest_parent(agent, observed_state))
    plan = background_short_operation('harvester_prepare', lambda: _prepare(agent, state, ref, scope, observed))
    if type(plan) is str:
        return {'outcome': 'pending' if plan == 'busy' else 'handled', 'status': plan}
    try:
        drain, chunks = prepare_harvest_plan(plan, tool._fetch_json)
        healthy = background_short_operation('harvester_commit', lambda: _commit(agent, plan, ref, drain, chunks))
    except RequestExecutionOutcomeUncertainError:
        raise
    except BackgroundSourceChanged as error:
        from ..gateway_parts.multiprocess_background_execution import _uncertain_execution

        raise _uncertain_execution(scope.transition, 'harvester_source_changed') from error
    except Exception:
        background_short_operation('harvester_recover', lambda: _recover(plan))
        raise
    if plan.working.audit_guarantee:
        from .source_worker import settle_audit_source_worker, wake_audit_source_worker

        callback = settle_audit_source_worker if plan.working.window_finalized_at > 0 else wake_audit_source_worker
        background_short_operation('harvester_notify_admission', lambda: _check_plan(plan))
        callback(agent, plan.state)
    background_short_operation('harvester_finish', lambda: _finish(plan))
    return {'outcome': 'handled', 'status': 'ok' if healthy else 'source_error', 'cursor': plan.state.cursor}
