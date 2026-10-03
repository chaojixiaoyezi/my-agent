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

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import (
    NoFollowPathError,
    open_directory_beneath,
    open_readonly_file_beneath,
    write_text_atomic_beneath,
)
from ..path_access_policy import HOST_SECRET_DIR_NAME

LOCAL_CLIENT_CREDENTIAL_FILE_NAME = "gateway-local-client-token"
_LOCAL_CLIENT_CREDENTIAL_PARTS = (HOST_SECRET_DIR_NAME, LOCAL_CLIENT_CREDENTIAL_FILE_NAME)
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
    return Path(data_root).joinpath(*_LOCAL_CLIENT_CREDENTIAL_PARTS)


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


# LLM: 数据根以下每一级目录与凭据叶子都经 no-follow dirfd 原语读取；所有权/权限和格式仍是 G1 合同。
# 函数用途: 从私有普通文件读取并验证随机凭据，不允许路径中的目录或文件符号链接。
def _read_credential(data_root: str | Path) -> str:
    try:
        directory = open_directory_beneath(data_root, _LOCAL_CLIENT_CREDENTIAL_PARTS[:-1])
    except NoFollowPathError:
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID") from None
    try:
        _check_private_stat(os.fstat(directory), 0o700)
        try:
            descriptor = open_readonly_file_beneath(data_root, _LOCAL_CLIENT_CREDENTIAL_PARTS)
        except NoFollowPathError:
            raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID") from None
        with os.fdopen(descriptor, "rb") as handle:
            _check_private_stat(os.fstat(handle.fileno()), 0o600)
            raw = handle.read(LOCAL_CLIENT_CREDENTIAL_READ_BYTES + 1)
    finally:
        os.close(directory)
    token = raw.removesuffix(b"\n").decode("ascii", errors="replace")
    if not re.fullmatch(r"[A-Za-z0-9_-]{" + str(LOCAL_CLIENT_CREDENTIAL_ENCODED_CHARS) + "}", token):
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID")
    return token


# LLM: 公开读取入口，不生成、不返回空值；后续客户端统一消费。
# 函数用途: 从明确的数据根读取本机客户端凭据。
def load_local_client_credential(data_root: str | Path) -> str:
    try:
        return _read_credential(data_root)
    except NoFollowPathError:
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID") from None
    except OSError as exc:
        raise _credential_io_error(exc) from None


# LLM: This read-only preflight runs before the shared lock primitive, whose contract tightens the directory it opens.
# 函数用途: 在任何凭据锁、目录权限调整或写入之前，确认已有 secrets 目录未被链接且权限已经合规。
def _check_existing_secret_directory(data_root: str | Path) -> None:
    try:
        descriptor = open_directory_beneath(data_root, _LOCAL_CLIENT_CREDENTIAL_PARTS[:-1])
    except FileNotFoundError:
        return
    except NoFollowPathError:
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID") from None
    try:
        _check_private_stat(os.fstat(descriptor), 0o700)
    finally:
        os.close(descriptor)


# LLM: 只由宿主启动调用；固定 secrets 目录锁与所有文件操作都经 no-follow 原语，坏文件/权限错误保留原件并拒绝启动。
#   目录检查、收紧和原子写入不能沿路径跟随链接；落盘后严读，不能把失败的私有写误当成功。
# 函数用途: 确保本机客户端凭据存在并返回其值。
def ensure_local_client_credential(data_root: str | Path) -> str:
    try:
        _check_existing_secret_directory(data_root)
        with locked_private_directory(
            Path(data_root),
            lock_name=LOCAL_CLIENT_CREDENTIAL_FILE_NAME + ".lock",
            relative_parts=(_LOCAL_CLIENT_CREDENTIAL_PARTS[0],),
        ):
            return _ensure_credential_locked(data_root)
    except NoFollowPathError:
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_INVALID") from None
    except OSError as exc:
        raise _credential_io_error(exc) from None


# LLM: 调用方持受信目录锁；仅 missing 可生成，其它错误不能造成轮换。写入在 data_root 下逐层 no-follow 原子提交。
# 函数用途: 在原临界区内复用已有凭据，或私有原子写入新凭据。
def _ensure_credential_locked(data_root: str | Path) -> str:
    try:
        return load_local_client_credential(data_root)
    except LocalClientCredentialError as exc:
        if exc.reason_code != "LOCAL_CLIENT_CREDENTIAL_MISSING":
            raise
    token = secrets.token_urlsafe(LOCAL_CLIENT_CREDENTIAL_ENTROPY_BYTES)
    write_text_atomic_beneath(data_root, _LOCAL_CLIENT_CREDENTIAL_PARTS, token + "\n")
    return load_local_client_credential(data_root)
