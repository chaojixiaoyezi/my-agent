# LLM: Offline scheduler/resource contracts validate worker leases and resource limits.
# 模块用途: 校验心跳、lease、队列、磁盘、子进程超时和日志轮转事实。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    positive_int,
    text,
    validation_report,
)


# LLM: validate_scheduler_resource_facts checks scheduler/resource facts without starting workers.
# 函数用途: 用 workers、results、queue、resource_events、logs 字段校验调度资源边界。
def validate_scheduler_resource_facts(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_workers(facts, findings)
    _validate_results(facts, findings)
    _validate_queue(facts, findings)
    _validate_resource_events(facts, findings)
    _validate_logs(facts, findings)
    return validation_report(findings)


# LLM: _validate_workers flags stale heartbeats.
# 函数用途: heartbeat_age_ms 超过 heartbeat_timeout_ms 时返回 WORKER_HEARTBEAT_STALE。
def _validate_workers(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for worker in dict_items(facts.get("workers")):
        if positive_int(worker.get("heartbeat_age_ms")) > positive_int(worker.get("heartbeat_timeout_ms")) > 0:
            findings.append(finding("WORKER_HEARTBEAT_STALE", {"worker_id": text(worker.get("worker_id"))}))


# LLM: _validate_results rejects writes from expired leases.
# 函数用途: lease_valid=false 且 submitted=true 返回 LEASE_EXPIRED_RESULT_REJECTED。
def _validate_results(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for result in dict_items(facts.get("results")):
        if result.get("submitted") is True and result.get("lease_valid") is False:
            findings.append(finding("LEASE_EXPIRED_RESULT_REJECTED", {"worker_id": text(result.get("worker_id"))}))


# LLM: _validate_queue flags bounded queue overflow.
# 函数用途: pending 超过 max_pending 时返回 QUEUE_LIMIT_EXCEEDED。
def _validate_queue(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    queue = facts.get("queue")
    if isinstance(queue, dict) and positive_int(queue.get("pending")) > positive_int(queue.get("max_pending")) > 0:
        findings.append(finding("QUEUE_LIMIT_EXCEEDED"))


# LLM: _validate_resource_events handles disk and subprocess failure facts.
# 函数用途: ENOSPC 返回 RESOURCE_DISK_FULL，超时未终止返回 PROCESS_TIMEOUT_NOT_TERMINATED。
def _validate_resource_events(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for event in dict_items(facts.get("resource_events")):
        if text(event.get("error_code")) == "ENOSPC":
            findings.append(finding("RESOURCE_DISK_FULL"))
        if text(event.get("type")) == "process_timeout" and event.get("terminated") is not True:
            findings.append(finding("PROCESS_TIMEOUT_NOT_TERMINATED"))


# LLM: _validate_logs requires rotation or externalization over size budget.
# 函数用途: size_bytes 超过 max_size_bytes 且 rotated=false 时返回 LOG_ROTATION_REQUIRED。
def _validate_logs(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for log in dict_items(facts.get("logs")):
        if positive_int(log.get("size_bytes")) > positive_int(log.get("max_size_bytes")) > 0 and log.get("rotated") is not True:
            findings.append(finding("LOG_ROTATION_REQUIRED"))


__all__ = ["validate_scheduler_resource_facts"]
