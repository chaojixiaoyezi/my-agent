# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Evidence file helpers for local log-analysis queries."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage.base import EvidenceRef, dict_to_model, stable_digest, utc_now


# LLM: evidence references use payload bundles so query-result files remain the fact source.
# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 QueryEvidencePayload 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryEvidencePayload 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class QueryEvidencePayload:
    # LLM: Evidence writes use this bundle for compatibility and code-size guardrails.
    query_id: str
    parameters: dict[str, Any]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    summary: dict[str, Any]


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _EvidenceRefPayloadRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _EvidenceRefPayloadRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _EvidenceRefPayloadRequest:
    # LLM: EvidenceRef metadata travels as one bundle to keep query evidence helpers small.
    params: QueryEvidencePayload
    path: Path
    digest: str
    created_at: str


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 LocalEvidenceStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 LocalEvidenceStore 的持久化入口，把路径、读写和查询操作集中到同一对象。
class LocalEvidenceStore:
    """Writes query evidence payloads under the local store root."""

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 write_query_result 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 write query result 相关记录，集中处理目标路径、格式化和状态更新。
    def write_query_result(
        self,
        *,
        params: QueryEvidencePayload | None = None,
        payload: QueryEvidencePayload | None = None,
        query_id: str = "",
        parameters: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        row_count: int = 0,
        truncated: bool = False,
        summary: dict[str, Any] | None = None,
    ) -> EvidenceRef:
        if params is None:
            params = payload or QueryEvidencePayload(
                query_id=str(query_id),
                parameters=dict(parameters or {}),
                rows=list(rows or []),
                row_count=int(row_count),
                truncated=bool(truncated),
                summary=dict(summary or {}),
            )
        evidence_id = params.query_id
        path = self.evidence_dir / f"{evidence_id}.json"
        evidence_dict = {
            "evidence_id": evidence_id,
            "kind": "query_result",
            "query_id": params.query_id,
            "created_at": utc_now(),
            "parameters": params.parameters,
            "row_count": params.row_count,
            "truncated": params.truncated,
            "summary": params.summary,
            "rows": params.rows,
        }
        encoded = json.dumps(evidence_dict, ensure_ascii=False, sort_keys=True, indent=2)
        path.write_text(encoded + "\n", encoding="utf-8")
        digest = stable_digest(evidence_dict)
        ref_request = _EvidenceRefPayloadRequest(params, path, digest, evidence_dict["created_at"])
        return dict_to_model(EvidenceRef, _query_evidence_ref_payload(ref_request))

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 read_json 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 read json 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def read_json(self, evidence_path: str | Path, *, max_bytes: int = 200_000) -> dict[str, Any]:
        path = Path(evidence_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > max_bytes:
            return {
                "path": str(path),
                "truncated": True,
                "size_bytes": path.stat().st_size,
            }
        return json.loads(path.read_text(encoding="utf-8"))


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _query_evidence_ref_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query evidence ref payload 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_evidence_ref_payload(request: _EvidenceRefPayloadRequest) -> dict[str, Any]:
    params = request.params
    return {
        "evidence_id": params.query_id,
        "kind": "query_result",
        "query_id": params.query_id,
        "uri": str(request.path),
        "path": str(request.path),
        "content_hash": request.digest,
        "sha256": request.digest,
        "row_count": params.row_count,
        "truncated": params.truncated,
        "created_at": request.created_at,
        "summary": f"query_result rows={params.row_count} truncated={params.truncated}",
        "metadata": {
            "evidence_path": str(request.path),
            "parameters": params.parameters,
            "summary": params.summary,
        },
    }
