# LLM: 唯一纯schema构造器；保持普通ID正则、skill_id例外、required/封闭参数及deferred hints，不声明执行策略。
# 模块用途: 给脚本、工作流和数据源工具共用ToolModelSpec构造，无IO或审批副作用。
from __future__ import annotations

from .models import ToolModelHints, ToolModelSpec


# LLM: 名称、说明、动作和属性由调用方显式提供；复制属性，不修改原schema或推导权限。
# 函数用途: 构造默认收起的工具描述，保持现行ID与动作参数合同。
def _spec(name, description, actions, properties) -> ToolModelSpec:
    properties = {key: {**value, **({'pattern': r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'}
                  if key.endswith('_id') and key != 'skill_id' or key in ('scope_key', 'entrypoint') else {})} for key, value in properties.items()}
    return ToolModelSpec(name, description, {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': actions}, **properties}, 'required': ['action'], 'additionalProperties': False},
        hints=ToolModelHints(category='capability', default_deferred=True,
                             deferred_summary=description.split('；', 1)[0][:60]))
