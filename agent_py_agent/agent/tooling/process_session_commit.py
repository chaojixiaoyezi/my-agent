# LLM: redo 是同一 Store 的短期安装日志，不是第二套进程状态；调用方必须持目录锁，恢复不启动或终止进程。
# 模块用途: 原版本安装一批 v2/v3/v4 记录，中断后补齐原批次，明确已提交但未安装完的结果，不迁移旧 session。
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_json_file_atomic_unlocked
from .process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    merge_process_record,
    validate_process_record,
    validate_session_id,
)

_REDO_SCHEMA = "managed_process_commit.v1"
_REDO_FILE = ".process-sessions.redo.json"


# LLM: 回执冻结本次 session 集合；recovery_required 不代表提交失败，调用方不能重新扫描或重复启动。
# 类用途: 返回一次文件事务的身份、完整有效记录和是否仍需安装恢复。
@dataclass(frozen=True)
class ProcessSessionCommitReceipt:
    transaction_id: str
    records: tuple[dict[str, object], ...]
    recovery_required: bool = False
    committed: bool = True


# LLM: 仅在 redo 已发布后抛出，receipt 保留原批次；不得转换成“未发生”或无条件重试业务副作用。
# 类用途: 告诉调用方事务已经提交，但磁盘安装或日志清除失败，需要恢复这一次事务。
class ProcessSessionCommitPendingError(RuntimeError):
    # LLM: 不把命令或记录正文放入异常文案；调用方从具名回执读取已提交事实。
    # 函数用途: 封装待恢复回执，让停止控制能够保留冻结的资源清单。
    def __init__(self, receipt: ProcessSessionCommitReceipt) -> None:
        super().__init__("managed process commit requires recovery")
        self.receipt = receipt


# LLM: 读取后验证文件名与正文身份；空对象和截断文件都不是缺失，不得被新记录覆盖。
# 函数用途: 在已持锁的目录里读一条完整进程记录，仅文件不存在时返回 None。
def read_process_record(root: Path, session_id: str) -> dict[str, object] | None:
    path = root / f"{validate_session_id(session_id)}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    record = validate_process_record(payload)
    if record["session_id"] != session_id:
        raise ValueError("managed process filename and record identity differ")
    return record


# LLM: 摘要只比较完整结构化记录，不从命令文本推导生命周期；拒绝非 JSON 值和非有限浮点。
# 函数用途: 生成记录内容指纹，用来识别原版本、已经安装的版本和冲突版本。
def _record_digest(record: dict[str, object]) -> str:
    content = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# LLM: 批量事务显式接纳 v2/v3/v4 原版本；先校验全批再发布，路径和记录身份不能由 redo 任意指定。
# 函数用途: 检查暂存日志格式、重复句柄和记录版本，返回可恢复的固定条目。
def _validate_redo(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "transaction_id", "entries"}:
        raise ValueError("invalid managed process redo")
    if payload["schema"] != _REDO_SCHEMA or not isinstance(payload["transaction_id"], str):
        raise ValueError("unsupported managed process redo schema")
    if not re.fullmatch(r"[0-9a-f]{32}", payload["transaction_id"]):
        raise ValueError("invalid managed process transaction id")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("managed process redo entries are required")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"before_digest", "record"}:
            raise ValueError("invalid managed process redo entry")
        before = entry["before_digest"]
        if before is not None and (
            not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before)
        ):
            raise ValueError("invalid managed process prior digest")
        record = validate_process_record(entry["record"])
        if record["schema"] not in MANAGED_PROCESS_SESSION_SCHEMAS or record["revision"] < 1:
            raise ValueError("managed process redo requires versioned managed records")
        if record["session_id"] in seen:
            raise ValueError("duplicate managed process redo identity")
        seen.add(record["session_id"])
        _record_digest(record)
        entry["record"] = record
    return payload


# LLM: 整批检查原/目标摘要并保持 v2/v3/v4 原 schema；后续版本、丢失或损坏记录拒绝，旧 redo 不能迁移身份。
# 函数用途: 确认当前磁盘仍是提交前版本或已经安装的同一版本，防止部分恢复先覆盖健康记录。
def _check_installation(root: Path, redo: dict[str, object]) -> None:
    for entry in redo["entries"]:
        record = entry["record"]
        current = read_process_record(root, record["session_id"])
        current_digest = _record_digest(current) if current is not None else None
        if current_digest not in {entry["before_digest"], _record_digest(record)}:
            raise ValueError("managed process redo conflicts with current record")
        if current_digest == entry["before_digest"]:
            if current is not None and current["schema"] not in MANAGED_PROCESS_SESSION_SCHEMAS:
                raise ValueError("managed process redo cannot migrate legacy records")
            expected_revision = current["revision"] + 1 if current is not None else 1
            if record["revision"] != expected_revision:
                raise ValueError("managed process redo revision is not the next version")
            if current is not None:
                merged = {**merge_process_record(current, record), "revision": expected_revision}
                if merged != record:
                    raise ValueError("managed process redo would erase monotonic facts")


# LLM: 发布日志后写入/权限/删除失败均属于已提交待恢复；只安装给定条目，不访问进程或通知。
# 函数用途: 在预检成功后逐条原子替换记录，全部完成后才移除暂存日志。
def _install_redo(root: Path, redo: dict[str, object]) -> ProcessSessionCommitReceipt:
    records = tuple(entry["record"] for entry in redo["entries"])
    try:
        _check_installation(root, redo)
        for record in records:
            path = root / f"{record['session_id']}.json"
            write_json_file_atomic_unlocked(path, record)
            path.chmod(0o600)
        (root / _REDO_FILE).unlink()
    except Exception as exc:
        receipt = ProcessSessionCommitReceipt(
            redo["transaction_id"], records, recovery_required=True
        )
        raise ProcessSessionCommitPendingError(receipt) from exc
    return ProcessSessionCommitReceipt(redo["transaction_id"], records)


# LLM: 只在同一目录锁内调用；恢复损坏日志直接报错，调用方不得继续返回健康子集或覆盖日志。
# 函数用途: 在任何读写和裁剪前完成上次固定批次的文件安装，没有日志时不修改记录。
def recover_process_commit(root: Path) -> ProcessSessionCommitReceipt | None:
    try:
        content = (root / _REDO_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    redo = _validate_redo(json.loads(content))
    return _install_redo(root, redo)


# LLM: transitions 由同锁内读取和 CAS 合并产生；redo 的原子替换为提交点，非掉电耐久承诺。
# 函数用途: 发布固定记录批次并安装，返回有效版本；提交前失败不会改变 session 文件。
def commit_process_records(
    root: Path,
    transitions: list[tuple[dict[str, object] | None, dict[str, object]]],
) -> ProcessSessionCommitReceipt:
    if (root / _REDO_FILE).exists():
        raise RuntimeError("pending managed process commit must be recovered first")
    entries = [
        {"before_digest": _record_digest(before) if before is not None else None, "record": after}
        for before, after in transitions
    ]
    redo = _validate_redo(
        {"schema": _REDO_SCHEMA, "transaction_id": uuid.uuid4().hex, "entries": entries}
    )
    _check_installation(root, redo)
    path = root / _REDO_FILE
    write_json_file_atomic_unlocked(path, redo)
    try:
        path.chmod(0o600)
    except OSError as exc:
        receipt = ProcessSessionCommitReceipt(
            redo["transaction_id"], tuple(entry["record"] for entry in entries), True
        )
        raise ProcessSessionCommitPendingError(receipt) from exc
    return _install_redo(root, redo)
