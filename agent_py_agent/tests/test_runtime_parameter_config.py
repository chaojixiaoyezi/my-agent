from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


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
            "subagent_watch_interval_seconds": "240",
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
    assert normalized["subagent_watch_interval_seconds"] == 240


def test_subagent_watch_interval_config_clamps_bad_values() -> None:
    from agent_py_agent.agent.settings.config import normalize_agent_config

    low, low_warnings = normalize_agent_config({"subagent_watch_interval_seconds": "10"})
    bad, bad_warnings = normalize_agent_config({"subagent_watch_interval_seconds": "soon"})
    high, high_warnings = normalize_agent_config({"subagent_watch_interval_seconds": "99999"})

    assert low["subagent_watch_interval_seconds"] == 60
    assert bad["subagent_watch_interval_seconds"] == 60
    assert high["subagent_watch_interval_seconds"] == 7200
    assert low_warnings
    assert bad_warnings
    assert high_warnings


def test_background_main_agent_allowed_tools_are_normalized() -> None:
    from agent_py_agent.agent.settings.config import normalize_agent_config

    normalized, warnings = normalize_agent_config(
        {"background_main_agent_allowed_tools": "inspect_agent_tree, send_guidance"}
    )

    assert warnings == []
    assert normalized["background_main_agent_allowed_tools"] == ["inspect_agent_tree", "send_guidance"]


def test_simple_agent_keeps_runtime_guard_policy_snapshot(tmp_path: Path) -> None:
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    snapshot = agent.runtime_guard_policy.snapshot(task_id="task-1", run_id="run-1")
    assert snapshot["schema_version"] == "runtime_guard_policy.v1"
    assert snapshot["task_id"] == "task-1"
    assert "values" in snapshot


def test_dispatch_runtime_policy_uses_agent_config_values() -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchRuntimePolicy

    policy = DispatchRuntimePolicy.from_config(
        SimpleNamespace(
            dispatch_max_consecutive_rounds=7,
            dispatch_active_interval=2,
            dispatch_idle_interval=9,
            dispatch_default_max_runners=4,
            dispatch_default_limit=33,
            dispatch_default_watch_interval=6.5,
        )
    )

    assert policy.snapshot()["schema_version"] == "dispatch_runtime_policy.v1"
    assert policy.max_consecutive_rounds == 7
    assert policy.active_interval == 2.0
    assert policy.idle_interval == 9.0
    assert policy.default_max_runners == 4
    assert policy.default_limit == 33
    assert policy.default_watch_interval == 6.5


def test_dispatch_loop_omitted_numbers_follow_agent_config() -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import dispatch_loop

    agent = SimpleNamespace(
        config=SimpleNamespace(
            runner_failure_policy="auto",
            dispatch_max_consecutive_rounds=1,
            dispatch_default_max_runners=3,
            dispatch_default_limit=44,
        ),
        has_pending_work=True,
    )
    agent.subagents = SimpleNamespace(list_runs=lambda: [])

    class Report:
        records = []

    seen_params = []

    def dispatch_subagents(_router, _capability_config, *, params):
        seen_params.append(params)
        return Report()

    agent.dispatch_subagents = dispatch_subagents

    result = dispatch_loop(agent, router=None)

    assert result.rounds_count == 1
    assert result.stopped_by_limit is True
    assert seen_params[0].execution_plan.max_runners == 3
    assert seen_params[0].limit == 44


def test_dispatch_loop_explicit_numbers_override_config() -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import dispatch_loop

    agent = SimpleNamespace(
        config=SimpleNamespace(
            runner_failure_policy="auto",
            dispatch_max_consecutive_rounds=1,
            dispatch_default_max_runners=3,
            dispatch_default_limit=44,
        ),
        has_pending_work=True,
    )
    agent.subagents = SimpleNamespace(list_runs=lambda: [])

    class Report:
        records = []

    seen_params = []

    def dispatch_subagents(_router, _capability_config, *, params):
        seen_params.append(params)
        agent.has_pending_work = False
        return Report()

    agent.dispatch_subagents = dispatch_subagents

    result = dispatch_loop(agent, router=None, max_consecutive_rounds=5, max_runners=2, limit=11)

    assert result.rounds_count == 1
    assert result.stopped_by_limit is False
    assert seen_params[0].execution_plan.max_runners == 2
    assert seen_params[0].limit == 11


def test_runner_auto_concurrency_uses_configured_limit() -> None:
    from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

    assert _resolve_runner_concurrency("auto", 20, auto_limit=3) == 3
    assert _resolve_runner_concurrency("bad", 20, auto_limit=4) == 4


def test_runner_timeout_defaults_to_no_total_deadline() -> None:
    from agent_py_agent.agent.agent_core.runner.timeout_policy import runner_timeout_disabled
    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig()

    assert config.runner_timeout_seconds == "off"
    assert runner_timeout_disabled(config) is True


def test_runner_timeout_off_still_disables_total_deadline() -> None:
    from agent_py_agent.agent.agent_core.runner.timeout_policy import runner_timeout_disabled
    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig(runner_timeout_seconds="off")

    assert runner_timeout_disabled(config) is True


def test_background_context_budget_uses_configured_values() -> None:
    from agent_py_agent.agent.conversation.context_budget import (
        BackgroundContextPayloadRequest,
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
        BackgroundContextPayloadRequest(
            bundle={"thread": {"long": "abcdef", "other": "ok", "third": "hidden"}, "messages": [{"content": "abcdef"}]},
            pending_wake_signals=[],
            agent_tree={"nodes": [{"a": 1}, {"b": 2}]},
            task_runtime_state={"summary": "abcdef"},
            budget=budget,
        )
    )

    assert payload["messages"][0]["content"]["preview"].startswith("abcde")
    assert payload["thread"]["_truncated_dict_items"] == 1
    assert payload["agent_tree"]["nodes"][-1]["omitted_items"] == 1
    assert payload["task_runtime_state"]["summary"]["preview"].startswith("abcde")


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


def test_tool_output_externalizer_default_keeps_few_kb_output_inline(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    output = "x" * 3500
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=tmp_path,
            tool="run_command",
            call_id="1",
            output=output,
            ok=True,
        )
    )

    assert record["output_externalized"] is False
    assert record["output_preview"] == output


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
