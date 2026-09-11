# LLM: 模型协议在此统一转换；采样和思考控制按配置/精确协议，不改共享后端、身份或其它供应商字段。
# 模块用途: 发送采样配置与模型请求，规范化文本、工具、思考和用量；须回归流式与非流式调用。
from __future__ import annotations

"""模型后端适配层。

这一层的作用很像'翻译器'：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..prompting_parts.cache_layout import prompt_cache_layout
from ..settings.defaults import DEFAULT_MODEL_MAX_TOKENS
from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice
from .anthropic_prompt_cache import (
    anthropic_messages_with_optional_cache,
    anthropic_prompt_cache_projection,
)
from .errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderResponseError,
)
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .provider_headers import endpoint_parts, request_headers
from .response_completion import (
    incomplete_response_fields,
    truncated_tool_names,
    without_tool_blocks,
)
from .stream_parsers import StreamCompletion
from .usage_metadata import (
    collect_anthropic_stream_with_completion,
    collect_openai_stream_with_completion,
    openai_stream_payload,
    usage_dict,
)


def _bounded_output_tokens(configured: int, requested: int | None) -> int:
    if requested is None:
        return configured
    return max(1, min(configured, int(requested)))


# LLM: Request-local stream controls are forwarded only when present so legacy/fake stream callables keep their established signature.
# 函数用途: 调用真实或测试流入口；只有本轮确有独立首包预算时才附加新参数。
def _request_stream_lines(
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


# LLM: 这是所有模型后端返回给运行时的唯一响应合同；新增内部历史字段时必须保证不会进入用户可见正文，并同步原生工具循环测试。
# 类用途: 统一保存模型正文、用量、工具调用和厂商原生历史块，供后续工具轮继续使用。
@dataclass
class ModelResponse:
    """Normalized model response returned to the agent runtime."""

    text: str
    backend: str
    runtime_status: str = "ok"
    runtime_reason: str = ""
    runtime_source: str = ""
    # LLM: 仅由宿主运行时写入 宿主协议 turn/end.reason；模型正文和验收结果不得设置。
    # 字段用途: 让主代理、子代理、Gateway 与 TUI 使用同一个本轮结束原因。
    turn_end_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    # 原生 tool_use 协议(tool_protocol=native)下，从结构化响应抽出的工具调用块，
    # 每块形如 {"id","name","input"}。文本协议下恒为空，不影响现有行为。
    tool_use_blocks: list[dict[str, Any]] = field(default_factory=list)
    # Anthropic Messages 返回的 assistant content blocks，经输入白名单清洗后保留原顺序。
    # 它主要供下一轮原生消息回放；只有显式 rich transcript sink 会另行投影 type=thinking 的正文，
    # signature/redacted_thinking/tool_use 仍不得进入用户可见事件。
    assistant_content_blocks: list[dict[str, Any]] = field(default_factory=list)
    # 原生响应不完整时为 True：工具整轮零执行，具体长度/断流/坏参数原因由上述 typed 终态区分。
    # 流式与非流式共用；不能把这个布尔单独当成输出上限或自动重试依据。
    truncated: bool = False
    # 上游返回的原始 stop_reason；EOF 没有该字段时保持空，不推断成 max_tokens。
    stop_reason: str = ""
    # 不完整响应里被丢弃工具调用的工具名（去重、保序）。只作结构化事实：整轮仍零工具执行，
    # 但工具循环要凭它判断"这次截断的是哪个工具"，才能给出分块写等有界恢复，而不是静默当无事发生。
    # 没有可识别工具名时保持空，调用方不得据此推测工具或伪造调用。
    truncated_tool_names: list[str] = field(default_factory=list)
    # 传输/流边界在形成完整响应前发现的结构化工具协议错误。这里只能由宿主适配层写入；
    # 模型正文不能设置该字段，也不能借此制造可执行 ToolCall。
    tool_protocol_violations: list[dict[str, str]] = field(default_factory=list)


# LLM: 后端连接与 top_p/温度是工作片冻结快照；请求头和模型名/密钥一起缓存，流式请求中不可热改。
# 类用途: 保存 HTTP 模型后端的连接、采样和兼容选项。
@dataclass(frozen=True)
class BackendOptions:
    """Connection and generation options shared by HTTP model backends."""

    api_base: str
    api_key: str
    model_name: str
    request_timeout: int = 240
    connect_timeout: float = 10.0
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    context_window_tokens: int = 0
    temperature: float = 0.2
    temperature_explicit: bool = False
    stream_enabled: bool = True
    prompt_cache_enabled: bool = True
    custom_headers: dict[str, str] = field(default_factory=dict)
    session_header: str = ""
    top_p: float | None = None


# LLM: OpenAI 请求对象按 typed 字段保留 system 和请求级思考控制；子适配器只发送其协议支持的字段。
# 类用途: 汇总一次 OpenAI-compatible 调用的规则、用户输入、工具、思考开关和输出格式选项。
@dataclass(frozen=True)
class _OpenAIGenerateRequest:
    prompt: str
    system_instruction: str = ""
    on_chunk: Callable[[str], None] | None = None
    on_thinking_delta: Callable[[str], None] | None = None
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: ToolChoice | None = None
    messages: list[dict[str, Any]] | None = None
    response_schema: dict[str, Any] | None = None
    json_object: bool = False
    thinking_disabled: bool = False
    max_output_tokens: int | None = None
    first_event_timeout_seconds: float | None = None


# LLM: 供应商级请求控制集中在 typed options；首包预算必须保持 request-local，不能通过修改共享 backend 传递。
# 类用途: 携带一次模型请求的宿主 system 指令、思考开关和首个流式事件等待预算，不混入用户正文或工具历史。
@dataclass(frozen=True)
class ProviderRequestOptions:
    system_instruction: str = ""
    thinking_disabled: bool = False
    first_event_timeout_seconds: float | None = None


# 原生工具能力探针的最大尝试次数(模型偶发未按探针要求调用工具而回散文,
# 单发误判"不支持 native"; 有界重试后仍无结构化调用才判不支持)。
_PROBE_MAX_ATTEMPTS = 3


# LLM: 后端 capability flag 决定上层能否传真实 system instruction；未声明支持的旧实现保持原关键字形态。
# 类用途: 定义所有模型后端共用接口，并明确哪些供应商适配器能承载高优先级宿主规则。
class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"
    # 只有确实能从 provider 流中取得结构化工具参数 delta 的后端才开启；
    # 上层据此避免给第三方/fake backend 传入未知关键字。
    supports_tool_input_progress = False
    # 只有能在 provider content block stop 处给出思考终态的后端才开启；
    # 上层据此保证终态先于后续正文，并避免给旧 backend 传入未知关键字。
    supports_thinking_completion = False
    # 只有把宿主规则送进供应商真实 system/developer 通道的后端才能开启；上层据此避免给旧 fake 传新参数。
    supports_system_instructions = False
    # 只有理解 ProviderRequestOptions 的后端才能开启；旧 fake 不接收新请求控制对象。
    supports_provider_request_options = False

    def provider_context_window_tokens(self) -> int:
        """Return provider-advertised context capacity, or zero when unavailable."""
        return 0

    # LLM: The base implementation declares no native transport; HTTP subclasses own live probing.
    # 函数用途: 为没有原生工具协议的后端返回明确的不支持事实，不发网络请求。
    def probe_tool_capability(self) -> ProviderToolCapability:
        """Return an explicit unsupported fact for backends without native tools."""

        return ProviderToolCapability(
            provider=str(self.name or "base"),
            endpoint=f"local://{self.name or 'base'}",
            model=str(getattr(self, "model_name", "") or ""),
            stream=bool(getattr(self, "stream_enabled", False)),
            native_supported=False,
            evidence="backend_declares_no_native_tool_transport",
            observed_at=_utc_now_iso(),
        )

    # LLM: Base signature 用 typed request options 承载供应商控制；具体 backend 未显式声明 capability 时，
    # 上层不得传入该对象或可选 tool-input callback。
    # 函数用途: 定义所有模型后端共用的生成入口、高优先级宿主规则和可选流式观察器。
    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ModelResponse:
        """Generate one assistant response for the supplied prompt.

        ``request_options.system_instruction`` carries stable host guidance in
        the provider's real high-priority instruction channel. It must not be
        folded into the user prompt by an HTTP backend that declares support.

        ``tools`` carries the run-fixed provider schema for native tool use.
        A backend without the selected native capability must fail closed;
        protocol selection happens before the run and never falls back here.

        ``messages`` carries a provider-native structured conversation (native
        tool_use IR translated to Anthropic ``messages``). When supplied it
        replaces the single ``{"role":"user","content":prompt}`` turn; ``prompt``
        remains the original user turn while the system instruction stays separate.
        Text-protocol callers omit it.

        ``on_thinking_delta`` may be a callable observer that also exposes a
        ``complete(text)`` method. ``on_tool_input_progress`` is a separate typed
        display observer. Neither changes model history or tool execution.
        """
        raise NotImplementedError

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate a JSON-shaped response, with prompt-only fallback by default.

        Provider adapters that support a native output schema override this method.
        Keeping the capability separate from ``generate`` preserves compatibility with
        third-party/test backends whose existing method signature is intentionally small.
        """

        del response_schema
        return self.generate(prompt, messages=messages)

    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate one JSON object, with prompt-only fallback by default."""

        del max_tokens
        return self.generate(prompt, messages=messages)


class EchoBackend(BaseBackend):
    """Local deterministic backend used by tests and dry development."""

    name = "echo"

    def probe_tool_capability(self) -> ProviderToolCapability:
        # EXEC-31b(text 删除后): echo 是本地测试后端, 工具循环在本地解析,
        # 声明 native 支持(native 是唯一协议, 不再有 text 降级)。
        return ProviderToolCapability(
            provider=self.name,
            endpoint=f"local://{self.name}",
            model=str(getattr(self, "model_name", "") or ""),
            stream=False,
            native_supported=True,
            evidence="echo_backend_declares_native",
            observed_at=_utc_now_iso(),
        )

    # LLM: Echo 不执行真实 provider role 映射；新增参数只为保持公开接口兼容，不能假装完成 system 验证。
    # 函数用途: 用确定性本地回复支撑测试，并安全忽略真实模型才会消费的 system/tool 参数。
    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ModelResponse:
        del request_options, tools, tool_choice, messages, on_thinking_delta
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        if "# User Task" in prompt:
            task = prompt.split("# User Task", 1)[-1]
            task = task.split("\n# ", 1)[0].strip()
        else:
            task = lines[-1] if lines else "空任务"

        summary = task[:300]
        text = (
            "这是 echo 后端的本地响应。\n"
            "我已接收任务，并基于当前 prompt 和记忆生成结构化结果。\n\n"
            f"任务摘要：{summary}\n\n"
            "建议步骤：\n"
            "1. 明确目标。\n"
            "2. 检查已有记忆。\n"
            "3. 必要时拆分 subagent。\n"
            "4. 输出可验证结果。"
        )
        return ModelResponse(text=text, backend=self.name)


# LLM: 所有内置 HTTP backend 都必须把 system_instruction 映射到供应商真实高优先级字段，而非拼回 user prompt。
# 类用途: 共享真实模型 HTTP、能力探针、连接和 metadata 逻辑，并声明支持独立 system 指令。
# LLM: HTTP 主链统一应用连接和会话头；新的协议必须复用同一传输、取消与超时语义。
# 类用途: 为模型接口提供 HTTP 请求、能力探测和上下文目录读取。
class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    supports_system_instructions = True
    supports_provider_request_options = True

    # LLM: 复制可变选项并校验 top_p；多线程仅用 request-local 身份，不能热改共享后端采样或头部。
    # 函数用途: 初始化可安全复用的后端，非法采样在发送 HTTP 之前报错。
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
        self.stream_enabled = bool(options.stream_enabled)
        self.prompt_cache_enabled = bool(options.prompt_cache_enabled)
        # Streaming HTTP transports enforce request_timeout as an SSE idle
        # timeout.  The tool-loop guard therefore must not also reinterpret it
        # as a total wall-clock limit while valid events keep arriving.
        self.stream_timeout_is_idle = self.stream_enabled

    # LLM: Serialize provider probes per backend instance and cache only proven-positive facts.
    # 函数用途: 并发冷启动共用一次有界协议探测，失败不缓存，避免暂时失败永久阻断后续任务。
    def probe_tool_capability(self) -> ProviderToolCapability:
        """Run a harmless structured-call probe (bounded retries) and cache only
        a proven-positive result.

        真机 2026-08-17 MiniMax: 探针单发时灵时不灵(模型偶尔无视
        工具调用要求而回散文)→ 单发探针把"没调用工具"误判成"模型不
        支持 native", 聊天直接报 ToolProtocolSelectionError。修: ①同一
        探针最多重试 _PROBE_MAX_ATTEMPTS 次(每次新 nonce), 一次结构化
        调用即通过; ②失败不缓存——进程存活期间下一轮再探(弱模型抖动
        不永久判死), 只有成功结果进缓存。本地确定性异常(非 provider)
        不重试, 立即落 evidence。
        """
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
        # 2026-08-17 真机: gateway 进程环境缺 AGENT_API_KEY → generate 抛
        # ValueError("api_key 为空") 被探针 except 吞成"不支持 native",
        # 用户看到误导性 ToolProtocolSelectionError。配置错误≠模型能力,
        # 先结构化检查 key 并原样上抛(调用方显示真实原因)。
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
                # EXEC-41b: provider 侧失败(429 额度/限流/超时/连接)不是"不支持
                # native"——探针吞掉它会把配额耗尽误报成"模型无原生工具能力",
                # 真机 2026-08-17 双线 quota 恢复前 resume 报
                # ToolProtocolSelectionError(RC=1 且文案误导)。服务端明确 4xx 拒绝同样
                # 属于 provider/configuration 事实，必须原样放行，不能降级成能力不足。
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

    def _tool_endpoint(self) -> str:
        return self.api_base

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

    # LLM: 唯一传输封装应用显式请求头和宿主会话；认证不被覆盖，URL 不重复 /v1，首包预算保持 request-local。
    # 函数用途: 组装统一的 HTTP 请求，不改变流式空闲/取消/重试行为。
    def _gateway_request(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        first_event_timeout_seconds: float | None = None,
    ) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        api_base, path = endpoint_parts(self.api_base, path)
        return GatewayRequest(
            api_base=api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=request_headers(headers, self.custom_headers, self.session_header),
            timeout=self.request_timeout,
            connect_timeout=self.connect_timeout,
            first_event_timeout=first_event_timeout_seconds,
        )

    # LLM: 供应商上下文窗口只从模型 metadata API 的结构化字段读取；失败或字段缺失返回 0，
    #   由上层使用本地配置兜底。不得根据模型名在这里硬编码容量。
    # 人类: 每个 backend 实例只探测一次，避免 compact 每轮重复访问 /models。
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


class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"
    # OpenAI-compatible chat 流没有统一 block-stop，但 reasoning_content 在首段正文或
    # 流结束时有确定边界；适配器会在该边界调用同一 typed complete observer。
    supports_thinking_completion = True
    supports_tool_input_progress = True

    def _tool_endpoint(self) -> str:
        return self.api_base + "/chat/completions"

    # LLM: OpenAI-compatible 传输必须保持 system -> 规范会话/当前 user -> 原生工具历史的追加顺序；
    # reasoning 和工具参数计数是展示边界；请求开关与观察器必须转交组包器，不能丢失或影响执行。
    # 函数用途: 调用 chat/completions，传递宿主规则、本次思考开关和流式展示，不改变后续请求。
    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ModelResponse:
        """Call the OpenAI-compatible chat completion endpoint."""
        provider_options = request_options or ProviderRequestOptions()
        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                system_instruction=provider_options.system_instruction,
                on_chunk=on_chunk,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
                thinking_disabled=provider_options.thinking_disabled,
                first_event_timeout_seconds=provider_options.first_event_timeout_seconds,
            )
        )

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Use the provider-native strict JSON-schema response format."""

        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                messages=messages,
                response_schema=response_schema,
            )
        )

    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Use the provider-native JSON-object response format."""

        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                messages=messages,
                json_object=True,
                max_output_tokens=max_tokens,
            )
        )

    # LLM: 角色与思考控制来自 typed request；精确端点/型号可采用采样默认，显式温度保留，
    # 已知 DeepSeek/Zen 出站补空 reasoning 不修改 canonical 或声称找回思考。
    # 函数用途: 组装 Chat 的采样、历史与工具请求；不改会话编号或添加失败重试。
    def _generate(self, request: _OpenAIGenerateRequest) -> ModelResponse:
        from .sampling import chat_sampling_fields

        payload = {
            "model": self.model_name,
            "max_tokens": _bounded_output_tokens(self.max_tokens, request.max_output_tokens),
        }
        payload.update(chat_sampling_fields(self.api_base, self.model_name, top_p=self.top_p,
            temperature=self.temperature, temperature_explicit=self.temperature_explicit,
            thinking_disabled=request.thinking_disabled))
        if request.thinking_disabled:
            # 参考 轻量运行时 的 thinkingFormat 适配：官方与 Zen/Go 都默认开思考，强制工具探针须显式关闭；
            # 不是通用 OpenAI 字段，因此只在已核对端点的分支里写入。
            _disable_thinking_for(payload, api_base=self.api_base)
        if request.messages is not None:
            payload["messages"] = _openai_messages_from_native(
                request.messages,
                initial_user_prompt=request.prompt,
                system_instruction=request.system_instruction,
            )
        else:
            payload["messages"] = _openai_messages_from_native(
                [],
                initial_user_prompt=request.prompt,
                system_instruction=request.system_instruction,
            )
        tools = _tools_for_choice(request.tools, request.tool_choice)
        if tools:
            from .tool_protocol_adapter import openai_tool_choice

            payload["tools"] = _openai_tools_from_native(tools)
            payload["tool_choice"] = openai_tool_choice(request.tool_choice or ToolChoice.auto())
            if _requires_thinking_disabled(
                payload["messages"], api_base=self.api_base, model_name=self.model_name,
            ):
                _disable_thinking_for(payload, api_base=self.api_base)
        elif request.tool_choice is not None and request.tool_choice.mode == "none":
            payload["tool_choice"] = "none"
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "my_agent_structured_output",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        elif request.json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.stream_enabled:
            return self._generate_stream(
                payload,
                headers,
                on_chunk=request.on_chunk,
                on_thinking_delta=request.on_thinking_delta,
                on_tool_input_progress=request.on_tool_input_progress,
                first_event_timeout_seconds=request.first_event_timeout_seconds,
            )
        obj = self.request_json("/chat/completions", payload, headers)
        return _openai_non_stream_response(
            obj,
            backend_name=self.name,
            tools_requested=bool(tools),
        )

    # LLM: reasoning_content 与正文必须走不同观察器，并在正文/工具或流结束前封口思考块；
    # 工具参数开始立即封口思考并发布计数；不完整响应零工具执行且保留 typed 终态与可归档部分。
    # 函数用途: 解析 OpenAI SSE，同时展示正文、思考与长工具参数的实时准备进度。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        first_event_timeout_seconds: float | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
    ) -> ModelResponse:
        """Parse OpenAI SSE and concatenate delta.content chunks."""
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        text, usage, blocks, completion = collect_openai_stream_with_completion(
            _request_stream_lines(
                lines,
                "/chat/completions",
                openai_stream_payload(payload),
                headers,
                first_event_timeout_seconds,
            ),
            on_chunk=on_chunk,
            on_thinking_delta=on_thinking_delta,
            on_tool_input_progress=on_tool_input_progress,
        )
        assistant_blocks = _openai_assistant_content_blocks(
            reasoning=_openai_reasoning_from_completion(completion),
            text=text,
            tool_blocks=blocks,
        )
        incomplete = incomplete_response_fields(completion.stop_reason, incomplete_reason=completion.incomplete_reason)
        if incomplete:
            return ModelResponse(
                text=text,
                backend=self.name,
                usage=usage,
                tool_use_blocks=[],
                assistant_content_blocks=without_tool_blocks(assistant_blocks),
                truncated_tool_names=truncated_tool_names(completion.tool_names),
                **incomplete,
            )
        if not text and not blocks and payload.get("tools"):
            raise ProviderResponseError(
                "OpenAI-compatible 流式响应没有文本或工具调用",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            assistant_content_blocks=assistant_blocks,
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )


# LLM: 保留 reasoning_content 的显式存在性；坏参数/截断整轮零执行，思考与正文仍可归档。
# 函数用途: 解析非流式回复，生成和 SSE 相同的正文、工具及原生思考历史，供跨轮回放。
def _openai_non_stream_response(
    obj: dict[str, Any],
    *,
    backend_name: str,
    tools_requested: bool,
) -> ModelResponse:
    try:
        choice = obj["choices"][0]
        message = choice["message"]
        if not any(
            field in message for field in ("content", "tool_calls", "reasoning_content")
        ):
            raise ValueError("message has neither content, reasoning, nor tool_calls")
        text = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "") if "reasoning_content" in message else None
        blocks, malformed, dropped_names = _openai_tool_use_blocks(message)
    except Exception as exc:
        raise ProviderResponseError(
            f"无法解析 OpenAI-compatible 响应: {_response_preview(obj)}"
        ) from exc
    finish_reason = str(choice.get("finish_reason") or "")
    incomplete = incomplete_response_fields(finish_reason, incomplete_reason="invalid_tool_arguments" if malformed else "")
    if incomplete:
        return ModelResponse(
            text=text,
            backend=backend_name,
            usage=usage_dict(obj.get("usage")),
            tool_use_blocks=[],
            assistant_content_blocks=_openai_assistant_content_blocks(
                reasoning=reasoning,
                text=text,
                tool_blocks=[],
            ),
            truncated_tool_names=truncated_tool_names(dropped_names),
            **incomplete,
        )
    if not text and not blocks and tools_requested:
        raise ProviderResponseError(
            f"OpenAI-compatible 响应没有文本或工具调用: {_response_preview(obj)}",
            error_code="MODEL_EMPTY_RESPONSE",
        )
    return ModelResponse(
        text=text,
        backend=backend_name,
        usage=usage_dict(obj.get("usage")),
        tool_use_blocks=blocks,
        assistant_content_blocks=_openai_assistant_content_blocks(
            reasoning=reasoning,
            text=text,
            tool_blocks=blocks,
        ),
        truncated=malformed,
        stop_reason=finish_reason,
    )


# LLM: 真机证据(2026-09-11): OpenCode Go 与 DeepSeek 官方在思考模式下会拒绝
# "assistant 有工具调用但缺少 reasoning_content" 的历史——上游原文
# "The `reasoning_content` in the thinking mode must be passed back to the API"。
# 实测：给旧历史补空串不能满足该要求(空串不等于思考原文)，必须显式关闭思考模式或回传真实原文。
# 因此这里只做两件事：①仅真实非空思考进入 wire 字段；②历史不完整时关思考。
# 不再补空字段——它既救不了旧会话，又让日志与历史看起来像"有思考"。
# 函数用途: 判断本次出站历史能否满足思考模式，不满足则显式请求关闭思考。
def _thinking_mode_supported(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if not str(message.get("reasoning_content") or "").strip():
            return False
    return True


# LLM: 只适配已核对的接口/模型组合(DeepSeek 官方 OpenAI 方言与 工具运行时 Zen/Go)；未知网关保持原消息。
# 历史无法满足思考模式时显式关闭，而不是伪造字段值冒充思考原文。
# 函数用途: 返回本次是否必须显式关闭思考，供 payload 组装使用。
def _requires_thinking_disabled(
    messages: list[dict[str, Any]], *, api_base: str, model_name: str,
) -> bool:
    endpoint = urlsplit(api_base)
    known_endpoint = endpoint.hostname == "api.deepseek.com" or (
        endpoint.hostname == "opencode.ai" and endpoint.path.startswith("/zen/")
    )
    if not known_endpoint or not model_name.lower().startswith("deepseek-"):
        return False
    return not _thinking_mode_supported(messages)


# LLM: thinking 是 DeepSeek/Zen 方言的显式关闭开关，不是通用 OpenAI 字段；只写给已核对端点。
# 函数用途: 在已知 DeepSeek 方言端点上写入关闭思考的请求字段；未知网关不写。
def _disable_thinking_for(payload: dict[str, Any], *, api_base: str) -> None:
    endpoint = urlsplit(api_base)
    known_endpoint = endpoint.hostname == "api.deepseek.com" or (
        endpoint.hostname == "opencode.ai" and endpoint.path.startswith("/zen/")
    )
    if known_endpoint:
        payload["thinking"] = {"type": "disabled"}


def _openai_tools_from_native(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the canonical internal schema to OpenAI function tools."""

    translated: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not str(tool.get("name") or ""):
            continue
        translated.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "parameters": dict(tool.get("input_schema") or {}),
                },
            }
        )
    return translated


