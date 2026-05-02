from __future__ import annotations

"""LLM: gateway HTTP request helpers shared by OpenAI-compatible and Anthropic-compatible backends.

给人看的解释：
这里放的是两种模型后端共用的网络请求逻辑——POST 请求、SSE 流式读取、逐行 yield。
把 HTTP 传输细节抽出来，后端类就只需要关心自己的请求/响应格式。
"""

import json
import urllib.error
import urllib.request
from typing import Any, Callable


def post_json(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
) -> dict[str, Any]:
    """LLM: send a POST request and parse the JSON response.

    新手说明:
    把请求发出去，再把模型返回的内容变成 Python 能继续处理的字典。
    如果 API Key 为空会直接报错，避免请求打到服务器后被拒。
    """

    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_base + path,
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def post_stream(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
) -> list[str]:
    """LLM: send a streaming POST request and collect all SSE data lines.

    新手说明:
    流式请求会持续返回数据行。这里把所有 data 行收集成列表返回，
    调用方再逐行解析具体内容。超时是两个 chunk 之间的间隔（socket timeout），不是总时长。
    """

    payload["stream"] = True

    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_base + path,
        data=data,
        method="POST",
        headers=headers,
    )
    data_lines: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    return data_lines


def post_stream_iter(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
):
    """LLM: send a streaming POST request and yield SSE data lines one by one.

    新手说明:
    与 post_stream 相同的网络逻辑，但用生成器逐行返回，
    调用方可以在收到每行时立即处理（比如逐字打印）。
    """

    payload["stream"] = True

    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_base + path,
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    yield line[5:].strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
