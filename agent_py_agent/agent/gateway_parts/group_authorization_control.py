# LLM: 人工授权只从 Gateway 已认证群 slash 入口进入；模型工具没有授予动作，原 control receipt 负责传输幂等。
# 模块用途: 让群主/已证邀请人明确开放监控，并在IM回执显示固定安全失败阶段和代码；其它功能不加该门。
from __future__ import annotations

from ..conversation.control_commands import ConversationControlResult
from ..user_space.group_authorization import (
    GroupAuthorizationChange,
    GroupAuthorizationError,
    change_group_authorization,
    read_group_authorization,
)
from ..user_space.group_authorization_context import (
    bind_group_control_host,
    current_group_authority,
    group_operation_actor,
)

_USAGE = '/group 监控 @某人；/group 监控 全群（含后续成员）；/group 查看；撤销：/group revoke <授权编号>'


# LLM: 仅认证slash调用；@目标由入站按可信mentions规范为精确member:ID，不猜显示名；全群明确含未来实际成员。
# 函数用途: 查看或由当前群主/邀请人明确授权监控，保留原CAS/撤销与历史English快照语义。
def execute_group_authorization_control(base, command, scope):
    try:
        from .model_profile_service import _scoped_model_host
        host, thread = _scoped_model_host(base, scope)
        bind_group_control_host(base, host, scope, thread.thread_id)
        if not command.valid or host.home_paths.owner_kind != "group":
            raise GroupAuthorizationError("GROUP_AUTHORIZATION_INVALID")
        _data, before = read_group_authorization(host.home_paths)
        parts = command.value.split()
        if not parts or parts in (['show'], ['查看']):
            return _result(True, _render(before), before)
        actor = group_operation_actor(host, thread.thread_id)
        change = _parse_change(parts, before['revision'], actor.actor_id)
        snapshot = current_group_authority(host)
        saved = change_group_authorization(host.home_paths, snapshot, actor, change)
        return _result(True, '已保存群监控授权。\n' + _render(saved), saved)
    except GroupAuthorizationError as exc:
        return _result(False, str(exc), code=exc.error_code, diagnostics=exc.diagnostics)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return _result(False, '群授权操作无法确认，未自动重试。\n' + _USAGE, code='GROUP_AUTHORIZATION_UNAVAILABLE')


# LLM: 仅解析固定slash控制与宿主已规范的member:ID；全群含未来成员，旧all-current仍只快照，不接受其它能力。
# 函数用途: 将中文监控命令或既有English入口变为明确授权范围，拒绝自由姓名和未解析的@文本。
def _parse_change(parts, revision, actor_id):
    if len(parts) == 2 and parts[0] == 'revoke':
        return GroupAuthorizationChange('revoke', revision, grant_id=parts[1])
    if len(parts) == 2 and parts[0] == '监控':
        target = parts[1]
        capability = 'monitors.create'
    elif len(parts) in {2, 3} and parts[0] == 'allow' and parts[1] == 'monitors.create':
        target = parts[2] if len(parts) == 3 else '全群'
        capability = parts[1]
    else:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    if target == '全群':
        return GroupAuthorizationChange('allow', revision, capability, group_wide=True)
    if target != 'all-current' and (not target.startswith('member:') or not target.removeprefix('member:')):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    return GroupAuthorizationChange('allow', revision, capability=capability,
        subject_id='' if target == 'all-current' else target.removeprefix('member:'), all_current=target == 'all-current')


# LLM: 仅展示原策略监控记录；旧assistant配置grant保持底账可读/可精确撤销，但不进入新群监控列表。
# 函数用途: 显示授权编号及明确范围，整群包含后续成员，历史成员快照不被误写成整群。
def _render(block):
    rows = [f"群监控授权版本：{block['revision']}。只影响监控，普通聊天及其它功能不受此授权限制。"]
    rows.extend(f"{row['grant_id']} {row['capability']} {row['status']} "
                + ('全群当前及后续成员' if row.get('scope') == 'group' else f"成员快照:{len(row['subjects'])}人（不含后续成员）")
                for row in block['grants'] if row['capability'] == 'monitors.create')
    return '\n'.join(rows) + '\n' + _USAGE


# LLM: provider诊断再次通过原唯一白名单净化；仅错误控制回执显示阶段/固定码/数值码，不输出异常正文、响应或ID。
# 函数用途: 让只显示message的IM客户端也能定位失败，成功回执不变，传输/分页失败不误要求重新授权。
def _result(ok, message, block=None, *, code='', diagnostics=None):
    details = {'revision': block['revision'], 'grant_ids': [x['grant_id'] for x in block['grants']]} if block else {}
    if diagnostics:
        from ..adapter.feishu_provider_facts import FeishuProviderFactError

        safe = FeishuProviderFactError(diagnostics.get('reason_code'), diagnostics.get('provider_code'),
            stage=diagnostics.get('stage')).safe_diagnostics()
        details['provider_diagnostics'] = safe
        if not ok:
            message = _provider_failure_message(safe)
    return ConversationControlResult('group', ok, message, error_code=code,
        details=details or None)


# LLM: 仅将provider安全投影中的固定阶段和原因映射为显式控制说明，不能据自由正文决定权限或申请新scope。
# 函数用途: 格式化中文阶段和代码，只有已核scope_required才建议检查群读取权限，其他失败保持中性排查。
def _provider_failure_message(safe):
    labels = {'chat_info': '群信息', 'bot_membership': '机器人在群', 'members': '群成员',
        'auth': '应用认证/token', 'registry': '适配器注册', 'response_validation': '响应校验', 'message': '来源消息'}
    stage = labels.get(safe['stage'], '群事实核验')
    provider = f"；供应商代码：{safe['provider_code']}" if safe['provider_code'] is not None else ''
    hint = ('请核对应用的群读取权限及发布状态。' if safe['reason_code'] == 'FEISHU_PROVIDER_SCOPE_REQUIRED'
        else '本次核验未完成，请按诊断排查或稍后重试；不代表需要重新授予群监控权限。')
    return f"群监控授权未更改。失败阶段：{stage}；原因：{safe['reason_code']}{provider}。\n{hint}"
