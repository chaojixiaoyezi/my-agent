from __future__ import annotations

from agent_py_agent.agent.log_analysis.agents.contracts import (
    AnalystReport,
    ContractValidationError,
    review_analyst_report,
    validate_analyst_input,
    validate_analyst_report,
)
from agent_py_agent.agent.log_analysis.agents.prompts import (
    SecurityPromptConfig,
    build_security_prompt,
)
from agent_py_agent.agent.log_analysis.agents.summaries import (
    case_summary_for_prompt,
    summarize_case,
)
from agent_py_agent.agent.log_analysis.interfaces import DispatchEngine as DispatchEngineProtocol
from agent_py_agent.agent.log_analysis.models import EvidenceRef
from agent_py_agent.agent.log_analysis.dispatch import (
    PENDING_INVESTIGATION,
    DispatchBudget,
    DispatchEngine,
    build_health_summary,
)


def _case_fixture() -> dict:
    return {
        "case_id": "case-001",
        "title": "WAF to EDR weak-signal chain",
        "status": "OPEN",
        "priority": "P2",
        "risk_score": 0.72,
        "entity_refs": ["ip:198.51.100.1", "host:web-01"],
        "evidence_refs": [
            {"id": "ev-waf-1", "summary": "WAF hit", "rows": [{"raw": "do not include"}]},
            {"id": "ev-edr-1", "summary": "EDR process anomaly"},
        ],
        "route_draft": {
            "entry_candidates": ["waf_uri:/login"],
            "timeline": ["2026-04-30T01:00Z WAF hit", "2026-04-30T01:04Z EDR alert"],
            "gaps": ["Need VPN account context"],
            "next_queries": ["traffic_related ip=198.51.100.1"],
        },
        "raw_events": [{"payload": "RAW_MARKER_SHOULD_NOT_APPEAR"}],
        "transcript": "TRANSCRIPT_MARKER_SHOULD_NOT_APPEAR" * 100,
    }


def test_security_prompt_disabled_leaves_base_prompt_unchanged():
    base_prompt = "ordinary agent prompt"

    rendered = build_security_prompt(
        base_prompt,
        SecurityPromptConfig(security_prompt_enabled=False, security_prompt_mode="analyst"),
        role="analyst-agent",
        is_security_case=True,
    )

    assert rendered == base_prompt


def test_security_prompt_analyst_mode_is_scoped_and_evidence_driven():
    rendered = build_security_prompt(
        "base",
        SecurityPromptConfig(security_prompt_enabled=True, security_prompt_mode="analyst"),
        role="analyst-agent",
        case_summary='{"case":{"case_id":"case-001"},"evidence":["ev-1"],"route":{}}',
    )

    assert "# Security Log Analysis Context" in rendered
    assert "evidence_refs" in rendered
    assert "traffic_query" in rendered
    assert "raw events" in rendered


def test_analyst_contract_requires_evidence_refs():
    try:
        validate_analyst_input(
            {
                "case_id": "case-001",
                "case_summary": "compact summary",
                "evidence_refs": [],
            }
        )
    except ContractValidationError as exc:
        assert "evidence_refs" in str(exc)
    else:
        raise AssertionError("empty evidence_refs should fail analyst input validation")


def test_reviewer_rejects_report_without_evidence():
    decision = review_analyst_report(
        {
            "case_id": "case-001",
            "summary": "Looks suspicious but has no refs",
            "facts": ["WAF hit was seen"],
            "evidence_refs": [],
        }
    )

    assert not decision.approved
    assert decision.decision == "REJECT"
    assert "evidence" in decision.reasons[0]


def test_reviewer_accepts_evidence_backed_report():
    report = AnalystReport(
        case_id="case-001",
        summary="WAF hit followed by EDR alert.",
        facts=["ev-waf-1 shows WAF hit", "ev-edr-1 shows process anomaly"],
        inferences=["Possible web-to-process chain"],
        gaps=["Need VPN context"],
        next_actions=["Run related account query"],
        evidence_refs=["ev-waf-1", "ev-edr-1"],
    )

    validated = validate_analyst_report(report)
    decision = review_analyst_report(validated, known_evidence_refs=["ev-waf-1", "ev-edr-1"])

    assert decision.approved
    assert decision.decision == "APPROVE"


