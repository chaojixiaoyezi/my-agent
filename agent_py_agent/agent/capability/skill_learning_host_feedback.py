# LLM: 仅由原验证命令/钉住能力包检查器宿主出口调用，不暴露模型工具；采用必须同owner、run、hash，副作用只写原registry。
# 模块用途: 把已落账的客观验证结果反馈给当前实际采用的方法，环境错误和普通工具成功不算质量证据。
from __future__ import annotations

from .skill_learning_maintenance import LearningObservationSource, observe_learning
from .skill_learning_store import SkillLearningStore


# LLM: 晚到工具回执不能冒当前新run/attempt记分；call身份来自宿主原调用，缺当前scope不猜测。
# 函数用途: 把同一实际执行的已落账证据批次交给质量记录器，跳过过期执行。
def observe_command_verifications(agent,call,evidence):
    current = getattr(agent,'_current_run_params',None)
    if getattr(current,'run_id',None) != call.run_id or getattr(current,'attempt_id',None) != call.attempt_id:
        return
    for fact in evidence:
        observe_command_verification(agent,current,fact)


# LLM: 调用点必须在原验证账本写入成功之后，receipt为原证据id；generic命令失败缺少内容/环境区分故不降分。
# 函数用途: 登记项目明确声明的验证命令通过事实，不解释命令、输出或整轮状态。
def observe_command_verification(agent, params, evidence):
    if evidence.get('status') == 'passed' and evidence.get('exit_code') == 0 and type(evidence.get('id')) is int:
        _observe(agent,params,'effective',f"verification:{evidence['id']}")


# LLM: 包检查器failed仅来自valid=false的声明式内容检查；not_run/error/cancelled均不记负样本，复用键绑定目标内容hash。
# 函数用途: 登记原沙箱检查器的内容有效性结论，不复制目标、检查错误或秘密。
def observe_pack_verification(agent, params, result, key):
    if result.status == 'passed' and result.valid is True:
        _observe(agent,params,'effective',f'pack:{key}')
    elif result.status == 'failed' and result.valid is False:
        _observe(agent,params,'ineffective',f'pack:{key}')


# LLM: 缺当前run或未登记准确采用时跳过，持久质量反馈尽力而为，绝不改变底层验证结果或授权。
# 函数用途: 将同一执行内采用的当前版本与宿主证据相连；不跨owner读取正文。
def _observe(agent,params,outcome,receipt):
    if not getattr(getattr(agent,'config',None),'enable_self_learning',False):
        return
    run_id = str(getattr(params,'run_id','') or '')
    current = getattr(agent,'_current_run_params',None)
    if not run_id or getattr(current,'run_id',None) != run_id or getattr(current,'attempt_id',None) != getattr(params,'attempt_id',None):
        return
    try:
        store = SkillLearningStore.for_home(agent.home_paths)
        registry = store.load_registry()
        for row in registry.skills.values():
            adoptions = [fact for fact in row.maintenance.get('observations',[]) if fact.get('authority')=='model' and fact.get('outcome')=='adopted' and fact.get('run_id')==run_id and fact.get('sha256')==row.sha256]
            for adoption in adoptions:
                observe_learning(store,row.name,sha256=row.sha256,task_id=adoption['task_id'],outcome=outcome,source=LearningObservationSource(authority='host',run_id=run_id,receipt_id=receipt,job_id=adoption.get('job_id','')))
    except (AttributeError,OSError,RuntimeError,ValueError):
        return
