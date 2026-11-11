# LLM: 所有正文以Owner根nofollow读写并共用run.json锁；同线程IO复用已持锁，执行锁不可重复进入，不授予执行权。
# 模块用途: 脱敏并原子保存运行进度、冻结定义与步骤快照，支持跨平台互斥及断点恢复。
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import local

from ..common.cancellation import raise_if_cancelled
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import ensure_private_dir, read_text_beneath, write_text_atomic_beneath
from ..concurrency.interrupt import is_interrupted
from .models import (
    WorkflowRunRecord,
    payload,
    redact_bearer_values,
    safe_path,
)

_HELD_LOCKS = local()


# LLM: 仅投影同一原运行事实；兼容公共DTO增量字段尚未集成的读者，不建立第二个运行账或状态机。
# 函数用途: 给原记录附上实际阶段、定义引用与已确认成功前缀，返回可序列化回执。
def execution_receipt(record, *, phase=None, effect_outcome=None, definition_ref=None):
    for name, value in (('phase', phase), ('effect_outcome', effect_outcome), ('execution_definition_ref', definition_ref)):
        if value is not None:
            setattr(record, name, value)
    confirmed = []
    for step in record.steps:
        if step.status not in ('succeeded', 'escalated_recovered', 'skipped'):
            break
        try:
            payload(step)
        except (TypeError, ValueError, OverflowError):
            break
        confirmed.append(step.step_id)
    record.confirmed_step_refs = tuple(confirmed)
    return {name: getattr(record, name, default) for name, default in (
        ('workflow_run_id', ''), ('execution_definition_ref', ''), ('phase', ''),
        ('effect_outcome', ''), ('confirmed_step_refs', ()), ('breakpoint', None))}


# LLM: 旧DTO上的增量属性不由dataclasses.replace携带；只复制同一原运行的回执，不创造执行事实。
# 函数用途: 替换运行字段时保留定义身份与已记录执行阶段，兼容W12正式字段。
def replace_run_record(record, changes):
    updated = replace(record, **changes)
    for name in ('phase', 'effect_outcome', 'execution_definition_ref'):
        if name not in changes and hasattr(record, name):
            setattr(updated, name, getattr(record, name))
    execution_receipt(updated)
    return updated


# LLM: 原DTO与新增执行字段一起进入原run.json；不从展示文案恢复执行事实。
# 函数用途: 序列化原运行和阶段回执。
def _run_payload(record):
    return {**payload(record), **execution_receipt(record)}


# LLM: 已开始后的失败携同一个record与cause，工具不得根据ValueError/OSError重新猜是否执行。
# 类用途: 将步骤/终态发表失败的原运行回执交给工具和调用方，不触发脚本重跑。
class WorkflowExecutionError(RuntimeError):
    # LLM: 错误包装不得再次依赖坏步骤可序列化；降级回执仍来自原run和已确认前缀。
    # 函数用途: 给异常出口保留完整记录，坏输出时提供可安全传递的最小回执。
    def __init__(self, record, error):
        super().__init__(str(error))
        self.record = record
        try:
            self.receipt = _run_payload(record)
        except (TypeError, ValueError, OverflowError):
            self.receipt = {'workflow_id': record.workflow_id, 'status': record.status,
                'needs_repair': record.needs_repair, **execution_receipt(record)}
        self.error_code = 'WORKFLOW_EXECUTION_FAILED'


def _concurrency_settled(record):
    """终态标签不等于义务消失；只消费原步骤上的完成、取消或未提交证明。"""
    if record.status in ('queued', 'running', 'waiting_for_agent', 'cancelling'):
        return False
    for step in record.steps:
        result = step.escalation_result
        if result.get('cleanup_error'):
            return False
        if step.mode == 'agent_task':
            obligation = (step.status == 'waiting_for_agent' or result.get('task_id')
                or 'session_task_dispatch' in result or result.get('submission_state') == 'unknown'
                or result.get('effect_started') and result.get('submission_state') != 'not_started')
            cancellation = result.get('cancellation_receipt', {})
            settled = (bool(result.get('completion_ref')) or cancellation.get('state') == 'completed'
                and cancellation.get('outstanding_refs') == [] or result.get('submission_state') == 'not_started'
                and result.get('reconciliation_state') == 'absent')
            if obligation and not settled:
                return False
        elif result.get('effect_started') and step.status not in ('succeeded', 'escalated_recovered', 'skipped'):
            return False
    return record.status in ('succeeded', 'failed', 'skipped', 'cancelled') or record.effect_outcome == 'not_started'


