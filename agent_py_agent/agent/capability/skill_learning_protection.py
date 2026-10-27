# LLM: 保护名单只从原任务/子代理/未删除监控的结构refs生成；读取不完整禁止归档，不从目标或正文猜引用。
# 模块用途: 为自学维护收集包括暂停、年度、远期任务在内的准确方法引用。
from __future__ import annotations

from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in


# LLM: 原threads活动索引定位在途任务；历史completed不消耗活跃扫描预算；空refs是完整空集，坏账仍保守拒绝归档。
# 函数用途: 返回本owner必须保留的方法编号与完整性，不访问其它owner或模型。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    protected = set()
    complete = True


    try:
        rows, complete = _active_tasks(agent.conversation_store, scan_limit)
        for row in rows:
            _include_refs(protected, row.to_dict())
        jobs,errors = agent.scheduler_repository.list_jobs(include_deleted=False)
        complete = complete and not errors
        for job in jobs:
            _include_refs(protected,job)
            _include_refs(protected,job.get('payload') or {})
        from .skill_learning_proposals import list_learning_proposals
        from .skill_learning_store import SkillLearningStore
        for proposal in list_learning_proposals(SkillLearningStore.for_home(agent.home_paths),include_overflow=True):
            if proposal['status']=='pending' and proposal.get('expected_sha256'):
                protected.add('owner:'+proposal['decision']['name'])
        report = agent.subagents.list_runs_report()
        complete = complete and not report.load_errors
        for child in report.runs:
            if not task_status_in(child.status,SUBAGENT_ENDED_STATUSES):
                _include_refs(protected,child.attributes or {})
    except (AttributeError,OSError,ValueError,TypeError):
        complete = False
    return {'protected_ids':tuple(sorted(protected)),'protection_complete':complete}


# LLM: threads及active_report均为canonical只读索引；有界处理活动任务，溢出保留已读引用但完整性为False。
# 函数用途: 跳过历史终态，仅从活动索引收集仍需保护的任务。
def _active_tasks(conversations, limit):
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
