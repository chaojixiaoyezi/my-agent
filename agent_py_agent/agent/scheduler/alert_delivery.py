# LLM: 多目标仍由原wake执行并调用唯一owner sender；回执在原job outbox分目标保存，不另建调度队列或执行身份。
# 模块用途: 展开已验证通知目标并只重投欠账路线，已外发路线仅补canonical，不重复发送已完成路线。
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import replace

from .delivery_policy import (
    TARGET_DELIVERY_MODES,
    ScheduleDeliveryTargetUnauthorized,
    alert_topic_enabled,
    available_delivery_targets,
    delivery_specs,
    normalize_delivery,
    validate_delivery_targets,
)


# LLM: 原job或wake账本不可写是全局故障，不能当单目标网络/历史故障吞掉后继续外发。
# 类用途: 区分共享outbox失效与可隔离的目标错误。
class AlertOutboxUnavailable(OSError):
    pass


# LLM: wake/来源事件/明确幂等键是稳定事件身份，不使用每轮随机attempt；无身份时才哈希完整业务载荷。
# 函数用途: 为同一run内不同通知分桶，同时保留完全相同事件的重投去重。
def delivery_event_key(request, context, *, content="", frozen=None, artifacts=()):
    signal = request.wake_signal or {}
    identity = next(((name, signal[name]) for name in ("wake_signal_id", "wake_id", "event_id", "idempotency_key") if signal.get(name)), None)
    if identity is None:
        token = str(context.idempotency_key or "")
        if token.startswith("scheduler-event:"):
            return token.split(":", 2)[1]
        if frozen is not None and frozen.message_metadata.get("scheduler_delivery_event_key"):
            return frozen.message_metadata["scheduler_delivery_event_key"]
        identity = ("idempotency_key", token) if token else ("payload", [content, list(artifacts)])
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# LLM: 目标目录来自同owner真实认证绑定；origin走原冻结路由和显式继任链，local仅指向原执行线程，不接任意地址。
# 函数用途: 生成稳定binding键、实际投递上下文和本地标记，并把include_origin与相同绑定去重。
def alert_delivery_targets(agent, request, context, run):
    from ..conversation.channels import binding_delivery_target
    from .execution import resolve_delivery

    policy = validate_delivery_targets(agent, run.get("delivery"))
    available = {row["binding_id"]: row for row in available_delivery_targets(agent)}
    resolved = {}
    specs = delivery_specs(policy, request)
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
                matches = [row for row in available.values() if any(
                    [binding.channel, binding.channel_user_id, binding.channel_conversation_id] == [row["channel"], row["account_id"], row["chat_id"]]
                    for binding in thread.channel_bindings)
                    and (row["thread_id"] == thread.thread_id or policy.get("follow_latest_channel_session") is False)
                    and row["channel"] == origin_channel and binding_delivery_target(row["channel"], row["account_id"],
                        row["chat_id"], group=agent.home_paths.owner_kind == "group") == origin_target]
                if len(matches) != 1:
                    raise ScheduleDeliveryTargetUnauthorized()
                row = matches[0]
            else:
                row = available[spec["binding_id"]]
                identifier = row["thread_id"]
                if policy.get("follow_latest_channel_session") is False:
                    identifier = (run.get("execution_context") or {}).get("delivery_target_threads", {}).get(spec["binding_id"])
                    if not identifier:
                        raise ScheduleDeliveryTargetUnauthorized()
                thread = agent.conversation_store.threads.require(identifier)
            if (thread.owner_id != agent.home_paths.owner_id or thread.owner_home != str(agent.home_paths.owner_home_dir)
                    or not any([binding.channel, binding.channel_user_id, binding.channel_conversation_id] ==
                        [row["channel"], row["account_id"], row["chat_id"]] for binding in thread.channel_bindings)):
                raise ScheduleDeliveryTargetUnauthorized()
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
    rows = agent.scheduler_repository.delivery_receipts(run["run_id"], event_key=delivery_event_key(request, context))
    if admitted_completion:
        if any(row.get("state") in {"sent", "committed"} and row.get("external_sent") is True
                and row.get("context") == _context_identity(context) for row in rows.values()):
            return True
    try:
        return any(plan["context"] is not None and _context_identity(plan["context"]) == _context_identity(context)
            for plan in _alert_delivery_plans(agent, request, context, run, rows).values())
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
    specs = delivery_specs(policy, request)
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
                one_run = {**run, "delivery": {**policy, "mode": "bound", "targets": [spec], "include_origin": False}}
                [(key, (target, local))] = alert_delivery_targets(agent, request, context, one_run).items()
        except (ValueError, KeyError):
            target, error = None, "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED"
        if key in plans:
            plans[key]["specs"].append(token)
        else:
            plans[key] = {"context": target, "local": local, "specs": [token], "error_code": error}
    if policy['mode'] == 'fallback_on_failure':
        winner = next((key for key in plans if receipts.get(key, {}).get('state') == 'committed'
            or receipts.get(key, {}).get('external_sent') is True), None)
        if winner is not None:
            return {winner: plans[winner]}
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
    # 普通wake仍由原单路sender处理；群thread偏好不能把它提升为调度告警。
    if run is None:
        return None
    if normalize_delivery(run.get("delivery"))["mode"] not in TARGET_DELIVERY_MODES:
        topic = _topic_delivery(delivery, request, reconcile_only=options.get('frozen_retry') is not None)
        return (record_one(topic, request, context, content, **options)
            if topic is not delivery else None)
    if not options.get("deliver") and not options.get("local_transcript_only"):
        return None
    repository = delivery.agent.scheduler_repository
    incoming = options.get("frozen_retry")
    event_key = delivery_event_key(request, context, content=content, frozen=incoming, artifacts=options.get("delivery_artifacts", ()))
    context = replace(context, idempotency_key=f"scheduler-event:{event_key}")
    receipts = repository.delivery_receipts(run["run_id"], event_key=event_key)
    targets = _alert_delivery_plans(delivery.agent, request, context, run, receipts)
    mode = normalize_delivery(run.get('delivery'))['mode']
    planned_specs = [spec for plan in targets.values() for spec in plan['specs']]
    business = _business_result(content, options, receipts)
    if "message_metadata" not in business:
        projection = project_user_reply(business["content"])
        business["content"] = projection.content
        business["evidence_refs"] = list(_background_evidence_refs_for(delivery, request, None))
        business["message_metadata"] = _background_owner_message_metadata(request,
            delivery_reason=options["delivery_reason"], projection_status=projection.projection_status,
            delivery_artifacts=tuple(business["delivery_artifacts"]), evidence_refs=tuple(business["evidence_refs"]),
            operation_verification=business["operation_verification"], public_receipt=business["public_receipt"], frozen_retry=None)
    business["message_metadata"]["scheduler_delivery_event_key"] = event_key
    seed = {"schema_version": "wake-owner-delivery.v2", "task_id": request.task_id, "reason": request.reason,
        "created_at": time.time(), **deepcopy(business), "external_sent": False, "receipt_id": "", "ownership": ""}
    cached = _cache_alert_delivery(delivery, request, seed)
    if not targets and mode == 'severity_routed':
        from .execution import scheduled_local_delivery
        if scheduled_local_delivery(delivery.agent, request):
            target = replace(context, thread_id=run['thread_id'], channel='internal', target=run['thread_id'],
                mode='proactive', reply_to='', progress_handle='')
            return record_one(delivery, request, target, business['content'], **{**options,
                'frozen_retry': frozen_owner_delivery(seed), 'local_transcript_only': True,
                'route_supports_transcript': True, 'delivery_event_key': event_key})
    commits = []
    for key, plan in targets.items():
        target, local = plan["context"], plan["local"]
        previous = receipts.get(key, {})
        if previous.get("state") == "committed":
            if mode == 'fallback_on_failure':
                break
            continue
        # 先存原结果和目标角色；网络在此之后，任何目标失败也不需重新调用模型。
        saved = {**previous, "state": previous.get("state", "pending"), "specs": plan["specs"], "local": local,
            "business_result": deepcopy(business), "error_code": plan["error_code"], "planned_specs": planned_specs}
        repository.record_delivery_receipt(run["run_id"], key, saved, event_key=event_key)
        if target is None:
            continue
        original_target = (incoming.message_metadata.get("scheduler_delivery_target_key") if incoming is not None else None)
        primary_sent = bool(incoming is not None and incoming.external_sent and incoming.receipt_id and not local
            and (original_target == key if original_target else target.channel == context.channel and target.target == context.target))
        retry = frozen_owner_delivery(previous["frozen"]) if previous.get("frozen") else incoming if primary_sent else (
            frozen_owner_delivery(seed) if options.get("frozen_retry") is not None else None)
        if retry is not None and not previous.get("external_sent") and not primary_sent:
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
                    "context": _context_identity(target), "frozen": frozen}, event_key=event_key)
            except OSError as exc:
                raise AlertOutboxUnavailable(str(exc)) from exc
            _cache_alert_delivery(delivery, request, frozen)
        kwargs = {**options, **{name: business[name] for name in ("operation_verification", "public_receipt", "display_snapshot")},
            "delivery_artifacts": tuple(business["delivery_artifacts"]), "assistant_commentaries": tuple(business["assistant_commentaries"]),
            "frozen_retry": retry, "local_transcript_only": local or options.get("local_transcript_only", False),
            "route_supports_transcript": True if local else None,
            "delivery_observer": observe, "delivery_target_key": key, "delivery_event_key": event_key}
        if retry is not None:
            kwargs.update(delivery_artifacts=retry.delivery_artifacts, assistant_commentaries=retry.assistant_commentaries)
        try:
            topic = _topic_delivery(delivery, request, reconcile_only=retry is not None)
            commit = record_one(topic, request, target, retry.content if retry is not None else business["content"], **kwargs)
        except AlertOutboxUnavailable:
            raise
        except OSError:
            saved = repository.delivery_receipts(run["run_id"], event_key=event_key).get(key, {})
            repository.record_delivery_receipt(run["run_id"], key, {**saved, "error_code": "TARGET_DELIVERY_IO_ERROR"}, event_key=event_key)
            # 已外发只欠canonical，不得推进下一线或覆盖原wake的已发回执。
            if mode == 'fallback_on_failure' and saved.get('external_sent'):
                break
            continue
        commits.append(commit)
        saved = repository.delivery_receipts(run["run_id"], event_key=event_key).get(key, {})
        repository.record_delivery_receipt(run["run_id"], key, {**saved,
            "state": "committed" if commit.persisted and (commit.delivery_status == "sent" or local and commit.delivery_status == "local_only")
                else "sent" if saved.get("external_sent") else "pending",
            "delivery_status": commit.delivery_status, "context": _context_identity(target), "message_id": commit.message_id}, event_key=event_key)
        if mode == 'fallback_on_failure' and (commit.persisted or saved.get('external_sent')):
            break
    receipts = repository.delivery_receipts(run["run_id"], event_key=event_key)
    complete = (any(row.get('state') == 'committed' for row in receipts.values()) if mode == 'fallback_on_failure' else
        bool(targets) and all(receipts.get(key, {}).get("state") == "committed" for key in targets))
    return BackgroundDeliveryCommit(content=business["content"], delivery_status=("sent" if any(row.get("delivery_status") == "sent" for row in receipts.values())
        else "local_only") if complete else "failed", persisted=complete,
        message_id=next((row.message_id for row in reversed(commits) if row.message_id), ""),
        commit_kind=("external_delivery" if any(row.get("delivery_status") == "sent" for row in receipts.values())
            else "canonical_transcript") if complete else "outbox_pending", transcript_route=True,
        outbox_frozen=cached or any(row.outbox_frozen for row in commits))


