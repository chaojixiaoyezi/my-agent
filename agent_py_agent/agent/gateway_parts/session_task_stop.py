# LLM: 会话间派活的取消要"真的叫停目标上正在执行的回合"，这里复用 /stop 的那条结构化控制通道
#   （gateway 控制服务按精确 expected_turn_id 选回合），所以只停这个任务对应的那一轮，不会停目标会话里别的工作。
#   停止是否生效只认控制结果：未确认时调用方不得声称"已停止"。目标渠道身份缺失、Gateway 路径不可用时一律
#   返回未确认（fail closed），不猜回合、不猜渠道。
# 模块用途: 让会话任务取消工具从目标会话外部请求它停止正在执行的那一轮。
from __future__ import annotations

from dataclasses import dataclass

from ..conversation.control_commands import ConversationControlCommand
from .control_service import GatewayControlScope, execute_gateway_conversation_control
from .paths import gateway_paths


# LLM: confirmed 只表示控制服务已受理并标记了这个精确回合；message 是控制服务的原话，调用方据此如实回报。
# 类用途: 保存一次"停止某条会话任务所在回合"的实际结果。
@dataclass(frozen=True)
class SessionTaskStopOutcome:
    confirmed: bool
    message: str
    request_id: str = ""
    error_code: str = ""


# LLM: 只按结构化身份构造作用域：渠道、会话、渠道用户来自目标会话的 channel_binding，回合用 expected_turn_id
#   精确绑定。任何一步拿不到结构化事实都返回未确认，绝不退化成"停掉目标会话当前任意回合"。
# 函数用途: 请求停止一条会话任务绑定的那个目标回合。
def stop_session_task_turn(agent: object, *, target_thread: object, turn_id: str,
                           canonical_task=None) -> SessionTaskStopOutcome:
    request_id = str(turn_id or "").strip()
    if not request_id:
        return SessionTaskStopOutcome(
            False, "任务还没有绑定到执行中的回合，未发送停止控制。"
        )
    from ..conversation.execution_threads import IM_TASK_EXECUTION_KEY

    if target_thread is not None and IM_TASK_EXECUTION_KEY in target_thread.metadata:
        return _stop_delegated_task(agent, target_thread, request_id)
    if canonical_task is not None and canonical_task.metadata.get("business_context") is not None:
        try:
            current = agent.conversation_store.session_tasks.require(canonical_task.task_id)
            if (current.target_thread_id != target_thread.thread_id or current.conversation_request_id != request_id
                    or _business_task_binding(current) != _business_task_binding(canonical_task)):
                raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
            confirmed = stop_business_session_task(agent, current)
            return SessionTaskStopOutcome(confirmed, "原业务执行已停止。" if confirmed else "原业务执行或资源退出未确认。", request_id)
        except (OSError, ValueError, RuntimeError, PermissionError):
            return SessionTaskStopOutcome(False, "原业务执行停止未确认。", request_id, "SESSION_TASK_STOP_UNCONFIRMED")
    binding = _primary_binding(target_thread)
    if binding is None:
        return SessionTaskStopOutcome(
            False, "目标会话没有可用的渠道身份，未发送停止控制。", request_id=request_id
        )
    try:
        paths = gateway_paths(agent)
    except Exception as exc:  # noqa: BLE001 - 拿不到 Gateway 路径就不能确认停止
        return SessionTaskStopOutcome(
            False,
            "当前运行环境没有 Gateway 队列，停止控制未发送。",
            request_id=request_id,
            error_code=_unconfirmed_code(exc),
        )
    scope = GatewayControlScope(
        user_id=str(binding.channel_user_id or binding.canonical_user_id or "").strip(),
        channel=str(binding.channel or "").strip(),
        conversation_id=str(binding.channel_conversation_id or "").strip(),
        metadata={"expected_turn_id": request_id},
    )
    if not (scope.user_id and scope.channel and scope.conversation_id):
        return SessionTaskStopOutcome(
            False, "目标会话的渠道身份不完整，未发送停止控制。", request_id=request_id
        )
    try:
        result = execute_gateway_conversation_control(
            agent, paths, ConversationControlCommand("stop"), scope
        )
    except Exception as exc:  # noqa: BLE001 - 异常不等于目标已停止
        return SessionTaskStopOutcome(
            False,
            "停止控制执行失败；目标是否已停止未确认。",
            request_id=request_id,
            error_code=_unconfirmed_code(exc),
        )
    return SessionTaskStopOutcome(
        bool(getattr(result, "ok", False)),
        str(getattr(result, "message", "") or ""),
        request_id=str(getattr(result, "request_id", "") or request_id),
        error_code=str(getattr(result, "error_code", "") or ""),
    )


