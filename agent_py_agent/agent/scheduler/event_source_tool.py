# LLM: 单计划和规则只接收公开引用，当前请求专有映射后沿原 REST 核验；模型不能传应用/租户/proofs，最终仍守原 actor/CAS。
# 模块用途: 将公开机器人引用转换为原监控 canonical 计划，来源证明不另存账本或继承到新请求。
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from ..adapter.feishu_provider_facts import (
    FeishuProviderFactError,
    group_authority_snapshot,
    resolve_bot_source,
)
from ..capability.runtime_config_reload import deployment_capability_config
from ..conversation.host_actor import resolve_host_actor
from ..user_space.group_authorization import group_chat_id
from .event_sources import normalize_event_schedule
from .schedule import ScheduleValidationError

EVENT_SOURCE_INPUT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'oneOf': [{'required': ['source_message_refs']}, {'required': ['source_message_ids']}],
    'properties': {
        'source_message_refs': {'type': 'array', 'minItems': 1, 'maxItems': 32,
            'items': {'type': 'string'}, 'description': '原样复制本请求引用资料的 source_message_refs；宿主解析真实消息，不能自行构造。'},
        'source_message_ids': {'type': 'array', 'minItems': 1, 'maxItems': 32,
            'items': {'type': 'string'}, 'description': '已知准确的供应商 om_ 消息编号，不能填诊断短引用；与 source_message_refs 二选一。'},
        'min_interval_seconds': {'type': 'integer', 'minimum': 60, 'maximum': 86400},
        'max_batch_events': {'type': 'integer', 'minimum': 1, 'maximum': 16},
    },
}


# LLM: 只由本次宿主工具调用构造，不能从模型参数接收；来源预检在群策略锁外，最终新工作仍须取真实群授权。
# 类用途: 将已核验事件来源带到原同步写事务，避免持授权锁等待外部平台网络。
@dataclass(frozen=True)
class PreparedEventSources:
    schedule: dict | None = None
    rules: list | None = None


# LLM: 公开单计划与每条规则先经同一宿主来源解析，再进入持久规则校验；不持群策略写锁，最终写仍复核 actor/CAS。
# 函数用途: 在创建或更新前把公开引用转换为规范规则，普通定时计划不增加平台请求，不修改模型参数。
def prepare_event_sources(agent, params):
    from .rules import normalize_rules

    rules = None
    if 'rules' in params:
        raw = deepcopy(params['rules'])
        if isinstance(raw, list):
            for rule in raw:
                schedule = rule.get('schedule') if isinstance(rule, dict) else None
                if isinstance(schedule, dict) and schedule.get('kind') == 'event_source':
                    rule['schedule'] = schedule_from_event_input(agent, {key: value
                        for key, value in schedule.items() if key != 'kind'})
        rules = normalize_rules(raw)
    schedule = None
    if params.get('schedule_kind') == 'event_source':
        if any(params.get(key) not in (None, '') for key in ('at', 'after_seconds', 'every_seconds', 'anchor_at', 'cron', 'timezone')):
            raise ScheduleValidationError('事件订阅不能同时填写定时参数。')
        schedule = schedule_from_event_input(agent, params.get('event_source'))
    return PreparedEventSources(schedule, rules)


# LLM: 错误只携带固定码，原 provider 响应、目标链接和秘密不进入工具异常正文。
# 类用途: 明确区分部署关闭、平台权限不足与来源无法证明，写入前失败不标副作用未知。
class EventSourceInputError(ValueError):
    # LLM: code/message 由本模块固定选择，无文件或网络副作用。
    # 函数用途: 建立可安全返回给用户的订阅登记错误。
    def __init__(self, code, message):
        self.error_code = code
        super().__init__(message)


# LLM: 开关只读共享部署配置，群 ID 只读原 owner；普通个人、其它群或模型局部设置均不形成来源权威。
# 函数用途: 检查事件订阅入口，并从实时官方事实获得当前接收应用和群租户。
def _source_scope(agent):
    home = getattr(agent, 'home_paths', None)
    if getattr(home, 'owner_kind', '') != 'group':
        raise EventSourceInputError('EVENT_SOURCE_GROUP_REQUIRED', '机器人消息订阅只能在原群创建。')
    try:
        if deployment_capability_config(home).feishu_bot_subscriptions_enabled is not True:
            raise EventSourceInputError('EVENT_SOURCE_DISABLED', '部署者已关闭机器人消息订阅。')
        chat_id = group_chat_id(home)
        snapshot = group_authority_snapshot(agent, chat_id)
        if (snapshot.get('chat_id') != chat_id or snapshot.get('members_complete') is not True
                or snapshot.get('bot_is_member') is not True or not snapshot.get('app_id') or not snapshot.get('tenant_key')):
            raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '无法核验当前群与机器人，请检查飞书权限。')
        return snapshot
    except FeishuProviderFactError as exc:
        raise EventSourceInputError(exc.reason_code, '飞书未允许核验机器人消息来源，请检查群消息与群信息权限。') from exc
    except EventSourceInputError:
        raise
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise EventSourceInputError('EVENT_SOURCE_UNAVAILABLE', '机器人消息订阅配置或宿主通道不可用。') from exc


