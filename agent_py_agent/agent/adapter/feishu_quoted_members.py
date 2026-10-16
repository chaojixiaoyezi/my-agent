# LLM: 仅从同次成功 GET 响应选择已验证 parent 的转发闭包；调用方必须先守当前群、撤回与话题门。
#   upper_message_id 只证明资料关系，成员 chat/sender 不产生 owner、工具或审批身份；不发请求、不解析正文。
#   根深度为 0，最多 16 条上级边、64 个输出成员（含根）；原响应关系索引线性遍历，不新建状态。
# 模块用途: 为引用读取筛选准确转发成员，计数已证明属于根但不能输出的记录，避免把陌生响应项当材料。
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .feishu_quoted_media import FeishuQuotedMediaError

# 单次引用最多输出的已核验转发成员数，包含根消息。
MAX_QUOTED_MEMBERS = 64
# 引用成员最多向下遍历的层数，根层为0。
MAX_QUOTED_MEMBER_DEPTH = 16


# LLM: tuple 与结果字段冻结，成员是原消息的分离深拷贝；这是资料选择结果，不可独立授权或恢复当前主体。
#   unreadable_count 只计有结构链通向根的拒绝成员；foreign、无根和断开的环不猜归属、不复制正文。
# 类用途: 保存根优先的可读转发成员及未输出数量，交原资料解析与 owner 下载器继续处理。
@dataclass(frozen=True)
class ValidatedQuoteMembers:
    members: tuple[dict[str, Any], ...]
    unreadable_count: int = 0


# LLM: 响应成功形状仍复用原 API 合同；非对象项不能证明消息关系，忽略它们但不恢复假根。
# 函数用途: 读取同次响应的对象消息集合，只检查结构，不查看消息正文或产生副作用。
def _response_items(response: object) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or type(response.get('code')) is not int or response['code'] != 0:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    data = response.get('data')
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    return [item for item in items if isinstance(item, dict)]


# LLM: 消息编号只能是非空原字符串，不 trim、不把数字或其它字段转成身份；上级编号也使用同一规则。
# 函数用途: 取精确关系编号，避免值转换把不同消息或坏记录合并。
def _exact_id(value: object) -> str:
    return value if isinstance(value, str) and value else ''


# LLM: 全量同次关系索引不解析正文；保留重复编号以便相关闭包 fail closed，不能任选一个重复变体。
# 函数用途: 按编号与上级编号建立只读查找投影，不改原列表，也不按 chat/sender 决定身份。
def _relation_indexes(items: list[dict[str, Any]]) -> tuple[dict[str, list], dict[str, list]]:
    by_id: dict[str, list] = defaultdict(list)
    children: dict[str, list] = defaultdict(list)
    for item in items:
        message_id = _exact_id(item.get('message_id'))
        if message_id:
            by_id[message_id].append(item)
        upper_id = _exact_id(item.get('upper_message_id'))
        if upper_id:
            children[upper_id].append(item)
    return by_id, children


# LLM: caller 已验证的 parent 必须仍是同次唯一根；不从 parent 参数自行造来源，根撤回或字段漂移立即拒绝。
# 函数用途: 复核准确根对象，让纯 helper 的调用错误不会升级为可信资料。
def _checked_root(parent: object, by_id: dict[str, list]) -> dict[str, Any]:
    if not isinstance(parent, dict) or not _exact_id(parent.get('message_id')):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    matches = by_id.get(parent['message_id'], [])
    if len(matches) != 1:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_ID_MISMATCH')
    root = matches[0]
    if root != parent or root.get('deleted') is not False:
        code = 'FEISHU_QUOTED_MEDIA_DELETED' if root.get('deleted') is True else 'FEISHU_QUOTED_MEDIA_INVALID'
        raise FeishuQuotedMediaError(code)
    return root


# LLM: 只沿准确根下行 upper 关系，任何未输出祖先会阻止后代输出；相关重复 ID 即使超过上限仍拒绝。
#   未输出但已证归根的记录仅加计数，继续结构遍历以准确计数；visited 终止环，正文只拷贝最多 64 个成员。
# 函数用途: 以根优先顺序筛选转发树，返回截断、撤回或坏来源的数量，不递归解析内容。
def _forward_members(root: dict[str, Any], by_id: dict[str, list], children: dict[str, list]) -> ValidatedQuoteMembers:
    members = [deepcopy(root)]
    root_id = root['message_id']
    seen = {root_id}
    queue = deque([(root_id, 0, True)])
    unreadable = 0
    while queue:
        ancestor, depth, ancestor_readable = queue.popleft()
        for item in children.get(ancestor, ()):
            message_id = _exact_id(item.get('message_id'))
            if not message_id:
                unreadable += 1
                continue
            if len(by_id[message_id]) != 1:
                raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_ID_MISMATCH')
            if message_id in seen:
                continue
            seen.add(message_id)
            readable = (ancestor_readable and item.get('deleted') is False
                        and depth + 1 <= MAX_QUOTED_MEMBER_DEPTH and len(members) < MAX_QUOTED_MEMBERS)
            if readable:
                members.append(deepcopy(item))
            else:
                unreadable += 1
            queue.append((message_id, depth + 1, readable))
    return ValidatedQuoteMembers(tuple(members), unreadable)


# LLM: 调用方先验证 root 的当前群、话题与认证；此入口仅将同次资料关系投影为有界成员，绝不独立授权。
#   非 merge_forward 不沿 body 声明编号、parent_id 或其它字段扩展；没有任何后续 GET、下载、身份转换或写账。
# 函数用途: 给原引用读取准备准确根与转发后代，复制资料记录并报告明确属于根的不可读成员。
def validated_quote_members(response: object, parent: object) -> ValidatedQuoteMembers:
    items = _response_items(response)
    by_id, children = _relation_indexes(items)
    root = _checked_root(parent, by_id)
    if root.get('msg_type') != 'merge_forward':
        return ValidatedQuoteMembers((deepcopy(root),))
    return _forward_members(root, by_id, children)
