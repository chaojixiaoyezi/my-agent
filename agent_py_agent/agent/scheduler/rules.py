# LLM: 一个监控的规则保存在原 job；运行、依赖、通知只读结构字段，不从监控名/提示词推断 LLM、周期或条件。
# 模块用途: 校验监控规则、工具动作和依赖图，供原调度仓库及工具 schema 共用，不执行副作用。
from __future__ import annotations

from copy import deepcopy

from .delivery_policy import normalize_delivery
from .execution_policy import normalize_execution, normalize_precheck
from .policy import normalize_policy
from .schedule import ScheduleValidationError, validate_schedule

_RULE_FIELDS = frozenset({"id", "name", "enabled", "schedule", "use_llm", "prompt", "tool", "execution", "policy", "precheck", "delivery", "depends_on", "condition"})
_CONDITION_OPERATORS = frozenset({"eq", "ne", "exists", "truthy"})


# LLM: use_llm 必须显式布尔，NO 动作必须是明确工具参数；工具可用性和权限仍在真实执行器复核。
# 函数用途: 校验一个监控的全部规则，保证编号唯一、依赖存在且无循环。
def normalize_rules(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleValidationError("rules 必须是至少一项规则的数组")
    rules = [_normalize_rule(row) for row in raw]
    identifiers = [rule["id"] for rule in rules]
    if len(set(identifiers)) != len(identifiers):
        raise ScheduleValidationError("监控规则编号重复")
    graph = {rule["id"]: rule["depends_on"] for rule in rules}
    for rule in rules:
        if any(identifier not in graph for identifier in rule["depends_on"]):
            raise ScheduleValidationError("规则依赖必须属于同一监控")
    _validate_acyclic(graph)
    return rules


# LLM: 每条规则只有一个 schedule/action/condition 权威，未提供的通知继承监控默认，不复制另一份默认值。
# 函数用途: 规范化一条 LLM 或纯工具规则，非法定义在登记前拒绝。
def _normalize_rule(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) - _RULE_FIELDS:
        raise ScheduleValidationError("监控规则字段无效")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 64 or not all(c.isalnum() or c in "_-" for c in identifier):
        raise ScheduleValidationError("规则 id 必须是 1～64 位字母数字、下划线或连字符")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ScheduleValidationError("规则 name 必须是 1～160 字符的短语")
    enabled, use_llm = raw.get("enabled", True), raw.get("use_llm")
    if not isinstance(enabled, bool) or not isinstance(use_llm, bool):
        raise ScheduleValidationError("enabled/use_llm 必须是布尔值")
    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 32000 or (use_llm and not prompt.strip()):
        raise ScheduleValidationError("LLM 规则需要非空 prompt，最多 32000 字符")
    tool = _normalize_tool(raw.get("tool"), use_llm=use_llm)
    if (raw.get('schedule') or {}).get('kind') == 'event_source' and not use_llm:
        raise ScheduleValidationError('事件订阅条件必须由原 LLM 研判')
    dependencies = raw.get("depends_on", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies) or len(set(dependencies)) != len(dependencies):
        raise ScheduleValidationError("depends_on 必须是同监控的不重复规则编号数组")
    condition = _normalize_condition(raw.get("condition"))
    if condition is not None and condition["rule_id"] not in dependencies:
        raise ScheduleValidationError("condition 的规则必须包含在 depends_on 中")
    return {"id": identifier, "name": name.strip(), "enabled": enabled, "use_llm": use_llm,
        "schedule": validate_schedule(raw.get("schedule")), "prompt": prompt.strip(), "tool": tool,
        "execution": normalize_execution(raw.get("execution")), "policy": normalize_policy(raw.get("policy")),
        "precheck": normalize_precheck(raw.get("precheck")),
        "delivery": normalize_delivery(raw["delivery"]) if raw.get("delivery") is not None else None,
        "depends_on": list(dependencies), "condition": condition}


# LLM: NO 动作保留原工具名和 JSON 参数，不拼 shell、不执行发现；宿主身份参数只能由运行时注入。
# 函数用途: 校验不调用模型的明确动作，与 LLM 规则互斥。
def _normalize_tool(raw: object, *, use_llm: bool) -> dict | None:
    if use_llm:
        if raw is not None:
            raise ScheduleValidationError("LLM 规则不同时指定 tool")
        return None
    if not isinstance(raw, dict) or set(raw) != {"name", "arguments"} or not isinstance(raw["name"], str) or not raw["name"].strip():
        raise ScheduleValidationError("NO 规则需要 tool.name 与 tool.arguments")
    arguments = raw["arguments"]
    if not isinstance(arguments, dict) or any(not isinstance(key, str) or key.startswith("__") for key in arguments):
        raise ScheduleValidationError("NO 工具参数不允许宿主身份字段")
    return {"name": raw["name"].strip(), "arguments": deepcopy(arguments)}


# LLM: 条件只选同监控最新确认规则结果的显式 JSON 指针；不解析自然语言，0 过期窗口表示不限制。
# 函数用途: 规范化可选执行条件，缺数据时运行端只能等待/跳过，不能假造满足。
def _normalize_condition(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"rule_id", "pointer", "op", "value", "max_age_seconds"}:
        raise ScheduleValidationError("condition 字段无效")
    identifier, pointer, op = raw.get("rule_id"), raw.get("pointer", ""), raw.get("op")
    if not isinstance(identifier, str) or not identifier or not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ScheduleValidationError("condition 需要规则编号与合法 JSON pointer")
    if not isinstance(op, str) or op not in _CONDITION_OPERATORS:
        raise ScheduleValidationError("condition.op 必须为 eq/ne/exists/truthy")
    age = raw.get("max_age_seconds", 0)
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise ScheduleValidationError("max_age_seconds 必须是非负整数，0 不限制")
    if op in {"eq", "ne"} and "value" not in raw:
        raise ScheduleValidationError("eq/ne 条件必须给出 value")
    return {"rule_id": identifier, "pointer": pointer, "op": op, "max_age_seconds": age,
            **({"value": deepcopy(raw["value"])} if "value" in raw else {})}


# LLM: 图只包含已校验的本监控规则编号；迭代检测不依赖 Python 递归深度，不读取其它监控或任务。
# 函数用途: 拒绝循环依赖，允许多个独立规则及合法先后关系。
def _validate_acyclic(graph: dict[str, list[str]]) -> None:
    remaining = {key: set(values) for key, values in graph.items()}
    while remaining:
        ready = {key for key, values in remaining.items() if not values}
        if not ready:
            raise ScheduleValidationError("监控规则不能循环依赖")
        remaining = {key: values - ready for key, values in remaining.items() if key not in ready}

RULES_SCHEMA = {'type':'array','minItems':1,'items':{'type':'object','additionalProperties':False,
    'properties':{
        'id':{'type':'string'},'name':{'type':'string'},'enabled':{'type':'boolean'},'use_llm':{'type':'boolean'},
        'schedule':{'type':'object','additionalProperties':False,'properties':{
            'kind':{'type':'string','enum':['every','at','cron','event_source']},'every_seconds':{'type':'integer','minimum':60,'maximum':31622400},
            'anchor_at':{},'at':{'type':'string'},'expression':{'type':'string'},'timezone':{'type':'string'}},'required':['kind']},
        'prompt':{'type':'string'},'tool':{'type':'object','additionalProperties':False,
            'properties':{'name':{'type':'string'},'arguments':{'type':'object'}},'required':['name','arguments']},
        'execution':{'type':'object'},'policy':{'type':'object'},'precheck':{'type':['object','null']},
        'delivery':{'type':['object','null'],'properties':{'mode':{'type':'string','enum':['origin','local','none']}}},
        'depends_on':{'type':'array','items':{'type':'string'}},'condition':{'type':['object','null'],'additionalProperties':False,
            'properties':{'rule_id':{'type':'string'},'pointer':{'type':'string'},'op':{'type':'string','enum':['eq','ne','exists','truthy']},
                'value':{},'max_age_seconds':{'type':'integer','minimum':0}},'required':['rule_id','op']}},
    'required':['id','name','schedule','use_llm']},
    'description':'同一监控的独立规则；LLM 给 prompt，NO 给 tool；cron.expression 支持 0 9,18 * * *。depends_on/condition 只读同监控确认 JSON 工具结果。'}

from .event_sources import EVENT_SOURCE_SCHEDULE_SCHEMA  # noqa: E402

RULES_SCHEMA['items']['properties']['schedule']['properties'].update({key: value
    for key, value in EVENT_SOURCE_SCHEDULE_SCHEMA['properties'].items() if key != 'kind'})
