# LLM: 结构化输出方式的唯一换算点。native 表示用协议自带的严格机制（OpenAI Chat 发 json_schema、Responses 发
#   text.format、Anthropic 用强制工具信封）；json_object 只适用于 OpenAI Chat 兼容接口：发 response_format json_object，
#   并把 schema 写进提示，输出仍由调用方的严格解析校验。方式来自 profile 显式声明（structured_output），auto 时只对
#   实测确认的供应商给默认（2026-09-26：DeepSeek 官方 OpenAI 兼容接口对 json_schema 返回 400、json_object 可用），其余
#   一律 native；绝不按报错文字或模型自述判断能力。改动须同步 openai_chat.generate_structured、model_provider_schema
#   与 test_structured_output_mode.py。
# 模块用途: 定义结构化输出方式、解析模型实际采用的方式，并生成 json_object 方式所需的提示前缀。
from __future__ import annotations

import json
from urllib.parse import urlsplit

STRUCTURED_OUTPUT_MODES = ("auto", "native", "json_object")
# 只有 OpenAI Chat 兼容接口有 json_object 这条退路；其它协议始终用自己的原生机制。
JSON_OBJECT_BACKENDS = frozenset({"openai_compatible"})
# 已知供应商的默认方式（只是优化，profile 可显式覆盖）。
_KNOWN_MODES = {("api.deepseek.com", "openai_compatible"): "json_object"}


# LLM: 大小写与首尾空白不敏感；空值视为 auto，不认识的值返回空串，由调用方决定报错或回默认。
# 函数用途: 把一个结构化输出方式写法规范成 STRUCTURED_OUTPUT_MODES 之一。
def normalize_structured_output(value: object) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in STRUCTURED_OUTPUT_MODES else ""


# LLM: 非 OpenAI Chat 协议总是 native；显式声明优先；auto 按 (接口域名, 协议) 查已知表，查不到为 native。
# 函数用途: 得到一个模型实际采用的结构化输出方式（native 或 json_object）。
def resolved_structured_output(declared: object, api_base: object, model_backend: object) -> str:
    backend = str(model_backend or "").strip().lower()
    if backend not in JSON_OBJECT_BACKENDS:
        return "native"
    mode = normalize_structured_output(declared) or "auto"
    if mode != "auto":
        return mode
    host = (urlsplit(str(api_base or "")).hostname or "").lower()
    return _KNOWN_MODES.get((host, backend), "native")


# LLM: 只在 json_object 方式下调用。供应商不执行 schema，所以把完整 schema 放在提示最前面；提示里含 “JSON”
#   也满足部分供应商对 json_object 的要求。这是给模型的输出说明，是否合规仍由调用方严格解析决定。
# 函数用途: 为 json_object 方式的结构化请求生成带 schema 的提示。
def json_object_prompt(prompt: str, response_schema: dict) -> str:
    schema = json.dumps(response_schema, ensure_ascii=False, sort_keys=True)
    return (
        "只输出一个 JSON 对象，必须符合下面的 JSON Schema；不要输出代码块、解释或任何其它文字。\n"
        f"JSON Schema：{schema}\n\n{prompt}"
    )


__all__ = [
    "JSON_OBJECT_BACKENDS",
    "STRUCTURED_OUTPUT_MODES",
    "json_object_prompt",
    "normalize_structured_output",
    "resolved_structured_output",
]
