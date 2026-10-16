# LLM: 只在原 durable prepare 内读取当前群明确 parent_id；准确消息、群和话题证明先于资源下载。
#   父发送者不进入当前身份；文字只作有界外部资料，无缓存、递归引用、最近消息查询或第二入站状态。
# 模块用途: 核验被引用消息的文字和附件来源，沿原入站入口提交资料或不可读取事实。
from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

from ..command_catalog import system_slash_command_name
from .feishu_quoted_content import extract_quoted_content
from .protocol import IncomingMessage, provider_message_reference

# 单条引用正文最多展示的字符数，不改变媒体或身份授权事实。
MAX_QUOTED_TEXT_CHARS = 16000
_REASONS = {
    'FEISHU_QUOTED_MEDIA_AUTH_UNAVAILABLE': '宿主飞书应用认证暂不可用，未读取引用资料。',
    'FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE': '引用消息不可读取，请检查机器人入群、群消息读取权限和消息可见范围。',
    'FEISHU_QUOTED_MEDIA_PERMISSION_REQUIRED': '应用缺少读取此群消息的权限；接收群内提及权限不等于读取原文件消息权限。',
    'FEISHU_QUOTED_MEDIA_DELETED': '引用消息已撤回，未读取引用资料。',
    'FEISHU_QUOTED_MEDIA_ID_MISMATCH': '返回内容没有唯一匹配被引用的消息，未读取引用资料。',
    'FEISHU_QUOTED_MEDIA_CHAT_MISMATCH': '被引用消息不属于当前群，禁止读取引用资料。',
    'FEISHU_QUOTED_MEDIA_TOPIC_MISMATCH': '无法证明引用消息属于当前话题，禁止读取引用资料。',
    'FEISHU_QUOTED_MEDIA_INVALID': '引用消息结构不完整，无法核验引用来源。',
    'FEISHU_QUOTED_MEDIA_NO_RESOURCE': '当前引用开关下没有可读取的文字或附件；未沿更早引用查找内容。',
    'FEISHU_QUOTED_MEDIA_RESOURCE_UNAVAILABLE': '引用附件下载未完成，可能已撤回、不可见或受飞书下载限制；未提供部分文件路径。',
}


# LLM: 这是单次读取失败的安全结构，不记录响应正文、URL、异常 str 或凭据；不触发 durable 无限重试。
# 类用途: 向原 prompt 投影明确的引用资料失败原因与可选供应商数值状态。
class FeishuQuotedMediaError(RuntimeError):
    # LLM: 原因码必须来自本模块的客观核验；HTTP/provider code 只允许整数，不接受外部错误描述。
    # 函数用途: 保存可读但不含秘密的失败事实，不执行请求或改变身份。
    def __init__(self, code: str, *, http_status: int = 0, provider_code: int = 0) -> None:
        self.code = code
        self.http_status = http_status if type(http_status) is int else 0
        self.provider_code = provider_code if type(provider_code) is int else 0
        super().__init__(code)


# LLM: 此对象只由准确 parent GET 校验生成，生命周期限于原消息 prepare；不能从模型或 media 任意字段恢复。
# 类用途: 将当前消息、被引用文字与资源绑定；正文只作资料，逐资源携带真实下载源编号。
@dataclass(frozen=True)
class ValidatedQuotedMedia:
    current_message_id: str
    parent_message_id: str
    chat_id: str
    items: tuple[dict[str, Any], ...]
    text: str = ''
    original_chars: int = 0
    source_message_ids: tuple[str, ...] = ()
    coverage: str = 'complete'
    unreadable_count: int = 0
    member_count: int = 1
    media_omitted_count: int = 0
    text_omitted: bool = False


# LLM: 核心控制命令先走原分流；这里只沿原开关和已受理群事件读取明确 parent，私聊及无引用不发请求。
# 函数用途: 按文字/附件开关判断是否单次读取明确的群引用，不新造提及证明或权限来源。
def quoted_media_requested(adapter: object, msg: IncomingMessage) -> bool:
    metadata = msg.metadata or {}
    enabled = getattr(adapter, 'quoted_media_enabled', False) or getattr(adapter, 'quoted_text_enabled', False)
    return bool(msg.channel == 'feishu' and enabled
        and metadata.get('feishu_chat_type') == 'group' and metadata.get('feishu_chat_id')
        and metadata.get('feishu_parent_id') and not system_slash_command_name(msg.content))


