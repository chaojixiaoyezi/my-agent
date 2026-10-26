# LLM: 只解析显式/delivery，文本不产生身份或管理员授权；执行身份必须由已认证宿主单独注入。
# 模块用途: 为跨用户资产投递提供纯语法解析和复用原生工具合同的会话控制处理器。
from __future__ import annotations

import json
import re
import shlex

from .control_commands import ConversationControlCommand, ConversationControlResult

_USAGE = ('用法：/delivery inbox|targets；preview <kind>:<id>；send <owner> <kind>:<id> [note]；'
    'accept <delivery_id> [keep_both_rename|overwrite|skip]；reject <delivery_id> [reason]；'
    'enable|disable <kind>:<id>；block|unblock <owner>。接收默认关闭，不自动执行。')


# LLM: kind与ID保持封闭且分离，合法ID不能携带路径或伪装Owner来源。
# 函数用途: 解析四类资产的明确kind:id。
def _asset_pair(value):
    from ..assets import safe_asset_id

    kind, separator, identifier = value.partition(':')
    if not separator or kind not in {'script', 'workflow', 'monitor', 'skill'}:
        raise ValueError('资产格式无效')
    return {'asset_kind': kind, 'asset_id': safe_asset_id(identifier)}


# LLM: 参数只映射现有八动作；拒绝尾随多参数、未知冲突策略和非规范Owner，不开放角色或自动启用。
# 函数用途: 将用户显式slash参数转换成唯一工具参数合同。
def _delivery_params(parts):
    from ..assets import safe_asset_id
    from ..assets.delivery_service import _identity

    if not parts:
        raise ValueError('缺少动作')
    operation, args = parts[0], parts[1:]
    if operation in {'inbox', 'targets'} and not args:
        return {'action': operation}
    if operation in {'preview', 'enable', 'disable'} and len(args) == 1:
        return {'action': 'toggle' if operation != 'preview' else 'preview', **_asset_pair(args[0]),
            **({'enabled': operation == 'enable'} if operation != 'preview' else {})}
    if operation == 'send' and len(args) >= 2:
        _identity(args[0])
        return {'action': 'send', 'recipient_owner_id': args[0], **_asset_pair(args[1]), 'note': ' '.join(args[2:])}
    if operation == 'accept' and 1 <= len(args) <= 2:
        strategy = args[1] if len(args) == 2 else 'keep_both_rename'
        if strategy not in {'keep_both_rename', 'overwrite', 'skip'}:
            raise ValueError('未知冲突策略')
        return {'action': 'accept', 'delivery_id': safe_asset_id(args[0]), 'conflict_strategy': strategy}
    if operation == 'reject' and args:
        return {'action': 'reject', 'delivery_id': safe_asset_id(args[0]), 'reason': ' '.join(args[1:])}
    if operation in {'block', 'unblock'} and len(args) == 1:
        _identity(args[0])
        return {'action': 'blocklist', 'operation': 'add' if operation == 'block' else 'remove', 'target_owner_id': args[0]}
    raise ValueError('参数不符合用法')


# LLM: 纯解析不创建服务、锁或存储；仅完整/delivery命名空间匹配，伪前缀和普通文本保持原路由。
# 函数用途: 将一个明确的投递命令解析成即时控制对象。
def parse_delivery_command(text):
    match = re.fullmatch(r'/delivery(?:\s+(.*))?', str(text or '').strip(), re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    try:
        params = _delivery_params(shlex.split(match.group(1) or ''))
        if any(len(params.get(key, '')) > 4000 for key in ('note', 'reason')):
            raise ValueError('备注或原因过长')
        return ConversationControlCommand('delivery', value=json.dumps(params, ensure_ascii=False),
            operation=params['action'], usage=_USAGE)
    except (ValueError, TypeError):
        return ConversationControlCommand('delivery', valid=False, usage=_USAGE)


# LLM: root_dir/owner_id只接受宿主绑定，command.value仍经原生封闭Schema复核；未知副作用不伪装为成功。
# 函数用途: 执行一个已解析的显式投递控制命令，返回公共会话控制回执。
def execute_delivery_control(root_dir, owner_id, command):
    from ..tooling.asset_delivery_tool import AssetDeliveryTool

    if command.kind != 'delivery' or not command.valid:
        return ConversationControlResult('delivery', False, _USAGE, error_code='TOOL_INVALID_ARGUMENTS')
    try:
        params = json.loads(command.value)
    except (ValueError, TypeError):
        return ConversationControlResult('delivery', False, _USAGE, error_code='TOOL_INVALID_ARGUMENTS')
    outcome = AssetDeliveryTool(root_dir, owner_id).execute(params)
    data = json.loads(outcome.output)
    message = json.dumps(data, ensure_ascii=False, indent=2)
    return ConversationControlResult('delivery', outcome.ok, message, details=data,
        error_code=outcome.reported_error_code or outcome.error_code)
