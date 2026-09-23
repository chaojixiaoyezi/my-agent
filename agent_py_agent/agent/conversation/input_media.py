# LLM: 入站媒体只有 owner 私有内容寻址文件和结构化 refs 两个组成部分；文本、文件名和展示占位符不能授权读取。
# 模块用途: 有界导入图片/视频、校验 owner 边界，并在供应商发送边界临时编码；历史只保存小引用。
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path

DEFAULT_MEDIA_BYTES = 16 * 1024 * 1024
DEFAULT_MEDIA_FILES = 8


# LLM: 媒体错误是确定的输入失败，不可丢掉附件继续文本请求或作为瞬时模型故障重试。
# 类用途: 标记无效、超限、损坏或跨用户的媒体输入。
class InputMediaError(ValueError):
    error_code = "INPUT_MEDIA_INVALID"


# LLM: owner_home_dir 来自宿主已认证身份；此函数不能接受用户提交的 owner 路径。
# 函数用途: 返回当前用户唯一的入站附件目录，不创建目录。
def input_media_root(agent: object) -> Path:
    return Path(agent.home_paths.owner_home_dir) / "media" / "input"


# LLM: MIME 属于开放世界；显式类型可覆盖系统推断，已知 MOV 仅修正标准库在平台间的差异。
# 函数用途: 判断文件属于图片或视频，未知扩展名可由调用者提供明确 MIME。
def media_type_for_path(path: Path, declared: str = "") -> str:
    media_type = declared or mimetypes.guess_type(str(path))[0] or ""
    if not media_type.startswith(("image/", "video/")):
        raise InputMediaError("附件必须是图片或视频；未知扩展名请显式指定 MIME 类型。")
    return media_type


