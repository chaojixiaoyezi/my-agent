"""F3领域入口。后台主是canonical thread，不是一个永不结束的子代理run。

宿主负责认证/能力闸；本层再核Owner/空间与CAS。接单复用原SessionTask、guidance和wake，
模型只在原调度车道执行；managed_by只描述组织，不参与执行父链。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace

from ..gateway_parts.io import locked_file_transition
from ..plugin_host.business_action_specs import business_action_spec
from ..plugin_host.schemas import validate_object
from .background_team_lifecycle import (
    BACKGROUND_IDENTITY_KEY,
    background_identity,
    background_identity_allows_work,
    background_identity_transition,
    identity_outstanding,
    reconcile_background_identity,
)
from .business_contracts import (
    BackgroundAgentIdentity,
    BusinessContextIntent,
    BusinessExecutionContext,
)
from .host_actor import HostChannelActor
from .models import ConversationThread
from .session_list_index import SessionListIndex
from .session_task_dispatch import (
    SessionTaskDispatchError,
    SessionTaskDispatchPlan,
    dispatch_session_task,
    session_task_pair_limit_error,
)
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES
from .store_threads import _read_thread_record_report, create_thread_atomic


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _params(action, request):
    return validate_object(request, business_action_spec(action).params_schema)


def _view(thread):
    identity = background_identity(thread)
    return {**identity.to_dict(), "thread_id": thread.thread_id, "name": thread.title,
            "purpose": str(thread.metadata.get("background_purpose") or ""),
            "model_profile_id": thread.model_profile_id, "selection_revision": thread.model_selection_revision,
            "revision": identity.identity_revision}


def _agent_view(thread):
    view = _view(thread)
    view.pop("identity_revision")
    return view


class BackgroundAgentService:
    def __init__(self, host):
        self.host = host
        self.store = host.conversation_store

    def _actor(self, actor):
        if not isinstance(actor, HostChannelActor) or actor.owner_id != str(self.host.home_paths.owner_id):
            raise PermissionError("OWNER_MISMATCH")
        source = self.store.threads.require(actor.source_thread_id)
        if source.owner_id != actor.owner_id:
            raise PermissionError("OWNER_MISMATCH")
        return source

    def _space(self, space_id, actor):
        from .business_spaces import BusinessSpaceService

        observed = BusinessSpaceService(self.host).get(space_id, actor=actor)
        space = observed.get("space", observed)
        if space.get("space_id") != space_id or space.get("state") != "active":
            raise PermissionError("SPACE_UNAVAILABLE")
        return space

    def _thread(self, agent_id, actor):
        self._actor(actor)
        thread = self.store.threads.require(agent_id)
        if thread.owner_id != actor.owner_id or background_identity(thread) is None:
            raise PermissionError("BACKGROUND_AGENT_OUT_OF_SCOPE")
        self._space(background_identity(thread).space_id, actor)
        return thread

    def _index(self):
        def read(path):
            thread, error = _read_thread_record_report(path)
            return thread.to_dict() if thread else {}, error
        return SessionListIndex(self.store.storage.threads_dir, kind="thread", reader=read)

    def _publish(self, thread):
        self._index().publish(self.store.storage.thread_path(thread.thread_id))
        return thread

    def _operation_key(self, actor, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 2048:
            raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
        return _digest([actor.owner_id, actor.source_thread_id, key])

    def _profile(self, requested=None):
        from ..settings.model_profiles import selected_model_config
        from ..settings.thread_model_selection import default_model_profile_id

        profile = requested or default_model_profile_id(self.host)
        if not profile:
            raise ValueError("MODEL_PROFILE_REQUIRED")
        selected_model_config(self.host, profile_id=profile)
        return profile

    # LLM: 只在创建锁内已确认不存在的新行上调用；适配器可附加原安装出处，不授权修改既有线程。
    # 函数用途: 为新线程提供元数据扩展点；原生入口保持输入不变。
    def _thread_creation_metadata(self, *, actor, metadata: dict) -> dict:
        return metadata

    def create(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
        request = _params("background_agents.create", request)
        source = self._actor(actor)
        self._space(request["space_id"], actor)
        key = self._operation_key(actor, idempotency_key)
        identifier = "thread-bg-" + key[:32]
        fingerprint = _digest(request)
        with locked_file_transition(self.store.storage.threads_dir / (".background-create-" + key)):
            existing = self.store.threads.load(identifier)
            if existing is not None:
                creation = existing.metadata.get("background_creation")
                if existing.owner_id != actor.owner_id or creation != {"key": key, "fingerprint": fingerprint}:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                self._thread(identifier, actor)
                return {"agent": _agent_view(existing), "revision": background_identity(existing).identity_revision}
            profile = self._profile(request.get("model_profile_id"))
            identity = BackgroundAgentIdentity(identifier, source.thread_id, request["space_id"], request.get("project_ref"),
                                               "available", request.get("after_task", "reuse"), 1)
            current = time.time()
            metadata = self._thread_creation_metadata(actor=actor, metadata={
                BACKGROUND_IDENTITY_KEY: identity.to_dict(), "background_purpose": request.get("purpose", ""),
                "background_creation": {"key": key, "fingerprint": fingerprint}})
            thread = create_thread_atomic(self.store.threads, ConversationThread(
                thread_id=identifier, canonical_user_id=source.canonical_user_id, owner_id=actor.owner_id,
                owner_home=str(self.host.home_paths.owner_home_dir), model_profile_id=profile,
                model_selection_revision=1, model_selection_source="explicit" if request.get("model_profile_id") else "default",
                model_selection_last_explicit_revision=1 if request.get("model_profile_id") else 0,
                model_selection_selected_at=current, title=request["name"], created_at=current, updated_at=current,
                metadata=metadata,
            ))
            self._publish(thread)
            return {"agent": _agent_view(thread), "revision": 1}

    def _controller_id(self, actor):
        return "thread-controller-" + _digest([actor.owner_id, "business-controller.v1"])[:32]

    def get_controller(self, *, actor):
        self._actor(actor)
        identifier = self._controller_id(actor)
        thread = self.store.threads.load(identifier)
        if thread is not None and (thread.owner_id != actor.owner_id or thread.metadata.get("business_controller") is not True):
            raise PermissionError("CONTROLLER_IDENTITY_CONFLICT")
        return {"agent_id": identifier, "thread_id": identifier, "available": thread is not None and thread.status == "active"}

    def ensure_controller(self, *, actor):
        source = self._actor(actor)
        identifier = self._controller_id(actor)
        with locked_file_transition(self.store.storage.threads_dir / ("." + identifier)):
            # inactive只是不可接单，不是不存在；仍按原校验拒绝身份冲突，不重写安装出处。
            self.get_controller(actor=actor)
            if self.store.threads.load(identifier) is not None:
                return {"agent_id": identifier, "thread_id": identifier, "created": False}
            current = time.time()
            metadata = self._thread_creation_metadata(actor=actor, metadata={"business_controller": True})
            create_thread_atomic(self.store.threads, ConversationThread(
                thread_id=identifier, canonical_user_id=source.canonical_user_id, owner_id=actor.owner_id,
                owner_home=str(self.host.home_paths.owner_home_dir), title="总控", created_at=current, updated_at=current,
                model_profile_id=self._profile(), model_selection_revision=1, model_selection_source="default",
                model_selection_selected_at=current, metadata=metadata,
            ))
            return {"agent_id": identifier, "thread_id": identifier, "created": True}

    def list(self, request: dict, *, actor) -> dict:
        request = _params("background_agents.list", request)
        self._actor(actor)
        if request.get("space_id"):
            self._space(request["space_id"], actor)
        page = self._index().page(limit=request.get("limit", 50), cursor=request.get("cursor"), filter_key=actor.owner_id)
        items = []
        for payload in page["items"]:
            thread = ConversationThread.from_dict(payload)
            identity = background_identity(thread)
            if identity is None or (request.get("space_id") and identity.space_id != request["space_id"]):
                continue
            if request.get("lifecycle", "all") not in {"all", identity.identity_state}:
                continue
            self._space(identity.space_id, actor)
            items.append(_agent_view(thread))
        return {"items": items, "next_cursor": page["next_cursor"],
                "coverage": "partial" if page["load_errors"] else page["coverage"]}

    def get(self, agent_id, *, actor):
        thread = self._thread(agent_id, actor)
        page = self.store.session_tasks.list_page(target_thread_id=agent_id, limit=100)
        pending = [self._task_view(task) for task in page["items"] if task.status not in SESSION_TASK_TERMINAL_STATUSES]
        return {"agent": _agent_view(thread), "current_execution": self._execution_view(thread), "pending_tasks": pending,
                "allowed_actions": ["background_agents.get", "background_agents.messages.send", "background_agents.suspend",
                                    "background_agents.resume", "background_agents.retire", "background_agents.model.set"]}

    def _execution_view(self, thread):
        claim, error = self.store.claims.load_report(thread.thread_id)
        if error:
            raise ValueError("CLAIM_UNREADABLE")
        authority = claim.get("runtime_authority")
        if claim.get("status") != "running" or not isinstance(authority, dict):
            return None
        repo = getattr(getattr(self.host, "subagents", None), "runtime_db", None)
        if repo is None:
            raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")
        ref = repo.parent_execution_ref(owner_id=thread.owner_id, run_id=authority["run_id"], attempt_id=authority["attempt_id"])
        context = repo.business_context_for_execution(owner_id=thread.owner_id, run_id=ref.run_id, attempt_id=ref.observed_attempt_id)
        return {"run_id": ref.run_id, "attempt_id": ref.observed_attempt_id, "task_id": context.task_id if context else None,
                "thread_id": thread.thread_id, "space_id": context.space_id if context else None, "status": "running",
                "effect_outcome": "running", "started_at": None, "ended_at": None, "error": None, "retry_refs": []}

    def _task_view(self, task):
        raw = task.metadata.get("business_context")
        context = BusinessExecutionContext.from_dict(raw) if raw else None
        return {"task_id": task.task_id, "thread_id": task.target_thread_id, "space_id": context.space_id if context else None,
                "project_ref": context.project_ref if context else None, "status": task.status,
                "current_run_id": None, "result_refs": list(task.result_refs), "revision": int(task.updated_at * 1000000)}

    def _mutate(self, agent_id, *, actor, action, changes, expected_revision, idempotency_key, model=False):
        self._thread(agent_id, actor)
        key = self._operation_key(actor, idempotency_key)
        fingerprint = _digest([action, changes, expected_revision])

        def apply(latest):
            if latest.owner_id != actor.owner_id:
                raise PermissionError("OWNER_MISMATCH")
            identity = background_identity(latest)
            receipts = dict(latest.metadata.get("background_receipts") or {})
            if key in receipts:
                if receipts[key] != fingerprint:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return latest
            revision = latest.model_selection_revision if model else identity.identity_revision
            if type(expected_revision) is not int or expected_revision != revision:
                raise ValueError("REVISION_CONFLICT")
            if model:
                revision += 1
                updated = replace(latest, model_profile_id=changes["model_profile_id"],
                    model_selection_revision=revision, model_selection_source="explicit",
                    model_selection_last_explicit_revision=revision, provider_context_observation={}, model_context_usage={})
            else:
                state = identity.identity_state
                if action in {"resume", "restore"}:
                    if (action == "resume" and state != "suspended") or (action == "restore" and state != "retired"):
                        raise ValueError("IDENTITY_TRANSITION_CONFLICT")
                    state = "available"
                elif action == "suspend":
                    if state not in {"available", "suspended"}:
                        raise ValueError("IDENTITY_TRANSITION_CONFLICT")
                    state = "suspended"
                elif action == "retire":
                    state = "retiring" if state != "retired" else state
                elif state in {"retired", "retiring"}:
                    raise ValueError("AGENT_NOT_AVAILABLE")
                identity = replace(identity, identity_state=state, after_task=changes.get("after_task", identity.after_task),
                                   identity_revision=identity.identity_revision + 1)
                metadata = {**latest.metadata, BACKGROUND_IDENTITY_KEY: identity.to_dict()}
                if "purpose" in changes:
                    metadata["background_purpose"] = changes["purpose"]
                if action == "retire":
                    metadata["background_retirement"] = {"disposition": changes["disposition"],
                                                         "operation_id": key, "handoff_agent_id": changes.get("handoff_agent_id")}
                updated = replace(latest, title=changes.get("name", latest.title), metadata=metadata)
            receipts[key] = fingerprint
            # 原线程只留有界操作回执；过期回执不得通过旧CAS重新执行。
            receipts = dict(list(receipts.items())[-64:])
            return replace(updated, metadata={**updated.metadata, "background_receipts": receipts}, updated_at=time.time())

        with background_identity_transition(self.store, agent_id):
            return self._publish(self.store.threads.update_atomic(agent_id, apply))

    def update(self, agent_id, changes: dict, *, actor, expected_revision, idempotency_key):
        changes = _params("background_agents.update", {**changes, "agent_id": agent_id})
        thread = self._mutate(agent_id, actor=actor, action="update", changes=changes,
                              expected_revision=expected_revision, idempotency_key=idempotency_key)
        return {"agent": _agent_view(thread), "revision": background_identity(thread).identity_revision}

    # LLM: 原准入锁、Owner/空间和身份CAS保持；动作产生的索引修订须重新完整核对，不能复用旧分页前缀。
    # 函数用途: 控制身份生命周期，按完整任务责任恢复唤醒或取消，并收口实际可完成的退休。
    def transition(self, agent_id, action, *, actor, expected_revision, idempotency_key,
                   disposition="drain", handoff_agent_id=None):
        changes = {"agent_id": agent_id}
        if action == "retire":
            changes["disposition"] = disposition
            if handoff_agent_id is not None:
                changes["handoff_agent_id"] = handoff_agent_id
                source, target = self._thread(agent_id, actor), self._thread(handoff_agent_id, actor)
                if background_identity(source).space_id != background_identity(target).space_id or agent_id == handoff_agent_id:
                    raise ValueError("SPACE_MISMATCH")
                # 不改写在途任务的冻结归属。交接是可观察的待处理义务，不复制任务到新空间。
        _params("background_agents." + action, changes)
        thread = self._mutate(agent_id, actor=actor, action=action, changes=changes,
                              expected_revision=expected_revision, idempotency_key=idempotency_key)
        refs, tasks = identity_outstanding(self.host, thread)
        if action == "retire":
            if disposition == "cancel_tasks":
                for task in tasks:
                    self.cancel_task(task.task_id, actor=actor, include_descendants=True)
            elif disposition == "drain":
                self._republish_pending_work(thread, tasks, actor=actor)
            thread = reconcile_background_identity(self.host, agent_id)
            refs, _ = identity_outstanding(self.host, thread)
            if not refs and background_identity(thread).identity_state == "retiring":
                # 取消后的首个索引回填可能partial；上面的新完整遍历追平后，再走原锁内CAS，不把新空账配给旧身份。
                thread = reconcile_background_identity(self.host, agent_id)
                if background_identity(thread).identity_state == "retiring":
                    refs, _ = identity_outstanding(self.host, thread)
            return {"agent": _agent_view(thread), "operation_id": self._operation_key(actor, idempotency_key), "outstanding_refs": refs[:100]}
        if action in {"resume", "restore"}:
            self._republish_pending_work(thread, tasks, actor=actor)
        return {"agent": _agent_view(thread), "operation_id": self._operation_key(actor, idempotency_key), "state": "completed"}

    # LLM: 只读一次原claim及精确owner/run/attempt关联；空串表示无执行，None表示未知责任，不能靠空root猜SessionTask。
    # 函数用途: 找到当前助手真正执行的派活任务，读取或身份异常时保守阻止重发。
    def _running_session_task(self, thread):
        try:
            claim, error = self.store.claims.load_report(thread.thread_id)
            if error:
                return None
            if claim.get("status") != "running":
                return ""
            authority = claim.get("runtime_authority")
            required = ("invocation_run_id", "task_id", "run_id", "agent_run_id", "attempt_id")
            if (not isinstance(authority, dict) or authority.get("schema_version") != "conversation_claim_execution.v1"
                    or authority.get("claim_id") != claim.get("claim_id")
                    or claim.get("thread_id") != thread.thread_id
                    or any(not isinstance(authority.get(key), str) or not authority[key] for key in required)):
                return None
            repo = self.host.subagents.runtime_db
            ref = repo.parent_execution_ref(owner_id=thread.owner_id, run_id=authority["run_id"], attempt_id=authority["attempt_id"])
            if (ref.agent_run_id != authority["agent_run_id"]
                    or repo.task_id_for_run_id(ref.run_id) != authority["task_id"]):
                return None
            context = repo.business_context_for_execution(owner_id=thread.owner_id, run_id=ref.run_id, attempt_id=ref.observed_attempt_id)
            if context is None or context.owner_id != thread.owner_id or context.space_id != background_identity(thread).space_id:
                return None
            task = self.store.session_tasks.require(context.task_id)
            if task.target_thread_id != thread.thread_id or task.metadata.get("business_context") != context.to_dict():
                return None
            return context.task_id
        except Exception:
            # 原读取边界的任何失败都保留未知责任，不把关联损坏解释成当前没有执行。
            return None

    # LLM: 恢复/退休drain共用原输入；原身份锁内只读一次claim，各任务在同一task锁内检查定位与发布，不追加正文或执行模型。
    # 函数用途: 仅在原唤醒及执行责任均已结束时恢复调度，允许暂停身份排空旧工作。
    def _republish_pending_work(self, thread, tasks, *, actor):
        from .session_messaging import (
            SESSION_MESSAGE_KEY_FIELD,
            SESSION_MESSAGE_ORIGIN_KIND,
            SESSION_TASK_ORIGIN_KIND,
        )

        with background_identity_transition(self.store, thread.thread_id):
            thread = self._thread(thread.thread_id, actor)
            if not background_identity_allows_work(thread, existing=True) or thread.status != "active":
                return
            generation = str(background_identity(thread).identity_revision)
            executing = self._running_session_task(thread)
            for task in tasks:
                current = self.store.session_tasks.require(task.task_id)
                fields = ("task_id", "sender_thread_id", "target_thread_id", "body_dedupe_key", "body_guidance_id")
                if any(getattr(current, field) != getattr(task, field) for field in fields):
                    raise ValueError("SESSION_TASK_SCOPE_MISMATCH")
                if current.target_thread_id != thread.thread_id or self.store.threads.require(current.sender_thread_id).owner_id != actor.owner_id:
                    raise PermissionError("OWNER_MISMATCH")
                if current.status in SESSION_TASK_TERMINAL_STATUSES or not current.body_guidance_id:
                    continue
                if executing is None or executing == current.task_id:
                    continue
                with self.store.wakes.task_responsibility_guard(thread.thread_id, current.task_id):
                    live, error = self.store.wakes.task_responsibility_report(thread.thread_id, current.task_id)
                    if error or live:
                        continue
                    # 消费者可在查询期间结束准确任务；发布前仍沿原任务权威重读。
                    if self.store.session_tasks.require(current.task_id).status in SESSION_TASK_TERMINAL_STATUSES:
                        continue
                    self.store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "session_task", "urgency": "normal",
                        "dedupe_key": f"session_task:{current.task_id}:{generation}", "retain_handled": True,
                        "summary": "恢复原任务", "metadata": {"origin_kind": SESSION_TASK_ORIGIN_KIND,
                            "origin_thread_id": current.sender_thread_id, "session_task_id": current.task_id}})
            # pending_report本来已扫描原队列；limit=0防止只发表最后100条后声称完整。
            inputs, errors = self.store.guidance.pending_report("thread", thread.thread_id, limit=0)
            if errors:
                raise ValueError("GUIDANCE_UNREADABLE")
            for entry in inputs:
                metadata = entry.metadata or {}
                if metadata.get("origin_kind") != SESSION_MESSAGE_ORIGIN_KIND or not metadata.get(SESSION_MESSAGE_KEY_FIELD):
                    continue
                key = metadata[SESSION_MESSAGE_KEY_FIELD]
                receipt = self.store.guidance.receipt(key)
                if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
                    raise ValueError("GUIDANCE_UNREADABLE")
                entry, metadata = receipt.entry, receipt.entry.metadata or {}
                if (receipt.dedupe_key != key or metadata.get(SESSION_MESSAGE_KEY_FIELD) != key
                        or metadata.get("origin_kind") != SESSION_MESSAGE_ORIGIN_KIND
                        or entry.target_type != "thread" or entry.target_id != thread.thread_id
                        or entry.sender != metadata.get("origin_thread_id")
                        or self.store.threads.require(entry.sender).owner_id != actor.owner_id):
                    raise PermissionError("SESSION_MESSAGE_SCOPE_MISMATCH")
                if receipt.status != "pending":
                    continue
                self.store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "session_message", "urgency": "normal",
                    "dedupe_key": str(key) + ":resume:" + generation, "retain_handled": True,
                    "summary": "恢复原补充消息", "metadata": dict(metadata)})

    # LLM: 两个模型读写入口共享同一claim事实映射；读取失败保守等待安全边界，不改变执行租约。
    # 函数用途: 同时给出模型查询的生效时点和写回执的冻结采纳状态。
    def _model_adoption(self, agent_id):
        claim, error = self.store.claims.load_report(agent_id)
        if error:
            return "pending", "pending_safe_boundary"
        if claim.get("status") == "running":
            return "next_safe_turn", "pending_safe_boundary"
        return "current", "applied"

    # LLM: 授权仍由原线程/空间门裁决；生效时点只取共享claim映射，不从模型名推断。
    # 函数用途: 查询后台助手的当前模型选择和采纳边界。
    def get_model(self, agent_id, *, actor):
        thread = self._thread(agent_id, actor)
        effective_at, _adoption_status = self._model_adoption(agent_id)
        return {"model_profile_id": thread.model_profile_id, "selection_revision": thread.model_selection_revision,
                "source": {"kind": thread.model_selection_source or "default", "source_ref": thread.thread_id,
                           "revision": thread.model_selection_revision},
                "effective_at": effective_at}

    # LLM: 原权限、CAS和幂等写入保持；相同选择仍经过_mutate，提交后只返回冻结结果枚举。
    # 函数用途: 保存后台模型选择，按是否改变选择及真实执行边界返回采纳状态。
    def set_model(self, agent_id, model_profile_id, *, actor, expected_revision, idempotency_key):
        from ..user_space.group_authorization_context import group_capability_guard

        _params("background_agents.model.set", {"agent_id": agent_id, "model_profile_id": model_profile_id})
        self._thread(agent_id, actor)
        self._profile(model_profile_id)
        with group_capability_guard(self.host, "assistant.configure", thread_id=agent_id):
            previous = self._thread(agent_id, actor)
            unchanged = previous.model_profile_id == model_profile_id
            thread = self._mutate(agent_id, actor=actor, action="model.set", changes={"model_profile_id": model_profile_id},
                                  expected_revision=expected_revision, idempotency_key=idempotency_key, model=True)
        _effective_at, adoption_status = self._model_adoption(agent_id)
        return {"selection_revision": thread.model_selection_revision,
                "adoption_status": "unchanged" if unchanged else adoption_status}

    def _dispatch(self, request, target, *, actor, idempotency_key, origin_task_id=""):
        with background_identity_transition(self.store, target.thread_id):
            target = self.store.threads.require(target.thread_id)
            if target.owner_id != actor.owner_id:
                raise PermissionError("OWNER_MISMATCH")
            return self._dispatch_locked(request, target, actor=actor, idempotency_key=idempotency_key,
                                         origin_task_id=origin_task_id)

    def _dispatch_locked(self, request, target, *, actor, idempotency_key, origin_task_id):
        from ..capability.config import CapabilityConfig
        from ..capability.runtime_config_reload import capability_config_for_agent
        from .business_context import resolve_business_context

        self._actor(actor)
        self._space(request["space_id"], actor)
        config = capability_config_for_agent(self.host) or CapabilityConfig()
        plan = SessionTaskDispatchPlan(actor.source_thread_id, target.thread_id, request["instruction"],
                                       origin_task_id, self._operation_key(actor, idempotency_key))
        from .session_task_dispatch import _task_dedupe_key
        previous = self.store.session_tasks.find_by_dedupe_key(_task_dedupe_key(plan))
        if previous is None and not background_identity_allows_work(target):
            raise ValueError("AGENT_NOT_AVAILABLE")
        if previous is None and (failure := session_task_pair_limit_error(self.host, plan.sender_thread_id, plan.target_thread_id, config)):
            raise ValueError(failure.error_code)
        intent = BusinessContextIntent(actor.owner_id, request["space_id"], request.get("project_ref"), None)

        def factory(task_id):
            return resolve_business_context(actor=actor, task_id=task_id, requested_space_id=intent.requested_space_id,
                project_ref=intent.project_ref, binding_id=intent.binding_id,
                requested_method_refs=intent.requested_method_refs, requested_asset_version_refs=intent.requested_asset_version_refs)

        outcome = dispatch_session_task(self.host, plan, target, config, count_pair=previous is None,
                                        business_context_intent=intent, business_context_factory=factory)
        if not outcome.ok:
            raise SessionTaskDispatchError(outcome)
        return json.loads(outcome.output)

    def assign(self, request: dict, *, actor, idempotency_key):
        request = _params("tasks.assign", request)
        target = self._thread(request["agent_id"], actor)
        identity = background_identity(target)
        if identity.space_id != request["space_id"] or identity.project_ref != request.get("project_ref"):
            raise ValueError("SPACE_MISMATCH")
        if not background_identity_allows_work(target):
            raise ValueError("AGENT_NOT_AVAILABLE")
        if request.get("origin_task_id"):
            self._owned_task(request["origin_task_id"], actor)
        result = self._dispatch(request, target, actor=actor, idempotency_key=idempotency_key,
                                origin_task_id=request.get("origin_task_id", ""))
        return {"assignment_ref": result["task_id"], "task_id": result["task_id"], "receipt_state": "accepted"}

    def submit(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
        request = _params("tasks.submit", request)
        self._actor(actor)
        # 资产并非权限。明确声明却尚无资产接入端口时拒绝，不静默丢弃用户的选择。
        if request.get("asset_refs"):
            raise ValueError("ASSET_BINDING_UNAVAILABLE")
        identifier = self.ensure_controller(actor=actor)["thread_id"]
        if request.get("controller_agent_id", identifier) != identifier:
            raise PermissionError("CONTROLLER_IDENTITY_CONFLICT")
        result = self._dispatch(request, self.store.threads.require(identifier), actor=actor, idempotency_key=idempotency_key)
        return {"task_id": result["task_id"], "request_id": result["task_id"],
                "operation_id": result["task_id"], "state": "accepted"}

    def _owned_task(self, task_id, actor):
        self._actor(actor)
        task = self.store.session_tasks.require(task_id)
        for identifier in (task.sender_thread_id, task.target_thread_id):
            if self.store.threads.require(identifier).owner_id != actor.owner_id:
                raise PermissionError("OWNER_MISMATCH")
        context = BusinessExecutionContext.from_dict(task.metadata["business_context"])
        if context.owner_id != actor.owner_id or context.task_id != task.task_id:
            raise PermissionError("OWNER_MISMATCH")
        self._space(context.space_id, actor)
        return task, context

    def follow_up(self, request: dict, *, actor, idempotency_key):
        request = _params("tasks.follow_up", request)
        task, context = self._owned_task(request["task_id"], actor)
        result = self._dispatch({"space_id": context.space_id, "project_ref": context.project_ref,
                                 "instruction": request["instruction"]}, self.store.threads.require(task.target_thread_id),
                                actor=actor, idempotency_key=idempotency_key, origin_task_id=task.task_id)
        return {"new_task_id": result["task_id"], "prior_task_ref": task.task_id, "operation_id": result["task_id"]}

    def cancel_task(self, task_id, *, actor, include_descendants=False):
        from .session_messaging import cancel_owned_session_task

        task, _ = self._owned_task(task_id, actor)
        return cancel_owned_session_task(self.host, task, include_descendants=include_descendants)


def cancel_business_execution(host, service, params, *, actor):
    from ..gateway_parts.session_task_stop import stop_business_session_task
    from ..subagents.cancellation import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
        cancel_subagent_tree,
    )
    from .session_messaging import authorize_business_child

    repo = host.subagents.runtime_db
    if repo is None:
        raise PermissionError("EXECUTION_AUTHORITY_REQUIRED")
    ref = repo.parent_execution_ref(owner_id=actor.owner_id, run_id=params["run_id"], attempt_id=params["attempt_id"])
    context = repo.business_context_for_execution(owner_id=actor.owner_id, run_id=ref.run_id, attempt_id=ref.observed_attempt_id)
    if context is None:
        raise PermissionError("BUSINESS_EXECUTION_CONTEXT_REQUIRED")
    task, _ = service._owned_task(context.task_id, actor)
    try:
        child = host.subagents.load(ref.run_id)
    except FileNotFoundError:
        child = None
    if child is not None:
        child = authorize_business_child(host, ref.run_id, actor, "cancel")
        cancel = cancel_subagent_tree if params["include_descendants"] else cancel_subagent_task
        result = cancel(host, CancelSubagentTaskRequest(child, "execution_cancel", source="business_api"))
        confirmed = result.get("ok") is True and not result.get("descendants", {}).get("failed")
        return {"operation_id": ref.run_id, "state": "completed" if confirmed else "pending",
                "unconfirmed_refs": [] if confirmed else [ref.run_id]}
    confirmed = stop_business_session_task(host, task, execution_ref=ref,
        include_descendants=params["include_descendants"], cancel_task=False)
    return {"operation_id": ref.run_id, "state": "completed" if confirmed else "pending",
            "unconfirmed_refs": [] if confirmed else [ref.run_id]}
