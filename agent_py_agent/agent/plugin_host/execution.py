# LLM: 执行只走目标用户新建快照和原ToolExecutor，不接受插件callable或main invoker。
# 模块用途: 装配受限工具执行与宿主队列、资产执行接缝。
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from ..contracts.tool_input_schema import validate_tool_input
from ..tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool
from ..tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from ..tooling.executor import ToolExecutor, ToolExecutorRequest
from ..tooling.models import ToolRuntime, ToolRuntimeSnapshot
from ..tooling.runtime_contracts import ToolCall
from .errors import HostAPIError
from .execution_policy import AssetGrant, build_restricted_snapshot, require_asset
from .local_users import LocalUserDomainResult
from .owner_context import call_payload
from .revocation import AssetStepFence, UserRevocationFence


# LLM: 原操作账及审批由B/宿主装配；缺失不自动批准写入，不从插件参数生成权限。
# 类用途: 为目标用户原工具管线提供唯一持久化和审批依赖。
@dataclass(frozen=True)
class ExecutionHostPorts:
    operation_store: object | None = None
    approved_actions: object | None = None


# LLM: 只构造已审的内置文件实现，不复制主代理工具、MCP、环境或模型凭据。
# 函数用途: 为独立目标目录建立没有进程和网络工具的冻结快照。
def file_snapshot(context, limits, run_id):
    home = context.owner_home
    access = FileSystemAccessOptions(owner_scope_root=str(home.home_dir), protected_persona_root=str(home.home_dir),
                                   owner_quota_max_bytes=limits.max_disk_mb * 1024 * 1024)
    tools = (ReadFileTool(home.workspace_dir, 16000, access_options=access),
             WriteFileTool(home.workspace_dir, list(context.execution_policy.write_roots),
                           options=WriteFileToolOptions(access_options=access)))
    runtimes = tuple(ToolRuntime(tool.model_spec, tool.runtime_policy, tool) for tool in tools)
    names = frozenset(tool.model_spec.name for tool in tools)
    snapshot = ToolRuntimeSnapshot(run_id, runtimes, names, (), allowed_tools=names, owner_type="subagent")
    return build_restricted_snapshot(snapshot, context.execution_policy)


# LLM: 原请求身份跨invoker重建保持稳定，步骤冲突继续由原操作账检测；无键只允许冷读取。
# 函数用途: 为原工具操作生成与安装、激活、用户、动作和原幂等键绑定的运行身份。
def execution_run_id(context):
    call = context.call
    if call.idempotency_key is None:
        return "plugin-read-" + uuid.uuid4().hex
    if type(call.idempotency_key) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", call.idempotency_key) is None:
        raise HostAPIError("INVALID_SCHEMA")
    source = call.grant.source_binding
    encoded = json.dumps([source.installation_ref, source.activation_id, context.identity.owner_id,
                          call.action, call.idempotency_key], ensure_ascii=False).encode("utf-8")
    return "plugin-execution-" + hashlib.sha256(encoded).hexdigest()


