# LLM: 文件版本是读写前置条件，不是权限或跨进程内核 CAS；外部程序在最后检查与 rename 间仍可能竞争。
# 模块用途: 给三个文件修改入口提供一致的过期版本错误，避免依据旧读取静默覆盖已变化文件。
from __future__ import annotations

import hashlib
from pathlib import Path

from .text_file_window import text_file_fingerprint


# LLM: 版本过期不得重试同一旧内容；调用方应返回 STALE_VERSION，并要求重新读取与合并。
# 类用途: 表示文件状态已变化，当前修改尚不应提交。
class StaleFileVersionError(OSError):
    pass


# LLM: 版本只投影 inode/size/mtime/ctime，不读整文件，也不能作为内容真伪或授权凭据。
# 函数用途: 生成当前文件的轻量版本号，缺失文件使用显式 absent。
def file_version(path: Path) -> str:
    try:
        fingerprint = text_file_fingerprint(path)
    except FileNotFoundError:
        return "absent"
    digest = hashlib.sha256(repr(fingerprint).encode("ascii")).hexdigest()
    return "file-v1:" + digest


# LLM: 预期版本省略时仍返回当前版本，便于同一次工具调用的读改写前后复检；参数不能由自然语言解析。
# 函数用途: 核对模型提供的旧版本，并返回本次观察到的版本。
def check_file_version(path: Path, expected: object = None) -> str:
    if expected is not None and (not isinstance(expected, str) or not expected or len(expected) > 128):
        raise ValueError("expected_version 必须是 read_file 返回的非空版本字符串")
    actual = file_version(path)
    if expected is not None and actual != expected:
        raise StaleFileVersionError("STALE_VERSION: 文件已变化，请重新 read_file 并合并修改，不要重放旧内容")
    return actual
