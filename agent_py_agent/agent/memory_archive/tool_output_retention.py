# LLM: 仅管理Owner规范根下已结束attempt的到期正文；索引不变，先刷退休证据再删正文，未知事实一律保留。
# 模块用途: 分片退休工具输出并为读取端提供可核验的过期状态，不管理任务目录。
from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    list_names_beneath,
    open_readonly_file_beneath,
)
from ..common.tool_output_paths import tool_output_root

# Owner根工具输出正文默认保留天数。
OWNER_TOOL_OUTPUT_RETENTION_DAYS = 30
# 完整扫描后再次扫描的最小间隔秒数。
_TOOL_OUTPUT_PASS_INTERVAL_SECONDS = 3600.0
# 单个工具输出包装文件最多读取的字节数。
_TOOL_OUTPUT_WRAPPER_MAX_BYTES = 1024 * 1024
# 已失去数据文件的锁侧文件清扫间隔秒数。
_TOOL_OUTPUT_LOCK_SWEEP_INTERVAL_SECONDS = 86400.0
# 镜像runtime_db.repository._QUIESCENT_ATTEMPT_STATUSES；unknown不证明执行已结束。
_ENDED_ATTEMPT_STATUSES = frozenset({'done', 'failed', 'cancelled', 'recovered'})
_PARTS = ('blobs', 'tool_outputs')


# LLM: 不接受无时区或非ISO值作为到期事实；纯计算，不读文件。
# 函数用途: 解析明确时区的持久时间。
def _instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


# LLM: 分片键仅来自原索引的创建时间，坏时间不能扩大读取范围。
# 函数用途: 计算UTC日期退休账本的安全相对路径。
def retired_shard_parts(created_at: str) -> tuple[str, ...] | None:
    try:
        return ('retired', _instant(created_at).date().isoformat() + '.jsonl')
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


# LLM: 只在原包装目录的日期分片里按完整basename和SHA匹配；失效证据只影响错误语义，不授读取权。
# 函数用途: 判断缺失正文是否有对应的退休记录。
def retired_tool_output(root: Path, path: Path, record: Mapping) -> bool:
    shard = retired_shard_parts(str(record.get('created_at') or ''))
    if shard is None:
        return False
    try:
        rel = path.parent.relative_to(root).parts
        descriptor = open_readonly_file_beneath(root, (*rel, *shard))
        with os.fdopen(descriptor, 'rb') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (isinstance(row, dict) and row.get('name') == path.name
                        and row.get('sha256') == str(record.get('sha256') or '')):
                    return True
    except (OSError, ValueError):
        pass
    return False


# LLM: 只选现存真实DB，所有父级及WAL/SHM拒绝链接；只读连接不初始化schema，未知或失败不能变成终态。
# 函数用途: 批量读取候选attempt的权威状态和结束时间。
def _attempt_states(owner_home: Path, attempt_ids: set[str]) -> dict[str, tuple[str, float]] | None:
    from ..runtime_db.schema import runtime_db_path

    try:
        db = None
        for candidate in dict.fromkeys((runtime_db_path(owner_home), owner_home / 'data' / 'runtime.db')):
            try:
                descriptor = open_readonly_file_beneath(owner_home, candidate.relative_to(owner_home).parts)
            except FileNotFoundError:
                continue
            with os.fdopen(descriptor, 'rb') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError('runtime db is not regular')
                header = handle.read(20)
            db = candidate
            break
        if db is None:
            return {}
        wal, shm = Path(str(db) + '-wal'), Path(str(db) + '-shm')
        if wal.is_symlink() or shm.is_symlink() or (wal.exists() and not shm.is_file()):
            raise ValueError('unsafe runtime sidecar')
        immutable = not wal.exists() and header[18:20] == b'\x02\x02'
        uri = db.absolute().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
        result = {}
        with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            ids = sorted(attempt_ids)
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                query = 'SELECT attempt_id, status, ended_at FROM agent_attempts WHERE attempt_id IN ('
                rows = conn.execute(query + ','.join('?' for _ in chunk) + ')', chunk)
                for identity, status, ended in rows:
                    ended = float(ended or 0)
                    if not math.isfinite(ended):
                        raise ValueError('invalid attempt end time')
                    result[str(identity)] = (str(status), ended)
        return result
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return None


