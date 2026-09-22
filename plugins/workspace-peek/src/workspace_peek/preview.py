# LLM: 文件预览不写入、不执行内容；权限、路径链、文件身份和 UTF-8 边界每次调用重新核验。
# 模块用途: 在固定文件描述符上读取有限字节并产生可续读的文本页。

from __future__ import annotations

import codecs
import os
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_readonly_file_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .reading import ReadError, authorized_path, decode_cursor, digest, encode_cursor, identity


# LLM: 预算限制实际 read；游标绑定权限/path/stat，不用路径重开做第二次内容读取，fd 在全部分支关闭。
# 函数用途: 读取文本的一页，返回字节区间、原换行、EOF 与下一页游标。
def preview(context: WorkspaceReadContext, path: str, *, budget: int, cursor: str | None = None) -> dict:
    if type(budget) is not int or not 4 <= budget <= 16384:
        raise ReadError("INVALID_BUDGET", "每页字节数必须在 4 到 16384 之间。")
    target = authorized_path(context, path)
    descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    try:
        before = identity(os.fstat(descriptor))
        binding = digest(["show", str(target), context.to_payload(), before])
        start = decode_cursor(cursor, binding, before[2])
        os.lseek(descriptor, start, os.SEEK_SET)
        raw = os.read(descriptor, budget)
        if identity(os.fstat(descriptor)) != before:
            raise ReadError("FILE_CHANGED", "文件在读取期间变化，请重新读取。")
        eof = start + len(raw) >= before[2]
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            text = decoder.decode(raw, final=eof)
        except UnicodeError as exc:
            raise ReadError("NOT_UTF8", "此预览只支持有效 UTF-8 文本。") from exc
        if "\x00" in text:
            raise ReadError("BINARY_CONTENT", "内容含空字节，不能作为文本预览。")
        end = start + len(raw) - len(decoder.getstate()[0])
        if end == start and not eof:
            raise ReadError("FILE_CHANGED", "未能取得下一段文本，请重新读取。")
        authorized_path(context, path)
        return {"path": str(target.relative_to(context.cwd)), "text": text, "start": start, "end": end,
                "size": before[2], "identity": list(before), "eof": eof,
                "cursor": None if eof else encode_cursor(binding, end)}
    finally:
        os.close(descriptor)
