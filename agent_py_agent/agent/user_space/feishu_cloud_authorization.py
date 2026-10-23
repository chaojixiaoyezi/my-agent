# LLM: 云资源自助授权只读取当前可信 owner 与飞书实时结构化事实；共享应用 token 不是用户授权，不缓存或写入第二份权限账。
# 模块用途: 核对用户本人、群协作者或准确管理员许可，并把 wiki 定位统一到实际业务资源。
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from ..adapter.feishu_cloud import FeishuCloudError, request_cloud_api
from ..capability.channel_message_target import resolve_message_target
from .resource_grants import resource_operation_allowed


# LLM: 操作元数据是授权、Schema与副作用分类的唯一来源，不包含URL或可由模型覆盖的许可。
# 类用途: 声明一个已实现云动作的资源类型及读写属性。
@dataclass(frozen=True)
class CloudOperation:
    action: str
    resource_type: str
    write: bool


CLOUD_OPERATIONS = {op.action: op for op in (
    CloudOperation('sheet_meta', 'sheet', False),
    CloudOperation('sheet_read', 'sheet', False),
    CloudOperation('sheet_append', 'sheet', True),
    CloudOperation('sheet_update', 'sheet', True),
    CloudOperation('document_read', 'docx', False),
    CloudOperation('document_append', 'docx', True),
    CloudOperation('wiki_resolve', 'wiki', False),
)}
READ_ACTIONS = frozenset(op.action for op in CLOUD_OPERATIONS.values() if not op.write)
WRITE_ACTIONS = frozenset(op.action for op in CLOUD_OPERATIONS.values() if op.write)


# LLM: 映射只来自官方 get_node 返回；模型不能提供 canonical token、owner 或 ACL，原输入仅用于准确显式许可匹配。
# 类用途: 保存当前云操作的原资源与实际业务对象，避免重复解析或把定位结果当成授权。
@dataclass(frozen=True)
class CloudResource:
    resource_type: str
    resource_token: str
    source_type: str
    source_token: str
    wiki_data: dict | None = None


# LLM: 所有对象 ID 走原准确编号校验；wiki 元数据只供宿主内部定位，授权通过前不能返回给模型。
# 函数用途: 用官方节点映射找到同一个实际对象，非 wiki 资源不发请求。
def resolve_cloud_resource(adapter, resource_type: str, token: str, action: str) -> CloudResource:
    if resource_type != 'wiki':
        return CloudResource(resource_type, token, resource_type, token)
    data = request_cloud_api(adapter, 'GET', '/wiki/v2/spaces/get_node?' + urlencode({'token': token}))
    node = data.get('node')
    if not isinstance(node, dict):
        raise FeishuCloudError('CLOUD_RESOURCE_MAPPING_INVALID', '知识库节点未返回可验证的资源映射。')
    kind, canonical = node.get('resource_type'), node.get('resource_id')
    from .resource_grants import normalized_resource_grant

    try:
        normalized_resource_grant({'provider': 'feishu', 'resource_type': kind,
                                  'resource_token': canonical, 'operations': [action]})
    except ValueError:
        raise FeishuCloudError('CLOUD_RESOURCE_MAPPING_INVALID', '知识库节点未返回可验证的资源映射。') from None
    return CloudResource(kind, canonical, 'wiki', token, data)


# LLM: 坏的 owner 策略先失败关闭；显式许可始终是准确 owner/资源/操作，自助模式只接受实时本人或群权限，不回落管理员。
# 函数用途: 为本次调用取得许可来源；不会给用户写入或扩展资源授权。
def authorize_cloud_resource(agent, adapter, resource: CloudResource, action: str, *, collaborator_enabled: bool) -> dict:
    home = agent.home_paths
    mapped_action = _read_action(resource.resource_type) if action == 'wiki_resolve' else action
    if action == 'wiki_resolve' and resource_operation_allowed(home, 'feishu', 'wiki', resource.source_token, action):
        return {'source': 'administrator_grant', 'permission': 'view'}
    if (resource_operation_allowed(home, 'feishu', resource.resource_type, resource.resource_token, mapped_action)
            or (action == 'sheet_meta' and resource_operation_allowed(home, 'feishu', resource.resource_type, resource.resource_token, 'sheet_read'))):
        return {'source': 'administrator_grant', 'permission': 'view' if action in READ_ACTIONS else 'edit'}
    if not collaborator_enabled:
        raise FeishuCloudError('CLOUD_RESOURCE_NOT_AUTHORIZED', '当前用户没有此资源的操作许可；管理员可登记准确资源和操作。')
    provider, member_id, _root, error = resolve_message_target(agent, 'feishu')
    if error or provider != 'feishu' or not member_id:
        raise FeishuCloudError('CLOUD_RESOURCE_PRINCIPAL_UNAVAILABLE', '当前用户没有唯一可核验的飞书身份；请检查身份绑定。')
    member_type = 'openchat' if getattr(home, 'owner_kind', '') == 'group' else 'openid'
    if getattr(home, 'owner_provider', '') == 'feishu':
        bucket = 'groups' if member_type == 'openchat' else 'users'
        if getattr(home, 'owner_id', '') != f'providers/feishu/{bucket}/{member_id}':
            raise FeishuCloudError('CLOUD_RESOURCE_PRINCIPAL_UNAVAILABLE', '当前飞书身份与工作空间归属不一致，未执行云资源操作。')
    permission = _collaborator_permission(adapter, resource, member_type, member_id)
    source = 'feishu_collaborator'
    if not permission and member_type == 'openid' and resource.source_type != 'wiki':
        permission = _owner_permission(adapter, resource, member_id)
        source = 'feishu_owner'
    allowed = {'view', 'edit', 'full_access'} if action in READ_ACTIONS else {'edit', 'full_access'}
    if permission not in allowed:
        raise FeishuCloudError('CLOUD_RESOURCE_NOT_AUTHORIZED',
            '无法证明当前用户或当前群拥有所需文档权限。请在飞书把资源分享给本人或当前群；隐藏/继承权限需明确分享或管理员准确授权。')
    return {'source': source, 'permission': permission, 'principal_type': member_type, 'principal_id': member_id}


