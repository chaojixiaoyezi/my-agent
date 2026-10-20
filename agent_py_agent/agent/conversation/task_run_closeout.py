# LLM: 输入取消仅消费原冻结attempt定位真实执行，审批task编号不当DB run；最终原SQL/树/UNKNOWN与持久TaskLink边界不变。
# LLM: 会话层是执行TaskRun收口的唯一领域判据。普通完成仍需根终态、TaskLink终态或确实缺失、原树CAS；输入静默退休例外复读准确行/等待结束事实，并在新SQL最终事务拒全树活动/UNKNOWN。
# 模块用途: 结束有证据的执行账，不把输入取消或本次执行结束变成长久TaskLink终态；callback和原完成边共同防race。
from __future__ import annotations

from typing import Any

from ..runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from .task_state import conversation_task_link_is_terminal


# LLM: A TaskRun spans every foreground/background slice and descendant of one conversation task. The durable link—not a
# turn-local flag or model prose—authorizes closeout, while RuntimeDB proves the exact agent tree terminal. run_id locates the
# run whose edge fired; with an empty run_id the root main run of task_id is used (the /recover child branch). A TaskRun whose
# task id has no link file at all is governed only by its own agent tree (operator agent-runtime, reason no_conversation_task).
# Only a real absence counts: missing store, empty task id, an unreadable link (DataCorruptionError) or a non-terminal link keep
# it open. Unknown attempts still block via the repository's tree rule. Side effect: may write task_run.closed.
# 函数用途: 在代理执行收口或显式恢复之后核对会话任务状态，并幂等关闭整棵任务执行总账；没有会话任务的请求在代理树结束后同样关闭。
def settle_terminal_task_run(repo: Any, store: Any, *, run_id: str = "", task_id: str = "") -> None:
    if repo is None or store is None:
        return
    run_id = str(run_id or "").strip()
    task_id = str(task_id or "").strip()
    try:
        row = repo.agent_run_for_run_id(run_id) if run_id else None
        if row is None and task_id:
            row = repo.main_agent_run_for_task(task_id)
        if row is None or str(row["status"] or "") not in AGENT_RUN_TERMINAL_STATUSES:
            return
        task_run_id = str(row["task_run_id"] or "").strip()
        task_run = repo.get_task_run(task_run_id)
        if task_run is None:
            return
        canonical_task_id = str(task_run["task_id"] or "").strip()
        if not canonical_task_id:
            return
        closeout = _task_run_closeout_reason(store.tasks.load(canonical_task_id))
        if closeout is None:
            return
        operator, reason = closeout
        repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=task_run_id,
            task_id=canonical_task_id,
            operator=operator,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 审计投影失败不反噬已完成的用户任务
        return


# LLM: Pure decision for settle_terminal_task_run. `link` is the result of
# ConversationStore.tasks.load: None means the link file is truly absent (D3: the request never
# promoted a conversation task, so its own agent tree governs closeout); a loaded link must be
# terminal per conversation_task_link_is_terminal. Returns (operator, reason) for the tree CAS or
# None to keep the TaskRun open. Callers must never pass None for an unreadable link — load raises
# DataCorruptionError there and the caller's except keeps the TaskRun open.
# 函数用途: 根据会话任务关联文件决定能否关闭任务执行总账，并给出关闭人和原因；返回 None 表示继续保持打开。
def _task_run_closeout_reason(link: object) -> tuple[str, str] | None:
    if link is None:
        # 没有会话任务关联文件：这条执行总账只归本次请求的代理树管，树结束即可关（D3）。
        return "agent-runtime", "no_conversation_task"
    link_status = str(getattr(link, "status", "") or "").strip().lower()
    if not conversation_task_link_is_terminal(link_status):
        return None
    return "conversation-runtime", f"conversation_task_{link_status}"


# LLM: callback与等待片完成边共用；archive ID只是lookup，quota→task锁内复读canonical原输入/refs，最终SQL再核current和全树UNKNOWN。
# 函数用途: 精确输入静默退休后结束已完成的等待执行账，不发Wake、不结束TaskLink。
def settle_retired_input_run(inputs, repo, input_id, *, run_id='', attempt_id=''):
    if repo is None or inputs.runtime_db is not repo:
        return False

    # LLM: 逐次消费原owner短门，quota先于task/输入/SQL；不缓存授权或无锁收口。
    # 函数用途: 为同一次原来源完成精确取消核验。
    def settle():
        with inputs.quota.admission(blocking=inputs.blocking):
            return _settle_retired_input_locked(inputs,repo,input_id,run_id,attempt_id)
    return inputs.perform(settle)


