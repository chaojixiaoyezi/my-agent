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

from .gateway_helpers import post_json, post_stream, post_stream_iter
from .stream_parsers import anthropic_stream_contents, openai_stream_contents


@dataclass
class ModelResponse:

    text: str
    backend: str


@dataclass(frozen=True)
class BackendOptions:
    """Connection and generation options shared by HTTP model backends."""

    api_base: str
    api_key: str
    model_name: str
    request_timeout: int = 60
    max_tokens: int = 1024
    temperature: float = 0.2
    stream_enabled: bool = True


class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    def generate(
        self, prompt: str, on_chunk: Callable[[str], None] | None = None
    ) -> ModelResponse:
        raise NotImplementedError


class EchoBackend(BaseBackend):

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
        options: BackendOptions | None = None,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        request_timeout: int | str = 60,
        max_tokens: int | str = 1024,
        temperature: float | str = 0.2,
        stream_enabled: bool = True,
    ):
        if options is None:
            if api_base is None or api_key is None or model_name is None:
                raise TypeError("HttpBackend requires BackendOptions or api_base/api_key/model_name")
            options = BackendOptions(
                api_base=str(api_base),
                api_key=str(api_key),
                model_name=str(model_name),
                request_timeout=int(request_timeout),
                max_tokens=int(max_tokens),
                temperature=float(temperature),
                stream_enabled=bool(stream_enabled),
            )
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

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(
            self.api_base,
            self.api_key,
            path,
            payload,
            headers,
            timeout=self.request_timeout,
        )

    def request_stream(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> list[str]:
        return post_stream(
            self.api_base,
            self.api_key,
            path,
            payload,
            headers,
            timeout=self.request_timeout,
        )

    def request_stream_iter(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ):
        yield from post_stream_iter(
            self.api_base,
            self.api_key,
            path,
            payload,
            headers,
            timeout=self.request_timeout,
        )


class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"

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


class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"

    def __init__(
        self,
        options: BackendOptions | None = None,
        *,
        anthropic_version: str = "2023-06-01",
        api_base: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        request_timeout: int | str = 60,
        max_tokens: int | str = 1024,
        temperature: float | str = 0.2,
        stream_enabled: bool = True,
    ):
        super().__init__(
            options,
            api_base=api_base,
            api_key=api_key,
            model_name=model_name,
            request_timeout=request_timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            stream_enabled=stream_enabled,
        )
        self.anthropic_version = anthropic_version

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
        obj = self.request_json("/v1/messages", payload, headers)
        try:
            parts = obj.get("content", [])
            text = "".join(
                part.get("text", "")
                for part in parts
                if part.get("type") in (None, "text")
            )
            if not text and "completion" in obj:
                text = obj["completion"]
        except Exception as exc:
            raise RuntimeError(f"无法解析 Anthropic-compatible 响应: {obj}") from exc
        if not text:
            raise RuntimeError(f"Anthropic-compatible 响应没有文本内容: {obj}")
        return ModelResponse(text=text, backend=self.name)

    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """流式解析 Anthropic SSE：监听 content_block_delta 事件拼接文本。"""
        # LLM: Anthropic SSE 用 event 行区分事件类型，data 行携带 JSON。
        # request_stream 已过滤 event 行，需从 data 行的 type 字段恢复事件类型。
        parts: list[str] = []
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        for text in anthropic_stream_contents(lines("/v1/messages", payload, headers)):
            parts.append(text)
            if on_chunk is not None:
                on_chunk(text)
        text = "".join(parts)
        if not text:
            raise RuntimeError("Anthropic-compatible 流式响应没有文本内容")
        return ModelResponse(text=text, backend=self.name)


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
