# LLM: Static-site validation record rendering is isolated from scanner logic for size and reuse.
# 模块用途: 把静态站点验收结果转成 TestExecutionRecord，并生成短失败摘要。

from __future__ import annotations

from typing import Any

from .execution_executor_helpers import _test_name, _utc_now_iso
from .execution_records import TestExecutionRecord


# LLM: static_site_record converts validator facts into the common TestExecutionRecord contract.
# 函数用途: 构造 static_site_check 执行记录；失败原因放摘要，详细列表放 validation_result。
def static_site_record(
    test: dict[str, Any],
    result: Any,
    *,
    executed: bool,
    error: str,
) -> TestExecutionRecord:
    return TestExecutionRecord(
        test_name=_test_name(test),
        executed=executed,
        exit_code=0 if result.ok and executed else 1,
        executed_at=_utc_now_iso(),
        error=error,
        validation_method="static_site_check",
        validation_result=result.to_dict(),
    )


# LLM: static_site_failure_summary keeps top-level errors concise and leaves details in validation_result.
# 函数用途: 生成父级看板易读的静态站点失败摘要。
def static_site_failure_summary(result: Any) -> str:
    parts: list[str] = []
    for field in (
        "missing_required_files",
        "placeholder_hits",
        "broken_local_refs",
        "html_structure_hits",
        "inert_control_hits",
        "form_binding_hits",
        "missing_dom_id_hits",
        "missing_js_api_hits",
    ):
        values = getattr(result, field, [])
        if values:
            parts.append(f"{field}={len(values)}")
    return "; ".join(parts)


__all__ = ["static_site_failure_summary", "static_site_record"]