# LLM: 错误只公开Owner相对路径，限量且不含异常正文或输出载荷。
# 函数用途: 为回执追加一条有界错误。
def _error(errors, code, path=''):
    if len(errors) < 16:
        errors.append({'code': code, **({'path': path} if path else {})})


# LLM: 所有keep理由按次计数，未知状态不冒充已删。
# 函数用途: 累加一个跳过原因。
def _skip(skipped, reason):
    skipped[reason] = skipped.get(reason, 0) + 1


# LLM: 只在nofollow FD上读有限字节；stat用于身份和跳过，不能单独授删除权。
# 函数用途: 返回包装的有限JSON、原始字节量与FD身份。
def _wrapper(home, name, *, cutoff=None, max_bytes=None):
    descriptor = open_readonly_file_beneath(home, (*_PARTS, name))
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None, info, 0
        if cutoff is not None and info.st_mtime > cutoff:
            return None, info, 0
        if info.st_size > _TOOL_OUTPUT_WRAPPER_MAX_BYTES or (max_bytes is not None and info.st_size > max_bytes):
            return None, info, 0
        raw = handle.read(_TOOL_OUTPUT_WRAPPER_MAX_BYTES + 1)
    if len(raw) > _TOOL_OUTPUT_WRAPPER_MAX_BYTES:
        return None, info, len(raw)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        value = None
    return value, info, len(raw)


# LLM: 包装格式仅限真实writer的两种布局；未知类型和时间均保留。
# 函数用途: 核对工具输出包装的必要身份和固定blob叶名。
def _valid_wrapper(value, name):
    if (not isinstance(value, dict) or value.get('kind') != 'tool_output'
            or not all(isinstance(value.get(key), str) for key in ('sha256', 'attempt_id', 'run_id', 'created_at'))
            or not value['sha256'] or retired_shard_parts(value['created_at']) is None):
        return False
    if value.get('artifact_format') == 'blob_v1':
        return value.get('content_blob') == name[:-5] + '.blob' and 'content' not in value
    return value.get('artifact_format') is None and isinstance(value.get('content'), str)


# LLM: 调用方持包装原锁；对同inode仍重读业务身份，原子替换或多链接不能越过扫描事实。
# 函数用途: 核对候选仍为本次扫描的同一份包装。
def _same_wrapper(home, candidate):
    value, info, _ = _wrapper(home, candidate['name'])
    if (not _valid_wrapper(value, candidate['name']) or info.st_nlink != 1
            or (info.st_dev, info.st_ino, info.st_size) != candidate['identity']):
        return False
    return all(value[key] == candidate['value'][key] for key in ('sha256', 'created_at', 'attempt_id', 'run_id'))


