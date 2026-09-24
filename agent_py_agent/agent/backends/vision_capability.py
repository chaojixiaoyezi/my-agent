# LLM: 视觉能力事实的唯一来源模块。事实只由结构化结果决定：正确颜色的 tool_use → supported；供应商对图片块的 typed 拒绝
#   （ProviderRequestRejectedError）→ unsupported；答错/不调用工具 → inconclusive（有界重试、不缓存）；工具能力探针未证明原生
#   工具 → unavailable（不缓存）；网络/额度/认证错误（ProviderRecoverableError/ProviderConfigurationError）原样上抛、不缓存。
#   缓存是进程级、按 provider endpoint + 模型 + api_base 键控、同键单飞，放在 owner Agent 之外（owner Agent 空闲 60s 会被释放）。
#   不落盘、不读模型自述、不按模型名猜；每个键最多一次真实请求（约几十 token 加一张 8×8 图）。
# 模块用途: 判断当前模型到底能不能看图，供含图历史压缩决定走归档引用还是随图摘要。
from __future__ import annotations

import base64
import secrets
import struct
import threading
import zlib
from dataclasses import dataclass

from ..tooling.runtime_contracts import ToolChoice
from .errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderRequestRejectedError,
)

VISION_SUPPORTED = "supported"
VISION_UNSUPPORTED = "unsupported"
VISION_INCONCLUSIVE = "inconclusive"
VISION_UNAVAILABLE = "unavailable"

# 探针用的四种纯色；模型必须用结构化工具参数回答其中一个，文字回答不算。
_PROBE_COLORS = {"red": (255, 0, 0), "green": (0, 160, 0), "blue": (0, 0, 255), "yellow": (255, 220, 0)}
_PROBE_TOOL_NAME = "my_agent_vision_probe"
_PROBE_MAX_ATTEMPTS = 2
_PROBE_TOOL = {
    "name": _PROBE_TOOL_NAME,
    "description": "Internal vision capability probe with no host-side effect. Report the dominant color of the attached image.",
    "input_schema": {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": sorted(_PROBE_COLORS)}},
        "required": ["color"],
        "additionalProperties": False,
    },
}
_PROBE_PROMPT = (
    "Internal vision capability probe. Look at the attached 8x8 image and call "
    f"{_PROBE_TOOL_NAME} exactly once with its dominant color. Do not answer in prose."
)


# LLM: status 是机器判定字段；fact_source 直接进压缩 checkpoint 的 media_fact_source；evidence 只作日志/展示。
# 类用途: 一次视觉能力判定的结构化结果。
@dataclass(frozen=True)
class VisionCapability:
    status: str
    fact_source: str
    evidence: str = ""

    @property
    def supported(self) -> bool:
        return self.status == VISION_SUPPORTED


_CACHE: dict[tuple[str, str, str], VisionCapability] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}


# 函数用途: 生成一张 8×8 纯色 PNG 的字节（进程内构造，不落盘），供探针请求携带。
def probe_png_bytes(color: str) -> bytes:
    rgb = bytes(_PROBE_COLORS[color])
    raw = b"".join(b"\x00" + rgb * 8 for _ in range(8))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# LLM: 键只用后端实例上的结构化连接事实；换模型、换端点或换 api_base 自然换键，编辑档案后不需要手动清缓存。
# 函数用途: 计算进程级缓存键。
def vision_capability_cache_key(backend: object) -> tuple[str, str, str]:
    endpoint = getattr(backend, "_tool_endpoint", None)
    try:
        endpoint_value = str(endpoint()) if callable(endpoint) else ""
    except Exception:  # noqa: BLE001 端点计算失败退回类型名，不影响事实判定
        endpoint_value = ""
    return (
        endpoint_value or type(backend).__name__,
        str(getattr(backend, "model", "") or getattr(backend, "model_name", "") or ""),
        str(getattr(backend, "api_base", "") or ""),
    )


# LLM: 只缓存 supported/unsupported 两种确定事实；同键并发只发一次探针（单飞）；调用方负责在同一模型绑定内调用。
# 函数用途: 取当前模型的视觉能力事实，必要时发一次结构化探针。
def resolve_vision_capability(backend: object) -> VisionCapability:
    key = vision_capability_cache_key(backend)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    if cached is not None:
        return cached
    with lock:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            return cached
        result = probe_vision_capability(backend)
        if result.status in (VISION_SUPPORTED, VISION_UNSUPPORTED):
            with _CACHE_LOCK:
                _CACHE[key] = result
        return result


# 函数用途: 清空进程级缓存；只供测试和显式管理入口使用。
def reset_vision_capability_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _KEY_LOCKS.clear()


# LLM: 探针依赖原生工具调用，先要求 probe_tool_capability().native_supported；发一次请求（图 + 固定说明 + 单工具），
#   只认与真实颜色一致的 tool_use 为 supported。异常分类见模块头；本函数不缓存，缓存由 resolve_vision_capability 负责。
# 函数用途: 对一个后端实例执行一次真实的视觉能力探针。
def probe_vision_capability(backend: object) -> VisionCapability:
    tool_probe = getattr(backend, "probe_tool_capability", None)
    if not callable(tool_probe):
        return VisionCapability(VISION_UNAVAILABLE, "probe_unavailable", "no_tool_capability_probe")
    if not bool(getattr(tool_probe(), "native_supported", False)):
        return VisionCapability(VISION_UNAVAILABLE, "probe_unavailable", "native_tools_unsupported")
    evidence = "probe_no_attempt"
    for attempt in range(1, _PROBE_MAX_ATTEMPTS + 1):
        color = secrets.choice(sorted(_PROBE_COLORS))
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(probe_png_bytes(color)).decode("ascii")}}
        message = {"role": "user", "content": [{"type": "text", "text": _PROBE_PROMPT}, image]}
        try:
            response = backend.generate(
                _PROBE_PROMPT, tools=[dict(_PROBE_TOOL)], tool_choice=ToolChoice.auto("vision_capability_probe"),
                messages=[message],
            )
        except ProviderRequestRejectedError as exc:
            code = str(getattr(exc, "error_code", "") or "").strip().upper()
            return VisionCapability(VISION_UNSUPPORTED, "probe_unsupported", f"provider_rejected_media{':' + code if code else ''}")
        except (ProviderRecoverableError, ProviderConfigurationError):
            raise
        except Exception as exc:  # noqa: BLE001 本地确定性异常（解析/协议）重试无意义，记类型后按不可用处理
            return VisionCapability(VISION_UNAVAILABLE, "probe_unavailable", f"live_probe_failed:{type(exc).__name__}")
        answers = [
            str(block["input"].get("color") or "")
            for block in (getattr(response, "tool_use_blocks", None) or ())
            if isinstance(block, dict) and block.get("name") == _PROBE_TOOL_NAME and isinstance(block.get("input"), dict)
        ]
        if answers and answers[0] == color:
            return VisionCapability(VISION_SUPPORTED, "probe_supported", f"probe_color_match:attempt_{attempt}")
        evidence = "probe_wrong_color" if answers else "probe_no_tool_call"
    return VisionCapability(VISION_INCONCLUSIVE, "probe_inconclusive", evidence)
