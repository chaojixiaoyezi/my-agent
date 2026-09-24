# LLM: 工作区只读、插件数据目录只写；工作区文件一律经逐次读取上下文授权并用 SDK no-follow 打开，
#   快照只落在宿主传入的插件自有数据目录，本模块从不写工作区，也不从 cwd 或安装目录补数据根。
# 模块用途: 提供工作区文件的授权读取与哈希，以及按文件路径分目录的快照存取。

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import (
    list_names_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    read_text_beneath,
    write_bytes_atomic_beneath,
)
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

SNAPSHOT_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_CHUNK = 65536


# LLM: code 是稳定的业务失败分类；正文是给用户看的中文说明，不含原始异常、配置或被拒绝文件内容。
# 类用途: 让协议入口把快照失败返回为工具结果，不终止整个插件进程。
class SavepointError(ValueError):
    # LLM: extra 只放结构化事实（如当前 sha256），协议入口原样并入错误结果；不能放文件正文。
    # 函数用途: 保存错误码、中文说明和可选的附加结构化字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


# LLM: 一次读取的结构化事实；content 仅在调用方要求保留时存在，mode 用于恢复时保持原权限位。
# 类用途: 汇总当前工作区文件的 sha256、大小、权限位和（可选）内容。
@dataclass(frozen=True)
class CurrentFile:
    sha256: str
    size: int
    mode: int
    content: bytes | None


