# LLM: 工作流通知步骤执行与无人值守失败告警 (WF-05)。
# 模块用途: 提供 notification 节点正式执行器、授权目标校验、安全模板渲染、敏感信息脱敏与基于日期分桶的持久化去重。
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.directory_lock import locked_private_directory
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_.$]+)\}")


def _notification_receipt_path(run, dedupe_key):
    # 原运行时间是稳定分桶事实；午夜后的重复事件不能换桶绕过原通知收据。
    date_str = datetime.fromtimestamp(run.started_at, timezone.utc).strftime('%Y-%m-%d')
    key_hash = hashlib.sha256(dedupe_key.encode('utf-8')).hexdigest()[:24]
    return ('data', 'workflow_notifications', date_str, f'{key_hash}.json')


def _claim_notification(owner_home, rel_path, dedupe_key, run):
    """原收据内先原子认领再锁外投递；崩溃遗留认领不能当作未发送而重放。"""
    stage = 'claim_read'
    try:
        with locked_private_directory(owner_home, relative_parts=rel_path[:-1], lock_name=rel_path[-1] + '.lock'):
            candidates = [rel_path]
            # 只按原失败时间与当前时间核对旧日期桶，不扫历史目录，也不改写旧收据。
            for timestamp in (getattr(run, 'ended_at', 0), datetime.now(timezone.utc).timestamp()):
                if timestamp:
                    day = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')
                    candidate = (*rel_path[:-2], day, rel_path[-1])
                    if candidate not in candidates:
                        candidates.append(candidate)
            for candidate in candidates:
                raw = read_text_beneath(owner_home, candidate)
                if not raw:
                    continue
                existing = json.loads(raw)
                if not isinstance(existing, dict) or existing.get('dedupe_key') != dedupe_key:
                    raise ValueError('notification receipt identity mismatch')
                return '', existing
            claim_id = uuid4().hex
            pending = {'dedupe_key': dedupe_key, 'claim_id': claim_id, 'status': 'dispatching',
                'effect_outcome': 'unknown', 'retry_same_request': False,
                'next_action': '原通知正在投递或投递结果尚未核实；读取此去重键的原收据，确认前禁止再次发送。'}
            stage = 'claim_write'
            write_text_atomic_beneath(owner_home, rel_path, json.dumps(pending, ensure_ascii=False))
            return claim_id, None
    except Exception as error:
        return '', {'dedupe_key': dedupe_key, 'status': 'notification_failed',
            'error_code': 'WORKFLOW_NOTIFICATION_RECEIPT_STORE_UNAVAILABLE',
            'exception_type': type(error).__name__, 'stage': stage,
            'effect_outcome': 'not_started' if stage == 'claim_write' else 'unknown',
            'retry_same_request': False,
            'next_action': '本次未调用发送端；原去重状态尚需核实。先核对收据和修复通知存储，再决定是否重新触发，不能更换去重键绕过。'}


def _settle_notification(owner_home, rel_path, claim_id, receipt):
    # 锁只护短暂收据 CAS，发送、入队与外部 I/O 始终在锁外；原认领丢失不能覆盖别人的状态。
    try:
        with locked_private_directory(owner_home, relative_parts=rel_path[:-1], lock_name=rel_path[-1] + '.lock'):
            current = json.loads(read_text_beneath(owner_home, rel_path))
            if current.get('claim_id') != claim_id or current.get('status') != 'dispatching':
                raise ValueError('notification claim changed')
            write_text_atomic_beneath(owner_home, rel_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    except Exception as error:
        receipt['receipt_persistence_error'] = {'code': 'WORKFLOW_NOTIFICATION_RECEIPT_STORE_UNAVAILABLE',
            'error_type': type(error).__name__,
            'recommended_action': 'manual_review',
            'recovery_hint': '保留原投递事实并核对去重收据；认领未确认前禁止重新发送。'}


def _safe_render_template(template_str: str, lookup: dict[str, Any]) -> str:
    """Safe placeholder replacement without str.format attribute traversal."""
    def _replace(match: re.Match) -> str:
        var_name = match.group(1).strip()
        if var_name in lookup:
            val = lookup[var_name]
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)
        parts = var_name.split(".")
        curr: Any = lookup
        for part in parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                return match.group(0)
        if isinstance(curr, (dict, list)):
            return json.dumps(curr, ensure_ascii=False)
        return str(curr)

    return _PLACEHOLDER_RE.sub(_replace, template_str)


