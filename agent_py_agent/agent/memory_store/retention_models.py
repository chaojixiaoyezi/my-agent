from __future__ import annotations

"""Memory v2 保留策略、计划动作和报告的数据合同。"""

# LLM: 所有 retention 决策必须由 v2 typed policy、结构化状态和文件事实产生；自然语言原因只用于展示。
# 模块用途: 定义默认天数、严格策略解析、幂等动作身份和管理员可读取的执行报告。

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

RETENTION_SCHEMA_VERSION = "my-agent.memory-retention.v2"
RETENTION_PLAN_SCHEMA_VERSION = "my-agent.memory-retention-plan.v2"

TASK_TERMINAL_STATUSES = frozenset(
    {
        "ABANDONED",
        "CANCELLED",
        "CHANNEL_ERROR",
        "DONE",
        "FAILED",
        "TAKEN_OVER",
        "TIMEOUT",
    }
)


# LLM: Policy 是 runtime 唯一 retention 配置合同；旧 key 必须经 migration 转换，不能在这里永久兼容。
# 类用途: 保存各类 owner 数据的保留天数、legal hold 和后台维护节流配置。
@dataclass(frozen=True)
class MemoryRetentionPolicy:
    conversation_days: int = 365
    audit_days: int = 180
    daily_days: int = 365
    tool_output_days_after_terminal: int = 30
    rejected_candidate_days: int = 30
    curator_run_days: int = 90
    compact_days: int = 365
    completed_task_days: int = 365
    cache_days: int = 30
    tmp_days: int = 7
    trash_days: int = 30
    subagent_scratch_days: int = 30
    legal_hold: bool = False
    legal_hold_task_ids: tuple[str, ...] = ()
    maintenance_enabled: bool = True
    maintenance_interval_seconds: int = 86_400
    schema_version: str = RETENTION_SCHEMA_VERSION

    # LLM: 缺字段、错类型、负值或旧 schema 都必须失败，避免默认值掩盖损坏/未迁移策略。
    # 函数用途: 从 retention.json 严格恢复一份可执行策略。
    @classmethod
    def from_payload(cls, payload: object) -> MemoryRetentionPolicy:
        if not isinstance(payload, dict):
            raise ValueError("retention policy must be a JSON object")
        if payload.get("schema_version") != RETENTION_SCHEMA_VERSION:
            raise ValueError("retention policy requires Memory v2 migration")
        day_fields = (
            "conversation_days",
            "audit_days",
            "daily_days",
            "tool_output_days_after_terminal",
            "rejected_candidate_days",
            "curator_run_days",
            "compact_days",
            "completed_task_days",
            "cache_days",
            "tmp_days",
            "trash_days",
            "subagent_scratch_days",
        )
        values = {
            field_name: _strict_nonnegative_int(payload, field_name)
            for field_name in day_fields
        }
        legal_hold = _strict_bool(payload, "legal_hold")
        maintenance_enabled = _strict_bool(payload, "maintenance_enabled")
        interval = _strict_nonnegative_int(payload, "maintenance_interval_seconds")
        task_ids = payload.get("legal_hold_task_ids")
        if not isinstance(task_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in task_ids
        ):
            raise ValueError("legal_hold_task_ids must be an array of non-empty strings")
        return cls(
            **values,
            legal_hold=legal_hold,
            legal_hold_task_ids=tuple(dict.fromkeys(item.strip() for item in task_ids)),
            maintenance_enabled=maintenance_enabled,
            maintenance_interval_seconds=interval,
        )

    # LLM: 持久化字段名是管理员配置协议；中文解释放 CLI/文档，不改机器 key。
    # 函数用途: 生成可写入 retention.json 的规范对象。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["legal_hold_task_ids"] = list(self.legal_hold_task_ids)
        return payload


# LLM: error_code 承担自动化控制语义，message 只提供人类诊断，禁止调用方解析 message。
# 类用途: 表示策略、状态或执行阶段的关闭式失败。
@dataclass(frozen=True)
class MemoryRetentionError:
    error_code: str
    path: str
    message: str

    # LLM: 输出只含稳定字段，不包含被删除文件正文。
    # 函数用途: 将错误放入 CLI JSON 和审计事件。
    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# LLM: Action 绑定精确路径、状态/fingerprint 和 cutoff；apply 不得用 category 重新猜目标。
