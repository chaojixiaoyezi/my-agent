"""调度执行身份及固定内容合同，历史解析沿发布版本而非当前替代定义。"""
# LLM: job来源沿原ExecutionOrigin/typed actor规范，执行位置与原聊天分开；策略保存在原job并复制到run，不从文本或attrs补身份。
# 模块用途: 统一IM计划的原聊天、后台位置、隔离、模型和预检合同，不保存凭据或另建来源账。
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import asdict, replace

from ..capability.config import CapabilityConfig
from ..capability.runtime_config_reload import capability_config_for_agent
from ..conversation.channels import supports_transcript_delivery
from ..conversation.execution_threads import (
    EXECUTION_ORIGIN_KEY,
    ensure_execution_thread,
    execution_origin_thread,
)
from ..conversation.host_actor import HostActorError, HostChannelActor, resolve_host_actor
from ..runtime_errors import DataCorruptionError
from .schedule import ScheduleValidationError

EXECUTION_DEFAULTS = {"mode": "followup", "model_policy": "inherit", "profile_id": ""}
EXECUTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "followup 沿计划执行会话（新 IM 已是后台）；isolated 每次新建独立上下文。inherit 跟随原来源聊天模型；fixed 固定档案 ID。IM 结果通知原布置者。",
    "properties": {"mode": {"type": "string", "enum": ["followup", "isolated"]},
                   "model_policy": {"type": "string", "enum": ["inherit", "fixed"]},
                   "profile_id": {"type": "string"}},
}
PRECHECK_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "description": "create/update 可直接设置的调度器预检：填 tool_name 和 arguments，在模型调用前执行该只读工具，结果无变化则跳过模型，失败记错。省略或 null 关闭；prompt 仅描述检查有变化后的工作。",
    "properties": {"tool_name": {"type": "string", "description": "当前可用的只读工具名，如 read_file；按本次参数由原权限链校验。"},
                   "arguments": {"type": "object", "description": "该工具的完整调用参数；例如 read_file 使用 {\"path\":\"source.txt\"}。不是 shell 命令或判断条件正文。"},
                   "json_pointer": {"type": ["string", "null"], "description": "比较完整工具输出时省略或填 null，不填空字符串。仅当工具输出本身是 JSON 时才使用 RFC 6901 指针；空字符串也要求 JSON。指针不针对工具读取的文件内容。"}},
    "required": ["tool_name", "arguments"],
}

EXECUTION_TARGET_SCHEMA = {
    'type': ['object', 'null'], 'additionalProperties': False,
    'description': '直接执行 workflow/script，不启动外层模型。省略定义引用时宿主固定当前定义；input_ref.inputs 为冻结 JSON 入参。定义变化须显式更新计划，运行统计不会使引用漂移。',
    'properties': {'kind': {'type': 'string', 'enum': ['workflow', 'script']},
        'asset_id': {'type': 'string'}, 'execution_definition_ref': {'type': 'object'},
        'pinned_version': {'type': 'integer', 'minimum': 1},
        'input_ref': {'type': 'object', 'additionalProperties': False,
            'properties': {'inputs': {'type': 'object'}}, 'required': ['inputs']}},
    'required': ['kind', 'asset_id'],
}


