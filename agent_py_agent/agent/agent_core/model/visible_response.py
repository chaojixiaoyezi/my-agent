# LLM: 公开提交只接受同run已采纳response和原sealed调用；不在生成完成前投递，不从候选/正文授予执行或续跑，联测取消与拒绝。
# 模块用途: 在原工具边界或最终收口，把模型可见正文交给现有显示接收器，不另开模型请求或投递账。
from __future__ import annotations

from collections.abc import Callable

from ...backends.assistant_messages import final_response_text, response_messages
from ...common.cancellation import CancellationToken
from ...concurrency.interrupt import is_interrupted
from .response_provenance import response_provenance_error, response_provenance_projection


# LLM: 同一sealed来源门供core identity和显示默认投影；拒绝/宿主转换保持当前text权威，不恢复rawblocks或靠投影改状态。
# 函数用途: 提供已采纳响应的最终阶段正文，渲染方式由内部结果或公开提交调用方明确传入。
def accepted_final_text(params: object, response: object, *, project: Callable[[str], str] | None = None) -> str:
    if response_provenance_error(params, response, purposes=("main", "natural_user_reply", "background_receipt_repair")):
        return str(getattr(response, "text", "") or "")
    return final_response_text(response, project=project)


# LLM: 只能由原已验工具轮和最终收口调用；源、状态和完整text一致性各守自己的门，sink失败不改执行结果。
# 函数用途: 将同次已采纳响应提交到既有正文出口，支持流式候选校正及非流式正文。
def commit_visible_response(params: object, response: object, *, boundary: str) -> None:
    token = getattr(params, "cancellation_token", None)
    if (boundary not in {"tools", "terminal"} or is_interrupted()
            or (isinstance(token, CancellationToken) and token.cancelled)
            or str(getattr(response, "runtime_status", "ok") or "ok") != "ok"
            or not str(getattr(response, "text", "") or "")):
        return
    if response_provenance_error(params, response, purposes=("main", "natural_user_reply", "background_receipt_repair")):
        return
    sink = getattr(params, "effective_on_chunk", None) or getattr(params, "on_chunk", None)
    commit = getattr(sink, "commit_model_response", None)
    if not callable(commit):
        return
    payload = {"schema": "assistant_messages.v1", "source": response_provenance_projection(response),
               "boundary": boundary, "messages": response_messages(response), "final_text": accepted_final_text(params, response)}
    try:
        commit(payload)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
