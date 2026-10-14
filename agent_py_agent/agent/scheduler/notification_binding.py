# LLM: 通知接管只冻结已认证管理员的原会话绑定，版本属于原 job/run；不改 creator、来源、模型或执行权限。
# 模块用途: 校验持久通知接管引用，投递时重新核对原绑定及管理员登录代次。
from __future__ import annotations

import math

from ..conversation.host_actor import HostActorError
from ..user_space.admin_channel_identity import find_admin_channel_identity
from .schedule import ScheduleValidationError

SCHEMA = 'scheduler_notification_binding.v1'
_FIELDS = {'schema_version', 'version', 'owner', 'source_thread_id', 'binding', 'admin_bound_at', 'request_id'}


# LLM: 原账本解析及写入共用严格形状；未知引用不得成为外发地址，owner 仍由原 repository 核对。
# 函数用途: 校验通知接管元数据，缺省表示继续原通知来源。
def normalize_notification_binding(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _FIELDS or value.get('schema_version') != SCHEMA:
        raise ScheduleValidationError('通知接管记录无效')
    binding = value.get('binding')
    if (type(value.get('version')) is not int or value['version'] < 1
            or not isinstance(value.get('owner'), dict)
            or set(value['owner']) != {'provider', 'kind', 'id'}
            or any(not isinstance(item, str) or not item for item in value['owner'].values())
            or not isinstance(binding, dict) or set(binding) != {'channel', 'conversation_id', 'actor_id'}
            or any(not isinstance(item, str) or not item or item != item.strip() for item in binding.values())
            or any(not isinstance(value.get(key), str) or not value[key] for key in ('source_thread_id', 'request_id'))
            or isinstance(value.get('admin_bound_at'), bool)
            or not isinstance(value.get('admin_bound_at'), (int, float))
            or not math.isfinite(value['admin_bound_at'])):
        raise ScheduleValidationError('通知接管记录无效')
    return value


# LLM: 原认证登录代次和 ThreadStore 是投递锚；注销/重绑/重置使旧目标失效，不猜新管理员或借最新绑定。
# 函数用途: 在接管写入和每次投递时核对准确私人通知目标，返回唯一原线程。
def bound_notification_thread(agent, value):
    value = normalize_notification_binding(value)
    home = agent.home_paths
    owner = {'provider': home.owner_provider, 'kind': home.owner_kind, 'id': home.owner_id}
    if value is None or value['owner'] != owner or owner['provider'] != 'local' or owner['kind'] != 'main':
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    binding = value['binding']
    identity = find_admin_channel_identity(home.root, binding['channel'], binding['actor_id'])
    if identity is None or identity.bound_at != value['admin_bound_at']:
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')
    thread = agent.conversation_store.threads.require(value['source_thread_id'])
    if thread.owner_id != owner['id'] or not any(
            row.channel == binding['channel'] and row.channel_user_id == binding['actor_id']
            and row.channel_conversation_id == binding['conversation_id'] for row in thread.channel_bindings):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    resolved, error = agent.conversation_store.threads.resolve_report(channel=binding['channel'],
        channel_conversation_id=binding['conversation_id'], channel_user_id=binding['actor_id'])
    if error or resolved is None or resolved.thread_id != thread.thread_id:
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    return thread
