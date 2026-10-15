# LLM: 此入口只由原/skills人类控制路由调用；工具没有authorize/accept能力，owner/actor来自认证scope，不消费模型自称身份。
# 模块用途: 为IM/TUI提供同源清单查看、来源授权/撤销与目标接受，群操作者必须有当次官方成员证明。
from __future__ import annotations

import json

from ..conversation.channels import supports_transcript_delivery
from ..conversation.control_commands import ConversationControlResult
from ..conversation.host_actor import HostActorError
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from ..user_space.owner_resolver import OwnerIdentity
from .skill_learning_store import SkillLearningStoreError
from .skill_transfer import (
    accept_transfer,
    authorize_transfer,
    load_transfer,
    require_transfer_enabled,
)
from .skill_transfer_manifest import SkillTransferError, host_owner, public_manifest


# LLM: 先按原owner解析控制宿主，再查当前人；不能用ref选择控制owner，转发到错误群/个人不会扩大权限。
# 函数用途: 执行显式人类迁移控制，返回准确范围和未验证边界，失败不回显底层异常或私有路径。
def execute_transfer_control(base_agent, scope, parts):
    try:
        if len(parts) != 2 or parts[0] not in {'show', 'authorize', 'accept', 'revoke'}:
            raise SkillTransferError('SKILL_TRANSFER_ARGUMENTS_INVALID')
        from ..gateway_parts.control_service import _request_agent_for_scope
        host = _request_agent_for_scope(base_agent, scope)
        require_transfer_enabled(host)
        actor = _control_actor(host, scope)
        operation, reference = parts
        if operation == 'show':
            _payload, row, manifest, _store = load_transfer(host, reference)
            result = {'ok': True, 'status': row['status'], 'manifest': public_manifest(manifest)}
        elif operation == 'accept':
            result = accept_transfer(host, reference, actor)
        else:
            result = authorize_transfer(host, reference, actor, revoke=operation == 'revoke')
        message = ('已完整接收允许清单；Skill执行仍用本助手原权限，记忆仅进入原待审核候选。' if operation == 'accept'
            else '来源授权已撤回；已接收独立副本保持原有状态。' if operation == 'revoke'
            else '精确清单已授权，请在目标助手用 /skills transfer accept <reference> 接受。' if operation == 'authorize'
            else '以下是当前助手有权核对的精确清单：')
        return ConversationControlResult('skills', True, message + '\n' + json.dumps(result, ensure_ascii=False), details=result)
    except OwnerQuotaExceeded:
        return ConversationControlResult('skills', False, '本助手存储容量不足，本次没有接收完整清单，不消耗成功复刻次数。', error_code='OWNER_DISK_QUOTA_EXCEEDED')
    except OwnerQuotaUnavailable:
        return ConversationControlResult('skills', False, '本助手存储配额暂时不可核验，本次未授权接收。', error_code='OWNER_QUOTA_UNAVAILABLE')
    except SkillTransferError as exc:
        return ConversationControlResult('skills', False, f'操作未完成（{exc.code}）；未绕过双端确认或复制权限。', error_code=exc.code)
    except (SkillLearningStoreError, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        return ConversationControlResult('skills', False, '迁移状态暂时无法确认，请保留原引用查询，勿换编号重复复刻。',
            error_code='SKILL_TRANSFER_UNAVAILABLE')


# LLM: scope已由原Gateway认证；个人owner仍是原本人/管理员绑定，群须同时匹配owner/实际chat且实时成员可证。
# 函数用途: 生成双端grant的真实人类身份，不按名称、角色文字或ref里的actor授权。
def _control_actor(host, scope):
    owner = host_owner(host)
    if owner['owner_kind'] == 'group':
        from ..scheduler.creator_access import current_group_members
        if owner['provider'] != 'feishu':
            raise SkillTransferError('SKILL_TRANSFER_MEMBERSHIP_UNAVAILABLE')
        if scope.channel != owner['provider'] or scope.conversation_id != owner['owner_id']:
            raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
        try:
            members = current_group_members(host, scope.conversation_id)
        except HostActorError:
            raise SkillTransferError('SKILL_TRANSFER_MEMBERSHIP_UNAVAILABLE') from None
        if scope.user_id not in members:
            raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    if owner == {'provider': 'local', 'owner_kind': 'main', 'owner_id': 'main'} and not supports_transcript_delivery(scope.channel):
        from ..user_space.admin_channel_identity import find_admin_channel_identity
        if find_admin_channel_identity(host.home_paths.root, scope.channel, scope.user_id) is None:
            raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    elif owner['owner_kind'] == 'user' and (scope.channel != owner['provider'] or scope.user_id != owner['owner_id']):
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    if not scope.user_id:
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    return {'owner': owner, 'channel': scope.channel, 'actor_id': scope.user_id, 'conversation_id': scope.conversation_id}
