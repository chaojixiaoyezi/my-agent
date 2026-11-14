"""工作流步骤水位与跨运行持久化游标 (WF-10)。

支持步骤成功后的原子水位提交、跨运行游标恢复与写后超时对账。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..assets import safe_asset_id
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    read_text_beneath,
    write_text_atomic_beneath,
)
from .models import payload

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
    import hashlib
    digest = hashlib.sha256(scope_key.encode()).hexdigest()
    return ('data', 'workflow_watermarks', workflow_id, step_id, digest[:2], f'{digest}.json')


def load_workflow_step_watermark(
    owner_home: str | Path,
    workflow_id: str,
    step_id: str,
    *, scope_key: str = '',
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
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
        raw = read_text_beneath(owner_home, relative_parts)
        if raw is not None:
            try:
                existing = json.loads(raw)
                if isinstance(existing, dict):
                    if scope_key and existing.get('operation_id') == operation_id and existing.get('watermark') == payload(watermark):
                        return existing
                    prev_rev = int(existing.get('revision', 0))
            except Exception:
                prev_rev = 0

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

    if not candidate or not isinstance(candidate, dict):
        return None

    wm_payload = dict(candidate)

    # 1. 持久化至当前 Run 记录（并行 batch worker 在内存记录 watermark，由协调线程原子写盘）
    if (
        engine is not None
        and getattr(engine, 'store', None) is not None
        and run is not None
        and not getattr(run, '_batch_worker', False)
    ):
        try:
            updated = engine.store.save_step_watermark(run.workflow_run_id, step.step_id, wm_payload)
            step.watermark = updated.watermark
        except Exception:
            step.watermark = wm_payload
    else:
        step.watermark = wm_payload

    # 2. 持久化跨运行 durable (workflow_id, step_id) 检查点
    owner_home = getattr(engine, 'owner_home', None) or (getattr(run, 'owner_home', None) if run else None)
    workflow_id = getattr(run, 'workflow_id', '') if run else ''
    run_id = getattr(run, 'workflow_run_id', '') if run else ''
    step_attempt_id = ''
    if isinstance(getattr(step, 'escalation_result', None), dict):
        step_attempt_id = step.escalation_result.get('attempt_id', '')
    effect_id = getattr(step, 'effect_id', '')
    if not effect_id and isinstance(getattr(step, 'escalation_result', None), dict):
        effect_id = step.escalation_result.get('effect_id', '')

    if owner_home and workflow_id:
        business_effect = step.escalation_result.get('business_effect', {})
        return save_workflow_step_watermark(
            owner_home,
            workflow_id,
            step.step_id,
            wm_payload,
            run_id=run_id,
            step_attempt_id=step_attempt_id,
            effect_id=effect_id,
            scope_key=business_effect.get('scope_key', ''),
            operation_id=business_effect.get('operation_id', ''),
        )
    return None
