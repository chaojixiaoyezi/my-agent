# LLM: nonce是唯一业务授权账；固定nonce→A锁序，管理的目录/锁出生和每耐久写均经原file commit，等待/网络不占C短门。
# 模块用途: 安全持久保存预览、直接确认原授权和一次性消费状态。
import json
import os
import re
import threading
from contextlib import contextmanager

from ..common.nofollow_fs import _openat_file
from ._agents_contract_fields import AgentsContractError
from .agents_source_files import (
    AGENTS_CONTROL_MAX_BYTES,
    open_control_directory,
    read_source_bytes,
    write_control_json,
)

_MUTEX = threading.RLock()


# LLM: nonce只能是固定长度摘要，不能让外部文本成为文件路径。
# 函数用途: 生成独立原授权记录的安全文件名。
def record_name(nonce):
    if type(nonce) is not str or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise AgentsContractError("AGENTS_WRITE_NONCE_INVALID", "authorization_id")
    return "agents-control-" + nonce + ".json"


# LLM: nonce锁在来源锁外；可选提交回调只包目录/锁FD创建，flock等待与后续业务在回调外。
# 函数用途: 原子串行化原授权发行、消费与结果落账。
@contextmanager
def control_transaction(home, *, file_commit=None):
    import fcntl
    with _MUTEX:
        # LLM: 只开本次原owner控制目录及锁FD，不等flock；失败关闭自己已经打开的FD。
        # 函数用途: 在管理原短门内完成锁文件出生。
        def open_lock():
            parent = open_control_directory(home, create=True)
            try:
                return parent, _openat_file(parent, "agents_controls.lock", os.O_RDWR | os.O_CREAT, 0o600)
            except BaseException:
                os.close(parent)
                raise
        parent, opened = file_commit(open_lock, owner_home=home) if file_commit is not None else open_lock()
        descriptor = None
        try:
            descriptor = opened
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)


# LLM: 使用A已有的严格同FD读取，缺记录与坏记录严格区分。
# 函数用途: 在C锁内取得独立原请求，不接收调用方JSON替代。
def read_record(home, nonce):
    try:
        raw, _ = read_source_bytes(home / "persona" / record_name(nonce))
    except FileNotFoundError:
        return None
    if len(raw) > AGENTS_CONTROL_MAX_BYTES:
        raise AgentsContractError("AGENTS_WRITE_LEDGER_INVALID", "ledger")
    result = json.loads(raw)
    if type(result) is not dict or result.get("schema_version") != 1 or result.get("nonce") != nonce:
        raise AgentsContractError("AGENTS_WRITE_LEDGER_INVALID", "ledger")
    return result


# LLM: 原子替换加文件和目录fsync；失败不推断成功，消费记录不删除、不恢复pending。
# 函数用途: 耐久写入单条预览/授权/消费/结果记录。
def save_record(home, record):
    write_control_json(home, record_name(record["nonce"]), record)
