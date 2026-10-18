# LLM: 原canonical child文件是唯一详细来源；locator只供原manager查找，meta发现不构造Agent/任务/attempt或授写许可。
# 模块用途: 校验后台孤儿来源的原owner/run/root/parent和canonical路径，供同一请求桥运输。
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ..common.opaque_id import validate_opaque_id


# LLM: 只接受原work/agents/<run>/canonical_state.json；路径必须与原物化字段完全一致，不信任projection自报ref。
# 函数用途: 拒绝用另一个文件或展示镜像作为孤儿的原工作身份。
def validate_orphan_payload(payload, home, relative, owner_id):
    parts = Path(relative).parts
    if len(parts) < 5 or parts[-4:-2] != ("work", "agents") or parts[-1] != "canonical_state.json":
        raise ValueError("background_orphan_path_invalid")
    if type(payload) is not dict or payload.get("owner") != owner_id:
        raise ValueError("background_orphan_owner_invalid")
    for field in ("id", "root_id"):
        validate_opaque_id(payload.get(field), kind="run_id")
    parent = payload.get("parent_id")
    if type(parent) is not str:
        raise ValueError("background_orphan_parent_invalid")
    if parent:
        validate_opaque_id(parent, kind="run_id")
    run_home = Path(home) / Path(*parts[:-1])
    task_home = Path(home) / Path(*parts[:-4])
    if (payload["id"] != parts[-2] or payload.get("agent_run_workspace_dir") != str(run_home)
            or payload.get("task_workspace_dir") != str(task_home)):
        raise ValueError("background_orphan_identity_invalid")
    from .models import TaskStatus

    if payload.get("status") not in {item.value for item in TaskStatus}:
        raise ValueError("background_orphan_status_invalid")


# LLM: 只投影原canonical谱系，不计算平台fingerprint、不从goal/summary派工；状态和attempt仍全字节source冻结。
# 函数用途: 返回原owner/root/parent/run物化身份，平台按唯一原算法计算摘要。
def orphan_source_identity(payload):
    return {key: payload.get(key) for key in
        ("id", "root_id", "parent_id", "owner", "task_workspace_dir", "agent_run_workspace_dir")}


# LLM: 此预筛只决定metadata运输，真实死亡/recovery/candidate仍原supervisor与RuntimeDB裁决；关闭run的待收口事实继续可达。
# 函数用途: 找到原孤儿监督还需要检查的canonical来源。
def orphan_has_work(payload):
    from .models import SUBAGENT_RECOVERY_CLOSED_STATUSES
    from .services.runtime_closeout import RUNTIME_CLOSEOUT_ATTR

    attributes = payload.get("attributes")
    pending = type(attributes) is dict and type(attributes.get(RUNTIME_CLOSEOUT_ATTR)) is dict
    if not pending:
        from .runner_session_liveness import has_fresh_runner_session

        if has_fresh_runner_session(SimpleNamespace(**payload)):
            return False
    return payload["status"] not in SUBAGENT_RECOVERY_CLOSED_STATUSES or pending
