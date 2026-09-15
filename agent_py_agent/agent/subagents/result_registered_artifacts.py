# LLM: 子代理收口从本 run 的工具产物账本构造交接投影；不解析回复、不补文件、不借用兄弟记录。
# 模块用途: 让自然语言完成的子代理也能交回真实文件路径，避免父级拿到空产物清单后重做。
from __future__ import annotations

from pathlib import Path

from ..artifacts.registry import (
    ARTIFACT_ROLE_METADATA_KEY,
    ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
    ArtifactRegistryRecord,
    latest_artifact_records_report,
)
from .models import SubAgentTask


# LLM: 只读取 canonical agent_run_workspace_dir 下的 no-follow registry；修改 task 内存投影，由既有收口事务保存。
# 函数用途: 从工具已落账的最新记录提取交付物；保留读取诊断，排除日志、删除记录和其它代理的产物。
def collect_registered_artifacts(task: SubAgentTask) -> list[dict[str, object]]:
    root = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not root:
        return []
    report = latest_artifact_records_report(Path(root))
    attrs = dict(getattr(task, "attributes", {}) or {})
    if report.errors:
        attrs["artifact_registry_read_errors"] = report.errors
    else:
        attrs.pop("artifact_registry_read_errors", None)
    records = [record for record in report.records.values() if record.run_id == task.id]
    observed_ids = {record.artifact_id for record in records}
    observed_paths = {record.path for record in records}
    previous = attrs.get("artifact_registry_refs")
    retained = [
        row for row in previous or []
        if isinstance(row, dict) and row.get("artifact_id") not in observed_ids and row.get("path") not in observed_paths
    ] if isinstance(previous, list) else []
    # 同一 run 的旧记录可能用不同 artifact_id 指向同一文件；最新删除不能被旧 ready 复活。
    latest_by_path: dict[str, ArtifactRegistryRecord] = {}
    for record in records:
        previous_record = latest_by_path.get(record.path)
        if previous_record is None or record.updated_at >= previous_record.updated_at:
            latest_by_path[record.path] = record
    ready = [record.to_dict() for record in latest_by_path.values() if _is_product_record(record)]
    # 这是原账本的投影，不重新登记或重新读取文件，也不把被删除的旧路径继续交给父级。
    if records or retained:
        attrs["artifact_registry_refs"] = [*retained, *ready]
    task.attributes = attrs
    ready_paths = [str(row["path"]) for row in ready]
    task.artifact_refs = list(dict.fromkeys([
        *[ref for ref in task.artifact_refs if ref not in observed_paths],
        *ready_paths,
    ]))
    return [
        {"path": row["path"], "kind": row["kind"], "artifact_id": row["artifact_id"], "registry_ref": row}
        for row in ready
    ]


# LLM: 只消费 registry 的结构化状态和保留角色；未知文件类型仍可交接，不按后缀或自然语言猜测。
# 函数用途: 区分可交付的文件记录与仅用于阅读诊断的工具输出归档。
def _is_product_record(record: ArtifactRegistryRecord) -> bool:
    return (
        record.status == "ready"
        and bool(record.path)
        and record.kind != "tool_output"
        and record.metadata.get(ARTIFACT_ROLE_METADATA_KEY) != ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE
    )