# LLM: 根必须是唯一准确 ID 及完整 API 成功结构；其它同次项只能在后续 upper 关系核验后成为转发资料。
# 函数用途: 从单条消息查询响应中选择被引用消息，丢弃供应商描述与其它消息正文。
def _exact_parent(response: object, parent_id: str) -> dict[str, Any]:
    if not isinstance(response, dict) or type(response.get('code')) is not int:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    code = response['code']
    if code != 0:
        reason = ('FEISHU_QUOTED_MEDIA_PERMISSION_REQUIRED' if code == 230027 else
                  'FEISHU_QUOTED_MEDIA_DELETED' if code == 230110 else 'FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE')
        raise FeishuQuotedMediaError(reason, provider_code=code)
    data = response.get('data')
    items = data.get('items') if isinstance(data, dict) else None
    matching = [item for item in items if isinstance(item, dict) and item.get('message_id') == parent_id] if isinstance(items, list) else []
    if len(matching) != 1:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_ID_MISMATCH')
    return matching[0]


# LLM: 根文件本身可没有 root/thread；当前 root==准确 parent 即根证明，其余话题必须有共同结构锚且无冲突。
# 函数用途: 验证同群引用仍属于当前话题，不因普通根文件缺字段而误拒，也不跨话题猜来源。
def _same_topic(metadata: dict[str, Any], parent: dict[str, Any], parent_id: str) -> bool:
    current_root, current_thread = str(metadata.get('feishu_root_id') or ''), str(metadata.get('feishu_thread_id') or '')
    parent_root, parent_thread = str(parent.get('root_id') or ''), str(parent.get('thread_id') or '')
    if (current_thread and parent_thread and current_thread != parent_thread) or (current_root and parent_root and current_root != parent_root):
        return False
    if current_root == parent_id:
        return True
    if current_thread and parent_thread:
        return current_thread == parent_thread
    if current_root and parent_root:
        return current_root == parent_root
    return not any((current_root, current_thread, parent_root, parent_thread))


# LLM: 只解析已核验成员的显示投影；正文、回调及发送者不能授予身份，字符限额仍只有本模块一处。
#   合并转发信封不作为正文，缺少子资料明确计缺口；资源逐项绑定自己准确编号，开关省略有事实记录。
# 函数用途: 汇总同次引用的文字、代码、链接和附件，生成当前消息绑定的准备期证明，不写第二份状态。
def _project_members(adapter: object, msg: IncomingMessage, members: object) -> ValidatedQuotedMedia:
    parts, items = [], []
    unreadable = members.unreadable_count
    partial, omitted, text_omitted = bool(unreadable), 0, False
    for member in members.members:
        if member.get('msg_type') == 'merge_forward':
            if not any(row.get('upper_message_id') == member['message_id'] for row in members.members):
                unreadable += 1
                partial = True
            continue
        body = member.get('body')
        projection = extract_quoted_content(str(member.get('msg_type') or ''),
            body.get('content') if isinstance(body, dict) else None)
        unreadable += projection.unreadable_count
        partial |= projection.coverage != 'complete'
        if getattr(adapter, 'quoted_text_enabled', False):
            if projection.text.strip():
                parts.append(projection.text)
        elif projection.text.strip():
            text_omitted = True
        media = projection.media.get('items', [])
        if getattr(adapter, 'quoted_media_enabled', False):
            items.extend({**item, 'source_message_id': member['message_id']} for item in media)
        else:
            omitted += len(media)
    text = '\n\n'.join(parts)
    if not items and not text.strip():
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_NO_RESOURCE')
    return ValidatedQuotedMedia(msg.message_id, str(msg.metadata['feishu_parent_id']),
        str(msg.metadata['feishu_chat_id']), tuple(items), text[:MAX_QUOTED_TEXT_CHARS], len(text),
        tuple(member['message_id'] for member in members.members),
        'visible_only' if partial or omitted or text_omitted else 'complete', unreadable,
        len(members.members), omitted, text_omitted)


