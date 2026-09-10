# LLM: 审批模式只存于既有 owner tool_policy.json；只有认证用户控制入口可改，不接受模型参数或自然语言授权。
# 模块用途: 保存用户默认确认、自主工作或管理员 Full Access 选择；禁用工具、SOUL 确认和用户身份不变。

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from .owner_policy_seed_payloads import default_tool_policy_payload

APPROVAL_MODES = {
    "ask": "默认确认（自己家目录内工作，敏感操作询问）",
    "auto": "自主工作（自己家目录内自动执行，主/子代理不逐次询问）",
    "full-access": "Full Access（仅本机管理员；可访问家目录外，不逐次询问）",
}


# LLM: 只暴露固定错误文案，不能让坏配置回退为 auto 或将私有策略正文带入公开回执。
# 类用途: 表示审批配置无效或不可读；调用者应安全拒绝操作。
class ApprovalModeError(ValueError):
    pass


# LLM: home 由宿主解析；旧文件缺新增可选字段时沿用原 ask 语义，未知 schema/模式和坏 JSON 均拒绝。
# 函数用途: 读取本用户原有工具策略，保留其他字段，不创建文件。
def _policy(home: object) -> dict:
    report = read_json_object_report(Path(home.owner_tool_policy_json), context="owner.approval_mode")
    if report.load_error is not None:
        raise ApprovalModeError("工具审批配置无法读取，未放宽权限。")
    data = report.payload or default_tool_policy_payload()
    mode = data.get("permission_mode", "ask")
    if data.get("schema_version") != "tool-policy.v1" or not isinstance(mode, str) or mode not in APPROVAL_MODES:
        raise ApprovalModeError("工具审批配置无效，未放宽权限。")
    if mode == "full-access" and not is_permission_admin(home):
        raise ApprovalModeError("Full Access 仅本机管理员可用。")
    return data


# LLM: 每个工具边界读取唯一 owner 策略，现有会话与子代理无需重建；不得缓存成另一份授权事实。
# 函数用途: 返回当前用户此刻的审批模式，读取失败由调用方安全拒绝。
def read_approval_mode(home: object) -> str:
    mode = read_permission_mode(home)
    return "auto" if mode in {"auto", "full-access"} else "ask"


# LLM: 管理员身份只认可信解析后的 local/main，空身份、同名 admin 和其他 provider 一律不能提权。
# 函数用途: 判断是否允许展示并选择 Full Access。
def is_permission_admin(home: object) -> bool:
    return getattr(home, "owner_provider", None) == "local" and getattr(home, "owner_kind", None) == "main"


# LLM: 手改文件不能为普通 owner 提权；非法 full 模式须明确报错而不是静默降级。
# 函数用途: 读取经过 owner 身份核验的模式；无用户选择时保留原默认确认。
def read_permission_mode(home: object) -> str:
    return _policy(home).get("permission_mode", "ask")


# LLM: 权限变更在新工作片边界冻结；子代理无条件保持家目录墙，旧部署尚无用户选择时不改原配置。
# 函数用途: 把已确认模式映射到原 access_mode/path_access_mode，不创建第二套路径权限规则。
def permission_config(config: object, home: object, *, inherited: bool = False):
    if not hasattr(home, "owner_tool_policy_json"):
        return config
    data = _policy(home)
    mode = data.get("permission_mode", "ask")
    if "permission_mode" not in data and not inherited:
        return config
    access = "full-access" if mode == "full-access" and not inherited else "workspace-write"
    if getattr(config, "access_mode", "") == "restricted":
        access = "restricted"
    path_mode = "full" if access == "full-access" else "normal"
    if config.access_mode == access and config.path_access_mode == path_mode:
        return config
    return replace(config, access_mode=access, path_access_mode=path_mode)


# LLM: 等待中的请求已通过硬门，模式改变只为原 request 产生精确批准；执行器会重新校验边界，always 仍须本人确认。
# 函数用途: 用户在 F4 菜单开启自主后，主/子代理当前审批可原地续跑，不必逐个点允许。
def autonomous_tool_decision(agent: object, request: ToolApprovalRequest) -> ToolApprovalDecision | None:
    try:
        tools = getattr(getattr(agent, "tools", None), "tools", {})
        tool = tools.get(request.tool_name)
        if tool is None or tool.runtime_policy.approval_policy.mode == "always":
            return None
        if read_approval_mode(agent.home_paths) == "auto":
            return ToolApprovalDecision(request.permission_id, "approved")
    except (OSError, ValueError):
        return ToolApprovalDecision(request.permission_id, "unavailable")
    return None


# LLM: set 只更新审批字段；锁内读改写防止丢失禁用工具等策略；不把自然语言标题作为枚举值。
# 函数用途: 列出或保存本用户的审批模式，保存后适用于本用户的会话和子代理，其他用户不变。
def execute_approval_mode_operation(home: object, operation: str, mode: object = None) -> dict:
    if operation not in {"get", "set"}:
        raise ApprovalModeError("不支持的审批配置操作。")
    if operation == "set":
        if not isinstance(mode, str) or mode not in APPROVAL_MODES:
            raise ApprovalModeError("模式必须为 ask、auto 或 full-access。")
        if mode == "full-access" and not is_permission_admin(home):
            raise ApprovalModeError("Full Access 仅本机管理员可用。")
        path = Path(home.owner_tool_policy_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_path(path):
            data = _policy(home)
            data.update(permission_mode=mode, permission_mode_updated_at=time.time())
            write_json_file_atomic_unlocked(path, data)
            path.chmod(0o600)
    else:
        mode = read_permission_mode(home)
    return {
        "ok": True, "mode": mode, "scope": "current_owner",
        "is_admin": is_permission_admin(home),
        "message": f"已{'保存' if operation == 'set' else '读取'}：{APPROVAL_MODES[mode]}。"
        "审批设置适用于本用户所有会话和子代理；路径权限在下一回合生效，不中断运行。"
        "子代理仍限定家目录；禁用工具、灾难命令保护和 SOUL 等本人确认不变。",
    }
