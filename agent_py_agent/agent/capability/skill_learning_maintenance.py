# LLM: 维护状态只在原registry中，观察不改变发布/授权；质量只认宿主核验或显式人类结果，模型只能记录采用，热度不等于有效性。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 管理自学Skill试用、推荐、冷藏和可恢复归档；保护用户固定与原任务/监控引用。
from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass, replace

from ..common.nofollow_fs import read_text_beneath
from .skill_learning_publish import SkillLearningGateError, _owned_record
from .skill_learning_store import SkillLearningEvent


# LLM: 同一原registry修改时间决定自主维护节拍；仅到期预筛，不替代owner、来源完整性或原请求末门。
# 函数用途: 共用原一天维护间隔，Count0和C发现都按当前真实时间判断，不缓存到期结论。
def learning_maintenance_due(modified_at, mode, now):
    return mode == "autonomous" and now - modified_at >= 86400


# LLM: 由调用方宿主提供的来源事实，模型工具只构造model，human只来自已认证用户control；不从工具参数反序列化。
# 类用途: 汇总反馈身份与执行来源，避免散落身份参数或让模型自称人类。
@dataclass(frozen=True)
class LearningObservationSource:
    authority: str = 'model'
    job_id: str = ''
    receipt_id: str = ''
    actor_id: str = ''
    run_id: str = ''


# LLM: 反馈绑定当前准确hash和任务/监控，重试去重；定时job只算一份独立证据；环境失败不能降低质量。
# 函数用途: 登记采用或用户验证结果，不从任务最终回复猜成败，不写Skill正文。
def observe_learning(store, name, *, sha256, task_id, outcome, source=None):
    source = source or LearningObservationSource()
    authority, job_id, receipt_id, actor_id, run_id = source.authority, source.job_id, source.receipt_id, source.actor_id, source.run_id
    if any(not isinstance(value,str) or len(value)>300 or '\n' in value or '\r' in value for value in (task_id,job_id,receipt_id,actor_id,run_id)):
        raise ValueError('学习反馈来源编号无效')
    allowed = {'adopted','unknown'} if authority == 'model' else {'adopted','unknown','effective','ineffective','environment'} if authority in {'human','host'} else set()
    if outcome not in allowed or not task_id or (authority in {'human','host'} and not receipt_id) or (authority == 'human' and not actor_id):
        raise ValueError('学习反馈缺少准确来源或确认事实')
    with store.state_lock():
        registry = store.load_registry()
        current = _owned_record(store,registry,name)
        if current.sha256 != sha256:
            raise SkillLearningGateError('SKILL_LEARNING_REVISION_CONFLICT')
        data = dict(current.maintenance)
        observations = list(data.get('observations',[]))
        adoption = next((row for row in observations if row.get('task_id')==task_id and row.get('sha256')==sha256 and row.get('authority')=='model' and row.get('outcome')=='adopted'),None)
        if authority in {'human','host'} and outcome in {'effective','ineffective'} and adoption is None:
            raise ValueError('该任务没有此版本的结构化采用回执')
        if adoption:
            job_id = adoption.get('job_id') or job_id
        if authority == 'host':
            observations = [row for row in observations if not (row.get('subject') == (job_id or task_id) and row.get('sha256') == sha256 and row.get('authority') == 'host' and row.get('receipt_id') != receipt_id)]
        key = (task_id if authority=='model' else job_id or task_id, sha256, authority)
        if not any((row['task_id'] if authority=='model' else row['subject'],row['sha256'],row['authority']) == key for row in observations):
            observations.append({'subject':job_id or task_id,'task_id':task_id,'job_id':job_id,'sha256':sha256,
                'authority':authority,'outcome':outcome,'receipt_id':receipt_id,'actor_id':actor_id,'run_id':run_id or task_id,'root_task_id':task_id,'at':time.time()})
        # 观察有界，但不因重复读取刷有效证据；当前版本只汇总独立核验结果。
        data.update(observations=observations[-200:],last_used_at=time.time(),baseline_at=data.get('baseline_at') or time.time())
        positive, negative = _verified_counts(observations,sha256)
        data['quality'] = 'unreliable' if negative >= 2 and negative >= positive else 'established' if positive >= 2 and positive > negative else 'trial'
        if data.get('state') == 'cooled' and outcome == 'adopted' and data['quality'] != 'unreliable':
            data['state'] = 'trial'
        if data.get('state') not in {'disabled','archived'}:
            data['state'] = 'cooled' if data['quality']=='unreliable' else 'active' if data['quality']=='established' else data.get('state','trial')
        record = replace(current,maintenance=data)
        store.save_registry(replace(registry,skills={**registry.skills,name:record}))
    return {'skill_name':name,'sha256':sha256,'quality':data['quality'],'state':data['state']}


