# LLM: 插件目录沿宿主已解析身份建立；这里不读取 argv 身份、不创建安装表，也不初始化插件或完整 Agent。
# 模块用途: 为本地与 Gateway 命令提供同一个只读目录和版本检查，过期输入不自动重放。

from __future__ import annotations

import hashlib
import json

from .plugin_command_catalog import PluginCommandCatalog
from .plugin_commands import plugin_command_response
from .user_space.owner_resolver import OwnerIdentity


# LLM: owner 必须由现有宿主解析器提供；引用只绑定客户端所见作用域，不是凭证，不用于推导 canonical thread。
# 函数用途: 为当前用户与会话生成稳定的命令目录，不读取磁盘或启动后台服务。
def read_plugin_catalog(
    owner: OwnerIdentity,
    *,
    channel: str,
    conversation_id: str,
) -> PluginCommandCatalog:
    identity = (owner.provider, owner.owner_kind, owner.owner_id, channel, conversation_id)
    scope_ref = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    return PluginCommandCatalog(scope_ref)


# 目录只读失败保留异常类型、结构化原因和恢复动作；未知异常正文不进入公开回执。
def plugin_catalog_unavailable(error: Exception | None = None, *, phase: str = 'catalog_read') -> dict[str, object]:
    """目录读取无执行副作用；仅暴露类型及结构化原因，不回显未知异常中的路径或密钥。"""
    code = str(getattr(error, 'error_code', '') or 'PLUGIN_CATALOG_UNAVAILABLE')
    reason = str(getattr(error, 'reason', '') or 'catalog_unavailable')
    if reason == 'invalid_state':
        code = 'PLUGIN_INSTALLATION_STATE_INVALID'
    details = {'phase': phase, 'exception_type': type(error).__name__ if error else 'GatewayUnavailable',
               'source_reason': reason}
    if error is not None and error.__cause__ is not None:
        details['cause_type'] = type(error.__cause__).__name__
    if code == 'GATEWAY_WORKSPACE_INVALID':
        explanation = str(error)
        next_action = '用 /dir 切换到本人可访问的现存目录后重新执行原插件命令；普通用户不能使用家目录外路径。'
    elif reason == 'invalid_state':
        explanation = '插件安装登记表损坏或格式不合法，原记录未被覆盖。'
        next_action = '请管理员检查本人用户目录内 data/plugins/installations.json 及其备份，修复后重新读取；不要删除有效安装记录。'
    else:
        explanation = f'插件目录在 {phase} 阶段不可读（{details["exception_type"]}）。'
        next_action = '核对 Gateway 是否就绪及当前身份的插件目录读取权限，再用 /plugins list 重新查询；未提交安装或启停。'
    return {
        "kind": "plugin_command",
        "ok": False,
        "request_id": "",
        "error_code": code,
        "reason": reason,
        "state": "rejected", "effect_outcome": "not_started", "details": details,
        "message": f'{code}：{explanation} 原因类型：{details["exception_type"]}。'
                   f'未执行安装或启停。下一步：{next_action}',
    }


# LLM: 传输失败不能证明宿主未执行；只保留客户端的稳定请求标识，不根据错误正文判断提交结果。
# 函数用途: 提示用户查询原请求，避免回包丢失后重复提交安装。
def plugin_command_unknown(request_id: str) -> dict[str, object]:
    return {
        "kind": "plugin_command", "ok": False, "state": "outcome_unknown",
        "request_id": request_id, "error_code": "PLUGIN_COMMAND_OUTCOME_UNKNOWN",
        "message": "未能确认插件请求结果。" + (
            f"请查询原请求：/plugins status {request_id}" if request_id else "请核对原请求编号后查询。"
        ),
    }


# LLM: revision 只做声明一致性检查，不授予执行权；未来装卸必须在原生命周期事务里再次核对，不能把此只读检查当提交锁。
# 函数用途: 按当前目录解释命令，旧版本或缺失业务版本明确拒绝，并返回可供重新查看的目录。
def execute_plugin_command(
    catalog: PluginCommandCatalog,
    text: str,
    *,
    revision: str = "",
) -> dict[str, object] | None:
    result = plugin_command_response(
        text, plugins=catalog.plugins, management_actions=catalog.management_actions
    )
    if result is None:
        return None
    missing_revision = not revision and result.get("reason") == "not_implemented"
    if (revision and revision != catalog.revision) or missing_revision:
        result = {
            "kind": "plugin_command",
            "ok": False,
            "request_id": "",
            "error_code": "PLUGIN_CATALOG_STALE",
            "reason": "stale_catalog" if revision else "missing_revision",
            "message": "插件目录已经变化或尚未读取；请重新查看帮助或按 Tab 后确认输入，本次没有执行操作。",
        }
    return {**result, "catalog": catalog.to_payload()}
