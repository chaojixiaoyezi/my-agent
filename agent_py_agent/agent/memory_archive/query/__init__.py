# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""query helpers for memory raw archive, hook snapshots, and resume evidence.

新手说明:
这个包只做"找线索"和"整理恢复依据"。
命令怎么打印放在 `rendering_adapter.py`，这样查询逻辑可以单独测试，也不会把 CLI 文件堆大。
`ResumeGuidanceRequest` 从这里 re-export，方便 CLI 和 runtime 统一走 bundle 接口。
"""

from .query import (
    ResumeGuidanceRequest,
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
    "ResumeGuidanceRequest",
    "strip_sort_keys",
]
