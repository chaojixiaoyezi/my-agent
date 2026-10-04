# LLM: 老格式模式来源统一读取原授权；确认与执行边界复核同一事实，旧代清理仍沿原撤销链和安装回执。
# 模块用途: 组合权限、确认身份、撤旧代计划与漂移复验，不实现 OS 沙箱或另一份授权账。
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from ..plugin_activation import PluginActivationRequest, prepare_activation, prepare_release
from ..plugin_deactivation import deactivate_plugin
from ..plugin_environment_plan import plan_plugin_environment
from ..plugin_installation import PluginInstallationError
from ..plugin_runtime_facts import PluginRuntimeError, resolve_plugin_runtime
from ..runtime_db.host_commands import HostCommandIdentity
from .grants import permission_details, permission_mode, select_permissions
from .state import legacy_permissions


# LLM: 身份/配置只由管理宿主注入；arguments 为原明确管理员参数，构造不读凭据或启动进程。
# 类用途: 将启用的必要依赖收成一份冻结上下文，避免长参数接口和第二套授权账。
@dataclass(frozen=True)
class LegacyEnableContext:
    owner: object
    repository: object
    binding: object
    arguments: dict
    process_sandbox: bool = False
    legacy_sandbox_default: bool = True
    forbidden_roots: tuple = ()


# LLM: 兼容与新授权在同一事实源判断；关闭默认不能把 restricted 变回 wide，不根据字段缺失授兼容。
# 函数用途: 选择本次重新授权的模式，现有进程的固定授权不被热改。
def enable_mode(entry, default: bool) -> str:
    grant = legacy_permissions(entry)
    return permission_mode(default, grant["mode"] if grant else "")


# LLM: 原 HostCommand 输入摘要不可改写；预览先为下一确认请求确定编号，使实际 operation/激活代与确认事实完全一致。
# 函数用途: 生成或恢复本次确认请求的精确身份，不写另一个确认表。
def authorization_identity(context: LegacyEnableContext) -> HostCommandIdentity:
    request = context.binding.request
    explicit = context.arguments.get("authorization")
    if explicit:
        if explicit != request.request_id or not explicit.startswith("permit-"):
            raise ValueError("确认请求未绑定")
        identity = explicit
    else:
        identity = "permit-" + hashlib.sha256(request.operation_id.encode()).hexdigest()
    return HostCommandIdentity(request.owner_id, request.actor_id, request.channel, request.thread_id, identity)


# LLM: 四维原参数均明确接入；宿主控制根不接受授权，JSON 真值不能替代布尔网络开关。
# 函数用途: 冻结管理员的有限路径身份和程序内容，原安装及私有配置不推导授权。
def enable_selection(context: LegacyEnableContext):
    values = context.arguments
    selected = {key: list(values.get(key) or ()) for key in ("read_roots", "write_roots", "program_roots")}
    selected["network"] = values.get("network", False)
    return select_permissions(selected, context.forbidden_roots)


# LLM: 撤旧代是同一管理 handler 的明确子提交，使用不同阶段身份避免 release 回执与新 prepare 冲突，不另建执行账。
# 函数用途: 为精确撤销/释放旧代生成固定的提交编号。
def retirement_operation(operation_id: str) -> str:
    return "retire-" + hashlib.sha256(operation_id.encode()).hexdigest()


# LLM: 只预测旧代撤销/释放后的版本；与 retire_for_enable 有意各算一次固定 operation，执行器比较实际记录与预测目标。
# 函数用途: 固定新授权的安装版本和代次，计算结果不是清理证明；两处身份漂移必须被目标相等守卫拒绝。
def authorization_target(entry, operation_id: str):
    if entry.activation is None:
        return entry
    if not entry.enabled:
        raise PluginInstallationError("activation_unsettled", "原激活尚未清理，不能准备新一代。")
    operation = retirement_operation(operation_id)
    stopped = prepare_activation(PluginActivationRequest(
        operation, entry.revision, replace(entry.activation, phase="revoked")), (entry,)).installation
    return prepare_release(operation, stopped, (stopped,)).installation