# LLM: 不嵌套artifact/shard锁；证据必须先成功刷盘，任一失败保留正文并留下可重试续点。
# 函数用途: 分验证、写账、删除三阶段退休一批同Owner候选。
def _retire_batch(home, eligible, *, now, days, budget):
    from ..common.nofollow_fs import append_text_beneath

    result = {'decided': set(), 'deleted_items': 0, 'freed_bytes': 0, 'skipped': {}, 'errors': []}
    verified = defaultdict(list)
    directory = tool_output_root(home)
    for candidate in eligible:
        if budget.remaining_ms() <= 0:
            break
        name = candidate['name']
        try:
            with locked_json_path(directory / name, blocking=False):
                if not _same_wrapper(home, candidate):
                    _skip(result['skipped'], 'identity_changed')
                    result['decided'].add(name)
                    continue
            verified[retired_shard_parts(candidate['value']['created_at'])].append(candidate)
        except BlockingIOError:
            _skip(result['skipped'], 'lock_busy')
        except FileNotFoundError:
            result['decided'].add(name)
        except (OSError, ValueError):
            _error(result['errors'], 'artifact_verify_failed', '/'.join((*_PARTS, name)))
    recorded = []
    for shard, candidates in verified.items():
        if budget.remaining_ms() <= 0:
            break
        try:
            ensure_private_dir(directory / 'retired', canonical_root=home)
            shard_path = directory.joinpath(*shard)
            with locked_json_path(shard_path, blocking=False):
                text = ''.join(json.dumps({'version': 1, 'name': item['name'],
                    **{key: item['value'][key] for key in ('sha256', 'attempt_id', 'run_id', 'created_at')},
                    'retired_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    'reason': 'ended_attempt_ttl', 'days': days}, ensure_ascii=False) + '\n' for item in candidates)
                append_text_beneath(home, (*_PARTS, *shard), text)
            recorded.extend(candidates)
        except BlockingIOError:
            result['skipped']['lock_busy'] = result['skipped'].get('lock_busy', 0) + len(candidates)
        except (OSError, ValueError):
            _error(result['errors'], 'retired_shard_unwritable', '/'.join((*_PARTS, *shard)))
    for candidate in recorded:
        if budget.remaining_ms() <= 0:
            break
        name = candidate['name']
        try:
            with locked_json_path(directory / name, blocking=False):
                if not _same_wrapper(home, candidate):
                    _skip(result['skipped'], 'identity_changed')
                    result['decided'].add(name)
                    continue
                blob = candidate['value'].get('content_blob')
                if blob:
                    try:
                        descriptor = open_readonly_file_beneath(home, (*_PARTS, blob))
                    except FileNotFoundError:
                        descriptor = None
                    if descriptor is not None:
                        try:
                            info = os.fstat(descriptor)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise ValueError('unsafe blob')
                        finally:
                            os.close(descriptor)
                        _unlink_retired_file(home, (*_PARTS, blob), (info.st_dev, info.st_ino, info.st_size), result)
                _unlink_retired_file(home, (*_PARTS, name), candidate['identity'], result, wrapper_name=name)
        except BlockingIOError:
            _skip(result['skipped'], 'lock_busy')
        except FileNotFoundError:
            result['decided'].add(name)
        except (OSError, ValueError):
            _error(result['errors'], 'artifact_unlink_failed', '/'.join((*_PARTS, name)))
    return result


# LLM: 原领域锁内固定父目录FD，unlink成功即记物理释放；后续目录fsync失败只增加错误，不抹去删除事实。
# 函数用途: 删除同一单链接普通叶并准确计量，绝不按可变父路径重开删除目标。
def _unlink_retired_file(home, parts, identity, result, *, wrapper_name=None):
    from ..common.nofollow_fs import open_directory_beneath

    parent = open_directory_beneath(home, parts[:-1], require_dir_fd=True)
    try:
        try:
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino, info.st_size) != identity):
            raise ValueError('retired leaf identity changed')
        os.unlink(parts[-1], dir_fd=parent)
        result['freed_bytes'] += info.st_size
        if wrapper_name is not None:
            result['deleted_items'] += 1
            result['decided'].add(wrapper_name)
        os.fsync(parent)
    finally:
        os.close(parent)


# LLM: pin读取保留原错误对象供领域复核，最终只投影结构化有界错误。
# 函数用途: 将pin来源失败转为通道回执错误。
def _pin_error_rows(source, errors):
    for error in source:
        _error(errors, str(getattr(error, 'code', None) or getattr(error, 'error_code', None) or 'pins_unreadable'))


# LLM: 只预筛至少一天且已无数据的json锁；实际清扫沿原唯一锁退休器，绝不直接unlink锁。
# 函数用途: 按日进行一次有真实候选才提交的孤儿锁清扫。
def _sweep_locks(request, cursor, directory, budget):
    from ..common.nofollow_fs import open_directory_beneath
    from ..gateway_parts.io import cleanup_orphan_lock_files
    from ..user_space.storage_lanes import STORAGE_LANE_SLICE_ENTRIES, lane_cursor_window

    if cursor.get('locks_due_at', 0) > request.now or budget.remaining_ms() <= 0:
        return
    descriptor = open_directory_beneath(request.home.owner_home_dir, _PARTS, require_dir_fd=True)
    try:
        with os.scandir(descriptor) as entries:
            window = lane_cursor_window({'after': cursor.get('locks_after', '')},
                                        (entry.name for entry in entries if entry.name.endswith('.json.lock')),
                                        STORAGE_LANE_SLICE_ENTRIES)
        due, examined = set(), set()
        for name in window:
            if budget.remaining_ms() <= 0:
                break
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_mtime > request.now - _TOOL_OUTPUT_LOCK_SWEEP_INTERVAL_SECONDS):
                    examined.add(name)
                    continue
                try:
                    os.stat(name[:-5], dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    due.add(name)
                    continue
                examined.add(name)
            except FileNotFoundError:
                examined.add(name)
        if due:
            # LLM: 本异常只结束本片清扫，不代表故障或已穷尽；续点保留原locks_due_at。
            # 类用途: 在canonical清扫器的单叶边界中断过期或超量批次。
            class SweepBudgetStop(Exception):
                pass

            # LLM: canonical清扫器在外层枚举；每个实际删除只进一次原写门，不在run_write内嵌套coordinator。
            # 函数用途: 限量放行一个原锁退休步骤。
            def commit_one(operation):
                # canonical operation以唯一默认参数绑定当前lock_path；未知形状保守中断而非猜路径。
                defaults = getattr(operation, '__defaults__', ()) or ()
                if len(defaults) != 1 or not isinstance(defaults[0], Path):
                    raise SweepBudgetStop
                name = defaults[0].name
                if name not in due:
                    return False
                if budget.remaining_ms() <= 0:
                    raise SweepBudgetStop
                result = request.run_write(operation)
                # 锁清扫扫描续点不是正文删除游标；原协议拒绝的活锁下一完整轮再检查。
                examined.add(name)
                return result

            try:
                cleanup_orphan_lock_files(directory, now=request.now, data_sidecars_only=True,
                                          directory_fd=descriptor, commit_transition=commit_one)
            except SweepBudgetStop:
                pass
        after = cursor.get('locks_after', '')
        for name in window:
            if name not in examined:
                break
            after = name
        if len(window) < STORAGE_LANE_SLICE_ENTRIES and all(name in examined for name in window):
            cursor.pop('locks_after', None)
            cursor['locks_due_at'] = request.now + _TOOL_OUTPUT_LOCK_SWEEP_INTERVAL_SECONDS
        else:
            cursor['locks_after'] = after
    finally:
        os.close(descriptor)


# LLM: 调度器拥有游标和写门；扫描锁外、pin只取一次，任何未处理/锁忙名称不能越过连续前缀。
# 函数用途: 在Owner规范根有界退休终态attempt的到期工具正文。
def maintain_owner_tool_outputs_lane(request) -> dict:
    from ..memory_store.retention_scan import current_retention_pins
    from ..user_space.storage_lanes import (
        STORAGE_LANE_SLICE_BYTES,
        STORAGE_LANE_SLICE_ENTRIES,
        STORAGE_LANE_SLICE_MS,
        lane_cursor_window,
        owner_conversation_roots,
        owner_retention_override,
        validate_lane_receipt,
    )

    cursor = dict(request.cursor)
    receipt = {'cursor': cursor, 'complete': True, 'items': 0, 'bytes': 0}
    if request.legal_hold:
        receipt['deferred_reason'] = 'legal_hold'
        return receipt
    if cursor.get('next_pass_at', 0) > request.now:
        return receipt
    home = Path(request.home.owner_home_dir)
    errors, skipped, decided, due = [], {}, set(), []
    receipt.update(errors=errors, skipped=skipped, protected=0, deleted_items=0, freed_bytes=0, backlog=0)
    try:
        names = list_names_beneath(home, _PARTS)
    except FileNotFoundError:
        names = []
    except (OSError, ValueError):
        _error(errors, 'tool_outputs_unreadable')
        return validate_lane_receipt(receipt)
    days = owner_retention_override(request.home, 'tool_output_days', OWNER_TOOL_OUTPUT_RETENTION_DAYS)
    cutoff = request.now - days * 86400
    budget = request.budget.capped(items=STORAGE_LANE_SLICE_ENTRIES, size=STORAGE_LANE_SLICE_BYTES, ms=STORAGE_LANE_SLICE_MS)
    window = lane_cursor_window(cursor, (name for name in names if name.endswith('.json')), STORAGE_LANE_SLICE_ENTRIES)
    for name in window:
        if budget.exhausted(receipt['items'], receipt['bytes']):
            break
        if due and budget.remaining_ms() <= STORAGE_LANE_SLICE_MS / 2:
            break
        if cursor.get('retry_name') and receipt['items'] >= 1:
            break
        receipt['items'] += 1
        try:
            value, info, size = _wrapper(home, name, cutoff=cutoff, max_bytes=budget.max_bytes - receipt['bytes'])
            receipt['bytes'] += size
            reason = None
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                reason = 'not_regular'
            elif info.st_mtime > cutoff:
                reason = 'young'
            elif not _valid_wrapper(value, name):
                reason = 'unknown_schema'
            elif _instant(value['created_at']).timestamp() > cutoff:
                reason = 'not_due'
            if reason:
                _skip(skipped, reason)
                decided.add(name)
                continue
            due.append({'name': name, 'value': value, 'identity': (info.st_dev, info.st_ino, info.st_size)})
        except FileNotFoundError:
            decided.add(name)
            _skip(skipped, 'missing')
        except (OSError, ValueError):
            decided.add(name)
            _error(errors, 'tool_output_unreadable', '/'.join((*_PARTS, name)))
    eligible, pin_errors = [], []
    if due:
        states = _attempt_states(home, {item['value']['attempt_id'] for item in due if item['value']['attempt_id']})
        if states is None:
            _error(errors, 'runtime_db_unreadable')
        candidates = []
        for item in due:
            state = states.get(item['value']['attempt_id']) if states is not None else None
            reason = None
            if state is None:
                reason = 'run_state_unknown'
            elif state[0] == 'unknown':
                reason = 'attempt_unknown'
            elif state[0] not in _ENDED_ATTEMPT_STATUSES:
                reason = 'attempt_active'
            elif state[1] <= 0:
                reason = 'run_state_unknown'
            elif state[1] > cutoff:
                reason = 'not_due'
            if reason:
                _skip(skipped, reason)
                receipt['protected'] += 1
                decided.add(item['name'])
            else:
                candidates.append(item)
        if candidates:
            pins, _ = current_retention_pins(request.home, owner_conversation_roots(request.home), pin_errors,
                                             now=datetime.fromtimestamp(request.now, timezone.utc))
            for item in candidates:
                path = tool_output_root(home) / item['name']
                blob = item['value'].get('content_blob')
                paths = (path, path.with_name(blob)) if blob else (path,)
                reason = None
                if not all(pins.complete_for(target, pin_errors) for target in paths):
                    reason = 'pins_incomplete'
                elif any(pins.protects(target, pin_errors) for target in paths):
                    reason = 'pinned'
                if reason:
                    _skip(skipped, reason)
                    receipt['protected'] += 1
                    decided.add(item['name'])
                else:
                    eligible.append(item)
    _pin_error_rows(pin_errors, errors)
    if eligible and budget.remaining_ms() > 0:
        result = request.run_write(lambda: _retire_batch(home, eligible, now=request.now, days=days, budget=budget))
        decided.update(result['decided'])
        for key in ('deleted_items', 'freed_bytes'):
            receipt[key] = result[key]
        for reason, count in result['skipped'].items():
            skipped[reason] = skipped.get(reason, 0) + count
        errors.extend(result['errors'][:max(0, 16 - len(errors))])
    after = cursor.get('after', '')
    for name in window:
        if name not in decided:
            break
        after = name
    receipt['backlog'] = sum(item['name'] not in decided for item in due)
    cursor.pop('retry_name', None)
    pending = [item['name'] for item in due if item['name'] not in decided]
    if pending and budget.remaining_ms() <= 0:
        cursor['retry_name'] = min(pending)
        receipt['deferred_reason'] = 'budget_exhausted'
    if due:
        receipt['oldest_due_age_seconds'] = max(0, request.now - min(_instant(item['value']['created_at']).timestamp() for item in due))
    complete = len(window) < STORAGE_LANE_SLICE_ENTRIES and all(name in decided for name in window)
    if complete:
        cursor = {'after': '', 'next_pass_at': request.now + _TOOL_OUTPUT_PASS_INTERVAL_SECONDS,
                  'locks_due_at': cursor.get('locks_due_at', 0), 'locks_after': cursor.get('locks_after', '')}
        if names:
            try:
                _sweep_locks(request, cursor, tool_output_root(home), budget)
            except (OSError, ValueError):
                _error(errors, 'orphan_locks_unreadable')
    else:
        cursor['after'] = after
    receipt.update(cursor=cursor, complete=complete)
    return validate_lane_receipt(receipt)