def _is_target_authorized(channel: str, target: str, owner_id: str, notif_spec: dict[str, Any]) -> bool:
    """Validate target authorization, blocking cross-owner and unauthorized URLs."""
    channel_clean = channel.lower().strip()
    target_clean = target.strip()
    owner_clean = owner_id.strip()

    if not target_clean:
        return True

    # Check for cross-owner targets
    if target_clean.startswith(("owner:", "user:")):
        target_owner = target_clean.split(":", 1)[1].strip()
        if owner_clean and target_owner != owner_clean:
            return False

    # Check URL targets: only allowed for webhook_authorized with explicit authorization
    if target_clean.startswith(("http://", "https://")):
        if channel_clean != "webhook_authorized":
            return False
        authorized_urls = notif_spec.get("authorized_urls") or notif_spec.get("authorized_targets") or ()
        if isinstance(authorized_urls, str):
            authorized_urls = (authorized_urls,)
        if not (notif_spec.get("authorized_url") is True or target_clean in authorized_urls):
            return False

    return True


def execute_notification_step(
    *,
    engine: Any,
    run: Any,
    workflow: Any,
    step: Any,
    spec: Any,
    context: Any,
    outputs: dict[str, Any],
) -> None:
    """Execute a formal workflow notification step (WF-05)."""
    notif = (
        (spec.get("notification_spec") if isinstance(spec, dict) else getattr(spec, "notification_spec", None))
        or (spec.get("config") if isinstance(spec, dict) else getattr(spec, "config", None))
        or {}
    )
    if not isinstance(notif, dict):
        notif = {}

    channel = str(notif.get("channel") or "").strip().lower()
    raw_template = str(notif.get("template") or notif.get("message") or "").strip()
    target = str(notif.get("target") or "").strip()
    owner_id = str(getattr(run, "owner_id", "") or "current_owner").strip()
    if not target:
        target = owner_id

    # Authorization and spec check
    if not channel or not raw_template:
        step.status = "failed"
        step.error_code = "WORKFLOW_NOTIFICATION_SPEC_INVALID"
        step.error_message = "notification step requires non-empty channel and template/message"
        step.needs_repair = True
        return

    if not _is_target_authorized(channel, target, owner_id, notif):
        step.status = "failed"
        step.error_code = "WORKFLOW_NOTIFICATION_TARGET_FORBIDDEN"
        step.error_message = f"notification target '{target}' forbidden or unauthorized for owner '{owner_id}'"
        step.needs_repair = True
        return

    title = str(
        notif.get("title")
        or f"Workflow {getattr(run, 'workflow_name', '') or getattr(run, 'workflow_id', '')} - {getattr(step, 'name', '') or getattr(step, 'step_id', '')}"
    ).strip()

    # Safe templating & secret redaction
    lookup: dict[str, Any] = {}
    run_inputs = getattr(run, "inputs", None)
    if isinstance(run_inputs, dict):
        lookup.update(run_inputs)
        lookup.update({f"inputs.{k}": v for k, v in run_inputs.items()})
        lookup.update({f"$inputs.{k}": v for k, v in run_inputs.items()})

    input_snapshot = getattr(step, "input_snapshot", None)
    if isinstance(input_snapshot, dict):
        lookup.update(input_snapshot)

    if isinstance(outputs, dict):
        lookup.update({f"outputs.{k}": v for k, v in outputs.items()})
        for k, v in outputs.items():
            if isinstance(v, dict):
                for subk, subv in v.items():
                    lookup[f"{k}.{subk}"] = subv

    lookup.update({
        "run_id": getattr(run, "workflow_run_id", ""),
        "workflow_run_id": getattr(run, "workflow_run_id", ""),
        "step_id": getattr(step, "step_id", ""),
        "workflow_id": getattr(run, "workflow_id", ""),
        "workflow_name": getattr(run, "workflow_name", ""),
        "owner_id": owner_id,
        "target": target,
        "channel": channel,
    })

    rendered_message = _safe_render_template(raw_template, lookup)
    clean_message = str(redact_sensitive_value(rendered_message))
    clean_title = str(redact_sensitive_value(title))

    # Durable deduplication
    attempt_id = getattr(step, "attempt_count", None) or getattr(step, "escalation_result", {}).get("attempt_id") or 1
    if notif.get("dedupe_scope") == "step":
        dedupe_key = f"wf-step-notify:{run.workflow_run_id}:{step.step_id}"
    else:
        dedupe_key = f"wf-step-notify:{run.workflow_run_id}:{step.step_id}:{attempt_id}"

    owner_home = Path(getattr(engine, "owner_home", None) or getattr(run, "owner_home", None) or Path.cwd()).absolute()
    rel_path = _notification_receipt_path(run, dedupe_key)

    # 原步骤输出也是框架恢复依据；本地辅助收据失败不能抹掉已经入队的事实。
    previous = getattr(step, "output_snapshot", None)
    if isinstance(previous, dict) and previous.get('effect_outcome') == 'unknown':
        step.status, step.needs_repair = 'failed', True
        step.error_code = 'WORKFLOW_NOTIFICATION_DELIVERY_FAILED'
        step.error_message = '原通知投递结果未知；先核对原去重收据，不能通过增加尝试次数重新发送。'
        return
    if (isinstance(previous, dict) and previous.get("dedupe_key") == dedupe_key
            and previous.get("status") in {"delivered", "queued"}):
        step.status = "succeeded"
        step.effect_id = dedupe_key
        return
    claim_id, existing_receipt = _claim_notification(owner_home, rel_path, dedupe_key, run)
    if existing_receipt is not None:
        step.output_snapshot = existing_receipt
        if existing_receipt.get('status') in {'delivered', 'queued'}:
            step.status, step.effect_id = 'succeeded', dedupe_key
        else:
            step.status = 'failed'
            step.error_code = existing_receipt.get('error_code') or 'WORKFLOW_NOTIFICATION_DELIVERY_FAILED'
            step.error_message = existing_receipt.get('next_action') or '原通知尚待核实，禁止重复发送。'
            step.needs_repair = True
        return

    receipt = {
        "dedupe_key": dedupe_key,
        "workflow_run_id": getattr(run, "workflow_run_id", ""),
        "step_id": getattr(step, "step_id", ""),
        "attempt_id": attempt_id,
        "channel": channel,
        "target": target,
        "title": clean_title,
        "message": clean_message,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "status": "delivered",
    }

    delivery_ok = False
    delivery_error = None

    sender = getattr(engine, "notification_sender", None)
    if callable(sender):
        try:
            send_res = sender(channel=channel, target=target, title=clean_title, message=clean_message, payload=receipt)
            if isinstance(send_res, dict) and send_res.get("status") in ("failed", "error", "no_channel", "unavailable", "unknown"):
                delivery_ok = False
                delivery_error = send_res.get("error") or "SENDER_RETURNED_FAILURE"
            else:
                delivery_ok = True
                if isinstance(send_res, dict):
                    receipt["delivery_result"] = send_res
                    if send_res.get("status") == "queued":
                        receipt["status"] = "queued"
                        receipt["queued_at"] = receipt.pop("delivered_at")
        except Exception as exc:
            delivery_ok = False
            delivery_error = str(exc)
    else:
        try:
            from .datasource_runtime import emit_asset_im_notification
            im_res = emit_asset_im_notification(
                owner_home,
                event_type=f"workflow_step_notification:{channel}",
                title=clean_title,
                message=clean_message,
                payload=receipt,
            )
            if im_res.get("delivery_status") == "queued":
                # 原宿主可能已入队而本地日志失败；队列事实优先，保留原record_error供修复。
                delivery_ok = True
                receipt["status"] = "queued"
                receipt["queued_at"] = receipt.pop("delivered_at")
                receipt["delivery_result"] = im_res
            elif im_res.get("recorded") is False:
                delivery_ok = False
                delivery_error = im_res.get("record_error", "NOTIFICATION_RECORD_FAILED")
                receipt["delivery_result"] = im_res
            elif im_res.get("delivery_status") != "queued":
                # 本地记账不是发出通知；没有渠道或渠道拒绝必须保留明确失败。
                delivery_error = "WORKFLOW_NOTIFICATION_NO_CHANNEL" if im_res.get("delivery_status") == "no_channel" else "WORKFLOW_NOTIFICATION_CHANNEL_UNAVAILABLE"
                receipt["delivery_result"] = im_res
        except Exception as exc:
            delivery_ok = False
            delivery_error = str(exc)

    if not delivery_ok:
        receipt["status"] = "failed"
        receipt.pop("delivered_at", None)
        step.status = "failed"
        step.error_code = "WORKFLOW_NOTIFICATION_DELIVERY_FAILED"
        step.error_message = f"notification delivery failed: {delivery_error}"
        step.needs_repair = True
        receipt['effect_outcome'] = 'not_started' if (receipt.get('delivery_result') or {}).get('delivery_status') == 'no_channel' else 'unknown'
        _settle_notification(owner_home, rel_path, claim_id, receipt)
        step.output_snapshot = receipt
        return

    _settle_notification(owner_home, rel_path, claim_id, receipt)

    step.status = "succeeded"
    step.output_snapshot = receipt
    step.effect_id = dedupe_key


