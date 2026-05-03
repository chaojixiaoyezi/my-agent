"""LLM: query facade — thin re-export from query/ subpackage.

给人看的解释：
这个文件是 query 的入口，只做转发，不做任何逻辑。
所有实现在 query/ 子目录下：query_models.py、query_service.py、filter_policy.py、rendering_adapter.py、query.py。
这样原来从 `memory_archive.query` 导入的地方仍然能用。
"""

from .query.query import (
    archive_filters_from_args,
    build_resume_guidance,
    collect_archive_records,
    collect_gateway_payloads,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
    strip_sort_keys,
)

__all__ = [
    "archive_filters_from_args",
    "build_resume_guidance",
    "collect_archive_records",
    "collect_gateway_payloads",
    "collect_resume_task_ids",
    "collect_task_payloads",
    "filter_archive_records",
    "local_hit_payload",
    "resume_local_query",
    "strip_sort_keys",
]
