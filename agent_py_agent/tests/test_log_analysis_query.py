from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.log_analysis.models import Case, EvidenceRef, Finding, NormalizedEvent
from agent_py_agent.agent.log_analysis.storage import (
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    LocalLogStore,
    QueryCriteria,
    execute_security_query,
)
from agent_py_agent.agent.log_analysis.tools import hunt_ip, security_query, trace_case


def _matching_events(count: int, *, attacker_ip: str = "198.51.100.80"):
    for index in range(count):
        hour = 10 + index // 60
        minute = index % 60
        yield {
            "event_id": f"evt-limit-{index}",
            "event_time": f"2026-04-30T{hour:02d}:{minute:02d}:00Z",
            "source_id": "limit-test",
            "alert_type": "web_attack",
            "attacker_ip": attacker_ip,
        }


def _write_matching_events(store: LocalLogStore, count: int, *, attacker_ip: str = "198.51.100.80") -> None:
    lines = [
        json.dumps(event, ensure_ascii=False, sort_keys=True)
        for event in _matching_events(count, attacker_ip=attacker_ip)
    ]
    store.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_local_store_upserts_and_queries_security_fields(tmp_path):
    store = LocalLogStore(tmp_path)
    inserted = store.upsert_events(
        [
            {
                "event_id": "evt-1",
                "event_time": "2026-04-30T10:00:00Z",
                "source_id": "waf-prod",
                "alert_type": "web_attack",
                "attacker_ip": "198.51.100.10",
                "victim_ip": "10.0.0.5",
                "domain": "app.example.test",
                "uri": "/upload.php",
                "raw_ref": "raw-1:1",
            },
            {
                "event_id": "evt-2",
                "event_time": "2026-04-30T10:05:00Z",
                "source_id": "edr-prod",
                "alert_type": "process_alert",
                "src_ip": "10.0.0.5",
                "dst_ip": "203.0.113.9",
                "raw_ref": "raw-2:1",
            },
        ]
    )

    assert inserted == 2
    assert store.upsert_event(
        {
            "event_id": "evt-1",
            "event_time": "2026-04-30T10:00:00Z",
            "source_id": "waf-prod",
            "alert_type": "web_attack",
            "attacker_ip": "198.51.100.10",
            "victim_ip": "10.0.0.5",
            "domain": "app.example.test",
            "uri": "/upload.php",
            "raw_ref": "raw-1:1",
        }
    ) is False
    assert len(store.list_events()) == 2
    assert store.upsert_event(
        NormalizedEvent(
            event_id="evt-model",
            source_id="dns-prod",
            event_time="2026-04-30T11:00:00Z",
            event_type="dns",
            attributes={"domain": "model.example.test"},
        )
    ) is True
    assert store.upsert_finding(Finding(finding_id="finding-1", detector_id="detector-1")) is True
    assert store.upsert_case(Case(case_id="case-model", title="model case")) is True
    assert store.upsert_evidence_ref(EvidenceRef(evidence_id="evidence-1", query_id="query-1")) is True
    assert store.get_finding("finding-1")["detector_id"] == "detector-1"
    assert store.get_case("case-model")["title"] == "model case"
    assert store.get_evidence_ref("evidence-1")["query_id"] == "query-1"

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.10",
            start_time="2026-04-30T09:59:00Z",
            end_time="2026-04-30T10:01:00Z",
            limit=10,
        ),
    )

    assert result.row_count == 1
    assert result.truncated is False
    assert result.rows[0]["event_id"] == "evt-1"
    assert result.evidence_path
    assert store.list_query_records()[0]["query_id"] == result.query_id


def test_large_query_writes_limited_evidence_and_returns_limited_preview(tmp_path):
    store = LocalLogStore(tmp_path)
    store.upsert_events(
        {
            "event_id": f"evt-{index}",
            "event_time": f"2026-04-30T10:{index:02d}:00Z",
            "source_id": "waf-prod",
            "alert_type": "web_attack",
            "attacker_ip": "198.51.100.20",
            "victim_ip": "10.0.0.5",
            "payload": "A" * 500,
            "raw_fields": {"Payload": "A" * 500, "secret": "keep-in-evidence-only"},
        }
        for index in range(12)
    )

    response = security_query(
        store=store,
        attacker_ip="198.51.100.20",
        start_time="2026-04-30T10:00:00Z",
        end_time="2026-04-30T10:59:00Z",
        limit=5,
    )

    assert response["row_count"] == 12
    assert response["truncated"] is True
    assert len(response["preview_rows"]) == 5
    assert "raw_fields" not in response["preview_rows"][0]
    assert "payload" not in response["preview_rows"][0]
    assert response["preview_rows"][0]["payload_preview"] == "A" * 120

    evidence = json.loads((tmp_path / "evidence" / f"{response['query_id']}.json").read_text(encoding="utf-8"))
    assert evidence["row_count"] == 12
    assert evidence["truncated"] is True
    assert evidence["summary"]["row_count"] == 12
    assert evidence["summary"]["returned_row_count"] == 5
    assert evidence["summary"]["truncated"] is True
    assert len(evidence["rows"]) == 5
    assert evidence["rows"][0]["raw_fields"]["secret"] == "keep-in-evidence-only"

    query_record = store.list_query_records()[0]
    assert query_record["row_count"] == 12
    assert query_record["truncated"] is True
    assert query_record["evidence_path"] == response["evidence_path"]


