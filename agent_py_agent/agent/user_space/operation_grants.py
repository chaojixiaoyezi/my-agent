# LLM: owner 级"长期允许某类操作"的唯一权威：记录在既有 owner tool_policy.json 的 operation_grants 字段里，键是
#   `工具名:参数=值` 这种结构化操作键（由工具 runtime policy 的 owner_grant_parameters 声明，见 contracts.tool_approval.operation_grant_key）。
#   只有用户在审批面板选择 approved_owner 才会写入；模型参数、自然语言和会话缓存都不能产生授权。读取失败一律按未授权。
#   改字段或键格式要同步 approval_mode.autonomous_tool_decision、agent_tool_approval、stream_approval 与 test_background_listen_scope.py。
# 模块用途: 让用户对"开放局域网"这类操作只确认一次，以后同类操作不再反复询问；授权只存在本用户的策略文件里。
from __future__ import annotations

import time
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .owner_policy_seed_payloads import default_tool_policy_payload

OPERATION_GRANTS_FIELD = "operation_grants"
OPERATION_GRANT_SCHEMA = "owner_operation_grant.v1"
_TOOL_POLICY_SCHEMA = "tool-policy.v1"


# LLM: 只认 tool-policy.v1 里 operation_grants 的精确键；文件缺失、坏 JSON、schema 不符或键不存在都返回 False，不抛错。
# 函数用途: 判断本用户是否已长期允许这一类操作。
def owner_operation_granted(home: object, grant_key: str) -> bool:
    key = str(grant_key or "").strip()
    path = _policy_path(home)
    if not key or path is None:
        return False
    report = read_json_object_report(path, context="owner.operation_grants.read")
    payload = report.payload if report.load_error is None else None
    if not isinstance(payload, dict) or payload.get("schema_version") != _TOOL_POLICY_SCHEMA:
        return False
    grants = payload.get(OPERATION_GRANTS_FIELD)
    return isinstance(grants, dict) and isinstance(grants.get(key), dict)


# LLM: 锁内读改写同一 tool_policy.json，保留其它字段（审批模式、禁用工具等）；文件不存在时从默认策略种子建立。
#   有副作用：写 owner 策略文件并 chmod 600。已存在的键原样保留（不刷新时间），返回当前授权记录。
# 函数用途: 用户在审批面板选"长期允许"后，把这类操作记进本用户策略。
def record_owner_operation_grant(home: object, grant_key: str, *, source: str) -> dict[str, object]:
    key = str(grant_key or "").strip()
    path = _policy_path(home)
    if not key or path is None:
        raise ValueError("owner operation grant requires a grant key and an owner policy path")
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        report = read_json_object_report(path, context="owner.operation_grants.write")
        if report.load_error is not None:
            raise OSError("owner tool policy is unreadable; grant not recorded")
        data = dict(report.payload or default_tool_policy_payload())
        if data.get("schema_version") != _TOOL_POLICY_SCHEMA:
            raise ValueError("owner tool policy schema mismatch; grant not recorded")
        grants = dict(data.get(OPERATION_GRANTS_FIELD) or {})
        if not isinstance(grants.get(key), dict):
            grants[key] = {"schema": OPERATION_GRANT_SCHEMA, "granted_at": time.time(), "source": str(source or "")}
        data[OPERATION_GRANTS_FIELD] = grants
        write_json_file_atomic_unlocked(path, data)
        path.chmod(0o600)
        return dict(grants[key])


# 函数用途: 从 owner home 事实取策略文件路径；替身或未知对象返回 None。
def _policy_path(home: object) -> Path | None:
    value = getattr(home, "owner_tool_policy_json", None)
    return Path(value) if value else None


__all__ = ["OPERATION_GRANTS_FIELD", "OPERATION_GRANT_SCHEMA", "owner_operation_granted", "record_owner_operation_grant"]
