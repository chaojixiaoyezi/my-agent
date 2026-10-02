from __future__ import annotations

# LLM: H3（3a 最终裁定的顺手项，2026-10-02）：模型在宿主命令沙箱里跑 my-agent CLI 时宿主状态只读（A 类），而 CLI 每条命令都要
#   构造完整 SimpleAgent、启动时写 workspace/runtime 下的本地库，所以起不来（Full Access 和隔离 owner 都这样）。这里只把这种失败
#   换成一行结构化错误：判断只读结构化事实——宿主给沙箱命令设的环境标记（path_access_policy.HOST_STATE_READ_ONLY_ENV，由
#   tooling/shell._subprocess_text_env 设）加异常的 errno / sqlite 错误码，不解析报错文字；其它异常原样抛出，沙箱外的 CLI 行为不变。
#   只读子命令走只读启动（库用 mode=ro 打开）是台账里的待做项，不在这里做。改动同步 test_host_files_access.py 的真实沙箱用例。
# 模块用途: 让 my-agent CLI 在模型命令沙箱里因宿主状态只读起不来时，输出结构化错误码而不是 sqlite/权限异常堆栈。
import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from ..agent.path_access_policy import HOST_STATE_READ_ONLY_ENV

# 常量用途: 表示“没有写权限”的 errno（Seatbelt 拒写是 EPERM，bwrap 只读挂载是 EROFS，普通权限是 EACCES）。
_PERMISSION_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS})
# 常量用途: 表示“没有写权限”的 sqlite 主错误码：SQLITE_PERM=3、SQLITE_READONLY=8、SQLITE_CANTOPEN=14（库或伴随文件建不出来）。
_SQLITE_PERMISSION_CODES = frozenset({3, 8, 14})
# 常量用途: 结构化错误码，写在 stderr 的一行里，模型和人都按它判断。
CLI_HOST_STATE_READ_ONLY = "CLI_HOST_STATE_READ_ONLY"


# LLM: 包住非交互命令的整个执行（启动和命令本身）：沙箱里命令真要写宿主状态时同样给这个码，不崩出堆栈。退出码 1。
# 函数用途: 在命令沙箱里把宿主状态只读导致的权限类失败换成一行结构化错误并退出。
@contextmanager
def host_state_read_only_guard() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        if not _host_state_permission_failure(exc):
            raise
        path = str(getattr(exc, "filename", "") or "")
        print(f"my-agent: error_code={CLI_HOST_STATE_READ_ONLY} path={path} "
              "在模型命令的沙箱里宿主状态只读，my-agent CLI 起不来；请改用对应的内置工具。", file=sys.stderr)
        raise SystemExit(1) from exc


# LLM: 只认宿主设的标记 "1" 和结构化错误码；Python 3.10 的 sqlite 异常没有 sqlite_errorcode，按不是权限失败处理（原样抛出）。
# 函数用途: 判断一个异常是否是“命令沙箱里宿主状态只读”造成的权限失败。
def _host_state_permission_failure(exc: BaseException) -> bool:
    if os.environ.get(HOST_STATE_READ_ONLY_ENV) != "1":
        return False
    if isinstance(exc, OSError):
        return exc.errno in _PERMISSION_ERRNOS
    import sqlite3

    code = getattr(exc, "sqlite_errorcode", None) if isinstance(exc, sqlite3.Error) else None
    return isinstance(code, int) and (code & 0xFF) in _SQLITE_PERMISSION_CODES


__all__ = ["CLI_HOST_STATE_READ_ONLY", "host_state_read_only_guard"]
