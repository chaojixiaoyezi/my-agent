# LLM: 显式Store叶子与Agent包装共享原atomic updater；C仅传非阻塞参数，不改refs上限、CAS或状态归属。
# 模块用途: 在唯一TaskStore投影中维护原输入引用。
# LLM: 任务只存输入 ID 查找投影，状态和期限仍在原输入行；直接复用 TaskStore 原路径和原文件锁，不能另建任务生命周期。
# 模块用途: 维护准确任务的输入引用，减少所有自动续跑检查的全历史扫描。
from __future__ import annotations

from dataclasses import replace

from ..gateway_parts.io import update_json_file_atomic
from .agent_tool_approval import AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT
from .models import ThreadTaskLink


# LLM: refs 只是查找投影；容量检查与 persist 回调共用原 TaskStore 锁；replaces_input_id仅在persist成功后原子退休旧槽，不能更改任务状态。
# 函数用途: 原子维护准确任务的输入引用，最多沿原私交互上限保留32条，终态移除后放槽。
def update_task_input_refs(agent, task_id: str, input_id: str, *, remove: bool = False, persist=None, replaces_input_id: str = ""):
    return update_task_input_refs_from_store(agent.conversation_store, task_id, input_id,
        remove=remove, persist=persist, replaces_input_id=replaces_input_id)


# LLM: Store由已认证owner原路径给出；只给原atomic updater透传blocking，refs仍仅查找投影，不授执行。
# 函数用途: 在同一原TaskStore锁和CAS内维护输入引用，供Count0与C共用。
def update_task_input_refs_from_store(store, task_id: str, input_id: str, *, remove=False, persist=None, replaces_input_id="", blocking=True):
    path = store.tasks.storage.task_path(task_id)
    # LLM: 同一任务的不同输入并发提交不得互相覆盖；任务身份从原文件复核。
    # 函数用途: 在任务记录锁内合并或移除一个引用。
    def update(data):
        link = ThreadTaskLink.from_dict(data)
        if link.task_id != task_id:
            raise ValueError('输入任务绑定冲突')
        if persist is not None and link.status != 'active':
            raise ValueError('输入任务已失效')
        refs = [item for item in link.input_dependency_refs if item != input_id and item != replaces_input_id]
        available = input_id in link.input_dependency_refs or len(refs) < AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT
        if persist is not None:
            saved = persist(available)
            retain = saved['status'] == 'pending'
        else:
            retain = not remove
            if retain and not available:
                raise ValueError('TASK_INPUT_CAPACITY_EXCEEDED')
        if retain:
            refs.append(input_id)
        return replace(link, input_dependency_refs=tuple(refs)).to_dict()
    return ThreadTaskLink.from_dict(update_json_file_atomic(path, update, require_existing=True, blocking=blocking))
