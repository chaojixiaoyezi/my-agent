# LLM: 模型只提供准确引用消息，宿主沿原 REST 身份核验来源；不能用模型传入的应用、租户、群或姓名授予订阅权限。
# 模块用途: 将机器人引用转换为原监控事件计划，并在规则新增/更新时复核真实来源，不保存第二授权账。
from __future__ import annotations

from dataclasses import dataclass

from ..adapter.feishu_provider_facts import (
    FeishuProviderFactError,
    group_authority_snapshot,
    resolve_bot_source,
)
from ..capability.runtime_config_reload import deployment_capability_config
from ..user_space.group_authorization import group_chat_id
from .event_sources import normalize_event_schedule
from .schedule import ScheduleValidationError

EVENT_SOURCE_INPUT_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['source_message_ids'],
    'properties': {
        'source_message_ids': {'type': 'array', 'minItems': 1, 'maxItems': 32,
            'items': {'type': 'string'}, 'description': '当前群被引用的其他机器人原消息编号，宿主核验实际发送应用。'},
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


# LLM: 查询外部平台不持原群策略写锁；结果只是本次来源证明，不是权限租约，不能绕过最终 actor/CAS 核对。
# 函数用途: 在创建或更新前规范规则并核验事件来源，普通定时计划不增加平台请求。
def prepare_event_sources(agent, params):
    from .rules import normalize_rules

    rules = normalize_rules(params['rules']) if 'rules' in params else None
    if rules is not None:
        verify_event_rule_sources(agent, rules)
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


# LLM: 人工权限门应先于本函数；这里只生成已核来源的原 schedule，数据库、线程与运行创建由既有工具负责。
# 函数用途: 将模型的精确引用输入转换为 canonical event_source 计划，不扩大普通消息接收权限。
def schedule_from_event_input(agent, raw):
    if not isinstance(raw, dict) or set(raw) - {'source_message_ids', 'min_interval_seconds', 'max_batch_events'}:
        raise ScheduleValidationError('event_source 参数无效。')
    scope = _source_scope(agent)
    proofs = _source_proofs(agent, scope, raw.get('source_message_ids'))
    return normalize_event_schedule({'kind': 'event_source',
        'source': {'channel': 'feishu', 'event_type': 'message', 'app_id': scope['app_id'],
            'tenant_key': scope['tenant_key'], 'chat_id': scope['chat_id'], 'sender_type': 'bot',
            'sender_id_type': 'app_id', 'sender_ids': [row['sender_id'] for row in proofs], 'proofs': proofs},
        **{key: raw[key] for key in ('min_interval_seconds', 'max_batch_events') if key in raw}})


# LLM: canonical schema 校验不等于来源授权；所有 event_source 规则写入前仍逐条核验原群、接收应用和准确发送应用。
# 函数用途: 防止通过多规则入口直接伪造应用/租户/群身份，普通定时规则不发额外请求。
def verify_event_rule_sources(agent, rules):
    for rule in rules:
        schedule = rule.get('schedule') or {}
        if schedule.get('kind') != 'event_source':
            continue
        schedule = normalize_event_schedule(schedule)
        source = schedule['source']
        scope = _source_scope(agent)
        if any(source[key] != scope[key] for key in ('app_id', 'tenant_key', 'chat_id')):
            raise EventSourceInputError('EVENT_SOURCE_SCOPE_UNVERIFIED', '事件规则只能订阅当前群的准确机器人来源。')
        facts = _source_proofs(agent, scope, [row['source_message_id'] for row in source['proofs']])
        if {row['sender_id']: row['source_message_id'] for row in facts} != {
                row['sender_id']: row['source_message_id'] for row in source['proofs']}:
            raise EventSourceInputError('EVENT_SOURCE_SENDER_UNVERIFIED', '事件规则发送应用与原引用消息不一致。')