# LLM: 发送适配沿原 DeliveryService 协议，不另建发送出口或修改共享配置。
# 函数用途: 为一次告警构造隔离的锚点投递视图。
def _topic_delivery(delivery, request, *, reconcile_only=False):
    if getattr(getattr(delivery.agent, 'home_paths', None), 'owner_kind', '') != 'group':
        return delivery
    signal = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = signal.get('metadata') if isinstance(signal.get('metadata'), dict) else {}
    event_id = (signal.get('event_id') or metadata.get('event_id') or signal.get('wake_signal_id')
        or signal.get('wake_id') or signal.get('idempotency_key'))
    source = signal.get('source') or metadata.get('source') or metadata.get('source_system') or 'scheduler'
    if not isinstance(event_id, str) or not event_id or not isinstance(source, str) or not source:
        return delivery
    return replace(delivery, channels=_AlertTopicChannels(delivery.channels, delivery.agent, request,
        source=source, event_id=event_id, reconcile_only=reconcile_only))


# LLM: 只观察本次原DeliveryService进入provider的事实，不修改共享registry或绕过其前置门。
# 类用途: 给首发提供一个同授权、同能力的临时注册表视图。
class _AlertDispatchRegistry:
    # LLM: 旧组件事实必须一起核验，不能把本次零调用当成历史从未发送。
    # 函数用途: 捕获原组件账并初始化派发观察。
    def __init__(self, registry):
        from ..delivery.registry import current_component_delivery

        self._registry, self.dispatched = registry, False
        state = current_component_delivery()
        self.prior_receipts = state.snapshot() if state is not None else ()

    # LLM: 能力、目标校验和配置仍由原registry负责。
    # 函数用途: 透传非发送查询。
    def __getattr__(self, name):
        return getattr(self._registry, name)

    # LLM: 不构造或替换provider，只在原解析结果外记录进入发送接口的时刻。
    # 函数用途: 包装本次已解析适配器。
    def adapter_for(self, channel):
        adapter = self._registry.adapter_for(channel)
        return _AlertDispatchAdapter(adapter, self,
            tracks_components=self._registry.capabilities_for(channel).component_receipts) if adapter is not None else None


