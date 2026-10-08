# LLM: 服务端压缩（官方 Codex 的路线）的共享助手：拿主请求同一份 system/工具/历史、末尾放一个 compaction_trigger 项，
#   让服务端一次压缩并返回不透明的 compaction 项；不生成文字摘要。只在后端声明 supports_remote_compaction 且
#   memory_compact_remote_enabled 开着时可用。超预算先按单次缓存面同一规则（compact_message_source.shrink_tool_outputs）
#   把最早的工具输出换占位；省不够、后端拒绝、没返回压缩项都返回 None，调用方走原客户端压缩链，不进压缩熔断器
#   （中断仍传播）。检查点记录 provider_compaction（schema/protocol/endpoint/model/sha256/item），compact_summary_view
#   用 provider_compaction_compatible 判断当前后端能否读；读不了的后端把该检查点当透明、从归档原文重新压缩。
#   2026-10-08 在 192.168.1.16 上用 sol 真机核实：45k 历史 11–14 秒返回压缩项、输出 152 token、前缀 100% 命中，
#   之后只带压缩项（154 token）+ 提问仍能答出历史细节，链式压缩与 astra 读 sol 的项都可行。
# 模块用途: 让 GPT 订阅模型的压缩从"再发一次摘要请求"变成"服务端一次压缩"，并给检查点/视图一个统一的兼容性判断。
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .compact_calibration import (
    CompactRequestCalibration,
    calibrated_compact_request_tokens,
    raw_token_excess,
)
from .compact_message_source import CompactMessageSource

PROVIDER_COMPACTION_SCHEMA = "provider_compaction.v1"
REMOTE_COMPACTION_PURPOSE = "conversation_compact_remote"
_LOGGER = logging.getLogger(__name__)


# LLM: prompt 必须是没有动态尾巴的稳定前缀（CacheStructuredPrompt 的 volatile_suffix 为空或纯字符串），否则触发项
#   前面会多出一条 user 消息、前缀失配；messages 是主请求同一份 provider 消息数组（含上一代摘要/压缩项）。
# messages可携带同次冻结来源：预算通过前不物化正文，最终Auxiliary运输仍只接完整列表。
# 类用途: 一次服务端压缩请求的材料与身份，允许流式检查大历史预算。
@dataclass(frozen=True)
class RemoteCompactionRequest:
    agent: object
    prompt: object
    messages: list[dict[str, Any]] | CompactMessageSource
    tools: list[dict[str, Any]] | None = None
    system_instruction: str = ""
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    # 主请求的校准事实（本地估算 ÷ 供应商实际）：预算检查按它换算，None 按原始上界。10-08 生产复盘：原始上界把装得下的
    #   请求瘦身，最早的工具输出换了占位，前缀只命中 system + 工具目录。
    calibration: CompactRequestCalibration | None = None


# LLM: 只读结构化事实：配置开关 + 后端能力方法；没有 backend 或方法不存在都算不可用。
# 函数用途: 这次压缩能不能走服务端。
def remote_compaction_enabled(agent: object) -> bool:
    if getattr(getattr(agent, "config", None), "memory_compact_remote_enabled", True) is not True:
        return False
    supports = getattr(getattr(agent, "backend", None), "supports_remote_compaction", None)
    return callable(supports) and bool(supports())


# LLM: 预算与单次缓存面同源（compact_request_budget.compact_cache_surface_budget）；计量按 request.calibration 换算（与
#   compact_request_budget._single_request_source 同一口径）；瘦身后仍超预算返回 None。
#   辅助调用走 generate_auxiliary_model_response（账本、准入、期限、缓存诊断都沿用），用途 conversation_compact_remote。
#   除中断外的任何异常都记一行 warning（只记异常类名）并返回 None，让调用方回退客户端压缩。
# 函数用途: 发一次带压缩触发项的请求，返回检查点要存的 provider_compaction 记录；不可行返回 None。
def request_remote_compaction(request: RemoteCompactionRequest) -> dict[str, Any] | None:
    from ..common.cancellation import ToolCancelled
    from .auxiliary_model_call import AuxiliaryModelCallRequest, generate_auxiliary_model_response

    messages = _messages_within_budget(request)
    if messages is None:
        return None
    try:
        response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(
            agent=request.agent, prompt=request.prompt, messages=messages, tools=request.tools,
            system_instruction=request.system_instruction, request_id=request.request_id, run_id=request.run_id,
            task_id=request.task_id, thread_id=request.thread_id, purpose=REMOTE_COMPACTION_PURPOSE,
            compaction_trigger=True,
        ))
    except (InterruptedError, ToolCancelled):
        raise
    except Exception as exc:  # noqa: BLE001 - 服务端压缩是增强路径，失败形状交给调用方回退客户端压缩
        _LOGGER.warning("remote compaction request failed (%s); falling back to client-side compaction", type(exc).__name__)
        return None
    item = _compaction_item_from_response(response)
    if not item:
        _LOGGER.warning("remote compaction returned no compaction item; falling back to client-side compaction")
        return None
    return provider_compaction_record(getattr(request.agent, "backend", None), item)