# LLM: 返回 lexical 绝对路径用于 no-follow 打开和快照分组；resolve 只在 SDK check 内做权限判断，不能先 resolve 擦掉链接。
# 函数用途: 组合当前工作区路径，拒绝空路径与上溯组件，再按读取上下文检查读取资格。
def workspace_target(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise SavepointError("INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    target = context.cwd / value
    decision = context.check(target)
    if not decision.allowed:
        raise SavepointError(decision.code, "目标不在本次允许读取的工作区范围内。")
    return target


# LLM: 目标相对 cwd 时返回相对路径，否则返回绝对路径；只用于展示，不参与授权。
# 函数用途: 生成结果里给用户看的路径。
def display_path(context: WorkspaceReadContext, target: Path) -> str:
    return str(target.relative_to(context.cwd)) if target.is_relative_to(context.cwd) else str(target)


# LLM: 从文件系统根逐段 no-follow 打开（链接、硬链接、非普通文件由 SDK 拒绝）；limit 同时检查 stat 和实际读取量，
#   前后 fstat 身份不一致视为读取期间变化；fd 在全部分支关闭。
# 函数用途: 读取工作区文件并计算 sha256，按需保留内容供保存快照。
def read_current(target: Path, *, limit: int | None, keep: bool) -> CurrentFile:
    try:
        descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    except FileNotFoundError as exc:
        raise SavepointError("FILE_NOT_FOUND", "文件不存在。") from exc
    try:
        before = os.fstat(descriptor)
        if limit is not None and before.st_size > limit:
            raise SavepointError("FILE_TOO_LARGE", f"文件超过快照上限 {limit} 字节。")
        digest, pieces, total = hashlib.sha256(), [], 0
        while chunk := os.read(descriptor, _CHUNK):
            total += len(chunk)
            if limit is not None and total > limit:
                raise SavepointError("FILE_TOO_LARGE", f"文件超过快照上限 {limit} 字节。")
            digest.update(chunk)
            if keep:
                pieces.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, total, after.st_mtime_ns):
            raise SavepointError("FILE_CHANGED", "文件在读取期间发生变化，请重试。")
        return CurrentFile(digest.hexdigest(), total, before.st_mode & 0o7777, b"".join(pieces) if keep else None)
    finally:
        os.close(descriptor)


# LLM: 数据根是宿主传入的插件自有目录，所有读写经 SDK no-follow 原语锚定在此；按目标 lexical 绝对路径的 sha256 分目录，
#   先写内容文件再写元数据，元数据存在才算快照提交。不自动删除旧快照。
# 类用途: 管理一个插件数据目录下的全部文件快照。
class SnapshotStore:
    # LLM: root 必须是绝对路径；不创建数据根本身（宿主负责），只在其下创建 snapshots 子目录。
    # 函数用途: 绑定插件数据目录。
    def __init__(self, root: Path):
        self.root = root

    # LLM: 分组键只由规范绝对路径决定；元数据里的 path 用于排除哈希碰撞或损坏条目。
    # 函数用途: 计算某个工作区文件的快照目录段。
    @staticmethod
    def group(target: Path) -> tuple[str, str]:
        return "snapshots", hashlib.sha256(str(target).encode("utf-8")).hexdigest()

    # LLM: 读取失败或字段不符的元数据计入 damaged，不中断列表，也不自动清理。
    # 函数用途: 列出某文件的有效快照元数据（按创建时间升序）及损坏条目数。
    def entries(self, target: Path) -> tuple[list[dict], int]:
        group = self.group(target)
        valid, damaged = [], 0
        for name in list_names_beneath(self.root, group):
            if not name.endswith(".json"):
                continue
            meta = self._meta(target, name[:-5])
            if meta is None:
                damaged += 1
            else:
                valid.append(meta)
        return sorted(valid, key=lambda item: (item["created_ns"], item["id"])), damaged

    # LLM: 有副作用：在插件数据目录写两个文件。数量上限按元数据文件数（含损坏条目）计算，超出拒绝且不删旧快照；
    #   并发保存可能在上限附近多写一份，属已知的尽力检查。
    # 函数用途: 为某文件保存一份新快照并返回元数据。
    def save(self, target: Path, current: CurrentFile, max_snapshots: int) -> dict:
        group = self.group(target)
        names = set(list_names_beneath(self.root, group))
        if sum(name.endswith(".json") for name in names) >= max_snapshots:
            raise SavepointError("SNAPSHOT_LIMIT", f"该文件已有 {max_snapshots} 份快照，达到上限；请先清理旧快照再保存。")
        now = datetime.now(timezone.utc)
        while True:
            snapshot_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            if snapshot_id + ".json" not in names and snapshot_id + ".bin" not in names:
                break
        meta = {"version": 1, "id": snapshot_id, "path": str(target), "sha256": current.sha256, "bytes": current.size,
                "created_at": now.isoformat(timespec="seconds"), "created_ns": time_ns(now)}
        write_bytes_atomic_beneath(self.root, (*group, snapshot_id + ".bin"), current.content or b"")
        write_bytes_atomic_beneath(self.root, (*group, snapshot_id + ".json"),
                                   json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return meta

    # LLM: 编号先按固定格式校验再作为路径段使用；内容按元数据字节数有界读取并核对 sha256，损坏不返回部分内容。
    # 函数用途: 读取某文件指定快照的元数据和内容。
    def load(self, target: Path, snapshot_id: str) -> tuple[dict, bytes]:
        meta = self._meta(target, snapshot_id) if SNAPSHOT_ID.fullmatch(snapshot_id) else None
        if meta is None:
            raise SavepointError("SNAPSHOT_NOT_FOUND", "该文件没有这个快照编号；请先用 list 查看。")
        try:
            content = read_bytes_beneath(self.root, (*self.group(target), snapshot_id + ".bin"), max_bytes=meta["bytes"])
        except (OSError, ValueError):
            content = None  # 超出记录字节数、链接或读取失败都按损坏处理
        if content is None or len(content) != meta["bytes"] or hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise SavepointError("SNAPSHOT_CORRUPT", "快照内容缺失或与记录的 sha256 不符，拒绝恢复。")
        return meta, content

    # LLM: 只接受字段齐全、编号与文件名一致、路径与目标一致的元数据；其余返回 None。
    # 函数用途: 读取并验证一条快照元数据。
    def _meta(self, target: Path, snapshot_id: str) -> dict | None:
        if not SNAPSHOT_ID.fullmatch(snapshot_id):
            return None
        try:
            text = read_text_beneath(self.root, (*self.group(target), snapshot_id + ".json"))
            meta = json.loads(text) if text is not None else None
        except (OSError, ValueError):
            return None
        valid = (isinstance(meta, dict) and meta.get("id") == snapshot_id and meta.get("path") == str(target)
                 and isinstance(meta.get("sha256"), str) and type(meta.get("bytes")) is int and meta["bytes"] >= 0
                 and type(meta.get("created_ns")) is int and isinstance(meta.get("created_at"), str))
        return meta if valid else None


# LLM: 纯换算，排序用整数纳秒避免同秒快照顺序不稳定。
# 函数用途: 把带时区的时间转成 Unix 纳秒。
def time_ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1000
