from __future__ import annotations


# LLM: Worker leases need heartbeat freshness and stale workers cannot submit results.
# 函数用途: 验证 heartbeat 过期和 lease 失效后的迟到结果会被拒绝。
def test_scheduler_rejects_stale_heartbeat_and_expired_lease_result() -> None:
    from agent_py_agent.agent.contracts.offline_scheduler_resource_contract import (
        validate_scheduler_resource_facts,
    )

    result = validate_scheduler_resource_facts(
        {
            "workers": [{"worker_id": "w1", "heartbeat_age_ms": 5000, "heartbeat_timeout_ms": 1000}],
            "results": [{"worker_id": "w1", "lease_valid": False, "submitted": True}],
        }
    )

    assert result.error_codes == ("WORKER_HEARTBEAT_STALE", "LEASE_EXPIRED_RESULT_REJECTED")


# LLM: Queue and resource limits should fail clearly rather than exhausting the process.
# 函数用途: 验证队列积压、磁盘满和超时子进程未终止都会进入资源合同失败。
def test_scheduler_resource_limits_are_explicit() -> None:
    from agent_py_agent.agent.contracts.offline_scheduler_resource_contract import (
        validate_scheduler_resource_facts,
    )

    result = validate_scheduler_resource_facts(
        {
            "queue": {"pending": 101, "max_pending": 100},
            "resource_events": [
                {"type": "artifact_write", "error_code": "ENOSPC"},
                {"type": "process_timeout", "terminated": False},
            ],
        }
    )

    assert result.error_codes == ("QUEUE_LIMIT_EXCEEDED", "RESOURCE_DISK_FULL", "PROCESS_TIMEOUT_NOT_TERMINATED")


# LLM: Logs should rotate or externalize once size budgets are exceeded.
# 函数用途: 验证 RunLog/ToolTrace 超预算但未归档会被发现。
def test_scheduler_requires_log_rotation_for_large_logs() -> None:
    from agent_py_agent.agent.contracts.offline_scheduler_resource_contract import (
        validate_scheduler_resource_facts,
    )

    result = validate_scheduler_resource_facts({"logs": [{"size_bytes": 2048, "max_size_bytes": 1024, "rotated": False}]})

    assert result.error_codes == ("LOG_ROTATION_REQUIRED",)
