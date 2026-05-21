# LLM: Real run review models are refs-first payloads shared by review code and scripts.
# 模块用途: 定义真实运行复盘的记录、聚类和总报告模型，避免复盘逻辑文件膨胀。

from __future__ import annotations

from dataclasses import dataclass


# LLM: RealRunRecord is one row of the real-run review table and must stay refs-first.
# 类用途: 表示单个真实运行目录的结构化复盘结果，便于报告、聚类和后续失败样本转换。
@dataclass(frozen=True)
class RealRunRecord:
    run_id: str
    run_ref: str
    task_types: tuple[str, ...]
    final_status: str
    first_failure_code: str
    last_surface_code: str
    failure_stage: str
    root_cause_tags: tuple[str, ...]
    priority: str
    evidence_refs: tuple[str, ...]
    has_offline_regression_test: bool
    recommended_offline_test: str
    missing_artifact: bool = False
    empty_artifact: bool = False
    artifact_path_mismatch: bool = False
    tool_failed: bool = False
    tool_param_error: bool = False
    tool_result_format_error: bool = False
    repeated_tool_call: bool = False
    contract_acceptance_failed: bool = False
    dry_run_real_conflict: bool = False
    approval_anomaly: bool = False
    invalid_state_transition: bool = False
    context_contract_lost: bool = False

    # LLM: to_dict keeps reports stable and independent of dataclass internals.
    # 函数用途: 输出机器可读复盘行，写 JSONL/报告时统一走这里。
    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "run_ref": self.run_ref,
            "task_types": list(self.task_types),
            "final_status": self.final_status,
            "first_failure_code": self.first_failure_code,
            "last_surface_code": self.last_surface_code,
            "failure_stage": self.failure_stage,
            "root_cause_tags": list(self.root_cause_tags),
            "priority": self.priority,
            "evidence_refs": list(self.evidence_refs),
            "has_offline_regression_test": self.has_offline_regression_test,
            "recommended_offline_test": self.recommended_offline_test,
            "missing_artifact": self.missing_artifact,
            "empty_artifact": self.empty_artifact,
            "artifact_path_mismatch": self.artifact_path_mismatch,
            "tool_failed": self.tool_failed,
            "tool_param_error": self.tool_param_error,
            "tool_result_format_error": self.tool_result_format_error,
            "repeated_tool_call": self.repeated_tool_call,
            "contract_acceptance_failed": self.contract_acceptance_failed,
            "dry_run_real_conflict": self.dry_run_real_conflict,
            "approval_anomaly": self.approval_anomaly,
            "invalid_state_transition": self.invalid_state_transition,
            "context_contract_lost": self.context_contract_lost,
        }


# LLM: FailurePattern groups runs by one machine failure tag.
# 类用途: 表示同一失败标签影响了哪些 run，用于排序 P0/P1 修复队列。
@dataclass(frozen=True)
class FailurePattern:
    tag: str
    count: int
    priority: str
    run_ids: tuple[str, ...]
    first_failure_codes: tuple[str, ...]

    # LLM: to_dict keeps cluster output stable for scripts and docs.
    # 函数用途: 输出聚类统计行，方便 JSON 报告和 Markdown 表格共用。
    def to_dict(self) -> dict[str, object]:
        return {
            "tag": self.tag,
            "count": self.count,
            "priority": self.priority,
            "run_ids": list(self.run_ids),
            "first_failure_codes": list(self.first_failure_codes),
        }


# LLM: RealRunReview is the top-level review payload for a run tree.
# 类用途: 保存复盘摘要、每轮记录和失败聚类，供脚本落盘和最终汇报引用。
@dataclass(frozen=True)
class RealRunReview:
    summary: dict[str, int]
    records: tuple[RealRunRecord, ...]
    clusters: tuple[FailurePattern, ...]

    # LLM: to_dict keeps review snapshots replayable across scripts.
    # 函数用途: 把复盘报告转成纯 JSON 结构，避免调用方读取 dataclass。
    def to_dict(self) -> dict[str, object]:
        return {
            "summary": dict(self.summary),
            "records": [record.to_dict() for record in self.records],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


__all__ = ["FailurePattern", "RealRunRecord", "RealRunReview"]
