# LLM: 调度通知只读原 job/run 的结构化 delivery，不解析收尾正文；旧记录缺省保留 origin+always，explicit才使用受管公开意图。
# 模块用途: 统一创建、更新、运行快照及投递边界的通知策略，静默仍记录执行结果。
from __future__ import annotations

from .schedule import ScheduleValidationError

DELIVERY_SCHEMA = {"type": "object", "properties": {
    "mode": {"type": "string", "enum": ["origin", "local", "none"],
             "description": "origin 回执发给原布置者；local/none 仅记录内部结果，不逐轮外发。明确提醒、告警和定时报表应设置 origin。"},
    "reply_policy": {"type": "string", "enum": ["always", "explicit"],
                     "description": "缺省always保留逐轮回执；origin+explicit使用受管完成协议，只有明确公开意图才通知，无对外事项可内部成功。"}},
    "required": ["mode"], "additionalProperties": False}


# LLM: 缺省仅用于已发布记录和底层创建 API；非法字段不能吞掉或悄悄改 origin，模型也不能改收件地址。
# 函数用途: 校验通知方式与显式公开意图开关，返回纯字典用于原调度事务和冻结run，旧记录形状不变。
def normalize_delivery(raw: object) -> dict[str, str]:
    if raw is None:
        return {"mode": "origin"}
    if not isinstance(raw, dict) or not {'mode'} <= set(raw) <= {'mode', 'reply_policy'}:
        raise ScheduleValidationError("delivery 只允许 mode 和可选 reply_policy，不能指定其它收件人")
    if not isinstance(raw['mode'], str) or raw['mode'] not in {'origin', 'local', 'none'}:
        raise ScheduleValidationError("delivery.mode 必须为 origin/local/none")
    if 'reply_policy' in raw and (not isinstance(raw['reply_policy'], str) or raw['reply_policy'] not in {'always', 'explicit'}):
        raise ScheduleValidationError("delivery.reply_policy 必须为 always/explicit")
    return {key: raw[key] for key in ('mode', 'reply_policy') if key in raw}


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
