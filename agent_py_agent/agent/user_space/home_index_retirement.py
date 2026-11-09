"""全局索引退役只接受 canonical 消失及原运行权威终态；仍可定位历史保持原引用。"""
from __future__ import annotations

import heapq
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath
from ..io.sqlite_probe import require_plain_sqlite_files, sqlite_readonly_probe_uri
from ..runtime_db.current_obligations import closed_task_ids
from ..runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from ..runtime_db.schema import runtime_db_path


def _missing(path):
    if not path.is_absolute() or '..' in path.parts:
        return False
    # 不跟链接；任何权限、类型、IO错误都不是不存在证明。
    try:
        descriptor = open_directory_beneath(Path(path.anchor), path.parts[1:], require_dir_fd=True)
    except FileNotFoundError:
        return True
    else:
        os.close(descriptor)
        return False


def retirement_candidates(target, prefix_len, latest, *, now, after=(), limit=128):
    """只检查游标后128个最新key；同owner共用一个只读事务，不在逐key循环重开SQLite。"""
    selected = heapq.nsmallest(limit, (key for key in latest if key > tuple(after)))
    if not selected:
        return {}, ()
    wanted = {latest[key] for key in selected}
    records = {}
    with target.open('rb') as source:
        offset = 0
        for index, raw in enumerate(source):
            offset += len(raw)
            if offset > prefix_len:
                break
            if index in wanted:
                records[index] = json.loads(raw)
    groups, retired = defaultdict(list), {}
    root = target.parent.parent
    for index, row in records.items():
        owner_id = row.get('owner_id')
        if type(owner_id) is not str:
            continue
        parts = Path(owner_id).parts
        if ('..' in parts or Path(owner_id).is_absolute()
                or not ((len(parts) == 2 and parts[0] == 'local')
                        or (len(parts) == 4 and parts[0] == 'providers' and parts[2] in ('users', 'groups')))):
            continue
        owner = root / 'owners' / owner_id
        try:
            old = now - datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00')).timestamp() >= 30 * 86400
            if not old:
                continue
            owner_missing = _missing(owner)
            if target.name == 'owners.jsonl':
                if owner_missing and Path(row.get('owner_home', '')) == owner:
                    retired[index] = row
                continue
            if str(row.get('status', '')).lower() not in AGENT_RUN_TERMINAL_STATUSES:
                continue
            field = 'task_path' if target.name == 'active_tasks.jsonl' else 'run_path'
            source = Path(row.get(field, ''))
            if not _missing(source):
                continue
            if owner_missing:
                retired[index] = row
            else:
                groups[owner].append((index, row))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    for owner, entries in groups.items():
        try:
            db = runtime_db_path(owner)
            require_plain_sqlite_files(db, beneath=owner)
            with closing(sqlite3.connect(sqlite_readonly_probe_uri(db), uri=True, timeout=0)) as conn:
                conn.execute('BEGIN')
                task_ids = list({row.get('task_id') for _, row in entries} - {None, ''})
                if not task_ids:
                    continue
                closed = closed_task_ids(conn, task_ids)
                for index, row in entries:
                    if row.get('task_id') in closed:
                        retired[index] = row
        except (OSError, ValueError, TypeError, sqlite3.Error):
            continue
    return retired, selected[-1] if len(selected) == limit else ()


def sources_still_missing(target, rows):
    """索引锁内末次复核；canonical恢复后必须放弃旧退役快照，后续append仍沿原tail。"""
    try:
        for row in rows:
            field = 'owner_home' if target.name == 'owners.jsonl' else 'task_path' if target.name == 'active_tasks.jsonl' else 'run_path'
            path = Path(row.get(field, ''))
            if not _missing(path):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False