# LLM: 拷贝和哈希分块进行，临时文件与最终文件同目录并原子替换；引用永远指向导入时的字节，原文件变化不影响重试。
# 函数用途: 把用户明确选择的本机文件保存到私有附件目录，返回不含二进制的小引用。
def import_input_media(source: Path, root: Path, *, max_bytes: int = DEFAULT_MEDIA_BYTES,
                       media_type: str = "") -> dict:
    source = source.expanduser().resolve(strict=True)
    kind = media_type_for_path(source, media_type)
    if not source.is_file() or source.stat().st_size > max_bytes:
        raise InputMediaError(f"附件不是普通文件或超过 {max_bytes // 1024 // 1024} MiB 上限。")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest, size = hashlib.sha256(), 0
    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=root)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as reader:
            while chunk := reader.read(256 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise InputMediaError("附件导入时增长并超过大小上限。")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not size:
            raise InputMediaError("附件为空，未添加。")
        sha = digest.hexdigest()
        destination = root / sha
        os.replace(temporary, destination)
        return {"path": str(destination.resolve()), "sha256": sha,
                "media_type": kind, "size_bytes": size, "name": source.name}
    finally:
        Path(temporary).unlink(missing_ok=True)


# LLM: 验证发生在 owner 路由之后；仅接受其 canonical 附件目录内的内容寻址普通文件，拒绝 symlink 和目录穿越。
# 函数用途: 检查附件引用、大小和归属，保持请求总资源有界；不把路径文本猜成附件。
def validate_input_media(value: object, *, root: Path | None = None,
                         max_bytes: int = DEFAULT_MEDIA_BYTES,
                         max_files: int = DEFAULT_MEDIA_FILES) -> tuple[dict, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > max_files:
        raise InputMediaError(f"单条消息最多允许 {max_files} 个附件。")
    result, total = [], 0
    for item in value:
        if not isinstance(item, dict):
            raise InputMediaError("附件引用格式无效。")
        sha = str(item.get("sha256") or "")
        path = Path(str(item.get("path") or ""))
        if not re.fullmatch(r"[a-f0-9]{64}", sha) or path.name != sha:
            raise InputMediaError("附件内容标识无效。")
        if path.is_symlink() or not path.is_file() or (root is not None and path.resolve().parent != root.resolve()):
            raise InputMediaError("附件不存在或不属于当前用户。")
        size = item.get("size_bytes")
        if type(size) is not int or size <= 0 or size != path.stat().st_size:
            raise InputMediaError("附件大小已变化，请重新添加。")
        total += size
        if total > max_bytes:
            raise InputMediaError(f"单条消息附件总大小超过 {max_bytes // 1024 // 1024} MiB 上限。")
        result.append({"path": str(path.resolve()), "sha256": sha,
                       "media_type": media_type_for_path(path, str(item.get("media_type") or "")),
                       "size_bytes": size, "name": str(item.get("name") or sha)})
    return tuple(result)


# LLM: canonical 原生历史保存 local_file source；供应商适配之前不得展开 base64，避免重复持久化媒体字节。
# 函数用途: 将已验证的附件引用投影成内部原生媒体块。
def input_media_blocks(refs: object) -> list[dict]:
    return [{"type": ref["media_type"].split("/", 1)[0],
             "source": {"type": "local_file", **ref}} for ref in refs or ()]


# LLM: 仅本机结构化媒体 source 在网络边界展开；校验哈希以防落盘后被改写；不修改调用者的历史或持有进程级字节缓存。
# 函数用途: 为一次模型请求读取附件原件，并生成标准 base64 source。
def provider_media_block(block: dict) -> dict:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "local_file":
        return block
    # 总量已在 Gateway 入口按配置验证；这里仍按落盘时的长度有界读取。
    ref = validate_input_media([source], max_bytes=int(source.get("size_bytes") or 0))[0]
    with Path(ref["path"]).open("rb") as reader:
        data = reader.read(ref["size_bytes"] + 1)
    if len(data) != ref["size_bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise InputMediaError("附件内容已变化，拒绝发送不一致的字节。")
    media_type = ref["media_type"]
    if media_type == "video/quicktime":
        media_type = "video/mov"
    return {"type": block["type"], "source": {"type": "base64", "media_type": media_type,
                                               "data": base64.b64encode(data).decode("ascii")}}


# LLM: 仅 user media 块可读取文件，assistant 内容不获得文件读取权；返回新容器，不将编码结果回写 canonical messages。
# 函数用途: 发送 Anthropic 请求前临时展开媒体附件。
def provider_media_messages(messages: list[dict]) -> list[dict]:
    return [{**row, "content": [provider_media_block(block) if block.get("type") in {"image", "video"}
                               else block for block in row["content"]]}
            if row.get("role") == "user" and isinstance(row.get("content"), list) else row
            for row in messages]


# LLM: 历史只在发送投影中受媒体字节预算约束，canonical refs 永远保留；当前最新媒体消息必须完整，旧媒体显式投影为归档引用。
# 函数用途: 在编码前从新到旧选择预算内附件，避免长图片/视频历史一次性展开到内存。
def project_input_media(messages: list[dict] | None, max_bytes: int) -> list[dict] | None:
    if messages is None:
        return None
    result = list(messages)
    remaining = max_bytes
    newest = True
    for index in range(len(result) - 1, -1, -1):
        row = result[index]
        content = row.get("content")
        if row.get("role") != "user" or not isinstance(content, list):
            continue
        media = [block for block in content if isinstance(block.get("source"), dict)
                 and block["source"].get("type") == "local_file"]
        if not media:
            continue
        size = sum(int(block["source"].get("size_bytes") or 0) for block in media)
        if newest and size > max_bytes:
            raise InputMediaError("最近一条消息的附件超过当前媒体预算，请提高限制或重新添加较小附件。")
        newest = False
        if size <= remaining:
            remaining -= size
            continue
        replacements = {id(block): {"type": "text", "text": (
            f"[历史附件已归档，本轮媒体预算未加载：{block['source'].get('name', '')}；"
            f"原件引用：{block['source'].get('path', '')}。需要视觉重读时请重新添加附件。]"
        )} for block in media}
        result[index] = {**row, "content": [replacements.get(id(block), block) for block in content]}
    return result
