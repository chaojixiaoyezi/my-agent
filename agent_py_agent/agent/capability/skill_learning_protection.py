# LLM: 保护名单只从原任务/子代理/未删除监控的结构refs生成；读取不完整禁止归档，不从目标或正文猜引用。
# 模块用途: 为自学维护收集包括暂停、年度、远期任务在内的准确方法引用。
from __future__ import annotations

from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in


# LLM: owner原stores是唯一来源，候选含归档pending更新；扫描上限只导致incomplete，不代表多余任务没有保护。
# 函数用途: 返回本owner必须保留的方法编号与完整性，不访问其它owner或模型。
def learning_protection_for_agent(agent, *, scan_limit=1000):
    protected = set()
    complete = True


    try:
        task_store = agent.conversation_store.tasks
        paths = sorted(task_store.storage.tasks_dir.glob('*.json'))
        if len(paths)>scan_limit:
            complete = False
        for path in paths[:scan_limit]:
            row,error = task_store.load_report(path.stem)
            if error or row is None:
                complete = False
            elif row.status not in THREAD_TASK_LINK_INACTIVE_STATUSES:
                _include_refs(protected,row.to_dict())
                complete = complete and bool(row.skill_snapshot_refs)
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


# LLM: 仅取声明式skill_snapshot_refs/allowed_skills，不递归解析任意metadata文案。
# 函数用途: 合并一个canonical任务或监控的明确引用。
def _include_refs(protected,data):
    refs = [*data.get('skill_snapshot_refs',[]),*data.get('skill_refs',[]),*data.get('scheduler_skill_refs',[])]
    if isinstance(refs,(list,tuple)):
        protected.update(ref['stable_id'] for ref in refs if isinstance(ref,dict) and isinstance(ref.get('stable_id'),str))
    allowed = data.get('allowed_skills',[])
    if isinstance(allowed,(list,tuple)):
        protected.update(ref for ref in allowed if isinstance(ref,str))