# LLM: 同一独立任务/job最多计一份结果，人类纠正优先宿主；模型声明和环境异常没有质量权重。
# 函数用途: 汇总当前方法版本可核验的独立质量样本，不让多次检查刷分。
def _verified_counts(observations,sha256):
    facts = {}
    for authority in ('host','human'):
        for row in observations:
            if row.get('sha256') == sha256 and row.get('authority') == authority and row.get('outcome') in {'effective','ineffective'}:
                facts[row['subject']] = row['outcome']
    return sum(value=='effective' for value in facts.values()),sum(value=='ineffective' for value in facts.values())


# LLM: 只在准确托管记录上设置固定/停用/恢复；人工删除名单无恢复入口，归档与用户删除分账。
# 函数用途: 执行用户显式维护动作，不确认候选、不修改来源授权。
def control_learning(store, name, *, operation):
    if operation not in {'pin','unpin','disable','enable','restore'}:
        raise ValueError('维护动作无效')
    with store.state_lock(), ExitStack() as rollback:
        registry = store.load_registry()
        current = registry.skills.get(name)
        if current is None or name in registry.blocked_names:
            raise SkillLearningGateError('SKILL_LEARNING_NOT_LEARNED')
        data = _controlled_state(store,current,operation,rollback)
        next_record = replace(current,maintenance=data)
        if operation in {'disable','enable','restore'}:
            path = store.skill_path(name)
            text = read_text_beneath(store.skills_root,path.relative_to(store.skills_root).parts)
            next_record = replace(next_record,revision=current.revision+1)
            store.save_version(name,next_record.revision,text)
        store.save_registry(replace(registry,skills={**registry.skills,name:next_record}))
        rollback.pop_all()
    return {'skill_name':name,'maintenance':data}


# LLM: 目录恢复登记失败由调用方ExitStack恢复原位置；这里只改变准确原托管路径，不接纳用户指定目标。
# 函数用途: 计算用户控制后的维护状态，需要时执行可回滚的目录恢复。
def _controlled_state(store,current,operation,rollback):
    data = dict(current.maintenance)
    if operation in {'pin','unpin'}:
        data['pinned'] = operation == 'pin'
    elif operation == 'disable':
        data['state'] = 'disabled'
    elif operation == 'enable':
        if data.get('state') == 'archived':
            raise ValueError('请显式restore归档条目')
        data['state'] = 'trial'
    else:
        if data.get('state') != 'archived':
            raise ValueError('条目未归档')
        source = store.directory/'archived'/current.name
        target = store.skill_path(current.name).parent
        if source.is_symlink() or not source.is_dir() or target.exists() or target.is_symlink():
            raise ValueError('归档恢复位置无效')
        import os
        target.parent.mkdir(parents=True,exist_ok=True)
        os.replace(source,target)
        rollback.callback(os.replace,target,source)
        data.update(state='trial',baseline_at=time.time(),last_used_at=time.time())
    return data


# LLM: 推荐只是默认展示投影，不影响搜索/get授权；固定项不被新导入挤掉，质量与热度只在相关性之外提供目录优先级。
# 函数用途: 从同一registry选择有界常用名单，满额不删除或阻止保存其它Skill。
def recommended_learning(registry, limit=50):
    rows = [row for row in registry.skills.values() if row.maintenance.get('state') not in {'disabled','archived','cooled'}]
    rows.sort(key=lambda row:(not row.maintenance.get('pinned',False),row.maintenance.get('quality')!='established',-float(row.maintenance.get('last_used_at',0)),row.name))
    return {row.name for row in rows[:limit] if limit > 0} if limit > 0 else {row.name for row in rows}


