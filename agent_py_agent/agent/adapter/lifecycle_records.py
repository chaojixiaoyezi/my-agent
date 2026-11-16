"""原交互 spool 的只读诊断；索引只定位，规范记录决定归属和状态。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from time import monotonic

from ..common.nofollow_fs import read_bytes_beneath
from ..gateway_parts.recovery_pending_index import private_projection_connection
from .delivery import PendingGatewayReply
from .ingress import GatewayIngressRecord
from .protocol import provider_message_reference


def _json(root, parts, budget):
    raw = read_bytes_beneath(root, parts, max_bytes=min(1024 * 1024, budget['bytes']))
    if raw is not None:
        budget['bytes'] -= len(raw)
    return None if raw is None else json.loads(raw)


def adapter_lifecycle_records(spool_root: Path, *, allowed_owners: frozenset[str] | None,
                              message_ref: str = '', request_id: str = '', request_ref: str = '',
                              limit: int = 20, cursor: str = '') -> dict:
    """每页至多核读100条/8MiB/25ms；稀疏授权页也推进位置，不建立任何目录或锁。"""
    deadline = monotonic() + 0.025
    root = Path(spool_root)
    limit = min(100, max(1, int(limit)))
    filters = {'message_ref': message_ref, 'request_id': request_id, 'request_ref': request_ref}
    try:
        source = (root / 'ingress' / 'index.sqlite3').lstat()
    except OSError:
        return {'matches': [], 'next_cursor': '', 'coverage': {'state': 'unavailable', 'next_step': '等待原 spool 投影可读；缺失不表示未发送'}}
    binding = hashlib.sha256(json.dumps([str(root), (source.st_dev, source.st_ino), sorted(allowed_owners) if allowed_owners is not None else None,
                                       filters], sort_keys=True).encode()).hexdigest()
    after = ''
    prior_ambiguous = False
    if cursor:
        try:
            token = json.loads(cursor)
            if token['binding'] != binding or not re.fullmatch('[0-9a-f]{64}', token['after']):
                raise ValueError('cursor scope mismatch')
            after = token['after']
            prior_ambiguous = bool(message_ref or request_ref)
        except (ValueError, TypeError, KeyError):
            return {'matches': [], 'next_cursor': '', 'coverage': {'state': 'invalid_cursor'}}
    clauses = ['ingress_id > ?']
    args = [after]
    if allowed_owners is not None:
        if not allowed_owners:
            return {'matches': [], 'next_cursor': '', 'coverage': {'state': 'complete', 'visited': 0}}
    for field, value in filters.items():
        if value:
            clauses.append(field + ' = ?')
            args.append(value)
            break
    try:
        with private_projection_connection(root / 'ingress' / 'index.sqlite3', read_only=True, blocking=False, deadline=deadline) as db:
            rows = db.execute('SELECT ingress_id,owner_id FROM ingress_correlation WHERE ' + ' AND '.join(clauses) +
                              ' ORDER BY ingress_id LIMIT ?', (*args, limit + 1)).fetchall()
            legacy = db.execute("SELECT value FROM meta WHERE key='legacy'").fetchone()
            correlation = db.execute("SELECT value FROM meta WHERE key='correlation_legacy'").fetchone()
            incomplete = legacy is None or legacy[0] != '0' or correlation is None or correlation[0] != '0' or db.execute('SELECT 1 FROM intents LIMIT 1').fetchone() is not None
    except (OSError, sqlite3.Error):
        return {'matches': [], 'next_cursor': '', 'coverage': {'state': 'unavailable', 'next_step': '等待原 spool 维护补齐投影；缺失不表示未发送'}}
    budget = {'bytes': 8 * 1024 * 1024}
    stopped = False
    visited = 0
    matches = []
    identities = set()
    problems = []
    for key, projected_owner in rows[:limit]:
        if monotonic() >= deadline or budget['bytes'] <= 0:
            stopped = True
            break
        visited += 1
        after = key
        if allowed_owners is not None and projected_owner not in allowed_owners:
            continue
        if not re.fullmatch('[0-9a-f]{64}', key):
            problems.append('unreadable')
            continue
        try:
            data = _json(root, ('ingress', 'records', key + '.json'), budget)
            if data is None:
                problems.append('expired_or_missing')
                continue
            record = GatewayIngressRecord.from_dict(data)
            if record.ingress_id != key or (allowed_owners is not None and record.owner_id not in allowed_owners):
                continue
            submission = record.submission or {}
            rid = str(submission.get('request_id') or '')
            facts = {'message_ref': provider_message_reference(record.provider_message_id),
                     'request_id': rid, 'request_ref': provider_message_reference(rid)}
            if any(value and facts[field] != value for field, value in filters.items()):
                continue
            identities.add((record.provider_message_id if message_ref else '', rid if request_ref else ''))
            stable = str(submission.get('operation_id') or rid)
            summaries = []
            if stable:
                receipt = _json(root, ('sent', hashlib.sha256(stable.encode()).hexdigest() + '.json'), budget)
                if isinstance(receipt, dict):
                    summaries.append({'state': str(receipt.get('disposition') or 'sent'),
                                      'sent_at': receipt.get('sent_at'),
                                      'dispatch_started_at': receipt.get('dispatch_started_at'),
                                      'dispatch_completed_at': receipt.get('dispatch_completed_at'),
                                      'dispatch_duration_seconds': receipt.get('dispatch_duration_seconds')})
                else:
                    pending_data = _json(root, ('pending', hashlib.sha256(stable.encode()).hexdigest() + '.json'), budget)
                    pending = PendingGatewayReply.from_dict(pending_data) if isinstance(pending_data, dict) else None
                    same = pending is not None and pending.stable_id == stable and pending.message_id == record.provider_message_id
                    summaries.append({'state': 'unknown_dispatch' if same and pending.dispatch else 'pending' if same else 'missing',
                                      'delivery_outcome': 'unknown'})
            matches.append({'ingress_id': key, **facts, 'owner_id': record.owner_id, 'state': record.state,
                            'target_turn_id': str(submission.get('target_turn_id') or ''),
                            'receipt_id': str(submission.get('receipt_id') or ''),
                            'operation_id': str(submission.get('operation_id') or ''),
                            'timestamps': {'created_at': record.created_at, 'updated_at': record.updated_at},
                            'reply_receipt_summaries': summaries})
        except (OSError, ValueError, TypeError):
            problems.append('unreadable')
    more = len(rows) > limit or stopped
    ambiguous = (prior_ambiguous or len(identities) > 1 or more) and bool(message_ref or request_ref)
    state = 'ambiguous' if ambiguous else 'partial' if incomplete or problems or more else 'complete'
    return {'matches': [] if ambiguous else matches,
            'next_cursor': json.dumps({'binding': binding, 'after': after, 'ambiguous': ambiguous}) if more else '',
            'coverage': {'state': state, 'visited': visited, 'problems': problems,
                         'receipt_retention_days': 7, 'missing_receipt_means': 'unknown'}}
