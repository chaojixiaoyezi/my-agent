"""F3领域入口。后台主是canonical thread，不是一个永不结束的子代理run。

宿主负责认证/能力闸；本层再核Owner/空间与CAS。接单复用原SessionTask、guidance和wake，
模型只在原调度车道执行；managed_by只描述组织，不参与执行父链。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import replace

from ..gateway_parts.io import locked_file_transition
from ..plugin_host.business_action_specs import business_action_spec
from ..plugin_host.schemas import validate_object
from ..runtime_errors import DataCorruptionError
from .background_agent_quota import BackgroundAgentQuota
from .background_team_lifecycle import (
    BACKGROUND_IDENTITY_KEY,
    background_identity,
    background_identity_allows_work,
    background_identity_transition,
    identity_outstanding,
    reconcile_background_identity,
)
from .business_access import BusinessError
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
from .session_task_report import public_result_refs
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES
from .store_threads import _read_thread_record_report, create_thread_atomic


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class BackgroundAgentListIndex(SessionListIndex):
    """隔离前台 chat threads，确保后台 seat 索引互不污染且按 version 平滑自愈。"""

    def __init__(self, root, *, reader):
        super().__init__(root, kind="thread", reader=reader)
        self.path = self.root.parent / f".{self.root.name}-background_agents_v2-list.sqlite3"
        self.scope = hashlib.sha256(f"{self.root.resolve()}:background_agents_v2".encode()).hexdigest()[:24]


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


def background_agent_index(store):
    """后台身份与树共用原索引、规范reader，不增加影子目录。"""
    def read(path):
        thread, error = _read_thread_record_report(path)
        if error is not None:
            return {}, error
        if thread is None or background_identity(thread) is None:
            return {"owner_id": "__foreground__"}, None
        return thread.to_dict(), None
    return BackgroundAgentListIndex(store.storage.threads_dir, reader=read)


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

    # LLM: 供身份创建、查询和派活复核归属；先核Actor与Owner，None表示无归属，非空只读核活跃空间，不选择默认空间。
    # 函数用途: 确认指定空间可用；无归属时直接返回None。
    def _space(self, space_id, actor):
        from .business_access import BusinessActor, BusinessError
        from .business_spaces import BusinessSpaceService

        self._actor(actor)
        if space_id is None:
            return None
        catalog = getattr(self.host, "business_spaces_catalog", None)
        if catalog is not None and hasattr(catalog, "get"):
            try:
                observed = catalog.get(space_id, actor=actor)
            except (BusinessError, TypeError):
                biz_actor = BusinessActor(actor.owner_id, actor.source_thread_id, catalog, lambda _a, _f: True)
                observed = catalog.get(space_id, actor=biz_actor)
        else:
            try:
                observed = BusinessSpaceService(self.host).get(space_id, actor=actor)
            except (BusinessError, TypeError):
                owner_home = getattr(getattr(self.host, "home_paths", None), "owner_home_dir", None)
                owner_id = str(getattr(getattr(self.host, "home_paths", None), "owner_id", "") or "")
                enabled = bool(getattr(getattr(self.host, "agent_config", None), "business_spaces_enabled", False))
                svc = BusinessSpaceService(owner_home, owner_id=owner_id, enabled=enabled)
                biz_actor = BusinessActor(actor.owner_id, actor.source_thread_id, svc, lambda _a, _f: True)
                observed = svc.get(space_id, actor=biz_actor)
        space = observed.get("space", observed)
        if space.get("space_id") != space_id or space.get("state") != "active":
            raise PermissionError("SPACE_UNAVAILABLE")
        return space

    # LLM: 校验目标后台助手归属与身份；优先按 thread_id 加载，找不到时回退按助手名称定位。
    # 函数用途: 定位并复核一个后台助手的线程记录与空间访问许可。
    def _thread(self, agent_id, actor):
        self._actor(actor)
        thread = self.store.threads.load(agent_id)
        if thread is None:
            page = self._index().page(limit=100, cursor=None, filter_key=actor.owner_id)
            for payload in page["items"]:
                cand = ConversationThread.from_dict(payload)
                if cand.title == agent_id and cand.owner_id == actor.owner_id and background_identity(cand) is not None:
                    thread = self.store.threads.load(cand.thread_id)
                    if thread is not None:
                        break
        if thread is None:
            thread = self.store.threads.require(agent_id)
        if thread.owner_id != actor.owner_id or background_identity(thread) is None:
            raise PermissionError("BACKGROUND_AGENT_OUT_OF_SCOPE")
        self._space(background_identity(thread).space_id, actor)
        return thread

    def _index(self):
        return background_agent_index(self.store)

    def _publish(self, thread):
        self._index().publish(self.store.storage.thread_path(thread.thread_id))
        return thread

    def _operation_key(self, actor, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 2048:
            raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
        return _digest([actor.owner_id, actor.source_thread_id, key])

    def _profile(self, requested=None, *, source_profile: str = ""):
        from ..settings.model_profiles import selected_model_config
        from ..settings.model_provider_schema import ModelProfileError
        from ..settings.thread_model_selection import default_model_profile_id

        effective_source = source_profile or getattr(self, "_current_source_profile", "")

        if requested:
            try:
                selected_model_config(self.host, profile_id=requested)
                return requested
            except ModelProfileError as exc:
                # BIZ-006：写入前的确定性拒绝必须是结构化 not_started，不能被运输层归成 OUTCOME_UNKNOWN。
                code = "MODEL_PROFILE_NOT_FOUND" if getattr(exc, "reason", "") == "profile_not_found" else "MODEL_PROFILE_UNAVAILABLE"
                raise BusinessError(code) from exc

        if effective_source:
            try:
                selected_model_config(self.host, profile_id=effective_source)
                return effective_source
            except ModelProfileError:
                pass

        profile = default_model_profile_id(self.host)
        if not profile:
            raise BusinessError("MODEL_PROFILE_REQUIRED")
        try:
            selected_model_config(self.host, profile_id=profile)
        except ModelProfileError as exc:
            # BIZ-006：写入前的确定性拒绝必须是结构化 not_started，不能被运输层归成 OUTCOME_UNKNOWN。
            code = "MODEL_PROFILE_NOT_FOUND" if getattr(exc, "reason", "") == "profile_not_found" else "MODEL_PROFILE_UNAVAILABLE"
            raise BusinessError(code) from exc
        return profile

    # LLM: 只在创建锁内已确认不存在的新行上调用；适配器可附加原安装出处，不授权修改既有线程。
    # 函数用途: 为新线程提供元数据扩展点；原生入口保持输入不变。
    def _thread_creation_metadata(self, *, actor, metadata: dict) -> dict:
        return metadata

    # LLM: 业务创建入口先校验请求、Owner和归属，再在配额及幂等锁内预留额度、原子建线程并发布索引；同键同请求复用，不启动模型。
    # 函数用途: 创建可接单的后台助手身份，拒绝幂等冲突、超额及无空间却指定项目的请求。
    def create(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
        request = _params("background_agents.create", request)
        source = self._actor(actor)
        self._space(request["space_id"], actor)
        if request["space_id"] is None and request.get("project_ref") is not None:
            raise ValueError("CONTEXT_REQUIRED")
        key = self._operation_key(actor, idempotency_key)
        identifier = "thread-bg-" + key[:32]
        fingerprint = _digest(request)
        quota = BackgroundAgentQuota(self)
        with quota.guard(), locked_file_transition(self.store.storage.threads_dir / (".background-create-" + key)):
            existing = self.store.threads.load(identifier)
            if existing is not None:
                creation = existing.metadata.get("background_creation")
                if existing.owner_id != actor.owner_id or creation != {"key": key, "fingerprint": fingerprint}:
                    raise BusinessError("IDEMPOTENCY_CONFLICT")
                self._thread(identifier, actor)
                quota.track(identifier)
                return {"agent": self._agent_view(existing), "revision": background_identity(existing).identity_revision}
            source_profile = str(getattr(source, "model_profile_id", "") or "").strip()
            explicit = bool(request.get("model_profile_id"))
            self._current_source_profile = source_profile
            try:
                try:
                    profile = self._profile(request.get("model_profile_id"), source_profile=source_profile)
                except TypeError:
                    profile = self._profile(request.get("model_profile_id"))
            finally:
                self._current_source_profile = ""
            selection_source = "explicit" if explicit else ("inherited" if (source_profile and profile == source_profile) else "default")
            identity = BackgroundAgentIdentity(identifier, source.thread_id, request["space_id"], request.get("project_ref"),
                                               "available", request.get("after_task", "reuse"), 1)
            current = time.time()
            metadata = self._thread_creation_metadata(actor=actor, metadata={
                BACKGROUND_IDENTITY_KEY: identity.to_dict(), "background_purpose": request.get("purpose", ""),
                "background_creation": {"key": key, "fingerprint": fingerprint}})
            quota.reserve(identifier)
            thread = create_thread_atomic(self.store.threads, ConversationThread(
                thread_id=identifier, canonical_user_id=source.canonical_user_id, owner_id=actor.owner_id,
                owner_home=str(self.host.home_paths.owner_home_dir), model_profile_id=profile,
                model_selection_revision=1, model_selection_source=selection_source,
                model_selection_last_explicit_revision=1 if explicit else 0,
                model_selection_selected_at=current, title=request["name"], created_at=current, updated_at=current,
                metadata=metadata,
            ))
            self._publish(thread)
            return {"agent": self._agent_view(thread), "revision": 1}

    # LLM: 可信Actor守Owner边界；读失败只降级观察，不创建任务或改变额度/身份。
    # 函数用途: 提供route与列表共用的真实身份配额摘要。
    def quota_summary(self, *, actor):
        self._actor(actor)
        quota = BackgroundAgentQuota(self)
        try:
            with quota.guard():
                return quota.snapshot()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            from ..settings.defaults import default_config_value

            limit = getattr(self.host.config, "max_background_agents", default_config_value("max_background_agents"))
            limit = limit if type(limit) is int and limit >= 0 else None
            return {"limit": limit, "used": None, "available": None, "unassigned": None,
                    "available_agents": None, "over_limit": None, "unlimited": limit == 0 if limit is not None else None,
                    "coverage": "unknown"}

    # LLM: 工作态核对同thread的claim、canonical任务和有界guidance报告；坏账或覆盖不足不假称idle。
    # 函数用途: 给身份DTO添加独立当前工作观察，available从不等价于running。
    def _agent_view(self, thread, *, guidance=None, task_page=None, execution_state=None):
        view = _agent_view(thread)
        view["work_state"] = "unknown"
        try:
            page = task_page if task_page is not None else self.store.session_tasks.list_page(
                target_thread_id=thread.thread_id, limit=100)
            if execution_state is None:
                execution, known_running, execution_error = self._execution_for_tasks(thread, page)
            else:
                execution, known_running, execution_error = execution_state
            if known_running:
                view["work_state"] = "running"
                return view
            if execution_error or page["load_errors"]:
                return view
            claim, error = self.store.claims.load_report(thread.thread_id)
            if error or (claim and claim.get("thread_id") != thread.thread_id):
                return view
            pending = [task for task in page["items"] if task.status not in SESSION_TASK_TERMINAL_STATUSES]
            if pending:
                view["work_state"] = "waiting"
            elif page["coverage"] == "complete" and page["next_cursor"] is None:
                inputs, errors = (guidance if guidance is not None
                                  else self.store.guidance.pending_report("thread", thread.thread_id, limit=100))
                if errors:
                    return view
                if inputs:
                    view["work_state"] = "waiting"
                elif page["items"]:
                    latest = max(page["items"], key=lambda task: task.updated_at)
                    view["work_state"] = "failed" if latest.status == "failed" else "idle"
                else:
                    view["work_state"] = "failed" if claim.get("status") == "failed" else "idle"
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            pass
        return view

    def _execution_for_tasks(self, thread, page):
        """已核验运行与剩余覆盖分别保留；单current字段不猜多运行的代表。"""
        from .background_execution_view import ExecutionViewError, thread_execution_view
        from .run_claim import detached_task_claim_scope_id

        incomplete = bool(page['load_errors'] or page['coverage'] != 'complete' or page['next_cursor'] is not None)
        executions, errors = {}, []
        def capture(read):
            try:
                execution = read()
            except ExecutionViewError as error:
                errors.append(error.code)
                return None
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
                errors.append('EXECUTION_AUTHORITY_UNAVAILABLE')
                return None
            if execution is not None:
                executions[(execution['run_id'], execution['attempt_id'])] = execution
            return execution

        def ordinary():
            claim, error = self.store.claims.load_report(thread.thread_id)
            if error or (claim and claim.get('thread_id') != thread.thread_id):
                raise ExecutionViewError('CLAIM_UNREADABLE')
            return self._execution_view(thread)
        capture(ordinary)
        runtime = getattr(getattr(self.host, 'subagents', None), 'runtime_db', None)
        services = getattr(self, 'services', None)
        if services is not None:
            runtime = getattr(services.observation, 'runtime', runtime)
        scopes = set()
        for task in page['items']:
            control = task.metadata.get('cancellation_control', {})
            owed = isinstance(control, dict) and bool(control.get('outstanding_refs'))
            if task.status in SESSION_TASK_TERMINAL_STATUSES and not owed:
                continue
            def detached():
                link, error = self.store.tasks.link_report(thread.thread_id, task.task_id)
                if error:
                    raise ExecutionViewError('TASK_LINK_UNREADABLE')
                if link is None or link.cancellation_scope != 'detached':
                    return None
                scope = detached_task_claim_scope_id(thread.thread_id, task.task_id)
                if scope in scopes:
                    return None
                scopes.add(scope)
                execution = thread_execution_view(self.store, runtime, thread, claim_scope_id=scope)
                if execution is not None and execution['task_id'] != task.task_id:
                    raise ExecutionViewError('EXECUTION_AUTHORITY_UNAVAILABLE')
                return execution
            capture(detached)
        known_running = bool(executions)
        if len(executions) > 1:
            return None, known_running, 'MULTIPLE_ACTIVE_EXECUTIONS'
        if errors:
            return None, known_running, errors[0]
        return next(iter(executions.values()), None), known_running, ('EXECUTION_SOURCE_INCOMPLETE' if incomplete else '')

    def _controller_id(self, actor):
        return "thread-controller-" + _digest([actor.owner_id, "business-controller.v1"])[:32]

    def get_controller(self, *, actor):
        self._actor(actor)
        identifier = self._controller_id(actor)
        thread = self.store.threads.load(identifier)
        if thread is not None and (thread.owner_id != actor.owner_id or thread.metadata.get("business_controller") is not True):
            raise BusinessError("CONTROLLER_IDENTITY_CONFLICT", details={
                "reason": "CONTROLLER_RECORD_IDENTITY_CONFLICT", "side_effect_state": "not_started",
                "next_action": "原总控记录未通过当前Owner与总控标记核验；请让宿主检查原记录，不覆盖或改用其它Owner。"})
        return {"agent_id": identifier, "thread_id": identifier, "available": thread is not None and thread.status == "active"}

    def ensure_controller(self, *, actor, expected_controller_id=None):
        source = self._actor(actor)
        identifier = self._controller_id(actor)
        with locked_file_transition(self.store.storage.threads_dir / ("." + identifier)):
            # inactive只是不可接单，不是不存在；仍按原校验拒绝身份冲突，不重写安装出处。
            current_controller = self.get_controller(actor=actor)
            if expected_controller_id is not None and (current_controller["agent_id"] != expected_controller_id
                    or current_controller["thread_id"] != identifier):
                raise BusinessError("CONTROLLER_IDENTITY_CONFLICT", details={
                    "reason": "CONTROLLER_IDENTITY_DRIFT", "side_effect_state": "not_started",
                    "next_action": "原Owner总控身份在提交前复核不一致；重新调用controllers.get核对，禁止按旧编号派发。"})
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

    # LLM: 供业务名册按Owner读取有界索引页并核空间访问；省略空间表示全部，显式None只选无归属，支持按名称过滤，返回真实分页覆盖与独立配额观察，不派活。
    # 函数用途: 分页列出符合空间、生命周期和名称筛选的后台助手及当前工作观察。
    def list(self, request: dict, *, actor) -> dict:
        name_filter = request.get("name") if isinstance(request, dict) else None
        cleaned = {k: v for k, v in request.items() if k != "name"} if isinstance(request, dict) and "name" in request else request
        params = _params("background_agents.list", cleaned)
        if name_filter:
            params["name"] = name_filter
        self._actor(actor)
        if params.get("space_id"):
            self._space(params["space_id"], actor)
        page = self._index().page(limit=params.get("limit", 50), cursor=params.get("cursor"), filter_key=actor.owner_id)
        candidate_threads = [ConversationThread.from_dict(payload) for payload in page["items"]]
        if not params.get("cursor"):
            seen_ids = {t.thread_id for t in candidate_threads}
            quota = BackgroundAgentQuota(self)
            try:
                with quota._connection() as conn:
                    rows = conn.execute("SELECT thread_id FROM active_refs WHERE owner_id=?", (actor.owner_id,)).fetchall()
                    active_ids = {r[0] for r in rows if r and r[0]}
            except Exception:
                active_ids = set()
            for tid in sorted(active_ids - seen_ids):
                path = self.store.storage.thread_path(tid)
                thread, error = _read_thread_record_report(path)
                if not error and thread is not None:
                    try:
                        self._publish(thread)
                    except Exception:
                        pass
                    candidate_threads.append(thread)
            candidate_threads.sort(key=lambda t: (-float(getattr(t, "updated_at", 0) or 0), getattr(t, "thread_id", "")))
        items = []
        for thread in candidate_threads:
            identity = background_identity(thread)
            if thread.owner_id != actor.owner_id or identity is None or ("space_id" in params and identity.space_id != params["space_id"]):
                continue
            if params.get("name") and thread.title != params["name"]:
                continue
            if params.get("lifecycle", "all") not in {"all", identity.identity_state}:
                continue
            self._space(identity.space_id, actor)
            items.append(self._agent_view(thread))
            if len(items) >= params.get("limit", 50):
                break
        return {"items": items, "next_cursor": page["next_cursor"],
                "coverage": "partial" if page["load_errors"] else page["coverage"],
                "quota": self.quota_summary(actor=actor)}

    # LLM: 业务详情入口先核目标Owner与空间，再读有界待办页和准确执行关联；执行观察异常仅降级为None，不创建任务或改写身份。
    # 函数用途: 查询一个后台助手的身份、待办及可核实的当前执行。
    def get(self, agent_id, *, actor):
        from .background_scheduling_view import model_readiness, scheduling_view

        thread = self._thread(agent_id, actor)
        resolved_id = thread.thread_id
        page = self.store.session_tasks.list_page(target_thread_id=resolved_id, limit=100)
        open_tasks = [task for task in page["items"] if task.status not in SESSION_TASK_TERMINAL_STATUSES]
        execution_error, known_running = "", False
        try:
            execution, known_running, execution_error = self._execution_for_tasks(thread, page)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
            execution = None
            from .background_execution_view import ExecutionViewError
            execution_error = exc.code if isinstance(exc, ExecutionViewError) else 'EXECUTION_AUTHORITY_UNAVAILABLE'
        guidance, pending_inputs = None, None
        if not open_tasks and not page["load_errors"]:
            try:
                guidance = self.store.guidance.pending_report("thread", thread.thread_id, limit=100)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                guidance = ([], [{"code": "GUIDANCE_READ_FAILED"}])
            pending_inputs = None if guidance[1] else bool(guidance[0])
        ready, reason = model_readiness(self.host, thread)
        complete = not page["load_errors"] and page["coverage"] == "complete" and page["next_cursor"] is None
        scheduling = scheduling_view(self.store, thread_id=thread.thread_id, tasks=open_tasks,
            identity_state=background_identity(thread).identity_state, running=execution is not None,
            model_ready=ready, model_reason=reason, pending_inputs=pending_inputs, tasks_complete=complete, runtime=getattr(getattr(self.host, "subagents", None), "runtime_db", None),
            owner_id=thread.owner_id)
        if execution_error and scheduling["state"] not in {"paused", "retiring", "retired"}:
            # 执行身份读失败不能冒充 no_work；保留失败码，不领取或消费任何工作。
            scheduling.update(reason_code=execution_error, coverage="partial")
            if execution is None and scheduling["state"] != "waiting_child":
                scheduling["state"] = "unknown"
        return {"agent": self._agent_view(thread, guidance=guidance, task_page=page,
                                          execution_state=(execution, known_running, execution_error)), "current_execution": execution,
                "pending_tasks": [self._task_view(task, execution) for task in open_tasks],
                "allowed_actions": ["background_agents.get", "background_agents.messages.send", "background_agents.suspend",
                                    "background_agents.resume", "background_agents.retire", "background_agents.model.set"],
                "scheduling": scheduling}

    def _execution_view(self, thread):
        from .background_execution_view import thread_execution_view

        runtime = getattr(getattr(self.host, "subagents", None), "runtime_db", None)
        return thread_execution_view(self.store, runtime, thread)

    # LLM: current_run_id 只取调用方已按 claim/运行库/canonical 任务复核过的准确执行，且其冻结 task_id 等于本任务；其它一律 None。
    #   result_refs 仅投影为公开登记 ID，绝不向外暴露宿主绝对路径。
    # 函数用途: 把 canonical 任务整理成冻结 TaskView，正在执行的任务关联到它的准确 run。
    def _task_view(self, task, execution=None):
        raw = task.metadata.get("business_context")
        context = BusinessExecutionContext.from_dict(raw) if raw else None
        run_id = execution.get("run_id") if isinstance(execution, dict) and execution.get("task_id") == task.task_id else None
        refs, _ = public_result_refs(task, store=self.store, owner_root=getattr(getattr(self.host, "home_paths", None), "owner_home_dir", None))
        return {"task_id": task.task_id, "thread_id": task.target_thread_id, "space_id": context.space_id if context else None,
                "project_ref": context.project_ref if context else None, "status": task.status,
                "current_run_id": run_id, "result_refs": refs, "revision": int(task.updated_at * 1000000)}

    # LLM: 更新、模型选择和生命周期共用此写入口；核目标范围后在身份锁及线程原子更新内处理CAS与幂等回执并发布索引，restore还须预留配额。
    # 函数用途: 按修订号安全修改后台身份或模型选择，同键重放不重复变更。
    def _mutate(self, agent_id, *, actor, action, changes, expected_revision, idempotency_key, model=False):
        thread = self._thread(agent_id, actor)
        resolved_id = thread.thread_id
        key = self._operation_key(actor, idempotency_key)
        fingerprint = _digest([action, changes, expected_revision])

        # LLM: 由线程update_atomic传入最新记录；核Owner、幂等指纹和对应修订，再形成模型或身份变更及有界回执，恢复身份时会写配额预留。
        # 函数用途: 计算一次受CAS约束的线程新值，冲突或非法生命周期转换立即拒绝。
        def apply(latest):
            if latest.owner_id != actor.owner_id:
                raise PermissionError("OWNER_MISMATCH")
            identity = background_identity(latest)
            receipts = dict(latest.metadata.get("background_receipts") or {})
            if key in receipts:
                if receipts[key] != fingerprint:
                    raise BusinessError("IDEMPOTENCY_CONFLICT")
                return latest
            revision = latest.model_selection_revision if model else identity.identity_revision
            if type(expected_revision) is not int or expected_revision != revision:
                raise BusinessError("REVISION_CONFLICT")
            if model:
                revision += 1
                updated = replace(latest, model_profile_id=changes["model_profile_id"],
                    model_selection_revision=revision, model_selection_source="explicit",
                    model_selection_last_explicit_revision=revision, provider_context_observation={}, model_context_usage={})
            else:
                state = identity.identity_state
                if action in {"resume", "restore"}:
                    if (action == "resume" and state != "suspended") or (action == "restore" and state != "retired"):
                        raise BusinessError("IDENTITY_TRANSITION_CONFLICT")
                    state = "available"
                elif action == "suspend":
                    if state not in {"available", "suspended"}:
                        raise BusinessError("IDENTITY_TRANSITION_CONFLICT")
                    state = "suspended"
                elif action == "retire":
                    state = "retiring" if state != "retired" else state
                elif state in {"retired", "retiring"}:
                    raise BusinessError("AGENT_NOT_AVAILABLE")
                if action == "restore":
                    BackgroundAgentQuota(self).reserve(resolved_id)
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

        with background_identity_transition(self.store, resolved_id):
            return self._publish(self.store.threads.update_atomic(resolved_id, apply))

    # LLM: 业务编辑入口校验update schema后委托_mutate完成范围、CAS和幂等写入；返回含独立工作观察的身份DTO及身份修订，不启动执行。
    # 函数用途: 保存后台助手的名称、用途等允许编辑的身份字段。
    def update(self, agent_id, changes: dict, *, actor, expected_revision, idempotency_key):
        thread = self._thread(agent_id, actor)
        resolved_id = thread.thread_id
        changes = _params("background_agents.update", {**changes, "agent_id": resolved_id})
        thread = self._mutate(resolved_id, actor=actor, action="update", changes=changes,
                              expected_revision=expected_revision, idempotency_key=idempotency_key)
        return {"agent": self._agent_view(thread), "revision": background_identity(thread).identity_revision}

    # LLM: 原准入锁、Owner/空间和身份CAS保持；动作产生的索引修订须重新完整核对，不能复用旧分页前缀。
    # 函数用途: 控制身份生命周期，按完整任务责任恢复唤醒或取消，并收口实际可完成的退休。
    def transition(self, agent_id, action, *, actor, expected_revision, idempotency_key,
                   disposition="drain", handoff_agent_id=None):
        source = self._thread(agent_id, actor)
        resolved_id = source.thread_id
        changes = {"agent_id": resolved_id}
        if action == "retire":
            changes["disposition"] = disposition
            if handoff_agent_id is not None:
                changes["handoff_agent_id"] = handoff_agent_id
                target = self._thread(handoff_agent_id, actor)
                if background_identity(source).space_id != background_identity(target).space_id or source.thread_id == target.thread_id:
                    raise BusinessError("SPACE_MISMATCH")
                # 不改写在途任务的冻结归属。交接是可观察的待处理义务，不复制任务到新空间。
        _params("background_agents." + action, changes)
        quota = BackgroundAgentQuota(self)
        with quota.guard() if action == "restore" else nullcontext():
            thread = self._mutate(resolved_id, actor=actor, action=action, changes=changes,
                                  expected_revision=expected_revision, idempotency_key=idempotency_key)
        refs, tasks = identity_outstanding(self.host, thread)
        if action == "retire":
            if disposition == "cancel_tasks":
                for task in tasks:
                    self.cancel_task(task.task_id, actor=actor, include_descendants=True)
            elif disposition == "drain":
                self._republish_pending_work(thread, tasks, actor=actor)
            thread = reconcile_background_identity(self.host, resolved_id)
            refs, _ = identity_outstanding(self.host, thread)
            if not refs and background_identity(thread).identity_state == "retiring":
                # 取消后的首个索引回填可能partial；上面的新完整遍历追平后，再走原锁内CAS，不把新空账配给旧身份。
                thread = reconcile_background_identity(self.host, resolved_id)
                if background_identity(thread).identity_state == "retiring":
                    refs, _ = identity_outstanding(self.host, thread)
            return {"agent": self._agent_view(thread), "operation_id": self._operation_key(actor, idempotency_key), "outstanding_refs": refs[:100]}
        if action in {"resume", "restore"}:
            self._republish_pending_work(thread, tasks, actor=actor)
        return {"agent": self._agent_view(thread), "operation_id": self._operation_key(actor, idempotency_key), "state": "completed"}

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

    # LLM: 恢复/退休drain共用原输入；原身份锁内先在所有task共享门外恢复索引，再只读一次claim，各任务同锁检查/发布。
    #   恢复失败或锁内责任报告未知均显式拒绝，已提交身份的同键重放仍会重试此义务；原queued正文、任务身份与去重证明不变。
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
            if tasks:
                self.store.wakes.recover_task_responsibility_index()
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
                    if error:
                        # 快查complete后另一重建可能已发表unknown；持共享门时只报错，绝不升级锁或假报恢复成功。
                        raise DataCorruptionError("task wake responsibility recovery is pending")
                    if live:
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
        resolved_id = thread.thread_id
        effective_at, _adoption_status = self._model_adoption(resolved_id)
        return {"model_profile_id": thread.model_profile_id, "selection_revision": thread.model_selection_revision,
                "source": {"kind": thread.model_selection_source or "default", "source_ref": thread.thread_id,
                           "revision": thread.model_selection_revision},
                "effective_at": effective_at}

    # LLM: 原权限、CAS和幂等写入保持；相同选择仍经过_mutate，提交后只返回冻结结果枚举。
    # 函数用途: 保存后台模型选择，按是否改变选择及真实执行边界返回采纳状态。
    def set_model(self, agent_id, model_profile_id, *, actor, expected_revision, idempotency_key):
        from ..user_space.group_authorization_context import group_capability_guard

        thread = self._thread(agent_id, actor)
        resolved_id = thread.thread_id
        _params("background_agents.model.set", {"agent_id": resolved_id, "model_profile_id": model_profile_id})
        self._profile(model_profile_id)
        with group_capability_guard(self.host, "assistant.configure", thread_id=resolved_id):
            previous = self._thread(resolved_id, actor)
            unchanged = previous.model_profile_id == model_profile_id
            thread = self._mutate(resolved_id, actor=actor, action="model.set", changes={"model_profile_id": model_profile_id},
                                  expected_revision=expected_revision, idempotency_key=idempotency_key, model=True)
        _effective_at, adoption_status = self._model_adoption(resolved_id)
        return {"selection_revision": thread.model_selection_revision,
                "adoption_status": "unchanged" if unchanged else adoption_status}

    def _dispatch(self, request, target, *, actor, idempotency_key, origin_task_id=""):
        with background_identity_transition(self.store, target.thread_id):
            target = self.store.threads.require(target.thread_id)
            if target.owner_id != actor.owner_id:
                raise PermissionError("OWNER_MISMATCH")
            return self._dispatch_locked(request, target, actor=actor, idempotency_key=idempotency_key,
                                         origin_task_id=origin_task_id)

    def _validate_dispatch_project(self, request, actor):
        """仅只读核显式项目；执行工厂仍须重核，不能以预检代替提交时权限。"""
        from .business_access import BusinessActor

        project = request.get("project_ref")
        if project is None:
            return
        self._actor(actor)
        self._space(request.get("space_id"), actor)
        catalog = getattr(self, "spaces", None) or getattr(self.host, "business_spaces_catalog", None)
        if catalog is None:
            from .business_spaces import BusinessSpaceService

            catalog = BusinessSpaceService(self.host.home_paths.owner_home_dir,
                owner_id=actor.owner_id,
                enabled=bool(getattr(self.host.config, "business_spaces_enabled", False)),
                reference_resolver=getattr(self.host, "business_space_reference_resolver", None))
        business_actor = getattr(self, "business_actor", None) or BusinessActor(
            actor.owner_id, actor.source_thread_id, catalog, lambda _a, _f: True)
        try:
            catalog.reference("project", project, actor=business_actor, space_id=request.get("space_id"))
        except BusinessError as error:
            raise BusinessError(error.code, details={**error.details,
                "field_path": "$.params.project_ref", "reference_kind": "project",
                "side_effect_state": "not_started", "task_created": False,
                "next_action": "project_ref只能填写本Owner预先存在的项目标识，不能填写workflow ID。"
                    "没有项目时省略project_ref；工作流编号放在任务instruction中。"
                    "修正后使用新request_id与幂等键提交一次，禁止重放此前未知的旧投递。"}) from error

    # LLM: 调用方_dispatch已持目标身份锁；复核Actor/空间及新任务接单和配对额度，沿原SessionTask派发写入冻结上下文、正文与唤醒，失败保留原outcome而不重派。
    # 函数用途: 将派活或后续请求幂等送入原任务通道，无归属任务不借用默认空间。
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
        if previous is None:
            self._validate_dispatch_project(request, actor)
        if previous is None and not background_identity_allows_work(target):
            raise BusinessError("AGENT_NOT_AVAILABLE")
        if previous is None and (failure := session_task_pair_limit_error(self.host, plan.sender_thread_id, plan.target_thread_id, config)):
            raise BusinessError(failure.error_code)
        asset_version_refs = tuple(request.get("asset_version_refs") or ())
        intent = BusinessContextIntent(actor.owner_id, request["space_id"], request.get("project_ref"), None,
                                       requested_asset_version_refs=asset_version_refs)

        # LLM: 仅由dispatch_session_task首次建canonical任务时传入准确task_id；只解析并校验上下文，None显式走无归属，重放不重算默认选择。
        # 函数用途: 为本次原生派活生成与准确任务绑定的冻结业务上下文。
        def factory(task_id):
            try:
                return resolve_business_context(actor=actor, task_id=task_id, requested_space_id=intent.requested_space_id,
                    project_ref=intent.project_ref, binding_id=intent.binding_id,
                    requested_method_refs=intent.requested_method_refs, requested_asset_version_refs=intent.requested_asset_version_refs,
                    unassigned=intent.requested_space_id is None, host=self.host)
            except Exception as e:
                from .business_access import BusinessActor, BusinessError
                from .business_spaces import BusinessSpaceService
                if not isinstance(e, BusinessError) or e.code != "ACTOR_REQUIRED":
                    raise
                catalog = getattr(self.host, "business_spaces_catalog", None)
                if catalog is None:
                    try:
                        catalog = BusinessSpaceService(self.host)
                    except TypeError:
                        owner_home = getattr(getattr(self.host, "home_paths", None), "owner_home_dir", None)
                        owner_id = str(getattr(getattr(self.host, "home_paths", None), "owner_id", "") or "")
                        enabled = bool(getattr(getattr(self.host, "agent_config", None), "business_spaces_enabled", False))
                        catalog = BusinessSpaceService(owner_home, owner_id=owner_id, enabled=enabled,
                            reference_resolver=getattr(self.host, "business_space_reference_resolver", None))
                biz_actor = BusinessActor(actor.owner_id, getattr(actor, "source_thread_id", str(actor)), catalog, lambda _a, _f: True)
                return resolve_business_context(actor=biz_actor, task_id=task_id, requested_space_id=intent.requested_space_id,
                    project_ref=intent.project_ref, binding_id=intent.binding_id,
                    requested_method_refs=intent.requested_method_refs, requested_asset_version_refs=intent.requested_asset_version_refs,
                    unassigned=intent.requested_space_id is None, host=self.host)

        from .execution_threads import EXECUTION_ORIGIN_KEY

        outcome = dispatch_session_task(self.host, plan, target, config, count_pair=previous is None,
            host_channel_actor=(actor if isinstance(target.metadata.get(EXECUTION_ORIGIN_KEY), dict)
                                and target.metadata[EXECUTION_ORIGIN_KEY].get('kind') == 'delegated' else None),
            business_context_intent=intent, business_context_factory=factory)
        if not outcome.ok:
            raise SessionTaskDispatchError(outcome)
        return json.loads(outcome.output)

    def assign(self, request: dict, *, actor, idempotency_key):
        request = _params("tasks.assign", request)
        target = self._thread(request["agent_id"], actor)
        identity = background_identity(target)
        # 先由_thread核验目标Owner和空间访问，再公开该已授权身份的绑定；不拿参数猜归属。
        for field, expected in (("space_id", identity.space_id), ("project_ref", identity.project_ref)):
            actual = request.get(field)
            if expected != actual:
                correction = ("省略project_ref（或传null）" if field == "project_ref" and expected is None
                              else f"使用background_agents.get返回的{field}")
                raise BusinessError("SPACE_MISMATCH", details={
                    "reason": "BACKGROUND_ASSIGNMENT_SCOPE_CONFLICT", "field_path": f"$.params.{field}",
                    "actual": actual, "expected": expected, "side_effect_state": "not_started",
                    "task_created": False, "retry_same_arguments": False,
                    "next_action": f"目标后台身份已绑定范围，不能用任务标记替代；{correction}，保留其它绑定字段。"
                        "修正后用新request_id和新幂等键提交一次；不要重建身份或更换目标。",
                    "read_example": '{"action":"background_agents.get","params":{"agent_id":"<已选后台编号>"},'
                        '"request_id":"check-selected-background"}',
                })
        if not background_identity_allows_work(target):
            raise BusinessError("AGENT_NOT_AVAILABLE")
        if request.get("origin_task_id"):
            self._owned_task(request["origin_task_id"], actor)
        result = self._dispatch(request, target, actor=actor, idempotency_key=idempotency_key,
                                origin_task_id=request.get("origin_task_id", ""))
        return {"assignment_ref": result["task_id"], "task_id": result["task_id"], "receipt_state": "accepted"}

    # LLM: 提交业务空间后台任务；核验所引用的业务资产必须属于目标空间且具备正式 accepted_version，候选版本拒绝，冻结选定版本。
    # 函数用途: 向总控提交空间任务，并冻结经核验的资产接受版本。
    def submit(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
        request = _params("tasks.submit", request)
        try:
            self._actor(actor)
            self._space(request["space_id"], actor)
        except PermissionError as error:
            raise BusinessError("BUSINESS_ACCESS_DENIED", details={
                "reason": "SUBMISSION_SCOPE_DENIED", "side_effect_state": "not_started",
                "next_action": "当前Owner、来源会话或空间未获准；从原已授权会话核对空间，不改写身份或跨Owner重试。"}) from error
        controller = self.get_controller(actor=actor)
        expected_id = controller["agent_id"]
        if request.get("controller_agent_id", expected_id) != expected_id:
            raise BusinessError("CONTROLLER_IDENTITY_CONFLICT", details={
                "field_path": "$.params.controller_agent_id", "actual": request["controller_agent_id"],
                "expected": expected_id, "reason": "CONTROLLER_AGENT_ID_MISMATCH",
                "side_effect_state": "not_started", "controller_created": False, "task_created": False,
                "retry_same_arguments": False,
                "next_action": "在原已授权会话调用controllers.get，取data.agent_id；不要传普通后台助手编号。"
                    "也可省略controller_agent_id，由宿主选择本Owner总控。修正后用新request_id和新幂等键提交一次。",
                "read_example": '{"action":"controllers.get","params":{},"request_id":"check-controller"}'})
        frozen_version_refs = []
        if request.get("asset_refs"):
            catalog = getattr(self.host, "business_spaces_catalog", None)
            if catalog is not None and hasattr(catalog, "_read"):
                space_service = catalog
            else:
                from .business_spaces import BusinessSpaceService
                try:
                    space_service = BusinessSpaceService(self.host)
                except TypeError:
                    owner_home = getattr(getattr(self.host, "home_paths", None), "owner_home_dir", None)
                    owner_id = str(getattr(getattr(self.host, "home_paths", None), "owner_id", "") or "")
                    enabled = bool(getattr(getattr(self.host, "agent_config", None), "business_spaces_enabled", False))
                    space_service = BusinessSpaceService(owner_home, owner_id=owner_id, enabled=enabled)
            space_state = space_service._read() if callable(getattr(space_service, "_read", None)) else {}
            assets_catalog = space_state.get("assets", {}) if isinstance(space_state, dict) else {}
            req_space_id = request.get("space_id")
            for ref in request["asset_refs"]:
                asset_id = ref.get("artifact_id") or ref.get("asset_id") if isinstance(ref, dict) else str(ref)
                asset_row = assets_catalog.get(asset_id)
                if asset_row is None:
                    raise BusinessError("REFERENCE_NOT_FOUND")
                if req_space_id is not None and asset_row.get("space_id") != req_space_id:
                    raise BusinessError("SPACE_MISMATCH")
                accepted_ver = asset_row.get("accepted_version")
                if not accepted_ver:
                    # 仅候选版本，尚未接受为正式版本，拒绝 409
                    raise BusinessError("STATE_CONFLICT")
                if isinstance(ref, dict) and ref.get("version_ref") and ref["version_ref"] != accepted_ver:
                    raise BusinessError("STATE_CONFLICT")
                frozen_version_refs.append(str(accepted_ver))
        # 已受理任务必须沿原冻结context重放，不能因当前项目变化重选上下文。
        from .session_task_dispatch import _task_dedupe_key
        replay_plan = SessionTaskDispatchPlan(actor.source_thread_id, expected_id, request["instruction"],
            "", self._operation_key(actor, idempotency_key))
        if self.store.session_tasks.find_by_dedupe_key(_task_dedupe_key(replay_plan)) is None:
            self._validate_dispatch_project(request, actor)
        prepared = self.ensure_controller(actor=actor, expected_controller_id=expected_id)
        identifier = prepared["thread_id"]
        dispatch_req = dict(request)
        if frozen_version_refs:
            dispatch_req["asset_version_refs"] = tuple(frozen_version_refs)
        try:
            result = self._dispatch(dispatch_req, self.store.threads.require(identifier), actor=actor, idempotency_key=idempotency_key)
        except BusinessError as error:
            if not prepared["created"]:
                raise
            if error.effect_outcome != "not_started":
                error.details = {**error.details, "controller_created": True,
                    "task_dispatch_outcome": error.effect_outcome,
                    "next_action": "总控已创建，原任务提交已发生或结果未知；仅按原请求与幂等键核对原账，禁止换键重派。"}
                raise
            # 此类型的not_started仅来自派发调用前检查；canonical创建后的异常由原派发口包装为unknown。
            raise BusinessError(error.code, effect_outcome="committed", details={**error.details,
                "controller_created": True, "task_created": False,
                "side_effect_state": "controller_created_task_not_started",
                "next_action": "总控已创建，但任务未创建。按原错误核对空间、身份或配额；原总控不需重建，"
                    "修正后以新的request_id与幂等键提交一次。"}) from error
        except SessionTaskDispatchError as error:
            if prepared["created"]:
                if error.effect_outcome == "not_started":
                    raise BusinessError(error.error_code, effect_outcome="committed", details={
                        "controller_created": True, "task_created": False,
                        "task_dispatch_outcome": "not_started", "side_effect_state": "controller_created_task_not_started",
                        "next_action": "总控已创建，原派发口确认任务未创建；核对原派发限制后以新请求和幂等键提交，勿重建总控。"}) from error
                error.details = {**getattr(error, "details", {}), "controller_created": True, "task_dispatch_outcome": "unknown",
                    "side_effect_state": "controller_created_task_unknown",
                    "next_action": "总控已创建，任务是否入队未知；仅用原request_id和幂等键核对原操作与任务，禁止改参数或换键重派。"}
            raise
        return {"task_id": result["task_id"], "request_id": result["task_id"],
                "operation_id": result["task_id"], "state": "accepted"}

    def _owned_task(self, task_id, actor):
        self._actor(actor)
        try:
            task = self.store.session_tasks.require(task_id)
            threads = [self.store.threads.require(identifier)
                       for identifier in (task.sender_thread_id, task.target_thread_id)]
        except (FileNotFoundError, KeyError, ValueError, DataCorruptionError) as error:
            raise BusinessError("RESOURCE_NOT_FOUND", effect_outcome="not_started", details={
                "reason": "TASK_OR_THREAD_SOURCE_UNAVAILABLE", "exception_type": type(error).__name__,
                "side_effect_state": "not_started", "task_created": False,
                "next_action": "调用tasks.list核对当前Owner可见的原任务，不猜测他人的任务编号。"}) from None
        if any(thread.owner_id != actor.owner_id for thread in threads):
            raise BusinessError("RESOURCE_NOT_FOUND", effect_outcome="not_started", details={
                "reason": "TASK_THREAD_OWNER_MISMATCH", "side_effect_state": "not_started", "task_created": False,
                "next_action": "仅从当前Owner的原发送会话跟进其可见任务。"})
        raw_context = task.metadata.get("business_context")
        if raw_context is None:
            self._owned_unassigned_task(task, actor)
            return task, None
        context = BusinessExecutionContext.from_dict(raw_context)
        if context.owner_id != actor.owner_id or context.task_id != task.task_id:
            raise PermissionError("OWNER_MISMATCH")
        if context.space_id is None and getattr(getattr(self, 'call_context', None), 'selected_space_id', None) is not None:
            raise BusinessError("SPACE_SCOPE_MISMATCH", effect_outcome="not_started", details={
                "reason": "UNASSIGNED_TASK_SPACE_CONFLICT", "side_effect_state": "not_started", "task_created": False,
                "next_action": "该原任务无空间；保持无空间跟进，不选择其它业务空间。"})
        self._space(context.space_id, actor)
        return task, context

    def _owned_unassigned_task(self, task, actor):
        """历史普通SessionTask只沿原Owner/来源/任务谱系授权，不补业务空间。"""
        from pathlib import Path

        from .channels import supports_transcript_delivery
        from .execution_threads import EXECUTION_ORIGIN_KEY
        from .host_actor import HostActorError, _validate_scope

        def reject(reason, code="RESOURCE_NOT_FOUND"):
            raise BusinessError(code, effect_outcome="not_started", details={
                "reason": reason, "side_effect_state": "not_started", "task_created": False,
                "next_action": "从原任务发送会话调用tasks.get核对原任务及来源；无空间任务不得指定空间，未核清前不要重派。"})

        if getattr(getattr(self, 'call_context', None), 'selected_space_id', None) is not None:
            reject("UNASSIGNED_TASK_SPACE_CONFLICT", "SPACE_SCOPE_MISMATCH")
        if actor.source_thread_id != task.sender_thread_id:
            reject("UNASSIGNED_TASK_SOURCE_MISMATCH", "CAPABILITY_DENIED")
        home = Path(self.host.home_paths.owner_home_dir).resolve()
        current, seen = task, set()
        for _ in range(64):
            if current.task_id in seen:
                reject("UNASSIGNED_TASK_LINEAGE_CYCLE")
            seen.add(current.task_id)
            if (current.sender_thread_id != task.sender_thread_id
                    or current.target_thread_id != task.target_thread_id):
                reject("UNASSIGNED_TASK_LINEAGE_MISMATCH")
            for identifier in (current.sender_thread_id, current.target_thread_id):
                thread = self.store.threads.require(identifier)
                if thread.owner_id != actor.owner_id or Path(thread.owner_home).resolve() != home:
                    reject("UNASSIGNED_TASK_OWNER_HOME_MISMATCH")
            context = current.metadata.get('business_context')
            if context is not None:
                try:
                    frozen = BusinessExecutionContext.from_dict(context)
                except (ValueError, TypeError, KeyError):
                    reject("UNASSIGNED_TASK_LINEAGE_CONTEXT_INVALID")
                if frozen.owner_id != actor.owner_id or frozen.task_id != current.task_id or frozen.space_id is not None:
                    reject("UNASSIGNED_TASK_LINEAGE_SPACE_MISMATCH", "SPACE_SCOPE_MISMATCH")
            if not current.origin_task_id:
                break
            current = self.store.session_tasks.load(current.origin_task_id)
            if current is None:
                reject("UNASSIGNED_TASK_LINEAGE_NOT_FOUND")
        else:
            reject("UNASSIGNED_TASK_LINEAGE_LIMIT")
        target = self.store.threads.require(task.target_thread_id)
        identity = background_identity(target)
        if identity is not None and (identity.space_id is not None or identity.project_ref is not None):
            reject("UNASSIGNED_TASK_TARGET_SPACE_MISMATCH", "SPACE_SCOPE_MISMATCH")
        if EXECUTION_ORIGIN_KEY in target.metadata or not supports_transcript_delivery(actor.channel):
            try:
                _validate_scope(self.host, target.thread_id, actor)
            except (HostActorError, DataCorruptionError, KeyError, ValueError) as error:
                reject(getattr(error, 'error_code', 'UNASSIGNED_TASK_ORIGIN_INVALID'), "CAPABILITY_DENIED")

    def follow_up(self, request: dict, *, actor, idempotency_key):
        request = _params("tasks.follow_up", request)
        task, context = self._owned_task(request["task_id"], actor)
        target = self.store.threads.require(task.target_thread_id)
        from .execution_threads import IM_TASK_EXECUTION_KEY, ensure_execution_thread

        if context is None and IM_TASK_EXECUTION_KEY in target.metadata:
            # 原自动委派线程只绑定原task；追问是新普通工作，不改绑原线程也不自动再派孩子。
            target = ensure_execution_thread(self.host, task.sender_thread_id,
                "task-follow-up:" + self._operation_key(actor, idempotency_key), kind="delegated",
                source_binding=actor.binding())
        result = self._dispatch({"space_id": context.space_id if context is not None else None,
                                 "project_ref": context.project_ref if context is not None else None,
                                 "instruction": request["instruction"]}, target,
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
