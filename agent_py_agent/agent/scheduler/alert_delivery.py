# LLM: 多目标仍由原wake执行并调用唯一owner sender；回执在原job outbox分目标保存，不另建调度队列或执行身份。
# 模块用途: 展开已验证通知目标并只重投欠账路线，已外发路线仅补canonical，不重复发送已完成路线。
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import replace

from .delivery_policy import (
    ScheduleDeliveryTargetUnauthorized,
    available_delivery_targets,
    normalize_delivery,
    validate_delivery_targets,
)


# LLM: 原job或wake账本不可写是全局故障，不能当单目标网络/历史故障吞掉后继续外发。
# 类用途: 区分共享outbox失效与可隔离的目标错误。
class AlertOutboxUnavailable(OSError):
    pass


# LLM: 目标目录来自同owner真实认证绑定；origin走原冻结路由和显式继任链，local仅指向原执行线程，不接任意地址。
# 函数用途: 生成稳定binding键、实际投递上下文和本地标记，并把include_origin与相同绑定去重。
def alert_delivery_targets(agent, request, context, run):
    from ..conversation.channels import binding_delivery_target
    from .execution import resolve_delivery

    policy = validate_delivery_targets(agent, run.get("delivery"))
    available = {row["binding_id"]: row for row in available_delivery_targets(agent)}
    resolved = {}
    specs = list(policy.get("targets", []))
    if policy.get("include_origin"):
        specs.append({"target_kind": "origin"})
    for spec in specs:
        if spec["target_kind"] == "local":
            thread = agent.conversation_store.threads.require(run["thread_id"])
            if thread.owner_id != agent.home_paths.owner_id or thread.owner_home != str(agent.home_paths.owner_home_dir):
                raise ScheduleDeliveryTargetUnauthorized()
            key, channel, target, local = "local", "internal", thread.thread_id, True
        else:
            if spec["target_kind"] == "origin":
                seed = agent.conversation_store.threads.require(run["thread_id"])
                thread, origin_channel, origin_target = resolve_delivery(agent, seed, request, context.channel, context.target)
                matches = [row for row in available.values() if row["thread_id"] == thread.thread_id
                    and row["channel"] == origin_channel and binding_delivery_target(row["channel"], row["account_id"],
                        row["chat_id"], group=agent.home_paths.owner_kind == "group") == origin_target]
                if len(matches) != 1:
                    raise ScheduleDeliveryTargetUnauthorized()
                row = matches[0]
            else:
                row = available[spec["binding_id"]]
                thread = agent.conversation_store.threads.require(row["thread_id"])
            key, local = row["binding_id"], row["local"]
            channel = "internal" if local else row["channel"]
            target = thread.thread_id if local else binding_delivery_target(row["channel"], row["account_id"],
                row["chat_id"], group=agent.home_paths.owner_kind == "group")
        resolved[key] = (replace(context, thread_id=thread.thread_id, channel=channel, target=target,
            task_id=request.task_id, mode="proactive", reply_to="", progress_handle="",
            conversation_id="" if spec["target_kind"] == "local" else row["chat_id"],
            idempotency_key=f"{context.idempotency_key or request.task_id}:{key}"), local)
    return resolved


# LLM: admitted仅接受原outbox已经保存的准确sent路线；只能补历史，不授权新的外发或换目标，普通发送逐次复核认证。
# 函数用途: 为原后台sender核验本次多目标上下文，绑定注销后拒绝新发送。
def validate_alert_delivery(agent, request, context, run, *, admitted_completion=False):
    if not _canonical_owner_matches(agent, request, context):
        return False
    if admitted_completion:
        rows = agent.scheduler_repository.delivery_receipts(run["run_id"])
        if any(row.get("state") in {"sent", "committed"} and row.get("external_sent") is True
                and row.get("context") == _context_identity(context) for row in rows.values()):
            return True
    try:
        return any(plan["context"] is not None and _context_identity(plan["context"]) == _context_identity(context)
            for plan in _alert_delivery_plans(agent, request, context, run, {}).values())
    except (ValueError, KeyError, OSError):
        return False


# LLM: 已发送回执只证明旧运输，不授予当前异owner线程的写入权；缺失或不精确身份均拒绝。
# 函数用途: 核对canonical所属owner/home及原运行任务。
def _canonical_owner_matches(agent, request, context):
    thread = agent.conversation_store.threads.load(context.thread_id)
    return bool(thread and context.task_id == request.task_id and thread.owner_id == agent.home_paths.owner_id
        and thread.owner_home == str(agent.home_paths.owner_home_dir))


