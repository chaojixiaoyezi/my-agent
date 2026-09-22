# LLM: 目录页必须来自预算内完整受限枚举，不用 scandir 任意前缀伪造快照；每个子项重新授权且不跟随链接。
# 模块用途: 生成有深度和扫描上限的目录结果，按稳定顺序分页并拒绝过期游标。

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_directory_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .reading import ReadError, authorized_path, decode_cursor, digest, encode_cursor, identity


# LLM: 扫描对象只属于单次调用；不跨请求缓存权限或文件系统状态，计数包括被拒绝的条目。
# 类用途: 用一个共享预算递归查看目录，保存稳定分页所需的结果与目录变化标志。
class DirectoryScan:
    # LLM: 构造不打开目录；根和上下文由已授权调用传入，列表只保存本次运行结果。
    # 函数用途: 准备扫描预算和统计。
    def __init__(self, context: WorkspaceReadContext, root: Path, depth: int, budget: int):
        self.context, self.root, self.depth, self.budget = context, root, depth, budget
        self.entries: list[dict] = []
        self.directories: list[tuple] = []
        self.scanned = self.denied = 0

    # LLM: 每层从锚点严格打开并在结束时比较原 fd 身份；对象或目录变化使整页失败，不返回不完整快照。
    # 函数用途: 枚举一个目录，把允许的普通文件/目录加入结果并在深度内递归。
    def visit(self, path: Path, level: int, expected: tuple | None = None) -> None:
        descriptor = open_directory_beneath(Path(path.anchor), path.parts[1:], require_dir_fd=True)
        try:
            before = identity(os.fstat(descriptor))
            if expected is not None and before != expected:
                raise ReadError("DIRECTORY_CHANGED", "目录在扫描期间变化，请重新读取。")
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    self.scanned += 1
                    if self.scanned > self.budget:
                        raise ReadError("SCAN_LIMIT", "目录超过扫描预算，请缩小目录或深度后重试。")
                    self.include(path / entry.name, entry.stat(follow_symlinks=False), level)
            if identity(os.fstat(descriptor)) != before:
                raise ReadError("DIRECTORY_CHANGED", "目录在扫描期间变化，请重新读取。")
            authorized_path(self.context, str(path))
            self.directories.append((str(path.relative_to(self.root)), before))
        finally:
            os.close(descriptor)

    # LLM: 被拒绝的路径、链接或设备不暴露名称；工具预算不能覆盖 SDK 的权限结果。
    # 函数用途: 筛选一项并按深度递归，记录文件身份供后续页检测变化。
    def include(self, path: Path, info: os.stat_result, level: int) -> None:
        directory = stat.S_ISDIR(info.st_mode)
        if (not (directory or stat.S_ISREG(info.st_mode)) or (not directory and info.st_nlink != 1)
                or not self.context.check(path).allowed):
            self.denied += 1
            return
        self.entries.append({"path": str(path.relative_to(self.root)), "kind": "directory" if directory else "file",
                             "size": info.st_size, "identity": list(identity(info))})
        if directory and level < self.depth:
            self.visit(path, level + 1, identity(info))


# LLM: 先完成有界扫描再分页，页数和摘要只描述本次受限视图，不宣称原子文件系统快照或未授权目录的覆盖。
# 函数用途: 返回按路径排序的一页目录内容，扫描失败不返回下一页。
def directory_tree(context: WorkspaceReadContext, path: str, *, depth: int, limit: int,
                   scan_budget: int, cursor: str | None = None) -> dict:
    if (type(depth) is not int or not 1 <= depth <= 5 or type(limit) is not int or not 1 <= limit <= 200
            or type(scan_budget) is not int or not 1 <= scan_budget <= 10000):
        raise ReadError("INVALID_BUDGET", "目录深度或读取预算无效。")
    if os.scandir not in os.supports_fd:
        raise ReadError("UNSUPPORTED_PLATFORM", "此平台不支持严格目录描述符扫描。")
    target = authorized_path(context, path)
    scan = DirectoryScan(context, target, depth, scan_budget)
    scan.visit(target, 1)
    scan.entries.sort(key=lambda item: item["path"])
    binding = digest(["tree", str(target), context.to_payload(), depth, scan_budget, scan.entries,
                      sorted(scan.directories), scan.denied])
    start = decode_cursor(cursor, binding, len(scan.entries))
    end = min(start + limit, len(scan.entries))
    result = {"path": str(target.relative_to(context.cwd)), "depth": depth, "entries": scan.entries[start:end],
              "total": len(scan.entries), "scanned": scan.scanned, "denied": scan.denied, "start": start, "end": end,
              "cursor": encode_cursor(binding, end) if end < len(scan.entries) else None}
    if len(json.dumps(result, ensure_ascii=True).encode()) > 65536:
        raise ReadError("OUTPUT_LIMIT", "目录页过大，请减小每页条目数。")
    return result
