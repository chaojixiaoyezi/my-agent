# LLM: 必要输入沿 SessionTaskStore 原目录持久化；它不是执行租约，不复制任务状态或调度队列，Wake 是唯一恢复出口。
# 模块用途: 登记准确任务的外部输入依赖，归档后让出执行，回调或到期通过原唤醒一次恢复。
from __future__ import annotations

import json
import math
import time
from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import OwnerQuotaChange
from ..user_space.owner_resolver import owner_identity_from_config
from .input_dependency_index import update_task_input_refs


# LLM: 路径只由原 owner SessionTaskStore 和拒绝式 ID 决定；调用方不能传任意文件位置。
# 函数用途: 返回一条输入依赖的唯一保存位置。
def input_dependency_path(agent, input_id: str) -> Path:
    return agent.conversation_store.session_tasks.storage.session_tasks_dir / 'inputs' / (sha256(input_id.encode()).hexdigest() + '.json')


# LLM: 配额锁先于TaskStore/输入锁；创建可复用外层 admission，锁内不得执行模型、网络或取得执行权。
# 函数用途: 原子保存不含秘密的依赖，创建时与根refs容量检查处于同一原任务锁内。
def _mutate(agent, input_id, update, *, admission=None):
    path = input_dependency_path(agent, input_id)
    quota = nullcontext(admission) if admission is not None else agent.owner_quota.admission()
    with quota as admission, locked_json_path(path):
        report = read_json_object_report(path, context='task_input.read')
        if report.load_error:
            raise ValueError('输入依赖记录不可读')
        row = update(dict(report.payload or {}))
        text = json.dumps(row, ensure_ascii=False)
        admission.check([OwnerQuotaChange(path, len(text.encode()))])
        write_private_text_file_atomic_unlocked(path, text)
    return row


# LLM: child 恢复必须携带原父用户请求，不能把 child run 当成不存在的聊天消息；先读取宿主冻结属性。
# 函数用途: 固定必要输入完成后原 Wake 应续接的用户请求编号，不解析任务正文。
def _origin_request_id(agent, scope):
    current = getattr(agent, '_current_run_params', None)
    attributes = getattr(current, 'task_attributes', None) or {}
    return str(attributes.get('conversation_request_id')
               or (getattr(current, 'request_id', '') if scope.agent_kind == 'main' else '')
               or scope.root_task_id)


# LLM: 输入需要真实执行资格；原TaskStore锁内最多32准确refs，替换仅接同owner/scope/provider的pending输入；超限或写入失败保留旧槽。
# 函数用途: 有界登记必要输入，同ID异绑定拒绝；不启动等待线程或新模型回合。
def create_input_dependency(agent, scope, *, input_id, provider, expires_at, reference='', metadata=None, replaces_input_id=''):
    if (not scope.active or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at) or expires_at <= time.time() or not provider
            or not isinstance(input_id, str) or not input_id):
        raise ValueError('当前任务或输入期限无效')
    row = {'schema_version': 'task_input.v1', 'input_id': input_id, 'provider': provider,
           'owner': asdict(owner_identity_from_config(agent.config)), 'scope': asdict(scope),
           'reference': reference, 'expires_at': expires_at, 'status': 'pending',
           'armed': False, 'created_at': time.time(), 'metadata': dict(metadata or {}), 'result_refs': [],
           'conversation_request_id': _origin_request_id(agent, scope)}
    # LLM: 只核对冻结身份，不重置期限或覆盖已经提交的结果。
    # 函数用途: 在记录锁内创建或重放同一依赖。
    def create(previous, available):
        if previous:
            keys = ('provider', 'owner', 'scope', 'reference', 'metadata')
            if any(previous.get(key) != row[key] for key in keys):
                raise ValueError('同一输入依赖绑定冲突')
            if previous['status'] == 'pending' and not available:
                raise ValueError('TASK_INPUT_CAPACITY_EXCEEDED')
            return previous
        if not available:
            raise ValueError('TASK_INPUT_CAPACITY_EXCEEDED')
        return row
    if replaces_input_id:
        previous = read_json_object_report(input_dependency_path(agent, replaces_input_id), context='task_input.replace').payload
        if not previous or previous.get('status') != 'pending' or any(previous.get(key) != row[key] for key in ('owner','scope','provider')):
            raise ValueError('被替换输入绑定无效')
    saved = {}
    # LLM: owner配额→原task→输入记录固定锁序；回调仅保存依赖，不获取worker或发卡。
    # 函数用途: 在容量判断所在任务锁内写canonical行，拒绝时不留下新依赖。
    def persist(available):
        saved.update(_mutate(agent, input_id, lambda previous: create(previous, available), admission=admission))
        return saved
    with agent.owner_quota.admission() as admission:
        update_task_input_refs(agent, scope.root_task_id, input_id, persist=persist, replaces_input_id=replaces_input_id)
    _schedule_input_deadline(agent, scope.root_task_id)
    return saved