# LLM: 身份只含原运输和canonical目标，不能把本片随机回合号当目标或从正文重建。
# 函数用途: 投影可持久校验的准确路线。
def _context_identity(context):
    return {key: getattr(context, key) for key in ("channel", "target", "thread_id", "task_id")}


# LLM: 已发送路线先按可信原回执补账，未发送路线逐一复核当前绑定；某一路撤权不扩大其它路线权限。
# 函数用途: 建立独立目标计划并把同一绑定的origin别名去重。
def _alert_delivery_plans(agent, request, context, run, receipts):
    policy = normalize_delivery(run["delivery"])
    specs = list(policy["targets"])
    if policy.get("include_origin"):
        specs.append({"target_kind": "origin"})
    plans = {}
    for spec in specs:
        token = spec.get("binding_id") or spec["target_kind"]
        prior_key = next((key for key, row in receipts.items() if token in row.get("specs", [])), token)
        previous = receipts.get(prior_key, {})
        key, target, local, error = prior_key, None, bool(previous.get("local")), ""
        try:
            if previous.get("state") == "committed" or previous.get("external_sent") is True:
                target = replace(context, **previous["context"])
                if previous.get("state") != "committed" and not _canonical_owner_matches(agent, request, target):
                    raise ScheduleDeliveryTargetUnauthorized()
            else:
                one_run = {**run, "delivery": {"mode": "bound", "targets": [spec]}}
                [(key, (target, local))] = alert_delivery_targets(agent, request, context, one_run).items()
        except (ValueError, KeyError):
            target, error = None, "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED"
        if key in plans:
            plans[key]["specs"].append(token)
        else:
            plans[key] = {"context": target, "local": local, "specs": [token], "error_code": error}
    return plans


# LLM: 原业务结果与目标级投影分开；所有新目标都从此不可变结果生成自己的正文/元数据，不借其它目标的freeze。
# 函数用途: 恢复同run原结果或在首次投递前建立完整业务快照。
def _business_result(content, options, receipts):
    saved = next((row["business_result"] for row in receipts.values() if row.get("business_result")), None)
    if saved is not None:
        return deepcopy(saved)
    return {"content": content, "delivery_artifacts": list(options.get("delivery_artifacts", ())),
        "assistant_commentaries": list(options.get("assistant_commentaries", ())),
        **{key: deepcopy(options.get(key)) for key in ("operation_verification", "public_receipt", "display_snapshot")}}


# LLM: 所有冻结都保存到原wake；失败必须显式上抛全局账本错误，不能继续其它运输。
# 函数用途: 在网络或canonical之前保留同一任务的完整重投载荷。
def _cache_alert_delivery(delivery, request, frozen):
    wake_id = (request.wake_signal or {}).get("wake_signal_id")
    if not wake_id:
        return False
    try:
        return delivery.store.wakes.cache_delivery(wake_id, frozen) is not None
    except OSError as exc:
        raise AlertOutboxUnavailable(str(exc)) from exc