# LLM: RuntimeError是新执行锁合同；同时保留旧引擎调用方捕获ValueError的兼容性。
# 类用途: 明确说明同一工作流运行已被其它执行者持有。
class _RunBusy(RuntimeError, ValueError):
    pass


# LLM: 未启动与已结清各自需要完整状态证据，queued 标签本身不能代表没有委派责任。
# 函数用途: 判定一份持久运行快照是否允许归还未启动的占位。
def _unstarted_or_settled(record):
    return (record.status == 'queued' and record.effect_outcome == 'not_started'
        and all(step.status == 'pending' and not step.escalation_result for step in record.steps)
        or _concurrency_settled(record))


# LLM: 调用方已持run.json锁，正文与中间目录始终从Owner锚点nofollow；先复制脱敏，绝不修改调用方内存。
# 函数用途: 将结构化记录以0600原子写入账本。
def _write_json(root, parts, value):
    raw = json.dumps(redact_bearer_values(payload(value)), ensure_ascii=False, indent=2, allow_nan=False)
    if parts[-1] == 'run.json' and value.get('status') == 'succeeded':
        # 序列化和步骤写入也可能耗时；在真实终态写入口再核同一停止事实。
        raise_if_cancelled()
        if is_interrupted():
            raise InterruptedError('workflow interrupted')
    if len(parts) == 4 and parts[:2] == ('data', 'workflow_runs') and parts[-1] == 'run.json':
        from .run_list_index import commit_run
        commit_run(root, value, lambda: write_text_atomic_beneath(root, parts, raw + '\n'))
    else:
        write_text_atomic_beneath(root, parts, raw + '\n')


# LLM: Store仅过滤运行外层扩展，步骤恢复一律委托模型权威合同。
# 函数用途: 恢复强类型运行记录，不重复维护步骤字段和状态枚举。
def _decode_run(data):
    values = {key: value for key, value in data.items() if key in WorkflowRunRecord.__dataclass_fields__}
    record = WorkflowRunRecord.from_dict(values)
    for name in ('phase', 'effect_outcome', 'execution_definition_ref'):
        if name in data:
            setattr(record, name, data[name])
    execution_receipt(record)
    return record