# LLM: 这里只把已有动作转换成读许可操作；未知资源不能被 wiki 定位扩展成宿主未实现的读写工具。
# 函数用途: 选择定位结果的准确只读许可，不推断业务内容。
def _read_action(resource_type: str) -> str:
    return {'sheet': 'sheet_read', 'docx': 'document_read'}.get(resource_type, 'wiki_resolve')


# LLM: wiki 必须核验原节点而非底层对象的私人权限；container 和 single_page 都是供应商准确页面范围，不推断父节点或隐藏 ACL。
# 函数用途: 现查本人或群在当前文档/知识库页面的许可，取两种明确页面许可中的较高档。
def _collaborator_permission(adapter, resource: CloudResource, member_type: str, member_id: str) -> str:
    path = '/drive/v1/permissions/' + quote(resource.source_token, safe='') + '/members?'
    queries = [{'type': resource.source_type}]
    if resource.source_type == 'wiki':
        queries = [{'type': 'wiki', 'perm_type': scope} for scope in ('container', 'single_page')]
    permissions = [_member_permission(request_cloud_api(adapter, 'GET', path + urlencode(query)), member_type, member_id)
                   for query in queries]
    ranks = {'': 0, 'view': 1, 'edit': 2, 'full_access': 3}
    return max(permissions, key=ranks.__getitem__)


# LLM: 只匹配准确 member_type/member_id；应用、其它群或群中某个人均不代表当前群，未知与冲突许可失败关闭。
# 函数用途: 从一次官方协作者响应抽取当前主体的明确许可，不向模型返回其它成员身份。
def _member_permission(data: dict, member_type: str, member_id: str) -> str:
    members = data.get('items')
    if not isinstance(members, list) or any(not isinstance(row, dict) for row in members):
        raise FeishuCloudError('CLOUD_RESOURCE_PERMISSION_UNKNOWN', '飞书协作者列表无法核验，未执行云资源操作。')
    matching = [row.get('perm') for row in members
                if row.get('member_type') == member_type and row.get('member_id') == member_id]
    if any(not isinstance(value, str) for value in matching):
        raise FeishuCloudError('CLOUD_RESOURCE_PERMISSION_UNKNOWN', '飞书返回了无效权限，未执行云资源操作。')
    values = set(matching)
    if len(values) > 1 or any(value not in {'view', 'edit', 'full_access'} for value in values):
        raise FeishuCloudError('CLOUD_RESOURCE_PERMISSION_UNKNOWN', '飞书返回了冲突或未知权限，未执行云资源操作。')
    return next(iter(values), '')


# LLM: 仅本人可从官方 owner_id 证明所有者；user_id_type 固定 open_id，且准确对象类型和 token 必须同时相等，群从不借用此路径。
# 函数用途: 处理所有者未出现在协作者列表的情况；只读取元数据，不返回标题、其他成员或私人数据。
def _owner_permission(adapter, resource: CloudResource, member_id: str) -> str:
    body = {'request_docs': [{'doc_token': resource.resource_token, 'doc_type': resource.resource_type}], 'with_url': False}
    data = request_cloud_api(adapter, 'POST', '/drive/v1/metas/batch_query?user_id_type=open_id', body, read_only=True)
    rows = data.get('metas')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise FeishuCloudError('CLOUD_RESOURCE_PERMISSION_UNKNOWN', '飞书未返回可核验的文档所有者，未执行云资源操作。')
    matching = [row for row in rows if row.get('resource_id') == resource.resource_token
                and row.get('doc_type') == resource.resource_type]
    return 'full_access' if len(matching) == 1 and matching[0].get('owner_id') == member_id else ''