# LLM: 调用方已排除委派内部结果；每目标复用原sender，外发后先记原job回执，再落canonical；无配置不扩展义务。
# 函数用途: 完成多线告警或冻结欠账，已成功的目标在后续重投中直接跳过。
def record_alert_delivery(delivery, request, context, content, *, record_one, options):
    from ..conversation.background_delivery import (
        _background_evidence_refs_for,
        _background_owner_message_metadata,
        frozen_owner_delivery,
        project_user_reply,
    )
    from ..conversation.models import BackgroundDeliveryCommit
    from .execution import execution_run

    run = execution_run(delivery.agent, request)
    if run is None or normalize_delivery(run.get("delivery"))["mode"] not in {"bound", "multi"}:
        return None
    if not options.get("deliver") and not options.get("local_transcript_only"):
        return None
    repository = delivery.agent.scheduler_repository
    receipts = repository.delivery_receipts(run["run_id"])
    targets = _alert_delivery_plans(delivery.agent, request, context, run, receipts)
    business = _business_result(content, options, receipts)
    if "message_metadata" not in business:
        projection = project_user_reply(business["content"])
        business["content"] = projection.content
        business["evidence_refs"] = list(_background_evidence_refs_for(delivery, request, None))
        business["message_metadata"] = _background_owner_message_metadata(request,
            delivery_reason=options["delivery_reason"], projection_status=projection.projection_status,
            delivery_artifacts=tuple(business["delivery_artifacts"]), evidence_refs=tuple(business["evidence_refs"]),
            operation_verification=business["operation_verification"], public_receipt=business["public_receipt"], frozen_retry=None)
    seed = {"schema_version": "wake-owner-delivery.v2", "task_id": request.task_id, "reason": request.reason,
        "created_at": time.time(), **deepcopy(business), "external_sent": False, "receipt_id": "", "ownership": ""}
    cached = _cache_alert_delivery(delivery, request, seed)
    commits = []
    for key, plan in targets.items():
        target, local = plan["context"], plan["local"]
        previous = receipts.get(key, {})
        if previous.get("state") == "committed":
            continue
        # 先存原结果和目标角色；网络在此之后，任何目标失败也不需重新调用模型。
        saved = {**previous, "state": previous.get("state", "pending"), "specs": plan["specs"], "local": local,
            "business_result": deepcopy(business), "error_code": plan["error_code"]}
        repository.record_delivery_receipt(run["run_id"], key, saved)
        if target is None:
            continue
        retry = frozen_owner_delivery(previous["frozen"]) if previous.get("frozen") else (
            frozen_owner_delivery(seed) if options.get("frozen_retry") is not None else None)
        if retry is not None and not previous.get("external_sent"):
            retry = replace(retry, external_sent=False, receipt_id="")
        # LLM: 闭包保存当前目标实际状态；只有sent才记external_sent，本地失败也冻原wake，不持锁跨网络或改执行身份。
        # 函数用途: 在canonical前保存目标回执；首条本地路线失败时同样保留整封结果供重投。
        def observe(freeze, *, key=key, target=target, saved=saved):
            sent = freeze.delivery_status == "sent"
            frozen = {"schema_version": "wake-owner-delivery.v2", "task_id": request.task_id, "reason": request.reason,
                "created_at": time.time(), "content": freeze.content, "delivery_artifacts": list(freeze.delivery_artifacts),
                "assistant_commentaries": list(freeze.assistant_commentaries), "evidence_refs": list(freeze.evidence_refs),
                "message_metadata": freeze.message_metadata, "external_sent": sent,
                "receipt_id": freeze.receipt_id, "ownership": freeze.ownership}
            try:
                repository.record_delivery_receipt(run["run_id"], key, {**saved, "state": "sent" if sent else "pending",
                    "delivery_status": freeze.delivery_status, "external_sent": sent, "receipt_id": freeze.receipt_id,
                    "context": _context_identity(target), "frozen": frozen})
            except OSError as exc:
                raise AlertOutboxUnavailable(str(exc)) from exc
            _cache_alert_delivery(delivery, request, frozen)
        kwargs = {**options, **{name: business[name] for name in ("operation_verification", "public_receipt", "display_snapshot")},
            "delivery_artifacts": tuple(business["delivery_artifacts"]), "assistant_commentaries": tuple(business["assistant_commentaries"]),
            "frozen_retry": retry, "local_transcript_only": local or options.get("local_transcript_only", False),
            "route_supports_transcript": True if local else None,
            "delivery_observer": observe, "delivery_target_key": key}
        if retry is not None:
            kwargs.update(delivery_artifacts=retry.delivery_artifacts, assistant_commentaries=retry.assistant_commentaries)
        try:
            commit = record_one(delivery, request, target, retry.content if retry is not None else business["content"], **kwargs)
        except AlertOutboxUnavailable:
            raise
        except OSError:
            saved = repository.delivery_receipts(run["run_id"]).get(key, {})
            repository.record_delivery_receipt(run["run_id"], key, {**saved, "error_code": "TARGET_DELIVERY_IO_ERROR"})
            continue
        commits.append(commit)
        saved = repository.delivery_receipts(run["run_id"]).get(key, {})
        repository.record_delivery_receipt(run["run_id"], key, {**saved,
            "state": "committed" if commit.persisted and (commit.delivery_status == "sent" or local and commit.delivery_status == "local_only")
                else "sent" if saved.get("external_sent") else "pending",
            "delivery_status": commit.delivery_status, "context": _context_identity(target), "message_id": commit.message_id})
    receipts = repository.delivery_receipts(run["run_id"])
    complete = bool(targets) and all(receipts.get(key, {}).get("state") == "committed" for key in targets)
    return BackgroundDeliveryCommit(content=business["content"], delivery_status=("sent" if any(row.get("delivery_status") == "sent" for row in receipts.values())
        else "local_only") if complete else "failed", persisted=complete,
        message_id=next((row.message_id for row in reversed(commits) if row.message_id), ""),
        commit_kind=("external_delivery" if any(row.get("delivery_status") == "sent" for row in receipts.values())
            else "canonical_transcript") if complete else "outbox_pending", transcript_route=True,
        outbox_frozen=cached or any(row.outbox_frozen for row in commits))
