"""候选原HAMT内的精确经验锚点投影；正文和权限仍以原候选行核验。"""
from __future__ import annotations

import hashlib
import json

STATUSES = frozenset({'pending_review', 'approved', 'observed', 'blocked_missing_evidence'})


def group_key(scope, candidate_type):
    raw = json.dumps([scope['scope_type'], scope['scope_key'], candidate_type], ensure_ascii=False, separators=(',', ':'))
    return 'anchorroot/' + hashlib.sha256(raw.encode()).hexdigest()


def eligible(row):
    return row.status in STATUSES and row.candidate_type in {'lesson', 'hot_rule'} and row.proposed_action == 'add'


def rank(row):
    return [row.last_observed_at, row.candidate_id]


def plan_anchors(rows, root, changes):
    groups = {}
    index = rows.index
    valid = source_valid(rows)
    generation = (index.get(rows.root, 'anchor/generation') or 'initial') if valid else rows.root['ref']['sha256']
    catalog = index.get(rows.root, 'anchor/groups') if valid else None
    if not valid:
        previous = index.get(rows.root, 'anchor/groups')
        if previous is not None:
            index.obsolete.add(previous['ref']['ref'])
        # 仅已发布v1原型的首次转换需要枚举元数据键；不读正文，之后换代只解绑单个目录引用。
        if index.get(rows.root, 'anchor/version') == 1 and index.get(rows.root, 'anchor/generation') is None:
            for key, _ in index.items(rows.root):
                if key.startswith('anchorroot/'):
                    root = index.remove(root, key)
        root = index.put(root, 'anchor/version', 0)
        root = index.put(root, 'anchor/progress', 0)
    root = index.put(root, 'anchor/generation', generation)
    for _, row, old in changes:
        if old is not None and eligible(old):
            key = group_key(old.scope, old.candidate_type)
            if key not in groups:
                groups[key] = index.get(catalog, key)
            previous = groups[key]
            groups[key] = index.remove(previous, old.candidate_id)
        if eligible(row):
            key = group_key(row.scope, row.candidate_type)
            if key not in groups:
                groups[key] = index.get(catalog, key)
            previous = groups[key]
            groups[key] = index.put(previous, row.candidate_id, {'anchor_rank': rank(row), 'candidate_id': row.candidate_id})
    for key, reference in groups.items():
        catalog = index.put(catalog, key, reference) if reference is not None else index.remove(catalog, key)
    root = index.put(root, 'anchor/groups', catalog)
    end = getattr(rows, 'anchor_build_end', None)
    if rows.root is None or end is not None and end == len(rows):
        root = index.put(root, 'anchor/version', 1)
    if end is not None:
        root = index.put(root, 'anchor/progress', end)
    return seal_root(index, root)


def seal_root(index, root):
    # 保留seal键，只中和其值；插入/删除第九键会改变HAMT拓扑，remove并非put的逆。
    unsigned = index.put(root, 'anchor/seal', None)
    return index.put(unsigned, 'anchor/seal', unsigned['ref']['sha256'])


def source_valid(rows):
    """seal值固定为空后的规范根摘要是写者覆盖证明；旧写者改任意其它键都会自然撤销。"""
    if rows.root is None:
        return True
    cached = getattr(rows, '_anchor_source_valid', None)
    if cached is not None:
        return cached
    seal = rows.index.get(rows.root, 'anchor/seal')
    unsigned = rows.index.put(rows.root, 'anchor/seal', None)
    valid = isinstance(seal, str) and unsigned is not None and seal == unsigned['ref']['sha256']
    rows._anchor_source_valid = valid
    return valid


def ready(rows):
    if not source_valid(rows):
        return False
    version = rows.index.get(rows.root, 'anchor/version')
    if version is None or type(version) is int and version == 0:
        return False
    if type(version) is not int or version != 1:
        raise ValueError('candidate anchor index version is invalid')
    return True


def read_anchors(rows, scopes):
    index = rows.index
    if not ready(rows):
        raise ValueError('candidate anchor index is incomplete')
    groups = {}
    catalog = index.get(rows.root, 'anchor/groups')
    for scope_type, scope_key in scopes:
        for candidate_type in ('lesson', 'hot_rule'):
            key = group_key({'scope_type': scope_type, 'scope_key': scope_key}, candidate_type)
            reference = index.get(catalog, key)
            if reference is not None:
                groups[key] = reference
    result, counts = [], {}
    while groups and len(result) < 12:
        key = max(groups, key=lambda key: groups[key]['maximum']['anchor_rank'])
        reference = groups[key]
        value = reference['maximum']
        identifier = value['candidate_id']
        # 最大值必须来自该组真实成员；根/每级摘要同时受原不可变节点封印约束。
        current = reference
        for depth in range(65):
            node = index._load(current)
            values = node['entries'].values() if 'entries' in node else (child['maximum'] for child in node['children'].values())
            if index._maximum(values) != current['maximum']:
                raise ValueError('candidate anchor maximum is invalid')
            if 'entries' in node:
                if node['entries'].get(identifier) != value:
                    raise ValueError('candidate anchor maximum is not a member')
                break
            current = node['children'].get(index._route(identifier)[depth])
            if current is None or current['maximum'] != value:
                raise ValueError('candidate anchor maximum branch is invalid')
        else:
            raise ValueError('candidate anchor depth is invalid')
        position = rows.index_for(identifier, None)
        if position is None:
            raise ValueError('candidate anchor identity is missing')
        row = rows[position]
        if (not eligible(row) or group_key(row.scope, row.candidate_type) != key
                or rank(row) != value['anchor_rank']):
            raise ValueError('candidate anchor projection changed')
        result.append(row)
        counts[key] = counts.get(key, 0) + 1
        following = index.remove(reference, identifier) if counts[key] < 3 else None
        if following is None:
            del groups[key]
        else:
            groups[key] = following
    return tuple(result)