# LLM: Plain prompts preserve legacy ordering. Typed cache layouts must mirror 会话运行时
# append-only order so local OpenAI-compatible KV caches can reuse history without cache_control.
# 函数用途: 把统一原生消息转换成 OpenAI 顺序；typed prompt 使用稳定 system→规范消息→动态事实。
def _openai_messages_from_native(
    messages: list[dict[str, Any]],
    *,
    initial_user_prompt: str,
    system_instruction: str = "",
) -> list[dict[str, Any]]:
    """Translate IR while preserving the first-turn user message across tool rounds."""

    layout = prompt_cache_layout(initial_user_prompt)
    if layout is not None:
        return _openai_messages_from_cache_layout(
            messages,
            stable_system_prefix=layout.stable_prefix,
            stable_user_prefix=layout.stable_user_prefix,
            volatile_suffix=layout.volatile_suffix,
            system_instruction=system_instruction,
        )
    translated: list[dict[str, Any]] = []
    if system_instruction:
        translated.append({"role": "system", "content": system_instruction})
    translated.append({"role": "user", "content": initial_user_prompt})
    for message in messages:
        translated.extend(_openai_message_from_native(message))
    return translated


# LLM: The system prefix and caller-supplied chronological messages are byte-stable until a real
# Compact boundary; request-varying facts append last. Never parse headings to create this order.
# 函数用途: 为没有显式 cache_control 的 OpenAI 兼容端点构造可做 token 前缀复用的消息序列。
def _openai_messages_from_cache_layout(
    messages: list[dict[str, Any]],
    *,
    stable_system_prefix: str,
    stable_user_prefix: str,
    volatile_suffix: str,
    system_instruction: str,
) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    system_text = "\n\n".join(
        text
        for text in (str(system_instruction or ""), str(stable_system_prefix or ""))
        if text
    )
    if system_text:
        translated.append({"role": "system", "content": system_text})
    if stable_user_prefix:
        translated.append({"role": "user", "content": str(stable_user_prefix)})
    for message in messages:
        translated.extend(_openai_message_from_native(message))
    return _append_openai_volatile_user_text(translated, volatile_suffix)