# LLM: 包装原组件状态的发送边界，不新建账或替换persist；旧事实与宿主outbox仍由原state保存。
# 类用途: 观察适配器承诺的before_send阶段。
class _AlertComponentState:
    # LLM: 只持同一次发送的原状态和内存观察器。
    # 函数用途: 绑定组件状态的同权视图。
    def __init__(self, state, observer):
        self._state, self._observer = state, observer

    # LLM: ensure/save/finish/snapshot及所有字段均使用原账和原sink。
    # 函数用途: 透传原组件状态接口。
    def __getattr__(self, name):
        return getattr(self._state, name)

    # LLM: before_send成功预记派发才算进入潜在发送；持久化失败前不伪造已派发。
    # 函数用途: 记录适配器真正取得提供方发送许可的时刻。
    def before_send(self, receipt, provider):
        prepared = self._state.before_send(receipt, provider)
        if prepared is not None:
            self._observer.dispatched = True
        return prepared


# LLM: 发送参数和返回值原样交回已注册provider；旧适配器无组件契约时仍保守按入口判断。
# 类用途: 观察真实provider发送边界。
class _AlertDispatchAdapter:
    # LLM: 仅registry显式承诺component_receipts时采用组件派发合同，不从类名或空回执猜测。
    # 函数用途: 绑定原适配器、能力和本次观察器。
    def __init__(self, adapter, observer, *, tracks_components):
        self._adapter, self._observer, self._tracks_components = adapter, observer, tracks_components

    # LLM: 不伪造未实现的能力或提供方lookup。
    # 函数用途: 透传原适配器属性。
    def __getattr__(self, name):
        return getattr(self._adapter, name)

    # LLM: 宿主outbox已经进入本次原state后才加观察，不提前注入state绕过host_delivery_store。
    # 函数用途: 沿原组件发送门观察派发，旧适配器一进入发送仍保守未知。
    @contextmanager
    def _dispatch_scope(self):
        from ..delivery.registry import component_delivery_scope, current_component_delivery

        state = current_component_delivery()
        if self._tracks_components and state is not None:
            with component_delivery_scope(_AlertComponentState(state, self._observer)):
                yield
        else:
            self._observer.dispatched = True
            yield

    # LLM: 原适配器仍负责正文格式、认证与请求；不把适配器准备失败当作消息已经发出。
    # 函数用途: 观察主动正文发送。
    def send_message(self, target, message):
        with self._dispatch_scope():
            return self._adapter.send_message(target, message)

    # LLM: 回复沿原状态和原提供方，不另建发送出口。
    # 函数用途: 观察原线程回复。
    def finalize_response(self, target, handle, message):
        with self._dispatch_scope():
            return self._adapter.finalize_response(target, handle, message)

    # LLM: 可选线程参数仅在原调用提供时传递，不扩大不支持thread的适配器协议。
    # 函数用途: 观察冻结附件发送。
    def send_attachment_content(self, target, content, *, idempotency_key, as_image=False,
                                reply_to=None, reply_in_thread=None):
        options = {} if reply_to is None and reply_in_thread is None else {
            'reply_to': reply_to, 'reply_in_thread': reply_in_thread}
        with self._dispatch_scope():
            return self._adapter.send_attachment_content(target, content,
                idempotency_key=idempotency_key, as_image=as_image, **options)


