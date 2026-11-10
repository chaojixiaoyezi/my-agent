"""从同线程旧回执定位原产物账本；位置仅为候选，发送仍核原登记版本。"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from ..common.nofollow_fs import (
    inspect_file_snapshot_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    read_tail_snapshot_beneath,
)
from ..common.path_segments import safe_path_segment
from ..gateway_parts.owner_retention import _RECEIPT_MAX_FILE_BYTES, lookup_sealed_receipt
from ..gateway_parts.paths import gateway_paths
from ..gateway_parts.request_owner_binding import frozen_request_owner
from ..gateway_parts.response_renderer import validate_gateway_terminal_envelope_report
from ..user_space.owner_resolver import resolve_owner_home

_RECENT_REQUEST_LIMIT = 8
_HISTORY_WINDOW_BYTES = 256 * 1024
_HISTORY_WINDOW_LINES = 80


def verified_recent_registry_roots(agent: object, trusted_scope: Mapping) -> tuple[tuple[Path, ...], tuple[dict, ...]]:
    """只消费宿主 scope 和同线程固定256KiB/80物理行；最多8份精确终态，坏/缺来源保 unknown。"""
    thread_id = str(trusted_scope.get('session_id') or '')
    request_id = str(trusted_scope.get('request_id') or '')
    if not thread_id:
        return (), ()
    home = getattr(agent, 'home_paths', None)
    owner_root = getattr(home, 'owner_home_dir', None)
    if not owner_root or not request_id or trusted_scope.get('owner_id') != getattr(home, 'owner_id', None):
        return (), (_error('scope_unconfirmed'),)
    try:
        owner_root = Path(owner_root).absolute()
        selected = _bounded_previous_ids(agent.conversation_store.storage, thread_id, request_id)
        if selected is None:
            return (), ({**_error('history_window_partial'), 'partial': True, 'window_reason': 'cold_transcript'},)
        roots = []
        paths = gateway_paths(agent)
        for previous_id in selected:
            report = _read_previous_receipt(paths, previous_id)
            if report.load_error or not report.payload:
                return (), (_error('receipt_unavailable'),)
            payload = report.payload
            previous_owner = frozen_request_owner(payload)
            if previous_owner is None:
                return (), (_error('owner_unconfirmed'),)
            location = resolve_owner_home(home.root, previous_owner)
            if location.owner_id != home.owner_id or location.home_dir.absolute() != owner_root:
                return (), (_error('owner_mismatch'),)
            runtime = payload.get('conversation_runtime')
            if (not isinstance(runtime, dict) or runtime.get('request_id') != previous_id
                    or runtime.get('thread_id') != thread_id or not runtime.get('task_id')):
                return (), (_error('request_binding_unconfirmed'),)
            raw = runtime.get('task_path')
            if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
                return (), (_error('registry_root_unconfirmed'),)
            root = Path(raw)
            relative = root.relative_to(owner_root)
            descriptor = open_directory_beneath(owner_root, relative.parts)
            os.close(descriptor)
            roots.append(root)
        # 近期窗口只能提供真命中来源，不能证明所有历史已穷尽。
        return tuple(dict.fromkeys(roots)), ({**_error('history_window_partial'), 'partial': True},)
    except Exception as exc:
        return (), (_error('source_unavailable', type(exc).__name__),)


def _bounded_previous_ids(storage, thread_id: str, current_request_id: str) -> list[str] | None:
    path = storage.message_path(thread_id)
    relative = path.relative_to(storage.root)
    # 冷布局必须经过其独立窗口合同，本读口不解析/扩扫 manifest。
    manifest = (*relative.parts[:-1], path.with_suffix('.manifest.json').name)
    if inspect_file_snapshot_beneath(storage.root, manifest) is not None:
        return None
    snapshot = read_tail_snapshot_beneath(storage.root, relative.parts, max_bytes=_HISTORY_WINDOW_BYTES)
    if inspect_file_snapshot_beneath(storage.root, manifest) is not None:
        return None
    if snapshot is None:
        return []
    data = snapshot.content
    if snapshot.stat.st_size > len(data):
        # 头部只可能是行片段；宁可少一个候选也不解析不完整来源。
        data = data.partition(b'\n')[2]
    lines = data.split(b'\n')[:-1]  # 未封口尾行不能贡献来源。
    selected = []
    for line in reversed(lines[-_HISTORY_WINDOW_LINES:]):
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, dict) or row.get('thread_id') != thread_id
                or not isinstance(row.get('message_id'), str) or not row['message_id']):
            raise ValueError('historical message identity unavailable')
        if row.get('role') != 'assistant':
            continue
        metadata = row.get('metadata')
        previous_id = metadata.get('gateway_request_id') if isinstance(metadata, dict) else None
        if previous_id is None or previous_id == current_request_id:
            continue
        if not isinstance(previous_id, str) or not previous_id or safe_path_segment(previous_id) != previous_id:
            raise ValueError('historical request identity unavailable')
        if previous_id not in selected:
            selected.append(previous_id)
        if len(selected) == _RECENT_REQUEST_LIMIT:
            break
    return selected


def _read_previous_receipt(paths, previous_id):
    relative = ('requests', 'terminal', f'{previous_id}.json')
    raw = read_bytes_beneath(paths.root, relative, max_bytes=_RECEIPT_MAX_FILE_BYTES)
    if raw is None:
        raw = lookup_sealed_receipt(paths, '/'.join(relative))
    if raw is None:
        from ..gateway_parts.io import GatewayJsonReadReport
        return GatewayJsonReadReport({})
    if len(raw) > _RECEIPT_MAX_FILE_BYTES:
        raise ValueError('historical receipt byte budget exhausted')
    return validate_gateway_terminal_envelope_report(json.loads(raw), request_id=previous_id,
                                                     context='artifact.registry.previous_request')


def _error(reason: str, error_type: str = '') -> dict:
    return {'context': 'artifact.registry.history', 'reason': reason,
            **({'error_type': error_type} if error_type else {})}