# LLM: 目标 thread 的第一个渠道绑定就是控制作用的会话身份；没有绑定就没有可确认的目标身份。
# 函数用途: 取目标会话的渠道绑定。
def _business_task_binding(task):
    return (task.task_id, task.sender_thread_id, task.target_thread_id, task.conversation_request_id,
            task.task_run_id, task.metadata.get("business_context"))


# LLM: 只消费原canonical/SQLite/claim；独立执行入口必须提供原ref，任务入口只从其原claim或取消账取ref。
# 函数用途: 验证同一owner、任务、线程、主run、attempt与claim，不选择任意current任务。
def _business_stop_target(agent, task, execution_ref=None):
    from ..conversation.business_contracts import BusinessExecutionContext, HostParentExecutionRef

    store, owner = agent.conversation_store, str(agent.home_paths.owner_id)
    current = store.session_tasks.require(task.task_id)
    if _business_task_binding(current) != _business_task_binding(task):
        raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
    context = BusinessExecutionContext.from_dict(current.metadata.get("business_context"))
    if context.owner_id != owner or context.task_id != current.task_id:
        raise PermissionError("OWNER_MISMATCH")
    for identifier in (current.sender_thread_id, current.target_thread_id):
        if store.threads.require(identifier).owner_id != owner:
            raise PermissionError("OWNER_MISMATCH")
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not current.task_run_id:
        raise ValueError("EXECUTION_AUTHORITY_REQUIRED")
    claim, error = store.claims.load_report(current.target_thread_id)
    if error:
        raise ValueError("CLAIM_UNREADABLE")
    control = current.metadata.get("cancellation_control", {})
    raw = control.get("execution_ref")
    authority = claim.get("runtime_authority") or {}
    if execution_ref is None:
        execution_ref = (HostParentExecutionRef.from_dict(raw) if raw else repo.parent_execution_ref(
            owner_id=owner, run_id=authority.get("run_id", ""), attempt_id=authority.get("attempt_id", "")))
    ref = repo.parent_execution_ref(owner_id=owner, run_id=execution_ref.run_id, attempt_id=execution_ref.observed_attempt_id)
    if ref != execution_ref or ref.owner_id != owner or current.task_run_id != ref.task_run_id or not ref.observed_attempt_id:
        raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
    if raw is not None and raw != ref.to_dict():
        raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
    observed = repo.business_context_for_execution(owner_id=owner, run_id=ref.run_id, attempt_id=ref.observed_attempt_id)
    run, task_run = repo.get_agent_run(ref.agent_run_id), repo.get_task_run(ref.task_run_id)
    if observed != context or run["role"] not in {"main", ""} or run["parent_agent_run_id"]:
        raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
    root_id = str(task_run["task_id"])
    expected = {"schema_version": "conversation_claim_execution.v1", "claim_id": claim.get("claim_id"),
                "task_id": root_id, "run_id": ref.run_id, "agent_run_id": ref.agent_run_id,
                "attempt_id": ref.observed_attempt_id, "invocation_run_id": current.conversation_request_id}
    matches = (bool(current.conversation_request_id) and claim.get("schema_version") == "background_run_claim.v1"
               and claim.get("thread_id") == current.target_thread_id and claim.get("claim_scope_id") == current.target_thread_id
               and bool(claim.get("claim_id")) and authority == expected)
    claim_ref = control.get("claim_ref")
    if claim_ref is not None:
        matches = matches and claim_ref == {"claim_id": claim.get("claim_id"), "authority": authority,
                                           "task_id": claim.get("task_id")}
    elif matches:
        claim_ref = {"claim_id": claim["claim_id"], "authority": authority, "task_id": claim.get("task_id")}
    return repo, current, ref, root_id, claim, claim_ref, matches


