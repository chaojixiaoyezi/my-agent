"""飞书富媒体 — 入站消息解析(text/post/image/file/audio/...)、文件类型路由、multipart 构造(纯函数)。

移植自 参考实现:按 msg_type 抽取文本+媒体引用(未知类型给占位、绝不丢消息);上传按扩展名
路由 file_type;下载/发送的 HTTP 在 feishu.py。入站图片/文件下载后 agent 可按类型看图(analyze_image)/读文件。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024

_DOC_UPLOAD_TYPES = {
    ".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
}


def _parse_content_dict(raw_content: Any) -> dict[str, Any]:
    """飞书消息 content(JSON 字符串或 dict)解析成 dict;失败给空。"""
    if not raw_content:
        return {}
    try:
        parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_feishu_content(msg_type: str, raw_content: Any) -> tuple[str, dict[str, str]]:
    """按消息类型抽取(文本, 媒体引用);未知类型给占位符,绝不丢消息。"""
    content = _parse_content_dict(raw_content)
    if msg_type == "text":
        text = str(content.get("text") or "")
        if not content and isinstance(raw_content, str) and raw_content.strip():
            text = raw_content.strip()  # content 解析失败(空dict)但有原始串:容错当纯文本,不丢消息
        return text, {}
    if msg_type == "post":
        return _flatten_post(content), {}
    if msg_type == "image":
        key = str(content.get("image_key") or "")
        return "[图片]", ({"image_key": key} if key else {})
    if msg_type == "file":
        return _file_content(content)
    if msg_type == "audio":
        key = str(content.get("file_key") or "")
        return "[语音]", ({"file_key": key} if key else {})
    if msg_type == "media":
        key = str(content.get("file_key") or "")
        return "[视频]", ({"file_key": key} if key else {})
    return f"[{msg_type} 消息]", {}


def _file_content(content: dict[str, Any]) -> tuple[str, dict[str, str]]:
    name = str(content.get("file_name") or "")
    key = str(content.get("file_key") or "")
    text = f"[文件:{name}]" if name else "[文件]"
    if not key:
        return text, {}
    media = {"file_key": key}
    if name:  # 带上原名 → 下载保留扩展名(.zip/.py/.pdf),agent 才能按类型开/跑/解包
        media["file_name"] = name
    return text, media


def _flatten_post(content: dict[str, Any]) -> str:
    """post 富文本拍平成可读文本:text/a 取值(媒体省略)。"""
    locale: dict[str, Any] = content
    for lang in ("zh_cn", "en_us"):
        if isinstance(content.get(lang), dict):
            locale = content[lang]
            break
    title = str(locale.get("title") or "")
    lines = [title] if title else []
    rows = locale.get("content") or []
    for row in rows if isinstance(rows, list) else []:
        parts = [str(el.get("text") or "") for el in row
                 if isinstance(row, list) and isinstance(el, dict) and el.get("tag") in ("text", "a")]
        lines.append("".join(parts))
    return "\n".join(lines).strip()


def file_upload_type(path: Path) -> str:
    """上传 file_type 路由(按扩展名);默认 stream。"""
    ext = path.suffix.lower()
    if ext in (".ogg", ".opus"):
        return "opus"
    if ext in (".mp4", ".mov", ".avi", ".m4v"):
        return "mp4"
    return _DOC_UPLOAD_TYPES.get(ext, "stream")


def file_message_type(path: Path) -> str:
    """发送 msg_type 路由(音频/视频/文件)。"""
    ext = path.suffix.lower()
    if ext in (".ogg", ".opus"):
        return "audio"
    if ext in (".mp4", ".mov", ".avi", ".m4v"):
        return "media"
    return "file"


def build_multipart(fields: dict[str, str], file_field: str, filename: str, blob: bytes) -> tuple[bytes, str]:
    """构造 multipart/form-data body + content-type(图片/文件上传用)。"""
    boundary = "----myagent" + secrets.token_hex(8)
    out = bytearray()
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    out += f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n\r\n'.encode()
    out += blob
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def safe_media_filename(message_id: str, media: dict[str, str], key: str, suffix: str) -> str:
    """入站媒体落盘文件名:保留原名+扩展名、去路径分隔防穿越、前缀 message_id 防同名覆盖。"""
    orig = str(media.get("file_name") or "")
    if orig:
        safe = "".join(c for c in orig if c.isalnum() or c in "-_.")[:80].strip(".") or "file"
        return f"{message_id}-{safe}"
    safe_key = "".join(c for c in key if c.isalnum() or c in "-_")[:48]
    return f"{message_id}-{safe_key}{suffix}"
