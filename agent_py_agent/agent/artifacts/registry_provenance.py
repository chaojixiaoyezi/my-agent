"""从同线程旧回执定位原产物账本；位置仅为候选，发送仍核原登记版本。"""
from __future__ import annotations

import io
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from ..common import nofollow_fs as managed_fs
from ..common.nofollow_fs import (
    inspect_file_snapshot_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    read_bytes_snapshot_beneath,
    read_tail_snapshot_beneath,
)
from ..common.path_segments import safe_path_segment
from ..conversation.message_scan import _transcript_prefix_matches, _validate_transcript_manifest
from ..gateway_parts.owner_retention import _RECEIPT_MAX_FILE_BYTES, lookup_sealed_receipt
from ..gateway_parts.paths import gateway_paths_from_root
from ..gateway_parts.request_owner_binding import frozen_request_owner
from ..gateway_parts.response_renderer import validate_gateway_terminal_envelope_report
from ..user_space.owner_resolver import resolve_owner_home

_RECENT_REQUEST_LIMIT = 8
_HISTORY_WINDOW_BYTES = 256 * 1024
_HISTORY_MAX_BYTES = 1_048_576
_HISTORY_WINDOW_LINES = 80
_MANIFEST_MAX_BYTES = 65536


def gateway_root_from_host_params(agent: object, params: object) -> Path | None:
    """从原宿主认领路径取共享队列根；不从模型参数或执行 owner 的相对目录猜测。"""
    if getattr(params, 'source', '') != 'gateway':
        return None
    request_id = str(getattr(params, 'request_id', '') or '')
    attrs = getattr(params, 'task_attributes', None)
    thread_id = attrs.get('conversation_thread_id') if isinstance(attrs, dict) else None
    paths = getattr(params, 'recovery_content_paths', None)
    home = getattr(agent, 'home_paths', None)
    if not request_id or safe_path_segment(request_id) != request_id or not thread_id or not isinstance(paths, (list, tuple)):
        return None
    try:
        candidates = [Path(value) for value in paths if isinstance(value, (str, Path))]
        for claimed in candidates:
            if (not claimed.is_absolute() or claimed.name != request_id + '.json'
                    or claimed.parent.name != 'processing' or claimed.parent.parent.name != 'requests'):
                continue
            root = claimed.parents[2]
            if root / 'responses' / claimed.name not in candidates:
                continue
            raw = read_bytes_beneath(root, claimed.relative_to(root).parts, max_bytes=_RECEIPT_MAX_FILE_BYTES)
            payload = json.loads(raw) if raw is not None else {}
            runtime = payload.get('conversation_runtime')
            if (payload.get('id') != request_id or not _same_owner(home, payload)
                    or not isinstance(runtime, dict) or runtime.get('request_id') != request_id
                    or runtime.get('thread_id') != thread_id):
                continue
            return root
    except Exception:
        return None
    return None


def verified_recent_registry_roots(agent: object, trusted_scope: Mapping, *, gateway_root: Path | None = None
                                   ) -> tuple[tuple[Path, ...], tuple[dict, ...]]:
    """固定两级热尾/8份原终态；坏候选不授权，也不覆盖另一份独立已核来源。"""
    thread_id = str(trusted_scope.get('session_id') or '')
    request_id = str(trusted_scope.get('request_id') or '')
    if not thread_id:
        return (), ()
    home = getattr(agent, 'home_paths', None)
    owner_root = getattr(home, 'owner_home_dir', None)
    if not owner_root or not request_id or trusted_scope.get('owner_id') != getattr(home, 'owner_id', None):
        return (), (_error('scope_unconfirmed'),)
    try:
        configured = Path(getattr(getattr(agent, 'config', None), 'gateway_workspace', ''))
        if gateway_root is None:
            if not configured.is_absolute():
                return (), (_error('gateway_root_unconfirmed'),)
            gateway_root = configured
        gateway_root = Path(gateway_root)
        if not gateway_root.is_absolute() or '..' in gateway_root.parts:
            return (), (_error('gateway_root_unconfirmed'),)
        paths = gateway_paths_from_root(gateway_root)
        owner_root = Path(owner_root).absolute()
        selected = _bounded_previous_ids(agent.conversation_store.storage, thread_id, request_id)
        if selected is None:
            return (), ({**_error('history_window_partial'), 'partial': True, 'window_reason': 'cold_transcript'},)
        roots, failures = [], []
        for previous_id in selected:
            try:
                root, error = _verified_previous_root(home, owner_root, paths, previous_id, thread_id)
            except Exception as exc:
                root, error = None, _error('source_unavailable', type(exc).__name__)
            if error:
                failures.append(error)
            elif root is not None:
                roots.append(root)
        coverage = {**_error('history_window_partial'), 'partial': True}
        if failures:
            coverage['source_errors'] = failures
        return tuple(dict.fromkeys(roots)), (coverage,)
    except Exception as exc:
        return (), (_error('source_unavailable', type(exc).__name__),)


