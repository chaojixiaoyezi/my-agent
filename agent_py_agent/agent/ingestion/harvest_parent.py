# LLM: 原父Audit全列表/坏账/重复观测在T外，最终只核原single link/线程索引/epoch；不从新current给旧plan签权，Count0原parent函数不改。
# 模块用途: 保存一拍来源收割的原父观察并在短提交中精确复核，避免长任务历史扫描占请求T。
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..conversation.models import ThreadTaskLink
from ..gateway_parts.multiprocess_background_request import BackgroundSourceChanged
from ..gateway_parts.request_errors import RequestExecutionOutcomeUncertainError

# 原metadata不可核只让本次来源pending，不建立写许可或把它当已结束。
PARENT_UNAVAILABLE = object()


# LLM: 原link完整字节及原thread任务索引都是限制事实，不是新授权账；只能配相同Audit/epoch源使用。
# 类用途: 捕获T外已核的唯一父关联与原线程归属。
@dataclass(frozen=True)
class HarvestParentSnapshot:
    audit_id: str
    epoch: int
    thread_id: str
    task_ids: tuple
    link_sha256: str


# LLM: 路径来自原ConversationStorage且必须仍在同owner canonical home内；nofollow/完整JSON只读，不创建状态。
# 函数用途: 读取一个原父任务关联的确切字节和原typed对象。
def _link(agent, state):
    path = agent.conversation_store.storage.task_path(state.audit_root_task_id)
    try:
        parts = path.relative_to(Path(state.owner_home)).parts
        raw = read_bytes_beneath(state.owner_home, parts, max_bytes=1_048_576, require_dir_fd=True)
        if raw is None:
            raise BackgroundSourceChanged()
        payload = load_strict_json(raw)
        link = ThreadTaskLink.from_dict(payload)
        if payload.get('status') != link.status or payload.get('work_kind') != link.work_kind:
            raise BackgroundSourceChanged()
        return raw, link
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BackgroundSourceChanged() from error


# LLM: 先按原全列表拒绝坏关联/重复，再捕获同一原link/thread/epoch；此观察必须在原T外，不恢复缺字段或借新代次。
# 函数用途: 为本次watch准备原父活动事实，MP只接受原canonical active/audit，不沿Count0别名，普通watch不读会话账。
def observe_harvest_parent(agent, state):
    if not state.audit_guarantee:
        return None
    from .source_worker import audit_parent_reconcile_state

    active, status = audit_parent_reconcile_state(agent, state.audit_root_task_id)
    if not active:
        return PARENT_UNAVAILABLE if status == 'unavailable' else None
    if agent.conversation_store is None:
        return PARENT_UNAVAILABLE
    raw, link = _link(agent, state)
    try:
        thread = agent.conversation_store.tasks.thread_for(state.audit_root_task_id)
    except RequestExecutionOutcomeUncertainError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise BackgroundSourceChanged() from error
    if (thread is None or link.task_id != state.audit_root_task_id or link.thread_id != thread.thread_id
            or link.status != 'active' or link.work_kind != 'audit'
            or type(link.run_epoch) is not int or link.run_epoch != state.audit_run_epoch
            or thread.task_ids.count(link.task_id) != 1):
        return None
    snapshot = HarvestParentSnapshot(link.task_id, link.run_epoch, thread.thread_id,
                                     tuple(thread.task_ids), hashlib.sha256(raw).hexdigest())
    # 单link与全列表之间有变化则本次观察不能签给较新原事实。
    check_harvest_parent(agent, state, snapshot)
    return snapshot


# LLM: 只读同一原single link/thread，不重走list_report；任何字节/索引/epoch变化拒旧plan，而非跟随新current刷新snapshot。
# 函数用途: 在原T内最终复核一拍已捕获的父关联。
def check_harvest_parent(agent, state, snapshot):
    if not state.audit_guarantee:
        return True
    if snapshot is PARENT_UNAVAILABLE:
        return None
    if snapshot is None:
        return False
    if (type(snapshot) is not HarvestParentSnapshot or snapshot.audit_id != state.audit_root_task_id
            or snapshot.epoch != state.audit_run_epoch):
        raise BackgroundSourceChanged()
    raw, link = _link(agent, state)
    try:
        thread = agent.conversation_store.tasks.thread_for(snapshot.audit_id)
    except RequestExecutionOutcomeUncertainError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise BackgroundSourceChanged() from error
    if (hashlib.sha256(raw).hexdigest() != snapshot.link_sha256 or thread is None
            or link.task_id != snapshot.audit_id or link.thread_id != snapshot.thread_id
            or thread.thread_id != snapshot.thread_id or tuple(thread.task_ids) != snapshot.task_ids
            or thread.task_ids.count(snapshot.audit_id) != 1 or link.run_epoch != snapshot.epoch):
        raise BackgroundSourceChanged()
    return True
