"""工作流反向引用投影；定义仍是唯一权威，未完成发布或旧库覆盖不明时拒绝证明无引用。"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from .run_list_index import index_connection

_FILENAME = 'workflow_reference_index.sqlite3'
# 最多保留32个正在分页的目录游标；空闲回收与容量淘汰都立即close。
_SCANS = OrderedDict()
_SCAN_LOCK = threading.Lock()
_MAX_SCANS = 32
_SCAN_IDLE_SECONDS = 60



@dataclass
class _ScanCursor:
    stamp: str
    epoch: int
    iterator: object = None
    touched: float = 0
    busy: bool = True
    done: bool = False


@contextmanager
def _borrow_cursor(store, stamp, epoch):
    """池锁只负责借还；在用游标有强引用且不会被过期/容量回收关闭。"""
    key, now = str(store.owner_home), time.monotonic()
    closing = []
    with _SCAN_LOCK:
        for old_key, old in list(_SCANS.items()):
            if not old.busy and (now - old.touched >= _SCAN_IDLE_SECONDS
                    or old_key == key and (old.stamp, old.epoch) != (stamp, epoch)):
                closing.append(old.iterator)
                del _SCANS[old_key]
        cursor = _SCANS.get(key)
        resumed = cursor is not None
        if cursor is not None and cursor.busy:
            cursor = None
        elif cursor is not None:
            cursor.busy = True
        else:
            if len(_SCANS) >= _MAX_SCANS:
                idle = next((name for name, entry in _SCANS.items() if not entry.busy), None)
                if idle is not None:
                    closing.append(_SCANS.pop(idle).iterator)
            if len(_SCANS) < _MAX_SCANS:
                cursor = _ScanCursor(stamp, epoch)
                _SCANS[key] = cursor
    for iterator in closing:
        if iterator is not None:
            iterator.close()
    try:
        if cursor is not None and cursor.iterator is None:
            cursor.iterator = os.scandir(store.owner_home / store.collection)
        yield cursor, resumed
    except BaseException:
        if cursor is not None:
            cursor.done = True
        raise
    finally:
        closing = None
        with _SCAN_LOCK:
            if cursor is not None:
                cursor.busy, cursor.touched = False, time.monotonic()
                if cursor.done:
                    _SCANS.pop(key, None)
                    closing = cursor.iterator
                else:
                    _SCANS.move_to_end(key)
        if closing is not None:
            closing.close()


def _directory_stamp(store):
    return str((store.owner_home / store.collection).stat().st_mtime_ns)


def _initial_coverage(store):
    # 初始化至多查看固定数量目录项；已有定义不通过首次全扫“补齐”索引。
    with os.scandir(store.owner_home / store.collection) as entries:
        for number, entry in enumerate(entries):
            if number >= 32 or not entry.name.startswith('.'):
                return False
    return True


def _targets(definition):
    from ..assets.workflow_store import BUSINESS_WORKFLOW_METADATA, normalize_workflow_asset_id

    state = definition.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA, {})
    if state.get('lifecycle') == 'recycled':
        return set()
    targets = set()
    for step in definition.steps:
        # 保留原引用检查的字段，并包括执行器实际支持的 config.subworkflow_ref。
        for value in (step.subworkflow_ref, step.config.get('workflow_id'),
                      step.config.get('subworkflow_id'), step.config.get('subworkflow_ref')):
            if isinstance(value, str) and value.strip():
                targets.add(normalize_workflow_asset_id(value))
    return targets


@contextmanager
def _connection(store, *, initialize=False):
    with index_connection(store.owner_home, _FILENAME) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version == 0 and initialize:
            conn.executescript('''
                PRAGMA auto_vacuum=INCREMENTAL;
                PRAGMA journal_size_limit=1048576;
                CREATE TABLE state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), complete INTEGER, stamp TEXT, epoch INTEGER);
                CREATE TABLE pending(parent TEXT PRIMARY KEY, failed_epoch INTEGER NOT NULL);
                CREATE INDEX pending_failed_epoch ON pending(failed_epoch,parent);
                CREATE TABLE refs(parent TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY(parent,target));
                CREATE TABLE discovery(singleton INTEGER PRIMARY KEY CHECK(singleton=1), stamp TEXT, cursor INTEGER, errors INTEGER, done INTEGER, epoch INTEGER, phase TEXT);
                CREATE INDEX refs_target ON refs(target,parent);
                CREATE TABLE parents(parent TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
                CREATE INDEX parents_epoch ON parents(epoch,parent);
                PRAGMA user_version=1;
            ''')
            conn.execute('INSERT INTO state VALUES(1,?,?,0)', (int(_initial_coverage(store)), _directory_stamp(store)))
        elif version != 1:
            raise ValueError('WORKFLOW_REFERENCE_INDEX_SCHEMA_INVALID')
        yield conn


def mark_pending(store, parent):
    """调用者持原资产锁；待发表标记先于正文，因此崩溃不会漏掉新依赖。"""
    with _connection(store, initialize=True) as conn:
        conn.execute('UPDATE state SET complete=0 WHERE stamp<>?', (_directory_stamp(store),))
        conn.execute('INSERT OR IGNORE INTO pending VALUES(?,-1)', (parent,))


def _publish_row(conn, parent, definition, epoch):
    conn.execute('DELETE FROM refs WHERE parent=?', (parent,))
    if definition is not None:
        conn.executemany('INSERT INTO refs VALUES(?,?)', ((parent, target) for target in _targets(definition)))
        conn.execute('INSERT OR REPLACE INTO parents VALUES(?,?)', (parent, epoch))
    else:
        conn.execute('DELETE FROM parents WHERE parent=?', (parent,))
    conn.execute('DELETE FROM pending WHERE parent=?', (parent,))


def _skip_failed_parent(conn, parent, epoch):
    # 本epoch不反复撞同一坏页；保留边及pending，整轮结束保持unknown，等下次常规维护。
    conn.execute('UPDATE parents SET epoch=? WHERE parent=?', (epoch, parent))
    conn.execute('UPDATE pending SET failed_epoch=? WHERE parent=?', (epoch, parent))


def publish(store, parent, definition):
    """正文提交后仅替换该父节点；历史覆盖未知不会因一次写入而被宣布完整。"""
    with _connection(store) as conn:
        epoch = conn.execute('SELECT epoch FROM state WHERE singleton=1').fetchone()[0]
        _publish_row(conn, parent, definition, epoch)
        conn.execute('UPDATE state SET stamp=?', (_directory_stamp(store),))


def read_parents(store, target, *, limit, diagnostics=None):
    """至多读一页规范定义；超页、缺库、旧库及发布未完成都返回不完整。"""
    from .models import safe_path

    if diagnostics is None:
        diagnostics = {}
    if not safe_path(store.owner_home, 'data', _FILENAME).exists():
        diagnostics['reason_code'] = 'WORKFLOW_REFERENCE_INDEX_MISSING'
        try:
            return _initial_coverage(store), []
        except FileNotFoundError:
            return True, []
    from ..assets import asset_root_lock_held

    with (nullcontext() if asset_root_lock_held(store.owner_home) else store._locked()):
        with _connection(store) as conn:
            state = conn.execute('SELECT complete,stamp FROM state WHERE singleton=1').fetchone()
            complete = bool(state and state[0] and state[1] == _directory_stamp(store))
            if not complete:
                diagnostics['reason_code'] = 'WORKFLOW_REFERENCE_DISCOVERY_INCOMPLETE'
            if conn.execute('SELECT 1 FROM pending LIMIT 1').fetchone():
                complete = False
                diagnostics['reason_code'] = 'WORKFLOW_REFERENCE_PUBLICATION_PENDING'
            rows = conn.execute('SELECT parent FROM refs WHERE target=? AND parent<>? ORDER BY parent LIMIT ?',
                                (target, target, limit + 1)).fetchall()
        if len(rows) > limit:
            complete = False
            diagnostics['reason_code'] = 'WORKFLOW_REFERENCE_PAGE_INCOMPLETE'
        parents = []
        for row in rows[:limit]:
            definition = store._get(row[0])
            if definition is None:
                complete = False
            elif target in _targets(definition):
                parents.append(row[0])
            else:
                # 外部修改不能让陈旧投影证明完整；通过原写口重发后才能清掉旧边。
                complete = False
        return complete, parents


def discover(store, *, limit=128):
    """原维护tick的一页扫描/清理；页进度持久化，重启只会按页重放，不执行无界seek。"""
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('WORKFLOW_REFERENCE_DISCOVERY_LIMIT_INVALID')
    with store._locked():
        with _connection(store, initialize=True) as conn:
            state = conn.execute('SELECT complete,stamp,epoch FROM state WHERE singleton=1').fetchone()
            stamp = _directory_stamp(store)
            if state[0] and state[1] == stamp and not conn.execute('SELECT 1 FROM pending LIMIT 1').fetchone():
                return {'complete': True, 'scanned': 0}
            row = conn.execute('SELECT stamp,cursor,errors,done,epoch,phase FROM discovery WHERE singleton=1').fetchone()
            if row is None or row[0] != stamp or row[3]:
                epoch, position, errors, phase = max(time.time_ns(), state[2] + 1), 0, 0, 'scan'
            else:
                _, position, errors, _, epoch, phase = row
            conn.execute('UPDATE state SET complete=0,epoch=? WHERE singleton=1', (epoch,))
            scanned, complete = 0, False
            if phase == 'scan':
                with _borrow_cursor(store, stamp, epoch) as (cursor, resumed):
                    if cursor is None:
                        conn.execute('INSERT OR REPLACE INTO discovery VALUES(1,?,?,?,?,?,?)',
                            (stamp, position, errors, 0, epoch, phase))
                        return {'complete': False, 'scanned': 0, 'cursor': position,
                                'errors': errors, 'reason_code': 'WORKFLOW_REFERENCE_CURSOR_CAPACITY'}
                    if not resumed:
                        # 原生目录迭代器不能持久化：句柄丢失后从头逐页重放当前epoch。
                        position = 0
                        errors = 0
                    for _ in range(limit):
                        entry = next(cursor.iterator, None)
                        if entry is None:
                            cursor.done = True
                            phase = 'prune'
                            break
                        scanned += 1
                        position += 1
                        if entry.name.startswith('.'):
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            errors += 1
                            continue
                        try:
                            definition = store._get(entry.name)
                            _publish_row(conn, entry.name, definition, epoch)
                        except (OSError, ValueError, KeyError):
                            errors += 1
                            _skip_failed_parent(conn, entry.name, epoch)
            if phase == 'prune' and scanned < limit:
                # 清理未在当前代次看见的旧parent及遗留pending，同样只取剩余一页。
                budget = limit - scanned
                old = conn.execute('SELECT parent FROM parents WHERE epoch<? ORDER BY epoch,parent LIMIT ?',
                                   (epoch, budget + 1)).fetchall()
                pending = conn.execute('SELECT parent FROM pending WHERE failed_epoch<? ORDER BY failed_epoch,parent LIMIT ?',
                                       (epoch, budget + 1)).fetchall()
                names = list(dict.fromkeys(row[0] for row in [*old, *pending]))
                for parent in names[:budget]:
                    scanned += 1
                    try:
                        _publish_row(conn, parent, store._get(parent), epoch)
                    except (OSError, ValueError, KeyError):
                        errors += 1
                        _skip_failed_parent(conn, parent, epoch)
                if len(names) <= budget:
                    phase = 'done'
                    complete = not errors and stamp == _directory_stamp(store)
            conn.execute('UPDATE state SET complete=?,stamp=? WHERE singleton=1', (int(complete), stamp))
            conn.execute('INSERT OR REPLACE INTO discovery VALUES(1,?,?,?,?,?,?)',
                         (stamp, position, errors, int(phase == 'done'), epoch, phase))
            return {'complete': complete, 'scanned': scanned, 'cursor': position, 'errors': errors,
                    'reason_code': 'WORKFLOW_REFERENCE_DEFINITION_UNREADABLE' if errors else ''}


def discovery_status(store):
    """给原维护调度器返回续页事实；坏页到EOF后等待下一次常规维护，不忙循环。"""
    with _connection(store) as conn:
        state = conn.execute('SELECT complete FROM state WHERE singleton=1').fetchone()
        row = conn.execute('SELECT cursor,errors,done FROM discovery WHERE singleton=1').fetchone()
        return {'complete': bool(state and state[0]),
                'partial': bool(row and not row[2]),
                'cursor': row[0] if row else 0, 'errors': row[1] if row else 0}


def publish_committed(store, parent, definition):
    """canonical已经成功时不撤销或重报未执行；耐久pending保留，原维护tick重读恢复。"""
    try:
        publish(store, parent, definition)
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning(
            'WORKFLOW_REFERENCE_PUBLICATION_PENDING parent=%s canonical_committed=true error_type=%s',
            parent, type(error).__name__)
