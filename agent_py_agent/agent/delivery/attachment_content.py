# LLM: 附件载体不授予权限；owner_root 必须由当前宿主 owner 提供，内容只从该根内原 no-follow FD 读取。
#   snapshot 限于一次 DeliveryAttempt，不进账/缓存/模型；原 hash/size 必须与实际上传的不可变字节一致。
# 模块用途: 在首个外部副作用前安全冻结附件，防止验证后改路径、符号链接或内容导致跨用户上传。
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.channels import ChannelAttachment


# LLM: 此类型仅在本模块安全读/核验完成后交 adapter；serialized attachment/content 和模型 attrs 不进入构造流程。
# 类用途: 携带本次已经核验的附件元数据和实际要上传的不可变字节，不再依赖可变磁盘路径。
@dataclass(frozen=True)
class VerifiedAttachmentContent:
    attachment: ChannelAttachment
    content: bytes


# LLM: 原文件权威/hashes 不完整或发生变化则首发前拒绝；不补造旧记录的版本证明，不降级普通 Path.read_bytes。
# 类用途: 表示附件尚未完成安全准备，不能发正文或把投递标记 sent。
class AttachmentContentError(ValueError):
    pass


# LLM: 只接受宿主 typed 附件；alias root 先规范化，实际读仍锚 owner 内逐段 no-follow，缺平台能力不得跟随路径兜底。
# 函数用途: 有界读取并核对原 size/hash，返回本次上传唯一字节源；不写新文件或附件账。
def verify_attachment_content(attachment: ChannelAttachment, *, max_bytes: int = 0) -> VerifiedAttachmentContent:
    if (not isinstance(attachment, ChannelAttachment) or not attachment.owner_root
        or not attachment.sha256 or len(attachment.sha256) != 64
        or type(attachment.size_bytes) is not int or attachment.size_bytes < 0):
        raise AttachmentContentError('附件缺少宿主归属或原版本证明')
    if max_bytes and attachment.size_bytes > max_bytes:
        raise AttachmentContentError('附件超过通道上传上限')
    root = Path(attachment.owner_root).expanduser().resolve(strict=True)
    path = Path(attachment.path).expanduser().resolve(strict=True)
    relative = path.relative_to(root)
    if (not relative.parts or not attachment.name or Path(attachment.name).name != attachment.name
        or any(ord(char) < 32 or ord(char) == 127 for char in attachment.name)):
        raise AttachmentContentError('附件名称或路径无效')
    content = read_bytes_beneath(root, relative.parts, max_bytes=attachment.size_bytes, require_dir_fd=True)
    if content is None or len(content) != attachment.size_bytes or hashlib.sha256(content).hexdigest() != attachment.sha256:
        raise AttachmentContentError('附件内容已变化，需要重新验证原产物')
    return VerifiedAttachmentContent(attachment, content)
