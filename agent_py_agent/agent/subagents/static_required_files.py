# LLM: Static required-file extraction keeps acceptance controllers thin and schema tolerant.
# 模块用途: 从任务文本里提取静态 Web 必需文件名，供父级 static_site_check 合并 required_files。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .required_file_terms import required_file_terms_from_text

_STATIC_FILE_SUFFIXES = {".html", ".htm", ".css", ".js"}


# LLM: static_required_files_from_texts extracts small static-web file expectations from human/task text.
# 函数用途: 从任务目标、验收条件等文本里提取 index.html/style.css/app.js 这类必需文件名。
def static_required_files_from_texts(texts: list[object]) -> list[str]:
    matches = [
        match
        for value in texts
        for match in required_file_terms_from_text(str(value or ""), extensions=r"html?|css|js")
    ]
    return _dedupe(matches)[:50]


# LLM: required_static_files_for_task centralizes task text fields used by parent acceptance.
# 函数用途: 从 task 的目标、思考、说明和验收条件里抽取静态站点必需文件；不读取任何产物正文。
def required_static_files_for_task(task: Any) -> list[str]:
    files = static_required_files_from_texts([
        getattr(task, "goal", ""),
        getattr(task, "thought", ""),
        getattr(task, "description", ""),
        *(getattr(task, "acceptance_checks", []) or []),
    ])
    return _scope_to_allowed_write_files(files, getattr(task, "allowed_write_roots", []) or [])


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


# LLM: _dedupe preserves first-seen file order from user/task text.
# 函数用途: 去重静态文件名，保持任务文本里的出现顺序。
def _dedupe(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value not in items:
            items.append(value)
    return items
