# LLM: 桥接原input/claim与受限工具；Bot冻结离线集合、物理根与总预算，由原worker复验执行，不借main invoker。
# 模块用途: 组装生产请求提交、受限工具和有界撤权观察。
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict
from types import SimpleNamespace

from ..common.nofollow_fs import read_bytes_beneath
from ..gateway_parts import input_delivery_service as inputs
from ..gateway_parts.io import write_json_file_atomic
from ..gateway_parts.queue_service import claim_request, ensure_gateway_folders
from ..gateway_parts.recovery import (
    _commit_gateway_terminal_request,
    _terminal_gateway_request_payload,
)
from ..local_storage import LocalStore
from .contracts import resolve_read_context
from .errors import HostAPIError
from .execution import ExecutionAccepted, ExecutionHostPorts, RestrictedToolInvoker
from .local_users import UserRuntimeUsage
from .requests import read_request_facts
from .schemas import canonical_json, strict_json, thaw

# 每次观察最多读取1024份原记录；超过预算失败关闭，不报告不完整零占用。
RUNTIME_OBSERVATION_MAX_COUNT = 1024
# 单条原claim或输入记录的读取上限，单位字节。
RUNTIME_RECORD_MAX_BYTES = 4 * 1024 * 1024


# LLM: 目录和文件都不跟随链接；不存在为真实空集合，错误/超限不是零占用。
# 函数用途: 有界冷读一个原运行目录。
def runtime_rows(home, directory):
    try:
        relative = directory.relative_to(home.root)
        # 使用原nofollow目录打开器，目录FD固定到同一次观察。
        from ..common.nofollow_fs import open_directory_beneath
        descriptor = open_directory_beneath(home.root, relative.parts)
    except FileNotFoundError:
        return []
    rows = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                if len(rows) >= RUNTIME_OBSERVATION_MAX_COUNT:
                    raise HostAPIError("STORE_UNAVAILABLE", 503)
                raw = read_bytes_beneath(home.root, (*relative.parts, entry.name), max_bytes=RUNTIME_RECORD_MAX_BYTES,
                                         require_dir_fd=True)
                rows.append(strict_json(raw, RUNTIME_RECORD_MAX_BYTES))
        return rows
    except (OSError, ValueError, TypeError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    finally:
        os.close(descriptor)


# LLM: claim是当前执行占用观察，不等价OS进程存活；磁盘没有完整测量，不伪造complete。
# 函数用途: 读取指定普通用户的原claim与请求状态。
def runtime_usage(resources, uid):
    home = resources.users.owner_home(uid)
    context = SimpleNamespace(owner_home=home)
    claims = runtime_rows(home, resources.store_for(context).storage.background_claims_dir)
    paths = resources.paths_for(context)
    requests = runtime_rows(home, paths.inbox) + runtime_rows(home, paths.processing)
    active = [row for row in claims if row.get("status") in {"running", "claimed"}]
    busy = any(row.get("cancel_requested") is not True for row in requests)
    return UserRuntimeUsage(len(active), 0, bool(active) or busy, complete=False)


# LLM: 只标记本owner原队列，禁用账号已由C持久提交；资源停止未知，不能报告已终止。
# 函数用途: 在原输入转换锁内请求取消存活请求。
def stop_owner_requests(resources, uid, revision):
    home = resources.users.owner_home(uid)
    paths = resources.paths_for(SimpleNamespace(owner_home=home))
    requested = False
    for folder in (paths.inbox, paths.processing):
        for row in runtime_rows(home, folder):
            request_id = row.get("id")
            if not isinstance(request_id, str) or not request_id.startswith("plugin-"):
                continue
            with inputs.gateway_input_transition(paths, request_id):
                raw = read_bytes_beneath(home.root, (folder / (request_id + ".json")).relative_to(home.root).parts,
                                         max_bytes=RUNTIME_RECORD_MAX_BYTES, require_dir_fd=True)
                if raw is None:
                    continue
                current = strict_json(raw, RUNTIME_RECORD_MAX_BYTES)
                if current.get("host_request_owner.v1") != asdict(home.identity):
                    raise HostAPIError("TARGET_DENIED", 403)
                current.update(cancel_requested=True, cancel_requested_at=time.time())
                write_json_file_atomic(folder / (request_id + ".json"), current)
                requested = True
    return requested


# LLM: 请求服务不注册模型/插件callable，不创建第二份队列或结果账；只执行已审有界文件工具。
# 类用途: 接入B2原输入和C准确执行队列端口。
class GatewayPluginRequests:
    # LLM: 构造零IO，conversation由同一组合根在暴露端口前绑定。
    # 函数用途: 保存当前宿主依赖。
    def __init__(self, resources):
        self.resources, self.conversation = resources, None

    # LLM: Bot只用已持久绑定代次；普通消息保留零工具/禁网络策略，仅显式打开原公开typed transcript出口。
    # 函数用途: 准备准确线程、模式与公开流显示能力，不把显示能力当作执行授权。
    def prepare(self, authorized, context, execution, prepared):
        from .authorization import revalidate_call
        revalidate_call(authorized)
        if authorized.call.action == "conversations.bots.submit":
            return self.prepare_bot_request(authorized, context, execution, prepared)
        if authorized.call.payload.get("input_artifact_refs"):
            raise HostAPIError("ARTIFACT_INPUT_NOT_SUPPORTED", 403)
        if authorized.call.payload.get("tool_policy", "none") != "none" or authorized.execution_policy.tool_names:
            raise HostAPIError("HOST_NOT_READY", 503)
        ensure_gateway_folders(self.resources.paths_for(context))
        return {**prepared, "owner_id": context.owner_home.owner_id, "client_capabilities": {"rich_transcript": True},
                "conversation": {"thread_id": authorized.call.payload["thread_id"]},
                "metadata": {**prepared["metadata"], "plugin_execution_policy": {
                    "schema": "plugin_execution_policy.v1", "tool_names": [],
                    "network": False, "budgets": thaw(authorized.execution_policy.budgets),
                    "policy_digest": authorized.execution_policy.policy_digest,
                    "thread_id": authorized.call.payload["thread_id"]}}}

    # LLM: 绑定来自原prepare_submit回执，并与canonical线程双核；普通payload/page_context无权覆盖模式与禁学。
    # 函数用途: 给Bot原输入准备函数生成固定线程、代次和工具策略。
    def prepare_bot_request(self, authorized, context, binding, prepared):
        from ..conversation.bot_sessions import bot_thread_fields
        from .authorization import revalidate_call

        revalidate_call(authorized)
        tid = binding["active_thread_id"]
        store = self.resources.store_for(context)
        thread = self.conversation.require_thread(store, tid, authorized, context)
        fields = bot_thread_fields(thread)
        if fields.get("bot_mode") != binding["bot_mode"] or fields.get("bot_generation") != binding["generation"]:
            raise HostAPIError("BOT_MODE_MISMATCH", 409)
        policy = bot_execution_policy(authorized, context, fields)
        policy["thread_id"] = tid
        ensure_gateway_folders(self.resources.paths_for(context))
        metadata = {**prepared["metadata"], **{key: fields[key] for key in
            ("bot_mode", "bot_generation", "learning_excluded")},
            "read_only_mode": fields["bot_mode"] == "query_readonly", "plugin_execution_policy": policy}
        return {**prepared, "owner_id": context.owner_home.owner_id,
                "conversation": {"thread_id": tid}, "metadata": metadata}

    # LLM: 原input receipt是唯一入队权威，key不含代次以防重放跨线程；权限现查，队列只受理不冒领业务完成。
    # 函数用途: 将已准备的Bot代次幂等排入调用者自己的Gateway队列。
    def submit_bot_message(self, authorized, context, binding):
        from .authorization import revalidate_call

        revalidate_call(authorized)
        payload = thaw(authorized.call.payload)
        if payload["text"].lstrip().startswith("/"):
            raise HostAPIError("CONTROL_COMMAND_NOT_ALLOWED", 403)
        scope = [authorized.binding.installation_ref, asdict(context.identity),
                 authorized.call.action, authorized.call.idempotency_key]
        request_id = "plugin-msg-" + hashlib.sha256(canonical_json(scope).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical_json([scope, payload, binding["active_thread_id"],
                                                     binding["generation"]]).encode()).hexdigest()
        provenance = {"installation_ref": authorized.binding.installation_ref,
            "package_sha256": authorized.binding.package_sha256, "activation_id": authorized.binding.activation_id,
            "grant_revision": authorized.binding.grant_revision, "thread_id": binding["active_thread_id"],
            "kind": "chat", "tool_policy": payload["tool_policy"]}
        prepared = {"id": request_id, "kind": "ask", "source": "plugin", "goal": payload["text"],
            "host_request_owner.v1": asdict(context.identity), "metadata": {"origin": "plugin",
            "client_input_digest": fingerprint, "plugin_provenance": provenance,
            "page_context": payload["page_context"]}}
        prepared = self.prepare(authorized, context, binding, prepared)
        request = inputs.GatewayInputPreparation(request_id, fingerprint, request_id, request_id, prepared)
        paths = self.resources.paths_for(context)
        with inputs.gateway_input_transition(paths, request_id):
            revalidate_call(authorized)
            try:
                receipt, fresh = inputs.load_or_prepare_gateway_input_request_locked(paths, request)
            except ValueError:
                raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409) from None
            revalidate_call(authorized)
            if receipt.state in {"pending", "queued"}:
                inputs.queue_gateway_input_locked(paths, receipt)
        facts = read_request_facts(paths, request_id, expected_owner=context.identity,
                                    installation_ref=authorized.binding.installation_ref)
        return {"bot_mode": binding["bot_mode"], "thread_id": binding["active_thread_id"],
                "request_id": request_id, "status": facts["state"], "generation": binding["generation"],
                "rolled_over": binding["rolled_over"], "previous_thread_id": binding["previous_thread_id"],
                "rollover_reason": binding["rollover_reason"], "idempotent_replay": not fresh}

    # LLM: 原cancel已落原账才调用；只发准确request命名中断，不影响父兄弟；同Owner实际Agent可沿原控制补停，未核实不称停止。
    # 函数用途: 把运行中取消接到真实线程信号及既有会话回合停止口。
    def cancel(self, authorized, context, row):
        from ..concurrency.interrupt import interrupt_by_name
        from ..conversation.control_commands import conversation_request_interrupt_name
        from ..gateway_parts.session_task_stop import stop_session_task_turn
        from .authorization import revalidate_call

        revalidate_call(authorized)
        if authorized.call.action != "conversations.requests.cancel" or row.get("id") != authorized.call.payload["request_id"]:
            raise HostAPIError("TARGET_DENIED", 403)
        provenance = row.get("metadata", {}).get("plugin_provenance", {})
        thread = self.conversation.require_thread(self.resources.store_for(context), provenance["thread_id"], authorized, context)
        interrupt_by_name(conversation_request_interrupt_name(row["id"]))
        agent = getattr(self.resources.server, "agent", None)
        home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if home is not None and str(home) == str(context.owner_home.home_dir):
            stop_session_task_turn(agent, target_thread=thread, turn_id=row["id"])
        return None

    # LLM: C上下文不能替代B线程范围，仍由真实AuthorizedCall读取同owner canonical线程。
    # 函数用途: 在执行准入前要求精确线程授权。
    def require_thread(self, context, thread_id):
        read = resolve_read_context(context.source)
        return self.conversation.require_thread(self.resources.store_for(read), thread_id, context.source, read)

    # LLM: 资产必须从原owner注册表读取内容和revision，不按路径或插件声明授执行许可。
    # 函数用途: 取得当前准确注册资产。
    def read_asset(self, context, payload):
        from .gateway_assets import registered_asset
        return registered_asset(context, payload)

    # LLM: 仅允许已审内置文件工具或已注册脚本/工作流执行，返回成功状态与安全错误码。
    # 函数用途: 执行单次已获批工具或资产请求。
    def _run_execution_plan(self, context, plan, payload, fence) -> tuple[bool, str]:
        if plan.action == "execute.tool":
            ledger = LocalStore(context.owner_home.home_dir / "data/tool-operations.db")
            try:
                result = RestrictedToolInvoker(
                    self.resources.resolver.factory, context, ExecutionHostPorts(ledger)
                ).invoke(payload["tool_name"], payload["arguments"])
                ok = bool(result.result.ok)
                return ok, "" if ok else "TOOL_EXECUTION_FAILED"
            finally:
                close = getattr(ledger, "close", None)
                if callable(close):
                    close()
        from .gateway_assets import execute_registered_asset
        return execute_registered_asset(context, plan, fence)

    # LLM: 原receipt/claim/canonical为唯一执行事实；同键重放不重复文件副作用，未知claim拒绝重做。
    # 函数用途: 受理并经原执行器完成有界内置文件操作或已注册资产执行。
    def enqueue(self, context, plan, fence):
        if plan.action not in {"execute.tool", "execute.script", "execute.workflow", "execute.workflow_resume"}:
            raise HostAPIError("ASSET_EXECUTION_NOT_SUPPORTED", 403)
        authorized = context.source
        read = resolve_read_context(authorized)
        paths = self.resources.paths_for(read)
        ensure_gateway_folders(paths)
        identity = [authorized.binding.installation_ref, asdict(context.identity), plan.action, authorized.call.idempotency_key]
        request_id = "plugin-exec-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        digest = hashlib.sha256(canonical_json([identity, plan.payload_json]).encode()).hexdigest()
        payload = thaw(authorized.call.payload)
        kind = "tool" if plan.action == "execute.tool" else plan.action.split(".", 1)[1]
        prepared = {"id": request_id, "kind": "ask", "source": "plugin", "goal": f"Restricted {kind}",
            "host_request_owner.v1": asdict(context.identity), "owner_id": context.owner_home.owner_id,
            "metadata": {"client_input_digest": digest, "plugin_provenance": {
                "installation_ref": authorized.binding.installation_ref, "thread_id": payload["thread_id"],
                "kind": kind, "activation_id": authorized.binding.activation_id}}}
        request = inputs.GatewayInputPreparation(request_id, digest, request_id, request_id, prepared)
        with inputs.gateway_input_transition(paths, request_id):
            fence.check()
            try:
                receipt, fresh = inputs.load_or_prepare_gateway_input_request_locked(paths, request)
            except ValueError:
                raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409) from None
            facts = None if fresh else read_request_facts(paths, request_id, expected_owner=context.identity,
                                                        installation_ref=authorized.binding.installation_ref)
            if facts and facts["terminal"] is not None:
                return ExecutionAccepted(request_id, payload["thread_id"], True)
            if not fresh:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            inputs.queue_gateway_input_locked(paths, receipt)
        claim = claim_request(paths, paths.inbox / (request_id + ".json"))
        if claim is None:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        row = strict_json(claim.path.read_bytes(), RUNTIME_RECORD_MAX_BYTES)
        try:
            fence.check()
            ok, error_code = self._run_execution_plan(context, plan, payload, fence)
        except Exception as exc:
            err = exc.code if isinstance(exc, HostAPIError) else "EXECUTION_ABORTED"
            failed = _terminal_gateway_request_payload(
                request_id, row, {"id": request_id, "status": "failed", "ok": False, "error_code": err, "ended_at": time.time()}
            )
            _commit_gateway_terminal_request(paths, claim.path, paths.failed, request_id, failed)
            raise
        response = {"id": request_id, "status": "done" if ok else "failed", "ok": ok,
                    "error_code": error_code, "ended_at": time.time()}
        terminal = _terminal_gateway_request_payload(request_id, row, response)
        _commit_gateway_terminal_request(paths, claim.path, paths.done if ok else paths.failed, request_id, terminal)
        return ExecutionAccepted(request_id, payload["thread_id"], False)


