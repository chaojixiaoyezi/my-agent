# LLM: 原输入读口与只读迁移共用纯记录校验，协议状态/owner/期限判据不分叉。
# LLM: Count0原Agent资源与冷C显式输入服务共用唯一算法；owner资源工厂组装原quota/child loader/既存RuntimeDB，C每次RMW仍核原producer，不从refs或callback授信。
# 模块用途: 原输入行/refs/Wake/deadline共用于无Agent的C和既有W；空期限精确退休，保部分提交回放。
# LLM: 必要输入沿 SessionTaskStore 原目录持久化；它不是执行租约，不复制任务状态或调度队列，Wake 是唯一恢复出口。
# 模块用途: 登记准确任务的外部输入依赖，归档后让出执行，回调或到期通过原唤醒一次恢复。
from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import OwnerQuotaChange
from ..user_space.owner_resolver import owner_identity_from_config
from .input_dependency_index import update_task_input_refs

_TERMINAL_INPUT_STATUSES = frozenset({'ready','cancelled','expired','revoked','failed'})
_INPUT_DEP_RECORD_CACHE_MAX_ENTRIES_COUNT = 256
_INPUT_DEP_RECORD_CACHE_LOCK = threading.RLock()
_INPUT_DEP_RECORD_CACHE: OrderedDict[tuple[str, int, int, int, int, int], dict[str, Any]] = OrderedDict()


# LLM: key只代表物理版本、不代表授权；ctime识别等长修改后恢复mtime，stat失败继续回原reader，读前后稳定核验不变。
# 函数用途: 为输入热读生成路径/设备/inode/大小/mtime/ctime缓存键，及时丢弃旧输入投影。
def _input_dependency_record_key(path):
    try:
        info = path.stat()
    except OSError:
        return None
    return str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 仅pending只读热路径复用成功稳定负载；canonical读和RMW不经过缓存，返回深拷贝且current仍逐次核验。
# 函数用途: 省掉未变化输入文件的重复JSON读取，不缓存错误或空记录。
def _read_pending_input(path):
    key = _input_dependency_record_key(path)
    with _INPUT_DEP_RECORD_CACHE_LOCK:
        cached = _INPUT_DEP_RECORD_CACHE.get(key) if key is not None else None
        if cached is not None:
            _INPUT_DEP_RECORD_CACHE.move_to_end(key)
    if cached is not None:
        return copy.deepcopy(cached)
    report = read_json_object_report(path, context='task_input.pending')
    if report.payload and not report.load_error and key is not None and _input_dependency_record_key(path) == key:
        snapshot = copy.deepcopy(report.payload)
        with _INPUT_DEP_RECORD_CACHE_LOCK:
            _INPUT_DEP_RECORD_CACHE[key] = snapshot
            _INPUT_DEP_RECORD_CACHE.move_to_end(key)
            while len(_INPUT_DEP_RECORD_CACHE) > _INPUT_DEP_RECORD_CACHE_MAX_ENTRIES_COUNT:
                _INPUT_DEP_RECORD_CACHE.popitem(last=False)
    return report.payload


# LLM: 路径只由原 owner SessionTaskStore 和拒绝式 ID 决定；调用方不能传任意文件位置。
# 函数用途: 返回一条输入依赖的唯一保存位置。
# LLM: 路径只取原Store同一实现，不从正文或scope猜owner。
# 函数用途: 保持Agent原输入定位接口。
def input_dependency_path(agent, input_id: str) -> Path:
    return input_dependency_path_from_store(agent.conversation_store, input_id)


# LLM: 配额锁先于TaskStore/输入锁；创建可复用外层 admission，锁内不得执行模型、网络或取得执行权。
# 函数用途: 原子保存不含秘密的依赖，创建时与根refs容量检查处于同一原任务锁内。
# LLM: 原Agent调用继续原quota/阻塞锁和私有字节，C使用同一领域实现逐次消费真实门。
# 函数用途: 更新原输入行，不初始化额外服务。
def _mutate(agent, input_id, update, *, admission=None):
    return input_dependency_access(agent).mutate(input_id, update, admission=admission)


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
# LLM: 原current算法唯一位于InputDependencyAccess，actor/权限不从本返回值新建。
# 函数用途: 核对原输入归属及活动任务。
def input_dependency_current(agent, row):
    return input_dependency_access(agent).current(row)