def _original_business_claim_finished(task, claim, matches):
    # 后继领取保留的previous_claim是原ClaimStore写入的有限终态证据；到期接管不是退出。
    control = task.metadata.get("cancellation_control", {})
    original = control.get("claim_ref") or {}
    if not original.get("claim_id"):
        return False
    if control.get("claim_finished") is True:
        return True
    if matches:
        return claim.get("status") in {"finished", "failed", "cancelled"} and bool(claim.get("finished_at"))
    previous = claim.get("previous_claim") or {}
    return (previous.get("claim_id") == original["claim_id"]
            and previous.get("task_id") == original.get("task_id")
            and previous.get("status") in {"finished", "failed", "cancelled"})


# LLM: 注册表保留的回调只含原会话存储定位与不可变任务/执行身份，不持池化宿主、过期租约或可扩大选择的查询；
#   以只读布局打开原存储，不为已消失的owner重建目录。选择清单尚未定稿时返回False保留原证明给正常对账重试；
#   原任务已不存在或定稿清单不含该资源时无须保存；写入仍由原canonical写者核CAS与身份。
# 类用途: 把PTY注册表交来的准确清理证明写入原SessionTask取消账。
@dataclass(frozen=True)
class _CleanupProofConsumer:
    storage_root: str
    task_id: str
    execution_ref: object

    def __call__(self, proof: dict) -> bool:
        from ..conversation.session_tasks import SessionTaskStore
        from ..conversation.store_layout import ConversationStorage

        tasks = SessionTaskStore(ConversationStorage(self.storage_root, initialize=False))
        current = tasks.load(self.task_id)
        if current is None:
            return True
        control = current.metadata.get("cancellation_control", {})
        selected = [item for item in control.get("resource_refs", [])
                    if item.get("kind") == "pty" and item.get("id") == proof.get("session_id")]
        if not selected:
            return control.get("resources_prepared") is True
        tasks.record_resource_cleanup_proof(self.task_id, execution_ref=self.execution_ref,
                                            resource_ref=selected[0], proof=proof)
        return True


# LLM: 在宿主对账线程（不持注册表/关闭/进程锁）重试保留原证明的检查点；不依赖活跃PTY、巡检线程或新启动。
# 函数用途: 让已选PTY的原清理证明在正常取消对账中持久化，返回是否有证明就绪需要重读原任务。
def _retry_cleanup_proof_checkpoints(task) -> bool:
    from ..tooling.pty_sessions import pty_session_registry

    refs = task.metadata.get("cancellation_control", {}).get("resource_refs")
    ids = [item["id"] for item in refs if item.get("kind") == "pty"] if isinstance(refs, list) else []
    return bool(ids and pty_session_registry.checkpoint_cleanup_proofs(ids))


def _business_resource_references(frozen, scope):
    # 只运输冻结批次的原身份；状态仍由ProcessSessionStore、PTY注册表与RuntimeDB负责。
    from dataclasses import asdict

    from ..tooling.pty_sessions import _session_in_scope, pty_session_registry

    refs = []

    def processes(batch, scope):
        if batch is None:
            return
        keys = ("schema", "session_id", "access_scope", "execution_scope", "reserved_at")
        for record in batch.receipt.records:
            refs.append({"kind": "background", "id": record["session_id"],
                         "identity": {key: record.get(key) for key in keys}})
        for identifier in batch.pty_session_ids:
            session = _session_in_scope(pty_session_registry, identifier, None)
            refs.append({"kind": "pty", "id": identifier, "scope": scope,
                         "identity": ({"pid": session.process.pid, "started_at": session.started_at,
                                       "scope": asdict(session.execution_scope)} if session is not None else None)})

    for stop in frozen.children.stops:
        binding = {key: stop.report["runtime_authority"].get(key) for key in
                   ("task_id", "run_id", "agent_run_id", "attempt_id")}
        refs.append({"kind": "execution", "id": f'{binding["run_id"]}:{binding["attempt_id"]}',
                     "binding": binding, "runner": asdict(stop.runner)})
        processes(stop.resources, {"owner_home": scope.owner_home, "run_id": binding["run_id"]})
    processes(frozen.main, asdict(scope))
    return refs


