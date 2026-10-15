# LLM: 模型工具只能准备本owner精确清单与查询自己可见的状态，永远不能授权来源或替目标accept；人类控制走原/skills。
# 模块用途: 提供Skill投递/一次复刻准备入口，不复制权限、公共受管包或未经明确选定的私人内容。
from __future__ import annotations

import json

from ..capability.skill_learning_store import SkillLearningStoreError
from ..capability.skill_transfer import load_transfer, prepare_transfer, require_transfer_enabled
from ..capability.skill_transfer_manifest import SkillTransferError, public_manifest
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: 不提供authorize/accept参数，来源只用当前owner，资源按显式相对路径冻结。
# 函数用途: 构造默认延迟加载、可tool_search发现的准备/状态schema，不能从参数制造双端授权。
def build_skill_transfer_spec():
    owner = {'type': 'object', 'properties': {key: {'type': 'string'} for key in ('provider', 'owner_kind', 'owner_id')},
         'required': ['provider', 'owner_kind', 'owner_id'], 'additionalProperties': False}
    return ToolModelSpec(name='skill_transfer', description='准备Skill定向投递/一次复刻清单；双端本人必须分别用/skills transfer命令确认。', input_schema={'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['prepare', 'status']}, 'target': owner,
        'skills': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'stable_id': {'type': 'string'}, 'content_sha256': {'type': 'string'},
        'resources': {'type': 'array', 'items': {'type': 'string'}}, 'target_name': {'type': 'string'}},
        'required': ['stable_id', 'content_sha256'], 'additionalProperties': False}},
        'clone': {'type': 'boolean'}, 'memory_candidates': {'type': 'array', 'items': {'type': 'string'}},
        'reference': {'type': 'string'}}, 'required': ['action'], 'additionalProperties': False},
        hints=ToolModelHints(category='capability',default_deferred=True,
            deferred_summary='准备Skill定向投递或一次复刻的准确清单、查询状态；双端本人用/skills transfer确认。',
            keywords=('skill','transfer','clone','投递','复刻','迁移')))



# LLM: 工具绑定当前owner宿主，参数不能指定source或自称authorized；根注册需覆盖普通/群/admin主代理，不默认下放孩子。
# 类用途: 让模型按用户要求准备准确可审阅的跨助手内容清单。
class SkillTransferTool(BaseTool):
    name = 'skill_transfer'
    model_spec = build_skill_transfer_spec()
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('mutating'), idempotency_policy=IdempotencyPolicy('operation'))
    description = '准备向指定助手分享私有Skill或一次复刻精确Skill/已审核记忆候选；双端本人须用/skills transfer命令确认。'

    # LLM: 构造无文件/网络副作用，owner只读传入宿主。
    # 函数用途: 绑定当前执行助手。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 精确schema校验在生产执行口重复，不只依赖模型schema；失败不输出异常正文、bundle或私人路径。
    # 函数用途: 准备清单/查看状态，向模型返回可让本人确认的摘要和原引用。
    def execute(self, params):
        try:
            if not isinstance(params, dict) or set(params) - {'action', 'target', 'skills', 'clone', 'memory_candidates', 'reference'}:
                raise SkillTransferError('SKILL_TRANSFER_ARGUMENTS_INVALID')
            require_transfer_enabled(self.agent)
            if params.get('action') == 'prepare':
                if 'reference' in params:
                    raise SkillTransferError('SKILL_TRANSFER_ARGUMENTS_INVALID')
                result = prepare_transfer(self.agent, params.get('target'), params.get('skills', []),
                    clone=params.get('clone', False), memory_candidates=params.get('memory_candidates', []))
            elif params.get('action') == 'status' and set(params) == {'action', 'reference'}:
                _payload, row, manifest, _store = load_transfer(self.agent, params['reference'])
                result = {'ok': True, 'status': row['status'], 'manifest': public_manifest(manifest), 'reference': params['reference']}
            else:
                raise SkillTransferError('SKILL_TRANSFER_ARGUMENTS_INVALID')
            return ToolHandlerOutcome(self.name, True, json.dumps(result, ensure_ascii=False), result_envelope={self.name: result})
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
            code = 'OWNER_DISK_QUOTA_EXCEEDED' if isinstance(exc, OwnerQuotaExceeded) else 'OWNER_QUOTA_UNAVAILABLE'
            return ToolHandlerOutcome(self.name, False, '本助手存储配额不足或不可核验；没有导入目标，不消耗复刻次数。', error_code=code, effect_outcome='not_started')
        except SkillTransferError as exc:
            return ToolHandlerOutcome(self.name, False, f'迁移准备未完成（{exc.code}）。', error_code=exc.code, effect_outcome='not_started')
        except (SkillLearningStoreError, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
            return ToolHandlerOutcome(self.name, False, '迁移准备状态尚未确认，请查询原引用，不要重复生成操作。',
                error_code='SKILL_TRANSFER_UNAVAILABLE', effect_outcome='unknown')
