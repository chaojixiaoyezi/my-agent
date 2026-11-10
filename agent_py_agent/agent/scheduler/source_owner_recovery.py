"""原 owner 分页发现内重建来源投影；不构造 Agent、队列或历史仓库。"""
from __future__ import annotations

from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import regular_file_exists_beneath
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home


def repair_source_owner(index, root, owner, home):
    from ..assets.workspace_assets import _BATCH_FILE, _read_lock
    from .repository import (
        _parse_json_object_bytes,
        _read_store_bytes,
        parse_scheduler_job,
        validate_scheduler_store,
    )

    index._ensure_schema()
    identity = OwnerIdentity(*owner)
    resolved = resolve_owner_home(root, identity)
    if Path(home) != resolved.home_dir:
        raise ValueError('EVENT_SOURCE_OWNER_HOME_MISMATCH')
    if index.source_owner_ready(owner):
        return
    canonical_owner = {'provider': identity.provider, 'kind': identity.owner_kind, 'id': resolved.owner_id}
    route_owner = dict(zip(('provider', 'kind', 'id'), owner))
    path = resolved.scheduler_store_json
    try:
        path.lstat()
    except FileNotFoundError:
        index.settle_source_owner(route_owner, {'jobs': {}})
        return
    with _read_lock(resolved.home_dir, ('.asset-batch.lock',)):
        if regular_file_exists_beneath(resolved.home_dir, (_BATCH_FILE,)):
            raise RuntimeError('EVENT_SOURCE_ASSET_RECOVERY_REQUIRED')
        with locked_json_path(path):
            content = _read_store_bytes(path)
            payload, error = _parse_json_object_bytes(path, content) if content is not None else ({}, None)
            store = validate_scheduler_store(payload, error, canonical_owner)
            for raw in store['jobs'].values():
                job, error = parse_scheduler_job(raw, canonical_owner)
                if job is None:
                    raise RuntimeError('EVENT_SOURCE_STATE_INVALID')
            index.settle_source_owner(route_owner, store)
