# LLM: 权威治理只读宿主data/managed_user_governance，owner镜像不授权；配额仅记调用者。
# 模块用途: 黑盒执行前后复核调用者治理和空间配额。
from __future__ import annotations

import json

from ..common.nofollow_fs import unlink_file_beneath, write_bytes_atomic_beneath
from ..user_space.managed_user_governance import _GovernanceControl
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from ..user_space.owner_resolver import resolve_owner_home
from .access_policy import (
    AuditGroupError,
    current_tool_policy,
    owner_identity,
    require_audit_group_member,
)


# LLM: 实时成员和严格工具策略校验后读取治理authority，损坏失败关闭；不调用有副作用的overview资产Store。
# 函数用途: 获取调用者规范home和当前治理修订，禁用工具策略一并执行。
def caller_policy(root, caller, kind):
    member = require_audit_group_member(root, caller)
    identity = owner_identity(member["owner_id"])
    home = resolve_owner_home(root, identity)
    policy = _GovernanceControl(root, identity.owner_id).policy() if identity.owner_kind == "user" else {}
    tool_policy, controls = current_tool_policy(root, home)
    if (controls.get("disabled", False)
            or "audit_group_invoke_asset" in tool_policy.get("disabled_tools", [])
            or {"script": "scripts", "workflow": "workflows", "skill": "skill_search", "monitor": "schedule"}[kind]
                in tool_policy.get("disabled_tools", [])):
        raise AuditGroupError("GOVERNANCE_EXECUTION_DENIED")
    return home, policy


# LLM: 本次预算占位和回执共用原OwnerQuotaEnforcer；所有持久写均在调用者原配额锁下。
# 函数用途: 为匿名执行缓冲预占真实调用者空间。
def reserve_buffer(home, parts, size):
    quota = owner_quota_enforcer_from_policy(home.home_dir)
    target = home.home_dir.joinpath(*parts, "buffer.reserve")
    with quota.reserve((OwnerQuotaChange(target, size),)):
        write_bytes_atomic_beneath(home.home_dir, (*parts, "buffer.reserve"), b"\0" * size)


# LLM: 只公开已经脱敏的JSON；写前复核实时配额，源owner不参与预算或回执写入。
# 函数用途: 原子保存调用者自己的安全执行结果。
def save_public_result(home, parts, result):
    data = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    if len(data) > 1024 * 1024:
        raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
    quota = owner_quota_enforcer_from_policy(home.home_dir)
    with quota.reserve((OwnerQuotaChange(home.home_dir.joinpath(*parts, "result.json"), len(data)),)):
        write_bytes_atomic_beneath(home.home_dir, (*parts, "result.json"), data)


# LLM: 仅删本次占位叶，不递归清理工作区或来源目录；异常不隐式新建存储。
# 函数用途: 释放黑盒执行缓冲占用的空间。
def release_buffer(home, parts):
    unlink_file_beneath(home.home_dir, (*parts, "buffer.reserve"))