# LLM: 调用方已持quota；先以输入冻结attempt核真实run，再于原TaskStore锁内复读准确输入归属/有界refs并持锁到最终SQL，不允许新输入在判空后挤入。
# 函数用途: 区分原审批任务编号与执行编号，核验输入及剩余引用后调用唯一执行账SQL收口。
def _settle_retired_input_locked(inputs,repo,input_id,run_id,attempt_id):
    from ..common.json_io import locked_json_path

    try:
        row = inputs.read(input_id)
    except ValueError:
        return False
    scope = row['scope']
    if row['status'] != 'cancelled' or row.get('resolution_delivery') != 'retire':
        return False
    selected_run = _retired_input_runtime_run(inputs,repo,scope,run_id,attempt_id)
    selected_attempt = scope.get('execution_attempt_id')
    if selected_run is None:
        return False
    task_id = scope['root_task_id']
    with locked_json_path(inputs.store.tasks.storage.task_path(task_id),blocking=inputs.blocking):
        link,error = inputs.store.tasks.load_report(task_id)
        if error or link is None or link.status != 'active':
            return False
        if not _retired_input_thread_matches(inputs,scope,link):
            return False
        if not _retired_input_refs_settled(inputs,link):
            return False
        return repo.settle_retired_input_execution(task_id=task_id,run_id=selected_run,attempt_id=selected_attempt)


# LLM: 仅从输入已冻结非空attempt反向定位原run，DB current只核相等不补空；main审批run语义仍是task，child仍核原canonical run/attempt，最终SQL原CAS不变。
# 函数用途: 区分审批任务编号与实际执行编号，防同任务不同运行名使准确取消永久悬挂。
def _retired_input_runtime_run(inputs, repo, scope, callback_run, callback_attempt):
    from pathlib import Path

    from ..user_space.owner_resolver import _owner_home_result

    selected = scope.get('execution_attempt_id')
    if not isinstance(selected,str) or not selected or (callback_attempt and callback_attempt != selected):
        return None
    attempt = repo.get_attempt(selected)
    run = repo.get_agent_run(attempt['agent_run_id']) if attempt is not None else None
    task_run = repo.get_task_run(run['task_run_id']) if run is not None else None
    task = repo.get_task(scope['root_task_id'])
    thread,error = inputs.store.threads.load_report(scope['thread_id'])
    owner_id = _owner_home_result(Path('/'),inputs.owner,Path('/')).owner_id
    if (run is None or task_run is None or task is None or error or thread is None
            or run['current_attempt_id'] != selected
            or attempt['attempt_generation'] != run['current_attempt_generation']
            or task_run['task_id'] != scope['root_task_id'] or task['owner_id'] != owner_id
            or thread.owner_id != owner_id
            or (callback_run and callback_run != run['run_id'])):
        return None
    if scope['agent_kind'] == 'main':
        if (scope['run_id'] != scope['root_task_id'] or task['thread_id'] != scope['thread_id']
                or run['role'] != 'main' or run['parent_agent_run_id']):
            return None
    elif scope['agent_kind'] == 'subagent' and inputs.load_child is not None:
        if run['run_id'] != scope['run_id']:
            return None
        child = inputs.load_child(scope['run_id'])
        if (child.id != run['run_id']
                or child.root_id != scope['root_task_id'] or child.agent_thread_id != scope['thread_id']
                or child.runner_active_attempt_id != selected):
            return None
    else:
        return None
    return run['run_id']


# LLM: 主片绑定原TaskLink线程，子片只读原canonical child谱系；错误来源不能借根任务取消父/兄弟。
# 函数用途: 核对输入所属的准确执行线程。
def _retired_input_thread_matches(inputs,scope,link):
    if scope['agent_kind'] == 'main':
        return link.thread_id == scope['thread_id']
    if scope['agent_kind'] != 'subagent' or inputs.load_child is None:
        return False
    child = inputs.load_child(scope['run_id'])
    return child.root_id == scope['root_task_id'] and child.agent_thread_id == scope['thread_id']


# LLM: refs只作有界查找，所有行仍核schema/id/owner/root和已知状态；未知或pending都阻断，不把读错变空。
# 函数用途: 判断本任务是否确实没有其它待填必要输入。
def _retired_input_refs_settled(inputs,link):
    for ref in link.input_dependency_refs:
        try:
            candidate = inputs.read(ref,task_id=link.task_id)
        except ValueError:
            return False
        if candidate['status'] == 'pending':
            return False
    return True
