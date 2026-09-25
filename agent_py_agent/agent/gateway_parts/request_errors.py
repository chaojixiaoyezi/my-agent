# LLM: 客户端只接收结构化错误码对应的安全文案；保留模型截断事实，不公开内部异常或把思考当正文。
# 模块用途: 统一 Gateway 请求失败与输入附件错误，区分模型连接、长度限制、会话读写和执行结果未确认。
from __future__ import annotations

"""Shared gateway request error response builders."""

import time
from collections.abc import Mapping
from pathlib import Path

from ..backends.errors import ProviderRequestRejectedError, provider_error_http_status
from ..runtime_errors import runtime_error_report
from ..turn_end import result_turn_end_reason


class ConversationPersistenceError(RuntimeError):
    """结构化会话无法可靠读取或写入时，阻止无上下文继续回答。"""

    error_code = "CONVERSATION_PERSISTENCE_UNAVAILABLE"


# LLM: 任务绑定冲突与历史文件读写故障分别报告；不据此清空历史或自动重放旧操作。
# 类用途: 表示会话任务与执行身份不一致，提供可定位的错误码而不归咎用户聊天记录。
class ConversationTaskBindingError(RuntimeError):
    error_code = "CONVERSATION_TASK_BINDING_CONFLICT"


# LLM: 仅精确恢复返回 operation_outcome_uncertain 时使用；不能靠异常文本推断或扩大到所有持久化错误。
# 类用途: 表示上一轮操作结果未核清，恢复被阻止，避免界面误导用户反复重发任务。
class ActiveTurnOutcomeUncertainError(ConversationPersistenceError):
    error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"


class UserReplyUnavailableError(RuntimeError):
    """The model reply phase ended without any text safe for user delivery."""

    error_code = "USER_REPLY_UNAVAILABLE"


class SystemCommandRoutingError(RuntimeError):
    """A slash command reached a model execution queue instead of the control plane."""

    error_code = "SYSTEM_COMMAND_ROUTING_ERROR"