# LLM: 真实撤销沿原安装/资源账；operation 再算一次是有意守卫，实际记录须与 authorization_target 的完整预测相等。
# 函数用途: 确认后结束一次性兼容，退出未知保持 revoked；不把预测或存在一份报告当作实际撤权证明。
def retire_for_enable(context: LegacyEnableContext, entry):
    if entry.activation is None:
        return entry, None
    result = deactivate_plugin(context.owner, context.repository, entry,
                               retirement_operation(context.binding.request.operation_id))
    return result.installation, result.report


# LLM: 仅供实际返回记录已与预测目标相等后调用；旧代存在、同撤旧子提交 release 回执与空激活共同证明，不信报告是否存在。
# 函数用途: 判断本次是否真的撤销并释放过原激活，供失败回执区分原表已提交与尚未提交。
def retirement_committed(before, after, operation_id: str) -> bool:
    return (before.activation is not None and after.activation is None and after.last_commit.action == "release"
            and after.last_commit.operation_id == retirement_operation(operation_id))


# LLM: 模式、路径、包、解释器与精确操作同次冻结，只纯计算，不施加 OS 规则。
# 函数用途: 统一生成旧代释放后的目标与新授权，兼容和新模式不得分别走旁路。
def prepare_enable_authorization(context: LegacyEnableContext, entry, runtime):
    identity = authorization_identity(context)
    target = authorization_target(entry, identity.operation_id)
    plan = plan_plugin_environment(target, identity.operation_id,
                                   runtime_fingerprint=runtime.fingerprint if runtime else None)
    grant = permission_details(target, plan, enable_selection(context), enable_mode(entry, context.legacy_sandbox_default))
    return target, plan, grant


# LLM: 与构造时共用同一授权生成器；只读取明确授权根和解释器，不修改安装、不撤旧或启动程序。
# 函数用途: 在执行边界重新取得完整权限事实，拒绝构造后发生的链接、路径或解释器漂移。
def refresh_enable_authorization(context: LegacyEnableContext, entry):
    try:
        runtime = resolve_plugin_runtime(entry.manifest) if entry.manifest.entry else None
        return (runtime, *prepare_enable_authorization(context, entry, runtime))
    except PluginRuntimeError as exc:
        raise PluginInstallationError(exc.reason, "插件运行时已变化，请重新取得完整预览。") from exc
    except PluginInstallationError:
        raise
    except (OSError, ValueError) as exc:
        raise PluginInstallationError("legacy_permission_invalid", "权限路径或授权身份已无效，请重新取得完整预览。") from exc


# LLM: 后续启动/发布不得重新绑定新事实；核对同一原安装预测计划与全部权限，不把检查当作原子 OS 隔离。
# 函数用途: 发现准备期间权限或程序变化时停止本次启用，不自动扩权、换代或重新准备。
def require_enable_authorization(context: LegacyEnableContext, entry, grant) -> None:
    if refresh_enable_authorization(context, entry)[3] != grant:
        raise PluginInstallationError("legacy_permission_changed", "确认后权限事实已变化，本次没有发布新代。")


# LLM: UNKNOWN 原工具结果不能当事实；只观察同表中属于原撤旧子提交的 revoked，不能借当前插件名或后来的激活补原结果。
# 函数用途: 为尚未确认的重新启用显示已经证实的撤权与未验证清理，保持 outcome_unknown 不变。
def unconfirmed_reenable_observation(store, payload: dict) -> dict:
    if payload.get("state") != "outcome_unknown" or payload.get("tool_name") != "plugin_enable":
        return payload
    operation = payload.get("operation_id")
    if not isinstance(operation, str):
        return payload
    try:
        entry = next((row for row in store.snapshot() if row.last_commit.operation_id == retirement_operation(operation)
                      and row.last_commit.action == "revoke"), None)
    except (OSError, ValueError):
        return payload
    if entry is None or entry.activation is None or entry.activation.phase != "revoked":
        return payload
    return {**payload, "details": {"reason": "previous_cleanup_unconfirmed", "commit_state": "unknown",
            "previous_cleanup": {"activation_id": entry.activation_id, "authority_revoked": True,
                                 "released": False, "cleanup_confirmed": False, "cleanup_state": "unverified",
                                 "source": "installation_table"}}}
