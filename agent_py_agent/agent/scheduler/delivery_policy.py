# LLM: 原job/run.delivery为唯一策略，delivery_policy/notify_mode仅入口别名；跨通道必须核owner原认证账及当前ThreadStore绑定。
# 模块用途: 统一通知策略和已验证目标目录；发送前重验绑定，静默只记录内部结果，不以模型地址授权。
from __future__ import annotations

import hashlib
import json

from .schedule import ScheduleValidationError

DELIVERY_SCHEMA = {"type": "object", "properties": {
    "mode": {"type": "string", "enum": ["origin", "local", "none", "bound", "multi"],
             "description": "origin 跟随同通道/new；local/none 仅内部留档；bound/multi只向delivery_targets返回的已认证绑定投递。"},
    "targets": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
        "target_kind": {"enum": ["origin", "local", "channel_binding"]}, "binding_id": {"type": "string"},
        "channel": {"type": "string"}, "chat_id": {"type": "string"}, "account_id": {"type": "string"}},
        "required": ["target_kind"], "additionalProperties": False}},
    "include_origin": {"type": "boolean"},
    "reply_policy": {"type": "string", "enum": ["always", "explicit"],
                     "description": "缺省always保留逐轮回执；origin+explicit使用受管完成协议，只有明确公开意图才通知，无对外事项可内部成功。"}},
    "required": ["mode"], "additionalProperties": False}


# LLM: 这里只核形状不授予目标权；旧mode/reply_policy形状原样保留，新目标另由宿主认证目录核验。
# 函数用途: 校验通知方式、双线开关及有界目标引用，拒绝任意收件人字段。
def normalize_delivery(raw: object) -> dict:
    if raw is None:
        return {"mode": "origin"}
    if not isinstance(raw, dict) or not {'mode'} <= set(raw) <= {'mode', 'reply_policy', 'targets', 'include_origin'}:
        raise ScheduleValidationError("delivery 字段无效，不能指定任意收件人")
    if not isinstance(raw['mode'], str) or raw['mode'] not in {'origin', 'local', 'none', 'bound', 'multi'}:
        raise ScheduleValidationError("delivery.mode 必须为 origin/local/none/bound/multi")
    if 'reply_policy' in raw and (not isinstance(raw['reply_policy'], str) or raw['reply_policy'] not in {'always', 'explicit'}):
        raise ScheduleValidationError("delivery.reply_policy 必须为 always/explicit")
    if 'include_origin' in raw and type(raw['include_origin']) is not bool:
        raise ScheduleValidationError("include_origin 必须为布尔值")
    targets = raw.get('targets', [])
    if not isinstance(targets, list) or len(targets) > 16:
        raise ScheduleValidationError("targets 必须为最多16项的数组")
    for target in targets:
        if (not isinstance(target, dict) or not {'target_kind'} <= set(target) <=
                {'target_kind', 'binding_id', 'channel', 'chat_id', 'account_id'}
                or target.get('target_kind') not in ('origin', 'local', 'channel_binding')
                or any(not isinstance(value, str) or not value or value != value.strip() for value in target.values())
                or (target['target_kind'] == 'channel_binding' and not target.get('binding_id'))
                or (target['target_kind'] != 'channel_binding' and set(target) != {'target_kind'})):
            raise ScheduleDeliveryTargetUnauthorized()
    if raw['mode'] in {'bound', 'multi'} and not targets:
        raise ScheduleValidationError("bound/multi 必须提供已认证目标")
    if raw['mode'] not in {'bound', 'multi'} and (targets or raw.get('include_origin')):
        raise ScheduleValidationError("多目标和include_origin需要bound/multi")
    return {key: raw[key] for key in ('mode', 'reply_policy', 'targets', 'include_origin') if key in raw}


# LLM: 未认证目标是写前/发送前安全拒绝，不能降级origin或借任意chat_id发送。
# 类用途: 给工具与Web保留同一精确投递授权错误码。
class ScheduleDeliveryTargetUnauthorized(ScheduleValidationError):
    error_code = "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED"

    # LLM: 固定公开文案不回显其它owner地址或认证账正文。
    # 函数用途: 构造统一目标未授权错误。
    def __init__(self):
        super().__init__("通知目标不是当前用户已验证的通道绑定。")


# LLM: 入口别名只能产生同一delivery对象；冲突拒绝，不持久化第二套策略或在回执文字中猜mode。
# 函数用途: 兼容delivery_policy、现有delivery及历史notify_mode的显式输入。
def delivery_from_params(params):
    choices = [normalize_delivery(params[key]) for key in ('delivery', 'delivery_policy') if key in params]
    if 'notify_mode' in params:
        choices.append(normalize_delivery({'mode': params['notify_mode']}))
    if choices and any(value != choices[0] for value in choices):
        raise ScheduleValidationError("通知策略参数互相冲突")
    return choices[0] if choices else None


