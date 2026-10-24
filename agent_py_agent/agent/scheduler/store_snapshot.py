# LLM: 调度写方仍用原锁与原子 rename；读方仅打开受管普通文件，不创建锁、不写目录，不返回跨代或截断字节。
# 模块用途: 为调度查询提供只读完整快照，原地违规写入或原子替换竞争只重读三次并返回明确忙碌错误。
from __future__ import annotations

import errno
from pathlib import Path

from ..common.nofollow_fs import NoFollowPathError, read_bytes_beneath


# LLM: stat 签名只核实本次读取的一代文件，不充当授权或写方版本 CAS。
# 函数用途: 获取普通文件签名，缺失返回 None，其余权限或文件系统错误保留 errno。
def snapshot_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_mtime_ns, value.st_size, value.st_ino, value.st_dev


# LLM: common no-follow 读取拒绝链接与非普通文件；完整字节必须与读取前后同一签名匹配，open 期间的原子替换同样进入有界重试。
# 函数用途: 无写锁读取一代完整账本，连续竞争达到上限时及时向调用方报告 EAGAIN。
def read_atomic_store_bytes(path: Path) -> bytes | None:
    for _attempt in range(3):
        before = snapshot_signature(path)
        try:
            content = read_bytes_beneath(path.parent, (path.name,))
        except NoFollowPathError as exc:
            if str(exc) == "managed file changed during open":
                continue
            raise
        after = snapshot_signature(path)
        if before == after and (content is None or (before is not None and len(content) == before[1])):
            return content
    raise BlockingIOError(errno.EAGAIN, "scheduler snapshot changed during bounded read")
