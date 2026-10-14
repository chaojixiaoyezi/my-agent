# LLM: runtime 拒绝记录按准确 task ID 持久继承；只存审批 binding/原因，不存工具参数、模型正文或秘密，不能赋予授权。
# 模块用途: 保持同任务跨片、Compact 和重启的拒绝事实，避免新的调用 ID 把已拒绝操作重新询问。
from __future__ import annotations

from hashlib import sha256

from ..common.json_io import read_json_object_report
from .input_dependencies import _mutate, input_dependency_path


# LLM: 使用唯一 SessionTaskStore 子目录及独立命名空间，原 run/task 不从自然语言恢复。
# 函数用途: 给同一准确执行任务生成拒绝记录的内部键。
def _key(params):
    task_id = str(params.task_id or params.run_id or '')
    return 'rejections-' + sha256(task_id.encode()).hexdigest() if task_id else ''


# LLM: 原列表是当前工具循环事实；锁内合并持久 binding，未知字段不传播，参数原文永不保存。
# 函数用途: 在模型前恢复、工具归档后保存同任务拒绝；坏账明确失败，不默默清空。
def sync_task_rejected_actions(agent, params):
    if not hasattr(agent, 'owner_quota') or not hasattr(agent, 'conversation_store'):
        return
    key = _key(params)
    if not key:
        return
    fields = {'tool_name', 'run_id', 'operation_id', 'idempotency_key', 'args_hash',
              'approval_id', 'decision', 'reason_code', 'status'}
    current = [{k: v for k, v in item.items() if k in fields} for item in params.runtime_rejected_actions]
    path = input_dependency_path(agent, key)
    if not current and not path.exists():
        return
    if not current:
        report = read_json_object_report(path, context="task_rejections.read")
        if report.load_error:
            raise ValueError("原任务拒绝记录不可读")
        params.runtime_rejected_actions[:] = report.payload["runtime_rejected_actions"]
        return
    # LLM: 精确字段合并只能追加拒绝；同参数未来调用不能覆盖历史原因。
    # 函数用途: 在原记录锁内合并同任务拒绝集合。
    def merge(row):
        items = list(row.get('runtime_rejected_actions', []))
        for item in current:
            if item not in items:
                items.append(item)
        return {'schema_version': 'task-rejected-actions.v1', 'runtime_rejected_actions': items}
    row = _mutate(agent, key, merge)
    params.runtime_rejected_actions[:] = row['runtime_rejected_actions']
