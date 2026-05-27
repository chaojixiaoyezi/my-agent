from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


# LLM: Runtime numeric knobs must be visible in agent_config.yaml and AgentConfig.
# 函数用途: 验证运行预算、上下文裁剪、工具解析和只读看板扫描参数都能通过主配置归一化。
def test_runtime_parameter_knobs_are_normalized_from_agent_config() -> None:
    from agent_py_agent.agent.settings.config import normalize_agent_config

    normalized, warnings = normalize_agent_config(
        {
            "runner_auto_concurrency": "3",
            "background_context_max_string_chars": "11",
            "background_context_max_list_items": "4",
            "background_context_max_dict_items": "5",
            "background_context_max_depth": "2",
            "conversation_pending_wake_limit": "7",
            "conversation_context_recent_limit": "6",
            "background_pending_wake_prompt_limit": "5",
            "tool_output_externalize_min_chars": "44",
            "tool_output_preview_chars": "12",
            "tool_payload_max_fields": "8",
            "tool_payload_max_field_name_chars": "30",
            "tool_payload_max_name_chars": "31",
            "tool_payload_parse_error_raw_chars": "32",
            "contract_status_max_scan_files": "9",
            "contract_status_max_report_bytes": "999",
            "contract_status_recent_findings_limit": "3",
            "skill_guard_max_files": "4",
            "skill_guard_max_size_kb": "5",
            "small_real_acceptance_max_runtime_seconds": "60",
            "real_run_review_max_report_bytes": "1234",
            "real_run_review_max_log_bytes": "4321",
            "background_claim_ttl_seconds": "120",
            "background_claim_heartbeat_interval_seconds": "30",
        }
    )

    assert warnings == []
    assert normalized["runner_auto_concurrency"] == 3
    assert normalized["background_context_max_string_chars"] == 11
    assert normalized["conversation_pending_wake_limit"] == 7
    assert normalized["tool_output_externalize_min_chars"] == 44
    assert normalized["tool_payload_max_fields"] == 8
    assert normalized["contract_status_max_scan_files"] == 9
    assert normalized["skill_guard_max_files"] == 4
    assert normalized["small_real_acceptance_max_runtime_seconds"] == 60
    assert normalized["real_run_review_max_report_bytes"] == 1234
    assert normalized["real_run_review_max_log_bytes"] == 4321
    assert normalized["background_claim_ttl_seconds"] == 120


# LLM: runner_concurrency=auto must not hide a second hard-coded max worker count.
# 函数用途: 验证 auto 并发上限可由配置字段控制。
def test_runner_auto_concurrency_uses_configured_limit() -> None:
    from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

    assert _resolve_runner_concurrency("auto", 20, auto_limit=3) == 3
    assert _resolve_runner_concurrency("bad", 20, auto_limit=4) == 4


# LLM: Background prompt trimming must be controlled by AgentConfig fields, not module constants.
# 函数用途: 验证长期会话上下文预算能从配置对象生成并影响 prompt 副本裁剪。
def test_background_context_budget_uses_configured_values() -> None:
    from agent_py_agent.agent.conversation.context_budget import (
        background_context_budget_from_config,
        bounded_background_context_payload,
    )

    budget = background_context_budget_from_config(
        SimpleNamespace(
            background_context_max_string_chars=5,
            background_context_max_list_items=1,
            background_context_max_dict_items=2,
            background_context_max_depth=2,
        )
    )
    payload = bounded_background_context_payload(
        bundle={"thread": {"long": "abcdef", "other": "ok", "third": "hidden"}, "messages": [{"content": "abcdef"}]},
        pending_wake_signals=[],
        agent_tree={"nodes": [{"a": 1}, {"b": 2}]},
        budget=budget,
    )

    assert payload["messages"][0]["content"]["preview"].startswith("abcde")
    assert payload["thread"]["_truncated_dict_items"] == 1
    assert payload["agent_tree"]["nodes"][-1]["omitted_items"] == 1


# LLM: Large tool-output archive thresholds and previews must come from config.
# 函数用途: 验证工具输出外置阈值和预览长度都能由调用方配置。
def test_tool_output_externalizer_uses_configured_threshold_and_preview(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="demo",
            call_id="1",
            output="abcdefghijklmnop",
            ok=True,
            min_chars=100,
            preview_chars=6,
        )
    )

    assert record["output_externalized"] is False
    assert record["output_preview"] == "abcdef\n... [truncated 10 chars]"


# LLM: Contract status scan defaults should be provided by config when callers do not pass explicit caps.
# 函数用途: 验证合同状态汇总能用配置对象控制扫描文件数和 recent finding 条数。
def test_contract_status_summary_can_read_scan_limits_from_config(tmp_path: Path) -> None:
    import json

    from agent_py_agent.agent.contracts.contract_status import (
        ContractStatusScanRequest,
        summarize_contract_status,
    )

    for index in range(3):
        (tmp_path / f"report-{index}.json").write_text(
            json.dumps({"findings": [{"code": f"F{index}", "severity": "soft"}]}),
            encoding="utf-8",
        )

    status = summarize_contract_status(
        tmp_path,
        ContractStatusScanRequest(
            config=SimpleNamespace(
                contract_status_max_scan_files=2,
                contract_status_max_report_bytes=2000,
                contract_status_recent_findings_limit=1,
            ),
        ),
    )

    assert status.scanned_files == 2
    assert len(status.recent_findings) == 1