def test_evidence_ref_dataclass_normalizes_to_id_in_analyst_input_and_summary():
    evidence = EvidenceRef(
        evidence_id="ev-model-1",
        kind="query_result",
        path="/tmp/ev-model-1.json",
        sha256="abc123",
        row_count=3,
    )

    analyst_input = validate_analyst_input(
        {
            "case_id": "case-001",
            "case_summary": "compact summary",
            "evidence_refs": [evidence],
        }
    ).to_dict()
    rendered = case_summary_for_prompt({"case_id": "case-001", "evidence_refs": [evidence]})

    assert analyst_input["evidence_refs"] == ["ev-model-1"]
    assert "EvidenceRef(" not in rendered
    assert "ev-model-1" in rendered
    assert "/tmp/ev-model-1.json" in rendered
    assert "abc123" in rendered


def test_reviewer_known_refs_accepts_evidence_ref_dataclass_boundary():
    report = AnalystReport(
        case_id="case-001",
        summary="Evidence-backed fact.",
        facts=["ev-model-1 shows a suspicious query result"],
        evidence_refs=[EvidenceRef(evidence_id="ev-model-1")],
    )

    decision = review_analyst_report(report, known_evidence_refs=[EvidenceRef(evidence_id="ev-model-1")])

    assert decision.approved
    assert decision.evidence_refs == ["ev-model-1"]


def test_dispatch_engine_default_budget_only_writes_pending_queue():
    engine = DispatchEngine()

    result = engine.submit_case(_case_fixture())

    assert not result.dispatched
    assert result.request.status == PENDING_INVESTIGATION
    assert result.request.reason == "max_parallel_analyst_agents=0 disables analyst dispatch"
    assert engine.queue.agent_backlog()["pending_investigation"] == 1
    assert engine.queue.agent_backlog()["active_analyst_agents"] == 0


def test_dispatch_engine_public_protocol_contract_is_minimally_usable():
    engine = DispatchEngine()

    result = engine.enqueue_case(_case_fixture(), context={"source": "test"})
    payload = result.to_dict()
    health = engine.health()

    assert isinstance(engine, DispatchEngineProtocol)
    assert result.case_id == "case-001"
    assert result.status == PENDING_INVESTIGATION
    assert result.run_id.startswith("logdisp-")
    assert result.message == "max_parallel_analyst_agents=0 disables analyst dispatch"
    assert payload["case_id"] == "case-001"
    assert payload["metadata"]["request_id"] == result.request.request_id
    assert health["agent_backlog"]["pending_investigation"] == 1


def test_dispatch_engine_respects_enabled_budget():
    engine = DispatchEngine(
        budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=1,
            analyst_agent_budget_per_hour=1,
            analyst_agent_timeout_seconds=120,
        )
    )

    result = engine.submit_case(_case_fixture())

    assert result.dispatched
    assert result.agent_id.startswith("analyst-logdisp-")
    assert engine.queue.agent_backlog()["active_analyst_agents"] == 1


def test_case_summary_is_compact_and_excludes_raw_events_and_transcript():
    summary = summarize_case(_case_fixture())
    rendered = case_summary_for_prompt(_case_fixture())

    assert set(summary.to_dict()) == {"case", "evidence", "route"}
    assert "RAW_MARKER_SHOULD_NOT_APPEAR" not in rendered
    assert "TRANSCRIPT_MARKER_SHOULD_NOT_APPEAR" not in rendered
    assert "rows" not in rendered
    assert "ev-waf-1" in rendered
    assert "next_queries" in rendered


def test_health_summary_exposes_backlogs_prompt_switch_and_budget():
    engine = DispatchEngine()
    engine.submit_case(_case_fixture())

    summary = build_health_summary(
        cases=[_case_fixture()],
        queue=engine.queue,
        budget=engine.budget,
        prompt_config=SecurityPromptConfig(),
    ).to_dict()

    assert summary["case_backlog"]["OPEN"] == 1
    assert summary["agent_backlog"]["pending_investigation"] == 1
    assert summary["prompt_switch"] == {
        "security_prompt_enabled": False,
        "security_prompt_mode": "off",
    }
    assert summary["dispatch_budget"]["max_parallel_analyst_agents"] == 0