# LLM: armed只在完整归档/native配对后写；同一_mutate锁内卡片静默退休不得重臂，泛用cancelled仍按原Wake合同，原子返回终态由边界决定不等待。
# 函数用途: 让已归档的输入等待可被一次唤醒，保留崩溃后可重放的同源记录。
def arm_input_dependency(agent, input_id):
    row = _mutate(agent,input_id,lambda row:{**row,'armed':not (row.get('status')=='cancelled' and row.get('resolution_delivery')=='retire')} if row else _missing())
    _schedule_input_deadline(agent, row['scope']['root_task_id'])
    return _publish_resolution(agent, row)


# LLM: 终态单调，重复同决定只补原 Wake；不同决定不能覆盖，超期不能接受 ready。
# 函数用途: 保存输入完成、取消或到期事实，成功后恢复原任务一次。
# LLM: 同原终态/期限/锁和Wake顺序，不吞部分提交失败或生成替代决定。
# 函数用途: 沿原领域事务保存输入决定。
def resolve_input_dependency(agent, input_id, *, status='ready', result_refs=()):
    return input_dependency_access(agent).resolve(input_id, status=status, result_refs=result_refs)


# LLM: 仅armed终态发布原去重Wake；终态移除refs放槽，提前结果仍能在归档arming后沿canonical重放。
# 函数用途: 回收终态等待引用，归档后把已结算依赖交原调度，不在回调里执行工作。
# LLM: 原调用仍补同键Wake/refs/deadline；C共享同一算法并在每个RMW末核。
# 函数用途: 发布原输入已存事实，不重复执行任务。
def _publish_resolution(agent, row):
    return input_dependency_access(agent).publish(row)


# LLM: 不存在的依赖是客观坏绑定，不得创建空记录或恢复其它任务。
# 函数用途: 为锁内状态转换返回稳定错误。
def _missing():
    raise ValueError('原输入依赖不存在')


# LLM: 到期沿原 RuntimeDB 的任务级 wake_queue；不同输入取最近期限，不新增定时线程或第二调度队列。
# 函数用途: 为任务的现存准确输入安排一次期限检查；仅保存查找 refs，状态由原行读取。
# LLM: 期限仅取原refs/pending行，原repo缺失保持旧合同，不新造调度源。
# 函数用途: 更新原任务输入deadline。
def _schedule_input_deadline(agent, task_id):
    return input_dependency_access(agent).schedule(task_id)


# LLM: 只读取一个准确任务的 refs，不全扫 owner 历史；完成记录先发布原 Wake 再移除投影，失败仍可重试。
# 函数用途: 在原任务到期唤醒时回收输入或恢复此前未完成的提交，不拥有常驻等待资源。
def reconcile_input_dependencies(agent, *, task_id='', now=None, schedule=True):
    if getattr(agent, 'conversation_store', None) is None or not task_id:
        return
    return input_dependency_access(agent).reconcile(task_id, now=now, schedule=schedule)


# LLM: 原任务refs/status/armed/current每拍核验，缓存只省输入行I/O，不缓存活动性或授信；canonical与mutate原入口不变。
# 函数用途: 沿准确任务refs检查等待输入，复用未变文件负载但保留实时任务判据。
def task_has_waiting_input(agent, task_id):
    store = getattr(agent, 'conversation_store', None)
    tasks = getattr(store, 'tasks', None)
    if tasks is None or not task_id or not callable(getattr(tasks, 'load_report', None)):
        return False
    link, error = tasks.load_report(task_id)
    if error or link is None:
        return False
    for input_id in getattr(link, 'input_dependency_refs', ()):
        row = _read_pending_input(input_dependency_path(agent, input_id))
        if row and row['status'] == 'pending' and row.get('armed') and input_dependency_current(agent, row):
            return True
    return False


# LLM: 凭据记录只桥接通用输入 ID，不把密文/表单放入依赖；原人核验在私交互入口。
# 函数用途: 私卡提交时接受原任务持久绑定；直接存储调用也沿同一依赖。
# LLM: 保原pending/current判据，不为C添加第二输入schema或改变既有card期限。
# 函数用途: 核对原卡的待填输入仍可提交。
def credential_submission_current(agent, row):
    return credential_submission_current_from_access(input_dependency_access(agent), row)


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


