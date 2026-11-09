"""在原owner维护写通道分片补建用量来源索引，不在查询或启动时扫描旧历史。"""
from __future__ import annotations

import os
import sqlite3
from copy import deepcopy
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath
from ..user_space.directory_paging import directory_page
from ..user_space.storage_lanes import StorageLaneRequest
from .store import ConversationStore


def _prepare(request, parts, limit, deadline):
    owner = Path(request.home.owner_home_dir)
    # 缺源是纯读空事实，不消耗协调写门；真正写入前仍重新开目录核实。
    try:
        descriptor = open_directory_beneath(owner, (*parts, 'model_usage'), require_dir_fd=True)
    except (FileNotFoundError, NotADirectoryError):
        return {'coverage': 'complete', 'sources_examined': 0, 'errors': []}
    os.close(descriptor)
    # 缺失目录不因后台索引维护而重建；每次真正写前重新核实owner内路径。
    def write():
        try:
            descriptor = open_directory_beneath(owner, (*parts, 'model_usage'), require_dir_fd=True)
        except (FileNotFoundError, NotADirectoryError):
            return {'coverage': 'complete', 'sources_examined': 0, 'errors': []}
        try:
            store = ConversationStore(owner.joinpath(*parts), initialize=False)
            return store.model_usage.prepare_window_sources(limit=limit, deadline=deadline)
        finally:
            os.close(descriptor)
    return request.run_write(write)


def maintain_usage_sources_lane(request: StorageLaneRequest) -> dict:
    """每拍最多四个目录/来源步骤、25ms；游标只定位，完整性仍由源索引确认。"""
    if request.legal_hold:
        return {'cursor': dict(request.cursor), 'complete': True, 'items': 0, 'bytes': 0,
                'deferred_reason': 'legal_hold'}
    if request.budget.exhausted():
        return {'cursor': dict(request.cursor), 'complete': False, 'items': 0, 'bytes': 0,
                'deferred_reason': 'budget'}
    budget = request.budget.capped(items=4, size=8 * 1024 * 1024, ms=25)
    cursor = deepcopy(request.cursor) if not request.cursor.get('complete') else {}
    result = {'cursor': cursor, 'complete': False, 'items': 0, 'bytes': 0, 'errors': []}
    if cursor.get('phase', 'direct') not in {'direct', 'workspaces'}:
        raise ValueError('invalid_usage_source_maintenance_phase')
    owner = Path(request.home.owner_home_dir)
    workspace_parts = Path(request.home.owner_workspace_dir).relative_to(owner).parts + ('runtime', 'workspaces')
    while not budget.exhausted(result['items']):
        phase = cursor.get('phase', 'direct')
        if phase == 'workspaces' and not cursor.get('workspace'):
            names, position, complete = directory_page(owner, workspace_parts, cursor.get('scan', {}), 1)
            cursor['scan'] = position
            result['items'] += max(1, position.pop('last_scanned', len(names)))
            if not names:
                result['complete'] = complete
                cursor['complete'] = complete
                if cursor.get('unknown_seen'):
                    result['deferred_reason'] = 'source_coverage_unknown'
                return result
            cursor['workspace'] = names[0]
            if budget.exhausted(result['items']):
                return result
        name = cursor.get('workspace', '')
        if phase == 'workspaces' and (not isinstance(name, str) or name in {'', '.', '..'}
                or any(part in name for part in ('/', '\\', '\x00'))):
            raise ValueError('invalid_usage_source_workspace')
        parts = ('conversations',) if phase == 'direct' else (*workspace_parts, name, 'conversations')
        try:
            report = _prepare(request, parts, min(4, budget.max_items - result['items']), budget.deadline)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            report = {'coverage': 'unknown', 'sources_examined': 1,
                      'errors': [{'code': 'USAGE_SOURCE_MAINTENANCE_UNAVAILABLE'}]}
        result['items'] += max(1, report['sources_examined'])
        if report['coverage'] == 'partial':
            result['deferred_reason'] = 'source_index_partial'
            return result
        if report['coverage'] != 'complete':
            # 坏源仍保持unknown；推进其它workspace，不能让单个坏源永久饿死其它索引。
            cursor['unknown_seen'] = True
            result['errors'].append({'code': 'USAGE_SOURCE_MAINTENANCE_UNAVAILABLE', 'path': '/'.join(parts)})
        cursor.update(phase='workspaces')
        cursor.pop('workspace', None)
    return result
