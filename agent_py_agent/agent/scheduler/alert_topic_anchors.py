"""Owner 私有告警话题锚点；未知发送结果不得触发第二个根消息。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _portable_directory,
    _supports_dir_fd,
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)


# LLM: 锚点身份由显式来源、Owner、目标和事件决定。
# 函数用途: 计算稳定告警键。
def make_alert_key(source, owner_id, target_chat_id, event_id):
    values = (source, owner_id, target_chat_id, event_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError('alert identity must be explicit')
    return hashlib.sha256('|'.join(values).encode('utf-8')).hexdigest()


# LLM: 固定目录逐段 nofollow 创建；平台不支持 dir_fd 时沿用同一模块的便携路径校验。
# 函数用途: 准备 Owner 私有锚点目录并返回准确锁位置。
def _prepare(owner_home, alert_key):
    if not isinstance(alert_key, str) or not re.fullmatch('[0-9a-f]{64}', alert_key):
        raise ValueError('invalid alert key')
    root = Path(owner_home)
    ensure_private_dir(root)
    if _supports_dir_fd():
        os.close(open_directory_beneath(root, ('data',), create=True))
    else:
        _portable_directory(root, ('data',), create=True)
    return root, root / 'data/alert_topic_anchors.json'


# LLM: 损坏账本必须明确失败，不能重置为空后把未知旧发送当成首次创建。
# 函数用途: 安全读取当前 Owner 唯一锚点账本。
def _read(root):
    raw = read_bytes_beneath(root, ('data', 'alert_topic_anchors.json'), max_bytes=16777216)
    if raw is None:
        return {'schema_version': 'alert_topic_anchors.v1', 'anchors': {}}
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get('schema_version') != 'alert_topic_anchors.v1'
            or not isinstance(data.get('anchors'), dict)):
        raise ValueError('invalid alert anchor ledger')
    return data


# LLM: 临时文件和原子替换由原私有写原语负责；调用方必须已持同一路径锁。
# 函数用途: 原子保存完整锚点账本。
def _write(root, data):
    write_text_atomic_beneath(root, ('data', 'alert_topic_anchors.json'),
        json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')


# LLM: 新建判断和 pending 落盘同处锁内；私有布尔只给当前发送调用，不作为持久重放许可。
# 函数用途: 预留锚点并区分唯一首发者与已有预留。
def _reserve_for_delivery(owner_home, alert_key, *, now, dispatch_key=''):
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        data = _read(root)
        if alert_key in data['anchors']:
            row = data['anchors'][alert_key]
            if not isinstance(row, dict) or row.get('state') not in {'pending', 'sent', 'unknown'}:
                raise ValueError('invalid alert anchor entry')
            return dict(row), False, data.get('root_delivery_keys', {}).get(alert_key, '')
        row = {'root_message_id': '', 'thread_id': '', 'state': 'pending', 'updated_at': now}
        data['anchors'][alert_key] = row
        if dispatch_key:
            data.setdefault('root_delivery_keys', {})[alert_key] = dispatch_key
        _write(root, data)
        return dict(row), True, dispatch_key


# LLM: 公共预留接口幂等，不把重放解释成新发送许可。
# 函数用途: 为告警预留根锚点。
def reserve(owner_home, alert_key, *, now) -> dict:
    row, _, _ = _reserve_for_delivery(owner_home, alert_key, now=now)
    return row


# LLM: 根消息 ID 必须来自提供方确认，不接受逻辑回执替代。
# 函数用途: 标记已确认的根与线程。
def mark_sent(owner_home, alert_key, *, root_message_id, thread_id, now) -> dict:
    if not isinstance(root_message_id, str) or not root_message_id or not isinstance(thread_id, str):
        raise ValueError('confirmed provider root is required')
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        data = _read(root)
        previous = data['anchors'].get(alert_key, {})
        if previous.get('root_message_id') and previous['root_message_id'] != root_message_id:
            raise ValueError('alert root cannot change after confirmation')
        if previous.get('state') == 'sent' and (not thread_id or previous.get('thread_id')):
            return dict(previous)
        row = {'root_message_id': root_message_id, 'thread_id': thread_id, 'state': 'sent', 'updated_at': now}
        data['anchors'][alert_key] = row
        _write(root, data)
        return dict(row)


# LLM: 未知不清除已确认的根，也不重新授予发送许可。
# 函数用途: 保留未知发送状态。
def mark_unknown(owner_home, alert_key, *, now) -> dict:
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        data = _read(root)
        previous = data['anchors'].get(alert_key, {})
        if previous.get('state') == 'sent':
            return dict(previous)
        row = {'root_message_id': previous.get('root_message_id', ''),
            'thread_id': previous.get('thread_id', ''), 'state': 'unknown', 'updated_at': now}
        data['anchors'][alert_key] = row
        _write(root, data)
        return dict(row)