def _attempt_physical_operations_exited(repo, attempt_id, agent_run_id):
    """业务UNKNOWN不等于物理执行仍活跃；只消费原host已落账的准确进程清理回执。"""
    import json

    for operation in repo.unsettled_attempt_operations(attempt_id):
        if operation["status"] != "UNKNOWN":
            return False
        row = repo.get_operation(operation["operation_id"])
        if (row is None or row["attempt_id"] != attempt_id or row["agent_run_id"] != agent_run_id
                or row["status"] != "UNKNOWN"):
            return False
        try:
            result = json.loads(row["outcome_json"])["result"]
            process = result["result_envelope"]["process"]
            termination = process["termination"]
            if (result.get("handler_executed") is not True
                    or process.get("cleanup_schema") != "foreground-process-cleanup.v1"
                    or process.get("status") != "cancelled" or process.get("cleanup_confirmed") is not True
                    or type(process.get("pid")) is not int or process["pid"] <= 0
                    or not isinstance(process.get("birth_token"), str) or not process["birth_token"]
                    or termination.get("confirmed") is not True or termination.get("unresolved_pids") != []
                    or type(termination.get("observed_processes")) is not int
                    or termination["observed_processes"] < 1):
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
    return True


def _business_resources_exited(agent, repo, task):
    # 零等待读取原资源；已请求、父进程poll、children.ok或租约到期都不是完整退出证明。
    from dataclasses import asdict

    from ..concurrency.interrupt import is_interruptible_registered
    from ..contracts.model_call_ledger import model_call_drain_snapshot
    from ..conversation.session_tasks import cleanup_frozen_runner_stop
    from ..tooling.process_cleanup_evidence import process_cleanup_reference
    from ..tooling.process_scope import ProcessExecutionScope
    from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root
    from ..tooling.pty_sessions import (
        _session_in_scope,
        _source_resource_retired,
        pty_session_registry,
    )

    control = task.metadata.get("cancellation_control", {})
    if control.get("resources_prepared") is not True or not isinstance(control.get("resource_refs"), list):
        return False
    # 只认原canonical写者锁内按准确已选引用核准后追加的证明；它在内存有界历史淘汰原对象后仍是同一退出证据。
    proven = [item.get("resource_ref") for item in control.get("resource_cleanup_proofs", []) if isinstance(item, dict)]
    home = str(agent.home_paths.owner_home_dir)
    store = ProcessSessionStore(process_session_store_root(home, home))
    for reference in control.get("resource_refs", []):
        if reference["kind"] == "background":
            observed = store.load(reference["id"])
            if observed.load_error or not observed.record or any(observed.record.get(key) != value
                    for key, value in reference["identity"].items()):
                return False
            try:
                process_cleanup_reference(observed.record)
            except ValueError:
                return False
        elif reference["kind"] == "pty":
            if reference in proven:
                continue
            session = _session_in_scope(pty_session_registry, reference["id"], None)
            if session is None or not session.execution_scope.matches(ProcessExecutionScope(**reference["scope"])):
                return False
            identity = reference["identity"]
            if identity is not None and identity != {"pid": session.process.pid, "started_at": session.started_at,
                                                      "scope": asdict(session.execution_scope)}:
                return False
            if not _source_resource_retired(session):
                return False
        elif reference["kind"] == "execution":
            binding = reference["binding"]
            attempt, run = repo.get_attempt(binding["attempt_id"]), repo.get_agent_run(binding["agent_run_id"])
            if (attempt is None or run is None or run["run_id"] != binding["run_id"]
                    or attempt["agent_run_id"] != binding["agent_run_id"]
                    or run["current_attempt_id"] != binding["attempt_id"]
                    or attempt["status"] not in {"done", "failed", "cancelled", "stopped"} or not attempt["ended_at"]
                    or repo.get_agent_run_exec_lock(binding["agent_run_id"]) is not None
                    or not _attempt_physical_operations_exited(repo, binding["attempt_id"], binding["agent_run_id"])
                    or is_interruptible_registered(f'subagent-runner-attempt:{binding["run_id"]}:{binding["attempt_id"]}')
                    or any(record.run_id == binding["run_id"] and record.metadata.get("attempt_id") == binding["attempt_id"]
                           for _ledger, record, _retained in model_call_drain_snapshot())):
                return False
            runner = cleanup_frozen_runner_stop(reference["runner"])
            if runner.get("status") != "no_pid" and runner.get("host_exited") is not True:
                return False
        else:
            return False
    return True


