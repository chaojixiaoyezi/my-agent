"""候选当前态增量账本：唯一头指向当前树，所有节点均先规划后准入。

锁、配额与审核权仍归 CandidateService。本模块不解释审核状态；仅提供按身份
和序号的惰性当前态访问以及同一次提交中的观察/证据绑定索引。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import MutableSequence
from copy import deepcopy
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.store_io import ImmutableJsonIndex

FORMAT = 'memory-candidates-v2'


def read_manifest(path: Path):
    try:
        return _read_manifest(path)
    except (ValueError, TypeError, KeyError) as error:
        from .candidates import CandidateStoreCorruptError
        raise CandidateStoreCorruptError('candidate indexed header is unreadable') from error


def _read_manifest(path: Path):
    # 旧JSONL首行可能非常大；只探测格式前缀，不对旧库额外整文件读。
    import os

    from ..common.nofollow_fs import open_readonly_file_beneath
    try:
        fd = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        prefix = stream.read(64)
        if not prefix.startswith(b'{"candidate_ledger":"memory-candidates-v2",'):
            return None
        raw = prefix + stream.read(1024 * 1024)
        if stream.read(1):
            raise ValueError('候选索引头超出预算')
    return _validate_manifest(json.loads(raw))


def _validate_manifest(record):
    if (type(record) is not dict or set(record) != {'candidate_ledger', 'count', 'root', 'cleanup'}
            or record.get('candidate_ledger') != FORMAT or type(record.get('count')) is not int or record['count'] < 0
            or (record['count'] > 0 and not isinstance(record['root'], dict))
            or (record['root'] is not None and not isinstance(record['root'], dict))
            or not isinstance(record['cleanup'], (list, dict))):
        raise ValueError('候选索引头损坏')
    return record


def manifest_text(root, count, cleanup=()):
    # 固定首字段允许只读探测识别格式；不依赖自然语言或文件扩展名。
    return json.dumps({'candidate_ledger': FORMAT, 'count': count, 'root': root,
                       'cleanup': cleanup if isinstance(cleanup, dict) else list(cleanup)}, ensure_ascii=False, separators=(',', ':')) + '\n'


def _read_candidate_row(index, reference, *, staged=None, loaded=None):
    name = reference['ref']
    digest = reference['sha256']
    if (type(digest) is not str or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)
            or name != digest + '.row' or type(reference['byte_length']) is not int
            or reference['byte_length'] <= 0):
        raise ValueError('候选正文引用损坏')
    raw = (staged or {}).get(name)
    if raw is None:
        raw = read_bytes_beneath(index.root, (*index.parts, name), max_bytes=reference['byte_length'])
        if loaded is not None:
            loaded.add(name)
    if raw is None or len(raw) != reference['byte_length'] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('候选正文缺失或摘要不符')
    from .candidates import _parse_candidate_line
    return _parse_candidate_line(raw.decode())


def iter_candidate_rows(path: Path, manifest):
    """只读完整位置流；正文逐条释放，不保留修改基线、历史行或已读节点集合。"""
    header = _validate_manifest(manifest)
    index = ImmutableJsonIndex(path.parent, (path.name + ".records",))
    root = header["root"]
    for position in range(header["count"]):
        identifier = index.get(root, "pos/" + str(position))
        entry = index.get(root, "row/" + identifier) if identifier else None
        if not entry or entry["position"] != position:
            raise ValueError("候选序号索引损坏")
        row = _read_candidate_row(index, entry["record"])
        if row.candidate_id != identifier:
            raise ValueError("候选身份索引损坏")
        yield row


class PlannedIndex(ImmutableJsonIndex):
    """覆盖存储叶，复用通用HAMT路由；规划期间零文件写入。"""
    def __init__(self, path):
        super().__init__(path.parent, (path.name + '.records',))
        self.staged = {}
        self.loaded = set()
        self.obsolete = set()

    def _save(self, node):
        raw = (json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(raw) > 1024 * 1024:
            raise ValueError('候选索引节点超出预算')
        digest = hashlib.sha256(raw).hexdigest()
        self.staged[digest + '.json'] = raw
        values = node.get('entries', {}).values() if 'entries' in node else (
            child.get('maximum') for child in node['children'].values())
        return {'ref': {'sha256': digest, 'byte_length': len(raw), 'ref': digest + '.json'},
                'maximum': self._maximum(values)}

    def _load(self, reference):
        name = reference['ref']['ref']
        if name in self.staged:
            return json.loads(self.staged[name])
        self.loaded.add(name)
        return super()._load(reference)

    def _put(self, reference, key, value, route, depth):
        result = super()._put(reference, key, value, route, depth)
        if reference is not None and result != reference:
            self.obsolete.add(reference['ref']['ref'])
        return result

    def remove(self, reference, key, depth=0):
        if reference is None:
            return None
        node = self._load(reference)
        if 'entries' in node:
            entries = dict(node['entries'])
            if key not in entries:
                return reference
            del entries[key]
            result = self._save({'entries': entries}) if entries else None
        else:
            digit = self._route(key)[depth]
            children = dict(node['children'])
            child = self.remove(children.get(digit), key, depth + 1)
            if child is None:
                children.pop(digit, None)
            else:
                children[digit] = child
            result = self._save({'children': children}) if children else None
        if result != reference:
            self.obsolete.add(reference['ref']['ref'])
        return result

    def save_row(self, row):
        raw = (json.dumps(deepcopy(row).to_record(), ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        digest = hashlib.sha256(raw).hexdigest()
        name = digest + '.row'
        self.staged[name] = raw
        return {'ref': name, 'sha256': digest, 'byte_length': len(raw)}

    def read_row(self, reference):
        return _read_candidate_row(self, reference, staged=self.staged, loaded=self.loaded)

    def finish(self, root):
        """仅发表新根可达暂存节点；被重写路径和正文在发表后可安全回收。"""
        reachable, protected, pending = set(), set(), [root] if root is not None else []
        while pending:
            ref = pending.pop()
            name = ref['ref']['ref']
            protected.add(name)
            if name not in self.staged or name in reachable:
                continue
            reachable.add(name)
            node = json.loads(self.staged[name])
            if 'children' in node:
                pending.extend(node['children'].values())
            else:
                for key, value in node['entries'].items():
                    if key.startswith('statusroot/') and value is not None:
                        pending.append(value)
                    if key.startswith('row/') and value:
                        blob = value['record']['ref']
                        protected.add(blob)
                        if blob in self.staged:
                            reachable.add(blob)
        return {name: self.staged[name] for name in reachable}, sorted(self.obsolete - protected)


class CandidateRows(MutableSequence):
    """按需读取可变工作副本；单条更新只加载命中项，不复制或扫描全表。"""
    def __init__(self, path, manifest):
        self.path, self.manifest = path, manifest
        self.index = PlannedIndex(path)
        self.root = manifest['root']
        self.count = manifest['count']
        self.loaded, self.baselines, self.ids = {}, {}, {}

    def __len__(self):
        return self.count

    def __getitem__(self, position):
        if isinstance(position, slice):
            return [self[i] for i in range(*position.indices(self.count))]
        if position < 0:
            position += self.count
        if not 0 <= position < self.count:
            raise IndexError(position)
        if position not in self.loaded:
            identifier = self.index.get(self.root, 'pos/' + str(position))
            entry = self.index.get(self.root, 'row/' + identifier) if identifier else None
            if not entry or entry['position'] != position:
                raise ValueError('候选序号索引损坏')
            row = self.index.read_row(entry['record'])
            if row.candidate_id != identifier:
                raise ValueError('候选身份索引损坏')
            self.loaded[position] = row
            self.baselines[position] = deepcopy(row)
            self.ids[identifier] = position
        return self.loaded[position]

    def __setitem__(self, position, value):
        current = self[position]
        if current.candidate_id != value.candidate_id:
            raise ValueError('候选更新不能改变身份')
        self.loaded[position] = value

    def __delitem__(self, position):
        raise TypeError('候选删除须经过显式保留清理规划')

    def insert(self, position, value):
        if position != self.count:
            raise TypeError('候选只允许追加')
        if self.index_for(value.candidate_id, None) is not None:
            raise ValueError('候选身份重复')
        self.loaded[position] = value
        self.baselines[position] = None
        self.ids[value.candidate_id] = position
        self.count += 1

    def index_for(self, identifier, default=...):
        if identifier in self.ids:
            return self.ids[identifier]
        record = self.index.get(self.root, 'row/' + identifier)
        if record is not None:
            position = record.get('position')
            if type(position) is not int or not 0 <= position < self.count:
                raise ValueError('候选身份序号损坏')
            return position
        if default is not ...:
            return default
        raise KeyError(identifier)

    def fork(self):
        other = CandidateRows(self.path, self.manifest)
        other.index, other.root, other.count = self.index, self.root, self.count
        other.loaded, other.baselines, other.ids = deepcopy(self.loaded), self.baselines.copy(), self.ids.copy()
        return other

    def observation_positions(self, key):
        identifiers = self.index.get(self.root, 'obs/' + key) or []
        return {self.index_for(identifier) for identifier in identifiers}

    def binding_versions(self, key):
        return set(self.index.get(self.root, 'binding/' + json.dumps(key, separators=(',', ':'))) or [])

    def select_statuses(self, statuses):
        positions = set()
        for status in statuses:
            reference = self.index.get(self.root, 'statusroot/' + status)
            for _, position in self.index.items(reference):
                positions.add(position)
        return [self[position] for position in positions]

    def changed(self):
        return [(position, row, self.baselines.get(position)) for position, row in self.loaded.items()
                if row != self.baselines.get(position)]

    def plan(self):
        from .candidate_target_binding import binding_history
        from .candidates import _rejected_draft
        root = self.root
        changes = self.changed()
        observations, bindings, status_roots = {}, {}, {}
        counts = deepcopy(self.index.get(self.root, 'counts') or {'statuses': {}, 'types': {}})
        for position, row, old in changes:
            for field, current in (('statuses', old.status if old else None), ('types', old.candidate_type if old else None)):
                if current is not None:
                    counts[field][current] = counts[field].get(current, 0) - 1
                    if not counts[field][current]:
                        counts[field].pop(current)
            counts['statuses'][row.status] = counts['statuses'].get(row.status, 0) + 1
            counts['types'][row.candidate_type] = counts['types'].get(row.candidate_type, 0) + 1
            if old is not None and old.status != row.status:
                previous = status_roots.setdefault(old.status, self.index.get(self.root, 'statusroot/' + old.status))
                status_roots[old.status] = self.index.remove(previous, row.candidate_id)
            current = status_roots.setdefault(row.status, self.index.get(self.root, 'statusroot/' + row.status))
            status_roots[row.status] = self.index.put(current, row.candidate_id, position)
            if old:
                oldref = self.index.get(root, 'row/' + old.candidate_id)
                if oldref:
                    self.index.obsolete.add(oldref['record']['ref'])
            record = self.index.save_row(row)
            root = self.index.put(root, 'row/' + row.candidate_id, {'position': position, 'record': record})
            root = self.index.put(root, 'pos/' + str(position), row.candidate_id)
            oldkeys = set(old.observation_keys) if old and not _rejected_draft(old) else set()
            newkeys = set(row.observation_keys) if not _rejected_draft(row) else set()
            for key in oldkeys | newkeys:
                values = observations.setdefault(key, set(self.index.get(self.root, 'obs/' + key) or []))
                values.discard(row.candidate_id)
                if key in newkeys:
                    values.add(row.candidate_id)
            # 已有版本绑定只增不减；保留清理会保留墓碑，不让删除候选刷新旧证据。
            for binding in binding_history(row):
                key = json.dumps(binding['key'], separators=(',', ':'))
                bindings.setdefault(key, set(self.index.get(self.root, 'binding/' + key) or [])).add(binding['version'])
        for status, reference in status_roots.items():
            root = self.index.put(root, 'statusroot/' + status, reference)
        root = self.index.put(root, 'counts', counts)
        for key, values in observations.items():
            root = self.index.put(root, 'obs/' + key, sorted(values))
        for key, values in bindings.items():
            root = self.index.put(root, 'binding/' + key, sorted(values, key=lambda value: -1 if value is None else value))
        writes, cleanup = self.index.finish(root)
        return root, writes, cleanup


class CandidateIdPositions:
    def __init__(self, rows):
        self.rows = rows

    def get(self, key, default=None):
        return self.rows.index_for(key, default)

    def __getitem__(self, key):
        return self.rows.index_for(key)

    def __setitem__(self, key, value):
        self.rows.ids[key] = value


class ObservationPositions(dict):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def get(self, key, default=None):
        if key not in self:
            self[key] = self.rows.observation_positions(key)
        return super().get(key, default)

    def setdefault(self, key, default=None):
        self.get(key)
        return super().setdefault(key, default)


class IndexedBindings:
    def __init__(self, rows):
        self.rows, self.added = rows, {}

    def conflicts(self, observation):
        from .candidate_target_binding import _binding_keys
        for key in _binding_keys(observation):
            versions = self.rows.binding_versions(key) | self.added.get(key, set())
            if versions and versions != {observation.target_entry_version}:
                return True
        return False

    def add(self, observation):
        from .candidate_target_binding import binding_history
        for binding in binding_history(observation):
            self.added.setdefault(tuple(binding['key']), set()).add(binding['version'])


def seal_cleanup(writes, cleanup):
    if not cleanup:
        return []
    raw = (json.dumps(cleanup, separators=(',', ':')) + '\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    name = digest + '.cleanup'
    writes[name] = raw
    return {'ref': name, 'sha256': digest, 'byte_length': len(raw)}