# LLM: 与原read共用完全相同的schema/owner/状态/scope判据；纯读取者先自行nofollow取完整raw，不在此处创建或修改输入。
# 函数用途: 校验一条原输入依赖的结构身份，避免迁移读取另抄一份协议。
def validate_input_dependency_record(row, owner, input_id, *, task_id=''):
    scope = row.get('scope') if isinstance(row, dict) else None
    if (not isinstance(row, dict) or not row or row.get('schema_version') != 'task_input.v1'
            or row.get('input_id') != input_id or row.get('owner') != asdict(owner)
            or not isinstance(row.get('status'), str) or row['status'] not in _TERMINAL_INPUT_STATUSES | {'pending'}
            or type(row.get('armed')) is not bool or not isinstance(scope, dict)
            or not isinstance(scope.get('agent_kind'), str) or scope['agent_kind'] not in {'main', 'subagent'}
            or any(not isinstance(scope.get(key), str) or not scope[key] for key in ('run_id', 'root_task_id', 'thread_id'))
            or isinstance(row.get('expires_at'), bool) or not isinstance(row.get('expires_at'), (int, float))
            or not math.isfinite(row['expires_at'])
            or (task_id and scope['root_task_id'] != task_id)):
        raise ValueError('输入依赖规范记录不可核验')
    return row