# LLM: 终态/过期租约不等于退出；原claim finally、准确attempt及操作/锁/模型保留和资源回执都必须已核收。
# 函数用途: 只读判断原主执行已经收尾，未知或换代始终保留取消责任。
def _business_execution_exited(agent, repo, task, ref, claim, matches):
    from ..concurrency.interrupt import is_interruptible_registered
    from ..contracts.model_call_ledger import model_call_drain_snapshot
    from ..conversation.run_claim import conversation_claim_interrupt_name

    if task.metadata.get("cancellation_control", {}).get("model_worker_exit_unknown"):
        # 旧的未跟踪 worker 没有原物理身份；当前账本为空也不能替旧未知补签退出。
        return False
    if not _original_business_claim_finished(task, claim, matches):
        return False
    original = task.metadata["cancellation_control"]["claim_ref"]
    if is_interruptible_registered(conversation_claim_interrupt_name(original["claim_id"])):
        return False
    attempt, run = repo.get_attempt(ref.observed_attempt_id), repo.get_agent_run(ref.agent_run_id)
    if (attempt["status"] not in {"done", "failed", "cancelled", "stopped"} or not attempt["ended_at"]
            or run["status"] not in {"done", "failed", "cancelled", "stopped"}
            or repo.get_agent_run_exec_lock(ref.agent_run_id) is not None
            or repo.unsettled_attempt_operations(ref.observed_attempt_id)):
        return False
    return not any(record.run_id == ref.run_id and record.metadata.get("attempt_id") == ref.observed_attempt_id
                   for _ledger, record, _retained in model_call_drain_snapshot())