# LLM: Merge only with an existing trailing user message; tool and assistant ordering must remain
# untouched so provider tool-call pairing stays valid.
# 函数用途: 把本轮变化事实放到 OpenAI 消息尾部，并避免无意义的连续 user 消息。
def _append_openai_volatile_user_text(
    messages: list[dict[str, Any]],
    volatile_suffix: str,
) -> list[dict[str, Any]]:
    text = str(volatile_suffix or "")
    if not text:
        return messages
    if messages and messages[-1].get("role") == "user":
        prepared = list(messages)
        tail = prepared[-1]
        prior = str(tail.get("content") or "")
        prepared[-1] = {
            **tail,
            "content": f"{prior}\n\n{text}" if prior else text,
        }
        return prepared
    return [*messages, {"role": "user", "content": text}]


def _openai_message_from_native(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    blocks = (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )
    if role == "assistant":
        return _openai_assistant_messages(blocks)
    if role == "user":
        return _openai_user_messages(blocks)
    return []


# LLM: 所有真实 thinking 块（包括显式空块）都随 assistant 原样回放，不仅是 tool_use 轮；
# 普通文本不制造厂商扩展字段。改动须同步检查流式/非流式解析和跨轮 native history。
# 函数用途: 把规范化 assistant 块恢复成 Chat Completions 消息，避免插话或最终答复后的工具请求丢失思考字段。
def _openai_assistant_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    texts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
    # 空思考块必须按"没有思考"处理：写空串会变成上游拒绝的 reasoning_content=""，
    # 也会让"历史是否满足思考模式"的判定失真。
    reasoning = [
        text
        for text in (str(block.get("thinking") or "") for block in blocks if block.get("type") == "thinking")
        if text.strip()
    ]
    calls = [_openai_function_call(block) for block in blocks if block.get("type") == "tool_use"]
    if texts or calls or reasoning:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
        if calls:
            message["tool_calls"] = calls
        if reasoning:
            # 普通答复同样属于 reasoning 历史，不能按是否调工具丢弃。
            message["reasoning_content"] = "".join(reasoning)
        return [message]
    return []


# LLM: OpenAI-compatible reasoning/text/tool_calls 进入 canonical IR 前只能经过这一白名单，
# 未知 message 字段不得原样回放到后续请求。
# 函数用途: 把回复整理成有序块，None 表示未返回思考字段，空字符串表示返回了空思考；不混淆两者。
def _openai_assistant_content_blocks(
    *,
    reasoning: str | None,
    text: str,
    tool_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if reasoning is not None:
        blocks.append({"type": "thinking", "thinking": reasoning})
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(
        {
            "type": "tool_use",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or ""),
            "input": dict(block.get("input") or {}),
        }
        for block in tool_blocks
    )
    return blocks


# LLM: StreamCompletion 只允许携带白名单 assistant blocks；本帮助函数只读取 thinking，
# 不接受正文、工具参数或未知 provider 扩展字段。
# 函数用途: 取得完整思考文本，无思考块时返回 None，让普通模型保持原协议。
def _openai_reasoning_from_completion(completion: StreamCompletion) -> str | None:
    parts = [
        str(block.get("thinking") or "")
        for block in completion.assistant_content_blocks
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    return "".join(parts) if parts else None


def _openai_function_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(block.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
        },
    }


def _openai_user_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if pending_text:
            translated.append({"role": "user", "content": "".join(pending_text)})
            pending_text.clear()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            pending_text.append(str(block.get("text") or ""))
            continue
        if block.get("type") != "tool_result":
            continue
        flush_text()
        translated.append(
            {
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": str(block.get("content") or ""),
            }
        )
    flush_text()
    return translated


# LLM: JSON 必须是完整对象；坏块不上报为空参数调用，错误标记由响应层执行整轮零工具边界。
# 函数用途: 提取非流式工具参数，区分真实空对象与损坏/缺失参数。
def _openai_tool_use_blocks(message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, list[str]]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return [], False, []
    blocks: list[dict[str, Any]] = []
    malformed = False
    dropped: list[str] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            malformed = True
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        arguments = function.get("arguments")
        try:
            tool_input = (
                arguments if isinstance(arguments, dict) else json.loads(arguments)
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed = True
            dropped.append(str(function.get("name") or ""))
            continue
        if not isinstance(tool_input, dict):
            malformed = True
            dropped.append(str(function.get("name") or ""))
            continue
        blocks.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": tool_input,
            }
        )
    return blocks, malformed, dropped