# LLM: 和单次缓存面同一预算、同一瘦身规则；瘦身只改交给这次请求的副本。
# 函数用途: 返回装得进窗口的消息数组；装不下返回 None。
def _messages_within_budget(request: RemoteCompactionRequest) -> list[dict[str, Any]] | None:
    from .compact_message_source import (
        CompactMessageSource,
        estimate_compact_payload,
        shrink_tool_outputs,
    )
    from .compact_request_budget import compact_cache_surface_budget

    budget = compact_cache_surface_budget(request.agent)
    source = CompactMessageSource(lambda: iter(request.messages))

    def raw_tokens(value: CompactMessageSource) -> int:
        return estimate_compact_payload({"prompt": request.prompt, "messages": value, "tools": request.tools or [],
                                        "system_instruction": request.system_instruction})

    raw = raw_tokens(source)
    total = calibrated_compact_request_tokens(raw, request.calibration)
    if total <= budget:
        return list(request.messages)
    shrunk = shrink_tool_outputs(source, raw_token_excess(raw, total, budget))
    if shrunk is None or calibrated_compact_request_tokens(raw_tokens(shrunk.source), request.calibration) > budget:
        return None
    return list(shrunk.source)


# 函数用途: 从辅助调用响应里取出第一个服务端压缩项（backends/responses_wire 已清洗），没有返回空字典。
def _compaction_item_from_response(response: object) -> dict[str, Any]:
    for block in getattr(response, "assistant_content_blocks", None) or []:
        if isinstance(block, dict) and block.get("type") == "responses_compaction" and isinstance(block.get("item"), dict):
            return dict(block["item"])
    return {}


# LLM: 记录只含范围（协议 + 端点）、模型名、密文摘要和压缩项本身，不含凭据；sha256 让检查点 id 对内容寻址。
# 函数用途: 生成写进检查点的 provider_compaction 记录。
def provider_compaction_record(backend: object, item: dict[str, Any]) -> dict[str, Any]:
    scope = getattr(backend, "provider_compaction_scope", None)
    content = str(item.get("encrypted_content") or "")
    return {"schema": PROVIDER_COMPACTION_SCHEMA, **(scope() if callable(scope) else {}),
            "model": str(getattr(backend, "model_name", "") or ""),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "item": {"type": "compaction", "encrypted_content": content}}


# LLM: 摘要文本只是给人和不兼容后端看的占位（含密文摘要前 16 位，保证检查点 id 随内容变）；机器判断只读记录字段。
# 函数用途: 服务端压缩检查点在 summary 字段里写的占位文本。
def provider_compaction_marker(record: dict[str, Any]) -> str:
    return (f"[provider-compaction {record.get('protocol', '')} sha256={str(record.get('sha256', ''))[:16]}] "
            "更早的对话已由模型服务端压缩成一个压缩项，由同一后端直接读取；换到其它模型时程序会从归档原文重新压缩。")


# LLM: 只比协议与端点两个结构化字段；后端没有 provider_compaction_scope 方法一律不兼容。
# 函数用途: 当前后端能不能读这条检查点里的压缩项。
def provider_compaction_compatible(backend: object, record: object) -> bool:
    scope = getattr(backend, "provider_compaction_scope", None)
    if not isinstance(record, dict) or not callable(scope):
        return False
    current = scope()
    return all(str(record.get(key) or "") == str(current.get(key) or "") for key in ("protocol", "endpoint"))


# 函数用途: 取记录里的压缩项密文；记录无效返回空串。
def provider_compaction_content(record: object) -> str:
    item = record.get("item") if isinstance(record, dict) else None
    content = item.get("encrypted_content") if isinstance(item, dict) else None
    return content if isinstance(content, str) else ""