# 类用途: 描述一次可重验证的删除、移入回收站或候选清理动作。
@dataclass(frozen=True)
class MemoryRetentionAction:
    action_id: str
    category: str
    operation: str
    path: Path
    reason: str
    status: str = "planned"
    destination: Path | None = None
    authority_path: Path | None = None
    authority_status: str = ""
    authority_fingerprint: str = ""
    source_fingerprint: str = ""
    record_id: str = ""
    cutoff_timestamp: float | None = None
    related_paths: tuple[Path, ...] = ()

    # LLM: 报告不得包含候选/对话正文；related_paths 只暴露管理员本地路径。
    # 函数用途: 将一条计划或结果转成 JSON 友好对象。
    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action_id": self.action_id,
            "category": self.category,
            "operation": self.operation,
            "path": str(self.path),
            "reason": self.reason,
            "status": self.status,
        }
        if self.destination is not None:
            payload["destination"] = str(self.destination)
        if self.authority_path is not None:
            payload["authority_path"] = str(self.authority_path)
        if self.authority_status:
            payload["authority_status"] = self.authority_status
        if self.record_id:
            payload["record_id"] = self.record_id
        if self.related_paths:
            payload["related_paths"] = [str(path) for path in self.related_paths]
        return payload


# LLM: Report 同时表达 dry-run、apply、legal hold 和错误；applied 不能等同于 ok。
# 类用途: 返回一轮 retention 规划或执行的完整无正文结果。
@dataclass(frozen=True)
class MemoryRetentionReport:
    applied: bool
    actions: tuple[MemoryRetentionAction, ...]
    errors: tuple[MemoryRetentionError, ...] = field(default_factory=tuple)
    legal_hold: bool = False
    policy_fingerprint: str = ""
    schema_version: str = RETENTION_PLAN_SCHEMA_VERSION

    # LLM: 任一策略/状态/动作错误都使报告不成功；legal hold 是安全停止而非错误。
    # 函数用途: 告诉 CLI 是否可把本轮视为完整成功。
    @property
    def ok(self) -> bool:
        return not self.errors

    # LLM: legacy callers may display load_errors, but authority remains typed errors and同一 Service；这不是第二套错误状态。
    # 函数用途: 为现有 home maintenance/doctor 调用方提供只读错误投影。
    @property
    def load_errors(self) -> tuple[dict[str, str], ...]:
        return tuple(error.to_dict() for error in self.errors)

    # LLM: JSON 输出保持 action/error 结构，不能降成无法机器判断的文本列表。
    # 函数用途: 生成 CLI、doctor 和测试共用报告。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "applied": self.applied,
            "legal_hold": self.legal_hold,
            "policy_fingerprint": self.policy_fingerprint,
            "actions": [action.to_dict() for action in self.actions],
            "errors": [error.to_dict() for error in self.errors],
            "load_errors": [error.to_dict() for error in self.errors],
        }


# LLM: action_id 的输入只含结构化目标和 cutoff，reason 文案不影响幂等身份。
# 函数用途: 为一次 retention 目标生成稳定编号。
def retention_action_id(
    *,
    category: str,
    operation: str,
    path: Path,
    record_id: str = "",
    cutoff_timestamp: float | None = None,
) -> str:
    material = json.dumps(
        {
            "category": category,
            "operation": operation,
            "path": str(path.resolve(strict=False)),
            "record_id": record_id,
            "cutoff_timestamp": cutoff_timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "retention-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: bool 是 int 的子类，必须显式拒绝；零表示管理员关闭该类别自动删除。
# 函数用途: 严格读取一个非负整数配置字段。
def _strict_nonnegative_int(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


# LLM: 配置布尔值不接受字符串 true/false，防止损坏配置被静默纠正。
# 函数用途: 严格读取一个布尔配置字段。
def _strict_bool(payload: dict[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


__all__ = [
    "RETENTION_PLAN_SCHEMA_VERSION",
    "RETENTION_SCHEMA_VERSION",
    "TASK_TERMINAL_STATUSES",
    "MemoryRetentionAction",
    "MemoryRetentionError",
    "MemoryRetentionPolicy",
    "MemoryRetentionReport",
    "retention_action_id",
]
