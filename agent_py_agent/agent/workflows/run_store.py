# LLM: 所有正文以Owner根nofollow读写并共用run.json锁；同线程IO复用已持锁，执行锁不可重复进入，不授予执行权。
# 模块用途: 脱敏并原子保存运行进度、冻结定义与步骤快照，支持跨平台互斥及断点恢复。
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import local

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import ensure_private_dir, read_text_beneath, write_text_atomic_beneath
from ..common.nofollow_fs import list_names_beneath as list_dir_names_beneath
from .models import (
    StepExecutionRecord,
    WorkflowRunRecord,
    payload,
    redact_bearer_values,
    safe_path,
)

_HELD_LOCKS = local()


# LLM: RuntimeError是新执行锁合同；同时保留旧引擎调用方捕获ValueError的兼容性。
# 类用途: 明确说明同一工作流运行已被其它执行者持有。
class _RunBusy(RuntimeError, ValueError):
    pass


# LLM: 调用方已持run.json锁，正文与中间目录始终从Owner锚点nofollow；先复制脱敏，绝不修改调用方内存。
# 函数用途: 将结构化记录以0600原子写入账本。
def _write_json(root, parts, value):
    raw = json.dumps(redact_bearer_values(payload(value)), ensure_ascii=False, indent=2, allow_nan=False)
    write_text_atomic_beneath(root, parts, raw + '\n')


# LLM: 运行记录反序列化只认模型字段，步骤单独还原 tuple；不加载可执行对象。
# 函数用途: 把 JSON 运行记录还原成强类型步骤及运行数据。
def _decode_run(data):
    values = {key: value for key, value in data.items() if key in WorkflowRunRecord.__dataclass_fields__}
    values['steps'] = tuple(_decode_step(step) for step in values.get('steps', ()))
    return WorkflowRunRecord(**values)


# LLM: 状态和技能编号保持 JSON/内存往返一致；非法状态明确拒绝。
# 函数用途: 还原一个步骤快照并校验状态。
def _decode_step(data):
    if data['status'] not in ('pending', 'running', 'succeeded', 'skipped', 'failed',
                              'escalated_recovered', 'paused_for_resume'):
        raise ValueError('未知步骤状态')
    return StepExecutionRecord(**{**data, 'resolved_skill_ids': tuple(data.get('resolved_skill_ids', ()))})


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
            self.save_run(record, workflow_snapshot)

    # LLM: 仅引擎在启动、恢复和结束时调用；定义只在首次传入时替换，不跟随资产后来修改。
    # 函数用途: 保存运行全貌与所有步骤快照。
    def save_run(self, record, workflow_snapshot=None):
        for step in record.steps:
            safe_path(self.root, step.step_id + '.json')
        with self._lock(record.workflow_run_id):
            parts = ('data', 'workflow_runs', record.workflow_run_id, 'run.json')
            raw = read_text_beneath(self.owner_home, parts)
            data = json.loads(raw) if raw is not None else {}
            data.update(payload(record))
            if workflow_snapshot is not None:
                data['workflow_snapshot'] = payload(workflow_snapshot)
            for step in record.steps:
                self._write_step(record.workflow_run_id, step)
            _write_json(self.owner_home, parts, data)

    # LLM: 先发表独立快照再更新运行；崩溃窗口由 load_run 读取独立快照弥合。
    # 函数用途: 保存一个已存在步骤的运行中或完成状态，并推进当前步骤位置。
    def save_step_result(self, workflow_run_id, step_record):
        with self._lock(workflow_run_id):
            record = self.load_run(workflow_run_id)
            if step_record.step_id not in {step.step_id for step in record.steps}:
                raise ValueError('运行中不存在该步骤')
            self._write_step(workflow_run_id, step_record)
            record = replace(record, current_step_id=step_record.step_id,
                             steps=tuple(step_record if s.step_id == step_record.step_id else s for s in record.steps))
            data = self._read(workflow_run_id, 'run.json')
            data.update(payload(record))
            _write_json(self.owner_home, ('data', 'workflow_runs', workflow_run_id, 'run.json'), data)
            return record

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

    # LLM: 返回最近运行，过滤条件只读明确字段；损坏记录不静默吞掉。
    # 函数用途: 按工作流、状态和条数查询运行历史。
    def list_runs(self, workflow_id=None, status=None, limit=50):
        if not isinstance(limit, int) or limit < 0:
            raise ValueError('limit 必须为非负整数')
        if limit == 0:
            return []
        records = []
        for name in list_dir_names_beneath(self.owner_home, ('data', 'workflow_runs')):
            with self._lock(name):
                if read_text_beneath(self.owner_home, ('data', 'workflow_runs', name, 'run.json')) is not None:
                    records.append(self.load_run(name))
        records = [r for r in records if (workflow_id is None or r.workflow_id == workflow_id)
                   and (status is None or r.status == status)]
        return sorted(records, key=lambda r: r.started_at, reverse=True)[:limit]
