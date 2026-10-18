# LLM: P3 文件系统观察辅助；缓存和耐久确认只属于一个 gate 的固定目录，不保存业务状态或共享跨 owner 结论。
#   改动必须联测 review26 的 ctime 碰撞、首次/重试链同步、稳态成本；记录原子提交仍由 admission 模块负责。
# 模块用途: 给 released 扫描提供文件身份与 racy 判据，并按实例确认整条预留目录链。
from __future__ import annotations

import os
import time

from ..common.nofollow_fs import ensure_private_dir


# LLM: 五元组同时覆盖换 inode 和同 inode 内容改写；ctime 不能用 utime 恢复，不能仅比较 mtime/size。
# 函数用途: 返回一次 stat 的设备、inode、长度及两种修改时间。
def reservation_signature(info) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: POSIX 常见的十进制时间量化按实际 ctime 对齐保守放大，含 10/100ns 等中间粒度；偶然对齐只会多读。
#   整秒、无有效 ctime、Windows（ctime 是出生时间）不当细粒度内容不变证明；命中时墙钟回退也须回读。
# 函数用途: 判断已读缓存是否太接近文件时间量化边界，不够稳妥时不允许跳过读取。
def released_cache_is_stable(cached, info) -> bool:
    signature, observed_ns = cached
    if os.name == "nt" or signature != reservation_signature(info) or info.st_ctime_ns <= 0:
        return False
    # 历史观察已远离 ctime 也不能盖过当前墙钟回退，否则未来 ctime 会再次落入同一量化窗。
    if time.time_ns() < observed_ns:
        return False
    precision_ns = _ctime_precision_ns(info.st_ctime_ns)
    return observed_ns - info.st_ctime_ns > precision_ns


# LLM: 所支持的本地 POSIX 时间戳采用十进制量化；整秒至少保守覆盖一秒，不能把 Python 的 ns 单位当文件精度。
#   这不是性能配置；粗时间戳宁可重读，只有 ctime 不整除任何十进制粗粒度才用纳秒边界。
# 函数用途: 从 ctime 的量化对齐取保守窗口，偶然整齐的时间戳不缩小保护。
def _ctime_precision_ns(ctime_ns: int) -> int:
    for exponent in range(9, 0, -1):
        precision_ns = 10 ** exponent
        if ctime_ns % precision_ns == 0:
            return precision_ns
    return 1


# LLM: 目录存在不等于耐久已确认；每个新 gate 首写均重新确认，失败不发布缓存，换目录身份重新确认。
#   sync 为原 admission 目录 fsync 原语，保留错误注入与平台边界；成功后的稳态不增加 fsync。
# 函数用途: 建齐 gate 的预留目录，浅到深同步整链，全成功才记住身份。
def confirm_reservation_chain(gate, directory, sync) -> None:
    ensure_private_dir(directory)
    chain = (gate.paths.root.parent, gate.paths.root, directory.parent, directory)
    identities = tuple(_directory_identity(path) for path in chain)
    if gate._durable_chain == identities:
        return
    gate._durable_chain = None
    for path in chain:
        sync(path)
    if identities != tuple(_directory_identity(path) for path in chain):
        raise OSError("admission directory chain changed during fsync")
    gate._durable_chain = identities


# LLM: 目录的 dev/inode 是同一实例确认的范围，不以路径相同继承被替换目录的耐久假设。
# 函数用途: 读取目录身份；观察失败向上抛，让准入保持未发布。
def _directory_identity(path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino
