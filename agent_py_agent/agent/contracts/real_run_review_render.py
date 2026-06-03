
from __future__ import annotations

from .real_run_review_models import RealRunReview


def render_real_run_review_markdown(review: RealRunReview) -> str:
    lines = [
        "# 真实运行复盘报告",
        "",
        "## 摘要",
        "",
        f"- total: {review.summary['total']}",
        f"- passed: {review.summary['passed']}",
        f"- failed: {review.summary['failed']}",
        f"- unknown: {review.summary['unknown']}",
        "",
        "## 失败模式聚类",
        "",
        "| tag | priority | count | runs | first_failure_codes |",
        "|---|---:|---:|---|---|",
    ]
    for cluster in review.clusters:
        lines.append(_cluster_row(cluster))
    lines.extend(_record_table_header())
    for record in review.records:
        lines.append(
            f"| {record.run_id} | {record.final_status} | {record.failure_stage} | "
            f"{record.first_failure_code} | {', '.join(record.root_cause_tags)} | "
            f"{', '.join(record.evidence_refs[:3])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _cluster_row(cluster) -> str:
    return (
        f"| {cluster.tag} | {cluster.priority} | {cluster.count} | "
        f"{', '.join(cluster.run_ids)} | {', '.join(cluster.first_failure_codes)} |"
    )


def _record_table_header() -> list[str]:
    return [
        "",
        "## 每轮摘要",
        "",
        "| run_id | status | stage | first_failure | tags | evidence_refs |",
        "|---|---|---|---|---|---|",
    ]


__all__ = ["render_real_run_review_markdown"]
