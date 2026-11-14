# LLM: 此模块独占 HTTP 后端公共传输、能力探针和目录元数据；保持请求局部预算、取消与配置错误合同。
# 模块用途: 为模型协议提供带物理并发限制的统一网络入口和有界工具探测；联合后端、OAuth、关闭与排队回归。
from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..concurrency.interrupt import wait_interruptibly
from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice
from .base import BackendOptions, BaseBackend, ProviderRequestOptions, _utc_now_iso
from .errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
)
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .provider_headers import current_probe_accounting, endpoint_parts, request_headers
from .request_budget import RequestBudgetExceeded, RequestTokenBudget

# 探针只缓存成功证据；短暂失败允许之后重试，但单次探测有界。
_PROBE_MAX_ATTEMPT_COUNT = 3

# LLM: 进程内计数器：记录“没有绑定记账范围”的工具能力探测尝试，只读出口经 GET /status 的 usage_accounting 段
#   投影（gateway_parts/http_handlers._usage_accounting_diagnostics），线上非零就说明有探测没进用量账。
#   不写日志避免高频噪声；计数只反映次数，不含任何探测细节。宿主绑定 scope 的探测不计入。
_UNACCOUNTED_PROBE_ATTEMPT_COUNT = 0
_UNACCOUNTED_PROBE_LOCK = threading.Lock()


# 函数用途: 记录一次未绑定记账范围的工具能力探测尝试。
def _count_unaccounted_probe_attempt() -> None:
    global _UNACCOUNTED_PROBE_ATTEMPT_COUNT
    with _UNACCOUNTED_PROBE_LOCK:
        _UNACCOUNTED_PROBE_ATTEMPT_COUNT += 1


# LLM: 只读；改名或改语义须同步 http_handlers 的 /status 投影与 test_probe_tool_capability_metering。
# 函数用途: 返回进程内未绑定记账范围的工具能力探测尝试次数（诊断用，只读，/status 可见）。
def unaccounted_probe_attempt_count() -> int:
    with _UNACCOUNTED_PROBE_LOCK:
        return _UNACCOUNTED_PROBE_ATTEMPT_COUNT


# 函数用途: 清零未绑定探测计数；只供测试和显式管理入口使用。
def reset_unaccounted_probe_attempt_count() -> None:
    global _UNACCOUNTED_PROBE_ATTEMPT_COUNT
    with _UNACCOUNTED_PROBE_LOCK:
        _UNACCOUNTED_PROBE_ATTEMPT_COUNT = 0


# LLM: HTTP 协议共同使用此请求级限额；不能热改实例配置，需同步 Chat/Messages/Responses 的摘要调用测试。
# 函数用途: 将本轮输出上限限制在配置范围内，未覆盖时保留配置值。
def bounded_output_tokens(configured: int, requested: int | None, *,
                          configured_ceiling: int | None = None,
                          token_budget: RequestTokenBudget | None = None) -> int:
    ceiling = configured_ceiling if configured_ceiling is not None else configured
    legacy = min(configured, ceiling)
    if token_budget is None:
        return legacy if requested is None else max(1, min(legacy, int(requested)))
    budget = token_budget
    limit = ceiling if budget.shared_window and not budget.server_managed_output else legacy
    invalid = (type(budget.output_tokens) is not int or budget.output_tokens <= 0
               or budget.output_tokens > limit or budget.input_tokens < 0 or budget.safety_tokens < 0
               or (requested is not None and int(requested) != budget.output_tokens))
    if budget.shared_window:
        invalid = invalid or budget.input_tokens + budget.output_tokens + budget.safety_tokens > budget.context_window_tokens
    if invalid:
        raise RequestBudgetExceeded(reason="invalid_budget", input_tokens=budget.input_tokens,
                                    output_ceiling=budget.output_tokens,
                                    context_window_tokens=budget.context_window_tokens)
    return budget.output_tokens


