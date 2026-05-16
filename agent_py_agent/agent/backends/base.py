# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

from __future__ import annotations

"""模型后端适配层。

这一层的作用很像'翻译器'：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .stream_parsers import anthropic_stream_contents, openai_stream_contents


# LLM: ModelResponse 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 集中保存模型响应字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ModelResponse:

    text: str
    backend: str


# LLM: BackendOptions 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 集中保存后端选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class BackendOptions:
    """Connection and generation options shared by HTTP model backends."""

    api_base: str
    api_key: str
    model_name: str
    request_timeout: int = 240
    max_tokens: int = 1024
    temperature: float = 0.2
    stream_enabled: bool = True


# LLM: BaseBackend 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 适配基础后端协议，把模型请求响应归一到内部后端契约；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    # LLM: generate 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 提交提示词并返回归一化模型响应，供运行循环继续消费；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        raise NotImplementedError


# LLM: EchoBackend 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 适配 echo 本地后端协议，把模型请求响应归一到内部后端契约；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
class EchoBackend(BaseBackend):

    name = "echo"

    # LLM: generate 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 提交提示词并返回归一化模型响应，供运行循环继续消费；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
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


# LLM: HttpBackend 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 适配HTTP后端协议，把模型请求响应归一到内部后端契约；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    # LLM: __init__ 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def __init__(
        self,
        options: BackendOptions,
    ):
        self.api_base = str(options.api_base).rstrip("/")
        self.api_key = str(options.api_key)
        self.model_name = str(options.model_name)
        self.request_timeout = int(options.request_timeout)
        self.max_tokens = int(options.max_tokens)
        self.temperature = float(options.temperature)
        self.stream_enabled = bool(options.stream_enabled)

    # LLM: request_json 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 发送 JSON 模型请求并解析响应对象，供后端生成流程使用；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(self._gateway_request(path, payload, headers))

    # LLM: request_stream 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 发送流式模型请求并收集响应片段，供后端拼接完整文本；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def request_stream(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> list[str]:
        return post_stream(self._gateway_request(path, payload, headers))

    # LLM: request_stream_iter 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 发送流式模型请求并逐段产出内容，支持调用方实时消费；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def request_stream_iter(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ):
        yield from post_stream_iter(self._gateway_request(path, payload, headers))

    # LLM: _gateway_request 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 组装网关请求对象，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _gateway_request(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> GatewayRequest:
        return GatewayRequest(
            api_base=self.api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=headers,
            timeout=self.request_timeout,
        )


# LLM: OpenAICompatibleBackend 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 适配 OpenAI 兼容后端协议，把模型请求响应归一到内部后端契约；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"

    # LLM: generate 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 提交提示词并返回归一化模型响应，供运行循环继续消费；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.stream_enabled:
            return self._generate_stream(payload, headers, on_chunk=on_chunk)
        obj = self.request_json("/chat/completions", payload, headers)
        try:
            text = obj["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"无法解析 OpenAI-compatible 响应: {obj}") from exc
        return ModelResponse(text=text, backend=self.name)

    # LLM: _generate_stream 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 处理流式生成响应的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """流式解析 OpenAI SSE：逐行拼接 delta.content。"""
        parts: list[str] = []
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        for content in openai_stream_contents(lines("/chat/completions", payload, headers)):
            parts.append(content)
            if on_chunk is not None:
                on_chunk(content)
        return ModelResponse(text="".join(parts), backend=self.name)


# LLM: AnthropicCompatibleBackend 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 适配 Anthropic 兼容后端协议，把模型请求响应归一到内部后端契约；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"

    # LLM: __init__ 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def __init__(
        self,
        options: BackendOptions,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(options)
        self.anthropic_version = anthropic_version

    # LLM: generate 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 提交提示词并返回归一化模型响应，供运行循环继续消费；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": self.anthropic_version,
        }
        if self.stream_enabled:
            return self._generate_stream(payload, headers, on_chunk=on_chunk)
        obj: dict[str, Any] = {}
        text = ""
        for attempt in range(2):
            obj = self.request_json("/v1/messages", payload, headers)
            try:
                text = _anthropic_text_from_response(obj)
            except Exception as exc:
                raise RuntimeError(f"无法解析 Anthropic-compatible 响应: {obj}") from exc
            if text or attempt > 0 or not _anthropic_has_thinking_without_text(obj):
                break
        if not text:
            raise RuntimeError(f"Anthropic-compatible 响应没有文本内容: {obj}")
        return ModelResponse(text=text, backend=self.name)

    # LLM: _generate_stream 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 处理流式生成响应的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """流式解析 Anthropic SSE：监听 content_block_delta 事件拼接文本。"""
        # LLM: Anthropic SSE 用事件行区分类型，数据行携带 JSON 片段。
        # request_stream 已过滤 event 行，需从 data 行的 type 字段恢复事件类型。
        for attempt in range(2):
            text = self._stream_text_once(payload, headers, on_chunk)
            if text or attempt > 0:
                break
        if not text:
            raise RuntimeError("Anthropic-compatible 流式响应没有文本内容")
        return ModelResponse(text=text, backend=self.name)

    # LLM: _stream_text_once isolates one Anthropic SSE attempt so retry logic stays flat.
    # 函数用途: 执行一次流式请求并拼接文本；有 on_chunk 时同步把片段推给调用方。
    def _stream_text_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None,
    ) -> str:
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        parts: list[str] = []
        for chunk in anthropic_stream_contents(lines("/v1/messages", payload, headers)):
            parts.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        return "".join(parts)


# LLM: _anthropic_text_from_response extracts only assistant-visible text from messages payloads.
# 函数用途: 解析 Anthropic-compatible 非流式响应，兼容 content text 和旧 completion 字段。
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


# LLM: _anthropic_has_thinking_without_text identifies transient MiniMax/Anthropic-compatible shapes.
# 函数用途: 模型偶尔只返回 thinking block 时触发一次非流式重试，避免把可恢复空正文直接打成 runner 失败。
def _anthropic_has_thinking_without_text(obj: dict[str, Any]) -> bool:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and "thinking" in part for part in parts)


# LLM: get_backend 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 按名称选择模型后端实现，并用配置构造可调用实例；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def get_backend(name: str, config: Any | None = None) -> BaseBackend:

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
        temperature=float(config.temperature),
        stream_enabled=getattr(config, "stream_enabled", True),
    )

    if name == "openai_compatible":
        return OpenAICompatibleBackend(common)
    if name == "anthropic_compatible":
        return AnthropicCompatibleBackend(
            common,
            anthropic_version=config.anthropic_version,
        )

    raise ValueError(
        "未知模型后端: %s。当前内置 echo / openai_compatible / anthropic_compatible。"
        % name
    )