# LLM: 运行编号先单段验证，正文IO不能依赖safe_path预检；锁与数据分工，根路径只取宿主构造参数。
# 类用途: 管理Owner下的冻结工作流定义和可恢复运行账本。
class WorkflowRunStore:
    # LLM: owner_home 必须是宿主已核验的路径，不由请求中的 owner_id 推导。
    # 函数用途: 绑定工作流运行账本的根位置。
    def __init__(self, owner_home):
        self.owner_home = Path(owner_home).absolute()
        self.root = safe_path(owner_home, 'data', 'workflow_runs')

    # LLM: 每次访问重新检查整条本地账本路径，不跟随根以下的符号链接。
    # 函数用途: 校验运行编号并返回其目录。
    def _folder(self, workflow_run_id):
        return safe_path(self.owner_home, 'data', 'workflow_runs', workflow_run_id)

    # LLM: 目录仅是兼容展示路径，不允许调用方以此绕开本Store的nofollow读写。
    # 函数用途: 返回已验证的运行目录。
    def run_dir(self, workflow_run_id):
        return self._folder(workflow_run_id)

    # LLM: 已持锁集合限定进程和线程，同线程普通IO可重入；执行准入总是非阻塞并拒绝重入。
    # 函数用途: 让整次执行与每次账本读改写共用同一把跨平台JSON锁。
    @contextmanager
    def _lock(self, workflow_run_id, *, execution=False):
        folder = self.run_dir(workflow_run_id)
        path = folder / 'run.json'
        key = (os.getpid(), os.path.normcase(str(path)))
        held = getattr(_HELD_LOCKS, 'paths', set())
        if key in held:
            if execution:
                raise _RunBusy('工作流正在执行，不能重复续跑')
            yield
            return
        ensure_private_dir(folder, canonical_root=self.owner_home)
        try:
            with locked_json_path(path, blocking=not execution):
                _HELD_LOCKS.paths = held | {key}
                try:
                    yield
                finally:
                    _HELD_LOCKS.paths = held
        except BlockingIOError as exc:
            raise _RunBusy('工作流正在执行，运行锁繁忙') from exc

    # LLM: name只由固定文件名与安全单段编号组成；缺失记录不能冒充空JSON。
    # 函数用途: 从Owner根读取一个工作流运行账本对象。
    def _read(self, workflow_run_id, name):
        self._folder(workflow_run_id)
        raw = read_text_beneath(self.owner_home, ('data', 'workflow_runs', workflow_run_id, name))
        if raw is None:
            raise FileNotFoundError('工作流运行记录不存在')
        return json.loads(raw)

    # LLM: 文件名要校验加上.json后的长度，避免先创建残缺运行；所有写都复用本次运行锁。
    # 函数用途: 原子保存一个步骤的脱敏副本。
    def _write_step(self, workflow_run_id, step):
        safe_path(self.root, step.step_id + '.json')
        _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'steps', step.step_id + '.json'), step)

    # LLM: 排他创建防同编号覆盖；步骤与冻结定义均由引擎提供的结构化值保存。
    # 函数用途: 新建一个运行及各步初始快照。
    def create_run(self, record, workflow_snapshot=None):
        payload(record)
        for step in record.steps:
            safe_path(self.root, step.step_id + '.json')
        with self._lock(record.workflow_run_id):
            raw = read_text_beneath(self.owner_home, ('data', 'workflow_runs', record.workflow_run_id, 'run.json'))
            if raw is not None:
                raise ValueError('运行编号已存在')
            # DTO 可复制来创建不同运行；新行的 CAS 起点不能继承另一个 ID 的修订。
            record.revision = 0
            self.save_run(record, workflow_snapshot)

    # LLM: 仅引擎在启动、恢复和结束时调用；定义只在首次传入时替换，不跟随资产后来修改。
    # 函数用途: 保存运行全貌与所有步骤快照。
    def save_run(self, record, workflow_snapshot=None, *, expected_revision=None, resuming=False):
        for step in record.steps:
            safe_path(self.root, step.step_id + '.json')
        with self._lock(record.workflow_run_id):
            parts = ('data', 'workflow_runs', record.workflow_run_id, 'run.json')
            raw = read_text_beneath(self.owner_home, parts)
            data = json.loads(raw) if raw is not None else {}
            revision = data.get('revision', 0)
            if (record.revision != revision or expected_revision is not None and expected_revision != revision):
                raise ValueError('WORKFLOW_RUN_REVISION_CONFLICT')
            if (data.get('status') == 'cancelled' and record.status != 'cancelled'
                    or data.get('status') in ('succeeded', 'skipped') and record.status != data['status'] and not resuming
                    or data.get('status') == 'failed' and record.status in ('queued', 'running', 'waiting_for_agent') and not resuming):
                raise ValueError('WORKFLOW_RUN_TERMINAL')
            for name in ('business_context', 'scope_snapshot', 'scope_revision'):
                if raw is not None and data.get(name, {} if name != 'scope_revision' else 0) != getattr(record, name):
                    raise ValueError('WORKFLOW_RUN_BINDING_IMMUTABLE')
            data.update(_run_payload(record))
            data['revision'] = revision + 1
            if workflow_snapshot is not None:
                data['workflow_snapshot'] = payload(workflow_snapshot)
            for step in record.steps:
                self._write_step(record.workflow_run_id, step)
            _write_json(self.owner_home, parts, data)
            record.revision = revision + 1

    # LLM: 先发表独立快照再更新运行；全部终态拒绝普通晚到改写，显式恢复不允许复活cancelled。
    # 函数用途: 保存已存在步骤并推进位置，保留终态保护及同状态运行元数据更新。
    def save_step_result(self, workflow_run_id, step_record, *, resuming=False):
        with self._lock(workflow_run_id):
            record = self.load_run(workflow_run_id)
            previous = next((s for s in record.steps if s.step_id == step_record.step_id), None)
            changed = previous is not None and payload(previous) != payload(step_record)
            if changed and (record.status == 'cancelled' or record.status in ('succeeded', 'failed', 'skipped') and not resuming):
                raise ValueError('WORKFLOW_RUN_TERMINAL')
            if step_record.step_id not in {step.step_id for step in record.steps}:
                raise ValueError('运行中不存在该步骤')
            self._write_step(workflow_run_id, step_record)
            record = replace_run_record(record, dict(current_step_id=step_record.step_id,
                             steps=tuple(step_record if s.step_id == step_record.step_id else s for s in record.steps)))
            data = self._read(workflow_run_id, 'run.json')
            execution_receipt(record, phase='step_publish', effect_outcome=(
                'unknown' if step_record.escalation_result.get('effect_started')
                or step_record.status not in ('pending', 'running') else getattr(record, 'effect_outcome', 'not_started')))
            data.update(_run_payload(record))
            data['revision'] = record.revision + 1
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            record.revision = data['revision']
            return record

    def begin_control(self, workflow_run_id, *, action, operation_key, expected_revision, intent=None):
        """恢复/取消沿原 run.json 记同键意图，不能用 updated_at 或第二任务账当版本。"""
        if not isinstance(operation_key, str) or not 0 < len(operation_key.encode()) <= 256:
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=False,
                                                 allow_nan=False).encode()).hexdigest()
        with self._lock(workflow_run_id):
            data = self._read(workflow_run_id, 'run.json')
            operations = data.setdefault('control_operations', {})
            previous = operations.get(operation_key)
            if previous is not None:
                if (previous['action'] != action or previous['expected_revision'] != expected_revision
                        or previous.get('intent') != fingerprint):
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return previous['state'] == 'completed', (_decode_run(previous['receipt'])
                    if previous.get('receipt') else self.load_run(workflow_run_id))
            if type(expected_revision) is not int or expected_revision != data.get('revision', 0):
                raise ValueError('WORKFLOW_RUN_REVISION_CONFLICT')
            operations[operation_key] = {'action': action, 'intent': fingerprint,
                                        'expected_revision': expected_revision, 'state': 'pending'}
            data['revision'] = data.get('revision', 0) + 1
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            return False, self.load_run(workflow_run_id)

    def complete_control(self, workflow_run_id, operation_key):
        with self._lock(workflow_run_id):
            data = self._read(workflow_run_id, 'run.json')
            operation = data['control_operations'][operation_key]
            if operation['state'] != 'completed':
                operation['state'] = 'completed'
                data['revision'] = data.get('revision', 0) + 1
                receipt = _run_payload(self.load_run(workflow_run_id))
                receipt['revision'] = data['revision']
                operation['receipt'] = receipt
                _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            return _decode_run(operation['receipt']) if operation.get('receipt') else self.load_run(workflow_run_id)

    # LLM: 序号先在原run锁内持久预留再发事件；revision与调用者同步，崩溃只允许序号有缺口而不重复。
    # 函数用途: 为同一运行领取一个耐久单调的进度序号。
    def reserve_progress_seq(self, workflow_run_id):
        with self._lock(workflow_run_id):
            data = self._read(workflow_run_id, 'run.json')
            data['progress_seq'] = data.get('progress_seq', 0) + 1
            data['revision'] = data.get('revision', 0) + 1
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            return data['progress_seq'], data['revision']

    # LLM: 准入回执附着原run，不更改run CAS或授予执行权；独立叶文件避免与已启动worker的步骤写回竞争。
    # 函数用途: 原run锁内保存一次可供同键重试读取的并发决策。
    def save_admission_receipt(self, workflow_run_id, receipt):
        value = payload(receipt)
        if not isinstance(value, dict) or value.get('status') not in ('queued', 'skipped'):
            raise ValueError('并发准入回执必须是queued或skipped对象')
        with self._lock(workflow_run_id):
            self._read(workflow_run_id, 'run.json')
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'admission.json'), value)

    # LLM: 仅读原run的说明性回执，缺失不推断准入或重建callback；终态仍只以原run.json为准。
    # 函数用途: 为业务幂等命中恢复原并发回执。
    def load_admission_receipt(self, workflow_run_id):
        with self._lock(workflow_run_id):
            self._read(workflow_run_id, 'run.json')
            try:
                return self._read(workflow_run_id, 'admission.json')
            except FileNotFoundError:
                return None

    def _concurrency_claim(self, lease):
        """精确比对原 run 的占位证明；私有元数据不推进业务 CAS。"""
        data = self._read(lease.record['run_id'], 'run.json')
        proof = data.get('concurrency_claim')
        if (data.get('workflow_run_id') != lease.record['run_id']
                or data.get('workflow_id') != lease.record.get('asset_id') or not isinstance(proof, dict)
                or proof.get('schema') != 'workflow-concurrency-lifecycle.v1'
                or proof.get('record') != lease.record or proof.get('stamp') != list(lease.stamp)):
            raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
        return data, proof

    # LLM: 只核原作用域内的完整记录与文件身份；缺失与被新代替换不等于原租约仍持有叶。
    # 函数用途: 验证租约格式，并查询当前叶是否仍属于该准确代次。
    def _claim_leaf_holds(self, lease):
        from ..common.nofollow_fs import open_directory_beneath
        from .concurrency_policy import _claim, _scope
        record = lease.record
        if (not isinstance(record, dict) or record.get('schema') != 'workflow-concurrency-claim.v1'
                or record.get('lifecycle') != 'workflow' or record.get('policy') not in ('skip_if_running', 'queue')
                or type(record.get('nonce')) is not str or len(record['nonce']) != 32
                or any(c not in '0123456789abcdef' for c in record['nonce'])
                or len(lease.stamp) != 2 or any(type(n) is not int or n < 0 for n in lease.stamp)):
            raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
        scope = _scope(self.owner_home, 'workflow', record.get('asset_id'), record.get('run_id'))
        try:
            parent = open_directory_beneath(scope.root, scope.parts, require_dir_fd=True)
        except FileNotFoundError:
            return False
        try:
            current = _claim(scope, parent)
            return current is not None and current.stamp == lease.stamp and current.record == record
        finally:
            os.close(parent)

    # LLM: 调用方持非阻塞运行锁及资产 gate；只有彻底缺证明、准确叶、死亡身份与无未结工作可恢复。
    # 函数用途: 为创建后尚未绑定就退出的原代次补写可释放证明，不启动回调。
    def _recover_bootstrap_claim(self, lease):
        data = self._read(lease.record['run_id'], 'run.json')
        if 'concurrency_claim' in data:
            return
        self._release_unbound_claim(data, lease)

    # LLM: 前代必须已由严格 reader 确认 released；只能结清另一准确死亡新代，不覆盖任何旧未决责任。
    # 函数用途: 恢复已完成运行续跑在新代绑定前退出的交接窗口。
    def _recover_released_bootstrap_claim(self, lease):
        data = self._read(lease.record['run_id'], 'run.json')
        previous = data.get('concurrency_claim')
        if not isinstance(previous, dict) or previous.get('phase') != 'released':
            return
        old = self._previous_claim_lease(data, lease)
        if old == lease or self._claim_leaf_holds(old):
            return
        self._release_unbound_claim(data, lease)

    # LLM: 当前run/asset先锚定，再允许严格reader按旧租约取数；外来合法proof不能借另一个run通过。
    # 函数用途: 从当前规范账本取得并核验同一运行的旧租约，不产生任何写入。
    def _previous_claim_lease(self, data, lease):
        from .concurrency_policy import _Lease
        proof = data.get('concurrency_claim')
        record = proof.get('record') if isinstance(proof, dict) else None
        if (data.get('workflow_run_id') != lease.record.get('run_id')
                or data.get('workflow_id') != lease.record.get('asset_id') or not isinstance(record, dict)
                or record.get('run_id') != data['workflow_run_id']
                or record.get('asset_id') != data['workflow_id']):
            raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
        try:
            old = _Lease(record, tuple(proof['stamp']))
            self._concurrency_claim(old)
        except (KeyError, TypeError) as exc:
            raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH') from exc
        return old

    # LLM: 两种 bootstrap 分支共用准确叶、身份与双快照义务核验，构造规则与绑定失败清理一致。
    # 函数用途: 为已证未启动或结清的死亡新代持久化待释放证明。
    def _release_unbound_claim(self, data, lease):
        if (data.get('workflow_run_id') != lease.record.get('run_id')
                or data.get('workflow_id') != lease.record.get('asset_id') or not self._claim_leaf_holds(lease)):
            raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
        from .concurrency_policy import _identity_live
        if _identity_live(lease.record.get('identity')) is not False:
            return
        # 步骤独立叶与 run.json 任一仍记未结责任，都不能靠另一份前像覆盖来放行。
        if not all(_unstarted_or_settled(record) for record in (
                _decode_run(data), self.load_run(lease.record['run_id']))):
            return
        data['concurrency_claim'] = {'schema': 'workflow-concurrency-lifecycle.v1',
            'record': payload(lease.record), 'stamp': list(lease.stamp), 'phase': 'release_pending'}
        _write_json(self.owner_home, ('data', 'workflow_runs', lease.record['run_id'], 'run.json'), data)

    # LLM: 新绑定和失败清理都先锚定并严格核验已有证明；released也不能借外来记录跳过核验。
    # 函数用途: 核验同运行前代，在旧叶已消失或换代后以一次重试收敛原 release_pending。
    def _settle_previous_release(self, data, lease):
        if 'concurrency_claim' not in data:
            return data
        old = self._previous_claim_lease(data, lease)
        previous = data.get('concurrency_claim')
        if previous['phase'] != 'release_pending':
            return data
        if self._claim_leaf_holds(old):
            return data
        for attempt in range(2):
            try:
                self._ack_concurrency_release(old)
                break
            except OSError:
                if attempt:
                    raise
        return self._read(old.record['run_id'], 'run.json')

    # LLM: 只在原run锁内绑定同Owner/资产/租约；callback身份来自本机原语，恢复不沿用老进程身份。
    # 函数用途: 将准确占位和当前回调进程写入原运行，不改变业务版本或执行结果。
    def _bind_concurrency_claim(self, lease):
        """在执行前将准确 nonce/文件身份附到原账本，不授予新的执行权限。"""
        run_id = lease.record['run_id']
        with self._lock(run_id):
            data = self._settle_previous_release(self._read(run_id, 'run.json'), lease)
            previous = data.get('concurrency_claim')
            if data.get('workflow_run_id') != run_id or data.get('workflow_id') != lease.record.get('asset_id'):
                raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
            if previous and previous.get('phase') != 'released':
                self._concurrency_claim(lease)
                if previous.get('phase') != 'retained':
                    raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_BUSY')
            from .concurrency_policy import _self_identity
            identity = _self_identity()
            if identity is None:
                raise ValueError('WORKFLOW_CONCURRENCY_IDENTITY_UNAVAILABLE')
            data['concurrency_claim'] = {'schema': 'workflow-concurrency-lifecycle.v1',
                'record': payload(lease.record), 'stamp': list(lease.stamp), 'phase': 'executing',
                'callback_identity': identity}
            _write_json(self.owner_home, ('data', 'workflow_runs', run_id, 'run.json'), data)

    # LLM: 调用栈已证明本次callback未进入；读回已提交绑定，只清同代或已释放前代，未知原责任保持占用。
    # 函数用途: 绑定失败时先持久化准确清理证明，或把原运行未结义务恢复为retained。
    def _abort_concurrency_binding(self, lease):
        run_id = lease.record['run_id']
        with self.execution_lock(run_id):
            data = self._settle_previous_release(self._read(run_id, 'run.json'), lease)
            if data.get('workflow_run_id') != run_id or data.get('workflow_id') != lease.record.get('asset_id'):
                raise ValueError('WORKFLOW_CONCURRENCY_CLAIM_MISMATCH')
            previous = data.get('concurrency_claim')
            if previous and previous.get('phase') != 'released':
                self._concurrency_claim(lease)
            record = self.load_run(run_id)
            unstarted = (record.status == 'queued' and record.effect_outcome == 'not_started'
                and all(step.status == 'pending' and not step.escalation_result for step in record.steps))
            phase = 'release_pending' if unstarted or _concurrency_settled(record) else 'retained'
            if not previous or previous.get('phase') == 'released':
                data['concurrency_claim'] = {'schema': 'workflow-concurrency-lifecycle.v1',
                    'record': payload(lease.record), 'stamp': list(lease.stamp), 'phase': phase}
            else:
                previous['phase'] = phase
            _write_json(self.owner_home, ('data', 'workflow_runs', run_id, 'run.json'), data)
            return phase

    # LLM: 本次callback确已退出；已发表release_pending/released不倒退，原步骤责任仍是唯一结算依据。
    # 函数用途: 在删除占位之前把退出与义务结算结果保存在原run。
    def _finish_concurrency_claim(self, lease):
        """回调返回后仍核规范步骤义务；未知责任保留，占位删除前先持久化清理证明。"""
        run_id = lease.record['run_id']
        with self._lock(run_id):
            data, proof = self._concurrency_claim(lease)
            if proof['phase'] in ('release_pending', 'released'):
                return proof['phase']
            phase = 'release_pending' if _concurrency_settled(self.load_run(run_id)) else 'retained'
            if proof['phase'] != phase:
                proof['phase'] = phase
                _write_json(self.owner_home, ('data', 'workflow_runs', run_id, 'run.json'), data)
            return phase

    # LLM: 准入非阻塞核原run和完整租约；可靠进程退出才恢复，非终态或未知委派责任只保留retained。
    # 函数用途: 查询准确阶段，并按需恢复死亡回调留下的规范清理证明。
    def _concurrency_claim_phase(self, lease, *, recover_exited=False):
        # 准入门不能等待正持运行锁的回调，否则等待者无法交还回调所等的事件。
        with self.execution_lock(lease.record['run_id']):
            if recover_exited:
                self._recover_released_bootstrap_claim(lease)
                self._recover_bootstrap_claim(lease)
            data, proof = self._concurrency_claim(lease)
            if recover_exited and proof['phase'] in ('executing', 'retained'):
                from .concurrency_policy import _identity_live
                identity = proof.get('callback_identity', lease.record.get('identity'))
                if _identity_live(identity) is False:
                    record = self.load_run(lease.record['run_id'])
                    phase = ('release_pending' if record.status in ('succeeded', 'failed', 'skipped', 'cancelled')
                        and _concurrency_settled(record) else 'retained')
                    if proof['phase'] != phase:
                        proof['phase'] = phase
                        _write_json(self.owner_home, ('data', 'workflow_runs', lease.record['run_id'], 'run.json'), data)
            return proof['phase']

    # LLM: ACK可在替换后报错重放；已确认同代不重复写，不接受其它阶段或不同租约。
    # 函数用途: 幂等确认已不再持叶的准确释放代次。
    def _ack_concurrency_release(self, lease):
        """准确占位已不在当前叶后才确认；未确认的证明继续保存在原 run。"""
        run_id = lease.record['run_id']
        with self._lock(run_id):
            data, proof = self._concurrency_claim(lease)
            if proof['phase'] == 'released':
                return
            if proof['phase'] != 'release_pending':
                raise ValueError('WORKFLOW_CONCURRENCY_RELEASE_UNCONFIRMED')
            proof['phase'] = 'released'
            _write_json(self.owner_home, ('data', 'workflow_runs', run_id, 'run.json'), data)

    def _concurrency_policy(self, workflow_run_id):
        with self._lock(workflow_run_id):
            proof = self._read(workflow_run_id, 'run.json').get('concurrency_claim', {})
            return proof.get('record', {}).get('policy', 'allow')

    # LLM: 兼容任务指定的步骤保存名，不新增另一套存储格式或锁。
    # 函数用途: 通过同一原子保存链写入步骤记录。
    def save_step_record(self, workflow_run_id, step_record):
        return self.save_step_result(workflow_run_id, step_record)

    # LLM: 独立步骤快照优先；只读已登记的步骤，不因目录里多出文件执行新步骤。
    # 函数用途: 加载运行并补齐最后一次原子发表的步骤结果。
    def load_run(self, workflow_run_id):
        with self._lock(workflow_run_id):
            data = self._read(workflow_run_id, 'run.json')
            if data['workflow_run_id'] != workflow_run_id:
                raise ValueError('运行身份与目录不匹配')
            for index, step in enumerate(data.get('steps', ())):
                safe_path(self.root, step['step_id'] + '.json')
                raw = read_text_beneath(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'steps', step['step_id'] + '.json'))
                if raw is not None:
                    snapshot = json.loads(raw)
                    if snapshot['step_id'] != step['step_id']:
                        raise ValueError('步骤身份与目录不匹配')
                    data['steps'][index] = snapshot
            return _decode_run(data)

    # LLM: 只加载run.json登记的步骤，不消费额外叶子，不把磁盘文件名当新的执行指令。
    # 函数用途: 按运行定义顺序返回恢复后的步骤记录。
    def load_step_records(self, workflow_run_id):
        return self.load_run(workflow_run_id).steps

    # LLM: 只能读取创建该运行时冻结的工作流，缺快照明确拒绝，不回退到最新资产定义。
    # 函数用途: 给断点恢复提供同一次工作流的原始定义。
    def load_workflow_snapshot(self, workflow_run_id):
        with self._lock(workflow_run_id):
            return self._read(workflow_run_id, 'run.json')['workflow_snapshot']

    # LLM: 使用共用JSON锁而非直接fcntl；非阻塞失败抛RuntimeError，内部读写复用本线程已经持有的锁。
    # 函数用途: 防止多个执行者重复续跑同一个工作流运行。
    @contextmanager
    def execution_lock(self, workflow_run_id):
        with self._lock(workflow_run_id, execution=True):
            yield

    # LLM: 索引只选候选，run.json验证事实；迁移每次只修一批，余集精确合并但不整批修复。
    # 函数用途: 按工作流、状态和条数查询有界运行页，不打开独立步骤叶。
    def list_runs(self, workflow_id=None, status=None, limit=50):
        if not isinstance(limit, int) or limit < 0:
            raise ValueError('limit 必须为非负整数')
        if limit == 0:
            return []
        from .run_list_index import RunListIndex
        index = RunListIndex(self.owner_home)
        index.replay(self)
        index.backfill(self)
        # 先固定余集，再取索引候选；并发迁移只能产生可去重的交集，不能产生遗漏缝隙。
        legacy = index.legacy_ids()
        records, seen, after = [], set(), None
        while len(records) < limit:
            candidates = index.page(workflow_id, status, after=after, limit=limit + 1)
            if not candidates:
                break
            for name, sort_time in candidates:
                after = (sort_time, name)
                with self._lock(name):
                    record = _decode_run(self._read(name, 'run.json'))
                if record.workflow_run_id != name:
                    raise ValueError('运行身份与目录不匹配')
                if name not in seen and (workflow_id is None or record.workflow_id == workflow_id) and (
                        status is None or record.status == status):
                    records.append(record)
                    seen.add(name)
                    if len(records) == limit:
                        break
            if len(candidates) < limit + 1:
                break
        # 只有尚未迁移的旧目录需要读余集；每次 backfill 单调缩小它，新运行不进入此队列。
        for name in legacy:
            if name in seen:
                continue
            with self._lock(name):
                raw = read_text_beneath(self.owner_home, ('data', 'workflow_runs', name, 'run.json'))
                if raw is None:
                    continue
                record = _decode_run(json.loads(raw))
            if record.workflow_run_id != name:
                raise ValueError('运行身份与目录不匹配')
            if (workflow_id is None or record.workflow_id == workflow_id) and (status is None or record.status == status):
                records.append(record)
        return sorted(records, key=lambda r: (-r.started_at, r.workflow_run_id))[:limit]

    # LLM: 步骤水位以准确原子快照持久化于原运行各步快照；无全表扫描，共用跨平台 run.json 锁。
    # 函数用途: 保存特定步骤的水位信息 (WF-10)，支持增量读取与断点游标。
    def save_step_watermark(self, workflow_run_id, step_id, watermark):
        safe_path(self.root, step_id + '.json')
        if not isinstance(watermark, dict):
            raise TypeError('watermark 必须是字典')
        with self._lock(workflow_run_id):
            record = self.load_run(workflow_run_id)
            step = next((s for s in record.steps if s.step_id == step_id), None)
            if step is None:
                raise ValueError('运行中不存在该步骤')
            updated_step = replace(step, watermark=payload(watermark))
            self._write_step(workflow_run_id, updated_step)
            record = replace_run_record(record, dict(
                steps=tuple(updated_step if s.step_id == step_id else s for s in record.steps)))
            data = self._read(workflow_run_id, 'run.json')
            data.update(_run_payload(record))
            data['revision'] = record.revision + 1
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            record.revision = data['revision']
            return updated_step

    # LLM: 仅通过工作流运行锁快速读取已持久化步骤水位；不进行无界目录扫描，未找到步骤报 ValueError。
    # 函数用途: 加载步骤持久化水位 (WF-10)，缺失返回 None。
    def load_step_watermark(self, workflow_run_id, step_id):
        safe_path(self.root, step_id + '.json')
        with self._lock(workflow_run_id):
            record = self.load_run(workflow_run_id)
            step = next((s for s in record.steps if s.step_id == step_id), None)
            if step is None:
                raise ValueError('运行中不存在该步骤')
            return step.watermark