# LLM: ID/chat/deleted/topic 全部通过后才按开关解析文字/资源；转发仅读同次已证 upper 闭包，不追加查询或改 owner。
# 函数用途: 核验同群父消息并准备有界资料与准确资源来源，记录覆盖缺口，不猜更早引用或陌生响应项。
def read_quoted_media(adapter: object, msg: IncomingMessage) -> ValidatedQuotedMedia:
    from .feishu_quoted_members import validated_quote_members

    metadata = msg.metadata
    parent_id, chat_id = str(metadata['feishu_parent_id']), str(metadata['feishu_chat_id'])
    try:
        response = adapter.fetch_message(parent_id)
        parent = _exact_parent(response, parent_id)
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        exc.close()
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE', http_status=http_status) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE') from None
    except (ValueError, TypeError):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID') from None
    if parent.get('chat_id') != chat_id:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_CHAT_MISMATCH')
    if parent.get('deleted') is True:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_DELETED')
    if parent.get('deleted') is not False:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    if not _same_topic(metadata, parent, parent_id):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_TOPIC_MISMATCH')
    return _project_members(adapter, msg, validated_quote_members(response, parent))


# LLM: 同一 prepare 的来源证明必须仍对应当前消息和群；source ID 只能是准确根或本次 GET 已证转发成员。
# 函数用途: 为原下载器核验被引用附件的逐资源来源，不改变资源或身份字段。
def quoted_media_references(msg: IncomingMessage, quoted: ValidatedQuotedMedia) -> dict[str, object]:
    if (quoted.current_message_id != msg.message_id or quoted.parent_message_id != msg.metadata.get('feishu_parent_id')
        or quoted.chat_id != msg.metadata.get('feishu_chat_id') or msg.metadata.get('feishu_chat_type') != 'group'
        or any(item.get('source_message_id') not in (quoted.source_message_ids or (quoted.parent_message_id,))
               for item in quoted.items)):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    return {'items': [dict(item) for item in quoted.items]}


# LLM: 文字/附件共用当前消息、parent、chat证明；公开短引用只作选择，真实ID绑定仅入认证metadata，不从正文反推权限。
# 函数用途: 准备成功后附加外部资料，并冻结本请求已证引用供宿主原Get复核；不写日志或授予订阅权限。
def append_quoted_text(msg: IncomingMessage, quoted: ValidatedQuotedMedia) -> None:
    quoted_media_references(msg, quoted)
    if not quoted.text and not quoted.items:
        return
    identifiers = quoted.source_message_ids or (quoted.parent_message_id,)
    msg.metadata['quoted_message_references'] = {'schema': 'quoted_message_references.v1',
        'channel': msg.channel, 'chat_id': quoted.chat_id, 'current_message_id': msg.message_id,
        'references': [{'ref': provider_message_reference(value), 'message_id': value} for value in identifiers]}
    facts = {'schema': 'quoted_message.v1', 'status': 'available', 'untrusted': True,
        'source_message_ref': provider_message_reference(quoted.parent_message_id),
        'content': quoted.text, 'original_chars': quoted.original_chars,
        'truncated': quoted.original_chars > len(quoted.text), 'coverage': quoted.coverage,
        'unreadable_count': quoted.unreadable_count, 'member_count': quoted.member_count,
        'media_omitted_count': quoted.media_omitted_count, 'text_omitted': quoted.text_omitted,
        'source_message_refs': [provider_message_reference(value) for value in identifiers]}
    msg.content += '\n\n[引用消息资料]\n' + json.dumps(facts, ensure_ascii=False)


# LLM: 失败事实只是当前用户需求的模型上下文，不作为身份/控制裁决；不包含 parent 正文、文件 key 或异常描述。
# 函数用途: 给原模型说明引用资料尚未读取，正常面向用户的解释仍由模型生成。
def append_quoted_media_failure(msg: IncomingMessage, error: FeishuQuotedMediaError) -> None:
    facts: dict[str, object] = {'schema': 'quoted_media.v1', 'status': 'unavailable', 'error_code': error.code,
        'source_message_ref': provider_message_reference(msg.metadata.get('feishu_parent_id')),
        'explanation': _REASONS.get(error.code, _REASONS['FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE'])}
    if error.http_status:
        facts['provider_http_status'] = error.http_status
    if error.provider_code:
        facts['provider_error_code'] = error.provider_code
    msg.content += '\n\n[附件读取事实]\n' + json.dumps(facts, ensure_ascii=False)
