# LLM: 此模块独占 HTTP 后端公共传输、能力探针和目录元数据；保持请求局部预算、取消与配置错误合同。
# 模块用途: 为模型协议提供统一网络入口和有界工具探测；修改时联合后端、OAuth 和传输测试。
from __future__ import annotations

import secrets
import threading
from typing import Any

from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice
from .base import BackendOptions, BaseBackend, _utc_now_iso
from .errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
)
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .provider_headers import endpoint_parts, request_headers

# 探针只缓存成功证据；短暂失败允许之后重试，但单次探测有界。
_PROBE_MAX_ATTEMPTS = 3


# LLM: HTTP 协议共同使用此请求级限额；不能热改实例配置，需同步 Chat/Messages/Responses 的摘要调用测试。
# 函数用途: 将本轮输出上限限制在配置范围内，未覆盖时保留配置值。
def bounded_output_tokens(configured: int, requested: int | None) -> int:
    if requested is None:
        return configured
    return max(1, min(configured, int(requested)))


# LLM: HTTP 请求控制只在显式给出时透传；流入口与测试替身须保持既有调用合同，不在此重试。
# 函数用途: 调用流式传输入口，按需附加当前请求的首事件等待预算。
def request_stream_lines(
    lines,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    first_event_timeout_seconds: float | None,
):
    if first_event_timeout_seconds is None:
        return lines(path, payload, headers)
    return lines(
        path,
        payload,
        headers,
        first_event_timeout_seconds=first_event_timeout_seconds,
    )


