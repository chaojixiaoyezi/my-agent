# LLM: 只准备本owner清单或只读自己回执；参数错误给字段修正提示，永远不能授权来源或替目标accept。
# 模块用途: 准备Skill投递/一次复刻并查询单条或完整本身份回执，不复制权限或私人正文。
from __future__ import annotations

import json

from ..capability.skill_learning_store import SkillLearningStoreError
from ..capability.skill_transfer import (
    SkillTransferInputError,
    list_transfer_statuses,
    load_transfer,
    prepare_transfer,
    require_transfer_enabled,
    validate_transfer_selection,
)
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


# LLM: 不提供authorize/accept，来源固定当前owner；status不传reference时只读本owner列表，不制造双端授权。
# 函数用途: 声明准备参数格式与可省略引用的状态查询schema。
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
        'reference': {'type': 'string', 'description': 'status可省略或留空以列出本身份发出/收到的投递回执；非空时查原引用。'}}, 'required': ['action'], 'additionalProperties': False},
        hints=ToolModelHints(category='capability',default_deferred=True,
            deferred_summary='准备Skill定向投递或一次复刻的准确清单、查询状态；双端本人用/skills transfer确认。',
            keywords=('skill','transfer','clone','投递','复刻','迁移')))



# LLM: 纯参数检查可在claim前执行；只说明宿主字段，不回显值，不读取存储或授权来源。
# 函数用途: 对缺动作、错误status字段和缺少prepare选择给出准确提示。
def _validate_transfer_arguments(params):
    if not isinstance(params, dict) or set(params) - {'action', 'target', 'skills', 'clone', 'memory_candidates', 'reference'}:
        raise SkillTransferInputError('参数须为对象，仅支持 action、target、skills、clone、memory_candidates、reference。')
    if params.get('action') not in ('prepare', 'status'):
        raise SkillTransferInputError('缺少或非法参数 action（可选值：prepare / status）。')
    if params['action'] == 'status':
        if set(params) - {'action', 'reference'}:
            raise SkillTransferInputError('action=status 仅支持 reference，不接受 target、skills、clone 或 memory_candidates。')
        if not isinstance(params.get('reference', ''), str):
            raise SkillTransferInputError('reference 必须为字符串；省略或空字符串可列出本身份回执。')
        return
    if 'reference' in params:
        raise SkillTransferInputError('action=prepare 不接受 reference；reference 只用于 status。')
    target = params.get('target')
    if not isinstance(target, dict) or set(target) != {'provider', 'owner_kind', 'owner_id'}:
        raise SkillTransferInputError('action=prepare 缺少或非法参数 target（target_owner，需包含 provider、owner_kind、owner_id）。')
    validate_transfer_selection(params.get('skills', []), clone=params.get('clone', False), memory_candidates=params.get('memory_candidates', []))


# LLM: 入口只能读取原引用或本owner列表；开关、原单引用验签/范围校验保持，不根据记录授予任何控制权限。
# 函数用途: 读取投递状态并统一为安全元数据回执。
def _transfer_status(agent, reference):
    if not reference:
        return list_transfer_statuses(agent)
    _payload, row, manifest, _store = load_transfer(agent, reference)
    return {'ok': True, 'status': row['status'], 'manifest': public_manifest(manifest), 'reference': reference}


# LLM: 绑定当前owner宿主；准备仍需人工双端确认，状态只读，不允许参数指定source或自称authorized。
# 类用途: 为模型提供准备与回执查询，不提供授权/接收动作。
class SkillTransferTool(BaseTool):
    name = 'skill_transfer'
    model_spec = build_skill_transfer_spec()
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('mutating',
        by_parameter=(('action', (('status', 'read_only'), ('prepare', 'mutating'))),)), idempotency_policy=IdempotencyPolicy('operation'))
    description = '准备向指定助手分享私有Skill或一次复刻精确Skill/已审核记忆候选；双端本人须用/skills transfer命令确认。'

    # LLM: 构造无文件/网络副作用，owner只读传入宿主。
    # 函数用途: 绑定当前执行助手。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 条件参数错误在操作claim前返回，纯检查不写文件或读取其它owner。
    # 函数用途: 提前解释prepare/status参数组合不合法的原因。
    def validate_invocation(self, params, context):
        try:
            _validate_transfer_arguments(params)
        except SkillTransferInputError as exc:
            return ToolHandlerOutcome(self.name, False, exc.message, error_code=exc.code, effect_outcome='not_started')
        return None

    # LLM: 原准备/单引用安全门保持；无引用只列本owner回执；仅固定字段说明可展示，底层异常不暴露。
    # 函数用途: 准备清单或查看状态，失败给可修正的字段提示，不代替用户确认。
    def execute(self, params):
        try:
            _validate_transfer_arguments(params)
            require_transfer_enabled(self.agent)
            if params.get('action') == 'prepare':
                result = prepare_transfer(self.agent, params.get('target'), params.get('skills', []),
                    clone=params.get('clone', False), memory_candidates=params.get('memory_candidates', []))
            else:
                result = _transfer_status(self.agent, params.get('reference', ''))
            return ToolHandlerOutcome(self.name, True, json.dumps(result, ensure_ascii=False), result_envelope={self.name: result})
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
            code = 'OWNER_DISK_QUOTA_EXCEEDED' if isinstance(exc, OwnerQuotaExceeded) else 'OWNER_QUOTA_UNAVAILABLE'
            return ToolHandlerOutcome(self.name, False, '本助手存储配额不足或不可核验；没有导入目标，不消耗复刻次数。', error_code=code, effect_outcome='not_started')
        except SkillTransferError as exc:
            return ToolHandlerOutcome(self.name, False, f'{getattr(exc, "message", "迁移准备未完成")}（{exc.code}）。', error_code=exc.code, effect_outcome='not_started')
        except (SkillLearningStoreError, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
            return ToolHandlerOutcome(self.name, False, '迁移准备状态尚未确认，请查询原引用，不要重复生成操作。',
                error_code='SKILL_TRANSFER_UNAVAILABLE', effect_outcome='unknown')
