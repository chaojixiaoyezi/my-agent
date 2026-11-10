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
    for _, row, old in changes:
        if old is not None and eligible(old):
            key = group_key(old.scope, old.candidate_type)
            if key not in groups:
                groups[key] = index.get(rows.root, key)
            previous = groups[key]
            groups[key] = index.remove(previous, old.candidate_id)
        if eligible(row):
            key = group_key(row.scope, row.candidate_type)
            if key not in groups:
                groups[key] = index.get(rows.root, key)
            previous = groups[key]
            groups[key] = index.put(previous, row.candidate_id, {'anchor_rank': rank(row), 'candidate_id': row.candidate_id})
    for key, reference in groups.items():
        root = index.put(root, key, reference)
    end = getattr(rows, 'anchor_build_end', None)
    if rows.root is None or end is not None and end == len(rows):
        root = index.put(root, 'anchor/version', 1)
    if end is not None:
        root = index.put(root, 'anchor/progress', end)
    return root


def ready(rows):
    version = rows.index.get(rows.root, 'anchor/version')
    if version is None:
        return False
    if type(version) is not int or version != 1:
        raise ValueError('candidate anchor index version is invalid')
    return True


def read_anchors(rows, scopes):
    index = rows.index
    if not ready(rows):
        raise ValueError('candidate anchor index is incomplete')
    groups = {}
    for scope_type, scope_key in scopes:
        for candidate_type in ('lesson', 'hot_rule'):
            key = group_key({'scope_type': scope_type, 'scope_key': scope_key}, candidate_type)
            reference = index.get(rows.root, key)
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