# LLM: 取消和终态从原 TaskLink/SessionTask/child 读取，旧 claim 不授予新执行；owner 仍由 canonical 配置核验。
# 函数用途: 核对私人回调或恢复所需的任务输入绑定，不读取表单值。
def input_dependency_current(agent, row):
    if row.get('owner') != asdict(owner_identity_from_config(agent.config)):
        return False
    scope = row.get('scope') or {}
    try:
        link, error = agent.conversation_store.tasks.load_report(scope['root_task_id'])
        if error or link is None or link.status != 'active':
            return False
        thread, error = agent.conversation_store.threads.load_report(scope['thread_id'])
        if error or thread is None:
            return False
        if scope.get('agent_kind') == 'subagent':
            task = agent.subagents.load(scope['run_id'])
            if task.root_id != scope['root_task_id'] or task.agent_thread_id != scope['thread_id']:
                return False
            if str(task.status).upper() in {'CANCELLED', 'FAILED', 'DONE', 'TAKEN_OVER', 'SUPERSEDED'}:
                return False
        session_task = agent.conversation_store.session_tasks.load(scope['run_id'])
        if session_task is not None and session_task.status in {'cancelled', 'failed', 'done'}:
            return False
        return True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        return False


# LLM: armed 仅在完整工具归档与 native 配对之后写；先到回调可提交状态，但必须等此边界才发布恢复。
# 函数用途: 让已归档的输入等待可被一次唤醒，保留崩溃后可重放的同源记录。
def arm_input_dependency(agent, input_id):
    row = _mutate(agent, input_id, lambda row: {**row, 'armed': True} if row else _missing())
    _schedule_input_deadline(agent, row['scope']['root_task_id'])
    return _publish_resolution(agent, row)


# LLM: 终态单调，重复同决定只补原 Wake；不同决定不能覆盖，超期不能接受 ready。
# 函数用途: 保存输入完成、取消或到期事实，成功后恢复原任务一次。
def resolve_input_dependency(agent, input_id, *, status='ready', result_refs=()):
    if status not in {'ready', 'cancelled', 'expired', 'revoked', 'failed'}:
        raise ValueError('输入决定无效')
    # LLM: 原终态不能被迟到或相反回调改写。
    # 函数用途: 在锁内保存一次输入决定。
    def resolve(row):
        if not row:
            return _missing()
        if row['status'] != 'pending':
            if row['status'] != status:
                raise ValueError('原输入依赖已结束')
            return row
        if status == 'ready' and (time.time() >= row['expires_at'] or not input_dependency_current(agent, row)):
            raise ValueError('原任务或输入期限已失效')
        return {**row, 'status': status, 'resolved_at': time.time(), 'result_refs': list(result_refs)}
    return _publish_resolution(agent, _mutate(agent, input_id, resolve))


# LLM: 仅armed终态发布原去重Wake；终态移除refs放槽，提前结果仍能在归档arming后沿canonical重放。
# 函数用途: 回收终态等待引用，归档后把已结算依赖交原调度，不在回调里执行工作。
def _publish_resolution(agent, row):
    if row.get('armed') and row['status'] != 'pending' and input_dependency_current(agent, row):
        scope = row['scope']
        thread_id = scope['thread_id']
        if scope['agent_kind'] == 'subagent':
            link, _ = agent.conversation_store.tasks.load_report(scope['root_task_id'])
            thread_id = link.thread_id
        agent.conversation_store.wakes.raise_signal({'thread_id': thread_id,
            'root_task_id': scope['root_task_id'], 'reason': 'task_input_resolved',
            'source_agent_id': scope['run_id'], 'evidence_refs': row.get('result_refs', []),
            'metadata': {'schema_version': 'task-input-event.v1', 'input_id': row['input_id'],
                         'input_status': row['status'], 'input_provider': row['provider'],
                         'input_run_id': scope['run_id'], 'conversation_request_id': row.get('conversation_request_id', scope['run_id'])},
            'dedupe_key': 'task-input:' + row['input_id'], 'retain_handled': True})
    if row['status'] != 'pending':
        update_task_input_refs(agent, row['scope']['root_task_id'], row['input_id'], remove=True)
        _schedule_input_deadline(agent, row['scope']['root_task_id'])
    return row


# LLM: 不存在的依赖是客观坏绑定，不得创建空记录或恢复其它任务。
# 函数用途: 为锁内状态转换返回稳定错误。
def _missing():
    raise ValueError('原输入依赖不存在')


