# LLM: 只在原 durable prepare 内读取当前群明确 parent_id；准确消息、群和话题证明先于资源下载。
#   父消息发送者与正文不进入当前身份或 prompt；无缓存、递归引用、最近消息查询或第二入站状态。
# 模块用途: 核验被引用附件的来源，并把不可读取的结构化事实交给原模型入口。
from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

from ..command_catalog import system_slash_command_name
from .feishu_media import extract_feishu_content
from .protocol import IncomingMessage, provider_message_reference

_REASONS = {
    'FEISHU_QUOTED_MEDIA_AUTH_UNAVAILABLE': '宿主飞书应用认证暂不可用，未读取引用附件。',
    'FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE': '引用消息不可读取，请检查机器人入群、群消息读取权限和消息可见范围。',
    'FEISHU_QUOTED_MEDIA_PERMISSION_REQUIRED': '应用缺少读取此群消息的权限；接收群内提及权限不等于读取原文件消息权限。',
    'FEISHU_QUOTED_MEDIA_DELETED': '引用消息已撤回，未读取附件。',
    'FEISHU_QUOTED_MEDIA_ID_MISMATCH': '返回内容没有唯一匹配被引用的消息，未读取附件。',
    'FEISHU_QUOTED_MEDIA_CHAT_MISMATCH': '被引用消息不属于当前群，禁止读取附件。',
    'FEISHU_QUOTED_MEDIA_TOPIC_MISMATCH': '无法证明引用消息属于当前话题，禁止读取附件。',
    'FEISHU_QUOTED_MEDIA_INVALID': '引用消息结构不完整，无法核验附件来源。',
    'FEISHU_QUOTED_MEDIA_NO_RESOURCE': '被引用消息不含可下载的附件；未沿更早引用查找文件。',
    'FEISHU_QUOTED_MEDIA_RESOURCE_UNAVAILABLE': '引用附件下载未完成，可能已撤回、不可见或受飞书下载限制；未提供部分文件路径。',
}


# LLM: 这是单次读取失败的安全结构，不记录响应正文、URL、异常 str 或凭据；不触发 durable 无限重试。
# 类用途: 向原 prompt 投影明确的引用附件失败原因与可选供应商数值状态。
class FeishuQuotedMediaError(RuntimeError):
    # LLM: 原因码必须来自本模块的客观核验；HTTP/provider code 只允许整数，不接受外部错误描述。
    # 函数用途: 保存可读但不含秘密的失败事实，不执行请求或改变身份。
    def __init__(self, code: str, *, http_status: int = 0, provider_code: int = 0) -> None:
        self.code = code
        self.http_status = http_status if type(http_status) is int else 0
        self.provider_code = provider_code if type(provider_code) is int else 0
        super().__init__(code)


# LLM: 此对象只由准确 parent GET 校验生成，生命周期限于原消息 prepare；不能从模型或 media 任意字段恢复。
# 类用途: 将当前消息和被引用资源绑定，逐资源携带真实下载源编号。
@dataclass(frozen=True)
class ValidatedQuotedMedia:
    current_message_id: str
    parent_message_id: str
    chat_id: str
    items: tuple[dict[str, Any], ...]


# LLM: 核心控制命令先走原分流；这里只沿原开关和已受理群事件读取明确 parent，私聊及无引用不发请求。
# 函数用途: 判断本次 prepare 是否需要单次读取被引用的群消息，不新造提及证明或权限来源。
def quoted_media_requested(adapter: object, msg: IncomingMessage) -> bool:
    metadata = msg.metadata or {}
    return bool(msg.channel == 'feishu' and getattr(adapter, 'quoted_media_enabled', False)
        and metadata.get('feishu_chat_type') == 'group' and metadata.get('feishu_chat_id')
        and metadata.get('feishu_parent_id') and not system_slash_command_name(msg.content))


# LLM: 不遍历合并转发的子消息，不把它们的资源当作 parent；只接受唯一准确 ID 及完整 API 成功结构。
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


# LLM: ID/chat/deleted/topic 全部通过后才解析原资源协议；HTTP 失败关闭响应且不读取正文，不改当前 owner/actor。
# 函数用途: 核验同群父消息并抽取附件引用，每个引用只附准确父消息源 ID。
def read_quoted_media(adapter: object, msg: IncomingMessage) -> ValidatedQuotedMedia:
    metadata = msg.metadata
    parent_id, chat_id = str(metadata['feishu_parent_id']), str(metadata['feishu_chat_id'])
    try:
        parent = _exact_parent(adapter.fetch_message(parent_id), parent_id)
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
    body = parent.get('body')
    _, media = extract_feishu_content(str(parent.get('msg_type') or ''), body.get('content') if isinstance(body, dict) else None)
    items = media.get('items') if isinstance(media.get('items'), list) else [media] if media else []
    if not items:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_NO_RESOURCE')
    return ValidatedQuotedMedia(msg.message_id, parent_id, chat_id,
        tuple({**item, 'source_message_id': parent_id} for item in items))


# LLM: 同一 prepare 的来源证明必须仍对应当前消息和群；每个 source ID 都要匹配准确 parent，原 media 不授予此能力。
# 函数用途: 为原下载器核验被引用附件的逐资源来源，不改变资源或身份字段。
def quoted_media_references(msg: IncomingMessage, quoted: ValidatedQuotedMedia) -> dict[str, object]:
    if (quoted.current_message_id != msg.message_id or quoted.parent_message_id != msg.metadata.get('feishu_parent_id')
        or quoted.chat_id != msg.metadata.get('feishu_chat_id') or msg.metadata.get('feishu_chat_type') != 'group'
        or any(item.get('source_message_id') != quoted.parent_message_id for item in quoted.items)):
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_INVALID')
    return {'items': [dict(item) for item in quoted.items]}


# LLM: 失败事实只是当前用户需求的模型上下文，不作为身份/控制裁决；不包含 parent 正文、文件 key 或异常描述。
# 函数用途: 给原模型说明引用附件尚未读取，正常面向用户的解释仍由模型生成。
def append_quoted_media_failure(msg: IncomingMessage, error: FeishuQuotedMediaError) -> None:
    facts: dict[str, object] = {'schema': 'quoted_media.v1', 'status': 'unavailable', 'error_code': error.code,
        'source_message_ref': provider_message_reference(msg.metadata.get('feishu_parent_id')),
        'explanation': _REASONS.get(error.code, _REASONS['FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE'])}
    if error.http_status:
        facts['provider_http_status'] = error.http_status
    if error.provider_code:
        facts['provider_error_code'] = error.provider_code
    msg.content += '\n\n[附件读取事实]\n' + json.dumps(facts, ensure_ascii=False)