# LLM: owner/home来自已认证Agent；仅列当前精确绑定，管理员IM必须仍有原密码认证代次；引用哈希不授予身份。
# 函数用途: 返回本owner可选的已验证通道目录，过滤旧线程、跨owner和已注销的IM身份。
def available_delivery_targets(agent):
    from ..conversation.channels import supports_transcript_delivery
    from ..user_space.admin_channel_identity import find_admin_channel_identity
    from ..user_space.home_layout import home_paths
    from ..user_space.identity_store import lookup_provider_identity

    home = agent.home_paths
    threads = agent.conversation_store.threads
    rows, errors = threads.list_report(limit=0)
    if errors:
        raise ScheduleDeliveryTargetUnauthorized()
    result = {}
    for thread in rows:
        if thread.owner_id != home.owner_id or thread.owner_home != str(home.owner_home_dir):
            continue
        for binding in thread.channel_bindings:
            current, error = threads.resolve_report(channel=binding.channel,
                channel_user_id=binding.channel_user_id, channel_conversation_id=binding.channel_conversation_id)
            if error or current is None or current.thread_id != thread.thread_id:
                continue
            stamp = None
            local = supports_transcript_delivery(binding.channel) or binding.channel == 'web'
            if not local:
                if home.owner_provider == 'local' and home.owner_kind == 'main':
                    identity = find_admin_channel_identity(home.root, binding.channel, binding.channel_user_id)
                    if identity is None:
                        continue
                    stamp = identity.bound_at
                else:
                    subject = binding.channel_conversation_id if home.owner_kind == 'group' else binding.channel_user_id
                    record = lookup_provider_identity(home_paths(home.root), provider=binding.channel, provider_subject_id=subject)
                    if (record is None or str(home.root / record.owner_home) != str(home.owner_home_dir)
                            or record.owner_kind != home.owner_kind):
                        continue
            identity = [home.owner_id, str(home.owner_home_dir), binding.channel,
                        binding.channel_user_id, binding.channel_conversation_id, stamp]
            key = 'binding-' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
            result[key] = {'binding_id': key, 'channel': binding.channel, 'account_id': binding.channel_user_id,
                'chat_id': binding.channel_conversation_id, 'thread_id': thread.thread_id, 'local': local}
    return [result[key] for key in sorted(result)]


# LLM: 任意附带selector也须与认证引用逐字一致，绑定被撤销/换owner立即拒绝，不按名字或当前聊天补地址。
# 函数用途: 创建、更新及发送前验证每一个目标，保留原引用形状用于冻结运行。
def validate_delivery_targets(agent, raw):
    policy = normalize_delivery(raw)
    targets = policy.get('targets', [])
    if not targets:
        return policy
    available = {row['binding_id']: row for row in available_delivery_targets(agent)}
    for target in targets:
        if target['target_kind'] != 'channel_binding':
            continue
        selected = available.get(target['binding_id'])
        if selected is None or any(key in target and target[key] != selected[key] for key in ('channel', 'account_id', 'chat_id')):
            raise ScheduleDeliveryTargetUnauthorized()
    return policy


# LLM: 冷仓库仍以原配额owner_root和规范身份构造只读认证上下文，不创建Agent、绑定或全局用户。
# 函数用途: 保证绕过模型工具直接写仓库时，也不能保存未认证的多通道目标。
def validate_repository_delivery(repository, raw):
    policy = normalize_delivery(raw)
    if not policy.get('targets'):
        return policy
    from pathlib import Path
    from types import SimpleNamespace

    from ..conversation.store import ConversationStore
    from ..path_access_policy import agent_home_root_for_owner
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent

    owner_home = Path(repository.quota_enforcer.owner_root)
    root = agent_home_root_for_owner(owner_home)
    if root is None:
        raise ScheduleDeliveryTargetUnauthorized()
    identity = OwnerIdentity(repository.owner['provider'], repository.owner['kind'], repository.owner['id'].split('/')[-1])
    owner = resolve_owner_home(root, identity)
    if owner.home_dir != owner_home:
        raise ScheduleDeliveryTargetUnauthorized()
    home = home_paths_with_owner(home_paths(root), owner)
    store = getattr(repository, 'conversation_store', None)
    if store is None:
        paths = resolve_runtime_paths_for_agent(SimpleNamespace(), owner_home, home).paths
        store = ConversationStore(paths['conversation_workspace'], initialize=False)
    agent = SimpleNamespace(home_paths=home, conversation_store=store)
    return validate_delivery_targets(agent, policy)


# LLM: IM 判定只取当前 owner 可信来源绑定；新计划消费显式配置，已有计划和已预留 run 不在这里迁移。
# 函数用途: 为新 IM 计划设置默认内部记录，明确提供的通知策略优先；本地创建保持原回执方式。
def creation_delivery(agent: object, source_thread_id: str, raw: object) -> dict[str, str]:
    if raw is not None:
        return normalize_delivery(raw)
    from ..conversation.channels import supports_transcript_delivery
    from ..conversation.execution_threads import execution_origin_thread

    store = agent.conversation_store
    source = execution_origin_thread(store.threads.require, store.threads.require(source_thread_id))
    if any(not supports_transcript_delivery(binding.channel) for binding in source.channel_bindings):
        return normalize_delivery({"mode": getattr(agent.config, "scheduler_im_default_delivery_mode", "local")})
    return normalize_delivery(None)