# LLM: 先可选 CG 再 task→claim（MP-00），内层 CG 重入且无 guard 仍 nullcontext；中断回调及资源清理在门外，原取消账保全部未知。
# 函数用途: 停止一个已核准的业务主执行，原权限立即关闭，实际退出后同操作解除责任。
def stop_business_session_task(agent, task, *, execution_ref=None, include_descendants=True, cancel_task=True):
    from contextlib import nullcontext

    from ..concurrency.interrupt import interrupt_by_name
    from ..conversation.run_claim import conversation_claim_interrupt_name
    from ..conversation.session_tasks import freeze_main_only_task_resources
    from ..conversation.task_resources import cleanup_task_resources, freeze_task_resources
    from ..runtime_db.run_cancellation import RuntimeCancellationTarget, cancel_runtime_run
    from ..tooling.process_scope import ProcessExecutionScope
    from ..tooling.pty_sessions import bind_cleanup_proof_consumer
    from .io import locked_file_transition

    store = agent.conversation_store
    repo, task, ref, root_id, claim, claim_ref, matches = _business_stop_target(agent, task, execution_ref)
    guard = getattr(agent.subagents, "creation_guard", None)
    with (guard() if callable(guard) else nullcontext(), store.tasks.transition_guard(root_id),
          locked_file_transition(store.storage.background_claim_path(task.target_thread_id))):
        with guard() if callable(guard) else nullcontext():
            repo, task, ref, root_id, claim, claim_ref, matches = _business_stop_target(agent, task, ref)
            # 完成缓存不跨取消模式复用；扩大到后代或整任务时，仍按准确原引用重新固定范围。
            link = store.tasks.load(root_id)
            if link is not None and link.thread_id != task.target_thread_id:
                raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
            store.session_tasks.record_execution_cancellation_control(task.task_id, execution_ref=ref,
                outstanding_refs=[ref.run_id], claim_ref=claim_ref,
                resources_prepared=False, resources_confirmed=False)
            cancel_runtime_run(repo, RuntimeCancellationTarget(root_id, ref.run_id, ref.agent_run_id, ref.observed_attempt_id),
                reason="business_cancel", source="session_task_control")
            if cancel_task and link is not None:
                if link.status not in {"done", "failed", "cancelled", "completed"}:
                    store.tasks.update_status({"task_id": root_id, "status": "cancelled", "expected_status": link.status})
            scope = ProcessExecutionScope(owner_home=str(agent.home_paths.owner_home_dir), thread_id=task.target_thread_id,
                root_task_id=root_id, run_id=ref.run_id, attempt_id=ref.observed_attempt_id)
            # 异步停止前绑定只含存储定位/不可变身份的证明消费者；预留出生或正面未启动都交接给同一选择。
            consumer = _CleanupProofConsumer(str(store.storage.root), task.task_id, ref)
            with bind_cleanup_proof_consumer(consumer):
                if include_descendants:
                    frozen = freeze_task_resources(agent, scope, root_id)
                else:
                    frozen = freeze_main_only_task_resources(scope)
            store.session_tasks.record_execution_cancellation_control(task.task_id, execution_ref=ref,
                outstanding_refs=[], resource_refs=_business_resource_references(frozen, scope),
                resources_prepared=not frozen.unconfirmed)
    if matches and claim.get("status") == "running":
        interrupt_by_name(conversation_claim_interrupt_name(claim["claim_id"]))
    cleanup_task_resources(frozen)
    repo, current, ref, root_id, claim, claim_ref, matches = _business_stop_target(agent, task, ref)
    if _retry_cleanup_proof_checkpoints(current):
        repo, current, ref, root_id, claim, claim_ref, matches = _business_stop_target(agent, task, ref)
    clean = _business_resources_exited(agent, repo, current)
    confirmed = clean and _business_execution_exited(agent, repo, current, ref, claim, matches)
    store.session_tasks.record_execution_cancellation_control(task.task_id, execution_ref=ref, outstanding_refs=[],
        resources_confirmed=clean, claim_finished=_original_business_claim_finished(current, claim, matches))
    if confirmed:
        try:
            store.session_tasks.record_cancellation_control(task.task_id, outstanding_refs=[], execution_ref=ref,
                confirmed_refs=(ref.run_id, task.task_id, task.conversation_request_id),
                expected_resource_refs=current.metadata["cancellation_control"]["resource_refs"])
        except ValueError:
            return False
    return confirmed


# LLM: 仅核已请求过的原引用和既有资源清理回执，不发停止、不等待；未知/新claim/attempt保持责任。
# 函数用途: 退休对账在原执行真正结束后消费同一取消账，而非永久被已确认责任阻塞。
def reconcile_business_session_task_cancellation(agent, task):
    control = task.metadata.get("cancellation_control", {})
    if not control.get("execution_ref") or not control.get("outstanding_refs"):
        return task
    try:
        repo, current, ref, _root, claim, _claim_ref, matches = _business_stop_target(agent, task)
        if _retry_cleanup_proof_checkpoints(current):
            repo, current, ref, _root, claim, _claim_ref, matches = _business_stop_target(agent, task)
        resources_exited = _business_resources_exited(agent, repo, current)
        if resources_exited and not control.get("resources_confirmed"):
            # 只更新物理退出事实；外部业务UNKNOWN仍保留原outstanding与禁止重放账。
            current = agent.conversation_store.session_tasks.record_execution_cancellation_control(task.task_id,
                execution_ref=ref, outstanding_refs=control.get("execution_outstanding_refs", control["outstanding_refs"]),
                resources_confirmed=True)
        if (resources_exited
                and _business_execution_exited(agent, repo, current, ref, claim, matches)):
            agent.conversation_store.session_tasks.record_execution_cancellation_control(task.task_id,
                execution_ref=ref, outstanding_refs=[], resources_confirmed=True, claim_finished=True)
            return agent.conversation_store.session_tasks.record_cancellation_control(task.task_id,
                outstanding_refs=[], execution_ref=ref, confirmed_refs=(ref.run_id, task.task_id, task.conversation_request_id),
                expected_resource_refs=current.metadata["cancellation_control"]["resource_refs"])
    except (OSError, ValueError, RuntimeError, PermissionError, TypeError, KeyError):
        pass
    return task


