# LLM: Root task policy separates main-agent-owned children from self-authorized coordinator seeds.
# 模块用途: 判断一个 subagent run 是否真的是自主管理的 root，而不是主代理直接创建的一层 worker。

from __future__ import annotations

_ROOT_ROLE_MARKERS = ("coordinator", "lead", "root")


# LLM: is_self_authorized_root_task keeps capability lanes open for top-level workers.
# 函数用途: 只有无父级且显式 root/coordinator/lead 的任务才按无上级 root 处理；普通一层小傻妞仍可向主代理申请能力。
def is_self_authorized_root_task(task: object) -> bool:
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id:
        return False
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict) and bool(attrs.get("self_authorized_root")):
        return True
    role = str(getattr(task, "role", "") or "").strip().lower()
    return any(marker in role for marker in _ROOT_ROLE_MARKERS)
