# LLM: 人工授权只从 Gateway 已认证群 slash 入口进入；模型工具没有授予动作，原 control receipt 负责传输幂等。
# 模块用途: 让当前群主或真实邀请人明确授予/撤销群能力，不等待 LLM 或后台审批。
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

_USAGE = '/group [show]；/group allow <monitors.create|assistant.configure> [member:<open_id>|all-current]；/group revoke <grant_id>'


# LLM: scope 由原认证入口冻结，只有显式 slash 才调用本函数；允许默认精确本人，all-current 必须人明确指定。
# 函数用途: 查看群能力，或由当前群主/邀请人执行一次有 CAS 的人工授予或撤销。
def execute_group_authorization_control(base, command, scope):
    try:
        from .model_profile_service import _scoped_model_host
        host, thread = _scoped_model_host(base, scope)
        bind_group_control_host(base, host, scope, thread.thread_id)
        if not command.valid or host.home_paths.owner_kind != "group":
            raise GroupAuthorizationError("GROUP_AUTHORIZATION_INVALID")
        _data, before = read_group_authorization(host.home_paths)
        parts = command.value.split()
        if not parts or parts == ['show']:
            return _result(True, _render(before), before)
        actor = group_operation_actor(host, thread.thread_id)
        change = _parse_change(parts, before['revision'], actor.actor_id)
        snapshot = current_group_authority(host)
        saved = change_group_authorization(host.home_paths, snapshot, actor, change)
        return _result(True, '已保存群能力授权。\n' + _render(saved), saved)
    except GroupAuthorizationError as exc:
        return _result(False, str(exc), code=exc.error_code)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return _result(False, '群授权操作无法确认，未自动重试。\n' + _USAGE, code='GROUP_AUTHORIZATION_UNAVAILABLE')


# LLM: 仅解析固定控制语法与精确 provider ID，不解析普通中文提示词、姓名或模型说明产生权限。
# 函数用途: 将人工命令转换为结构化授权意图，缺省目标为当前操作者本人。
def _parse_change(parts, revision, actor_id):
    if len(parts) == 2 and parts[0] == 'revoke':
        return GroupAuthorizationChange('revoke', revision, grant_id=parts[1])
    if parts[0] != 'allow' or len(parts) not in {2, 3}:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    target = parts[2] if len(parts) == 3 else 'member:' + actor_id
    if target != 'all-current' and (not target.startswith('member:') or not target.removeprefix('member:')):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    return GroupAuthorizationChange('allow', revision, capability=parts[1],
        subject_id='' if target == 'all-current' else target.removeprefix('member:'), all_current=target == 'all-current')


# LLM: 投影只来自原策略，不包含配置、凭据或当前成员名单；显示状态不代替每次副作用前复核。
# 函数用途: 告知用户版本、准确 grant_id 和范围，明确默认不授权与新成员不自动获得。
def _render(block):
    rows = [f"群授权版本：{block['revision']}。默认未授权；all-current 只覆盖授予当时成员。"]
    rows.extend(f"{row['grant_id']} {row['capability']} {row['status']} 成员数:{len(row['subjects'])}"
                for row in block['grants'])
    return '\n'.join(rows) + '\n' + _USAGE


# LLM: 与其它控制命令使用同一结果协议，错误码固定；details 只是已完成原策略操作的投影。
# 函数用途: 返回确定性人工控制回执。
def _result(ok, message, block=None, *, code=''):
    return ConversationControlResult('group', ok, message, error_code=code,
        details={'revision': block['revision'], 'grant_ids': [x['grant_id'] for x in block['grants']]} if block else None)
