# LLM: Hierarchy role identity uses structured role/template ids, not natural goal text.
# 模块用途: 当真实模型把 role 写成 child/general 时，只从 role/agent_name 的模板 id 兜底恢复角色。

from __future__ import annotations

from typing import Any

from ..role_contracts import normalize_subagent_role
from ..role_templates import role_template_id_for_role

_PLACEHOLDER_ROLES = {"", "general", "child"}


# LLM: role_from_child_spec_identity keeps scheduler resilient without hardcoded prose keywords.
# 函数用途: 优先信任 spec.role；placeholder 只尝试从 agent_name 里的模板 id 恢复，失败则回退 worker。
def role_from_child_spec_identity(spec: Any) -> str:
    role = normalize_subagent_role(str(getattr(spec, "role", "") or "").strip())
    if role not in _PLACEHOLDER_ROLES:
        return role
    template_role = role_template_id_for_role(str(getattr(spec, "agent_name", "") or ""), fallback="")
    return template_role or "worker"