# LLM: 探测记账只在宿主绑定的 scope 内生效；函数内导入避免 backends 与 agent_core 互相依赖。
#   每次真实 generate 一条记录（重试也算），成功用供应商 usage 收尾，失败记 failed。
#   无 scope（直调、测试替身）直接返回空 call_id，不产生任何账本副作用。
# 函数用途: 登记一次工具能力探测的模型调用开始（输入按实际发送的提示估算），返回本次探测的记账 id。
def _start_probe_call(scope: object | None, backend: object, prompt: str) -> str:
    if scope is None:
        return ""
    from ..contracts.model_call_ledger import ModelCallStartedParams
    from ..memory_archive import estimate_tokens

    call_id = f"probe:tool_capability:{secrets.token_hex(8)}"
    scope.ledger.started(
        ModelCallStartedParams(
            call_id=call_id,
            backend=str(getattr(backend, "name", "") or ""),
            model=str(getattr(backend, "model_name", "") or ""),
            input_tokens=estimate_tokens(prompt),
            output_tokens_estimate=0,
            request_id=str(getattr(scope, "request_id", "") or ""),
            run_id=str(getattr(scope, "run_id", "") or ""),
            is_probe=True,
            metadata={"purpose": "probe:tool_capability", "logical_call_id": call_id},
        )
    )
    return call_id


# LLM: 探测响应没有用户可见正文，usage 是否上报由原收尾函数按结构化字段决定；失败原因只记类型与错误码。
# 函数用途: 探测请求成功返回后按供应商用量收尾记账；无 scope 时直接跳过。
def _finish_probe_call(scope: object | None, call_id: str, response: object) -> None:
    if scope is None or not call_id:
        return
    from ..agent_core.model.call_runtime import record_model_call_finished

    record_model_call_finished(scope.ledger, call_id, response)


# 函数用途: 探测请求抛异常时把该次调用记为失败，不改变探测自身的异常传播；无 scope 时直接跳过。
def _fail_probe_call(scope: object | None, call_id: str, exc: BaseException) -> None:
    if scope is None or not call_id:
        return
    from ..agent_core.model.call_runtime import record_model_call_failed

    record_model_call_failed(scope.ledger, call_id, exc)


# LLM: 限流开启后 collector 提前返回也必须关流归还名额；默认关闭保持原收尾，list 等无 close 的替身不动作。
# 函数用途: 在需要归还名额时显式关闭同一个实际流；解析成功、提前结束和异常共用此出口。
@contextmanager
def provider_stream_scope(lines, *, enabled: bool = True):
    try:
        yield lines
    finally:
        close = getattr(lines, "close", None) if enabled else None
        if callable(close):
            close()


# LLM: HTTP 请求控制只在显式给出时透传；流入口与测试替身须保持既有调用合同，不在此重试。
#   cabfix：多一个可选的绝对期限——两者都没给时才退回最简两参调用，保持旧后端/替身零改动。
# 函数用途: 调用流式传输入口，按需附加当前请求的首事件等待预算。
def request_stream_lines(
    lines,
    request_args: tuple[str, dict[str, Any], dict[str, str], float | None, float | None],
):
    path, payload, headers, first_event_timeout_seconds, total_deadline_seconds = request_args
    if first_event_timeout_seconds is None and total_deadline_seconds is None:
        return lines(path, payload, headers)
    # LLM: 两条出口都按被调方签名选择，不用 TypeError 试探——一次失败可能被上层当成可重试
    #   而重复发送。③ 新形态 options=；② 只认首包预算；① 最简三参（旧替身/假后端）。
    if _accepts_kwarg(lines, "options"):
        return lines(
            path, payload, headers,
            options=StreamCallOptions(
                first_event_timeout_seconds=first_event_timeout_seconds,
                total_deadline_seconds=total_deadline_seconds,
            ),
        )
    if _accepts_kwarg(lines, "total_deadline_seconds"):
        return lines(
            path, payload, headers,
            first_event_timeout_seconds=first_event_timeout_seconds,
            total_deadline_seconds=total_deadline_seconds,
        )
    return lines(path, payload, headers, first_event_timeout_seconds=first_event_timeout_seconds)


# LLM: 只读 callable 签名；探测失败（内建/无签名）时不加新关键字，保持原调用形态。
# 函数用途: 判断流式入口是否接受某个关键字参数。
def _accepts_kwarg(callable_obj: object, keyword: str) -> bool:
    import inspect

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


# LLM: cabfix：流式/非流式入口的请求材料与超时选项各收成一个数据类，避免每加一项
#   超时事实就拉长一组公开函数签名（code-size 参数门禁也会拦）。
# 类用途: 携带一次 HTTP 调用的路径、请求体与请求头。
@dataclass(frozen=True)
class StreamCall:
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]


# LLM: 两个字段都可能为 None，表示沿用该调用方原有的超时语义（不设首包预算 / 不设总期限）。
# 类用途: 携带一次 HTTP 调用的首事件预算与绝对期限。
@dataclass(frozen=True)
class StreamCallOptions:
    first_event_timeout_seconds: float | None = None
    total_deadline_seconds: float | None = None