# LLM: 客户端错误仅按结构化 error_code 映射；未配置模型引导 /model（IM 里可用编号选择共享模型），不虚构默认模型或泄露异常原文。
# COMPACT_REQUEST_NON_TEXT 先于通用 COMPACT_ 前缀匹配：它现在表示"无法摘要的非文本内容"（unknown 块，或 compact_media_policy=off 时的图片），
# 文案要点明这一点并给出可行动的出路；已知图片在 auto/archived_refs 下会走归档引用，不再报这个码。
# COMPACT_VISION_SUMMARY_FAILED 同样先于前缀匹配：随图摘要本次失败，同代次下一次压缩自动改走归档引用，文案说明不必换模型。
# ACTIVE_TURN_OUTCOME_UNCERTAIN 与 RUN_RECOVERY_REQUIRED 都是执行结果未确认的人工恢复点：文案只指向 /recover，重试不会自行解除。
# 函数用途: 区分尚未配置、压缩、截断、持久化与请求拒绝，不把未知 400 归咎密钥或建议不安全重放。
def gateway_client_error_message(error_code: object) -> str:
    code = str(error_code or "").strip().upper()
    if code == "COMPACT_REQUEST_NON_TEXT":
        return (
            "会话里有无法摘要的非文本内容（compact_media_policy 为 off 时图片也算），上下文无法压缩，已超出模型可用窗口，本轮没有继续执行；"
            "原始历史和任务文件仍保留。可切换更大上下文的模型后继续原会话，或新开会话继续。"
        )
    if code == "COMPACT_VISION_SUMMARY_FAILED":
        return (
            "本次随图摘要未完成（预算、供应商窗口、媒体拒绝或截断），原始历史和附件仍保留，本轮没有继续执行；"
            "下一次压缩会自动把旧图降级为归档引用后重试，不需要换模型。"
        )
    if code.startswith("COMPACT_"):
        return (
            "上下文压缩未完成，原始历史和任务文件仍保留，本轮没有继续执行。"
            "请查看压缩诊断；切换更大上下文的模型后可继续原会话。"
        )
    messages = {
        "MODEL_NOT_CONFIGURED": (
            "尚未配置模型。请发送 /model 查看并选择可用模型；新增模型需在终端 TUI 的 /model 里操作。"
            "系统不会自动使用其它模型。"
        ),
        "MODEL_RESPONSE_TRUNCATED": (
            "本次模型输出达到上限，尚未形成完整正文；已有工具操作和历史保留。"
        ),
        "MODEL_TOOL_ARGUMENTS_INVALID": (
            "模型返回的工具调用参数不完整或格式无效，本次调用未执行，本轮已停止；已有工具操作和历史保留。"
        ),
        "MODEL_STREAM_INCOMPLETE": "模型响应流未完整结束，本轮已停止；已有工具操作和历史保留。",
        "MODEL_RESPONSE_CONTENT_FILTERED": "模型服务过滤了本次响应，本轮已停止；已有工具操作和历史保留。",
        "PROVIDER_REQUEST_REJECTED": (
            "模型服务拒绝了本次请求，本轮已停止。具体原因请查看请求诊断；此前已执行的工作不会因此撤销。"
        ),
        "PROVIDER_CONFIGURATION_INVALID": (
            "模型服务配置不可用。请检查接口地址、模型名称和密钥是否属于同一服务后重试。"
        ),
        "PROVIDER_CONNECTION_FAILED": "无法连接模型服务，请检查网络、代理和接口地址后重试。",
        "TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE": (
            "模型工具能力检查未通过，当前任务尚未开始。请检查模型是否支持原生工具调用。"
        ),
        "PROVIDER_QUOTA_EXHAUSTED": "模型服务当前没有可用额度，请补充额度或切换可用配置后重试。",
        "PROVIDERTRANSIENTERROR": "模型服务暂时不可用，系统已停止本轮请求，请稍后重试。",
        "PROVIDERTIMEOUTERROR": "模型服务响应超时，系统已停止本轮请求，请稍后重试。",
        "PROVIDERCONNECTIONERROR": "无法连接模型服务，请检查网络、代理和接口地址后重试。",
        "GATEWAY_WORKSPACE_INVALID": (
            "当前工作目录不可用，任务没有开始。请确认目录存在且重新从该目录启动客户端。"
        ),
        "ACTIVE_TURN_OUTCOME_UNCERTAIN": (
            "上一轮有操作已发起，但执行结果尚不能确认。为避免重复写入或重复启动，"
            "已停止自动恢复；请输入 /recover 查看未确认的操作，核对后选择处置，不要直接重做整个任务。"
        ),
        "RUN_RECOVERY_REQUIRED": (
            "上一轮执行结果尚未确认，本会话已暂停自动执行，重试不会自行解除。"
            "请输入 /recover 查看未确认的操作，核对后选择处置。"
        ),
        "CONVERSATION_PERSISTENCE_UNAVAILABLE": (
            "当前会话记录无法可靠读取或保存，本轮已停止，避免在缺少上下文时继续执行。"
            "请查看运行诊断后再恢复。"
        ),
        "INPUT_MEDIA_INVALID": "附件无效、已改变或超过限制，请重新添加图片/视频。",
        "CONVERSATION_TASK_BINDING_CONFLICT": (
            "当前任务的状态与执行绑定不一致，本轮未继续；不是聊天记录丢失。"
            "请核对运行诊断；确认旧执行已经停止后，可用 /goal resume 显式恢复目标。"
        ),
    }
    if code in messages:
        return messages[code]
    if code.startswith("MODEL_"):
        return "模型本轮响应未完成，具体原因请查看请求诊断；已有工具操作和历史保留。"
    return "任务处理失败，请稍后重试；如持续失败，请查看运行诊断。"


# LLM: 只投影宿主 typed provider error 或空正文截断；显式完成优先，不解析正文/思考、不修改结果或启动重试。
# 函数用途: 防止模型有半句正文就掩盖坏工具参数等错误；保留本轮真实技术终态，不否认已有工作。
def gateway_model_response_error_projection(result: object) -> dict:
    field = result.get if isinstance(result, Mapping) else lambda key: getattr(result, key, "")
    status, code = field("runtime_status"), str(field("runtime_reason") or "")
    end = result_turn_end_reason(result)
    empty_truncated = (
        not str(field("response") or "").strip() and status == "unfinished"
        and code == "MODEL_RESPONSE_TRUNCATED" and end == "max-tokens"
    )
    provider_error = (
        status == "error" and end == "error" and code.startswith("MODEL_")
        and field("runtime_source") == "model_provider"
    )
    if not (empty_truncated or provider_error):
        return {}
    return {
        "ok": False,
        "status": "failed",
        "error_code": code,
        "turn_end_reason": end,
        "user_error": gateway_client_error_message(code),
    }


