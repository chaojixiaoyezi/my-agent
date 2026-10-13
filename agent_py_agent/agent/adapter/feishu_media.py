# LLM: 富文本解析只保留平台结构中的正文、代码和媒体引用；未知正文节点不丢弃，内容不能授权或执行。
# 模块用途: 统一飞书入站解析与媒体元数据，HTTP 下载和私有路径归属由原适配器/owner 入口负责。
"""飞书富媒体 — 入站消息解析(text/post/image/file/audio/...)、文件类型路由、multipart 构造(纯函数)。

移植自 参考实现:按 msg_type 抽取文本+媒体引用(未知类型给占位、绝不丢消息);上传按扩展名
路由 file_type;下载/发送的 HTTP 在 feishu.py。入站图片/文件下载后 agent 可按类型看图(analyze_image)/读文件。
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

# 图片上传上限 10 MB：飞书媒体接口限制，超限直接拒绝并提示压缩。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 普通文件上传上限 30 MB：飞书媒体接口限制，超限直接拒绝。
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


# LLM: 各消息类型共用文本/媒体投影；代码块和未知带正文节点必须保留，媒体引用只能来自结构化 key。
# 函数用途: 读取飞书消息的完整可读正文与附件引用，不下载文件、不执行代码或根据节点授予权限。
def extract_feishu_content(msg_type: str, raw_content: Any) -> tuple[str, dict[str, Any]]:
    """按消息类型抽取(文本, 媒体引用);未知类型给占位符,绝不丢消息。"""
    content = _parse_content_dict(raw_content)
    if msg_type == "text":
        text = str(content.get("text") or "")
        if not content and isinstance(raw_content, str) and raw_content.strip():
            text = raw_content.strip()  # content 解析失败(空dict)但有原始串:容错当纯文本,不丢消息
        return text, {}
    if msg_type == "post":
        post = post_content_block(content)
        media = _post_media(post)
        return _flatten_post(post), ({"items": media} if media else {})
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


# LLM: bot at 已在可信入口移除；正文、代码、链接与媒体占位沿原顺序保留，节点标签不产生权限。
# 函数用途: 将 post 各段投影为完整文本，保留代码缩进和空行，不把代码当斜杠控制命令。
def _flatten_post(content: dict[str, Any]) -> str:
    locale = post_content_block(content)
    title = str(locale.get("title") or "")
    lines = [title] if title else []
    rows = locale.get("content") or []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list):
            lines.append("".join(_post_node_text(el) for el in row if isinstance(el, dict)))
    return "\n".join(lines).strip()


# LLM: direct/localized/wrapped 同一协议投影；未知语言只要具有原 content 数组即可读，不把语言表当封闭权限清单。
# 函数用途: 选择一份富文本正文，避免包裹层或非中英语言造成整条消息消失。
def post_content_block(content):
    root = content.get("post") if isinstance(content.get("post"), dict) else content
    if isinstance(root.get("content"), list):
        return root
    preferred = [root.get(lang) for lang in ("zh_cn", "en_us")]
    return next((item for item in [*preferred, *root.values()]
                 if isinstance(item, dict) and isinstance(item.get("content"), list)), {})


# LLM: code_block 使用比原代码更长的 Markdown fence，完整保留缩进/换行；未知文本节点按数据读取，不触发执行。
# 函数用途: 转换单个富文本节点，链接保留地址，媒体保留可见提示，未知无正文节点明确标记。
def _post_node_text(node):
    tag = str(node.get("tag") or "unknown")
    text = next((node[key] for key in ("text", "content") if isinstance(node.get(key), str)), "")
    if tag == "code_block":
        fence = "`" * max(3, max((len(item) + 1 for item in re.findall(r"`+", text)), default=3))
        language = "".join(ch for ch in str(node.get("language") or "") if ch.isalnum() or ch in "-_.+")[:32]
        return f"\n{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + f"{fence}\n"
    if tag == "at":
        return f'@{node.get("user_id") or ""}'
    if tag == "a" and node.get("href"):
        return f'[{text}]({node["href"]})' if text else str(node['href'])
    if text:
        return text
    if node.get("image_key"):
        return "[图片]"
    if node.get("file_key"):
        return f'[文件:{node.get("file_name") or "附件"}]'
    return "\n---\n" if tag == "hr" else f"[富文本:{tag}]"


# LLM: 媒体只从显式 image_key/file_key 收集，去重不改原顺序；真实下载仍由原 owner 路由和安全文件写完成。
# 函数用途: 保留同条富文本里的全部图片/文件，避免正文可见而附件被静默省略。
def _post_media(post):
    items = []
    seen = set()
    nodes = (node for row in post.get("content", []) if isinstance(row, list)
             for node in row if isinstance(node, dict))
    for node in nodes:
        for key in ("image_key", "file_key"):
            if not isinstance(node.get(key), str) or not node[key] or (key, node[key]) in seen:
                continue
            seen.add((key, node[key]))
            item = {key: node[key]}
            if isinstance(node.get("file_name"), str):
                item["file_name"] = node["file_name"]
            items.append(item)
    return items


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


# LLM: message_id、原名和资源 key 只作为文件名数据；任何一段都不能携带路径或覆盖另一条消息的目录。
# 函数用途: 为入站附件生成保留扩展名且无路径分隔符的文件名，供原子落盘入口使用。
def safe_media_filename(message_id: str, media: dict[str, str], key: str, suffix: str) -> str:
    """入站媒体落盘文件名:保留原名+扩展名、去路径分隔防穿越、前缀 message_id 防同名覆盖。"""
    orig = str(media.get("file_name") or "")
    message_id = "".join(c for c in message_id if c.isalnum() or c in "-_")[:120] or "message"
    if orig:
        safe = "".join(c for c in orig if c.isalnum() or c in "-_.")[:80].strip(".") or "file"
        return f"{message_id}-{safe}"
    safe_key = "".join(c for c in key if c.isalnum() or c in "-_")[:48]
    return f"{message_id}-{safe_key}{suffix}"