def _same_owner(home, payload):
    previous_owner = frozen_request_owner(payload)
    if previous_owner is None or home is None:
        return False
    location = resolve_owner_home(home.root, previous_owner)
    return (location.owner_id == home.owner_id
            and location.home_dir.absolute() == Path(home.owner_home_dir).absolute())


def _verified_previous_root(home, owner_root, paths, previous_id, thread_id):
    report = _read_previous_receipt(paths, previous_id)
    if report.load_error or not report.payload:
        return None, _error('receipt_unavailable')
    payload = report.payload
    if not _same_owner(home, payload):
        return None, _error('owner_unconfirmed')
    runtime = payload.get('conversation_runtime')
    if (not isinstance(runtime, dict) or runtime.get('request_id') != previous_id
            or runtime.get('thread_id') != thread_id or not runtime.get('task_id')):
        return None, _error('request_binding_unconfirmed')
    raw = runtime.get('task_path')
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        return None, _error('registry_root_unconfirmed')
    root = Path(raw)
    parts = root.relative_to(owner_root).parts
    managed_fs._validate_relative_parts(parts)
    if managed_fs._supports_dir_fd():
        descriptor = open_directory_beneath(owner_root, parts)
        os.close(descriptor)
    else:
        # 这里只验证候选目录，不授最终文件读权；Windows CRT不能打开目录FD。
        managed_fs._portable_directory(owner_root, parts, create=False)
        for depth in range(len(parts) + 1):
            info = owner_root.joinpath(*parts[:depth]).lstat()
            if getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                raise ValueError('registry candidate contains reparse directory')
    return root, None


def _bounded_previous_ids(storage, thread_id: str, current_request_id: str) -> list[str] | None:
    path = storage.message_path(thread_id)
    relative = path.relative_to(storage.root)
    manifest_parts = (*relative.parts[:-1], path.with_suffix('.manifest.json').name)
    manifest = read_bytes_snapshot_beneath(storage.root, manifest_parts, max_bytes=_MANIFEST_MAX_BYTES)
    if manifest is not None:
        state = json.loads(manifest.content)
        if state.get('phase') == 'prepared':
            return None
        _validate_transcript_manifest(state, path)
        proof = state.get('hot_frontier')
        if (not isinstance(proof, dict) or type(proof.get('bytes')) is not int
                or not 0 <= proof['bytes'] <= _HISTORY_MAX_BYTES or not state.get('rotation_id')):
            return None
        # 只有整个热区在常量预算内才核其已提交前缀；不触碰任何冷段/冷索引。
        hot = inspect_file_snapshot_beneath(storage.root, relative.parts)
        if hot is None or hot.stat.st_size > _HISTORY_MAX_BYTES:
            return None
        snapshot = read_bytes_snapshot_beneath(storage.root, relative.parts, max_bytes=_HISTORY_MAX_BYTES)
        if snapshot is None or not _transcript_prefix_matches(io.BytesIO(snapshot.content), state):
            raise ValueError('canonical hot transcript frontier unavailable')
        selected, _ = _parse_recent_ids(snapshot.content, thread_id, current_request_id)
    else:
        snapshot = read_tail_snapshot_beneath(storage.root, relative.parts, max_bytes=_HISTORY_WINDOW_BYTES)
        if snapshot is None:
            return []
        data = snapshot.content
        if snapshot.stat.st_size > len(data):
            data = data.partition(b'\n')[2]
        selected, assistants = _parse_recent_ids(data, thread_id, current_request_id)
        if not assistants and snapshot.stat.st_size > _HISTORY_WINDOW_BYTES:
            # 一次严格封顶的第二级，不按历史大小继续扩倍。
            second = read_tail_snapshot_beneath(storage.root, relative.parts, max_bytes=_HISTORY_MAX_BYTES)
            if second is None or _snapshot_stamp(second) != _snapshot_stamp(snapshot):
                raise ValueError('historical transcript changed between windows')
            data = second.content
            if second.stat.st_size > len(data):
                data = data.partition(b'\n')[2]
            selected, _ = _parse_recent_ids(data, thread_id, current_request_id)
    current = read_bytes_snapshot_beneath(storage.root, manifest_parts, max_bytes=_MANIFEST_MAX_BYTES)
    if (manifest is None) != (current is None) or (manifest is not None and (manifest.content != current.content
                                                      or _snapshot_stamp(manifest) != _snapshot_stamp(current))):
        return None
    return selected


def _snapshot_stamp(snapshot):
    return (tuple(getattr(snapshot.stat, key) for key in
                  ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns')),
            snapshot.changed_ns, snapshot.parents)


def _parse_recent_ids(data, thread_id, current_request_id):
    selected, assistants = [], 0
    for line in reversed(data.split(b'\n')[:-1][-_HISTORY_WINDOW_LINES:]):
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, dict) or row.get('thread_id') != thread_id
                or not isinstance(row.get('message_id'), str) or not row['message_id']):
            raise ValueError('historical message identity unavailable')
        if row.get('role') != 'assistant':
            continue
        assistants += 1
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
    return selected, assistants


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
