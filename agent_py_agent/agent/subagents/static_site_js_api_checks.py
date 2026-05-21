# LLM: Static JS API checks compare inline calls with exported app methods without executing scripts.
# 模块用途: 静态检查 `app.method()` 调用是否有对应导出，避免生成站点按钮运行时报错。

from __future__ import annotations

import re


# LLM: missing_window_app_method_hits 是 agent_py_agent/agent/subagents/static_site_js_api_checks.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 missing window app method hits 相关的结构化数据、路径或 finding，供当前合同链路调用。
def missing_window_app_method_hits(script_text: str) -> list[str]:
    refs = _referenced_window_app_methods(script_text)
    if not refs:
        return []
    exported = _exported_window_app_methods(script_text)
    return [f"app.{name}" for name in sorted(refs - exported)]


# LLM: _referenced_window_app_methods 是 agent_py_agent/agent/subagents/static_site_js_api_checks.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 referenced window app methods 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _referenced_window_app_methods(script_text: str) -> set[str]:
    return {
        name
        for name in re.findall(r"\bapp\.([A-Za-z_$][\w$]*)\s*\(", script_text or "")
        if name not in {"addEventListener"}
    }


# LLM: _exported_window_app_methods 是 agent_py_agent/agent/subagents/static_site_js_api_checks.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 exported window app methods 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _exported_window_app_methods(script_text: str) -> set[str]:
    text = script_text or ""
    names = {match.group(1) for match in re.finditer(r"\bwindow\.app\.([A-Za-z_$][\w$]*)\s*=", text)}
    for body in re.findall(r"\bwindow\.app\s*=\s*\{(?P<body>.*?)\}\s*;", text, flags=re.DOTALL):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;\s*window\.app\s*=\s*app\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    return names


# LLM: _object_property_names 是 agent_py_agent/agent/subagents/static_site_js_api_checks.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 object property names 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _object_property_names(body: str) -> set[str]:
    names: set[str] = set()
    cleaned = re.sub(r"//.*?$|/\*.*?\*/", "", body or "", flags=re.MULTILINE | re.DOTALL)
    for chunk in cleaned.split(","):
        item = chunk.strip()
        if not item:
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*:", item)
        if match:
            names.add(match.group(1))
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*(?:\(|$)", item)
        if match:
            names.add(match.group(1))
    return names


__all__ = ["missing_window_app_method_hits"]
