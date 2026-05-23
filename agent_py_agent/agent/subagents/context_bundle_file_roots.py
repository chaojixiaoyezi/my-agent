# LLM: Context-bundle file-root helpers keep product file contracts out of the main bundle builder.
# 模块用途: 处理 allowed_write_roots 中的具体文件授权，区分用户产物文件和内部运行文件。

from __future__ import annotations

import re
from pathlib import Path

from .models import SubAgentTask

_SAFE_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


# LLM: file_level_write_root_terms turns explicit product file grants into required file contracts.
# 函数用途: 真实 E2E 里父级常只传 `/.../index.html` 写入根；这里补出 `index.html`，避免 Context Gate 误挡。
def file_level_write_root_terms(task: SubAgentTask) -> list[str]:
    terms: list[str] = []
    task_dir = Path(str(getattr(task, "task_dir", "") or ""))
    for raw in getattr(task, "allowed_write_roots", []) or []:
        path = Path(str(raw or "").strip().replace("\\", "/"))
        if not is_contract_file_path(path) or is_internal_task_file(path, task_dir):
            continue
        append_file_root_term(terms, path.name)
        if len(path.parts) >= 2:
            append_file_root_term(terms, "/".join(path.parts[-2:]))
    return terms


# LLM: is_contract_file_path keeps directory roots out without a closed file-type enum.
# 函数用途: 只要结构化写入根是安全具体文件路径，就进入文件合同；不靠固定后缀表。
def is_contract_file_path(path: Path) -> bool:
    return bool(path.name and _SAFE_FILE_SUFFIX_RE.fullmatch(path.suffix.lower()))


# LLM: is_internal_task_file filters run-private output/checkpoint files from product contracts.
# 函数用途: 子代理自己的 task_dir/output.json 不是用户产物，不能因为可写就进入 required_files。
def is_internal_task_file(path: Path, task_dir: Path) -> bool:
    if not str(task_dir):
        return False
    try:
        return path.resolve().is_relative_to(task_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


# LLM: append_file_root_term preserves basename and scoped relative forms without duplicates.
# 函数用途: 让 `index.html` 和 `dir/index.html` 都能匹配不同自然语言写法。
def append_file_root_term(terms: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in terms:
        terms.append(text)