class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"
    supports_tool_input_progress = True
    supports_thinking_completion = True

    def _tool_endpoint(self) -> str:
        return self.api_base + "/v1/messages"

    def __init__(
        self,
        options: BackendOptions,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(options)
        self.anthropic_version = anthropic_version

    # LLM: Anthropic-compatible 声明工具参数 delta 与 thinking block-stop 两项能力；
    # callback 只投影 typed 展示边界，不能改变 payload、tool choice、历史或响应解析。
    # 函数用途: 调用 Anthropic messages，并可选上报思考终态和工具参数生成进度。
    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ModelResponse:
        """Call the Anthropic-compatible messages endpoint.

        text 协议（``messages`` 为 None）：单条 ``user`` 消息承载整段 prompt，行为不变。
        native 协议（``messages`` 非空）：保留第一轮真实发送的 ``user=prompt``，再接结构化
        IR 翻出的 assistant(tool_use)/user(tool_result) 序列。不能把同一 prompt 在续轮改成
        system；否则历史失去原始 user turn，严格 OpenAI chat template 会拒绝工具结果续轮。
        独立 ``request_options.system_instruction`` 始终走 Anthropic 顶层 system 字段，不改变上述 user 历史。
        思考 observer 的 ``complete`` 只接收完成块正文；工具参数 callback 只接收累计字符计数，均不参与工具执行。
        """
        provider_options = request_options or ProviderRequestOptions()
        return self._generate_request(
            prompt,
            system_instruction=provider_options.system_instruction,
            on_chunk=on_chunk,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
            thinking_disabled=provider_options.thinking_disabled,
            on_thinking_delta=on_thinking_delta,
            on_tool_input_progress=on_tool_input_progress,
            first_event_timeout_seconds=provider_options.first_event_timeout_seconds,
        )

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Force one non-executable tool block as the provider-native JSON envelope."""

        tool_name = "my_agent_structured_output"
        structured_prompt = (
            "MANDATORY OUTPUT CONTRACT: Call the provided my_agent_structured_output tool "
            "exactly once. Do not answer with prose, Markdown, XML, or a textual tool-call "
            "imitation.\n\n" + prompt
        )
        schema_tool = {
            "name": tool_name,
            "description": (
                "Required non-executable response envelope. Call exactly once; never answer in prose."
            ),
            "input_schema": response_schema,
        }
        for attempt in range(2):
            request_prompt = structured_prompt
            if attempt:
                request_prompt += (
                    "\n\nThe previous provider response ignored the mandatory structured channel. "
                    "Retry by calling my_agent_structured_output exactly once and emit no text."
                )
            response = self._generate_request(
                request_prompt,
                tools=[schema_tool],
                tool_choice=ToolChoice.specific(tool_name),
                messages=messages,
                stream_response=False,
                temperature=0.0,
                # LLM: 强制工具信封不需要推理；部分兼容端点(如 工具运行时 zen)在思考模式下拒绝强制 tool_choice，
                # 显式关闭 thinking 可同时满足该约束并节省结构化调用的延迟与 token。
                thinking_disabled=True,
            )
            blocks = [
                block
                for block in response.tool_use_blocks
                if isinstance(block, dict) and str(block.get("name") or "") == tool_name
            ]
            if len(blocks) == 1 and isinstance(blocks[0].get("input"), dict):
                return ModelResponse(
                    text=json.dumps(blocks[0]["input"], ensure_ascii=False),
                    backend=response.backend,
                    usage=dict(response.usage),
                    stop_reason=response.stop_reason,
                )
        raise ProviderResponseError(
            "Anthropic-compatible structured response did not return one valid schema block",
            error_code="MODEL_SCHEMA_INVALID",
        )

    # LLM: Auxiliary JSON calls share the normal Anthropic transport but may lower only this request's output cap.
    # 函数用途: 给聚焦判读等短 JSON 请求设置有界输出，不修改主会话的全局 max_tokens。
    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        return self._generate_request(
            prompt,
            messages=messages,
            max_output_tokens=max_tokens,
        )

    # LLM: Anthropic 单一组包入口仅发送显式 top_p，不套用 Chat 方言默认；流观察器不得进入 payload。
    # 函数用途: 组装并发送 Anthropic-compatible 连接/采样配置，同时传递流式展示观察器。
    def _generate_request(
        self,
        prompt: str,
        *,
        system_instruction: str = "",
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        stream_response: bool | None = None,
        temperature: float | None = None,
        thinking_disabled: bool = False,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        first_event_timeout_seconds: float | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": _bounded_output_tokens(
                self.max_tokens,
                max_output_tokens,
            ),
            "temperature": self.temperature if temperature is None else float(temperature),
        }
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        cache_projection = anthropic_prompt_cache_projection(
            system_instruction=system_instruction,
            prompt=prompt,
            cache_enabled=self.prompt_cache_enabled,
            native_messages=messages is not None,
        )
        if cache_projection.system:
            payload["system"] = cache_projection.system
        if thinking_disabled:
            # LLM: 兼容 Anthropic 官方 thinking 参数；不识别该字段的端点(如 MiniMax)静默忽略。
            payload["thinking"] = {"type": "disabled"}
        selected_tools = _tools_for_choice(tools, tool_choice)
        payload["messages"], selected_tools = anthropic_messages_with_optional_cache(
            prompt=cache_projection.prompt,
            messages=messages,
            tools=selected_tools,
            cache_enabled=self.prompt_cache_enabled,
            stable_user_prefix=cache_projection.stable_user_prefix,
            stable_system_cache_active=(
                cache_projection.stable_system_cache_active
            ),
            structured_native_layout_active=(
                cache_projection.structured_native_layout_active
            ),
        )
        if selected_tools:
            from .tool_protocol_adapter import anthropic_tool_choice

            payload["tools"] = selected_tools
            payload["tool_choice"] = anthropic_tool_choice(tool_choice or ToolChoice.auto())
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            # 兼容只认 x-api-key 的 Anthropic 兼容端点(如 工具运行时.ai/zen:Authorization: Bearer
            # 返回 403 Missing API key,x-api-key 才通过)。两头发,主流端点都接受。
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
        }
        use_stream = self.stream_enabled if stream_response is None else stream_response
        if use_stream:
            return self._generate_stream(
                payload,
                headers,
                on_chunk=on_chunk,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                first_event_timeout_seconds=first_event_timeout_seconds,
            )
        return self._generate_non_stream(payload, headers)

    # LLM: 非流式响应按同一完整性合同保留正文/思考，坏工具参数不能猜补或绕过整轮零执行。
    # 函数用途: 请求一次非流式 Anthropic-compatible 响应，并整理成运行时统一结果。
    def _generate_non_stream(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> ModelResponse:
        obj: dict[str, Any] = {}
        text = ""
        blocks: list[dict[str, Any]] = []
        assistant_blocks: list[dict[str, Any]] = []
        for attempt in range(2):
            obj = self.request_json("/v1/messages", payload, headers)
            try:
                text = _anthropic_text_from_response(obj)
                blocks, malformed, dropped_names = _anthropic_tool_use_blocks(obj)
                assistant_blocks = _anthropic_assistant_content_blocks(obj)
            except Exception as exc:
                raise ProviderResponseError(
                    f"无法解析 Anthropic-compatible 响应: {_response_preview(obj)}"
                ) from exc
            incomplete = incomplete_response_fields(str(obj.get("stop_reason") or ""),
                incomplete_reason="invalid_tool_arguments" if malformed else "")
            if incomplete:
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage_dict(obj.get("usage")),
                    tool_use_blocks=[],
                    assistant_content_blocks=without_tool_blocks(assistant_blocks),
                    truncated_tool_names=truncated_tool_names(dropped_names),
                    **incomplete,
                )
            if text or blocks or attempt > 0 or not _anthropic_has_thinking_without_text(obj):
                break
        # 原生 tool_use 下模型可能只回 tool_use 块、没有文本，这种是合法的，不报空响应。
        if not text and not blocks:
            raise ProviderResponseError(
                f"Anthropic-compatible 响应没有文本内容: {_response_preview(obj)}",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage_dict(obj.get("usage")),
            tool_use_blocks=blocks,
            assistant_content_blocks=assistant_blocks,
            stop_reason=str(obj.get("stop_reason") or ""),
        )

    # LLM: 流式 Anthropic 响应由 StreamCompletion 带回完整有序 assistant 块；thinking delta/block-stop 与
    # 工具参数计数各走显式 observer；EOF/坏参数不重试为空响应，不执行工具，不丢弃已闭合正文/思考。
    # 函数用途: 收集流式响应、保留内部历史，并按供应商块顺序投影思考和工具参数进度。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        first_event_timeout_seconds: float | None = None,
    ) -> ModelResponse:
        """Parse Anthropic SSE and retry the same stream path once when no text is visible."""
        text, usage, blocks, completion = "", {}, [], StreamCompletion()
        for attempt in range(2):
            text, usage, blocks, completion = self._stream_text_once(
                payload,
                headers,
                on_chunk,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                first_event_timeout_seconds=first_event_timeout_seconds,
            )
            incomplete = incomplete_response_fields(completion.stop_reason, incomplete_reason=completion.incomplete_reason)
            if incomplete:
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage,
                    tool_use_blocks=[],
                    assistant_content_blocks=without_tool_blocks(list(completion.assistant_content_blocks)),
                    truncated_tool_names=truncated_tool_names(completion.tool_names),
                    **incomplete,
                )
            if text or blocks or attempt > 0:
                break
        # 同非流式：只回 tool_use 块、无文本也合法，不报空响应。
        if not text and not blocks:
            raise ProviderResponseError(
                "Anthropic-compatible 流式响应没有文本内容",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            assistant_content_blocks=list(completion.assistant_content_blocks),
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )

    # LLM: 单次物理流必须把 delta、block-stop 与参数 observer 一起交给 collector；重试边界
    # 仍由上层 _generate_stream 掌权，不能在此创建工具、展示重放或重试状态。
    # 函数用途: 读取并收集一条 Anthropic SSE 响应，并保留供应商原始块顺序。
    def _stream_text_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None,
        *,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        first_event_timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        return collect_anthropic_stream_with_completion(
            _request_stream_lines(
                lines,
                "/v1/messages",
                payload,
                headers,
                first_event_timeout_seconds,
            ),
            on_chunk=on_chunk,
            on_thinking_delta=on_thinking_delta,
            on_tool_input_progress=on_tool_input_progress,
        )


def _tools_for_choice(
    tools: list[dict[str, Any]] | None,
    choice: ToolChoice | None,
) -> list[dict[str, Any]]:
    selected = list(tools or ())
    if choice is None or choice.mode == "auto":
        return selected
    if choice.mode == "none":
        return []
    if not selected:
        raise ValueError(f"tool_choice={choice.mode} requires a non-empty tools surface")
    names = {str(tool.get("name") or "").strip() for tool in selected if isinstance(tool, dict)}
    if choice.mode == "specific" and choice.tool_name not in names:
        raise ValueError(f"specific tool_choice is outside provider surface: {choice.tool_name}")
    return selected


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _anthropic_text_from_response(obj: dict[str, Any]) -> str:
    parts = obj.get("content", [])
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") in (None, "text")
    )
    if not text and "completion" in obj:
        text = obj["completion"]
    return str(text or "")


# LLM: 原生 input 必须是对象；缺失/坏值只报告协议失败，不转成可以执行的空参数。
# 函数用途: 读取 Anthropic 非流式工具块，并返回整轮是否存在损坏输入。
def _anthropic_tool_use_blocks(obj: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, list[str]]:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return [], False, []
    blocks: list[dict[str, Any]] = []
    malformed = False
    dropped: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        tool_input = part.get("input")
        if not isinstance(tool_input, dict):
            malformed = True
            dropped.append(str(part.get("name", "") or ""))
            continue
        blocks.append(
            {
                "id": str(part.get("id", "") or ""),
                "name": str(part.get("name", "") or ""),
                "input": tool_input,
            }
        )
    return blocks, malformed, dropped


# LLM: 该白名单是 Anthropic 响应块进入下一轮请求的唯一边界；禁止原样回放未来新增的输出专用字段。
# 函数用途: 从非流式响应中按原顺序提取可安全回放的 thinking、text、redacted_thinking 和 tool_use 块。
def _anthropic_assistant_content_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        block_type = str(part.get("type") or ("text" if "text" in part else ""))
        if block_type == "text":
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
            continue
        if block_type == "thinking":
            block = {"type": "thinking", "thinking": str(part.get("thinking") or "")}
            signature = part.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            blocks.append(block)
            continue
        if block_type == "redacted_thinking":
            data = part.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "redacted_thinking", "data": data})
            continue
        if block_type == "tool_use":
            tool_input = part.get("input")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(part.get("id") or ""),
                    "name": str(part.get("name") or ""),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
            )
    return blocks


def _anthropic_has_thinking_without_text(obj: dict[str, Any]) -> bool:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and "thinking" in part for part in parts)


def _response_preview(obj: object, *, max_chars: int = 1000) -> str:
    text = str(obj)
    return text if len(text) <= max_chars else text[:max_chars] + "... [truncated]"


# LLM: 只按显式协议构造后端，请求头/top_p/温度与模型配置同快照；不因失败换接口。
# 函数用途: 创建指定协议的适配器，让 YAML 和模型级采样配置进入真实请求。
def get_backend(name: str, config: Any | None = None) -> BaseBackend:
    """Resolve a configured backend name to a backend adapter instance."""

    if name == "echo":
        return EchoBackend()
    if config is None:
        raise ValueError("真实模型后端需要传入 config。")

    common = BackendOptions(
        api_base=config.api_base,
        api_key=config.api_key,
        model_name=config.model_name,
        request_timeout=config.request_timeout,
        max_tokens=config.max_tokens,
        context_window_tokens=getattr(config, "model_context_window_tokens", 0),
        temperature=float(config.temperature),
        temperature_explicit=getattr(config, "model_temperature_explicit", False),
        top_p=getattr(config, "top_p", None),
        stream_enabled=getattr(config, "stream_enabled", True),
        prompt_cache_enabled=getattr(config, "anthropic_prompt_cache_enabled", True),
        custom_headers=getattr(config, "model_custom_headers", {}),
        session_header=getattr(config, "model_session_header", ""),
    )

    if name == "openai_compatible":
        return OpenAICompatibleBackend(common)
    if name == "openai_responses":
        from .responses import OpenAIResponsesBackend

        return OpenAIResponsesBackend(common)
    if name == "anthropic_compatible":
        return AnthropicCompatibleBackend(
            common,
            anthropic_version=config.anthropic_version,
        )

    raise ValueError(
        "未知模型后端: %s。当前内置 echo / openai_compatible / openai_responses / anthropic_compatible。" % name
    )
