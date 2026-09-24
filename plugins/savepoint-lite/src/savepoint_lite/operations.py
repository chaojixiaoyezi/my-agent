# LLM: 三个工具的业务流程；save/list 只读工作区、只写插件数据目录；restore 是唯一写工作区的入口，
#   必须先核对 expect，再经写入上下文 check/anchor 裁决，最后用 SDK no-follow 原子替换写回。
# 模块用途: 实现保存、列出和恢复快照的具体步骤与结果结构。

from __future__ import annotations

import re

from my_agent_plugin_api.nofollow_fs import NoFollowPathError, write_bytes_atomic_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext
from my_agent_plugin_api.workspace_write_context import WorkspaceWriteContext

from .snapshots import SavepointError, SnapshotStore, display_path, read_current, workspace_target

_EXPECT = re.compile(r"[0-9a-f]{8,64}")


# LLM: 大小上限来自设置 max_file_bytes，数量上限来自 max_snapshots_per_file；有副作用：写插件数据目录。
# 函数用途: 读取工作区文件并保存为一份新快照。
def save_snapshot(context: WorkspaceReadContext, store: SnapshotStore, path: str, settings: dict) -> dict:
    target = workspace_target(context, path)
    current = read_current(target, limit=settings["max_file_bytes"], keep=True)
    meta = store.save(target, current, settings["max_snapshots_per_file"])
    return {"path": display_path(context, target), "id": meta["id"], "sha256": meta["sha256"],
            "bytes": meta["bytes"], "created_at": meta["created_at"]}


# LLM: 当前文件不存在时仍列出历史快照（current_sha256 为 null）；只读，不修改任何文件。
# 函数用途: 列出文件的快照并标出与当前内容相同的条目。
def list_snapshots(context: WorkspaceReadContext, store: SnapshotStore, path: str) -> dict:
    target = workspace_target(context, path)
    try:
        current = read_current(target, limit=None, keep=False).sha256
    except SavepointError as exc:
        if exc.code != "FILE_NOT_FOUND":
            raise
        current = None
    entries, damaged = store.entries(target)
    return {"path": display_path(context, target), "current_sha256": current, "damaged": damaged,
            "snapshots": [{"id": item["id"], "created_at": item["created_at"], "bytes": item["bytes"],
                           "sha256": item["sha256"][:12], "current": item["sha256"] == current} for item in entries]}


# LLM: 顺序固定：读取授权 → 重读当前文件核对 expect（不符返回当前 sha256）→ 载入并校验快照 → 写入上下文 check →
#   anchor 结果必须等于 lexical 路径（否则路径链含链接，拒绝）→ 保持原权限位原子替换。有副作用：覆盖工作区文件。
# 函数用途: 在确认当前内容未被他人改动后，把文件恢复为指定快照。
def restore_snapshot(context: WorkspaceReadContext, write: WorkspaceWriteContext, store: SnapshotStore,
                     path: str, snapshot_id: str, expect: str) -> dict:
    expect = expect.lower()
    if not _EXPECT.fullmatch(expect):
        raise SavepointError("INVALID_EXPECT", "--expect 必须是当前文件 sha256 的十六进制前缀，至少 8 位。")
    target = workspace_target(context, path)
    current = read_current(target, limit=None, keep=False)
    if not current.sha256.startswith(expect):
        raise SavepointError("EXPECT_MISMATCH", "当前文件内容与 --expect 不符，可能已被他人修改；已拒绝恢复。",
                             current_sha256=current.sha256)
    meta, content = store.load(target, snapshot_id)
    decision = write.check(target)
    if not decision.allowed:
        raise SavepointError(decision.code, "目标不在本次允许写入的工作区范围内。")
    root, parts = write.anchor(target)
    if root.joinpath(*parts) != target:
        raise NoFollowPathError("write anchor differs from lexical target")
    write_bytes_atomic_beneath(root, parts, content, file_mode=current.mode)
    return {"path": display_path(context, target), "id": meta["id"], "sha256": meta["sha256"], "bytes": meta["bytes"],
            "previous_sha256": current.sha256}
