# LLM: 预算仅作用于分析材料的数据域，不调用模型、不落盘、不改步骤规则；估算不是供应商实耗。
# 模块用途: 纯函数执行显式输入预算，返回模型可见截断标记与宿主attempt事实。
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..memory_archive.tokens import estimate_tokens

_DATA_FIELDS = ('prior_outputs', 'run_inputs', 'step_inputs', 'data_sources')
_MISSING = object()


# LLM: 异常事实与成功事实同形，调用者在任何模型调用前记录拒绝。
# 类用途: 携带输入预算错误码及可审计的候选输入事实。
class AnalysisInputBudgetError(ValueError):
    # LLM: 不解析异常文案决定错误类型。
    # 函数用途: 固定结构化错误码和预算事实。
    def __init__(self, message: str, *, error_code: str, facts: dict[str, Any]):
        super().__init__(message)
        self.error_code = error_code
        self.facts = facts


# LLM: 无预算完全短路；截断只保留原值前缀，不生成摘要或切片请求；标记本身也占预算。
# 函数用途: 校验步骤预算并返回不超额的分析材料，不修改任何调用方对象。
def apply_input_budget(material: dict[str, Any], budget: object) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return apply_prompt_input_budget(material, budget)


# LLM: 前缀为宿主计算的不可裁剪正文，不来自预算额外键；完整序列化投影参与同一估算，默认空前缀保留原合同。
# 函数用途: 为正式后端的user提示前缀预留预算，仍只裁四个材料数据域。
def apply_prompt_input_budget(material, budget, *, prompt_prefix=''):
    """字段选择器为四个数据域及其点分隔字典路径；优先字段按声明顺序保留。

    drop_fields只在超额时删除；未声明优先级的部分按域顺序、字典键排序和数组原序保留。
    truncated_item_count统计被删或被改短的标量叶项（空容器作为一项）。
    """
    if budget is None:
        return material, None
    before = _prompt_tokens(material, prompt_prefix=prompt_prefix)
    facts = _budget_facts(material, material, before, list(_DATA_FIELDS))
    try:
        limit, policy, priority, drop = _validate_budget(budget)
    except ValueError as exc:
        raise AnalysisInputBudgetError(str(exc), error_code='WORKFLOW_INPUT_BUDGET_INVALID', facts=facts) from exc
    if before <= limit:
        return material, facts
    if policy == 'reject':
        raise AnalysisInputBudgetError('分析输入超过声明预算',
                                       error_code='WORKFLOW_ANALYSIS_INPUT_OVER_BUDGET', facts=facts)

    data = deepcopy({key: material[key] for key in _DATA_FIELDS if key in material})
    for path in drop:
        _take_path(data, path)
    pieces = []
    for path in priority:
        value = _take_path(data, path)
        if value is not _MISSING:
            pieces.append((path, value))
    pieces.extend((key, data[key]) for key in _DATA_FIELDS if key in data)
    weighted = [(path, value, _weight(value)) for path, value in pieces]
    tracked = list(dict.fromkeys([*_DATA_FIELDS, *priority, *drop]))

    # LLM: 每轮是整份确定性前缀投影；试算次数对材料长度为对数，不逐项重编码整份大材料。
    # 函数用途: 为一个保留量生成完整候选及包含标记开销的事实。
    def candidate(allowance):
        result = {key: value for key, value in material.items() if key not in _DATA_FIELDS}
        for path, value, weight in weighted:
            kept = _prefix(value, min(allowance, weight))
            if kept is not _MISSING:
                _put_path(result, path, kept)
            allowance = max(0, allowance - weight)
        current = _budget_facts(material, result, before, tracked)
        result['input_budget'] = {key: value for key, value in current.items()
                                  if key not in ('input_tokens_before_budget', 'input_tokens_after_budget')}
        current['input_tokens_after_budget'] = _prompt_tokens(result, prompt_prefix=prompt_prefix)
        return result, current

    best, facts = candidate(0)
    if facts['input_tokens_after_budget'] > limit:
        raise AnalysisInputBudgetError('完整规则与截断标记无法容纳于声明预算',
                                       error_code='WORKFLOW_ANALYSIS_INPUT_OVER_BUDGET', facts=facts)
    low, high = 0, sum(weight for _, _, weight in weighted)
    complete, complete_facts = candidate(high)
    if complete_facts['input_tokens_after_budget'] <= limit:
        return complete, complete_facts
    while low < high:
        middle = (low + high + 1) // 2
        trial, trial_facts = candidate(middle)
        if trial_facts['input_tokens_after_budget'] <= limit:
            low, best, facts = middle, trial, trial_facts
        else:
            high = middle - 1
    return best, facts


# LLM: 四键白名单是显式版本合同；bool不能冒充整数，规则域不能作为裁剪目标。
# 函数用途: 校验预算结构、策略及无歧义的数据选择器。
def _validate_budget(budget):
    allowed = {'input_token_budget', 'overflow_policy', 'priority_fields', 'drop_fields'}
    if not isinstance(budget, dict) or set(budget) - allowed:
        raise ValueError('input_budget只接受四个声明字段')
    limit = budget.get('input_token_budget')
    policy = budget.get('overflow_policy', 'truncate')
    if type(limit) is not int or limit <= 0:
        raise ValueError('input_token_budget必须为正整数且非bool')
    if not isinstance(policy, str) or policy not in ('truncate', 'reject'):
        raise ValueError('overflow_policy仅支持truncate或reject')
    fields = []
    for name in ('priority_fields', 'drop_fields'):
        paths = budget.get(name, [])
        if (not isinstance(paths, list) or any(
                not isinstance(path, str) or path.split('.')[0] not in _DATA_FIELDS
                or any(not part for part in path.split('.')) for path in paths)):
            raise ValueError(f'{name}必须为数据域点路径列表')
        if len(set(paths)) != len(paths):
            raise ValueError(f'{name}不能包含重复路径')
        fields.append(paths)
    priority, drop = fields
    if any(a == b or a.startswith(b + '.') or b.startswith(a + '.') for a in priority for b in drop):
        raise ValueError('优先与丢弃路径不能重叠')
    # 同一优先集合的祖先覆盖子项，保留最先声明的优先顺序。
    priority = [path for path in priority if not any(
        path.startswith(parent + '.') for parent in priority)]
    return limit, policy, priority, drop