# LLM: 只有本次原生sender零调用且旧账/当前回执也无发送事实才可释放；不透明sender失败仍保守未知。
# 函数用途: 核对首发失败是否明确发生在provider派发前。
def _topic_send_not_started(observer, receipt=None):
    from ..delivery.registry import current_component_delivery

    if observer is None or observer.dispatched:
        return False
    if receipt is not None and receipt.delivery_status in {'sent', 'unknown'}:
        return False
    state = current_component_delivery()
    facts = (*observer.prior_receipts, *(state.snapshot() if state is not None else ()),
        *getattr(receipt, 'component_receipts', ()))
    for fact in facts:
        row = fact if isinstance(fact, dict) else vars(fact)
        if (row.get('first_dispatch_at') is not None or row.get('phase') in {'unknown', 'send_confirmed'}
                or row.get('effect_outcome') in {'unknown', 'confirmed'}):
            return False
    return True


# LLM: 沿唯一锚点账的原锁/nofollow写口释放当前首发预留；已确认根和其它派发身份不能被删除。
# 函数用途: 让确定未发送的告警在下一次重试重新取得首发权。
def _release_unsent_topic(owner_home, key, dispatch_key):
    from ..common.json_io import locked_json_path
    from . import alert_topic_anchors as anchors

    root, lock = anchors._prepare(owner_home, key)
    with locked_json_path(lock):
        data = anchors._read(root)
        row = data['anchors'].get(key, {})
        if (row.get('state') not in {'pending', 'unknown'} or row.get('root_message_id')
                or data.get('root_delivery_keys', {}).get(key) != dispatch_key):
            return
        del data['anchors'][key]
        data.get('root_delivery_keys', {}).pop(key, None)
        anchors._write(root, data)


