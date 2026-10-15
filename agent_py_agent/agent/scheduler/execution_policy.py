# LLM: job来源沿原ExecutionOrigin/typed actor规范，执行位置与原聊天分开；策略保存在原job并复制到run，不从文本或attrs补身份。
# 模块用途: 统一IM计划的原聊天、后台位置、隔离、模型和预检合同，不保存凭据或另建来源账。
from __future__ import annotations

import uuid
from dataclasses import replace

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


# LLM: 运行身份只取原 run 复制的 creator、owner 和首次冻结的 origin_bindings；job 的 immutable 来源仅核对，不从群最新发言者补人。
# 函数用途: 为新定时主根准备宿主 actor，供隔离线程和原 TaskLink 同源使用；本机和旧无证据任务保持 None。
def scheduled_run_actor(service, run):
    creator = run.get("creator")
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
