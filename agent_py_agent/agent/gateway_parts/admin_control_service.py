# LLM: /admin、/approve、/deny 的唯一 Gateway 执行入口。作用域只信任已认证 scope 的 channel/user_id/会话和 adapter 给出的
#   私聊类型；密码只在 command.value 里用于一次校验，结果、日志与回执都不带它（回执正文已由 control_operation_service 脱敏）。
#   /approve、/deny 只把决定写进既有 permission bridge 的精确决定文件：owner、会话、请求号、permission_id 逐项核对，
#   由原等待方再核对 binding 后继续；本模块不直接执行或跳过工具。改动时同步 control_service 分派、request_worker 的
#   管理员身份匹配和 test_admin_identity_gateway.py。
# 模块用途: 让管理员在飞书等 IM 私聊里用密码绑定管理员身份，并对本会话当前唯一等待确认的工具操作做一次批准或拒绝。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import jsonl_lines
from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from ..conversation.channels import project_host_paths_for_channel, project_user_reply
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    control_command_carries_secret,
)
from ..user_space.admin_channel_identity import (
    AdminChannelIdentity,
    AdminIdentityStoreError,
    bind_admin_channel_identity,
    find_admin_channel_identity,
    remove_admin_channel_identity,
)
from ..user_space.admin_password import (
    AdminPasswordError,
    admin_password_refusal_message,
    verify_admin_password,
)
from .control_service import (
    GatewayControlScope,
    _live_window_requests,
    _record_id,
    _scope_request_payload,
    resolve_gateway_scope_owner,
)
from .paths import GatewayPaths, claimed_request_chunk_path
from .permission_bridge import gateway_permission_decision_path, write_gateway_permission_decision

_SUMMARY_MAX_CHARS = 200
_BASE_CHANNEL_REFUSAL = (
    "本机终端已经是管理员；/admin、/approve、/deny 只用于飞书等 IM 私聊，终端里的工具确认请直接在审批面板中选择。"
)
_PRIVATE_ONLY_REFUSAL = "/admin、/approve、/deny 只能在与机器人的一对一私聊中使用，群聊不支持。"
_DISABLED_REFUSAL = "本服务没有启用 IM 管理员身份（需要本机主用户部署，且 admin_channel_identity_enabled 为 true）。"
_STORE_UNAVAILABLE = "管理员身份记录暂时无法读写，没有做任何更改；请本机管理员检查 my-agent 的 config 目录。"
_NOT_BOUND = "本私聊还没有绑定管理员身份，不能批准；请先发送 /admin <管理员密码> 完成验证。"
_NO_PENDING = "本会话当前没有等待确认的操作。"
_AMBIGUOUS = "本会话有多个等待确认的操作，系统不猜测处理哪一个；请先用 /stop 结束当前回合。"
_LOGIN_DONE = (
    "已验证管理员身份：本私聊之后按本机管理员运行（使用本机主用户的会话空间、记忆和权限设置），"
    "需要确认的工具操作会在这里请你用 /approve 批准。"
)
_LOGOUT_DONE = "已解除本私聊的管理员身份，之后回到你自己的用户空间。"
_LOGOUT_NOOP = "本私聊本来就没有绑定管理员身份。"
_DELETE_HINT = "聊天软件会保留原消息，建议撤回或删除刚才含密码的那条消息。"
_EXPOSED_HINT = "如果刚才的消息里有管理员密码，请立即撤回，并在本机用 my-agent admin-password set 更换密码。"


# LLM: 三个字段一起冻结一条待决审批的精确身份；decision 必须写到由 chunk_path 与 request 推导的唯一文件。
# 类用途: 表示本会话活动回合里一条仍在等待决定的工具审批。
@dataclass(frozen=True)
class PendingToolApproval:
    request_id: str
    chunk_path: Path
    request: ToolApprovalRequest