class _AlertTopicChannels:
    """一次告警的通道策略视图；网络和权限仍由原 sender 与 DeliveryService 执行。"""

    # LLM: 不修改共享 channels、config、Owner 或请求，只固定当前事件的路由资料。
    # 函数用途: 绑定当前告警的原投递能力和事件身份。
    def __init__(self, channels, agent, request, *, source, event_id, reconcile_only=False):
        self._channels, self._agent, self._request = channels, agent, request
        self._source, self._event_id, self._reconcile_only = source, event_id, reconcile_only

    # LLM: 既有声明、能力和注册表仍由同一原服务提供，不新增或伪造外发能力。
    # 函数用途: 保留原 DeliveryService 的能力查询接口。
    def __getattr__(self, name):
        return getattr(self._channels, name)

    # LLM: 首发 pending 先落盘且锁内只认一个首发者；未知先查提供方，查不到就停，不绕过原发送/审批/副作用门。
    # 函数用途: 首次创建告警根，后续在已确认根的话题内回复。
    def deliver(self, context, envelope):
        from datetime import datetime, timezone

        from . import alert_topic_anchors as anchors

        if not alert_topic_enabled(self._agent, context):
            return self._channels.deliver(context, envelope)
        home = self._agent.home_paths
        key = anchors.make_alert_key(self._source, home.owner_id, context.target, self._event_id)
        now = datetime.now(timezone.utc).isoformat()
        dispatch_key = str(context.idempotency_key or self._request.task_id)
        row, first, root_dispatch = anchors._reserve_for_delivery(home.owner_home_dir, key,
            now=now, dispatch_key=dispatch_key)
        reconciled = False
        if not first and row['state'] != 'sent':
            row = self._reconcile(context, key, row, now)
            reconciled = row['state'] == 'sent'
            if not reconciled:
                anchors.mark_unknown(home.owner_home_dir, key, now=now)
                return _topic_receipt(context, envelope, key, row, sent=False)
        if not first and (root_dispatch == dispatch_key or reconciled and not root_dispatch and self._reconcile_only):
            return _topic_receipt(context, envelope, key, row, sent=True)
        routed = (replace(context, mode='proactive', reply_to='', progress_handle='', reply_policy='reference',
            provider_thread_id='', root_id='', parent_id='', idempotency_key='alert-topic:' + key)
            if first else replace(context, mode='reply', reply_to=row['root_message_id'], progress_handle='',
                reply_policy='thread', provider_thread_id=row['thread_id'], root_id=row['root_message_id'], parent_id=''))
        # 仅原生统一sender可提供可证明的零派发观察；不透明发送器不因空回执获得重发许可。
        from ..delivery.service import DeliveryService

        channels, observer = self._channels, None
        if first and type(channels) is DeliveryService:
            observer = _AlertDispatchRegistry(channels.registry)
            channels = copy(channels)
            channels.registry = observer
        try:
            receipt = channels.deliver(routed, envelope)
        except Exception:
            if first:
                if _topic_send_not_started(observer):
                    _release_unsent_topic(home.owner_home_dir, key, dispatch_key)
                else:
                    anchors.mark_unknown(home.owner_home_dir, key, now=now)
            raise
        if first:
            root_message_id = _confirmed_provider_root(receipt)
            if receipt.delivery_status == 'sent' and root_message_id:
                row = anchors.mark_sent(home.owner_home_dir, key, root_message_id=root_message_id, thread_id='', now=now)
                self._remember_seed(context, envelope, row)
            elif _topic_send_not_started(observer, receipt):
                _release_unsent_topic(home.owner_home_dir, key, dispatch_key)
            else:
                anchors.mark_unknown(home.owner_home_dir, key, now=now)
        return receipt

    # LLM: 只有提供方显式 lookup 能力能恢复未知根，结果必须绑定当前键与目标；无能力不靠 fetch(id未知) 猜测。
    # 函数用途: 在再次发送前精确核对已创建的根消息。
    def _reconcile(self, context, key, row, now):
        from .alert_topic_anchors import mark_sent

        registry = getattr(self._channels, 'registry', None)
        adapter = registry.adapter_for(context.channel) if registry is not None else None
        lookup = getattr(adapter, 'lookup_alert_root', None)
        if not callable(lookup):
            return row
        try:
            found = lookup(alert_key=key, target_chat_id=context.target)
        except Exception as exc:
            logging.getLogger(__name__).warning('告警根核对失败 error_type=%s', type(exc).__name__)
            return row
        if (not isinstance(found, dict) or found.get('state') != 'sent' or found.get('alert_key') != key
                or found.get('target_chat_id') != context.target or not isinstance(found.get('root_message_id'), str)
                or not found['root_message_id'] or not isinstance(found.get('thread_id', ''), str)):
            return row
        return mark_sent(self._agent.home_paths.owner_home_dir, key, root_message_id=found['root_message_id'],
            thread_id=found.get('thread_id', ''), now=now)

    # LLM: 首发根已确认后才建立种子；可选资料写失败不冒充发送失败或再次创建根。
    # 函数用途: 保存原始告警标题、正文、事件与准确 run 引用。
    def _remember_seed(self, context, envelope, row):
        from ..adapter.topic_seed import remember_topic_seed

        signal = self._request.wake_signal or {}
        metadata = signal.get('metadata') if isinstance(signal.get('metadata'), dict) else {}
        try:
            remember_topic_seed(self._agent.home_paths.owner_home_dir, chat_id=context.target,
                root_message_id=row['root_message_id'], title=signal.get('title') or metadata.get('title') or '告警',
                source=self._source, event_id=self._event_id, original_text=envelope.content,
                run_refs=tuple(value for value in (self._request.task_id, metadata.get('run_id')) if isinstance(value, str)))
        except Exception as exc:
            logging.getLogger(__name__).warning('告警背景记录失败 error_type=%s', type(exc).__name__)