# LLM: 直接工具请求不可改名/改参，chat多步请求仍由原步骤操作账与白名单约束。
# 函数用途: 防止宿主执行接线意外把准确请求变成另一条工具调用。
def require_bound_tool_call(context, name, arguments):
    if name == "write_file" and context.call.idempotency_key is None:
        raise HostAPIError("INVALID_SCHEMA")
    if context.call.action != "execute.tool":
        return
    payload = call_payload(context.call)
    try:
        same = json.dumps(payload.get("arguments"), sort_keys=True, allow_nan=False) == json.dumps(arguments, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HostAPIError("INVALID_SCHEMA") from exc
    if payload.get("tool_name") != name or not same:
        raise HostAPIError("CALL_MISMATCH", 403)


# LLM: 当前来源和用户修订必须在每次调用及短提交时复核，重建不得更换原写操作身份。
# 类用途: 给代用户工作提供不可扩权的原工具执行入口。
class RestrictedToolInvoker:
    # LLM: 构造不启动模型或进程；快照绑定原请求稳定身份，未获批工具不进入快照。
    # 函数用途: 为本次有效上下文建立独立执行器。
    def __init__(self, factory, context, ports=None):
        factory.check(context)
        from .contracts import VerifiedPluginBinding
        if isinstance(context.source, VerifiedPluginBinding):
            # 发行期binding只供策略解析，不能绕过真实调用意图取得invoker。
            raise HostAPIError("SOURCE_INVALID", 403)
        if not context.execution:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        self.factory = factory
        self.context = context
        self.ports = ports or ExecutionHostPorts()
        self.fence = UserRevocationFence(factory, context)
        self.limits = context.execution_policy.quota
        self.snapshot = file_snapshot(context, self.limits, execution_run_id(context))
        budgets = getattr(getattr(context.source, "execution_policy", None), "budgets", None)
        self.max_calls = int(budgets["max_tool_calls"]) if budgets is not None and "max_tool_calls" in budgets else 1
        self.calls = 0

    # LLM: 实参不能覆盖owner或原直接工具意图，审批及原步骤操作账不变；B恢复须重进本入口。
    # 函数用途: 执行一个准确获批的目标用户工具调用。
    def invoke(self, tool_name, arguments, call_id="step-1"):
        self.fence.check()
        if self.calls >= self.max_calls or type(tool_name) is not str or tool_name not in self.snapshot.available_tool_names:
            raise HostAPIError("TOOL_DENIED", 403)
        self.calls += 1
        runtime = self.snapshot.runtime(tool_name)
        if type(arguments) is not dict:
            raise HostAPIError("INVALID_SCHEMA")
        require_bound_tool_call(self.context, tool_name, arguments)
        call = ToolCall(call_id, tool_name, arguments, "native", runtime.model_spec.schema_hash,
                        self.snapshot.run_id, "tool-turn", "attempt-1")
        boundary = self.boundary(call)
        # 只有有界内置文件写入持用户短门；此快照没有长进程、网络或审批等待handler。
        scope = self.factory.commit(self.context) if tool_name == "write_file" else nullcontext()
        with scope:
            self.fence.check()
            return ToolExecutor().execute(ToolExecutorRequest(
                call, self.snapshot, self.context.owner_home.workspace_dir,
                workspace_roots=self.context.execution_policy.write_roots,
                owner_scope_root=str(self.context.owner_home.home_dir), write_boundary=boundary,
                operation_owner_id=self.context.owner_home.owner_id, owner_type="subagent", approval_mode="ask",
                operation_store=self.ports.operation_store, operation_store_required=True,
                pre_handler_gate=self.pre_handler, allow_nested_dispatch=False, admin_full_access=False,
            ))

    # LLM: 边界只由目标路径和宿主已批准的原调用生成；插件实参不能覆盖。
    # 函数用途: 收窄原文件工具的精确业务根并接原审批绑定。
    def boundary(self, call):
        home = self.context.owner_home
        policy = self.context.execution_policy
        boundary = {"read_scope_mode": "exact", "allowed_read_roots": [str(path) for path in policy.read_roots],
                    "allowed_write_roots": [str(path) for path in policy.write_roots], "owner_home": str(home.home_dir)}
        if self.ports.approved_actions is not None:
            boundary["approved_actions"] = self.ports.approved_actions(self.context, call)
        return boundary

    # LLM: 原executor在审批和schema验证后仍重核用户，不能用批准前快照继续执行。
    # 函数用途: 在handler与原操作claim前阻断撤销。
    def pre_handler(self, call):
        self.fence.check()
        return None


# LLM: 仅为B原队列回执的安全投影，不保存新的执行权威。
# 类用途: 传回原请求受理编号。
@dataclass(frozen=True)
class ExecutionAccepted:
    request_id: str
    thread_id: str
    replayed: bool


# LLM: 只运输原调用的不可变业务意图，原队列仍拥有receipt、claim和终态。
# 类用途: 让B按当前目标上下文受理准确工具或资产请求。
@dataclass(frozen=True)
class ExecutionPlan:
    action: str
    payload_json: str
    asset: AssetGrant | None = None


# LLM: ID不允许空白、路径或隐式字符串转换；本函数不建立对象归属。
# 函数用途: 核验线程、请求与资产稳定编号的闭合格式。
def valid_execution_id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None


# LLM: 仅真实宿主注入；资产端口须核原owner/安装/checkpoint，队列必须装配受限沙箱及每步/审批后栅栏，缺失关闭。
# 类用途: 绑定原队列、线程权限和准确资产读取。
@dataclass(frozen=True)
class ExecutionQueuePorts:
    enqueue: object | None = None
    require_thread: object | None = None
    read_asset: object | None = None


# LLM: A提供已核验source，B提供原队列，本域不建立第二份任务账。
# 类用途: 解析并提交受控执行请求。
class ExecutionDomain:
    # LLM: 初始化无副作用，不自动发现主代理或提权回调。
    # 函数用途: 绑定目标工厂和宿主端口。
    def __init__(self, factory, ports=None):
        self.factory = factory
        self.ports = ports or ExecutionQueuePorts()

    # LLM: 完整验证和当前来源复核必须先于原队列提交。
    # 函数用途: 将一个execute请求映射到原宿主请求链。
    def submit(self, authorized):
        context = self.factory.resolve_execution_context(authorized)
        payload = call_payload(context.call)
        self.validate(context, payload)
        if self.ports.enqueue is None or self.ports.require_thread is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        self.ports.require_thread(context, payload["thread_id"])
        asset, fence = self.execution_fence(context, payload)
        fence.check()
        plan = ExecutionPlan(context.call.action, context.call.payload_json, asset)
        # B必须在原receipt/claim提交门再次消费fence；不能持用户锁等待队列或模型。
        receipt = self.ports.enqueue(context, plan, fence)
        if (type(receipt) is not ExecutionAccepted or not valid_execution_id(receipt.request_id)
                or receipt.thread_id != payload["thread_id"] or type(receipt.replayed) is not bool):
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        return LocalUserDomainResult(
            {"request_id": receipt.request_id, "thread_id": receipt.thread_id, "accepted": True,
             "state": "queued", "replayed": receipt.replayed}, status=202,
        )

    # LLM: 四个动作schema闭合，缺键、身份覆盖和未知动作在原队列之前拒绝。
    # 函数用途: 检查准确执行请求的公共参数及动作专有约束。
    def validate(self, context, payload):
        call = context.call
        if type(call.idempotency_key) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", call.idempotency_key) is None:
            raise HostAPIError("INVALID_SCHEMA")
        schemas = {
            "execute.tool": ({"thread_id", "tool_name", "arguments"}, set()),
            "execute.script": ({"thread_id", "script_id", "expected_revision"}, {"inputs"}),
            "execute.workflow": ({"thread_id", "workflow_id", "expected_revision"}, {"inputs"}),
            "execute.workflow_resume": ({"thread_id", "workflow_run_id", "expected_revision", "from_step_id"}, set()),
        }
        if call.action not in schemas:
            raise HostAPIError("ACTION_NOT_AVAILABLE", 403)
        required, optional = schemas[call.action]
        if not required <= set(payload) or set(payload) - required - optional:
            raise HostAPIError("INVALID_SCHEMA")
        if any(not valid_execution_id(value) for key, value in payload.items() if key.endswith("_id")):
            raise HostAPIError("INVALID_SCHEMA")
        if call.action == "execute.tool":
            return self.validate_tool(context, payload)
        if type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1:
            raise HostAPIError("INVALID_SCHEMA")
        if type(payload.get("inputs", {})) is not dict:
            raise HostAPIError("INVALID_SCHEMA")

    # LLM: 执行工具必须同时符合静态动作、当前目标快照及准确原工具schema。
    # 函数用途: 在任何队列副作用之前拒绝管理工具及非法工具参数。
    def validate_tool(self, context, payload):
        name = payload["tool_name"]
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9_]{1,96}", name) is None:
            raise HostAPIError("INVALID_SCHEMA")
        snapshot = file_snapshot(context, context.execution_policy.quota, "plugin-admission")
        if name not in snapshot.available_tool_names:
            raise HostAPIError("TOOL_DENIED", 403)
        if not validate_tool_input(payload["arguments"], snapshot.runtime(name).model_spec.input_schema).ok:
            raise HostAPIError("INVALID_SCHEMA")

    # LLM: 资产必须由原注册表解析；resume读口还核原run的owner、安装和失败断点，绝不猜路径重建。
    # 函数用途: 为工具或准确资产返回逐步撤权门。
    def execution_fence(self, context, payload):
        if context.call.action == "execute.tool":
            return None, UserRevocationFence(self.factory, context)
        if self.ports.read_asset is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        asset = self.ports.read_asset(context, payload)
        require_asset(context.execution_policy, asset)
        kind = "script" if context.call.action == "execute.script" else "workflow"
        if asset.kind != kind or asset.revision != payload["expected_revision"]:
            raise HostAPIError("ASSET_CHANGED", 409)
        if context.call.action != "execute.workflow_resume" and asset.asset_id != payload[kind + "_id"]:
            raise HostAPIError("ASSET_DENIED", 403)
        return asset, AssetStepFence(self.factory, context, asset, self.current_asset)

    # LLM: 重读始终使用原调用绑定，不接受步骤自行覆盖的资产选择器或来源。
    # 函数用途: 每步重新取得原注册资产或原断点当前授权版本。
    def current_asset(self, context, asset):
        return self.ports.read_asset(context, call_payload(context.call))