# LLM: _gateway_request 的两种调用形态只在这里拆：新的 (StreamCall, options) 与既有 (path, payload, headers)；
#   后者仍被大量旧调用方和测试使用。覆盖 _gateway_request 的子类（OAuth）必须用它并保持与基类相同的签名，
#   否则基类 request_json/request_stream/request_stream_iter 调进来就是 TypeError（17k Linux 车道实测）。
# 函数用途: 把 StreamCall 或旧的三段参数拆成 (path, payload, headers)；缺段时报 TypeError。
def stream_call_parts(
    call: StreamCall | str,
    payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    if isinstance(call, StreamCall):
        return call.path, call.payload, call.headers
    if payload is None or headers is None:
        raise TypeError("_gateway_request 需要 StreamCall 或 path/payload/headers")
    return call, payload, headers


# LLM: cabfix：调用方给的绝对墙钟上界换算成 monotonic 时刻，放进信封的 deadline 字段（传输层发送前、读正文、
#   流式共用这一条时限）。None 或非正数表示不设总期限，主模型与旧调用方语义不变。基类与 OAuth 共用这一处。
# 函数用途: 由请求选项算出信封里的绝对截止时刻；不设总期限时返回 None。
def stream_call_deadline(options: StreamCallOptions) -> float | None:
    if options.total_deadline_seconds is not None and float(options.total_deadline_seconds) > 0:
        return time.monotonic() + float(options.total_deadline_seconds)
    return None


# LLM: 所有内置 HTTP backend 都必须把 system_instruction 映射到供应商真实高优先级字段，而非拼回 user prompt。
# 类用途: 共享真实模型 HTTP、能力探针、连接和 metadata 逻辑，并声明支持独立 system 指令。
# LLM: HTTP 主链统一应用连接、会话头和冻结物理上限；新协议复用同一传输、取消与超时语义。
# 类用途: 为模型接口提供物理排队请求、能力探测和上下文目录读取。
class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    supports_system_instructions = True
    supports_provider_request_options = True

    # LLM: 复制可变选项并冻结物理并发上限；多线程仅用 request-local 身份，不能热改共享后端采样或头部。
    # 函数用途: 初始化可复用后端、媒体预算和默认关闭的排队限制，非法采样在发送前报错。
    def __init__(
        self,
        options: BackendOptions,
    ):
        self.api_base = str(options.api_base).rstrip("/")
        self.api_key = str(options.api_key)
        self.model_name = str(options.model_name)
        self.request_timeout = int(options.request_timeout)
        self.connect_timeout = float(options.connect_timeout)
        self.provider_max_concurrent_requests = max(0, int(options.provider_max_concurrent_requests))
        self.model_http_max_idle_connections = max(0, int(getattr(options, "model_http_max_idle_connections", 0) or 0))
        self.max_tokens = int(options.max_tokens)
        self.configured_output_ceiling = int(options.configured_output_ceiling or options.max_tokens)
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
        self.reasoning_levels = tuple(options.reasoning_levels or ())
        from .model_request_capabilities import (
            resolve_model_request_capabilities,
            validate_thinking_request,
        )
        self.model_request_capabilities = resolve_model_request_capabilities(
            options.thinking_requirement, self.api_base, self.model_name, self.name)
        self.thinking_requirement = self.model_request_capabilities.thinking_requirement
        validate_thinking_request(self, disabled=False)
        # 是否能用 configuration_update 项改档位随 profile 冻结；只有 Responses 后端按它判断，其它协议不读。
        self.reasoning_update_items = str(options.reasoning_update_items or "auto")
        # 结构化输出方式随 profile 冻结；只有 OpenAI Chat 兼容接口会用到 json_object。
        self.structured_output = str(options.structured_output or "native")
        # 工厂会绑定实际的live配置；独立构造仍使用显式BackendOptions。
        self.config = options
        self._stream_enabled_override: bool | None = None
        self._stream_timeout_is_idle_override: bool | None = None
        self.prompt_cache_enabled = bool(options.prompt_cache_enabled)
        self.input_media_max_bytes = max(1, int(options.input_media_max_bytes))

    # LLM: 各协议在请求分派时读取同一live配置；OAuth的显式协议覆盖不被用户开关撤销。
    # 函数用途: 取得下一次请求的流式选择，独立构造时保留原options语义。
    @property
    def stream_enabled(self) -> bool:
        if self._stream_enabled_override is not None:
            return self._stream_enabled_override
        return bool(getattr(self.config, "stream_enabled", True))

    # LLM: 保留OAuth订阅协议及既有调用者的显式覆盖；普通构造不再写入标量快照。
    # 函数用途: 设置协议明确要求的流式模式，不修改共享AgentConfig。
    @stream_enabled.setter
    def stream_enabled(self, enabled: bool) -> None:
        self._stream_enabled_override = bool(enabled)

    # LLM: SSE沿原空闲超时合同，非流式仍使用总时限；跟随当前流式选择，不冻结旧开关。
    # 函数用途: 向工具循环报告当前传输的超时语义。
    @property
    def stream_timeout_is_idle(self) -> bool:
        if self._stream_timeout_is_idle_override is not None:
            return self._stream_timeout_is_idle_override
        return self.stream_enabled

    # LLM: OAuth订阅接口已有显式idle声明，必须保持而不热改共享配置。
    # 函数用途: 保存协议显式覆盖的超时语义。
    @stream_timeout_is_idle.setter
    def stream_timeout_is_idle(self, enabled: bool) -> None:
        self._stream_timeout_is_idle_override = bool(enabled)

    # LLM: 只有Responses协议允许实测请求突破旧默认；其它协议保持工厂夹取，元数据只能继续收紧，不新增探测。
    # 函数用途: 已知供应商输出上限只会降低构造时冻结的配置天花板。
    def request_output_ceiling(self) -> int:
        ceiling = self.configured_output_ceiling
        if self.name != "openai_responses":
            ceiling = min(ceiling, self.max_tokens)
        for key in ("max_output_tokens", "max_output_token_count", "output_token_limit"):
            value = self.model_metadata.get(key)
            if type(value) is int and value > 0:
                ceiling = min(ceiling, value)
        return ceiling

    # LLM: Serialize provider probes per backend instance and cache only proven-positive facts.
    # 函数用途: 并发冷启动共用一次有界协议探测，失败不缓存，避免暂时失败永久阻断后续任务。
    def probe_tool_capability(self) -> ProviderToolCapability:
        """有界探测原生工具能力；只缓存成功事实，失败允许之后重新探测。"""
        cached = self._provider_tool_capability_cache
        if cached is not None:
            return cached
        # 同一 Gateway/owner 的 Agent 会被多个请求线程复用。探针必须 single-flight，
        # 否则冷启动并发会重复烧请求，并让一个成功、一个失败的结果互相打架。
        # 等待别的请求探测时仍消费本轮停止信号；未取得锁绝不能释放别人的执行权。
        while not self._provider_tool_capability_lock.acquire(blocking=False):
            wait_interruptibly(0.05)
        try:
            wait_interruptibly(0)
            cached = self._provider_tool_capability_cache
            if cached is not None:
                return cached
            return self._probe_tool_capability_uncached()
        finally:
            self._provider_tool_capability_lock.release()

    # LLM: Caller holds `_provider_tool_capability_lock`; probe the ordinary auto tool-choice
    # surface, not forced-call or reasoning-disable support. Only a matching nonce proves support.
    # 宿主显式标记本次为探测；空输出仍属未证明能力，保持三次有界重试、不缓存阴性及供应商错误原样上抛。
    # 函数用途: 用普通工具选择发起有界探测并缓存阳性结果，不执行工具；避免思考模型被强制参数挡住。
    def _probe_tool_capability_uncached(self) -> ProviderToolCapability:
        # 缺凭据属于配置错误，须在协议能力判定之前暴露给调用方。
        if hasattr(self, "api_key") and not str(self.api_key or "").strip():
            raise ValueError(
                "api_key 为空：无法执行工具能力探针（检查 AGENT_API_KEY 环境变量或配置）"
            )
        evidence = ""
        for _attempt in range(1, _PROBE_MAX_ATTEMPT_COUNT + 1):
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
            prompt = (
                "Call my_agent_capability_probe exactly once with nonce "
                f"{nonce}. Do not answer in prose."
            )
            from ..llm_scale.hot_path import global_llm_admission_slot
            from .request_scope import provider_request_timeout

            # 探针在协议准备阶段调用，不在普通generate的持槽区；仅真实运输领同一进程名额。
            # 缓存/单飞仍归外层，等待不冒充供应商尝试，拿槽后重核原停止与剩余预算。
            with global_llm_admission_slot(wait_timeout=provider_request_timeout(30)):
                wait_interruptibly(0)
                provider_request_timeout(30)
                scope = current_probe_accounting()
                call_id = _start_probe_call(scope, self, prompt)
                if scope is None:
                    # 未绑定账本的真实探测仍保留原诊断计数，排队失败不增加它。
                    _count_unaccounted_probe_attempt()
                try:
                    response = self.generate(
                        prompt,
                        tools=[probe_tool],
                        tool_choice=ToolChoice.auto("native_capability_probe"),
                        request_options=ProviderRequestOptions(
                            is_probe=True,
                            thinking_disabled=getattr(scope, 'thinking_disabled', False),
                            reasoning_effort=getattr(scope, 'reasoning_effort', ''),
                        ),
                    )
                except InterruptedError as exc:
                    _fail_probe_call(scope, call_id, exc)
                    raise
                except (ProviderRecoverableError, ProviderConfigurationError) as exc:
                    _fail_probe_call(scope, call_id, exc)
                    # 保留供应商失败分类，让调用方处理配置、额度和网络，不能改成能力不足。
                    raise
                except Exception as exc:
                    _fail_probe_call(scope, call_id, exc)
                    # 本地确定性异常(解析/配置错)重试无意义, 立即落 evidence。
                    code = str(getattr(exc, "error_code", "") or "").strip().upper()
                    suffix = f":{code}" if code else ""
                    evidence = f"live_probe_failed:{type(exc).__name__}{suffix}"
                    break
                _finish_probe_call(scope, call_id, response)
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
        self, path: str, payload: dict[str, Any], headers: dict[str, str],
        *, total_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON request through the shared gateway helper."""

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(self._gateway_request(
            StreamCall(path=path, payload=payload, headers=headers),
            options=StreamCallOptions(total_deadline_seconds=total_deadline_seconds),
        ))

    # LLM: Streaming collection forwards request-local first-event budget without changing backend-wide idle configuration.
    # 函数用途: 发起并完整收集一条流式 provider 请求，供非增量调用方使用。
    def request_stream(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        options: StreamCallOptions | None = None,
    ) -> list[str]:
        """Send a streaming request and collect all data lines."""
        return post_stream(self._gateway_request(
            StreamCall(path=path, payload=payload, headers=headers), options=options,
        ))

    # LLM: The iterator preserves provider data order and per-request first-event timing for live consumers.
    # 函数用途: 发起流式请求并逐条交付 data，使 TUI 能实时显示正文、思考和工具参数进度。
    def request_stream_iter(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        options: StreamCallOptions | None = None,
    ):
        """Send a streaming request and yield data lines as they arrive."""
        yield from post_stream_iter(self._gateway_request(
            StreamCall(path=path, payload=payload, headers=headers), options=options,
        ))

    # LLM: 唯一传输封装应用显式请求头和宿主会话；后台预算与首包预算保持 request-local，不改共享配置。
    #   cabfix：total_deadline_seconds 是调用方给的绝对墙钟上界，换算成 monotonic 时刻存进信封的
    #   deadline 字段——传输层已有该字段的完整机制（发送前、读正文、流式都共享同一条时限），
    #   不必另造一套。None 表示不设总期限，主模型与旧调用方语义完全不变。
    # 函数用途: 组装统一 HTTP 请求，将后台预算与物理并发限制传到底层；普通流式空闲/取消语义不变。
    def _gateway_request(
        self,
        call: StreamCall | str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        options: StreamCallOptions | None = None,
    ) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        from .keepalive_transport import main_connection_pool
        from .request_scope import provider_request_timeout

        path, payload, headers = stream_call_parts(call, payload, headers)
        options = options or StreamCallOptions()
        api_base, path = endpoint_parts(self.api_base, path)
        deadline = stream_call_deadline(options)
        capacity = getattr(self, "model_http_max_idle_connections", 0)
        pool = main_connection_pool(capacity) if payload is not None and capacity > 0 else None
        return GatewayRequest(
            api_base=api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=request_headers(headers, self.custom_headers, self.session_header),
            timeout=provider_request_timeout(self.request_timeout),
            connect_timeout=self.connect_timeout,
            first_event_timeout=options.first_event_timeout_seconds,
            # 按本次配置冻结退避序列；空值保持原传输默认，不热改共享后端。
            retry_backoff_seconds=tuple(getattr(getattr(self, "config", None), "model_retry_backoff_seconds", ()) or ()),
            deadline=deadline,
            connection_pool=pool,
            provider_max_concurrent_requests=getattr(self, "provider_max_concurrent_requests", 0),
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
