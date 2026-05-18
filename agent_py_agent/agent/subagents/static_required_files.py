# LLM: Static required-file extraction keeps acceptance controllers thin and schema tolerant.
# 模块用途: 从任务文本里提取静态 Web 必需文件名，供父级 static_site_check 合并 required_files。

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .required_file_terms import (
    labeled_required_file_terms_from_text,
    required_file_terms_from_text,
)

_STATIC_FILE_SUFFIXES = {".html", ".htm", ".css", ".js"}
_REQUIRED_DOM_IDS_RE = re.compile(r"\brequired_dom_ids?\s*[:=]\s*([^\n。；;]+)", re.IGNORECASE)


# LLM: static_required_files_from_texts merges generic and labeled task-contract refs without guessing broad prose.
# 函数用途: 从任务目标、验收条件等文本里提取 index.html/style.css/app.js 这类必需文件名，并支持“必需文件:”等标签化合同。
def static_required_files_from_texts(texts: list[object]) -> list[str]:
    matches = [
        match
        for value in texts
        for match in [
            *required_file_terms_from_text(str(value or ""), extensions=r"html?|css|js"),
            *labeled_required_file_terms_from_text(str(value or ""), extensions=r"html?|css|js"),
        ]
    ]
    return _dedupe(matches)[:50]


# LLM: required_static_dom_ids_from_texts extracts explicit DOM-id contracts only.
# 函数用途: 从 `required_dom_ids: a, b` 这类结构化验收文本提取业务区域 id；普通自然语言不猜。
def required_static_dom_ids_from_texts(texts: list[object]) -> list[str]:
    ids: list[str] = []
    for value in texts:
        for match in _REQUIRED_DOM_IDS_RE.findall(str(value or "")):
            ids.extend(_dom_id_items(match))
    return _dedupe(ids)[:50]


# LLM: required_static_files_for_task reads runtime static contracts from task attributes only.
# 函数用途: 从 task.attributes.required_files 读取静态站点必需文件；不解析 goal/acceptance 文本。
def required_static_files_for_task(task: Any) -> list[str]:
    files = _string_list(_task_attributes(task).get("required_files"))
    return _scope_to_allowed_write_files(files, getattr(task, "allowed_write_roots", []) or [])


# LLM: required_static_dom_ids_for_task reads explicit DOM contracts from task attributes only.
# 函数用途: 从 task.attributes.required_dom_ids 读取业务区域 id，交给父级 static_site_check 做机器验收。
def required_static_dom_ids_for_task(task: Any) -> list[str]:
    return _dedupe(_string_list(_task_attributes(task).get("required_dom_ids")))[:50]


# LLM: _task_attributes normalizes task attributes for runtime contract reads.
# 函数用途: 读取 task.attributes 字典；缺失或类型不对时返回空，避免从文本兜底。
def _task_attributes(task: Any) -> dict[str, Any]:
    attributes = getattr(task, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


# LLM: static_site_root_hints_for_task exposes write-root refs to parent static-site acceptance.
# 函数用途: 子代理漏写 artifacts 时，父级可用 allowed_write_roots 找到正确任务产物目录，而不是猜全局 deliverables。
def static_site_root_hints_for_task(task: Any) -> list[str]:
    hints: list[str] = []
    for value in getattr(task, "allowed_write_roots", []) or []:
        raw = str(value or "").strip()
        if raw and raw not in hints:
            hints.append(raw)
    return hints


# LLM: _scope_to_allowed_write_files prevents child acceptance from inheriting sibling deliverables.
# 函数用途: 如果当前子任务的 allowed_write_roots 明确是具体静态文件，只验收这些文件，不扫父级其它 sibling 文件。
def _scope_to_allowed_write_files(files: list[str], allowed_write_roots: list[object]) -> list[str]:
    allowed = _allowed_static_file_names(allowed_write_roots)
    if not allowed:
        return files
    scoped = [item for item in files if _matches_allowed_file(item, allowed)]
    return scoped or files


# LLM: _allowed_static_file_names extracts literal output filenames from file-level write roots.
# 函数用途: 从 allowed_write_roots 里找 `.../index.html` 这类具体文件；目录授权不参与收窄。
def _allowed_static_file_names(values: list[object]) -> set[str]:
    names: set[str] = set()
    for value in values:
        path = Path(str(value or "").strip().replace("\\", "/"))
        if path.suffix.lower() not in _STATIC_FILE_SUFFIXES:
            continue
        names.add(path.name)
        parts = [part for part in path.parts if part not in {"", "."}]
        if len(parts) >= 2:
            names.add("/".join(parts[-2:]))
    return names


# LLM: _matches_allowed_file compares task-extracted relative filenames to allowed concrete outputs.
# 函数用途: 支持 `index1.html` 和 `artifacts/index1.html` 两种写法都能匹配同一个具体产物。
def _matches_allowed_file(item: str, allowed: set[str]) -> bool:
    normalized = str(item or "").replace("\\", "/").lstrip("./")
    return normalized in allowed or Path(normalized).name in allowed


# LLM: _dom_id_items tokenizes a structured required_dom_ids line.
# 函数用途: 按逗号/空白切分 DOM id，并只保留常见 HTML id 字符。
def _dom_id_items(value: str) -> list[str]:
    items: list[str] = []
    for raw in re.split(r"[\s,，]+", value):
        text = raw.strip()
        if text and all(ch.isalnum() or ch in {"-", "_", ":"} for ch in text):
            items.append(text)
    return items


# LLM: _string_list normalizes structured list/scalar fields without splitting prose.
# 函数用途: 读取 attributes 中的列表或单值；不会按逗号/空白拆普通句子。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


# LLM: _dedupe preserves first-seen file order from user/task text.
# 函数用途: 去重静态文件名，保持任务文本里的出现顺序。
def _dedupe(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value not in items:
            items.append(value)
    return items
