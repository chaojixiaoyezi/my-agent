# LLM: 创建范围只读原会话绑定和能力配置，不解析用户要求；隔离线程仍写原 ThreadStore。
# 模块用途: IM 定时工作独立后台运行，管理员 TUI 保留原执行方式。
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


# LLM: 通道只取可信源线程绑定，空/本地绑定不因正文自称 IM 被改路由；写线程的幂等键由原工具操作生成。
# 函数用途: 返回计划实际执行的线程，IM 计划各自后台单飞，关开关或本地创建保持原线程。
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
    return ensure_execution_thread(
        agent, source.thread_id, operation_key or uuid.uuid4().hex, kind="scheduled", source_binding=binding,
    ).thread_id
