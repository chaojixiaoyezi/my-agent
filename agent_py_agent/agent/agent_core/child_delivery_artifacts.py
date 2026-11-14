"""收口只继承原父精确谱系的成功子登记；不扫描产物目录或从回复猜引用。"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..artifacts.registry import latest_artifact_records_report
from ..subagents.models import TaskStatus, task_status_in

# 每次收口最多复核64个直属成功子代；超量报告partial，不扫描整个Owner。
_CHILD_DELIVERY_MAX_COUNT = 64
# 单子代最多读取256KiB；所有子代共享4MiB总预算（含超限哨兵），超限只报partial。
_CHILD_REGISTRY_MAX_BYTES = 256 * 1024
# 所有直属子代合计的读取上限，计入每本登记簿用于确认超限的哨兵字节。
_CHILD_REGISTRY_TOTAL_MAX_BYTES = 4 * 1024 * 1024
# 每子代最多核验128个完成时refs；截断保留partial，不把未核验项当空。
_CHILD_DELIVERY_MAX_REF_COUNT = 128


def completed_child_artifact_refs(agent, ctx):
    """原runtime父索引挑有界候选，canonical与完成时登记再核版本；无身份不做补偿扫描。"""
    if agent is None:
        return []
    owner = str(getattr(getattr(agent, 'home_paths', None), 'owner_id', '') or '')
    owner_home = str(getattr(getattr(agent, 'home_paths', None), 'owner_home_dir', '') or '')
    parent_id = str(getattr(ctx, 'run_id', '') or '')
    manager = getattr(agent, 'subagents', None)
    repo = getattr(manager, 'runtime_db', None)
    if not owner or not owner_home or not parent_id or manager is None or repo is None:
        return []
    attrs = getattr(ctx, 'task_attributes', None)
    if isinstance(attrs, dict):
        attrs['child_delivery_diagnostics'] = []
    try:
        parent = repo.parent_execution_ref(owner_id=owner, run_id=parent_id,
            attempt_id=str(getattr(ctx, 'attempt_id', '') or '') or None)
        with repo._runtime_connection() as db:
            rows = db.execute('SELECT candidates.run_id,candidates.current_attempt_id,aa.status AS attempt_status '
                'FROM (SELECT agent_run_id,run_id,current_attempt_id FROM agent_runs '
                'WHERE parent_agent_run_id=? AND task_run_id=? AND status=? '
                'ORDER BY agent_run_id LIMIT ?) candidates '
                'LEFT JOIN agent_attempts aa ON aa.attempt_id=candidates.current_attempt_id '
                'AND aa.agent_run_id=candidates.agent_run_id',
                (parent.agent_run_id, parent.task_run_id, 'done', _CHILD_DELIVERY_MAX_COUNT + 1)).fetchall()
        if len(rows) > _CHILD_DELIVERY_MAX_COUNT:
            _diagnostic(ctx, 'CHILD_DELIVERY_PAGE_LIMIT', next_action='读取原子代结果分页；本轮交付投影不代表完整集合。')
        selected, remaining_bytes = [], _CHILD_REGISTRY_TOTAL_MAX_BYTES
        for row in rows[:_CHILD_DELIVERY_MAX_COUNT]:
            if row['attempt_status'] != 'done':
                continue
            child = manager.load(row['run_id'])
            if (child.id != row['run_id'] or child.parent_id != parent_id or child.owner != owner
                    or child.root_id != str(getattr(ctx, 'task_id', '') or parent_id)
                    or not task_status_in(child.status, {TaskStatus.DONE.value})):
                continue
            attrs = child.attributes if isinstance(child.attributes, dict) else {}
            frozen = attrs.get('artifact_registry_refs')
            if not isinstance(frozen, list):
                continue
            if len(frozen) > _CHILD_DELIVERY_MAX_REF_COUNT:
                _diagnostic(ctx, 'CHILD_DELIVERY_REF_LIMIT', run_id=child.id,
                    next_action='当前子代登记引用超过本次核验条数；按原引用分页核验，不声明完整交付。')
            frozen = frozen[:_CHILD_DELIVERY_MAX_REF_COUNT]
            root = str(child.agent_run_workspace_dir or '')
            if not root:
                continue
            home_path, root_path = Path(owner_home).resolve(), Path(root).resolve()
            if root_path == home_path or not root_path.is_relative_to(home_path) or root_path.name != child.id:
                _diagnostic(ctx, 'CHILD_DELIVERY_WORKSPACE_SCOPE_MISMATCH', run_id=child.id,
                    next_action='核对原子代的Owner内规范工作区；不读取外部登记簿补权。')
                continue
            if remaining_bytes <= 1:
                _diagnostic(ctx, 'CHILD_DELIVERY_TOTAL_READ_BUDGET_EXCEEDED',
                    next_action='本轮登记核验已达累计字节预算；保持partial，沿原引用继续核验。')
                break
            budget = min(_CHILD_REGISTRY_MAX_BYTES, remaining_bytes - 1)
            report = latest_artifact_records_report(Path(root), max_bytes=budget)
            remaining_bytes -= report.bytes_read if report.bytes_read else (budget + 1 if report.errors else 0)
            if report.errors:
                _diagnostic(ctx, 'CHILD_DELIVERY_REGISTRY_BUDGET_EXCEEDED' if any(
                    row.get('error_code') == 'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED' for row in report.errors
                ) else 'CHILD_DELIVERY_REGISTRY_UNREADABLE', run_id=child.id,
                    next_action='检查原子代登记簿；不从文件路径或文字恢复交付身份。')
                continue
            for old in frozen:
                if not isinstance(old, dict):
                    continue
                record = report.records.get(str(old.get('artifact_id') or ''))
                if (record is None or record.run_id != child.id or record.task_id != child.root_id
                        or record.status != 'ready' or record.kind == 'tool_output'
                        or record.metadata.get('artifact_role') == 'tool_output_archive'):
                    continue
                value = record.to_dict()
                if any(old.get(key) != value.get(key) for key in
                       ('artifact_id', 'run_id', 'task_id', 'path', 'sha256', 'size_bytes')):
                    _diagnostic(ctx, 'CHILD_DELIVERY_VERSION_CHANGED', run_id=child.id,
                        next_action='原子代完成版本已变化；复核新版本后沿正常任务重新交付。')
                    continue
                selected.append(value)
        return selected
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        _diagnostic(ctx, 'CHILD_DELIVERY_AUTHORITY_UNAVAILABLE', exception_type=type(error).__name__,
            next_action='核对原runtime父子归属及完成证明后重试交付，不扫描目录补权。')
        return []


def _diagnostic(ctx, code, *, run_id='', exception_type='', next_action=''):
    attrs = getattr(ctx, 'task_attributes', None)
    if isinstance(attrs, dict):
        rows = attrs.setdefault('child_delivery_diagnostics', [])
        if isinstance(rows, list) and len(rows) < 8:
            rows.append({'error_code': code, 'coverage': 'partial', 'effect_outcome': 'not_started',
                         'stage': 'delivery_observation', 'source_effect_outcome': 'unknown', 'run_id': run_id,
                         'exception_type': exception_type, 'next_action': next_action})
    logging.getLogger(__name__).warning('%s', code)