# LLM: 本对象只封装原输入领域的真实资源/算法，commit由原producer逐次末核；不提供Agent接口、不签claim或新scope。
# 类用途: 让Count0与冷C共用同一输入行/refs/Wake/deadline事务，不初始化业务Agent。
class InputDependencyAccess:
    # LLM: owner/store/子任务reader/upsert及同一个repo都由原组合根提供；repo只用于精确deadline/CAS收口，构造无I/O或新增权限。
    # 函数用途: 保存原输入依赖所需的最小领域资源。
    def __init__(self, store, owner, quota, load_child, upsert_wake, *, commit=None, blocking=True, runtime_db=None):
        self.store, self.owner, self.quota = store, owner, quota
        self.load_child, self.upsert_wake = load_child, upsert_wake
        self.commit, self.blocking = commit, blocking
        self.runtime_db = runtime_db

    # LLM: 有commit必须每次消费原门；默认Count0直接调用，不从对象字段推断许可。
    # 函数用途: 在本次真实提交入口中完成一个原同步RMW。
    def perform(self, operation):
        return operation() if self.commit is None else self.commit(operation)

    # LLM: 沿原SessionTaskStore和SHA256定位，不从请求提供路径或创建目录。
    # 函数用途: 返回原输入行唯一位置。
    def path(self, input_id):
        return input_dependency_path_from_store(self.store, input_id)

    # LLM: 新退休/收口效果只能消费准确canonical行；schema/owner/root/id/已知状态和bool armed全核，未知不等于无pending。
    # 函数用途: 读取一项已登记输入，拒绝坏账或其它归属。
    def read(self, input_id, *, task_id=''):
        report = read_json_object_report(self.path(input_id),context='task_input.canonical')
        if report.load_error:
            raise ValueError('输入依赖规范记录不可核验')
        return validate_input_dependency_record(report.payload, self.owner, input_id, task_id=task_id)

    # LLM: quota→输入锁及原序列化/私有写不变；新C只缩短锁等待，配额扫描仍在原锁内严格执行。
    # 函数用途: 在原owner短提交内更新一条输入记录。
    def mutate(self, input_id, update, *, admission=None):
        path = self.path(input_id)
        # LLM: 回调不跨线程/网络，仅在原门内拿原quota和记录锁。
        # 函数用途: 执行同一原输入RMW并保留读取失败。
        def commit():
            quota = nullcontext(admission) if admission is not None else self.quota.admission(blocking=self.blocking)
            with quota as admitted, locked_json_path(path, blocking=self.blocking):
                report = read_json_object_report(path, context='task_input.read')
                if report.load_error:
                    raise ValueError('输入依赖记录不可读')
                row = update(dict(report.payload or {}))
                text = json.dumps(row, ensure_ascii=False)
                admitted.check([OwnerQuotaChange(path, len(text.encode()))])
                write_private_text_file_atomic_unlocked(path, text)
            return row
        return self.perform(commit)

    # LLM: current是原canonical事实校验，不授新执行；坏任务/owner/child/SessionTask继续原关闭失败。
    # 函数用途: 核对输入是否仍绑定原活动任务。
    def current(self, row):
        if row.get('owner') != asdict(self.owner):
            return False
        scope = row.get('scope') or {}
        try:
            link, error = self.store.tasks.load_report(scope['root_task_id'])
            if error or link is None or link.status != 'active':
                return False
            thread, error = self.store.threads.load_report(scope['thread_id'])
            if error or thread is None:
                return False
            if scope.get('agent_kind') == 'subagent':
                task = self.load_child(scope['run_id'])
                if task.root_id != scope['root_task_id'] or task.agent_thread_id != scope['thread_id']:
                    return False
                if str(task.status).upper() in {'CANCELLED','FAILED','DONE','TAKEN_OVER','SUPERSEDED'}:
                    return False
            session_task = self.store.session_tasks.load(scope['run_id'])
            if session_task is not None and session_task.status in {'cancelled','failed','done'}:
                return False
            return True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            return False

    # LLM: refs依旧原TaskStore updater/32槽规则；只传blocking，不能修改task状态或另建引用账。
    # 函数用途: 在原门内维护同一任务的输入查找引用。
    def update_refs(self, task_id, input_id, *, remove=False):
        from .input_dependency_index import update_task_input_refs_from_store
        return self.perform(lambda: update_task_input_refs_from_store(self.store, task_id, input_id,
            remove=remove, blocking=self.blocking))

    # LLM: 只读准确task refs，Count0/W共用；到期只resolve原输入，无新线程或范围，整拍最后重排deadline。
    # 函数用途: 对账一项任务已登记的必要输入，并补原恢复投影。
    def reconcile(self, task_id, *, now=None, schedule=True, admission=None):
        if not task_id:
            return
        current = time.time() if now is None else now
        link, error = self.store.tasks.load_report(task_id)
        if error:
            raise ValueError('输入任务记录不可读')
        if link is None:
            return
        for input_id in link.input_dependency_refs:
            row = self.read(input_id,task_id=task_id)
            if row['status'] == 'pending' and (current >= row['expires_at'] or not self.current(row)):
                self.resolve(input_id,status='expired' if current >= row['expires_at'] else 'cancelled',reschedule=False,admission=admission)
            elif row.get('armed') and row['status'] != 'pending':
                self.publish(row,reschedule=False)
        if schedule:
            self.schedule(task_id)

    # LLM: 原row终态单调；ready仍在锁内核期限和current，成功才发布原输入，不生成新任务或执行。
    # 函数用途: 保存原决定并补齐既有恢复投影。
    def resolve(self, input_id, *, status='ready', result_refs=(), reschedule=True, admission=None):
        if status not in _TERMINAL_INPUT_STATUSES:
            raise ValueError('输入决定无效')
        # LLM: 同决定重放不改期限/结果，异决定拒绝。
        # 函数用途: 在原记录锁内推进一次原终态。
        def update(row):
            if not row:
                return _missing()
            if row['status'] != 'pending':
                if row['status'] != status:
                    raise ValueError('原输入依赖已结束')
                return row
            if status == 'ready' and (time.time() >= row['expires_at'] or not self.current(row)):
                raise ValueError('原任务或输入期限已失效')
            return {**row,'status':status,'resolved_at':time.time(),'result_refs':list(result_refs)}
        return self.publish(self.mutate(input_id, update, admission=admission), reschedule=reschedule)

    # LLM: 原armed/current才去重发布；Wake成功后refs/deadline仍各自原提交，失败保部分事实供同键重放。
    # 函数用途: 沿原唯一Wake恢复输入所属任务，不能跳过任何原投影。
    def publish(self, row, *, reschedule=True):
        if row.get('armed') and row['status'] != 'pending' and self.current(row):
            scope = row['scope']
            thread_id = scope['thread_id']
            if scope['agent_kind'] == 'subagent':
                link, _ = self.store.tasks.load_report(scope['root_task_id'])
                thread_id = link.thread_id
            request = {'thread_id':thread_id, 'root_task_id':scope['root_task_id'], 'reason':'task_input_resolved',
                'source_agent_id':scope['run_id'], 'evidence_refs':row.get('result_refs',[]),
                'metadata':{'schema_version':'task-input-event.v1','input_id':row['input_id'],
                    'input_status':row['status'],'input_provider':row['provider'],'input_run_id':scope['run_id'],
                    'conversation_request_id':row.get('conversation_request_id',scope['run_id'])},
                'dedupe_key':'task-input:'+row['input_id'],'retain_handled':True}
            self.perform(lambda:self.store.wakes.raise_signal(request, blocking=self.blocking))
        if row['status'] != 'pending':
            self.update_refs(row['scope']['root_task_id'], row['input_id'], remove=True)
            if reschedule:
                self.schedule(row['scope']['root_task_id'])
        return row

    # LLM: 只读准确refs；无pending时仅CAS退休同root输入期限，坏行不能当空，活claim/重排行不删；仍是唯一RuntimeDB队列。
    # 函数用途: 为尚存输入保留原最近deadline。
    def schedule(self, task_id):
        snapshots = self.runtime_db.input_deadline_wakes(task_id) if self.runtime_db is not None else []
        link, error = self.store.tasks.load_report(task_id)
        if error or link is None:
            raise ValueError('输入任务记录不可读')
        pending = []
        for input_id in getattr(link,'input_dependency_refs',()):
            row = self.read(input_id,task_id=task_id)
            if row['status'] == 'pending':
                pending.append(row['expires_at'])
        if pending and self.upsert_wake is not None:
            self.perform(lambda:self.upsert_wake(root_task_id=task_id, root_thread_id=link.thread_id,
                next_due_at=min(pending), kind='task_input_deadline'))
        elif not pending and self.runtime_db is not None:
            for snapshot in snapshots:
                self.perform(lambda snapshot=snapshot: self.runtime_db.retire_input_deadline(snapshot))


