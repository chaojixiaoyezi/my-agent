"""工作流步骤水位与跨运行持久化游标 (WF-10)。

支持步骤成功后的原子水位提交、跨运行游标恢复与写后超时对账。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from ..assets import safe_asset_id
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    inspect_file_snapshot_beneath,
    read_text_beneath,
    write_text_atomic_beneath,
)
from .models import payload

_LOG = logging.getLogger(__name__)

_ALLOWED_WATERMARK_STATUSES: tuple[str, ...] = (
    'succeeded',
    'escalated_recovered',
)


def _get_spec_val(spec: Any, key: str, default: Any = None) -> Any:
    if isinstance(spec, dict):
        return spec.get(key, default)
    return getattr(spec, key, default)


def _watermark_parts(workflow_id, step_id, scope_key):
    if not scope_key:
        return ('data', 'workflow_watermarks', workflow_id, f'{step_id}.json')
    digest = hashlib.sha256(scope_key.encode()).hexdigest()
    return ('data', 'workflow_watermarks', workflow_id, step_id, digest[:2], f'{digest}.json')


def load_workflow_step_watermark(
    owner_home: str | Path,
    workflow_id: str,
    step_id: str,
    *, scope_key: str = '', strict: bool = False,
) -> dict[str, Any] | None:
    """加载特定工作流特定步骤的持久化水位检查点 (WF-10)。

    路径: <owner_home>/data/workflow_watermarks/<workflow_id>/<step_id>.json
    O(1) 直接读取，无全局遍历。未找到返回 None。
    """
    owner_home = str(owner_home)
    safe_asset_id(str(workflow_id))
    safe_asset_id(str(step_id))
    if '..' in str(workflow_id) or '..' in str(step_id):
        raise ValueError('ID不能包含点穿越')
    relative_parts = _watermark_parts(workflow_id, step_id, scope_key)
    raw = read_text_beneath(owner_home, relative_parts)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        if strict:
            raise ValueError('invalid watermark checkpoint')
    except ValueError:
        if strict:
            raise
    return None


class _WatermarkConflict(ValueError):
    """Another checkpoint is present or the predecessor cannot be verified."""

    def __init__(self):
        super().__init__('WORKFLOW_RUN_REVISION_CONFLICT')


def _predecessor_fingerprint(owner_home, parts):
    try:
        snapshot = inspect_file_snapshot_beneath(owner_home, parts)
    except FileNotFoundError:
        snapshot = None
    if snapshot is None:
        return 'absent'
    stat = snapshot.stat
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                snapshot.changed_ns, snapshot.parents)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def save_workflow_step_watermark(
    owner_home: str | Path,
    workflow_id: str,
    step_id: str,
    watermark: dict[str, Any],
    *,
    run_id: str = '',
    step_attempt_id: str = '',
    effect_id: str = '',
    scope_key: str = '',
    operation_id: str = '',
    expected_revision: int | None = None,
    expected_predecessor: str | None = None,
    attempt_started_at: float = 0,
) -> dict[str, Any]:
    """保存特定工作流特定步骤的持久化水位检查点 (WF-10)。

    使用 per-workflow/step JSON 锁与原子写盘保证单调 revision。
    """
    owner_home = str(owner_home)
    safe_asset_id(str(workflow_id))
    safe_asset_id(str(step_id))
    if '..' in str(workflow_id) or '..' in str(step_id):
        raise ValueError('ID不能包含点穿越')
    if not isinstance(watermark, dict):
        raise TypeError('watermark 必须是字典')

    relative_parts = _watermark_parts(workflow_id, step_id, scope_key)
    target_path = Path(owner_home).joinpath(*relative_parts)
    ensure_private_dir(target_path.parent, canonical_root=owner_home)

    with locked_json_path(target_path):
        prev_rev = 0
        guarded = expected_revision is not None or expected_predecessor is not None
        raw = read_text_beneath(owner_home, relative_parts)
        if raw is not None:
            try:
                existing = json.loads(raw)
                if not isinstance(existing, dict):
                    raise ValueError('invalid watermark checkpoint')
                same_attempt = (bool(run_id) and existing.get('last_run_id') == run_id
                                and existing.get('attempt_id') == step_attempt_id)
                same_operation = bool(scope_key and operation_id) and existing.get('operation_id') == operation_id
                if (same_attempt or same_operation) and existing.get('watermark') == payload(watermark):
                    return existing
                prev_rev = int(existing.get('revision', 0))
            except (ValueError, TypeError):
                if guarded:
                    raise
                existing = {}
            published_attempt = existing.get('attempt_started_at', 0)
            if (attempt_started_at and type(published_attempt) in (int, float)
                    and published_attempt >= attempt_started_at):
                raise _WatermarkConflict()
        if expected_predecessor is not None and (
            not expected_predecessor or _predecessor_fingerprint(owner_home, relative_parts) != expected_predecessor
        ):
            raise _WatermarkConflict()
        if expected_revision is not None and prev_rev != expected_revision:
            raise _WatermarkConflict()

        checkpoint: dict[str, Any] = {
            'schema_version': 'workflow-step-watermark.v1',
            'workflow_id': workflow_id,
            'step_id': step_id,
            'last_run_id': run_id,
            'attempt_id': step_attempt_id,
            'effect_id': effect_id,
            'watermark': payload(watermark),
            'updated_at': time.time(),
            'revision': prev_rev + 1,
        }
        if attempt_started_at > 0 and math.isfinite(attempt_started_at):
            checkpoint['attempt_started_at'] = attempt_started_at
        if scope_key:
            checkpoint.update(scope_key=scope_key, operation_id=operation_id)
        write_text_atomic_beneath(
            owner_home,
            relative_parts,
            json.dumps(checkpoint, ensure_ascii=False, indent=2),
        )
        return checkpoint


def reconcile_step_watermark(
    owner_home: str | Path,
    workflow_id: str,
    step_id: str,
    effect_id: str,
) -> dict[str, Any] | None:
    """写后超时对账 (WF-10)。

    若此前尝试已将本次 effect_id / idempotency_key 提交至水位，直接返回检查点而无需重放写操作。
    """
    if not effect_id:
        return None
    checkpoint = load_workflow_step_watermark(owner_home, workflow_id, step_id)
    if checkpoint is None or not isinstance(checkpoint, dict):
        return None

    checkpoint_effect_id = checkpoint.get('effect_id', '')
    watermark_dict = checkpoint.get('watermark')
    wm_effect_id = ''
    if isinstance(watermark_dict, dict):
        wm_effect_id = watermark_dict.get('effect_id') or watermark_dict.get('idempotency_key', '')

    if effect_id in (checkpoint_effect_id, wm_effect_id):
        return checkpoint
    return None


def commit_verified_watermark(
    *,
    engine: Any,
    run: Any,
    step: Any,
    spec: Any,
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """在步骤完成并验证输出交付后提交水位 (WF-10)。

    仅在 step.status in ('succeeded', 'escalated_recovered') 时推进水位。
    未提供或无有效水位时干净返回 None，不产生多余磁盘写入。
    """
    if step.status not in _ALLOWED_WATERMARK_STATUSES:
        return None

    candidate: dict[str, Any] | None = None
    if isinstance(getattr(step, 'watermark', None), dict) and step.watermark:
        candidate = step.watermark
    elif isinstance(getattr(step, 'output_snapshot', None), dict) and isinstance(step.output_snapshot.get('watermark'), dict):
        candidate = step.output_snapshot['watermark']
    else:
        cfg = _get_spec_val(spec, 'config')
        if not isinstance(cfg, dict):
            cfg = {}
        watermark_key = cfg.get('watermark_key') or _get_spec_val(spec, 'watermark_key')
        if watermark_key and isinstance(getattr(step, 'output_snapshot', None), dict) and watermark_key in step.output_snapshot:
            candidate = {'cursor': step.output_snapshot[watermark_key], 'key': watermark_key}


    publication = step.escalation_result.get('watermark_publication')
    if publication and publication['state'] == 'published':
        return None
    if publication:
        candidate = publication['value']

    if not isinstance(candidate, dict) or not candidate:
        return None

    owner_home = getattr(engine, 'owner_home', None) or getattr(run, 'owner_home', None)
    workflow_id = getattr(run, 'workflow_id', '')
    run_id = getattr(run, 'workflow_run_id', '')
    store = getattr(engine, 'store', None) if run_id and not getattr(run, 'ephemeral_debug', False) else None
    facts = step.escalation_result
    attempt_id = facts.get('attempt_id', '')
    effect_id = getattr(step, 'effect_id', '') or facts.get('effect_id', '')
    business_effect = facts.get('business_effect', {})
    scope_key = business_effect.get('scope_key', '')
    operation_id = business_effect.get('operation_id', '')
    step.watermark = dict(candidate)
    if not owner_home or not workflow_id:
        return None

    observation_error = None
    if not publication and store is not None:
        # Freeze the exact leaf before reading the contents. A later successful read
        # may supply its revision only while that same predecessor still occupies the slot.
        parts = _watermark_parts(safe_asset_id(workflow_id), safe_asset_id(step.step_id), scope_key)
        facts['watermark_predecessor'] = ''
        try:
            facts['watermark_predecessor'] = _predecessor_fingerprint(owner_home, parts)
            base = load_workflow_step_watermark(owner_home, workflow_id, step.step_id, scope_key=scope_key, strict=True)
            facts['watermark_base_revision'] = int((base or {}).get('revision', 0))
        except (OSError, TypeError, ValueError) as error:
            facts['watermark_base_revision'] = None
            observation_error = error
        facts['watermark_publication'] = {'state': 'pending', 'value': step.watermark}
        run.revision = store.save_step_result(run_id, step).revision

    conflict = False
    try:
        if observation_error is not None:
            raise observation_error
        checkpoint = save_workflow_step_watermark(
            owner_home, workflow_id, step.step_id, step.watermark, run_id=run_id,
            step_attempt_id=attempt_id, effect_id=effect_id, scope_key=scope_key, operation_id=operation_id,
            expected_revision=facts.get('watermark_base_revision'),
            expected_predecessor=(facts.get('watermark_predecessor', '')
                                  if store is not None and facts.get('watermark_base_revision') is None
                                  else facts.get('watermark_predecessor')),
            attempt_started_at=getattr(step, 'started_at', 0),
        )
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        if store is None:
            raise
        _LOG.warning('workflow watermark publication needs repair', extra={
            'error_code': 'WORKFLOW_EXECUTION_FAILED', 'workflow_run_id': run_id,
            'step_id': step.step_id, 'error_type': type(error).__name__, 'phase': 'watermark_publication',
        })
        state = 'repair'
        conflict = isinstance(error, _WatermarkConflict)
        checkpoint = None
    else:
        state = 'published'
    if store is not None:
        changes = {'state': state, 'value': step.watermark}
        if conflict:
            changes['error_code'] = 'WORKFLOW_RUN_REVISION_CONFLICT'
        try:
            updated = store.save_step_watermark(run_id, step.step_id, step.watermark,
                                                publication=changes, expected_attempt_id=attempt_id)
        except OSError as error:
            # The step leaf may already contain the ACK. Its durable attempt and
            # publication state take precedence over the local pending snapshot.
            _LOG.warning('workflow watermark acknowledgement needs repair', extra={
                'workflow_run_id': run_id, 'step_id': step.step_id,
                'error_type': type(error).__name__, 'phase': 'watermark_ack',
            })
            try:
                current = store.load_run(run_id)
            except OSError:
                return checkpoint
            updated = next((item for item in current.steps if item.step_id == step.step_id
                            and item.escalation_result.get('attempt_id', '') == attempt_id), None)
        if updated is not None:
            step.escalation_result = updated.escalation_result
    return checkpoint


def repair_pending_watermarks(engine, run):
    """恢复已有成功步骤的水位发表，不调用步骤执行器。"""
    pending = [step for step in run.steps if step.escalation_result.get(
        'watermark_publication', {}).get('state') in ('pending', 'repair')]
    for step in pending:
        commit_verified_watermark(engine=engine, run=run, step=step, spec={})
    return engine.store.load_run(run.workflow_run_id) if pending else run