def _primary_binding(thread: object) -> object | None:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    for binding in bindings:
        return binding
    return None


# LLM: 委派目标不复制渠道绑定；按原 SessionTask/source/owner 和准确根/claim 调原主任务停止事务，不能去停原聊天。
# 函数用途: 取消隔离后台任务的实际父子资源，复用现有停止账和锁序，部分结果如实返回未确认。
def _stop_delegated_task(agent, target_thread, turn_id):
    from ..concurrency.interrupt import interrupt_by_name
    from ..conversation.control_commands import session_task_interrupt_name
    from ..conversation.execution_threads import (
        IM_TASK_EXECUTION_KEY,
        delegated_session_task,
        execution_origin_thread,
    )
    from .control_service import (
        _cancel_request_execution,
        _GatewayRequestRecord,
        _prepare_main_task_stop,
    )
    from .execution_target import read_gateway_execution_target

    store = agent.conversation_store
    try:
        task = delegated_session_task(store, target_thread.thread_id)
        if (task is None or task.task_id != turn_id or target_thread.owner_id != str(agent.home_paths.owner_id)):
            return SessionTaskStopOutcome(False, "后台停止身份不一致。", turn_id)
        current = store.threads.require(target_thread.thread_id)
        root_id = current.metadata[IM_TASK_EXECUTION_KEY].get("root_task_id", "")
        link = store.tasks.load(root_id) if root_id else None
        if link is None or link.thread_id != current.thread_id:
            return SessionTaskStopOutcome(False, "后台父任务尚未可靠绑定。", turn_id)
        source = execution_origin_thread(store.threads.require, current)
        binding = _primary_binding(source)
        if binding is None:
            return SessionTaskStopOutcome(False, "后台来源身份不可读。", turn_id)
        target = read_gateway_execution_target(gateway_paths(agent), store, current.thread_id)
        scope = GatewayControlScope(user_id=binding.channel_user_id, channel=binding.channel,
                                    conversation_id=binding.channel_conversation_id,
                                    metadata={"expected_execution": target.to_dict()} if target is not None else {})
        active = _GatewayRequestRecord(store.storage.task_path(link.task_id), {
            "id": link.task_id, "conversation_thread_id": link.thread_id, "conversation_task_link_status": link.status})
        prepared = _prepare_main_task_stop(agent, active, link.task_id, scope)
        if prepared.task_link is None:
            return SessionTaskStopOutcome(False, "后台执行已变化，请刷新任务状态。", turn_id)
        interrupt_by_name(session_task_interrupt_name(turn_id))
        _cancel_request_execution(link.task_id, resources=prepared.resources)
        confirmed = not prepared.preparation_errors and not (prepared.resources is not None and prepared.resources.unconfirmed)
        return SessionTaskStopOutcome(confirmed, "后台父子任务已请求停止。" if confirmed else "后台资源停止部分未确认。", turn_id)
    except (OSError, ValueError, RuntimeError):
        return SessionTaskStopOutcome(False, "后台资源停止未确认。", turn_id, "SESSION_TASK_STOP_UNCONFIRMED")


# LLM: 统一未确认错误码，避免把异常当成"已停止"；调用方只在 confirmed 为真时说已停止。
# 函数用途: 生成未确认的稳定错误码。
def _unconfirmed_code(exc: object) -> str:
    return "SESSION_TASK_STOP_UNCONFIRMED"


__all__ = ["SessionTaskStopOutcome", "stop_session_task_turn"]