# LLM: 目标只是原资产的结构化绑定，不提供 owner、路径、凭据或 handler；持久化必须有固定内容身份。
# 函数用途: 校验可冻结的执行目标与 JSON 入参，null 明确恢复普通提示词执行。
def normalize_execution_target(value, *, require_reference=True):
    from ..assets import safe_asset_id

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'kind', 'asset_id', 'execution_definition_ref', 'input_ref', 'pinned_version'}:
        raise ScheduleValidationError('execution_target 字段无效')
    kind, identifier = value.get('kind'), value.get('asset_id')
    if kind not in {'workflow', 'script'} or not isinstance(identifier, str):
        raise ScheduleValidationError('execution_target 需要 workflow/script 与准确 asset_id')
    safe_asset_id(identifier)
    reference = value.get('execution_definition_ref')
    if reference is not None:
        if (not isinstance(reference, dict) or set(reference) != {'schema_version', 'kind', 'asset_id', 'definition_sha256'}
                or reference.get('schema_version') != 'scheduler_execution_definition.v1'
                or reference.get('kind') != kind or reference.get('asset_id') != identifier
                or not isinstance(reference.get('definition_sha256'), str)
                or len(reference['definition_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in reference['definition_sha256'])):
            raise ScheduleValidationError('execution_definition_ref 必须是宿主固定的原资产定义引用')
    elif require_reference and 'pinned_version' not in value:
        raise ScheduleValidationError('execution_target 缺少固定 execution_definition_ref')
    inputs = value.get('input_ref', {'inputs': {}})
    if not isinstance(inputs, dict) or set(inputs) != {'inputs'} or not isinstance(inputs['inputs'], dict):
        raise ScheduleValidationError('input_ref 必须提供 inputs JSON 对象')
    try:
        json.dumps(inputs, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ScheduleValidationError('input_ref 必须为有限 JSON') from exc
    pin = value.get('pinned_version')
    if 'pinned_version' in value and (type(pin) is not int or pin < 1):
        raise ScheduleValidationError('pinned_version 必须是正式发布编号')
    return {'kind': kind, 'asset_id': identifier, 'execution_definition_ref': deepcopy(reference), 'input_ref': deepcopy(inputs),
            **({'pinned_version': pin} if pin is not None else {})}


# LLM: 摘要只排除原资产的运行/展示投影和 CAS；代码、步骤、来源、连接声明及执行开关仍属于固定定义。
# 函数用途: 从原领域读取的完整定义计算稳定执行引用，不执行或保存资产。
def execution_definition_ref(kind, asset_id, definition):
    from ..assets.descriptor import execution_definition_ref as script_reference
    from ..assets.workflow_store import execution_definition_ref as workflow_reference
    value = definition.to_dict() if hasattr(definition, 'to_dict') else definition
    digest = workflow_reference(value, kind=kind) if kind == 'workflow' else script_reference(value, kind=kind)
    return {'schema_version': 'scheduler_execution_definition.v1', 'kind': kind,
        'asset_id': asset_id, 'definition_sha256': digest}


# LLM: 只从当前原工具快照的同 owner 领域 Store 读取，不按参数或资产投影重建路径；可用不等于执行授权。
# 函数用途: 固定或复核资产定义引用，返回精确内容快照供同一调用栈消费。
def resolve_execution_target(agent, value, *, runtime=None):
    target = normalize_execution_target(value, require_reference=False)
    if target is None:
        return None, None
    name = 'workflows' if target['kind'] == 'workflow' else 'scripts'
    runtime = runtime or agent.tools.runtime_snapshot(allowed_tools=[name]).runtime(name)
    if runtime is None:
        raise ScheduleValidationError('定时执行目标工具当前不可用')
    tool = runtime.handler
    if getattr(tool, 'owner_home', None) != agent.home_paths.owner_home_dir:
        raise ScheduleValidationError('执行目标 owner 不一致')
    store = tool._store()
    definition = (store.get_workflow(target['asset_id']) if target['kind'] == 'workflow'
                  else store.get_script(target['asset_id']))
    reference = execution_definition_ref(target['kind'], target['asset_id'], definition) if definition is not None else None
    from .pinned_targets import resolve_pinned_target
    return resolve_pinned_target(agent.home_paths.owner_home_dir, target, definition, reference)


# LLM: 旧引用只按旧算法对真实内容重新计算，不能因查不到新索引便接受当前替代定义。
# 函数用途: 为升级前已冻结的调度引用提供字节可核验的兼容比较。
def _legacy_execution_definition_ref(definition):
    value = deepcopy(definition.to_dict() if hasattr(definition, 'to_dict') else definition)
    descriptor = value.get('descriptor', value)
    for key in ('revision', 'runtime_summary', 'used_by', 'entrypoint_path', 'source_check',
                'source_system', 'source_binding', 'last_check_result'):
        descriptor.pop(key, None)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# LLM: 机器分类只读取类型及错误码，文案仅供解释；继承原校验异常保持工具旧错误出口。
# 类用途: 区分目标定义漂移与一般输入失败，不从异常正文推导状态。
class ExecutionTargetError(ScheduleValidationError):
    # LLM: 原错误码不变，额外reason只供结构化展示和验证。
    # 函数用途: 携带固定版本失败的准确分类。
    def __init__(self, message, *, error_code, reason=''):
        super().__init__(message)
        self.error_code = error_code
        self.reason = reason


# LLM: 缺省保持原会话继承行为；固定引用必须是明确档案，不能用可变 default 别名代替固定语义。
# 函数用途: 校验一项计划的执行位置和模型来源，返回可冻结的规范对象。
def normalize_execution(value: object) -> dict:
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - EXECUTION_DEFAULTS.keys():
        raise ScheduleValidationError("execution 含不支持的字段")
    result = {**EXECUTION_DEFAULTS, **value}
    if result["mode"] not in ("followup", "isolated") or result["model_policy"] not in ("inherit", "fixed"):
        raise ScheduleValidationError("execution 的模式或模型策略无效")
    profile = result["profile_id"]
    if not isinstance(profile, str) or profile != profile.strip():
        raise ScheduleValidationError("profile_id 必须是明确档案 ID")
    if (result["model_policy"] == "fixed") != bool(profile) or profile == "default":
        raise ScheduleValidationError("只有 fixed 必须指定非 default 档案 ID")
    return result


# LLM: 写计划前解析引用只做本地配置校验，不发送模型请求；运行时仍重查撤销或权限变化，不能冻结凭据。
# 函数用途: 让不存在或无权使用的固定档案在创建/更新时明确失败，避免保存一项必然不能执行的计划。
def validate_execution_for_agent(agent, value: object) -> dict:
    from ..settings.model_profiles import ModelProfileError, selected_model_config

    execution = normalize_execution(value)
    if execution["model_policy"] == "fixed":
        try:
            selected_model_config(agent, profile_id=execution["profile_id"])
        except ModelProfileError as exc:
            raise ScheduleValidationError("固定模型档案不可用，请选择当前 owner 可用的档案 ID") from exc
    return execution


# LLM: 这里只做数据校验；实际只读、副作用与权限由执行时的同一工具快照裁决，不能凭名字认定安全。
# 函数用途: 校验预检工具和可选 JSON 指针，保留显式 null 关闭语义。
def normalize_precheck(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"tool_name", "arguments", "json_pointer"}:
        raise ScheduleValidationError("precheck 含不支持的字段")
    name, arguments = value.get("tool_name"), value.get("arguments")
    pointer = value.get("json_pointer")
    if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
        raise ScheduleValidationError("precheck 必须指定工具及参数")
    if pointer is not None and (not isinstance(pointer, str) or (pointer and not pointer.startswith("/"))):
        raise ScheduleValidationError("precheck.json_pointer 必须是 JSON 指针")
    if any(str(key).startswith("__") for key in arguments):
        raise ScheduleValidationError("precheck 不允许传入宿主身份参数")
    return {"tool_name": name.strip(), "arguments": dict(arguments),
            **({"json_pointer": pointer} if pointer is not None else {})}


# LLM: 来源只沿原ExecutionOrigin解析并复核当前typed HostActor；不取模型attrs或最新绑定，空本地owner字段保持原旧线程合同。
# 函数用途: 在创建计划时固定原聊天来源，让委派工作位置与模型继承/投递来源分开，不写线程或账本。
def schedule_source_thread_id(agent, thread_id):
    actor = resolve_host_actor(agent, thread_id)
    try:
        source = agent.conversation_store.threads.require(thread_id)
        origin = execution_origin_thread(agent.conversation_store.threads.require, source)
    except (DataCorruptionError, OSError, ValueError, KeyError, TypeError) as exc:
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_INVALID') from exc
    home = getattr(agent, 'home_paths', None)
    owner_id = agent.scheduler_repository.owner['id']
    if home is not None and home.owner_id != owner_id:
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    if any((row.owner_id and row.owner_id != owner_id)
           or (home is not None and row.owner_home and row.owner_home != str(home.owner_home_dir)) for row in (source, origin)):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    if actor is not None and (actor.owner_id != owner_id or actor.source_thread_id != origin.thread_id):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    return origin.thread_id


# LLM: 物理工作线程仍沿原严格来源与creator绑定创建；caller保存的job source必须同用schedule_source_thread_id，不能记录中间work。
# 函数用途: 返回计划实际执行的线程，IM计划后台单飞；关闭开关保持原执行位置，来源身份仍单独规范。
# LLM: 创建与控制共用专有 actor resolver；来源缺失/冲突在写执行线程前拒绝，不取最新群成员。
# 函数用途: 为 IM 监控冻结准确布置人，执行线程不改变 owner、claim 或通知目标。
def schedule_execution_thread(agent: object, source_thread_id: str, operation_key: str) -> str:
    actor = resolve_host_actor(agent, source_thread_id)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    if not config.im_scheduled_tasks_background:
        return source_thread_id
    source, error = agent.conversation_store.threads.load_report(source_thread_id)
    if error is not None or source is None:
        raise DataCorruptionError("定时任务来源会话不可读")
    source = execution_origin_thread(agent.conversation_store.threads.require, source)
    if not any(binding.channel and not supports_transcript_delivery(binding.channel) for binding in source.channel_bindings):
        return source_thread_id
    binding = actor.binding() if actor else None
    return ensure_execution_thread(
        agent, source.thread_id, operation_key or uuid.uuid4().hex, kind="scheduled", source_binding=binding,
    ).thread_id


# LLM: 缺源证明只读原job/run；owner、creator和原source必须一致，且只认确已缺失的来源，不授予通知目标执行身份。
# 函数用途: 拒绝伪造的兜底标记和本地身份降级，保留原计划来源不变。
def _validate_fallback_proof(service, run, marker):
    if (not isinstance(marker, dict) or set(marker) != {'reason', 'original_thread_id', 'fallback_thread_id'}
            or marker['reason'] != 'source_thread_unavailable' or run.get('owner') != service.repository.owner):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    job = service.repository.get_job(str(run['job_id']), include_deleted=True)
    original, error = service.conversation_store.threads.load_report(marker['original_thread_id'])
    if (error or original is not None or job['owner'] != run['owner'] or job.get('creator') != run.get('creator')
            or job['source_thread_id'] != run['source_thread_id']
            or marker['original_thread_id'] != run['source_thread_id']):
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')


# LLM: 缺源不能猜actor；仅原显式目标/带版本接管中当前仍认证、同owner且与原creator逐字相同的绑定可证明身份。
# 函数用途: 选取准确认证来源供原HostActor链使用，不拿最近会话或另一管理员补人。
def _fallback_actor_binding(service, run):
    from .delivery_policy import (
        _repository_delivery_agent,
        available_delivery_targets,
        validate_delivery_targets,
    )

    creator = run.get('creator')
    if not isinstance(creator, dict) or set(creator) != {'channel', 'actor_id'}:
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_INVALID')
    agent = _repository_delivery_agent(service.repository)
    policy = validate_delivery_targets(agent, run.get('delivery'))
    rows = {row['binding_id']: row for row in available_delivery_targets(agent)}
    source, selected = None, None
    for spec in policy.get('targets', []):
        row = rows.get(spec.get('binding_id')) if spec.get('target_kind') == 'channel_binding' else None
        if (row is not None and row['channel'] == creator['channel'] == 'feishu'
                and row['account_id'] == creator['actor_id']):
            source = service.conversation_store.threads.require(row['thread_id'])
            selected = row
            break
    notification = run.get('notification_binding')
    if source is None and notification is not None:
        from .notification_binding import bound_notification_thread

        binding = notification['binding']
        if binding['channel'] == creator['channel'] == 'feishu' and binding['actor_id'] == creator['actor_id']:
            source = bound_notification_thread(agent, notification,
                follow_latest_channel_session=policy.get('follow_latest_channel_session', True))
            selected = {'channel': binding['channel'], 'account_id': binding['actor_id'], 'chat_id': binding['conversation_id']}
    if (source is None or source.owner_id != run['owner']['id']
            or source.owner_home != str(service.repository.quota_enforcer.owner_root)
            or EXECUTION_ORIGIN_KEY in source.metadata):
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')
    binding = next((row for row in source.channel_bindings if row.channel == selected['channel']
        and row.channel_user_id == selected['account_id'] and row.channel_conversation_id == selected['chat_id']), None)
    if binding is None:
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')
    return source, asdict(binding)


# LLM: 只给同owner原scheduler专用线程追加一次不可变ExecutionOrigin；creator来自原run，目标先经原认证门，冲突拒绝。
# 函数用途: 为缺源IM运行固定可恢复的真实身份，不新增TaskLink或修改原job/历史。
def freeze_fallback_actor(service, run, marker):
    _validate_fallback_proof(service, run, marker)
    if run.get('creator') in (None, {'local': True}):
        return []
    source, binding = _fallback_actor_binding(service, run)
    reference = {'source_thread_id': source.thread_id, 'kind': 'scheduled',
        'operation_key': f"fallback:{run['job_id']}", 'binding': {
            'channel': binding['channel'], 'conversation_id': binding['channel_conversation_id'],
            'actor_id': binding['channel_user_id']}}

    # LLM: 原线程事务保留全部可变字段，不覆盖已有不同来源；scheduler绑定必须仍为准确job的专用地址。
    # 函数用途: 原子固定兜底来源，重试只能确认同一身份。
    def freeze(current):
        if (current.thread_id != marker['fallback_thread_id'] or current.thread_id == source.thread_id
                or current.owner_id != run['owner']['id']
                or current.owner_home != str(service.repository.quota_enforcer.owner_root)
                or not current.channel_bindings or any(row.channel != 'scheduler'
                    or row.channel_conversation_id != f"scheduler-job:{run['job_id']}" for row in current.channel_bindings)):
            raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
        existing = current.metadata.get(EXECUTION_ORIGIN_KEY)
        if existing is not None and existing != reference:
            raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')
        return replace(current, metadata={**current.metadata, EXECUTION_ORIGIN_KEY: reference})

    service.conversation_store.threads.update_atomic(marker['fallback_thread_id'], freeze)
    return [binding]


# LLM: 已准备的run仍重验目标认证、准确fallback线程及冻结三元组；不再require已删除的job工作线程，也不绕过HostActor。
# 函数用途: 从经认证的不可变兜底来源恢复原布置者，供原TaskLink和后续敏感操作复核。
def _scheduled_fallback_actor(service, run, identity):
    marker = run['thread_fallback']
    thread = service.conversation_store.threads.require(run['thread_id'])
    source, binding = _fallback_actor_binding(service, run)
    reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
    expected = {'channel': binding['channel'], 'conversation_id': binding['channel_conversation_id'],
        'actor_id': binding['channel_user_id']}
    if (run['thread_id'] != marker['fallback_thread_id'] or run['origin_thread_id'] != thread.thread_id
            or thread.owner_id != run['owner']['id'] or thread.owner_home != str(service.repository.quota_enforcer.owner_root)
            or not isinstance(reference, dict) or reference.get('kind') != 'scheduled'
            or reference.get('source_thread_id') != source.thread_id or reference.get('binding') != expected
            or reference.get('operation_key') != f"fallback:{run['job_id']}"
            or identity != (expected['channel'], expected['conversation_id'], expected['actor_id'])
            or execution_origin_thread(service.conversation_store.threads.require, thread).thread_id != source.thread_id):
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_CONFLICT')
    return HostChannelActor.from_record({'owner_id': run['owner']['id'], 'source_thread_id': source.thread_id,
        'entry_request_id': reference['operation_key'], **expected})


# LLM: 运行身份取原creator/owner及冻结origin_bindings；缺源须认证兜底证明，普通路径仍核原job immutable来源，不补最新群成员。
# 函数用途: 为新定时主根准备宿主 actor，供隔离线程和原 TaskLink 同源使用；本机和旧无证据任务保持 None。
def scheduled_run_actor(service, run):
    creator = run.get("creator")
    if run.get('thread_fallback') is not None:
        _validate_fallback_proof(service, run, run['thread_fallback'])
    if creator in (None, {"local": True}):
        return None
    if (not isinstance(creator, dict) or set(creator) != {"channel", "actor_id"}
            or run.get("owner") != service.repository.owner):
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    context = run.get("execution_context") or {}
    if context.get("origin_thread_id") != run.get("origin_thread_id"):
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    bindings = context.get("origin_bindings")
    if not isinstance(bindings, list):
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    matching = [row for row in bindings if isinstance(row, dict)
                and row.get("channel") == creator["channel"] and row.get("channel_user_id") == creator["actor_id"]]
    identities = {(row.get("channel"), row.get("channel_conversation_id", ""), row.get("channel_user_id")) for row in matching}
    if len(identities) != 1:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    channel, conversation, actor_id = identities.pop()
    if run.get('thread_fallback') is not None:
        return _scheduled_fallback_actor(service, run, (channel, conversation, actor_id))
    job = service.repository.get_job(str(run["job_id"]), include_deleted=True)
    thread = service.conversation_store.threads.require(job["thread_id"])
    reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
    entry = str(run["run_id"])
    if reference is not None:
        source = execution_origin_thread(service.conversation_store.threads.require, thread)
        if (source.thread_id != run["origin_thread_id"] or reference["binding"] != {
                "channel": channel, "conversation_id": conversation, "actor_id": actor_id}):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
        entry = reference["operation_key"]
    return HostChannelActor.from_record({"owner_id": run["owner"]["id"], "source_thread_id": run["origin_thread_id"],
        "entry_request_id": entry, "channel": channel, "conversation_id": conversation, "actor_id": actor_id})


# LLM: 隔离线程复用原 execution_origin schema，binding 只来自已冻结的 run 与准确 creator；不把 source 最新绑定抄成身份。
# 函数用途: 给新 isolated IM 线程固定原来源，后续模型、孩子和审批复用原身份解析，不产生第二份身份账。
def scheduled_execution_metadata(service, run):
    actor = scheduled_run_actor(service, run)
    return ({EXECUTION_ORIGIN_KEY: {"source_thread_id": actor.source_thread_id, "kind": "scheduled",
        "operation_key": actor.entry_request_id, "binding": actor.binding()}} if actor is not None else {})


# LLM: 只迁移尚未开工且与原 run 精确匹配的旧 isolated 线程；来源由冻结 creator/context 提供，原线程锁内再复核，不升级未知或活跃身份。
# 函数用途: 新包继续已排队的旧隔离任务，补原 immutable 来源而保留 cwd、模型、Compact 和全部业务字段。
def migrate_queued_execution_origin(service, run, seed):
    reference = seed.metadata.get(EXECUTION_ORIGIN_KEY)
    if reference is None:
        return
    with service.conversation_store.tasks.transition_guard(str(run["run_id"])):
        _migrate_queued_origin_locked(service, run, seed, reference)


# LLM: 调用方持原根锁，再用 repository 原 quota/store mutation scope 读取 run 并写线程；不写 scheduler store，claim/start 不能插入复核与线程写之间。
# 函数用途: 在原根和原账本事务内迁移一条旧 queued 来源，已领取/开工或冻结上下文变化均拒绝。
def _migrate_queued_origin_locked(service, run, seed, reference):
    store = service.conversation_store.threads
    old, error = store.load_report(seed.thread_id)
    if error:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    if old is None or old.metadata.get(EXECUTION_ORIGIN_KEY) is not None:
        return
    identity = {key: value for key, value in seed.metadata.items() if key != EXECUTION_ORIGIN_KEY}

    # LLM: 原 ThreadStore 事务内逐项核对旧身份；不覆盖已有来源，不改可变设置和正文，不跨 owner 写入。
    # 函数用途: 给准确旧 queued 线程追加其已知原来源，竞争或已有异源记录明确拒绝。
    def migrate(current):
        if (current.thread_id != seed.thread_id or current.owner_id != seed.owner_id
                or current.owner_home != seed.owner_home or current.channel_bindings
                or any(current.metadata.get(key) != value for key, value in identity.items())):
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        existing = current.metadata.get(EXECUTION_ORIGIN_KEY)
        if existing is not None and existing != reference:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
        return replace(current, metadata={**current.metadata, EXECUTION_ORIGIN_KEY: reference})

    with service.repository._mutation_scope() as (ledger, _admission):
        latest = service.repository._require_run(ledger, str(run["run_id"]))
        if latest["status"] != "queued" or float(latest.get("started_at") or 0) != 0:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
        if any(latest.get(key) != run.get(key) for key in ("owner", "thread_id", "creator", "execution_context")):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
        store.update_atomic(seed.thread_id, migrate)
