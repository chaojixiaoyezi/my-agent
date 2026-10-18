# LLM: 精确资源治理写入先消费原全局拒门；读取及真实资源裁决保持原owner策略，不从进程模式签权。
# 模块用途: 拦截受限进程直接登记/撤销资源许可，默认管理员身份、准确资源及锁内合并保持。
# LLM: 受管外部资源授权复用 owner 的 tool_policy.json、管理员身份和原文件锁；正文、URL 和模型角色不能授予权限。
# 模块用途: 管理员为用户登记准确的外部资源和操作，宿主工具每次执行前读取，不复制密钥。
from __future__ import annotations

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic_unlocked,
)
from .approval_mode import is_permission_admin

_FIELD = 'resource_grants'
_SCHEMA = 'owner_resource_grants.v1'


# LLM: 仅接受结构化准确资源，不支持通配符；操作名是开放的合同标识，具体工具负责其操作语义。
# 函数用途: 校验一条不包含凭据的授权记录，拒绝空值和多余字段。
def normalized_resource_grant(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {'provider', 'resource_type', 'resource_token', 'operations'}:
        raise ValueError('授权需包含 provider、resource_type、resource_token 和 operations。')
    result = {}
    for key in ('provider', 'resource_type', 'resource_token'):
        item = value[key]
        if not isinstance(item, str) or not item or len(item) > 256 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in item):
            raise ValueError('资源身份只能使用准确编号，不支持 URL、路径或通配符。')
        result[key] = item
    operations = value['operations']
    if not isinstance(operations, list) or not operations or any(not isinstance(x, str) or not x or len(x) > 80
            or not all(c.isascii() and (c.isalnum() or c in '_-') for c in x) for x in operations):
        raise ValueError('operations 需为非空的准确操作编号列表。')
    return {**result, 'operations': sorted(set(operations))}


# LLM: 每次从原受保护策略读取；坏文件、块或记录失败关闭，缺块表示没有授权，不创建新状态。
# 函数用途: 读取用户当前可使用的外部资源名单。
def read_resource_grants(home: object) -> list[dict]:
    report = read_json_object_report(home.owner_tool_policy_json, context='owner.resource_grants.read')
    if report.load_error or report.payload.get('schema_version') != 'tool-policy.v1':
        raise ValueError('用户工具策略不可读取，未调用外部资源。')
    block = report.payload.get(_FIELD)
    if block is None:
        return []
    if not isinstance(block, dict) or block.get('schema') != _SCHEMA or not isinstance(block.get('items'), list):
        raise ValueError('外部资源授权记录无效，未调用外部资源。')
    return [normalized_resource_grant(item) for item in block['items']]


# LLM: 只匹配当前真实 owner 的准确资源与操作；管理员和 Full Access 不隐式跳过资源门。
# 函数用途: 判断本次宿主调用是否已有管理员登记的资源许可。
def resource_operation_allowed(home: object, provider: str, resource_type: str, token: str, operation: str) -> bool:
    return any(row['provider'] == provider and row['resource_type'] == resource_type
               and row['resource_token'] == token and operation in row['operations'] for row in read_resource_grants(home))


# LLM: 调用方先沿原 owner resolver 选择目标；锁内只改 resource_grants，其它审批/控制字段原样保存，写入为 600。
# 函数用途: 管理员登记或撤销准确资源授权，普通用户不能为自己加权限。
# LLM: 必须在任何验证、读取、目录或锁副作用前拒绝受限进程；门通过后原权限和事务正文不变，同步检查全局写边界回归。
# 函数用途: 先阻止Worker/受限宿主直接修改本入口的治理事实，默认单进程继续原校验与写入。
def set_resource_grant(admin_home: object, target_home: object, value: object, *, revoke: bool = False) -> list[dict]:
    from ..common.global_write_guard import require_global_writer
    require_global_writer('owner_resource_grants')

    if not is_permission_admin(admin_home):
        raise PermissionError('只有管理员能登记外部资源授权。')
    grant = normalized_resource_grant(value)
    path = target_home.owner_tool_policy_json
    with locked_json_path(path):
        rows = read_resource_grants(target_home)
        report = read_json_object_report(path, context='owner.resource_grants.write')
        if report.load_error:
            raise ValueError('用户工具策略不可读取，未修改。')
        key = ('provider', 'resource_type', 'resource_token')
        rows = [row for row in rows if any(row[k] != grant[k] for k in key)]
        if not revoke:
            rows.append(grant)
        payload = {**report.payload, _FIELD: {'schema': _SCHEMA, 'items': rows}}
        write_private_json_file_atomic_unlocked(path, payload)
    return rows
