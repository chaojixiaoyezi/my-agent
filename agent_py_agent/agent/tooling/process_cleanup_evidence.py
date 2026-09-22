# LLM: 清理引用是原资源账的固定投影，不拥有另一份资源状态；消费必须先持久化调用方原操作结果，再在 Store 锁内核验。
# 模块用途: 保留完整退出证明和不可变身份摘要，让管理收尾只删除已确认的准确记录。

from __future__ import annotations

import hashlib
import json
import re

from .process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
    _validate_cleanup_evidence,
    validate_process_record,
    validate_session_id,
)


# LLM: 摘要覆盖原归属、出生身份、固定文件和已停止的启动阶段；迟到终态/通知更新不能改变这些权威字段。
# 函数用途: 比较同一句柄是否仍指向原来已清理的进程实例，不公开命令或私有路径。
def _identity_digest(record: dict) -> str:
    keys = ("schema", "session_id", "access_scope", "execution_scope", "activation_scope", "completion_target",
            "launcher_pid", "launcher_birth_token", "reserved_at", "command", "cwd", "output_file", "host_state_file",
            "pid", "pid_birth_token", "child_pid", "child_pid_birth_token", "started_at",
            "handoff_confirmed", "child_launch_started", "retain_until_consumed")
    return hashlib.sha256(json.dumps({key: record.get(key) for key in keys}, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


# LLM: 只有原账中 stop 与完整 cleanup 同时确认才能生成引用；业务 exited 或裸布尔值不能代替原生退出证明。
# 函数用途: 冻结可以随管理操作持久保存的最小清理证据，避免把路径、命令带到管理响应。
def process_cleanup_reference(record: dict) -> dict:
    record = validate_process_record(record)
    cleanup = (record.get("termination") or {}).get("cleanup")
    if (record["schema"] not in MANAGED_PROCESS_SESSION_SCHEMAS or not record["stop_requested"]
            or record["status"] not in PROCESS_TERMINAL_STATUSES
            or not isinstance(cleanup, dict) or cleanup.get("confirmed") is not True):
        raise ValueError("原资源退出尚未完整确认")
    return {"session_id": record["session_id"], "record_schema": record["schema"], "revision": record["revision"],
            "identity_sha256": _identity_digest(record), "status": record["status"],
            "cleanup": json.loads(json.dumps(cleanup))}


# LLM: 已持久化引用仍须严格解码；不存在只可在引用有效时幂等消费，不接受任意 session ID 或 cleaned 标志。
# 函数用途: 拒绝缺少出生身份摘要、完整退出回执或原版本的删除请求。
def validate_cleanup_reference(reference: dict) -> None:
    if not isinstance(reference, dict) or set(reference) != {
        "session_id", "record_schema", "revision", "identity_sha256", "status", "cleanup",
    }:
        raise ValueError("资源清理引用字段无效")
    validate_session_id(reference["session_id"])
    if (reference["record_schema"] not in MANAGED_PROCESS_SESSION_SCHEMAS
            or type(reference["revision"]) is not int or reference["revision"] <= 0
            or not isinstance(reference["identity_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", reference["identity_sha256"]) is None
            or reference["status"] not in PROCESS_TERMINAL_STATUSES):
        raise ValueError("资源清理引用身份无效")
    _validate_cleanup_evidence({"cleanup": reference["cleanup"]})
    if reference["cleanup"]["confirmed"] is not True:
        raise ValueError("资源清理引用尚未确认")


# LLM: 同一记录版本可以因迟到退出/通知前进，但固定身份与完整清理不得变；同 ID 的新记录绝不能被旧请求删除。
# 函数用途: 在消费前核验磁盘记录仍对应已落账的准确退出证明。
def confirm_cleanup_reference(reference: dict, record: dict) -> None:
    validate_cleanup_reference(reference)
    current = process_cleanup_reference(record)
    if (any(current[key] != reference[key] for key in ("session_id", "record_schema", "identity_sha256", "status", "cleanup"))
            or current["revision"] < reference["revision"]):
        raise ValueError("资源清理引用与原记录不符")
