# LLM: 文件代次指纹与粗 mtime 窗口的唯一实现；所有“按文件指纹缓存”的读取路径共用这里的判定。
#   改动窗口语义要同步 json_io、response_renderer、subagents persistence 三处消费方及各自回归测试。
# 模块用途: 给按文件内容做缓存的读取路径提供统一的文件代次指纹和 2 秒可信窗口判断，
#   防止低精度时间片里“先读后写”的原子替换把旧内容留在缓存里。

from __future__ import annotations

import os
import time

# 文件 mtime 距现在不足这个秒数时，按它建立或命中的缓存都不可信：低精度文件系统的同一时间片里
# 可能发生原子替换（inode 复用、时间戳同片）。窗口内读到的结果只返回、不放进缓存。
CACHE_TRUST_AGE_SECONDS = 2.0


# LLM: 指纹必须同时含 dev/ino/size/mtime/ctime；原子替换必然换 inode，只比 mtime+size 会在同一
#   时间片漏掉整代替换。返回不可变 tuple，可直接当缓存键。纯函数，无副作用。
# 函数用途: 从一次 stat 结果生成文件代次指纹，供各缓存判断文件有没有换过代。
def cache_stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


# LLM: 判定方向是“够老了才可信”：mtime 距现在 ≥ CACHE_TRUST_AGE_SECONDS 才允许信任已有条目
#   或把本次读到的内容写进缓存；窗口内一律只返回本次读到的内容。纯函数，读一次挂钟。
# 函数用途: 判断一个文件的 mtime 是否已离开 2 秒粗粒度保护窗口（可信、可入缓存）。
def cache_entry_trustworthy(mtime_ns: int) -> bool:
    return time.time() - mtime_ns / 1_000_000_000 >= CACHE_TRUST_AGE_SECONDS
