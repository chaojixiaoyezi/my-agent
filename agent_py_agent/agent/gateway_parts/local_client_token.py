# LLM: G1 本机客户端凭据的唯一宿主接口；只在缺失时生成，使用共用私有原子写与锁，不导出到日志/env/argv/status。
#   读取失败只给结构化原因，不回显路径或内容；客户端统一用 load。Full Access 的模型命令可读数据根，G1 不声称挡住它。
# 模块用途: 持久保存并安全读取本机客户端凭据，让 Gateway 重启不使已运行客户端失效。
from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..path_access_policy import HOST_SECRET_DIR_NAME

LOCAL_CLIENT_CREDENTIAL_FILE_NAME = "gateway-local-client-token"
# 随机凭据的熵字节数，token_urlsafe 编码后供宿主请求头使用。
LOCAL_CLIENT_CREDENTIAL_ENTROPY_BYTES = 32
# 32 字节无填充 URL-safe base64 的固定字符数；拒绝空串、损坏或非协议内容。
LOCAL_CLIENT_CREDENTIAL_ENCODED_CHARS = 43
# 读取凭据文件的最大字节数；避免损坏的大文件拖累宿主启动。
LOCAL_CLIENT_CREDENTIAL_READ_BYTES = 128


# LLM: 读取失败必须有结构化原因，禁止把凭据内容放进异常。
# 类用途: 表示宿主凭据无法安全读取。
class LocalClientCredentialError(RuntimeError):
    # LLM: 原因码是机器合同；异常文字不能包含凭据、底层异常或私人路径。
    # 函数用途: 构造仅含原因码的宿主凭据读取错误。
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        self.error_code = reason_code
        super().__init__(f"本机客户端凭据无法安全读取（{reason_code}）")


# LLM: 仅拼接明确的数据根，不读环境或猜 owner；插件 H2 与启动钩子必须使用同一路径。
# 函数用途: 返回本机客户端凭据的规范路径。
def local_client_credential_path(data_root: str | Path) -> Path:
    return Path(data_root) / HOST_SECRET_DIR_NAME / LOCAL_CLIENT_CREDENTIAL_FILE_NAME


# LLM: 属主与权限都须匹配；不替客户端修权限，失败不得静默降级成无凭据。
# 函数用途: 校验已取得的文件元信息是否只对宿主系统用户开放。
def _check_private_stat(info: os.stat_result, mode: int) -> None:
    if stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.geteuid():
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_PERMISSIONS")


# LLM: 从结构化 errno 分类，不把原异常（可能含路径）传给日志或客户端。
# 函数用途: 把文件缺失、权限拒绝和其它 I/O 问题变成不同的原因码。
def _credential_io_error(exc: OSError) -> LocalClientCredentialError:
    if exc.errno == errno.ENOENT:
        return LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_MISSING")
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_PERMISSIONS")
    return LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_UNREADABLE")


# LLM: 不跟凭据/直接父目录的符号链接；限额读取后只接受固定格式，坏内容不能进入异常链。
# 函数用途: 从私有普通文件读取并验证随机凭据。
def _read_credential(path: Path) -> str:
    info = path.lstat()
    parent = path.parent.lstat()
    if not stat.S_ISREG(info.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID")
    _check_private_stat(parent, 0o700)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        _check_private_stat(os.fstat(handle.fileno()), 0o600)
        raw = handle.read(LOCAL_CLIENT_CREDENTIAL_READ_BYTES + 1)
    token = raw.removesuffix(b"\n").decode("ascii", errors="replace")
    if not re.fullmatch(r"[A-Za-z0-9_-]{" + str(LOCAL_CLIENT_CREDENTIAL_ENCODED_CHARS) + "}", token):
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID")
    return token


# LLM: 公开读取入口，不生成、不返回空值；后续客户端统一消费。
# 函数用途: 从明确的数据根读取本机客户端凭据。
def load_local_client_credential(data_root: str | Path) -> str:
    try:
        return _read_credential(local_client_credential_path(data_root))
    except OSError as exc:
        raise _credential_io_error(exc) from None


# LLM: 只由宿主启动调用；锁覆盖存在检查与生成，跨线程/进程不覆盖赢家。坏文件/权限错误保留原件并拒绝启动。
#   写入和目录收紧只复用 json_io，不自写 chmod；落盘后严读保证私有写的尽力收紧不被误当成成功。
# 函数用途: 确保本机客户端凭据存在并返回其值。
def ensure_local_client_credential(data_root: str | Path) -> str:
    path = local_client_credential_path(data_root)
    try:
        with locked_json_path(path):
            return _ensure_credential_locked(data_root, path)
    except OSError as exc:
        raise _credential_io_error(exc) from None


# LLM: 调用方持共用不可重入锁；仅 missing 可生成，其它错误不能造成轮换。不得设置环境变量或调用日志。
# 函数用途: 在原临界区内复用已有凭据，或私有原子写入新凭据。
def _ensure_credential_locked(data_root: str | Path, path: Path) -> str:
    try:
        return load_local_client_credential(data_root)
    except LocalClientCredentialError as exc:
        if exc.reason_code != "LOCAL_CLIENT_CREDENTIAL_MISSING":
            raise
    token = secrets.token_urlsafe(LOCAL_CLIENT_CREDENTIAL_ENTROPY_BYTES)
    write_private_text_file_atomic_unlocked(path, token + "\n")
    return load_local_client_credential(data_root)