# LLM: 到期沿原 RuntimeDB 的任务级 wake_queue；不同输入取最近期限，不新增定时线程或第二调度队列。
# 函数用途: 为任务的现存准确输入安排一次期限检查；仅保存查找 refs，状态由原行读取。
def _schedule_input_deadline(agent, task_id):
    link, error = agent.conversation_store.tasks.load_report(task_id)
    if error or link is None:
        raise ValueError('输入任务记录不可读')
    pending = []
    for input_id in getattr(link, 'input_dependency_refs', ()):
        row = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.deadline').payload
        if row and row['status'] == 'pending':
            pending.append(row['expires_at'])
    repo = getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
    if pending and repo is not None:
        repo.upsert_wake(root_task_id=task_id, root_thread_id=link.thread_id,
                         next_due_at=min(pending), kind='task_input_deadline')


# LLM: 只读取一个准确任务的 refs，不全扫 owner 历史；完成记录先发布原 Wake 再移除投影，失败仍可重试。
# 函数用途: 在原任务到期唤醒时回收输入或恢复此前未完成的提交，不拥有常驻等待资源。
def reconcile_input_dependencies(agent, *, task_id='', now=None, schedule=True):
    store = getattr(agent, 'conversation_store', None)
    if store is None or not task_id:
        return
    current = time.time() if now is None else now
    link, error = store.tasks.load_report(task_id)
    if error or link is None:
        return
    for input_id in getattr(link, 'input_dependency_refs', ()):
        row = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.reconcile').payload
        if not row or row.get('schema_version') != 'task_input.v1':
            continue
        if row['status'] == 'pending' and (current >= row['expires_at'] or not input_dependency_current(agent, row)):
            resolve_input_dependency(agent, row['input_id'], status='expired' if current >= row['expires_at'] else 'cancelled')
            if not input_dependency_current(agent, row):
                update_task_input_refs(agent, task_id, input_id, remove=True)
        elif row.get('armed') and row['status'] != 'pending':
            _publish_resolution(agent, row)
    if schedule:
        _schedule_input_deadline(agent, task_id)


# LLM: 普通输入等待不阻塞新群消息；只给同一准确任务的自动续跑提供结构化静止事实；缺Store的无任务宿主保持无依赖。
# 函数用途: 沿任务 refs 直接查询必要输入，不扫描整个 owner 的凭据或历史。
def task_has_waiting_input(agent, task_id):
    store = getattr(agent, 'conversation_store', None)
    tasks = getattr(store, 'tasks', None)
    if tasks is None or not task_id or not callable(getattr(tasks, 'load_report', None)):
        return False
    link, error = tasks.load_report(task_id)
    if error or link is None:
        return False
    for input_id in getattr(link, 'input_dependency_refs', ()):
        row = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.pending').payload
        if row and row['status'] == 'pending' and row.get('armed') and input_dependency_current(agent, row):
            return True
    return False


# LLM: 凭据记录只桥接通用输入 ID，不把密文/表单放入依赖；原人核验在私交互入口。
# 函数用途: 私卡提交时接受原任务持久绑定；直接存储调用也沿同一依赖。
def credential_submission_current(agent, row):
    dependency = read_json_object_report(input_dependency_path(agent, row.get('input_id', row['credential_ref'])), context='credential.input').payload
    return bool(dependency and dependency['status'] == 'pending' and input_dependency_current(agent, dependency))


# LLM: 凭据成功 refs 只是 opaque cref，取消不会给模型任何认证值；原 Wake 是唯一续跑出口。
# 函数用途: 把私卡结果提交给通用任务输入恢复。
def publish_credential_resolution(agent, row):
    return resolve_input_dependency(agent, row.get('input_id', row['credential_ref']), status=row['status'],
                                    result_refs=(row['credential_ref'],) if row['status'] == 'ready' else ())


# LLM: 宿主准确停止任务后调用，不解析用户文字；原输入状态取消且移除投影，未知外部工具操作不被重写为未执行。
# 函数用途: 按准确root或run即时回收未提交输入；子取消不碰兄弟依赖，不发恢复Wake、不代替原停止或进程清理。
def cancel_task_inputs(agent, task_id, *, run_id=''):
    store = getattr(agent, 'conversation_store', None)
    if store is None:
        return
    link, error = store.tasks.load_report(task_id)
    if error or link is None:
        return
    for input_id in getattr(link, 'input_dependency_refs', ()):
        existing = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.cancel').payload
        if not existing or (run_id and existing.get('scope', {}).get('run_id') != run_id):
            continue
        # LLM: 控制取消不撤销已 ready 的独立凭据授权；只关闭未提交依赖，且不会发布业务恢复。
        # 函数用途: 在原输入锁内清理一个仍未完成的必要输入。
        def cancel(row):
            return {**row, 'status': 'cancelled', 'resolved_at': time.time(), 'armed': False} if row.get('status') == 'pending' else row
        _mutate(agent, input_id, cancel)
        update_task_input_refs(agent, task_id, input_id, remove=True)
    _schedule_input_deadline(agent, task_id)
