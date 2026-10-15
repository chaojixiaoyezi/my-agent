# LLM: 此工具只能声明当前任务采用或查本owner托管状态，不能提供human权威、修改质量、确认候选或跨owner读写。
# 模块用途: 为模型提供结构化方法采用回执，质量结果仍由用户控制/可靠验证事实登记。
from __future__ import annotations

import json

from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .skill_learning_maintenance import LearningObservationSource, observe_learning
from .skill_learning_publish import SkillLearningGateError
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_search_tool import _snapshot_for


# LLM: actor/run只读宿主当前执行，不接收模型身份参数；与skill_search使用同一固定快照。
# 类用途: 记录方法被采用的事实并查询推荐/维护状态，不把读取当成采用或成功。
class SkillFeedbackTool(BaseTool):
    model_spec = ToolModelSpec(name='skill_feedback',description='确实采用本轮已授权的自学Skill时登记adopted；此声明不会增加有效性评分。status查看本owner状态。用户结果反馈通过/skills learned feedback，正式内容确认不能由此工具执行。',
        input_schema={'type':'object','properties':{'operation':{'type':'string','enum':['adopted','status']},'skill_id':{'type':'string'}},'required':['operation','skill_id'],'additionalProperties':False})
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('mutating'))

    # LLM: 绑定owner Agent，不构造第二份存储或授权。
    # 函数用途: 保存当前宿主供每次调用核验。
    def __init__(self,agent):
        self.agent = agent

    # LLM: 只读取本轮scope和固定hash，未知/已撤销引用失败；异常不带正文或本地路径。
    # 函数用途: 为当前任务登记采用，或返回该自学条目的维护元数据。
    def execute(self,params):
        try:
            snapshot = _snapshot_for(self.agent)
            entry = snapshot.resolve(params.get('skill_id','')) if snapshot else None
            if entry is None or entry.source != 'owner':
                raise ValueError('当前任务没有该自学方法授权')
            store = SkillLearningStore.for_home(self.agent.home_paths)
            record = store.load_registry().skills.get(entry.name)
            if record is None or record.sha256 != entry.content_sha256:
                raise ValueError('当前方法版本不可登记')
            if params.get('operation') == 'status':
                result = {'skill_id':entry.stable_id,'sha256':record.sha256,'maintenance':record.maintenance}
            elif params.get('operation') == 'adopted':
                if not getattr(self.agent.config,'enable_self_learning',False):
                    raise ValueError('自学习未启用')
                current = self.agent._current_run_params
                from ..conversation.tool_approval_scope import (
                    tool_approval_scope,
                    tool_approval_scope_is_current,
                )
                scope = tool_approval_scope(self.agent,str(current.run_id or current.task_id))
                if not tool_approval_scope_is_current(self.agent,scope):
                    raise ValueError('当前执行已失效')
                attributes = getattr(current,'task_attributes',{}) or {}
                result = observe_learning(store,entry.name,sha256=entry.content_sha256,task_id=scope.root_task_id,
                    outcome='adopted',source=LearningObservationSource(run_id=scope.run_id,job_id=str(attributes.get('scheduler_job_id') or '')))
            else:
                raise ValueError('动作无效')
            return ToolHandlerOutcome('skill_feedback',True,json.dumps(result,ensure_ascii=False))
        except (AttributeError,ValueError,OSError,RuntimeError,SkillLearningStoreError,SkillLearningGateError,OwnerQuotaExceeded,OwnerQuotaUnavailable):
            return ToolHandlerOutcome('skill_feedback',False,'无法核验当前方法、任务或存储，未登记反馈。',error_code='SKILL_FEEDBACK_UNAVAILABLE')