# LLM: 逻辑 receipt_id 不是外部消息；只接受正文组件中提供方 send_confirmed/confirmed 的准确消息 ID。
# 函数用途: 从原 DeliveryService 的组件事实提取已确认根。
def _confirmed_provider_root(receipt):
    for component in getattr(receipt, 'component_receipts', ()):
        row = component if isinstance(component, dict) else vars(component)
        if row.get('phase') == 'send_confirmed' and row.get('effect_outcome') == 'confirmed':
            value = row.get('provider_message_id')
            if isinstance(value, str) and value:
                return value
    return ''


# LLM: 此回执只表示精确 lookup 的已送达事实或明确未知，绝不产生额外外部发送。
# 函数用途: 让原 sender 继续补 canonical 或冻结未知结果。
def _topic_receipt(context, envelope, key, row, *, sent):
    from ..conversation.channels import ComponentDeliveryReceipt, DeliveryReceipt

    return DeliveryReceipt(channel=context.channel, target=context.target, content=envelope.content,
        thread_id=context.thread_id, task_id=context.task_id, delivery_status='sent' if sent else 'unknown',
        receipt_id='alert-topic:' + key if sent else '',
        component_receipts=(ComponentDeliveryReceipt(component_id='root', message_key='alert-topic:' + key,
            phase='send_confirmed', effect_outcome='confirmed', provider_message_id=row['root_message_id']),) if sent else (),
        phase='send_confirmed' if sent else 'unknown', effect_outcome='confirmed' if sent else 'unknown')
