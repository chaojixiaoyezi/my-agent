"""线程任务历史的分页权威；线程 JSON 仅保存有界展示窗口。

写操作必须处于线程 JSON 锁内。每页至多128项，成员定位用散列分片；
迁移先耐久保存完整旧历史，再发表线程窗口，因此中断不会丢失旧身份。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..common.json_io import write_private_json_file_atomic_unlocked
from ..common.nofollow_fs import read_bytes_beneath

PAGE_SIZE = 128


def history_root(thread_path: Path) -> Path:
    return thread_path.parent / 'task_history' / thread_path.stem


def _read(root: Path, parts: tuple[str, ...]):
    raw = read_bytes_beneath(root, parts)
    return None if raw is None else json.loads(raw)


def _member_parts(task_id: str) -> tuple[str, ...]:
    digest = hashlib.sha256(task_id.encode('utf-8')).hexdigest()
    return ('members', digest[:2], digest[2:] + '.json')


def contains(thread_path: Path, thread, task_id: str) -> bool:
    root = history_root(thread_path)
    manifest = _read(root, ('index.json',))
    if manifest is None:
        return task_id in thread.task_ids
    return _contains_member(root, manifest, task_id)


def _contains_member(root, manifest, task_id, *, position=None):
    record = _read(root, _member_parts(task_id))
    return bool(record and record['task_id'] == task_id and record['position'] < manifest['count']
                and not record.get('removed') and (position is None or record['position'] == position))


def initialize(thread_path: Path, thread) -> None:
    root = history_root(thread_path)
    if _read(root, ('index.json',)) is not None:
        return
    # 旧线程唯一一次全量迁移；空索引也须显式发表，读路径不创建文件。
    ids = tuple(dict.fromkeys((*thread.task_ids, *thread.active_task_ids)))
    for start in range(0, len(ids), PAGE_SIZE):
        write_private_json_file_atomic_unlocked(root / 'pages' / f'{start // PAGE_SIZE}.json', list(ids[start:start + PAGE_SIZE]))
        for offset, identifier in enumerate(ids[start:start + PAGE_SIZE], start):
            write_private_json_file_atomic_unlocked(root.joinpath(*_member_parts(identifier)),
                                                   {'task_id': identifier, 'position': offset})
    write_private_json_file_atomic_unlocked(root / 'index.json', {'version': 1, 'count': len(ids), 'generation': 0,
        'incomplete': getattr(thread, 'task_history_version', 0) == 1})


def add(thread_path: Path, thread, task_id: str) -> None:
    initialize(thread_path, thread)
    if contains(thread_path, thread, task_id):
        return
    root = history_root(thread_path)
    manifest = _read(root, ('index.json',))
    position = manifest['count']
    page_path = root / 'pages' / f'{position // PAGE_SIZE}.json'
    page = _read(root, ('pages', page_path.name)) or []
    # 未发表尾页允许覆盖；manifest 是唯一已提交边界。
    page = page[:position % PAGE_SIZE] + [task_id]
    write_private_json_file_atomic_unlocked(page_path, page)
    write_private_json_file_atomic_unlocked(root.joinpath(*_member_parts(task_id)),
                                           {'task_id': task_id, 'position': position})
    write_private_json_file_atomic_unlocked(root / 'index.json', {**manifest, 'count': position + 1, 'generation': manifest.get('generation', 0) + 1})


def remove(thread_path: Path, thread, task_id: str) -> None:
    initialize(thread_path, thread)
    root = history_root(thread_path)
    parts = _member_parts(task_id)
    record = _read(root, parts)
    if record and record['task_id'] == task_id:
        if record.get('removed'):
            return
        write_private_json_file_atomic_unlocked(root.joinpath(*parts), {**record, 'removed': True})
        manifest = _read(root, ('index.json',))
        write_private_json_file_atomic_unlocked(root / 'index.json',
            {**manifest, 'generation': manifest.get('generation', 0) + 1})


def page(thread_path: Path, thread, *, cursor: int = 0, limit: int = PAGE_SIZE, allow_incomplete: bool = False):
    """返回 (ids, next_cursor)，游标是稳定物理位置；None表示已到末尾。"""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 0 < limit <= PAGE_SIZE:
        raise ValueError('任务历史分页需要非负游标及1至128的页大小')
    root = history_root(thread_path)
    manifest = _read(root, ('index.json',))
    if not allow_incomplete and ((manifest is None and getattr(thread, 'task_history_version', 0) == 1)
                                 or (manifest is not None and manifest.get('incomplete'))):
        raise FileNotFoundError('线程历史索引被删除，已知窗口仍可继续新任务但完整历史不可证明')
    if manifest is None:
        ids = thread.task_ids[cursor:cursor + limit]
        end = cursor + len(ids)
        return ids, end if end < len(thread.task_ids) else None
    count = manifest['count']
    stop = min(count, cursor + limit)
    result = []
    position = cursor
    while position < stop:
        number = position // PAGE_SIZE
        rows = _read(root, ('pages', f'{number}.json'))
        if rows is None:
            raise ValueError('任务历史分页缺失')
        end = min(stop, (number + 1) * PAGE_SIZE)
        for slot, identifier in enumerate(rows[position % PAGE_SIZE:position % PAGE_SIZE + end - position], position):
            if _contains_member(root, manifest, identifier, position=slot):
                result.append(identifier)
        position = end
    return tuple(result), stop if stop < count else None


def iter_all(thread_path: Path, thread):
    """仅显式历史导出/维护使用；常规查询调用page或contains。"""
    cursor = 0
    while cursor is not None:
        rows, cursor = page(thread_path, thread, cursor=cursor)
        yield from rows


def window(thread, task_id: str, *, active: bool):
    active_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id))) if active else tuple(
        value for value in thread.active_task_ids if value != task_id)
    recent = tuple(dict.fromkeys((*thread.task_ids, task_id)))[-PAGE_SIZE:]
    return tuple(dict.fromkeys((*recent, *active_ids))), active_ids


def recent(thread_path: Path, thread):
    manifest = _read(history_root(thread_path), ('index.json',))
    if manifest is None:
        return thread.task_ids[-PAGE_SIZE:]
    count = manifest['count']
    rows, _ = page(thread_path, thread, cursor=max(0, count - PAGE_SIZE), allow_incomplete=True)
    return rows


def member_paths(thread_path: Path):
    """显式会话归档收集分页家族，不移动锁文件。"""
    root = history_root(thread_path)
    manifest = _read(root, ('index.json',))
    if manifest is None:
        return
    yield root / 'index.json'
    for number in range((manifest['count'] + PAGE_SIZE - 1) // PAGE_SIZE):
        yield root / 'pages' / f'{number}.json'
        rows = _read(root, ('pages', f'{number}.json'))
        if rows is None:
            raise ValueError('任务历史分页缺失')
        for identifier in rows:
            yield root.joinpath(*_member_parts(identifier))
