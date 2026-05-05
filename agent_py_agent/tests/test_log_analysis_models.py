from __future__ import annotations

import json

from agent_py_agent.agent.log_analysis.capabilities import is_feature_enabled
from agent_py_agent.agent.log_analysis.config import (
    load_log_analysis_config,
    normalize_log_analysis_config,
)
from agent_py_agent.agent.log_analysis.doctor import collect_doctor_status
from agent_py_agent.agent.log_analysis.models import (
    CaseRecord,
    Checkpoint,
    EvidenceRef,
    Finding,
    NormalizedEvent,
    RawBatch,
    SecurityAlertV1,
    SourceSpec,
)


# ── Contract sample builders ──────────────────────────────────────────────────

def _sample_evidence_ref() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="query-q1",
        kind="query",
        uri="evidence/query-q1.json",
        source_id="waf-prod",
        row_count=2,
        truncated=False,
        summary="two matching WAF rows",
    )


def _sample_source_spec() -> SourceSpec:
    return SourceSpec(source_id="waf-prod", kind="file", format="csv", options={"glob": "fixtures/*.csv"})


def _sample_checkpoint() -> Checkpoint:
    return Checkpoint(
        source_id="waf-prod",
        cursor_kind="file_offset",
        cursor={"path": "fixtures/waf.csv", "offset": 42},
        last_committed_batch_id="batch-1",
        last_event_time="2026-04-30T10:15:00Z",
        updated_at="2026-04-30T10:16:00Z",
    )


def _sample_raw_batch() -> RawBatch:
    return RawBatch(
        batch_id="batch-1",
        source_id="waf-prod",
        source_kind="file",
        received_at="2026-04-30T10:16:00Z",
        time_range=["2026-04-30T10:15:00Z", "2026-04-30T10:16:00Z"],
        raw_refs=["spool/waf-prod/batch-1.log"],
        size_bytes=123,
        content_hash="sha256:abc",
    )


def _sample_normalized_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="evt-1",
        source_id="waf-prod",
        event_time="2026-04-30T10:15:12Z",
        ingest_time="2026-04-30T10:15:15Z",
        event_type="http",
        src_ip="198.51.100.1",
        dst_ip="10.1.2.3",
        dst_port=443,
        parser_id="security_alert_v1",
        parser_confidence=0.98,
        attributes={"uri": "/upload.php"},
    )


def _sample_security_alert_v1() -> SecurityAlertV1:
    return SecurityAlertV1(
        alert_id="alert-1",
        event_time="2026-04-30T10:15:12Z",
        source_id="waf-prod",
        source_product="waf",
        alert_type="Web攻击",
        threat_name="疑似命令执行",
        dst_port=443,
        attacker_ip="198.51.100.1",
        victim_ip="10.1.2.3",
        raw_ref="raw-batch-1:line-7",
        raw_fields={"告警类型": "Web攻击", "攻击IP": "198.51.100.1"},
    )


def _sample_finding(evidence: EvidenceRef) -> Finding:
    return Finding(
        finding_id="finding-1",
        detector_id="waf_attack_success_candidate",
        detector_kind="rule",
        window=["2026-04-30T10:00:00Z", "2026-04-30T10:15:00Z"],
        severity_hint="high",
        risk_score=0.87,
        entities={"attacker_ip": ["198.51.100.1"], "victim_ip": ["10.1.2.3"]},
        features={"post_exploit_signals": 2},
        evidence_refs=[evidence],
        hypothesis="WAF hit followed by host-side activity",
        confidence=0.72,
        gaps=["missing EDR process tree"],
        next_queries=["find EDR process events for victim_ip"],
    )


def _sample_case_record(evidence: EvidenceRef, finding_ref: Finding) -> CaseRecord:
    return CaseRecord(
        case_id="case-20260430-001",
        title="WAF exploit candidate",
        priority="P1",
        risk_score=0.87,
        finding_refs=["finding-1"],
        evidence_refs=[evidence],
        dedup_key="waf:10.1.2.3:2026-04-30T10",
        facts=["WAF alert observed"],
        inferences=["possible exploit attempt"],
        gaps=["EDR telemetry missing"],
        next_queries=["query EDR by victim_ip"],
    )


def _all_contract_samples() -> list:
    evidence = _sample_evidence_ref()
    return [
        evidence,
        _sample_source_spec(),
        _sample_checkpoint(),
        _sample_raw_batch(),
        _sample_normalized_event(),
        _sample_security_alert_v1(),
        evidence,  # duplicate reference is intentional (used in Finding.evidence_refs)
        _sample_finding(evidence),
        _sample_case_record(evidence, _sample_finding(evidence)),
    ]