# LLM: 先核作用域（非本机通道、功能开启、一对一私聊），再按 kind 分派；/approve 另需当前身份已绑定管理员并校验密码，
#   /deny 不需要密码但同样只作用于本会话自己的待决审批。返回值只含固定文案、错误码和目标请求号。
# 函数用途: 执行一条 /admin、/approve 或 /deny 控制命令。
def execute_admin_channel_control(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    identity, refusal = _private_admin_scope(agent, command, scope)
    if refusal is not None:
        return refusal
    home_root = getattr(getattr(agent, "home_paths", None), "root", None)
    if not home_root:
        return _refusal(command.kind, _STORE_UNAVAILABLE, "ADMIN_IDENTITY_STORE_UNAVAILABLE")
    if command.kind == "admin":
        return _execute_admin_identity(home_root, command, identity)
    if command.kind == "approve" and find_admin_channel_identity(home_root, *identity) is None:
        return _refusal("approve", _NOT_BOUND, "ADMIN_IDENTITY_NOT_BOUND")
    pending = pending_tool_approvals(agent, paths, scope)
    if not pending:
        return _refusal(command.kind, _NO_PENDING, "APPROVAL_NOT_PENDING")
    if len(pending) > 1:
        return _refusal(command.kind, _AMBIGUOUS, "APPROVAL_AMBIGUOUS")
    return _decide_pending_approval(home_root, command, identity, pending[0])


# LLM: 只读：从本会话仍开放的精确前台回合里找未决审批；请求 owner 必须与控制作用域 owner 一致，请求号必须等于回合号，
#   只认本次认领（lease_started_at）之后发布的请求——Gateway 重启前旧执行留下的请求已没有等待方；
#   已写过决定文件（等待方尚未消费）的不再算待决。不按正文或工具名猜目标。
# 函数用途: 列出本会话当前等待 /approve 或 /deny 的工具审批。
def pending_tool_approvals(
    agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> list[PendingToolApproval]:
    from .request_worker import OwnerScopeUnavailableError, _resolve_request_owner_identity

    try:
        scope_owner = resolve_gateway_scope_owner(agent, scope)
    except OwnerScopeUnavailableError:
        return []
    pending: list[PendingToolApproval] = []
    for record in _live_window_requests(paths, scope):
        request_id = _record_id(record)
        try:
            request_owner = _resolve_request_owner_identity(agent, record.payload)
        except OwnerScopeUnavailableError:
            continue
        if not request_id or record.path is None or request_owner != scope_owner:
            continue
        chunk_path = claimed_request_chunk_path(record.path, request_id)
        pending.extend(
            PendingToolApproval(request_id, chunk_path, request)
            for request in _unresolved_permission_requests(chunk_path, _lease_started_at(record.payload))
            if request.request_id == request_id
            and not gateway_permission_decision_path(chunk_path, request).exists()
        )
    return pending


# LLM: 基础通道（TUI/CLI/HTTP）直接拒绝，功能关闭或 base owner 不是 local/main 时拒绝，缺少显式私聊类型或群聊时拒绝；
#   身份只取 request_worker.private_channel_identity 的结构化结果。群聊等场合带着密码被拒时额外提醒撤回并更换密码。
# 函数用途: 确认命令来自允许的 IM 一对一私聊，并返回该私聊的 (channel, user_id)。
def _private_admin_scope(
    agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> tuple[tuple[str, str], ConversationControlResult | None]:
    from .request_worker import (
        _BASE_OWNER_CHANNELS,
        admin_channel_identity_enabled,
        private_channel_identity,
    )

    kind = command.kind
    channel = str(scope.channel or "").strip().casefold()
    if not channel or channel in _BASE_OWNER_CHANNELS:
        return ("", ""), _refusal(kind, _BASE_CHANNEL_REFUSAL, "ADMIN_IDENTITY_SCOPE_INVALID")
    if not admin_channel_identity_enabled(agent):
        return ("", ""), _refusal(kind, _DISABLED_REFUSAL, "ADMIN_IDENTITY_SCOPE_INVALID")
    identity = private_channel_identity(_scope_request_payload(scope))
    if identity is None:
        exposed = _EXPOSED_HINT if control_command_carries_secret(command) else ""
        return ("", ""), _refusal(kind, _PRIVATE_ONLY_REFUSAL + exposed, "ADMIN_IDENTITY_SCOPE_INVALID")
    return identity, None


# LLM: 有副作用：login 成功写绑定文件、logout 删除绑定、status 只读；密码校验失败或存储损坏都不改绑定。
# 函数用途: 执行 /admin 的登录、查看和解除三种操作。
def _execute_admin_identity(
    home_root: object,
    command: ConversationControlCommand,
    identity: tuple[str, str],
) -> ConversationControlResult:
    if command.operation == "status":
        return ConversationControlResult("admin", True, _status_message(find_admin_channel_identity(home_root, *identity)))
    try:
        if command.operation == "logout":
            removed = remove_admin_channel_identity(home_root, *identity)
            return ConversationControlResult("admin", True, _LOGOUT_DONE if removed else _LOGOUT_NOOP)
        refusal = _password_refusal(home_root, command, identity)
        if refusal is not None:
            return refusal
        bind_admin_channel_identity(home_root, *identity)
    except AdminIdentityStoreError:
        return _refusal("admin", _STORE_UNAVAILABLE, "ADMIN_IDENTITY_STORE_UNAVAILABLE")
    return ConversationControlResult("admin", True, _LOGIN_DONE + _DELETE_HINT)


# LLM: 有副作用：/approve 先校验密码（失败计入节流），通过后与 /deny 一样只原子写一份精确决定文件；原等待方负责消费并执行或拒绝。
# 函数用途: 对唯一的待决审批写入“仅本次批准”或“拒绝”。
def _decide_pending_approval(
    home_root: object,
    command: ConversationControlCommand,
    identity: tuple[str, str],
    target: PendingToolApproval,
) -> ConversationControlResult:
    approving = command.kind == "approve"
    if approving:
        refusal = _password_refusal(home_root, command, identity)
        if refusal is not None:
            return refusal
    decision = ToolApprovalDecision(target.request.permission_id, "approved" if approving else "denied")
    write_gateway_permission_decision(target.chunk_path, target.request, decision)
    summary = _approval_summary(target.request, identity[0])
    message = f"已批准本次操作：{summary}。{_DELETE_HINT}" if approving else f"已拒绝本次操作：{summary}。"
    return ConversationControlResult(command.kind, True, message, request_id=target.request_id)


# LLM: 节流键是可信渠道身份；节流或密码存储不可用时按拒绝处理（不放行），文案不区分密码错与锁定。
# 函数用途: 校验命令里的管理员密码，通过返回 None，否则返回拒绝结果。
def _password_refusal(
    home_root: object,
    command: ConversationControlCommand,
    identity: tuple[str, str],
) -> ConversationControlResult | None:
    try:
        check = verify_admin_password(home_root, command.value, attempt_key=":".join(identity))
    except (AdminPasswordError, OSError):
        return _refusal(command.kind, _STORE_UNAVAILABLE, "ADMIN_IDENTITY_STORE_UNAVAILABLE")
    if check.ok:
        return None
    return _refusal(command.kind, admin_password_refusal_message(check), "ADMIN_PASSWORD_REJECTED")


# LLM: 只认 chunk 流里结构化的 permission_requested / permission_resolved 事件并按 permission_id 抵消；早于 since
#   （本次认领时刻）的行属于已结束的旧执行，跳过。坏行跳过，请求对象必须通过 ToolApprovalRequest 校验；读不到流文件时视为没有。
# 函数用途: 从一条回合的事件流里找出本次执行中还没有结果的审批请求。
def _unresolved_permission_requests(chunk_path: Path, since: float) -> list[ToolApprovalRequest]:
    try:
        lines = jsonl_lines(chunk_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return []
    requested: dict[str, ToolApprovalRequest] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(row, dict) or _event_time(row) < since:
            continue
        if row.get("kind") == "permission_resolved":
            requested.pop(str(row.get("permission_id") or ""), None)
        elif row.get("kind") == "permission_requested" and isinstance(row.get("permission"), dict):
            try:
                request = ToolApprovalRequest.from_mapping(row["permission"])
            except (TypeError, ValueError):
                continue
            requested[request.permission_id] = request
    return list(requested.values())


# LLM: 认领时刻与事件时间都由同一台 Gateway 写入；缺失或坏值按 0 处理（旧请求不做过滤、坏事件视为最旧）。
# 函数用途: 读取请求本次被认领执行的时刻。
def _lease_started_at(payload: dict) -> float:
    try:
        return max(0.0, float(payload.get("lease_started_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# LLM: chunk 事件由 write_chunk_event 写入 t 字段；缺失或坏值按 0 处理。
# 函数用途: 读取一条流事件的写入时刻。
def _event_time(row: dict) -> float:
    try:
        return float(row.get("t") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: 摘要只用审批请求里已脱敏的 description，再按渠道收敛宿主路径并限长；不读工具参数原文。
# 函数用途: 生成回复里展示的工具操作摘要。
def _approval_summary(request: ToolApprovalRequest, channel: str) -> str:
    projected = project_host_paths_for_channel(project_user_reply(request.description or request.tool_name).content, channel)
    text = " ".join(projected.split()) or request.tool_name
    return text if len(text) <= _SUMMARY_MAX_CHARS else text[: _SUMMARY_MAX_CHARS - 1] + "…"


# LLM: 只展示绑定时间，不展示其他绑定身份。
# 函数用途: 生成 /admin status 的回复文案。
def _status_message(bound: AdminChannelIdentity | None) -> str:
    if bound is None:
        return "本私聊未绑定管理员身份；发送 /admin <管理员密码> 可以绑定。"
    bound_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(bound.bound_at))
    return f"本私聊已绑定管理员身份（{bound_at} 绑定），按本机管理员运行；发送 /admin logout 可以解除。"


# LLM: 拒绝结果 ok=False 并带稳定错误码，客户端按错误码区分，不解析文案。
# 函数用途: 构造一条拒绝结果。
def _refusal(kind: str, message: str, error_code: str) -> ConversationControlResult:
    return ConversationControlResult(kind, False, message, error_code=error_code)


__all__ = [
    "PendingToolApproval",
    "execute_admin_channel_control",
    "pending_tool_approvals",
]
