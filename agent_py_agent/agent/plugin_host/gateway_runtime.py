# LLM: 只桥接原input/claim/canonical与C受限文件工具；未知结局不得重做写，绝不借main invoker。
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

    # LLM: 原队列中记录显式零工具/受限策略，不允许降回main；输入制品须由注册服务另核，不默默忽略。
    # 函数用途: 准备准确消息的固定执行合同。
    def prepare(self, authorized, context, execution, prepared):
        from .authorization import revalidate_call
        revalidate_call(authorized)
        if authorized.call.payload.get("input_artifact_refs"):
            raise HostAPIError("ARTIFACT_INPUT_NOT_SUPPORTED", 403)
        ensure_gateway_folders(self.resources.paths_for(context))
        return {**prepared, "owner_id": context.owner_home.owner_id,
                "metadata": {**prepared["metadata"], "plugin_execution_policy": {
                    "schema": "plugin_execution_policy.v1", "tool_names": sorted(authorized.execution_policy.tool_names),
                    "network": False, "budgets": thaw(authorized.execution_policy.budgets),
                    "policy_digest": authorized.execution_policy.policy_digest,
                    "thread_id": authorized.call.payload["thread_id"]}}}

    # LLM: 原cancel已持久标记；没有当前运行句柄时不假装成功杀进程。
    # 函数用途: 对已受理请求保留取消请求，停止事实留待canonical观察。
    def cancel(self, authorized, context, row):
        from .authorization import revalidate_call
        revalidate_call(authorized)
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

    # LLM: 原receipt/claim/canonical为唯一执行事实；同键重放不重复文件副作用，未知claim拒绝重做。
    # 函数用途: 受理并经原执行器完成有界内置文件操作。
    def enqueue(self, context, plan, fence):
        if plan.action != "execute.tool":
            raise HostAPIError("ASSET_EXECUTION_NOT_SUPPORTED", 403)
        authorized = context.source
        read = resolve_read_context(authorized)
        paths = self.resources.paths_for(read)
        ensure_gateway_folders(paths)
        identity = [authorized.binding.installation_ref, asdict(context.identity), plan.action, authorized.call.idempotency_key]
        request_id = "plugin-exec-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        digest = hashlib.sha256(canonical_json([identity, plan.payload_json]).encode()).hexdigest()
        payload = thaw(authorized.call.payload)
        prepared = {"id": request_id, "kind": "ask", "source": "plugin", "goal": "Restricted file tool",
            "host_request_owner.v1": asdict(context.identity), "owner_id": context.owner_home.owner_id,
            "metadata": {"client_input_digest": digest, "plugin_provenance": {
                "installation_ref": authorized.binding.installation_ref, "thread_id": payload["thread_id"],
                "kind": "tool", "activation_id": authorized.binding.activation_id}}}
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
        fence.check()
        ledger = LocalStore(context.owner_home.home_dir / "data/tool-operations.db")
        try:
            result = RestrictedToolInvoker(self.resources.resolver.factory, context, ExecutionHostPorts(ledger)).invoke(
                payload["tool_name"], payload["arguments"])
            response = {"id": request_id, "status": "done" if result.result.ok else "failed", "ok": result.result.ok,
                        "ended_at": time.time()}
            row = strict_json(claim.path.read_bytes(), RUNTIME_RECORD_MAX_BYTES)
            terminal = _terminal_gateway_request_payload(request_id, row, response)
            _commit_gateway_terminal_request(paths, claim.path, paths.done if response["ok"] else paths.failed, request_id, terminal)
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()
        return ExecutionAccepted(request_id, payload["thread_id"], False)