def notify_unattended_run_failure(*, engine: Any, run: Any) -> dict[str, Any]:
    """Triggered when a formal WorkflowRunRecord ends with status == 'failed' (WF-05)."""
    # Anti-recursion guards
    if getattr(run, "_suppress_failure_notification", False):
        return {"status": "skipped", "reason": "anti_recursion"}
    if getattr(run, "failure_notification", None) is not None:
        previous = run.failure_notification
        if isinstance(previous, dict) and previous.get("status") in {"notified", "queued"}:
            return previous
        return {"status": "skipped", "reason": "anti_recursion"}

    steps = getattr(run, "steps", None) or {}
    step_list = list(steps.values()) if isinstance(steps, dict) else list(steps)
    if step_list and all(getattr(s, "mode", "") in ("notification",) for s in step_list):
        return {"status": "skipped", "reason": "anti_recursion"}

    # Determine whether run is unattended
    is_unattended = (
        getattr(run, "trigger_source", "") in ("schedule", "background", "unattended", "cron", "timer")
        or (isinstance(getattr(run, "inputs", None), dict) and run.inputs.get("unattended") is True)
        or (isinstance(getattr(run, "inputs", None), dict) and run.inputs.get("trigger_source") in ("schedule", "background", "unattended"))
        or (isinstance(getattr(run, "business_context", None), dict) and run.business_context.get("unattended") is True)
        or (isinstance(getattr(run, "business_context", None), dict) and run.business_context.get("trigger_source") in ("schedule", "background", "unattended"))
        or getattr(engine, "notification_sender", None) is not None
        or bool(getattr(run, "schedule_id", None) or getattr(run, "rule_id", None))
    )

    dedupe_key = f"wf-run-fail:{run.workflow_run_id}"
    owner_home = Path(getattr(engine, "owner_home", None) or getattr(run, "owner_home", None) or Path.cwd()).absolute()
    rel_path = _notification_receipt_path(run, dedupe_key)

    claim_id, cached = _claim_notification(owner_home, rel_path, dedupe_key, run)
    if cached is not None:
        return cached

    title = f"Workflow Failure Alert: {getattr(run, 'workflow_name', '') or getattr(run, 'workflow_id', '') or run.workflow_run_id}"
    failed_step = getattr(run, "failed_step_id", "") or "unknown"
    message = f"Workflow run {run.workflow_run_id} failed at step {failed_step}."
    failed_record = (steps.get(failed_step) if isinstance(steps, dict) else
        next((item for item in step_list if getattr(item, 'step_id', None) == failed_step), None))
    error_msg = getattr(failed_record, 'error_message', '') or getattr(run, "error_message", None)
    error_code = getattr(failed_record, 'error_code', '') or getattr(run, 'error_code', '')
    if error_msg:
        message += f" Reason: {error_msg}"

    clean_title = str(redact_sensitive_value(title))
    clean_message = str(redact_sensitive_value(message))

    receipt: dict[str, Any] = {
        "dedupe_key": dedupe_key,
        "workflow_run_id": getattr(run, "workflow_run_id", ""),
        "workflow_id": getattr(run, "workflow_id", ""),
        "failed_step_id": failed_step,
        "error_code": error_code,
        "title": clean_title,
        "message": clean_message,
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "status": "notified",
        "unattended": is_unattended,
    }

    sender = getattr(engine, "notification_sender", None)
    try:
        if callable(sender):
            send_res = sender(
                channel="owner_inbox",
                target=getattr(run, "owner_id", "current_owner"),
                title=clean_title,
                message=clean_message,
                payload=receipt,
            )
            if isinstance(send_res, dict) and send_res.get("status") in ("failed", "error", "no_channel", "unavailable", "unknown"):
                raise RuntimeError(send_res.get("error") or "SENDER_RETURNED_FAILURE")
            if isinstance(send_res, dict) and send_res.get("status") == "queued":
                receipt["status"] = "queued"
                receipt["queued_at"] = receipt.pop("notified_at")
                receipt["delivery_result"] = send_res
        else:
            from .datasource_runtime import emit_asset_im_notification
            delivery = emit_asset_im_notification(
                owner_home,
                event_type="workflow_run_failure",
                title=clean_title,
                message=clean_message,
                payload=receipt,
            )
            if delivery.get("delivery_status") != "queued":
                failure = {"status": "notification_failed", "dedupe_key": dedupe_key,
                    "error_code": (delivery.get("record_error") or "NOTIFICATION_RECORD_FAILED")
                        if delivery.get("recorded") is False else
                        "WORKFLOW_NOTIFICATION_NO_CHANNEL" if delivery.get("delivery_status") == "no_channel" else "WORKFLOW_NOTIFICATION_CHANNEL_UNAVAILABLE",
                    "delivery_result": delivery}
                failure['effect_outcome'] = 'not_started' if delivery.get('delivery_status') == 'no_channel' else 'unknown'
                failure['retry_same_request'] = False
                _settle_notification(owner_home, rel_path, claim_id, failure)
                return failure
            receipt["status"] = "queued"
            receipt["queued_at"] = receipt.pop("notified_at")
            receipt["delivery_result"] = delivery
    except Exception as exc:
        failure = {'status': 'notification_failed', 'error_code': 'WORKFLOW_FAILURE_NOTIFICATION_ERROR',
            'exception_type': type(exc).__name__, 'error': redact_sensitive_value(str(exc)),
            'dedupe_key': dedupe_key, 'effect_outcome': 'unknown', 'retry_same_request': False,
            'next_action': '发送端已进入但结果无法确认；核对原通知和去重收据，禁止原样重发。'}
        _settle_notification(owner_home, rel_path, claim_id, failure)
        return failure

    _settle_notification(owner_home, rel_path, claim_id, receipt)

    run.failure_notification = receipt
    return receipt