def test_query_uses_default_limit_when_criteria_limit_is_omitted(tmp_path):
    store = LocalLogStore(tmp_path)
    _write_matching_events(store, 120)

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.80",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-05-01T00:00:00Z",
        ),
    )

    assert result.row_count == 120
    assert result.parameters["limit"] == DEFAULT_QUERY_LIMIT
    assert len(result.rows) == DEFAULT_QUERY_LIMIT
    assert result.truncated is True


def test_query_accepts_configured_max_limit_above_storage_fallback(tmp_path):
    store = LocalLogStore(tmp_path)
    _write_matching_events(store, 520)

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.80",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-05-01T00:00:00Z",
            limit=520,
        ),
        max_limit=1000,
    )

    assert result.row_count == 520
    assert result.parameters["limit"] == 520
    assert len(result.rows) == 520
    assert result.truncated is False


def test_query_truncates_to_configured_max_limit(tmp_path):
    store = LocalLogStore(tmp_path)
    _write_matching_events(store, 12)

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.80",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-05-01T00:00:00Z",
            limit=10,
        ),
        max_limit=4,
    )

    assert result.row_count == 12
    assert result.parameters["limit"] == 4
    assert len(result.rows) == 4
    assert result.summary["returned_row_count"] == 4
    assert result.truncated is True


def test_query_uses_storage_max_fallback_without_configured_max_limit(tmp_path):
    store = LocalLogStore(tmp_path)
    _write_matching_events(store, 520)

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.80",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-05-01T00:00:00Z",
            limit=520,
        ),
    )

    assert result.row_count == 520
    assert result.parameters["limit"] == MAX_QUERY_LIMIT
    assert len(result.rows) == MAX_QUERY_LIMIT
    assert result.truncated is True


def test_local_store_skips_bad_jsonl_lines_during_query(tmp_path):
    store = LocalLogStore(tmp_path)
    store.upsert_event(
        {
            "event_id": "evt-good",
            "event_time": "2026-04-30T10:00:00Z",
            "source_id": "waf-prod",
            "alert_type": "web_attack",
            "attacker_ip": "198.51.100.50",
        }
    )
    store.events_path.write_text(
        store.events_path.read_text(encoding="utf-8") + "not-json\n[1, 2, 3]\n",
        encoding="utf-8",
    )

    result = execute_security_query(
        store,
        QueryCriteria(
            attacker_ip="198.51.100.50",
            start_time="2026-04-30T09:59:00Z",
            end_time="2026-04-30T10:01:00Z",
        ),
    )

    assert result.row_count == 1
    assert result.rows[0]["event_id"] == "evt-good"
    assert result.summary["corrupt_storage_lines"] == 1
    assert result.summary["skipped_storage_lines"] == 2

    audit = result.summary["storage_read_audit"]
    assert audit["path"] == str(store.events_path)
    assert audit["valid_records"] == 1
    assert audit["corrupt_lines"] == 1
    assert audit["non_object_lines"] == 1
    assert audit["skipped_lines"] == 2
    assert [sample["reason"] for sample in audit["samples"]] == ["invalid_json", "non_object_json"]

    persisted_audits = [
        json.loads(line)
        for line in store.corrupt_lines_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert persisted_audits[-1]["path"] == str(store.events_path)
    assert persisted_audits[-1]["skipped_lines"] == 2
    assert store.list_query_records()[0]["summary"]["storage_read_audit"]["skipped_lines"] == 2


def test_tools_require_time_range_and_support_hunt_and_trace_case(tmp_path):
    store = LocalLogStore(tmp_path)
    store.upsert_events(
        [
            {
                "event_id": "evt-1",
                "event_time": "2026-04-30T10:00:00Z",
                "alert_type": "vpn_login",
                "attacker_ip": "198.51.100.30",
                "victim_ip": "10.0.0.7",
            },
            {
                "event_id": "evt-2",
                "event_time": "2026-04-30T10:03:00Z",
                "alert_type": "edr_network",
                "src_ip": "10.0.0.7",
                "dst_ip": "198.51.100.30",
            },
        ]
    )
    store.upsert_case(
        {
            "case_id": "case-1",
            "title": "suspicious vpn",
            "attributes": {"attacker_ip": ["198.51.100.30"]},
        }
    )

    with pytest.raises(ValueError):
        security_query(store=store, attacker_ip="198.51.100.30")

    hunt = hunt_ip(
        "198.51.100.30",
        store=store,
        start_time="2026-04-30T09:59:00Z",
        end_time="2026-04-30T10:10:00Z",
        limit=10,
    )
    assert hunt["row_count"] == 2
    assert len(hunt["evidence_refs"]) == 2

    traced = trace_case(
        "case-1",
        store=store,
        start_time="2026-04-30T09:59:00Z",
        end_time="2026-04-30T10:10:00Z",
        limit=10,
    )
    assert traced["case_id"] == "case-1"
    assert traced["row_count"] == 1