# LLM: 旧入口仅取原Agent依赖，不物化新对象；Count0原callback/初始化/缺repo语义保持。
# 函数用途: 让原调用方复用唯一输入事务实现。
def input_dependency_access(agent):
    manager = getattr(agent,'subagents',None)
    repo = getattr(manager,'runtime_db',None)
    return InputDependencyAccess(agent.conversation_store, owner_identity_from_config(agent.config), getattr(agent,'owner_quota',None),
        getattr(manager,'load',None), getattr(repo,'upsert_wake',None), runtime_db=repo)


# LLM: store/owner/路径由调用方原来源冻结；只组装既有配额、canonical child loader和existing-only RuntimeDB，不创建Agent/owner/新库；commit逐次原位验权。
# 函数用途: 给没有业务Agent的同一owner入口组装完整输入依赖资源，保持原加载、配额和Wake算法。
def input_dependency_owner_resources(store, owner, *, owner_home, subagent_workspace, commit, blocking):
    from functools import partial

    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path
    from ..subagents.services.persistence.service import load_subagent_task
    from ..user_space.owner_quota import owner_quota_enforcer_from_policy

    quota = owner_quota_enforcer_from_policy(owner_home)
    repo = RuntimeRepository(runtime_db_path(owner_home), existing_only=True)
    access = InputDependencyAccess(store, owner, quota, partial(load_subagent_task, subagent_workspace),
        repo.upsert_wake, commit=commit, blocking=blocking, runtime_db=repo)
    return access, repo


# LLM: 路径公式仅此一处，Store必须来自原owner解析；无Agent/current/quota副作用。
# 函数用途: 为原包装和显式领域服务定位同一输入行。
def input_dependency_path_from_store(store, input_id):
    return store.session_tasks.storage.session_tasks_dir / 'inputs' / (sha256(input_id.encode()).hexdigest()+'.json')


# LLM: 原同一pending/current算法用于显式领域资源，读取失败不补输入或猜任务。
# 函数用途: 核对原凭据输入仍待处理。
def credential_submission_current_from_access(inputs, row):
    dependency = read_json_object_report(inputs.path(row.get('input_id', row['credential_ref'])), context='credential.input').payload
    return bool(dependency and dependency['status'] == 'pending' and inputs.current(dependency))
