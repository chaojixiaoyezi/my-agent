# LLM: 每项计划的显式执行配置保存在原 job，预留时复制到 run；预检只认结构化配置。指针 null 与省略均比较原输出，schema 必须与规范化一致。
# 模块用途: 统一 IM 计划后台归属、每次隔离、固定模型和预检校验；配置保存在原 job，不保存凭据或展开模型。
from __future__ import annotations

import uuid

from ..capability.config import CapabilityConfig
from ..capability.runtime_config_reload import capability_config_for_agent
from ..conversation.channels import supports_transcript_delivery
from ..conversation.execution_threads import (
    EXECUTION_ORIGIN_KEY,
    ensure_execution_thread,
    execution_origin_thread,
)
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


# LLM: 通道只取可信源线程绑定，空/本地绑定不因正文自称 IM 被改路由；写线程的幂等键由原工具操作生成。
# 函数用途: 返回计划实际执行的线程，IM 计划各自后台单飞，关开关或本地创建保持原线程。
# LLM: 群规则来源使用冻结可信 actor，执行线程不改变 origin 或 monitor owner。
# 函数用途: 群规则来源使用冻结可信 actor，执行线程不改变 origin 或 monitor owner。 修改需同步原控制、恢复或权限测试。
def schedule_execution_thread(agent: object, source_thread_id: str, operation_key: str) -> str:
    config = capability_config_for_agent(agent) or CapabilityConfig()
    if not config.im_scheduled_tasks_background:
        return source_thread_id
    source, error = agent.conversation_store.threads.load_report(source_thread_id)
    if error is not None or source is None:
        raise DataCorruptionError("定时任务来源会话不可读")
    binding = (source.metadata.get(EXECUTION_ORIGIN_KEY) or {}).get("binding")
    source = execution_origin_thread(agent.conversation_store.threads.require, source)
    if not any(binding.channel and not supports_transcript_delivery(binding.channel) for binding in source.channel_bindings):
        return source_thread_id
    current = getattr(agent, "_current_run_params", None)
    actor = (getattr(current, "task_attributes", None) or {}).get("host_channel_actor") or {}
    if actor.get("actor_id") and actor.get("channel") and actor.get("conversation_id"):
        binding = {key: actor[key] for key in ("channel", "conversation_id", "actor_id")}
    return ensure_execution_thread(
        agent, source.thread_id, operation_key or uuid.uuid4().hex, kind="scheduled", source_binding=binding,
    ).thread_id