# LLM: 所有内置 HTTP backend 都必须把 system_instruction 映射到供应商真实高优先级字段，而非拼回 user prompt。
# 类用途: 共享真实模型 HTTP、能力探针、连接和 metadata 逻辑，并声明支持独立 system 指令。
# LLM: HTTP 主链统一应用连接和会话头；新的协议必须复用同一传输、取消与超时语义。
# 类用途: 为模型接口提供 HTTP 请求、能力探测和上下文目录读取。
class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    supports_system_instructions = True
    supports_provider_request_options = True

    # LLM: 复制可变选项并校验 top_p；多线程仅用 request-local 身份，不能热改共享后端采样或头部。
    # 函数用途: 初始化可复用后端和媒体预算，非法采样在发送 HTTP 之前报错。
    def __init__(
        self,
        options: BackendOptions,
    ):
        self.api_base = str(options.api_base).rstrip("/")
        self.api_key = str(options.api_key)
        self.model_name = str(options.model_name)
        self.request_timeout = int(options.request_timeout)
        self.connect_timeout = float(options.connect_timeout)
        self.max_tokens = int(options.max_tokens)
        self.custom_headers = dict(options.custom_headers)
        self.session_header = options.session_header
        # 本地配置与供应商事实分开保存；不能再把 fallback 冒充 provider metadata。
        self.configured_context_window_tokens = int(options.context_window_tokens or 0)
        # 兼容既有只读调用方；context-window resolver 会识别 configured_ 字段，绝不把本属性
        # 当成 provider 事实。新代码应读取 configured_context_window_tokens。
        self.context_window_tokens = self.configured_context_window_tokens
        self._provider_tool_capability_cache: ProviderToolCapability | None = None
        self._provider_tool_capability_lock = threading.Lock()
        self._provider_context_window_cache: int | None = None
        self.model_metadata: dict[str, Any] = {}
        self.temperature = float(options.temperature)
        self.temperature_explicit = options.temperature_explicit
        from .sampling import validate_top_p

        self.top_p = validate_top_p(options.top_p)
        # 思考控制方式随 profile 冻结；档位由每次请求的 ProviderRequestOptions.reasoning_effort 传入。
        self.reasoning_control = str(options.reasoning_control or "none")
        # 结构化输出方式随 profile 冻结；只有 OpenAI Chat 兼容接口会用到 json_object。
        self.structured_output = str(options.structured_output or "native")
        self.stream_enabled = bool(options.stream_enabled)
        self.prompt_cache_enabled = bool(options.prompt_cache_enabled)
        self.input_media_max_bytes = max(1, int(options.input_media_max_bytes))
        # Streaming HTTP transports enforce request_timeout as an SSE idle
        # timeout.  The tool-loop guard therefore must not also reinterpret it
        # as a total wall-clock limit while valid events keep arriving.
        self.stream_timeout_is_idle = self.stream_enabled

    # LLM: Serialize provider probes per backend instance and cache only proven-positive facts.
    # 函数用途: 并发冷启动共用一次有界协议探测，失败不缓存，避免暂时失败永久阻断后续任务。
    def probe_tool_capability(self) -> ProviderToolCapability:
        """有界探测原生工具能力；只缓存成功事实，失败允许之后重新探测。"""
        cached = self._provider_tool_capability_cache
        if cached is not None:
            return cached
        # 同一 Gateway/owner 的 Agent 会被多个请求线程复用。探针必须 single-flight，
        # 否则冷启动并发会重复烧请求，并让一个成功、一个失败的结果互相打架。
        with self._provider_tool_capability_lock:
            cached = self._provider_tool_capability_cache
            if cached is not None:
                return cached
            return self._probe_tool_capability_uncached()

    # LLM: Caller holds `_provider_tool_capability_lock`; probe the ordinary auto tool-choice
    # surface, not forced-call or reasoning-disable support. Only a matching nonce proves support.
    # 函数用途: 用普通工具选择发起有界探测并缓存阳性结果，不执行工具；避免思考模型被强制参数挡住。
    def _probe_tool_capability_uncached(self) -> ProviderToolCapability:
        # 缺凭据属于配置错误，须在协议能力判定之前暴露给调用方。
        if hasattr(self, "api_key") and not str(self.api_key or "").strip():
            raise ValueError(
                "api_key 为空：无法执行工具能力探针（检查 AGENT_API_KEY 环境变量或配置）"
            )
        evidence = ""
        for _attempt in range(1, _PROBE_MAX_ATTEMPTS + 1):
            nonce = secrets.token_hex(8)
            probe_tool = {
                "name": "my_agent_capability_probe",
                "description": "Internal protocol capability probe with no host-side effect.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "nonce": {
                            "type": "string",
                            "description": "Copy the nonce from the user request exactly.",
                        }
                    },
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            }
            try:
                response = self.generate(
                    (
                        "Call my_agent_capability_probe exactly once with nonce "
                        f"{nonce}. Do not answer in prose."
                    ),
                    tools=[probe_tool],
                    tool_choice=ToolChoice.auto("native_capability_probe"),
                )
            except (ProviderRecoverableError, ProviderConfigurationError):
                # 保留供应商失败分类，让调用方处理配置、额度和网络，不能改成能力不足。
                raise
            except Exception as exc:
                # 本地确定性异常(解析/配置错)重试无意义, 立即落 evidence。
                code = str(getattr(exc, "error_code", "") or "").strip().upper()
                suffix = f":{code}" if code else ""
                evidence = f"live_probe_failed:{type(exc).__name__}{suffix}"
                break
            blocks = list(getattr(response, "tool_use_blocks", None) or ())
            supported = any(
                isinstance(block, dict)
                and str(block.get("id") or "").strip()
                and str(block.get("name") or "") == "my_agent_capability_probe"
                and isinstance(block.get("input"), dict)
                and str(block["input"].get("nonce") or "") == nonce
                for block in blocks
            )
            if supported:
                capability = ProviderToolCapability(
                    provider=str(self.name or type(self).__name__),
                    endpoint=self._tool_endpoint(),
                    model=self.model_name,
                    stream=self.stream_enabled,
                    native_supported=True,
                    evidence="live_probe_returned_structured_tool_call",
                    observed_at=_utc_now_iso(),
                )
                self._provider_tool_capability_cache = capability
                return capability
            evidence = "live_probe_returned_no_valid_structured_tool_call"
        # 重试耗尽仍未证明: 不缓存(下次 turn 再探), 如实报不支持。
        return ProviderToolCapability(
            provider=str(self.name or type(self).__name__),
            endpoint=self._tool_endpoint(),
            model=self.model_name,
            stream=self.stream_enabled,
            native_supported=False,
            evidence=evidence or "live_probe_returned_no_valid_structured_tool_call",
            observed_at=_utc_now_iso(),
        )

    # LLM: HTTP 能力证据应标记真实协议端点；具体适配器覆盖路径，不能用展示名称替代地址。
    # 函数用途: 提供通用后端的探针端点标识，不发网络请求。
    def _tool_endpoint(self) -> str:
        return self.api_base

    # LLM: HTTP JSON 请求必须经过唯一 GatewayRequest 和共享传输；OAuth 覆盖认证封装，需联合取消与认证回归。
    # 函数用途: 检查凭据并发送一次非流式请求，返回供应商 JSON，错误沿原传输合同上抛。
    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Send a JSON request through the shared gateway helper."""

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(self._gateway_request(path, payload, headers))

    # LLM: Streaming collection forwards request-local first-event budget without changing backend-wide idle configuration.
    # 函数用途: 发起并完整收集一条流式 provider 请求，供非增量调用方使用。
    def request_stream(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        first_event_timeout_seconds: float | None = None,
    ) -> list[str]:
        """Send a streaming request and collect all data lines."""
        return post_stream(
            self._gateway_request(
                path,
                payload,
                headers,
                first_event_timeout_seconds=first_event_timeout_seconds,
            )
        )

    # LLM: The iterator preserves provider data order and per-request first-event timing for live consumers.
    # 函数用途: 发起流式请求并逐条交付 data，使 TUI 能实时显示正文、思考和工具参数进度。
    def request_stream_iter(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        first_event_timeout_seconds: float | None = None,
    ):
        """Send a streaming request and yield data lines as they arrive."""
        yield from post_stream_iter(
            self._gateway_request(
                path,
                payload,
                headers,
                first_event_timeout_seconds=first_event_timeout_seconds,
            )
        )

    # LLM: 唯一传输封装应用显式请求头和宿主会话；后台预算与首包预算保持 request-local，不改共享配置。
    # 函数用途: 组装统一 HTTP 请求，把已授权后台预算传到底层；普通流式空闲/取消语义不变。
    def _gateway_request(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        first_event_timeout_seconds: float | None = None,
    ) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        from .request_scope import provider_request_timeout

        api_base, path = endpoint_parts(self.api_base, path)
        return GatewayRequest(
            api_base=api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=request_headers(headers, self.custom_headers, self.session_header),
            timeout=provider_request_timeout(self.request_timeout),
            connect_timeout=self.connect_timeout,
            first_event_timeout=first_event_timeout_seconds,
        )

    # LLM: 供应商上下文窗口只从模型 metadata API 的结构化字段读取；失败或字段缺失返回 0，
    #   由上层使用本地配置兜底。不得根据模型名在这里硬编码容量。
    # 函数用途: 请求并缓存供应商公开目录的容量信息，避免 Compact 每轮重复发起网络探测。
    def provider_context_window_tokens(self) -> int:
        """发现并缓存供应商模型目录公开的上下文窗口。"""
        if self._provider_context_window_cache is None:
            metadata = discover_provider_model_metadata(
                ProviderMetadataOptions(
                    api_base=self.api_base,
                    api_key=self.api_key,
                    model_name=self.model_name,
                    request_timeout=self.request_timeout,
                    connect_timeout=self.connect_timeout,
                    custom_headers=request_headers({}, self.custom_headers, self.session_header),
                )
            )
            self.model_metadata = metadata.record
            self._provider_context_window_cache = metadata.context_window_tokens
        return self._provider_context_window_cache
