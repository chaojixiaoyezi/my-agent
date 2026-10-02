# LLM: 作业元数据只允许进入宿主提供的插件私有目录；不能回退 cwd、HOME、安装目录，也不能把它冒充宿主任务账。
# 模块用途: 用 SDK no-follow 原语读取、枚举、原子写入和清理插件私有 JSON 作业记录。

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import (
    NoFollowPathError,
    list_names_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)

from .errors import DramaShellError

MAX_RECORD_BYTES = 256 * 1024


# LLM: 环境值由插件宿主注入，必须是现存、非链接的规范绝对目录；缺失时失败关闭而不是另选落点。
# 函数用途: 取得本次进程唯一允许保存私有作业记录的数据根。
def data_root() -> Path:
    raw = os.environ.get("MY_AGENT_PLUGIN_DATA_DIR")
    if not raw or "\x00" in raw:
        raise DramaShellError("PLUGIN_DATA_DIR_MISSING", "宿主未提供插件私有数据目录。")
    root = Path(raw)
    if not root.is_absolute() or str(root) != raw or ".." in root.parts:
        raise DramaShellError("PLUGIN_DATA_DIR_INVALID", "插件私有数据目录必须是规范绝对路径。")
    if root.is_symlink() or not root.is_dir():
        raise DramaShellError("PLUGIN_DATA_DIR_INVALID", "插件私有数据目录不可用。")
    return root


# LLM: 私有目录按 cwd 摘要隔离，不把工作区绝对路径写进记录，也避免不同工作区同名 job_id 相撞。
# 函数用途: 为当前工作区生成稳定且不泄露路径的私有命名空间。
def workspace_key(cwd: Path) -> str:
    return hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()


# LLM: job_id 仍在 JSON 正文中验证；文件名只使用摘要，避免路径字符和大小写文件系统歧义。
# 函数用途: 为作业记录生成安全稳定的文件名。
def job_key(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()


# LLM: JSON 固定排序和紧凑编码，使私有状态可复核且不会受区域设置影响；写入只走 SDK 原子替换。
# 函数用途: 原子保存一份插件私有 JSON 记录。
def write_record(root: Path, parts: tuple[str, ...], document: dict) -> None:
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(payload) > MAX_RECORD_BYTES:
        raise DramaShellError("RECORD_TOO_LARGE", "插件私有作业记录超过大小上限。")
    try:
        write_bytes_atomic_beneath(root, parts, payload)
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("PRIVATE_STORE_WRITE_FAILED", "写入插件私有作业记录失败。") from exc


# LLM: 私有记录也按 no-follow 和大小上限读取；畸形或非对象记录不得参与状态判断。
# 函数用途: 读取并解析一份插件私有 JSON 记录。
def read_record(root: Path, parts: tuple[str, ...]) -> dict:
    try:
        payload = read_bytes_beneath(root, parts, max_bytes=MAX_RECORD_BYTES, require_dir_fd=True)
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("PRIVATE_STORE_READ_FAILED", "读取插件私有作业记录失败。") from exc
    if payload is None:
        raise DramaShellError("JOB_NOT_FOUND", "未找到该作业。")
    try:
        document = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, DramaShellError) as exc:
        if isinstance(exc, DramaShellError):
            raise
        raise DramaShellError("PRIVATE_RECORD_INVALID", "插件私有作业记录损坏。") from exc
    if not isinstance(document, dict):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "插件私有作业记录不是对象。")
    return document


# LLM: 枚举只返回 SDK 已验证目录中的普通名字，调用方还必须逐个 read_record 校验正文。
# 函数用途: 列出一个私有记录目录里的 JSON 文件名。
def list_records(root: Path, parts: tuple[str, ...]) -> list[str]:
    try:
        names = list_names_beneath(root, parts)
    except FileNotFoundError:
        return []
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("PRIVATE_STORE_READ_FAILED", "枚举插件私有作业记录失败。") from exc
    return sorted(name for name in names if name.endswith(".json"))


# LLM: 重做 prepare 必须让旧确认失效；只删除 SDK 确认过的普通私有文件，缺失视为已清理。
# 函数用途: 删除一份过期私有记录。
def remove_record(root: Path, parts: tuple[str, ...]) -> None:
    try:
        unlink_file_beneath(root, parts)
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("PRIVATE_STORE_WRITE_FAILED", "清理插件私有作业记录失败。") from exc


# LLM: 私有 JSON 与工作区 JSON 一样拒绝 Python 默认接受的 NaN/Infinity，避免摘要和比较语义漂移。
# 函数用途: 让 JSON 解码器拒绝非有限数字。
def _reject_constant(value: str) -> None:
    raise DramaShellError("PRIVATE_RECORD_INVALID", f"私有记录含非有限数字：{value}。")