def test_log_analysis_contracts_json_round_trip():
    for item in _all_contract_samples():
        payload = item.to_json()
        loaded = type(item).from_json(payload)
        assert json.loads(payload) == item.to_dict()
        assert loaded.to_dict() == item.to_dict()


def test_log_analysis_defaults_are_disabled_and_heavy_features_off(tmp_path):
    config = load_log_analysis_config(tmp_path / "missing.yaml")

    assert config.enabled is False
    assert config.worker_enabled is False
    assert config.auto_dispatch_enabled is False
    assert config.ml_enabled is False
    assert config.cluster_enabled is False
    assert config.response_execution_enabled is False
    assert is_feature_enabled(config, "status") is True
    assert is_feature_enabled(config, "file_ingest") is False
    assert is_feature_enabled(config, "auto_dispatch") is False
    assert is_feature_enabled(config, "ml_scoring") is False
    assert is_feature_enabled(config, "cluster_backend") is False
    assert is_feature_enabled(config, "response_execution") is False


def test_log_analysis_invalid_config_values_fall_back_with_warnings():
    config, warnings = normalize_log_analysis_config(
        {
            "enabled": "maybe",
            "capability_level": "L9",
            "data_dir": "",
            "worker_enabled": "later",
            "auto_dispatch_enabled": "false; rm -rf /",
            "ml_enabled": "???",
            "cluster_enabled": [],
            "response_execution_enabled": "execute",
            "response_mode": "execute-now",
            "local_store_backend": "kafka",
            "query_default_limit": -1,
            "query_max_limit": "many",
            "source_retention_days": 0,
            "payload_preview_max_chars": True,
            "max_parallel_analyst_agents": -2,
            "dispatch_budget_per_hour": "nan",
            "case_merge_window_minutes": 0,
            "detector_window_minutes": 999999,
        }
    )

    assert config.enabled is False
    assert config.capability_level == "L0"
    assert config.data_dir == "data/log_analysis"
    assert config.response_mode == "recommend"
    assert config.local_store_backend == "jsonl"
    assert {warning.field_name for warning in warnings} == {
        "enabled",
        "capability_level",
        "data_dir",
        "worker_enabled",
        "auto_dispatch_enabled",
        "ml_enabled",
        "cluster_enabled",
        "response_execution_enabled",
        "response_mode",
        "local_store_backend",
        "query_default_limit",
        "query_max_limit",
        "source_retention_days",
        "payload_preview_max_chars",
        "max_parallel_analyst_agents",
        "dispatch_budget_per_hour",
        "case_merge_window_minutes",
        "detector_window_minutes",
    }
    assert [item["field_name"] for item in config.config_warnings] == [warning.field_name for warning in warnings]


def test_log_analysis_feature_gates_require_level_flags_and_budget():
    config, warnings = normalize_log_analysis_config(
        {
            "enabled": True,
            "capability_level": "L5",
            "worker_enabled": True,
            "security_prompt_enabled": True,
            "auto_dispatch_enabled": True,
            "ml_enabled": True,
            "cluster_enabled": True,
            "response_execution_enabled": True,
            "response_mode": "execute",
            "max_parallel_analyst_agents": 2,
            "dispatch_budget_per_hour": 10,
        }
    )

    assert warnings == []
    assert is_feature_enabled(config, "file_ingest") is True
    assert is_feature_enabled(config, "security_prompt") is True
    assert is_feature_enabled(config, "continuous_workers") is True
    assert is_feature_enabled(config, "auto_dispatch") is True
    assert is_feature_enabled(config, "ml_scoring") is True
    assert is_feature_enabled(config, "cluster_backend") is True
    assert is_feature_enabled(config, "response_execution") is True


def test_log_analysis_doctor_returns_disabled_paths_and_config_warnings(tmp_path):
    missing_status = collect_doctor_status(tmp_path / "missing.yaml", workspace_root=tmp_path)

    assert missing_status["state"] == "disabled"
    assert missing_status["enabled"] is False
    assert missing_status["heavy_dependencies_loaded"] is False
    assert missing_status["config"]["exists"] is False
    assert missing_status["paths"]["base"]["path"] == str(tmp_path / "data" / "log_analysis")
    assert missing_status["feature_gates"]["status"] is True
    assert missing_status["feature_gates"]["file_ingest"] is False

    config_path = tmp_path / "log_analysis_config.yaml"
    config_path.write_text("enabled: maybe\nquery_default_limit: nope\n", encoding="utf-8")
    status = collect_doctor_status(config_path, workspace_root=tmp_path)

    assert status["state"] == "disabled"
    assert status["config"]["exists"] is True
    assert [warning["field_name"] for warning in status["config"]["warnings"]] == [
        "enabled",
        "query_default_limit",
    ]
