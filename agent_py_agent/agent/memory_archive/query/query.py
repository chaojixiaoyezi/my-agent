
from __future__ import annotations

"""thin entry point for memory archive query operations.

新手说明:
这个文件是查询包的入口点，只做组合和导出，不含业务逻辑。
业务逻辑分散在 query_models、query_service、filter_policy、rendering_adapter 中。

命令怎么打印放在 rendering_adapter.py，这样查询逻辑可以单独测试，也不会把 CLI 文件堆大。
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
from .filter_policy import (
    ArchiveFilterOptions,
    evaluate_filters,
    filter_by_fields,
    filter_by_level,
    filter_by_query_text,
    filter_by_time_window,
)

# Public query imports; resume guidance lives in a bundle module.
from .query_logic import (
    archive_filters_from_args,
    collect_archive_records,
    collect_resume_task_ids,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
    strip_sort_keys,
)
from .query_models import (
    ArchiveQueryRequest,
    ArchiveQueryResponse,
    ResumeContext,
    paginate_records,
)
from .query_service import (
    RawArchiveCollectOptions,
    apply_filters,
    collect_gateway_payloads,
    collect_raw_archive_records,
    collect_task_payloads,
    execute_archive_query,
)
from .rendering_adapter import (
    format_archive_record,
    format_archive_records_table,
    format_query_response_json,
    render_resume_guidance,
)
from .resume_guidance import ResumeGuidanceRequest, build_resume_guidance

__all__ = [
    # query_models
    "ArchiveQueryRequest",
    "ArchiveQueryResponse",
    "ArchiveFilterOptions",
    "RawArchiveCollectOptions",
    "ResumeGuidanceRequest",
    "ResumeContext",
    "paginate_records",
    # query_service
    "apply_filters",
    "collect_archive_records",
    "collect_gateway_payloads",
    "collect_resume_task_ids",
    "collect_task_payloads",
    "execute_archive_query",
    # filter_policy
    "evaluate_filters",
    "filter_by_fields",
    "filter_by_level",
    "filter_by_query_text",
    "filter_by_time_window",
    # rendering_adapter
    "format_archive_record",
    "format_archive_records_table",
    "format_query_response_json",
    "render_resume_guidance",
    # query_logic
    "archive_filters_from_args",
    "build_resume_guidance",
    "filter_archive_records",
    "local_hit_payload",
    "resume_local_query",
    "strip_sort_keys",
    # archive_helpers
    "_append_run_id",
    "_archive_search_text",
    "_created_at_sort",
    "_dedupe_strings",
    "_is_date_only",
    # archive_io
    "_archive_files",
    "_gateway_terminal_request_path",
    "_normalize_archive_record",
    "_read_archive_file",
]
