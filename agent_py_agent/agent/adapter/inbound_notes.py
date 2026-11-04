"""Owner 入站附件便签；仅保存有界定位信息，不授予读取或执行权限。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..common.json_io import locked_json_path
from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)


# LLM: 这里只清洗展示内容，不参与 Owner 解析或授权；原文秘密不进入便签。
# 函数用途: 返回有界、单行且去控制字符的摘要。
def _display(value, limit):
    text = value if isinstance(value, str) else ''
    text = redact_sensitive_text(text[:4096], redact_assignment_labels=True)
    return ' '.join(''.join(char if not unicodedata.category(char).startswith('C') else ' '
                            for char in text).split())[:limit]


# LLM: 相对路径不得含盘符、反斜杠、点目录或控制字符，不通过 resolve 接受外部位置。
# 函数用途: 校验并保留 Owner 相对的规范 POSIX 路径。
def _relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 2048 or '\\' in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or any(unicodedata.category(char).startswith('C') for char in value)
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ValueError('inbound note path is not owner-relative')
    return value


# LLM: 读路径复用 nofollow 原语，坏格式按无可用摘要处理，不创建目录或改写原件。
# 函数用途: 从当前 Owner 的唯一便签文件读取有界条目。
def _entries(owner_home):
    text = read_bytes_beneath(Path(owner_home), ('media', 'recent_inbound_notes.json'), max_bytes=262144)
    if text is None:
        return []
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError):
        return []
    rows = data.get('entries') if isinstance(data, dict) and data.get('schema_version') == 'inbound_notes.v1' else None
    return [row for row in rows[-30:] if isinstance(row, dict)] if isinstance(rows, list) else []


# LLM: 整个读改写持同一路径锁；便签只能定位资料，不改变文件或授权，重放同一下载不重复追加。
# 函数用途: 追加一条当前 Owner 的附件便签。
def append_inbound_note(owner_home, *, channel, chat_id, message_id, media_kind, file_name,
                        relative_path, size_bytes, sha256, text_excerpt='', received_at=None) -> dict:
    relative_path = _relative_path(relative_path)
    if type(size_bytes) is not int or size_bytes < 0 or not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ValueError('invalid inbound note file facts')
    name = str(file_name).replace('\\', '/').rsplit('/', 1)[-1]
    name = _display(name, 200)
    identity = [channel, chat_id, message_id, relative_path, sha256]
    entry = {'note_id': hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest(),
        'received_at': received_at or datetime.now(timezone.utc).isoformat(),
        'channel': _display(channel, 64), 'chat_id': _display(chat_id, 256),
        'message_id': _display(message_id, 256), 'media_kind': _display(media_kind, 32),
        'file_name': name if name not in {'', '.', '..'} else 'attachment',
        'relative_path': relative_path, 'size_bytes': size_bytes, 'sha256': sha256,
        'text_excerpt': _display(text_excerpt, 200)}
    root = Path(owner_home)
    ensure_private_dir(root)
    os.close(open_directory_beneath(root, ('media',), create=True))
    with locked_json_path(root / 'media/recent_inbound_notes.json'):
        rows = _entries(root)
        previous = next((row for row in rows if row.get('note_id') == entry['note_id']), None)
        if previous is not None:
            return dict(previous)
        write_text_atomic_beneath(root, ('media', 'recent_inbound_notes.json'),
            json.dumps({'schema_version': 'inbound_notes.v1', 'entries': [*rows, entry][-30:]}, ensure_ascii=False) + '\n')
    return entry


# LLM: 读取只限显式 Owner，默认仅返回五条。
# 函数用途: 读取最近的附件便签。
def read_inbound_notes(owner_home, *, limit=5) -> list[dict]:
    if type(limit) is not int or limit <= 0:
        return []
    return list(reversed(_entries(owner_home)))[:min(limit, 30)]
