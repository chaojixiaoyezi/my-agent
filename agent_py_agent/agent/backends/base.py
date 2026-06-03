
from __future__ import annotations

"""模型后端适配层。

这一层的作用很像'翻译器'：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import ProviderResponseError
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .usage_metadata import (
    collect_anthropic_stream,
    collect_openai_stream,
    openai_stream_payload,
    usage_dict,
)


@dataclass
class ModelResponse:
    """Normalized model response returned to the agent runtime."""

    text: str
    backend: str
    runtime_status: str = "ok"
    runtime_reason: str = ""
    runtime_source: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

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


class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        """Generate one assistant response for the supplied prompt."""
        raise NotImplementedError


class EchoBackend(BaseBackend):
    """Local deterministic backend used by tests and dry development."""

    name = "echo"

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


class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

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

    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Send a JSON request through the shared gateway helper."""

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(self._gateway_request(path, payload, headers))

    def request_stream(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> list[str]:
        """Send a streaming request and collect all data lines."""
        return post_stream(self._gateway_request(path, payload, headers))

    def request_stream_iter(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ):
        """Send a streaming request and yield data lines as they arrive."""
        yield from post_stream_iter(self._gateway_request(path, payload, headers))

    def _gateway_request(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        return GatewayRequest(
            api_base=self.api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=headers,
            timeout=self.request_timeout,
        )


class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"

    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        """Call the OpenAI-compatible chat completion endpoint."""
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
            raise ProviderResponseError(f"无法解析 OpenAI-compatible 响应: {_response_preview(obj)}") from exc
        return ModelResponse(text=text, backend=self.name, usage=usage_dict(obj.get("usage")))

    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """Parse OpenAI SSE and concatenate delta.content chunks."""
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        text, usage = collect_openai_stream(
            lines("/chat/completions", openai_stream_payload(payload), headers),
            on_chunk=on_chunk,
        )
        return ModelResponse(text=text, backend=self.name, usage=usage)


class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"

    def __init__(
        self,
        options: BackendOptions,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(options)
        self.anthropic_version = anthropic_version

    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        """Call the Anthropic-compatible messages endpoint."""
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
                raise ProviderResponseError(f"无法解析 Anthropic-compatible 响应: {_response_preview(obj)}") from exc
            if text or attempt > 0 or not _anthropic_has_thinking_without_text(obj):
                break
        if not text:
            raise ProviderResponseError(f"Anthropic-compatible 响应没有文本内容: {_response_preview(obj)}")
        return ModelResponse(text=text, backend=self.name, usage=usage_dict(obj.get("usage")))

    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """Parse Anthropic SSE and fall back once when the stream has no visible text."""
        for attempt in range(2):
            text, usage = self._stream_text_once(payload, headers, on_chunk)
            if text or attempt > 0:
                break
        if not text:
            text, usage = self._fallback_non_stream_text(payload, headers)
            if text and on_chunk is not None:
                on_chunk(text)
        if not text:
            raise ProviderResponseError("Anthropic-compatible 流式响应没有文本内容")
        return ModelResponse(text=text, backend=self.name, usage=usage)

    def _stream_text_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None,
    ) -> tuple[str, dict[str, Any]]:
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        return collect_anthropic_stream(lines("/v1/messages", payload, headers), on_chunk=on_chunk)

    def _fallback_non_stream_text(self, payload: dict[str, Any], headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
        fallback_payload = dict(payload)
        fallback_payload.pop("stream", None)
        try:
            obj = self.request_json("/v1/messages", fallback_payload, headers)
            return _anthropic_text_from_response(obj), usage_dict(obj.get("usage"))
        except ProviderResponseError:
            raise
        except Exception as exc:
            raise ProviderResponseError(
                f"Anthropic-compatible 非流式兜底请求失败: {type(exc).__name__}: {exc}"
            ) from exc


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


def _anthropic_has_thinking_without_text(obj: dict[str, Any]) -> bool:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and "thinking" in part for part in parts)


def _response_preview(obj: object, *, max_chars: int = 1000) -> str:
    text = str(obj)
    return text if len(text) <= max_chars else text[:max_chars] + "... [truncated]"


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
