# LLM: Real run review rendering converts structured review snapshots into human audit Markdown.
# 模块用途: 渲染真实运行复盘报告，避免核心扫描文件同时承担展示逻辑。

from __future__ import annotations

from .real_run_review_models import RealRunReview


# LLM: render_real_run_review_markdown renders a compact human audit report from structured review data.
# 函数用途: 生成用户可读复盘文档；表格内容来自 RealRunReview 字段，不重新判断。
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


# LLM: _cluster_row keeps cluster Markdown formatting isolated from report assembly.
# 函数用途: 渲染单行失败聚类表格，避免主函数拼接过长。
def _cluster_row(cluster) -> str:
    return (
        f"| {cluster.tag} | {cluster.priority} | {cluster.count} | "
        f"{', '.join(cluster.run_ids)} | {', '.join(cluster.first_failure_codes)} |"
    )


# LLM: _record_table_header returns the static Markdown block for record rows.
# 函数用途: 提供每轮摘要表头，减少渲染函数内的固定文本。
def _record_table_header() -> list[str]:
    return [
        "",
        "## 每轮摘要",
        "",
        "| run_id | status | stage | first_failure | tags | evidence_refs |",
        "|---|---|---|---|---|---|",
    ]


__all__ = ["render_real_run_review_markdown"]