# LLM: 自动维护需显式策略，引用读取不完整时不归档；旧条目先建立baseline。自然语言和frontmatter不参与状态裁决。
# 函数用途: 按owner事件驱动执行一批冷藏/归档检查，不调用模型、不永久删除。
def maintain_learning(store, *, mode='observe', cool_seconds=0, archive_seconds=0, protected_ids=(), protection_complete=False, now=None, keep_versions=5):
    if mode not in {'observe','autonomous'}:
        raise ValueError('维护策略无效')
    now = time.time() if now is None else now
    changed = []
    removed_versions = 0
    with store.state_lock(), ExitStack() as rollback:
        registry = store.load_registry()
        skills = dict(registry.skills)
        policy = {'mode':mode,'cool':cool_seconds,'archive':archive_seconds,'protected':set(protected_ids),'complete':protection_complete,'now':now}
        for name,current in registry.skills.items():
            data = _maintained_state(store,registry,current,policy,rollback)
            if data != current.maintenance:
                skills[name] = replace(current,maintenance=data)
                changed.append(name)
        store.save_registry(replace(registry,skills=skills))
        if mode == 'autonomous' and protection_complete:
            removed_versions = _collect_unreferenced_versions(store,registry,set(protected_ids),keep_versions)
        rollback.pop_all()
    if changed:
        store.append_event(SkillLearningEvent('maintained',reason=','.join(changed)[:200]))
    return {'changed':changed,'protection_complete':protection_complete,'mode':mode,'removed_versions':removed_versions}


# LLM: idle只依结构时间，未读全保护账不归档；所有权校验失败保留原文件。目录移动由外层事务失败回滚。
# 函数用途: 为一条托管方法计算维护状态并执行安全的可恢复归档。
def _maintained_state(store,registry,current,policy,rollback):
    data = dict(current.maintenance)
    if not data.get('baseline_at'):
        data.update(baseline_at=policy['now'],state=data.get('state','trial'))
    idle = policy['now']-max(float(data['baseline_at']),float(data.get('last_used_at',0)))
    protected = data.get('pinned') or '*' in policy['protected'] or f'owner:{current.name}' in policy['protected'] or data.get('state') in {'disabled','archived'}
    if policy['mode'] != 'autonomous' or protected:
        return data
    if policy['archive'] > 0 and idle >= policy['archive'] and policy['complete'] and data.get('state') == 'cooled':
        if _archive_learning(store,registry,current.name,rollback):
            data['state'] = 'archived'
    elif policy['cool'] > 0 and idle >= policy['cool']:
        data.update(state='cooled',cooled_at=data.get('cooled_at') or policy['now'])
    return data


# LLM: 原registry+文件hash共同证明托管权，用户改过的文件不移动；归档没有blocked_names，不等于用户删除。
# 函数用途: 在原事务中移动一份可归档Skill并登记失败回滚。
def _archive_learning(store,registry,name,rollback):
    try:
        _owned_record(store,registry,name)
    except SkillLearningGateError:
        return False
    import os
    target = store.directory/'archived'/name
    if target.exists() or target.is_symlink():
        raise ValueError('归档目标冲突')
    target.parent.mkdir(parents=True,exist_ok=True)
    source = store.skill_path(name).parent
    os.replace(source,target)
    rollback.callback(os.replace,target,source)
    return True


# LLM: 清理只在引用完整且显式自主维护时运行，固定/在途/监控方法的全部历史保留；0明确不裁剪。
# 函数用途: 以安全编号清理未被引用的旧正文，保留当前激活版与最新若干修订，不永久删除Skill本身。
def _collect_unreferenced_versions(store,registry,protected,keep_versions):
    if keep_versions<=0:
        return 0
    removed = 0
    for name,record in registry.skills.items():
        if '*' in protected or f'owner:{name}' in protected or record.maintenance.get('pinned') or record.maintenance.get('state')=='disabled':
            continue
        paths = [path for path in (store.versions_dir/name).glob('v*.md') if path.stem[1:].isdigit()]
        paths.sort(key=lambda path:int(path.stem[1:]))
        if any(path.is_symlink() for path in paths):
            continue
        keep = {path.name for path in paths[-keep_versions:]} | {f'v{record.version}.md'}
        for path in paths:
            if path.name not in keep:
                path.unlink()
                removed += 1
    return removed