# LLM: 仅 typed 请求拒绝追加来源和 HTTP 白名单；不公开 details/body/headers，也不凭文本把主请求归咎子代理。
# 函数用途: 给当前请求失败补上可见状态码，让用户定位这一轮，未知原因仍保持未知。
def gateway_provider_error_projection(exc: BaseException) -> dict:
    if not isinstance(exc, ProviderRequestRejectedError):
        return {}
    status = provider_error_http_status(exc)
    message = gateway_client_error_message(exc.error_code)
    projection = {"error_origin": "model_provider", "user_error": message}
    if status is not None:
        projection["http_status"] = status
        projection["user_error"] = f"模型请求 HTTP {status}：{message}"
    return projection


def gateway_request_load_error_response(
    request_path: Path,
    load_error: dict,
    *,
    started_at: float | None = None,
) -> dict:
    request_id = str(request_path.stem)
    started_at = time.time() if started_at is None else started_at
    return {
        "id": request_id,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_REQUEST_LOAD_ERROR",
        "error": str(load_error.get("message") or "gateway request file could not be read"),
        "request_load_error": load_error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": 0,
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


# LLM: The queue filename is the canonical request id. This builder exposes payload-id corruption
# as a structured terminal failure and never echoes the conflicting id as a response authority.
# 函数用途: 请求文件名和正文 ID 冲突时生成失败答复，阻止它串到别的回合或调用模型。
def gateway_request_identity_error_response(
    request_path: Path,
    request: dict,
    identity_error: dict,
    *,
    request_id: str,
) -> dict:
    now = time.time()
    return {
        "id": request_id,
        "kind": str(request.get("kind") or "unknown"),
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_REQUEST_IDENTITY_CONFLICT",
        "error": str(
            identity_error.get("message")
            or "gateway request filename and payload identity conflict"
        ),
        "request_identity_error": dict(identity_error),
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": int(request.get("attempts") or 0),
        "lease_owner": str(request.get("lease_owner") or ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


def gateway_response_load_error_response(
    response_path: Path,
    load_error: dict,
    *,
    request_id: str | None = None,
) -> dict:
    return {
        "id": request_id or response_path.stem,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": 0,
        "ended_at": time.time(),
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_RESPONSE_LOAD_ERROR",
        "error": str(load_error.get("message") or "gateway response file could not be read"),
        "response_load_error": load_error,
        "response_file": str(response_path),
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
    }


def gateway_request_processing_state_error_response(
    request_path: Path,
    processing_error: dict,
    *,
    request_id: str,
    started_at: float | None = None,
) -> dict:
    started_at = time.time() if started_at is None else started_at
    return {
        "id": request_id,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": started_at,
        "ended_at": time.time(),
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_REQUEST_PROCESSING_STATE_WRITE_ERROR",
        "error": str(processing_error.get("message") or "gateway request processing state could not be written"),
        "processing_state_error": processing_error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": 0,
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


def gateway_owner_scope_error_response(
    request_path: Path,
    request: dict,
    error: BaseException,
    *,
    request_id: str,
) -> dict:
    """远程请求无法建立隔离 owner 时的终态响应；不得留 processing 重试或共享回退。"""
    now = time.time()
    error_code = str(getattr(error, "error_code", "") or "OWNER_SCOPE_UNAVAILABLE")
    return {
        "id": request_id,
        "kind": str(request.get("kind") or "ask"),
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0,
        "response": "",
        "error_code": error_code,
        "error": f"{type(error).__name__}: {error}",
        "user_error": gateway_client_error_message(error_code),
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": int(request.get("attempts") or 0),
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


def gateway_unhandled_worker_error_response(
    request_path: Path,
    request: dict,
    error: BaseException,
    *,
    request_id: str,
) -> dict:
    """Build a terminal response when a worker fails outside the turn handler.

    Normal model/tool failures are handled by ``_handle_gateway_request``.  This
    boundary covers earlier failures such as thread-local agent construction;
    leaving those requests in ``processing`` would make status and recovery lie
    until the lease timeout expires.
    """

    now = time.time()
    try:
        started_at = float(request.get("lease_started_at") or now)
    except (TypeError, ValueError):
        started_at = now
    report = runtime_error_report(error, context="gateway.worker.unhandled")
    error_code = str(
        getattr(error, "error_code", "") or "GATEWAY_WORKER_UNHANDLED_ERROR"
    )
    return {
        "id": request_id,
        "kind": str(request.get("kind") or "ask"),
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(max(0.0, now - started_at), 3),
        "response": "",
        "error_code": error_code,
        "error": f"{type(error).__name__}: {error}",
        "user_error": gateway_client_error_message(error_code),
        "worker_error": report,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": int(request.get("attempts") or 0),
        "lease_owner": str(request.get("lease_owner") or ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }
