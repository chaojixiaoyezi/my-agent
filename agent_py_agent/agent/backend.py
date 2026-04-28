from __future__ import annotations

"""模型后端适配层。

这一层的作用很像“翻译器”：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class ModelResponse:
    """统一的模型返回结果。

    不管底层用的是哪个模型厂商，最后都整理成这一个结构，
    这样上层逻辑就不用关心返回格式差异。
    """

    text: str
    backend: str


class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    def generate(self, prompt: str) -> ModelResponse:
        raise NotImplementedError


class EchoBackend(BaseBackend):
    """本地回声后端。

    这个后端不会真的请求外部模型，适合：
    - 离线开发
    - CLI 冒烟测试
    - 验证 prompt 拼装和主循环有没有跑通
    """

    name = "echo"

    def generate(self, prompt: str) -> ModelResponse:
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        if "# User Task" in prompt:
            task = prompt.split("# User Task", 1)[-1].strip()
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
        *,
        api_base: str,
        api_key: str,
        model_name: str,
        request_timeout: int = 60,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.request_timeout = int(request_timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)

    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """发送 POST 请求并把结果按 JSON 解析。

        大白话就是：把请求发出去，再把模型返回的内容变成 Python 能继续处理的字典。
        """

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.api_base + path,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"

    def generate(self, prompt: str) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        obj = self.request_json(
            "/chat/completions",
            payload,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            text = obj["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"无法解析 OpenAI-compatible 响应: {obj}") from exc
        return ModelResponse(text=text, backend=self.name)


class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"

    def __init__(self, *, anthropic_version: str = "2023-06-01", **kwargs: Any):
        super().__init__(**kwargs)
        self.anthropic_version = anthropic_version

    def generate(self, prompt: str) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        obj = self.request_json(
            "/v1/messages",
            payload,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": self.anthropic_version,
            },
        )
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


def get_backend(name: str, config: Any | None = None) -> BaseBackend:
    """根据配置创建后端实例。

    说白了，这里就是把配置里的字符串名字，变成真正可用的后端对象。
    """

    if name == "echo":
        return EchoBackend()
    if config is None:
        raise ValueError("真实模型后端需要传入 config。")

    common = dict(
        api_base=config.api_base,
        api_key=config.api_key,
        model_name=config.model_name,
        request_timeout=config.request_timeout,
        max_tokens=config.max_tokens,
        temperature=float(config.temperature),
    )

    if name == "openai_compatible":
        return OpenAICompatibleBackend(**common)
    if name == "anthropic_compatible":
        return AnthropicCompatibleBackend(
            **common,
            anthropic_version=config.anthropic_version,
        )

    raise ValueError(
        "未知模型后端: %s。当前内置 echo / openai_compatible / anthropic_compatible。"
        % name
    )
