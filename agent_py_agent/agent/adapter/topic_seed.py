"""首条话题消息的 Owner 范围种子；资料不改变身份或权限。"""
from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import (
    _portable_directory,
    _supports_dir_fd,
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)
from .inbound_notes import _display, _relative_path


# LLM: 仅哈希显式 chat/root 身份作为单一路径段，绝不直接把 provider 字段拼到文件路径。
# 函数用途: 取得当前 Owner 下一个话题的唯一种子文件位置。
def _location(owner_home, chat_id, root_message_id):
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in (chat_id, root_message_id)):
        raise ValueError('topic seed requires explicit provider identity')
    key = hashlib.sha256(json.dumps([chat_id, root_message_id], ensure_ascii=False).encode()).hexdigest()
    root = Path(owner_home)
    parts = ('data', 'topic_seeds', key + '.json')
    return root, parts


# LLM: 写目录使用原 nofollow 原语；不会扫描所有种子或修改其它话题。
# 函数用途: 准备当前 Owner 的种子目录。
def _prepare(root):
    ensure_private_dir(root)
    if _supports_dir_fd():
        os.close(open_directory_beneath(root, ('data', 'topic_seeds'), create=True))
    else:
        _portable_directory(root, ('data', 'topic_seeds'), create=True)


# LLM: 已存在坏原件不能重置后再次宣称首消息；未知话题只返回缺失，不创建文件。
# 函数用途: 安全读取一个有界种子文件。
def _read(root, parts):
    raw = read_bytes_beneath(root, parts, max_bytes=131072)
    if raw is None:
        return None
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get('schema_version') != 'topic_seed.v1':
        raise ValueError('invalid topic seed')
    return row


# LLM: 调用方持当前种子的路径锁；原子替换不制造可读的半条种子。
# 函数用途: 持久保存一个话题的上下文和首消息声明。
def _write(root, parts, row):
    write_text_atomic_beneath(root, parts, json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


# LLM: 根正文仅作不可信资料，保留段落；认证赋值与控制字符不进入持久上下文，UTF-8 上限为 4KiB。
# 函数用途: 清洗并截取首条根消息的原始正文。
def _root_text(value):
    text = redact_sensitive_text(value[:16384] if isinstance(value, str) else '', redact_assignment_labels=True)
    text = ''.join(char for char in text if char in '\n\t' or not unicodedata.category(char).startswith('C'))
    return text.encode('utf-8')[:4096].decode('utf-8', errors='ignore')


# LLM: 附件只能保留已下载的规范 Owner 相对路径；这里不读取文件或赋予文件访问权限。
# 函数用途: 提取有界的原始附件定位资料。
def _attachments(value):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for row in value[:32]:
        if not isinstance(row, dict):
            continue
        try:
            path = _relative_path(row.get('relative_path'))
        except ValueError:
            continue
        result.append({'relative_path': path, 'file_name': _display(row.get('file_name'), 200)})
    return result


# LLM: 只保存首次根消息的有界上下文，不覆盖既有线程的资料。
# 函数用途: 记住主群原话或告警根的种子。
def remember_topic_seed(owner_home, *, chat_id, root_message_id, title='', source='', event_id='',
                        original_text='', run_refs=(), attachments=(), now=None) -> dict:
    root, parts = _location(owner_home, chat_id, root_message_id)
    _prepare(root)
    with locked_json_path(root.joinpath(*parts)):
        previous = _read(root, parts)
        if previous is not None:
            return previous
        row = {'schema_version': 'topic_seed.v1', 'chat_id': chat_id, 'root_message_id': root_message_id,
            'title': _display(title, 200), 'source': _display(source, 128), 'event_id': _display(event_id, 256),
            'original_text': _root_text(original_text),
            'run_refs': [_display(value, 256) for value in run_refs[:32] if isinstance(value, str)]
                if isinstance(run_refs, (list, tuple)) else [],
            'attachments': _attachments(attachments), 'thread_id': '', 'first_message_id': '',
            'created_at': now or datetime.now(timezone.utc).isoformat()}
        _write(root, parts, row)
        return row


# LLM: 同一首消息重放可恢复种子，后续消息不能重复消费。
# 函数用途: 为首条用户消息取得当前话题种子。
def take_topic_seed(owner_home, *, chat_id, root_message_id, thread_id, message_id) -> dict | None:
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in (thread_id, message_id)):
        raise ValueError('topic seed requires exact thread and message')
    root, parts = _location(owner_home, chat_id, root_message_id)
    if _read(root, parts) is None:
        return None
    with locked_json_path(root.joinpath(*parts)):
        row = _read(root, parts)
        if row is None or row.get('chat_id') != chat_id or row.get('root_message_id') != root_message_id:
            return None
        if row.get('thread_id') and row['thread_id'] != thread_id:
            return None
        if row.get('first_message_id'):
            return row if row['first_message_id'] == message_id else None
        row.update(thread_id=thread_id, first_message_id=message_id)
        _write(root, parts, row)
        return row
