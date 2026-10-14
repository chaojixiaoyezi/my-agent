# LLM: 此接口只消费正式Delivery安全冻结的typedbytes；不解析模型blob、不重新开文件或保存另一份附件账。
# 模块用途: 为飞书原适配器提供不可变附件上传能力，沿原token/multipart/uuid运输发送。
from __future__ import annotations

import hashlib
from pathlib import Path

from ..delivery.attachment_content import VerifiedAttachmentContent
from .feishu_media import MAX_FILE_BYTES, MAX_IMAGE_BYTES, file_message_type, file_upload_type


# LLM: 正式Delivery只给已核验content；发送前再核同bytes的原size/hash，不读取元数据path或接受任意JSON blob。
# 函数用途: 将原授权目标与冻结附件组成原multipart/消息API请求，保留文件名/类型/稳定uuid。
def _send_frozen_feishu_attachment(adapter, target, item, idempotency_key, as_image):
    from .feishu import _feishu_request_uuid, _send_feishu_media, _upload_feishu_bytes

    if (not isinstance(item, VerifiedAttachmentContent) or type(item.content) is not bytes
        or len(item.content) != item.attachment.size_bytes
        or hashlib.sha256(item.content).hexdigest() != item.attachment.sha256):
        return False
    name = item.attachment.name
    token = adapter._get_tenant_access_token()
    if not token:
        return False
    kind = 'image' if as_image else 'file'
    fields = {'image_type': 'message'} if as_image else {'file_type': file_upload_type(Path(name)), 'file_name': name}
    key = _upload_feishu_bytes(item.content, name, kind, fields, token)
    return bool(key) and _send_feishu_media(target, 'image' if as_image else file_message_type(Path(name)),
        {'image_key' if as_image else 'file_key': key}, token,
        _feishu_request_uuid(idempotency_key, component=kind, index=0))


# LLM: 能力显式声明供原registry推导；每次upload的字节仍必须来自Delivery的owner/hash核验。
# 类用途: 为原飞书适配器声明文件/图片上限和唯一冻结内容发送入口。
class FeishuAttachmentMixin:
    supports_frozen_attachment_content = True
    max_file_bytes = MAX_FILE_BYTES
    max_image_bytes = MAX_IMAGE_BYTES

    # LLM: 消费Delivery已核同一hash的不可变bytes，不能从metadata/path或模型字段再次读文件。
    # 函数用途: 上传冻结附件并发回原授权目标，保留图片/音视频/文件路由与原稳定uuid。
    def send_attachment_content(self, user_id, content, *, idempotency_key='', as_image=False) -> bool:
        return _send_frozen_feishu_attachment(self, user_id, content, idempotency_key, as_image)
