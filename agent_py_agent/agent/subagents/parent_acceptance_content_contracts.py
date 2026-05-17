# LLM: Parent acceptance content contracts are shared by preflight, manual execution, and findings.
# 模块用途: 把 task 里的普通文件内容验收字段打包，避免多个父级验收入口重复散装参数。

from __future__ import annotations

from typing import Any

from .required_content_lines import (
    required_content_lines_by_file_for_task,
    required_content_lines_for_task,
)


# LLM: content_contract_args_for_task returns prepare_test_items kwargs for plain-file content checks.
# 函数用途: 给父级验收测试准备阶段统一传入全局内容行和 per-file 内容映射。
def content_contract_args_for_task(task: Any) -> dict[str, object]:
    return {
        "required_content_lines": required_content_lines_for_task(task),
        "required_content_files": required_content_lines_by_file_for_task(task),
    }