# LLM: 与handler及后端的最终user正文参数一致，计入不可裁剪前缀、转义符、非ASCII及模型可见标记。
# 函数用途: 用唯一估算器计算实际user prompt，不把协议schema/system或供应商实耗混入本口径。
def _prompt_tokens(material, *, prompt_prefix=''):
    return estimate_tokens(prompt_prefix + json.dumps(material, ensure_ascii=False))


# LLM: 选择器仅穿越字典，不解释内容或把业务ID当数组位置。
# 函数用途: 读取可选数据路径，保留缺失与null的区别。
def _get_path(data, path):
    value = data
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


# LLM: 仅对私有数据副本移除已选字段，不影响原材料。
# 函数用途: 提取优先字段或删除显式drop字段。
def _take_path(data, path):
    parts = path.split('.')
    parent = data
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            return _MISSING
        parent = parent[part]
    return parent.pop(parts[-1], _MISSING) if isinstance(parent, dict) else _MISSING


# LLM: 优先子树和剩余子树互不重叠，合并时不能覆盖先保留的优先字段。
# 函数用途: 把前缀投影放回原数据域形状。
def _put_path(data, path, value):
    parts = path.split('.')
    parent = data
    for part in parts[:-1]:
        parent = parent.setdefault(part, {})
    old = parent.get(parts[-1], _MISSING)
    parent[parts[-1]] = _merge(old, value) if isinstance(old, dict) and isinstance(value, dict) else value


# LLM: 合并只操作候选内部新容器，不触碰固定规则域。
# 函数用途: 组合互斥数据子树。
def _merge(left, right):
    result = dict(left)
    for key, value in right.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


# LLM: 保留量只是内部搜索坐标，最终准入仍依据完整JSON的token估算。
# 函数用途: 计算可确定性截断的数据树权重。
def _weight(value):
    if isinstance(value, dict):
        return 1 + sum(len(str(key)) + 1 + _weight(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_weight(item) for item in value)
    return 1 + len(value) if isinstance(value, str) else 1


# LLM: 原数组顺序不变，字符串只取前缀；零保留量显式缺失，不用伪造摘要替换原证据。
# 函数用途: 按保留量投影JSON树，返回完整可序列化值或缺失哨兵。
def _prefix(value, allowance):
    if allowance <= 0:
        return _MISSING
    if isinstance(value, str):
        return value[:allowance - 1]
    if isinstance(value, dict):
        result, remaining = {}, allowance - 1
        for key in sorted(value):
            cost = len(str(key)) + 1
            item = _prefix(value[key], remaining - cost)
            if item is _MISSING:
                break
            result[key] = item
            remaining -= cost + _weight(value[key])
        return result
    if isinstance(value, (list, tuple)):
        result, remaining = [], allowance - 1
        for item in value:
            kept = _prefix(item, remaining)
            if kept is _MISSING:
                break
            result.append(kept)
            remaining -= _weight(item)
        return result
    return value


# LLM: 叶项计数不把字符串字符冒充证据条数。
# 函数用途: 计算删除子树涉及的原始条目数。
def _item_count(value):
    if isinstance(value, dict):
        return sum(_item_count(item) for item in value.values()) or 1
    if isinstance(value, (list, tuple)):
        return sum(_item_count(item) for item in value) or 1
    return 1


# LLM: 删除与部分截短均计入，未改动字段不计入。
# 函数用途: 对照原始证据和候选投影统计截断叶项。
def _removed_count(before, after):
    if after is _MISSING:
        return _item_count(before)
    if isinstance(before, dict) and isinstance(after, dict):
        return sum(_removed_count(value, after.get(key, _MISSING)) for key, value in before.items())
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return sum(_removed_count(item, after[index] if index < len(after) else _MISSING)
                   for index, item in enumerate(before))
    return int(before != after)


# LLM: 原文产物能力尚未接入时明确记录不可用，不虚构文件或引用。
# 函数用途: 生成固定预算事实，标明仍有部分保留和发生省略的域及选择器。
def _budget_facts(original, result, before, tracked):
    present = [path for path in tracked if _get_path(original, path) is not _MISSING]
    omitted = [path for path in present if _get_path(original, path) != _get_path(result, path)]
    return {
        'input_tokens_before_budget': before,
        'input_tokens_after_budget': before,
        'truncated_item_count': sum(_removed_count(original[key], result.get(key, _MISSING))
                                    for key in _DATA_FIELDS if key in original),
        'evidence_truncated': bool(omitted),
        'retained_fields': [path for path in present if _get_path(result, path) is not _MISSING],
        'omitted_fields': omitted,
        'raw_artifact_ref': None,
        'raw_artifact_unavailable_reason': 'RUN_OUT_NOT_AVAILABLE',
    }