# LLM: 精确 Get 消息必须证明同一接收应用和群；原消息发送者类型 app/app_id 不与 event open_id 隐式互换。
# 函数用途: 读取有限原引用消息，生成去重的准确来源及证明清单。
def _source_proofs(agent, scope, message_ids):
    if (not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 32
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in message_ids)
            or len(set(message_ids)) != len(message_ids)):
        raise ScheduleValidationError('source_message_ids 需要 1～32 个不重复的原消息编号。')
    proofs = {}
    for message_id in message_ids:
        try:
            fact = resolve_bot_source(agent, scope['chat_id'], message_id)
        except FeishuProviderFactError as exc:
            raise EventSourceInputError(exc.reason_code, '无法核验所引用的其他机器人消息，未创建订阅。') from exc
        if (fact.get('app_id') != scope['app_id'] or fact.get('chat_id') != scope['chat_id']
                or fact.get('sender_id_type') != 'app_id' or fact.get('source_message_id') != message_id):
            raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '机器人来源与当前群不一致，未创建订阅。')
        proofs.setdefault(fact['sender_id'], {'sender_id': fact['sender_id'], 'source_message_id': message_id})
    return list(proofs.values())


# LLM: Gateway只消费本次专有引用，后台子孙须经原根/actor/当前attempt校验；不从prompt或attrs恢复，未知引用在Get前拒绝。
# 函数用途: 将本请求或准确原根的opaque引用解析为供应商编号，仍须原官方Get和人工授权。
def _message_ids(agent, raw):
    refs, identifiers = raw.get('source_message_refs'), raw.get('source_message_ids')
    if (refs is None) == (identifiers is None):
        raise ScheduleValidationError('source_message_refs 与 source_message_ids 必须且只能提供一个。')
    values = refs if refs is not None else identifiers
    if (not isinstance(values, list) or not 1 <= len(values) <= 32
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in values)
            or len(set(values)) != len(values)):
        raise ScheduleValidationError('消息引用需要 1～32 个不重复的字符串。')
    if refs is None:
        if any(not value.startswith('om_') for value in identifiers):
            raise ScheduleValidationError('source_message_ids 只能填写原供应商 om_ 编号；引用资料请使用 source_message_refs。')
        return identifiers
    current = getattr(agent, '_current_run_params', None)
    thread_id = str((getattr(current, 'task_attributes', None) or {}).get('conversation_thread_id') or '')
    actor = resolve_host_actor(agent, thread_id)
    from ..conversation.host_message_context import references_for_run

    if actor is None or actor.channel != 'feishu':
        raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '当前请求没有可核验的原消息引用。')
    try:
        mapping = dict(references_for_run(agent, actor))
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '原请求引用执行身份不可核验。') from exc
    if any(reference not in mapping for reference in refs):
        raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '引用不属于当前请求，未创建订阅。')
    return [mapping[reference] for reference in refs]


# LLM: 人工权限门应先于本函数；这里只生成已核来源的原 schedule，数据库、线程与运行创建由既有工具负责。
# 函数用途: 把公开引用经本请求映射和官方 Get 证明转换为唯一 canonical 计划，不接受模型身份字段。
def schedule_from_event_input(agent, raw):
    if not isinstance(raw, dict) or set(raw) - {'source_message_refs', 'source_message_ids', 'min_interval_seconds', 'max_batch_events'}:
        raise ScheduleValidationError('event_source 参数无效。')
    identifiers = _message_ids(agent, raw)
    scope = _source_scope(agent)
    proofs = _source_proofs(agent, scope, identifiers)
    return normalize_event_schedule({'kind': 'event_source',
        'source': {'channel': 'feishu', 'event_type': 'message', 'app_id': scope['app_id'],
            'tenant_key': scope['tenant_key'], 'chat_id': scope['chat_id'], 'sender_type': 'bot',
            'sender_id_type': 'app_id', 'sender_ids': [row['sender_id'] for row in proofs], 'proofs': proofs},
        **{key: raw[key] for key in ('min_interval_seconds', 'max_batch_events') if key in raw}})
