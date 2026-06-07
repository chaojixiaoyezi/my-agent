
from __future__ import annotations

"""query helpers for memory raw archive, hook snapshots, and resume evidence.

新手说明:
这个包只做"找线索"和"整理恢复依据"。
命令怎么打印放在 `rendering_adapter.py`，这样查询逻辑可以单独测试，也不会把 CLI 文件堆大。
CLI 和 runtime 从本包导入当前 archive/query API。
"""

from .archive_helpers import (
    _append_run_id,
    _archive_search_text,
    _created_at_sort,
    _dedupe_strings,
    _is_date_only,
)
from .archive_io import (
    _archive_files,
    _gateway_terminal_request_path,
    _normalize_archive_record,
    _read_archive_file,
)
from .query_logic import (
    ArchiveFilterOptions,
    ArchiveQueryRequest,
    ArchiveQueryResponse,
    RawArchiveCollectOptions,
    ResumeContext,
    ResumeGuidanceRequest,
    apply_filters,
    archive_filters_from_args,
    build_resume_guidance,
    collect_archive_records,
    collect_gateway_payloads,
    collect_raw_archive_records,
    collect_resume_task_ids,
    collect_task_payloads,
    evaluate_filters,
    execute_archive_query,
    filter_archive_records,
    filter_by_fields,
    filter_by_level,
    filter_by_query_text,
    filter_by_time_window,
    local_hit_payload,
    paginate_records,
    resume_local_query,
    strip_sort_keys,
)
from .rendering_adapter import (
    format_archive_record,
    format_archive_records_table,
    format_query_response_json,
    render_resume_guidance,
)

__all__ = [
    "ArchiveFilterOptions",
    "ArchiveQueryRequest",
    "ArchiveQueryResponse",
    "RawArchiveCollectOptions",
    "ResumeContext",
    "ResumeGuidanceRequest",
    "_append_run_id",
    "_archive_files",
    "_archive_search_text",
    "_created_at_sort",
    "_dedupe_strings",
    "_gateway_terminal_request_path",
    "_is_date_only",
    "_normalize_archive_record",
    "_read_archive_file",
    "apply_filters",
    "archive_filters_from_args",
    "build_resume_guidance",
    "collect_archive_records",
    "collect_gateway_payloads",
    "collect_raw_archive_records",
    "collect_resume_task_ids",
    "collect_task_payloads",
    "evaluate_filters",
    "execute_archive_query",
    "filter_by_fields",
    "filter_by_level",
    "filter_by_query_text",
    "filter_by_time_window",
    "filter_archive_records",
    "format_archive_record",
    "format_archive_records_table",
    "format_query_response_json",
    "local_hit_payload",
    "paginate_records",
    "render_resume_guidance",
    "resume_local_query",
    "strip_sort_keys",
]
