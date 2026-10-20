# LLM: 正式本地提交只经原认证HTTP/ask让宿主冻结owner；ID与callback先于effect，unknown只留同ID，不向文件队列fallback。
# 模块用途: 运输完整原输入与执行快照；受理未知立即保ID结束本地等待，既不构造Agent，也不签host owner/route或改宿主业务状态。
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from typing import TYPE_CHECKING

from .client_credentials import gateway_client_credentials
from .input_identity import http_input_identity
from .io import gateway_response_path, new_gateway_request_id

if TYPE_CHECKING:
    from .request_client import GatewayAskParams


# LLM: 此异常只说明客户端运输受理未确认，request_id仍原幂等身份；不能倒写宿主任务失败、取消、生成新ID或自动重投。
# 类用途: 让原CLI/TUI错误出口明确结束本地等待，同时保留用户查询原请求的编号。
class GatewayAskSubmissionUnconfirmed(TimeoutError):
    error_code = "INPUT_DISPOSITION_UNCONFIRMED"

    # LLM: 只保存既有请求身份与固定原因码，不保存HTTP正文/凭据；原TUI prepare_error/finally负责结束本地spinner。
    # 函数用途: 告诉用户这次提交是否已被宿主受理仍不确定，应保留ID查询而不要重发。
    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(
            f"Gateway受理未确认，请勿重发；请用原请求ID查询：{request_id}。这不表示宿主任务失败或未受理。"
        )


# LLM: raw来源必须与原typed任务解析完全一致；display_text和客户端system_task不能绕过服务端重解析。
# 函数用途: 核原始用户输入运输事实，缺slash来源时明确拒绝而非静默丢任务属性。
def _original_ask_prompt(params: GatewayAskParams) -> str:
    from .request_client import _normalized_gateway_prompt

    raw = params.original_prompt if params.original_prompt is not None else params.prompt
    if not isinstance(raw, str):
        raise ValueError("Gateway原始输入必须是文字。")
    prompt, actual = _normalized_gateway_prompt(replace(params, prompt=raw, system_task=None))
    expected_prompt, _ = _normalized_gateway_prompt(params)
    expected_task = dict(params.system_task or {})
    if prompt != expected_prompt or (expected_task and actual != expected_task):
        raise ValueError("Gateway任务命令缺少匹配的原始输入，请重新提交原命令。")
    return raw.strip()


# LLM: 仅沿原params通道/会话/附件快照建立HTTP body；queue是明确新回合意图，权限、归属与实际C准备均留宿主。
# 函数用途: 复用原ID公式预分配并先登记显示；只在原同ID排队回执确认后返回，未知明确收口而不无限等待/重送。
def submit_authenticated_ask(paths, params, submit_request, on_request_allocated):
    from .request_client import (
        GatewayAskExecutionOptions,
        _gateway_conversation_payload,
        _gateway_identity_metadata,
        gateway_request_workspace_payload,
    )

    prompt = _original_ask_prompt(params)
    conversation = _gateway_conversation_payload(params)
    user_id = str(params.canonical_user_id or params.channel_user_id)
    channel = conversation["channel"]
    message_id = str(params.client_message_id or new_gateway_request_id())
    request_id, _ = http_input_identity(
        user_id=user_id,
        channel=channel,
        conversation_id=conversation["channel_conversation_id"],
        message_id=message_id,
    )
    metadata = {**_gateway_identity_metadata(params, conversation), "message_id": message_id}
    options = GatewayAskExecutionOptions(
        inject=tuple(params.inject or ()),
        prompt_files=tuple(params.prompt_files or ()),
        save=params.save,
        include_prompt=params.include_prompt,
        resume_context=params.resume_context,
        tool_approval=params.interactive_approvals,
        rich_transcript=params.rich_transcript,
        input_media=params.input_media,
    )
    body = {
        "prompt": prompt,
        "user_id": user_id,
        "channel": channel,
        "conversation_id": conversation["channel_conversation_id"],
        "metadata": metadata,
        "delivery_intent": "queue",
        **options.to_payload(),
    }
    workspace = gateway_request_workspace_payload(params.workspace_root, params.workspace_roots)
    if workspace:
        body["workspace"] = workspace
    if on_request_allocated is not None:
        on_request_allocated(request_id)
    status, result = submit_request(body)
    if 400 <= status < 500 and status not in {401, 403}:
        raise ValueError(
            f"Gateway拒绝本次输入（HTTP {status}），请检查输入或服务入口；未改走文件队列。"
        )
    if status in {401, 403}:
        raise PermissionError("Gateway未授权本次输入，请检查原客户端凭据。")
    if isinstance(result, dict) and result.get("request_id") and result["request_id"] != request_id:
        raise ValueError("Gateway返回的请求身份不一致，不能接续其它任务。")
    if not (
        200 <= status < 300
        and isinstance(result, dict)
        and result.get("request_id") == request_id
        and result.get("delivery_status") == "accepted"
        and result.get("input_state") == "queued"
        and result.get("disposition") == "queued"
    ):
        raise GatewayAskSubmissionUnconfirmed(request_id)
    return (
        request_id,
        paths.inbox / (request_id + ".json"),
        gateway_response_path(paths, request_id),
    )


# LLM: 同原G1 reader加载宿主凭据，原typed用户/群header随body发送；没有客户端owner猜测、网络重试或无认证降级。
# 函数用途: 只向明确本地Gateway端口执行一次POST，响应未知返回原结构三态中的未知运输事实。
def post_gateway_ask(agent, payload):
    port = int(getattr(agent.config, "gateway_port", 0) or 0)
    if port <= 0:
        raise ValueError("本地Gateway端口未配置，未发送请求。")
    headers = {
        "Content-Type": "application/json",
        "X-User-Id": str(payload["user_id"]),
        "X-Channel": str(payload["channel"]),
        **gateway_client_credentials(agent).headers(),
    }
    metadata = payload["metadata"]
    if metadata.get("channel_chat_type") and metadata.get("channel_chat_id"):
        headers["X-Channel-Chat-Type"] = metadata["channel_chat_type"]
        headers["X-Channel-Chat-Id"] = metadata["channel_chat_id"]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ask",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    timeout = min(30.0, max(0.1, float(getattr(agent.config, "gateway_request_timeout", 30))))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return int(response.status), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            body = {}
        return error.code, body if isinstance(body, dict) else {}
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            raise ConnectionError("Gateway连接被拒绝，请确认原服务已启动；请求未发送。") from None
        return 0, {}
    except ConnectionRefusedError:
        raise ConnectionError("Gateway连接被拒绝，请确认原服务已启动；请求未发送。") from None
    except (OSError, UnicodeError, ValueError):
        return 0, {}
