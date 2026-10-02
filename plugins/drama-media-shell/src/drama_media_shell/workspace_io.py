# LLM: 工作区读取只有这一条路：先按逐次 WorkspaceReadContext 裁决，再用 SDK no-follow 原语有界读取；不得直接 Path.read_*。
# 模块用途: 为四个检查器和生产作业读取工作区 JSON/JSONL，并把路径、编码和格式失败转成稳定错误。

from __future__ import annotations

import json
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import NoFollowPathError, read_bytes_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .errors import DramaShellError

MAX_CHECK_BYTES = 8 * 1024 * 1024
MAX_JOB_BYTES = 256 * 1024
MAX_INPUT_BYTES = 50 * 1024 * 1024


# LLM: 返回 lexical 绝对路径以供 no-follow 打开；权限判断在原路径上进行，不能先 resolve 擦掉符号链接。
# 函数用途: 校验相对工作区路径并执行宿主同源读取范围裁决。
def workspace_target(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise DramaShellError("INVALID_PATH", "路径不能为空，也不能含 NUL 或 .. 上溯组件。")
    target = context.cwd / value
    decision = context.check(target)
    if not decision.allowed:
        raise DramaShellError(decision.code, "目标不在本次允许读取的工作区范围内。")
    return target


# LLM: SDK 从文件系统根逐段拒绝链接和非普通文件；limit 约束实际读取量，缺失与超限不返回部分内容。
# 函数用途: 安全、有界地读取一个工作区文件的原始字节。
def read_workspace_bytes(context: WorkspaceReadContext, value: str, limit: int) -> bytes:
    target = workspace_target(context, value)
    try:
        content = read_bytes_beneath(Path(target.anchor), target.parts[1:], max_bytes=limit, require_dir_fd=True)
    except NoFollowPathError as exc:
        raise DramaShellError("UNSAFE_PATH", "路径含符号链接、多链接文件或非普通对象，已拒绝。") from exc
    except ValueError as exc:
        raise DramaShellError("FILE_TOO_LARGE", f"文件超过 {limit} 字节上限。") from exc
    except OSError as exc:
        raise DramaShellError("IO_FAILED", "读取工作区文件失败。") from exc
    if content is None:
        raise DramaShellError("FILE_NOT_FOUND", "工作区文件不存在。")
    return content


# LLM: JSON 解析拒绝 NaN/Infinity，并只返回对象；错误不回显原文。
# 函数用途: 安全读取一份工作区 JSON 对象。
def read_json_object(context: WorkspaceReadContext, value: str, limit: int = MAX_JOB_BYTES) -> dict:
    content = read_workspace_bytes(context, value, limit)
    try:
        document = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, DramaShellError) as exc:
        if isinstance(exc, DramaShellError):
            raise
        raise DramaShellError("INVALID_JSON", "文件不是有效的 UTF-8 JSON。") from exc
    if not isinstance(document, dict):
        raise DramaShellError("INVALID_JSON", "JSON 顶层必须是对象。")
    return document


# LLM: 每行独立解析且拒绝非对象；comments 仅为 motion 上游合同保留井号注释，不改变其他检查器语义。
# 函数用途: 安全读取工作区 JSONL 对象列表。
def read_jsonl(context: WorkspaceReadContext, value: str, comments: bool = False,
               limit: int = MAX_CHECK_BYTES) -> list[dict]:
    content = read_workspace_bytes(context, value, limit)
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise DramaShellError("INVALID_JSONL", "JSONL 不是有效的 UTF-8 文本。") from exc
    records: list[dict] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or (comments and stripped.startswith("#")):
            continue
        records.append(_parse_jsonl_object(stripped, number))
    if not records:
        raise DramaShellError("EMPTY_INPUT", "JSONL 没有可检查的记录。")
    return records


# LLM: 单行解析独立出来以限制文件循环嵌套；异常只带行号，不回显可能敏感的输入正文。
# 函数用途: 把一行 UTF-8 JSONL 文本解析成对象。
def _parse_jsonl_object(line: str, number: int) -> dict:
    try:
        record = json.loads(line, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise DramaShellError("INVALID_JSONL", f"JSONL 第 {number} 行不是有效 JSON。") from exc
    if not isinstance(record, dict):
        raise DramaShellError("INVALID_JSONL", f"JSONL 第 {number} 行必须是对象。")
    return record


# LLM: 只识别上游约定的首行 sources 记录；调用方决定是否使用声明，原列表不会被共享修改。
# 函数用途: 从 JSONL 记录中拆出来源声明和业务记录。
def split_sources(records: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    copied = list(records)
    if not copied or copied[0].get("record_type") != "sources":
        return {}, copied
    header = copied.pop(0)
    sources = header.get("sources")
    if not isinstance(sources, dict) or not all(isinstance(key, str) and isinstance(item, dict)
                                                for key, item in sources.items()):
        raise DramaShellError("INVALID_SOURCES", "sources 首行必须声明对象映射。")
    if not copied:
        raise DramaShellError("EMPTY_INPUT", "来源声明后没有可检查记录。")
    return dict(sources), copied


# LLM: Python JSON 默认接受非标准 NaN/Infinity；上游时序和作业合同要求失败关闭。
# 函数用途: 让 JSON 解码器拒绝非有限数字常量。
def _reject_constant(value: str) -> None:
    raise DramaShellError("INVALID_JSON_NUMBER", f"不允许非有限 JSON 数字：{value}。")
