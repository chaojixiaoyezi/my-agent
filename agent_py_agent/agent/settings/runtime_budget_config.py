from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeBudgetConfigFields:
    contract_status_max_scan_files: int = 1000
    contract_status_max_report_bytes: int = 2_000_000
    contract_status_recent_findings_limit: int = 20
    skill_guard_max_files: int = 50
    skill_guard_max_size_kb: int = 1024
    small_real_acceptance_max_runtime_seconds: int = 900
    real_run_review_max_report_bytes: int = 5_000_000
    real_run_review_max_log_bytes: int = 1_000_000
    runner_auto_concurrency: int = 8
    conversation_thread_list_limit: int = 100
    conversation_pending_wake_limit: int = 100
    conversation_context_recent_limit: int = 20
    conversation_unhandled_observation_limit: int = 20
    background_pending_wake_prompt_limit: int = 20
    background_context_max_string_chars: int = 1200
    background_context_max_list_items: int = 20
    background_context_max_dict_items: int = 80
    background_context_max_depth: int = 6
    background_claim_ttl_seconds: int = 900
    background_claim_heartbeat_interval_seconds: int = 0
    background_main_agent_allowed_tools: list[str] = field(default_factory=list)


__all__ = ["RuntimeBudgetConfigFields"]
