# LLM: 保护名单只从原任务/子代理/未删除监控的结构refs生成；读取不完整禁止归档，不从目标或正文猜引用。
# 模块用途: 为自学维护收集包括暂停、年度、远期任务在内的准确方法引用。
from __future__ import annotations

from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in


# LLM: 四个canonical来源独立容错；任一不可读禁止归档，但其余来源仍补齐保护引用；不跨owner或读历史正文。
# 函数用途: 汇集在途任务、监控、待审更新及子代理的保护名单，不让一个坏来源挡住其它来源。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    protected = set()
    complete = True


    try:
        rows, tasks_complete = _active_tasks(agent.conversation_store, scan_limit)
        complete = complete and tasks_complete
        for row in rows:
            _include_refs(protected, row.to_dict())
    except Exception:
        complete = False
    try:
        jobs,errors = agent.scheduler_repository.list_jobs(include_deleted=False)
        complete = complete and not errors
        for job in jobs:
            _include_refs(protected,job)
            _include_refs(protected,job.get('payload') or {})
    except Exception:
        complete = False
    try:
        from .skill_learning_proposals import list_learning_proposals
        from .skill_learning_store import SkillLearningStore
        for proposal in list_learning_proposals(SkillLearningStore.for_home(agent.home_paths),include_overflow=True):
            if proposal['status']=='pending' and proposal.get('expected_sha256'):
                protected.add('owner:'+proposal['decision']['name'])
    except Exception:
        complete = False
    try:
        report = agent.subagents.list_runs_report()
        complete = complete and not report.load_errors
        for child in report.runs:
            if not task_status_in(child.status,SUBAGENT_ENDED_STATUSES):
                _include_refs(protected,child.attributes or {})
    except Exception:
        complete = False
    return {'protected_ids':tuple(sorted(protected)),'protection_complete':complete}


# LLM: 优先threads+active_report；仅tasks的旧宿主用全局active_report，缺API且物理任务库为空才认完整空集。
# 函数用途: 从活动索引读取有界保护任务；缺来源或有未解析旧文件时保守拒绝归档。
def _active_tasks(conversations, limit):
    if getattr(conversations, 'threads', None) is None:
        tasks = getattr(conversations, 'tasks', None)
        if callable(getattr(tasks, 'active_report', None)):
            rows, errors = tasks.active_report()
            active = [row for row in rows if row.status not in THREAD_TASK_LINK_INACTIVE_STATUSES]
            return active[:limit] if limit > 0 else active, not errors and (limit <= 0 or len(active) <= limit)
        directory = getattr(getattr(tasks, 'storage', None), 'tasks_dir', None)
        if directory is not None and not directory.is_symlink() and (not directory.exists() or not any(directory.iterdir())):
            return [], True
        return [], False
    threads, errors = conversations.threads.list_report(limit=0)
    complete, rows = not errors, []
    for thread in threads:
        active, errors = conversations.tasks.active_report(thread.thread_id)
        complete = complete and not errors
        rows.extend(row for row in active if row.status not in THREAD_TASK_LINK_INACTIVE_STATUSES)
        if limit > 0 and len(rows) > limit:
            return rows[:limit], False
    return rows, complete


# LLM: 仅取声明式refs/allowed_skills；空数组完整、坏形状抛错使外层保护不完整，禁止将坏账当空集。
# 函数用途: 合并一个canonical任务或监控的明确引用。
def _include_refs(protected,data):
    for key in ('skill_snapshot_refs','skill_refs','scheduler_skill_refs'):
        refs = data.get(key, [])
        if not isinstance(refs, (list, tuple)) or any(not isinstance(ref, dict) or not isinstance(ref.get('stable_id'), str) for ref in refs):
            raise ValueError('方法引用索引损坏')
        protected.update(ref['stable_id'] for ref in refs)
    allowed = data.get('allowed_skills',[])
    if not isinstance(allowed, (list, tuple)) or any(not isinstance(ref, str) for ref in allowed):
        raise ValueError('方法授权引用损坏')
    protected.update(allowed)
