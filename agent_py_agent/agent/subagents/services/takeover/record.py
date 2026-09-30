# LLM: 子代理接管边的唯一落账入口；SubAgentBaseService.record_takeover 授权后转调这里，显式接替、手动接管、领导权恢复
#   和接管 run 共用。已关闭来源（DONE/ABANDONED/CANCELLED）只追加 superseded_by 与一条 TakeoverRecord，终态不改写；
#   未关闭来源照旧转 TAKEN_OVER。保存后必须重新读取权威状态核对接替关系确实落盘，读不到就抛 TakeoverNotPersistedError，
#   且不写 TAKEOVER.md。审计投影：落盘核对通过后同步追加一条 subagent_takeover_recorded 事件，只供时间线展示，不是状态来源。
#   副作用：写 canonical 状态、TAKEOVER.md 与 LocalStore 审计事件。改动须同步 test_subagent_done_supersede 与接替回执。
# 模块用途: 记录“旧子代理由哪个新 run 接替”，并保证只有真正写进盘的接替才算成功。
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ...models import (
    TAKEOVER_DISPOSITION_SUPERSEDED,
    SubAgentTask,
    TakeoverRecord,
    TaskStatus,
    task_replacement_successor,
    task_takeover_disposition,
)


# LLM: 结构化异常：调用方只读 run_id/successor/disposition 映射成结构化失败状态，不解析 message。
# 类用途: 表示接替关系保存后在权威状态里读不到（被持久化边界还原或没有写进盘）。
class TakeoverNotPersistedError(RuntimeError):
    # LLM: 只保存结构化身份；message 仅供日志阅读。
    # 函数用途: 记下哪个 run 的哪条接替没有落盘。
    def __init__(self, run_id: str, successor: str, disposition: str) -> None:
        super().__init__(f"takeover of {run_id} by {successor} ({disposition}) was not persisted")
        self.run_id = run_id
        self.successor = successor
        self.disposition = disposition


# LLM: 调用方输入的冻结值；locked_files 只在未关闭来源转 TAKEN_OVER 时合并，已关闭来源不再持有锁。
# 类用途: 打包接替者、原因与锁文件，让落账函数保持少量参数。
@dataclass(frozen=True)
class TakeoverEdgeRequest:
    take_over_by: str
    reason: str
    locked_files: list[str] = field(default_factory=list)


# LLM: task 必须是刚通过授权门读取的权威记录。先按来源状态定处置，再写同一条 TakeoverRecord；保存后核对落盘，
#   未落盘时抛 TakeoverNotPersistedError，不写 TAKEOVER.md、不回报成功。落盘确认后追加审计事件（只读投影，失败不影响主链）。
#   副作用：保存 canonical 状态、写 TAKEOVER.md、追加 subagent_takeover_recorded 审计事件。
# 函数用途: 把一次接管或接替写进子代理状态，确认写进盘后再写接管说明文件和审计事件。
def record_takeover_edge(manager: Any, task: SubAgentTask, request: TakeoverEdgeRequest) -> TakeoverRecord:
    now = time.time()
    record = TakeoverRecord(
        id=manager._new_id("takeover"),
        run_id=task.id,
        take_over_by=request.take_over_by,
        reason=request.reason,
        locked_files=list(request.locked_files),
        previous_owner=task.owner,
        created_at=now,
    )
    disposition = task_takeover_disposition(task)
    task.takeover_records.append(record)
    if disposition == TAKEOVER_DISPOSITION_SUPERSEDED:
        task.superseded_by = request.take_over_by
    else:
        _apply_taken_over_fields(task, record)
    task.updated_at = now
    manager.save(task)
    _require_persisted(manager, task.id, (request.take_over_by, disposition))
    manager._write_takeover_file(task, record)
    _log_takeover_recorded_event(manager, task, record, disposition)
    return record


# LLM: 追加式事件日志的只读审计投影；事件来源始终是权威任务记录，这里只把接替事实投给时间线，不再建第二份状态。
#   写入失败与 subagent_run_saved 等事件一致：log_local_record 内部吞掉异常只记 warning，不影响落账主链。
# 函数用途: 在接替落盘确认后追加一条 subagent_takeover_recorded 事件，字段含来源/接替者 run_id、disposition 与时间。
def _log_takeover_recorded_event(
    manager: Any,
    task: SubAgentTask,
    record: TakeoverRecord,
    disposition: str,
) -> None:
    from ..indexing.records import LocalRecordParams

    manager.log_local_record(
        params=LocalRecordParams(
            source_type="subagent_run",
            source_id=task.id,
            title=f"Subagent takeover recorded: {task.id} -> {record.take_over_by}",
            content="",
            event_type="subagent_takeover_recorded",
            metadata={
                "source_run_id": task.id,
                "successor_run_id": record.take_over_by,
                "disposition": disposition,
                "record_id": record.id,
                "created_at": record.created_at,
            },
        ),
    )


# LLM: 未关闭来源的原接管语义：状态转 TAKEN_OVER、最终负责人和锁文件换成接替者，任务层配置记下接管记录。
#   runtime_config_scope 在 base.py，延迟导入避免 base -> takeover -> base 循环。只改内存中的 task。
# 函数用途: 把一个仍在运行或阻塞的子代理标成“已被接管”。
def _apply_taken_over_fields(task: SubAgentTask, record: TakeoverRecord) -> None:
    from ...utils import _merge_list
    from ..base import runtime_config_scope

    task.takeover_by = record.take_over_by
    task.takeover_reason = record.reason
    task.locked_files = _merge_list(task.locked_files, record.locked_files)
    task.final_owner = record.take_over_by
    task.status = TaskStatus.TAKEN_OVER.value
    attrs = dict(getattr(task, "attributes", {}) or {})
    runtime_scope = dict(attrs.get("runtime_config_scope") or runtime_config_scope(task))
    runtime_scope["takeover_by"] = record.take_over_by
    runtime_scope["takeover_record_id"] = record.id
    runtime_scope["loaded_as"] = "task_layer_after_takeover" if runtime_scope.get("overlay_ref") else "base_config"
    attrs["runtime_config_scope"] = runtime_scope
    task.attributes = attrs


# LLM: 只认重新读取的权威状态；接替者与处置必须和本次写入一致，否则视为被持久化边界还原或没写进盘。只读。
# 函数用途: 保存后核对接替关系确实落盘，不一致时抛结构化异常。
def _require_persisted(manager: Any, run_id: str, expected: tuple[str, str]) -> None:
    persisted = manager.load(run_id)
    if task_replacement_successor(persisted) != expected:
        raise TakeoverNotPersistedError(run_id, *expected)


__all__ = ["TakeoverEdgeRequest", "TakeoverNotPersistedError", "record_takeover_edge"]
