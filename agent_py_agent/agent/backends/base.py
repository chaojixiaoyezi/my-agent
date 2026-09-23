# LLM: 此模块只定义后端公共合同和本地实现；HTTP/协议/工厂各有唯一模块，变更接口须同步所有适配器与回归。
# 模块用途: 保存冻结连接选项、请求控制和统一响应，让运行时无需导入具体模型协议。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..settings.defaults import DEFAULT_MODEL_MAX_TOKENS
from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice
from .errors import (
    ModelNotConfiguredError,
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
# 类用途: 保存 HTTP 模型后端的连接、采样和媒体发送预算。
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
    input_media_max_bytes: int = 16 * 1024 * 1024
    top_p: float | None = None


# LLM: 供应商级请求控制集中在 typed options；首包预算必须保持 request-local，不能通过修改共享 backend 传递。
# 类用途: 携带一次模型请求的宿主 system 指令、思考开关和首个流式事件等待预算，不混入用户正文或工具历史。
@dataclass(frozen=True)
class ProviderRequestOptions:
    system_instruction: str = ""
    thinking_disabled: bool = False
    first_event_timeout_seconds: float | None = None


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

    # LLM: 公共合同不把配置容量冒充供应商证据；HTTP 与 OAuth 实现分别覆盖，需同步容量解析测试。
    # 函数用途: 为没有目录探测能力的后端返回未知容量，不发网络请求。
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

    # LLM: 基础后端不声明原生 schema 能力；保留 generate 的最小合同，具体协议负责覆盖与校验。
    # 函数用途: 为本地或自定义后端沿普通生成入口返回结果；实际调用可能由子类发起网络请求。
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

    # LLM: 基础合同不假装提供 JSON 输出保证；HTTP 子类负责请求级输出限制，需检查摘要调用方。
    # 函数用途: 将不支持原生 JSON 的后端交回普通生成入口，不修改实例的输出预算。
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


# LLM: 未配置适配器只保持构造与设置入口可用；所有生成和探针明确失败，不模拟模型、不发送网络请求。
# 类用途: 在新安装尚未选模型时承载空状态，让用户仍可进入 /model。
class UnconfiguredBackend(BaseBackend):
    name = "unconfigured"

    # LLM: 工具能力预检与实际生成共用相同缺配置错误，不能误报成模型不支持工具。
    # 函数用途: 尚无模型时拒绝能力探针，不发网络请求。
    def probe_tool_capability(self) -> ProviderToolCapability:
        raise ModelNotConfiguredError()

    # LLM: 参数逐项遵守 BaseBackend.generate；缺配置时不消费输入、不调用模型，修改公共签名须同步此处。
    # 函数用途: 明确提醒先选择模型，普通聊天和后台生成都不能偷偷回退。
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
        raise ModelNotConfiguredError()


# LLM: 此本地测试实现不代表真实模型或工具验收；工厂只有显式 echo 配置才能选用。
# 类用途: 提供无网络的确定性回复，供已有开发与合同测试使用。
class EchoBackend(BaseBackend):
    """Local deterministic backend used by tests and dry development."""

    name = "echo"

    # LLM: 本地测试后端的能力声明不代表已发起供应商探针；需保持测试工具协议选择一致。
    # 函数用途: 返回 echo 的本地原生工具能力，不启动进程或网络请求。
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


# LLM: 后端能力证据使用 UTC 观测时间；它不是执行状态或调度时钟，需保持工具探针格式。
# 函数用途: 生成带时区的 ISO 时间，供能力观测记录使用。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