# LLM: grant/owner/治理交集后只保已核离线工具；冻结同owner物理workspace、总调用预算及禁学，供receipt和worker共核。
# 函数用途: 准备当前Bot的可撤销工具与根目录策略，供原worker快照接缝消费。
def bot_execution_policy(authorized, context, fields):
    from ..audit_group.access_policy import current_tool_policy
    from ..conversation.bot_tool_policy import OFFLINE_TOOLS, READ_TOOLS, bot_execution_constraints
    from ..user_space.local_user_store import LocalUserStore
    from ..user_space.managed_user_governance import _GovernanceControl
    from ..user_space.owner_resolver import OwnerIdentity

    home = context.owner_home
    users = LocalUserStore(home.root)
    policy, controls = current_tool_policy(home.root, home)
    if controls.get("disabled", False):
        raise HostAPIError("USER_DISABLED", 403)
    member = context.identity == OwnerIdentity.local_main()
    governance = {}
    if context.identity.provider == "local" and context.identity.owner_kind == "user":
        user = users.require_enabled(context.identity.owner_id)
        member = user.in_audit_group is True
        governance = _GovernanceControl(home.root, user.user_id).policy()
    execution = authorized.binding.grant["execution"]
    names = (set(execution["tool_names"]) - set(policy.get("disabled_tools", []))) & OFFLINE_TOOLS
    if authorized.call.payload["tool_policy"] == "none" or execution["max_tool_calls"] == 0:
        names.clear()
    parts = home.home_dir.relative_to(users.root).parts
    permissions = users.read_json((*parts, "permissions.json")) or {}
    readonly = fields["bot_mode"] == "query_readonly"
    if readonly or permissions.get("filesystem", {}).get("access_mode") == "read-only":
        names &= READ_TOOLS
    if not member:
        names = {name for name in names if not name.startswith("audit_group_")}
    constraints = bot_execution_constraints(home, execution["max_tool_calls"], fields["learning_excluded"])
    fingerprint = hashlib.sha256(canonical_json([policy, permissions, governance, member, sorted(names), constraints]).encode()).hexdigest()
    return {"schema": "plugin_execution_policy.v1", "tool_names": sorted(names), "network": False,
            "budgets": {key: value for key, value in execution.items() if key.startswith("max_")},
            "policy_digest": fingerprint,
            "bot_mode": fields["bot_mode"], "bot_generation": fields["bot_generation"],
            "learning_excluded": fields["learning_excluded"], "read_only_mode": readonly,
            "in_audit_group": member, "governance_revision": governance.get("revision", 0),
            "bot_execution_policy": constraints,
            "read_roots": [constraints["workspace_root"]],
            "write_roots": [] if readonly else [constraints["workspace_root"]]}
