# LLM: Static required-file extraction keeps acceptance controllers thin and schema tolerant.
# 模块用途: 从任务文本里提取静态 Web 必需文件名，供父级 static_site_check 合并 required_files。

from __future__ import annotations

from typing import Any

from .required_file_terms import required_file_terms_from_text


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
    return static_required_files_from_texts([
        getattr(task, "goal", ""),
        getattr(task, "thought", ""),
        getattr(task, "description", ""),
        *(getattr(task, "acceptance_checks", []) or []),
    ])


# LLM: _dedupe preserves first-seen file order from user/task text.
# 函数用途: 去重静态文件名，保持任务文本里的出现顺序。
def _dedupe(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value not in items:
            items.append(value)
    return items
