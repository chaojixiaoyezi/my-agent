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
    SecurityPromptScope,
    build_security_prompt,
)
from agent_py_agent.agent.log_analysis.agents.summaries import (
    case_summary_for_prompt,
    summarize_case,
)
from agent_py_agent.agent.log_analysis.dispatch import (
    PENDING_INVESTIGATION,
    DispatchBudget,
    DispatchEngine,
    build_health_summary,
    create_subagent_tasks_from_work_order_plan,
    plan_case_subagent_work_orders,
)
from agent_py_agent.agent.log_analysis.interfaces import DispatchEngine as DispatchEngineProtocol
from agent_py_agent.agent.log_analysis.models import EvidenceRef
from agent_py_agent.agent.subagents.manager import SubAgentManager


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
            "next_queries": ["security_hunt_ip ip=198.51.100.1"],
        },
        "raw_events": [{"payload": "RAW_MARKER_SHOULD_NOT_APPEAR"}],
        "transcript": "TRANSCRIPT_MARKER_SHOULD_NOT_APPEAR" * 100,
    }


def test_security_prompt_disabled_leaves_base_prompt_unchanged():
    base_prompt = "ordinary agent prompt"

    rendered = build_security_prompt(
        base_prompt,
        SecurityPromptConfig(security_prompt_enabled=False, security_prompt_mode="analyst"),
        scope=SecurityPromptScope(role="analyst-agent", is_security_case=True),
    )

    assert rendered == base_prompt


def test_security_prompt_analyst_mode_is_scoped_and_evidence_driven():
    rendered = build_security_prompt(
        "base",
        SecurityPromptConfig(security_prompt_enabled=True, security_prompt_mode="analyst"),
        scope=SecurityPromptScope(
            role="analyst-agent",
            case_summary='{"case":{"case_id":"case-001"},"evidence":["ev-1"],"route":{}}',
        ),
    )

    assert "# Security Log Analysis Context" in rendered
    assert "evidence_refs" in rendered
    assert "security_query" in rendered
    assert "security_hunt_ip" in rendered
    assert "traffic_query" not in rendered
    assert "traffic_sample" not in rendered
    assert "traffic_topn" not in rendered
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

    result = engine.enqueue_case(_case_fixture())

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

    result = engine.enqueue_case(_case_fixture())

    assert result.dispatched
    assert result.agent_id.startswith("analyst-logdisp-")
    assert engine.queue.agent_backlog()["active_analyst_agents"] == 1


def test_dispatch_engine_builds_security_tool_names_for_analyst_input():
    engine = DispatchEngine()
    result = engine.enqueue_case(_case_fixture())

    analyst_input = engine.build_analyst_input(result.request)

    assert "security_query" in analyst_input["available_tools"]
    assert "security_hunt_ip" in analyst_input["available_tools"]
    assert "security_trace_case" in analyst_input["available_tools"]
    assert "traffic_query" not in analyst_input["available_tools"]


def test_work_order_plan_builds_manual_dry_run_analyst_and_reviewer_orders():
    plan = plan_case_subagent_work_orders(
        _case_fixture(),
        quality_contract={"acceptance_checks": ["Reviewer must enforce closeout."]},
    )
    payload = plan.to_dict()

    assert plan.ready
    assert plan.dry_run
    assert plan.mode == "manual"
    assert [order.role for order in plan.work_orders] == ["analyst", "reviewer"]
    assert payload["work_orders"][0]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]
    assert payload["work_orders"][1]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]

    analyst = payload["work_orders"][0]
    reviewer = payload["work_orders"][1]
    assert analyst["allowed_tools"] == [
        "security_query",
        "security_hunt_ip",
        "security_trace_case",
        "evidence_read",
    ]
    assert reviewer["allowed_tools"] == ["evidence_read"]
    assert reviewer["context"]["expected_input"] == "AnalystReport from analyst work order"
    assert not plan.issues


def test_work_order_plan_without_evidence_refs_is_not_ready_and_reports_risk():
    case = {
        "case_id": "case-no-evidence",
        "title": "Suspicious signal without retained evidence",
        "route_draft": {"gaps": ["Need source query evidence"]},
    }

    plan = plan_case_subagent_work_orders(case)
    payload = plan.to_dict()

    assert not plan.ready
    assert plan.dry_run
    assert "no evidence_refs" in plan.issues[0]
    assert "unsupported analysis" in plan.risks[0]
    assert all(not order["ready"] for order in payload["work_orders"])
    assert all(order["evidence_refs"] == [] for order in payload["work_orders"])
    assert all(order["issues"] == plan.issues for order in payload["work_orders"])


def test_work_order_plan_apply_creates_analyst_and_reviewer_subagent_tasks(tmp_path):
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(
        _case_fixture(),
        quality_contract={"acceptance_checks": ["Reviewer must check evidence."]},
    )

    result = create_subagent_tasks_from_work_order_plan(
        subagents,
        plan,
        apply=True,
        parent_id="parent-run",
        root_id="root-run",
        final_owner="parent",
    )
    tasks = [subagents.load(task_id) for task_id in result.task_ids]

    assert result.apply
    assert not result.dry_run
    assert result.mode == "apply"
    assert len(result.created) == 2
    assert [task.role for task in tasks] == ["analyst", "reviewer"]
    assert [task.agent_name for task in tasks] == ["log-analyst", "log-reviewer"]
    assert tasks[0].allowed_tools == [
        "security_query",
        "security_hunt_ip",
        "security_trace_case",
        "evidence_read",
    ]
    assert tasks[1].allowed_tools == ["evidence_read"]
    for task in tasks:
        assert task.status == "PLANNING"
        assert task.verification_status == "UNVERIFIED"
        assert task.runner_attempts == 0
        assert task.parent_id == "parent-run"
        assert task.root_id == "root-run"
        assert task.final_owner == "parent"
        assert "ev-waf-1" in task.quality_contract.evidence_required
        assert task.context_packs[0]["case_id"] == "case-001"
        assert task.context_packs[0]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]


def test_work_order_plan_dry_run_does_not_create_subagent_tasks(tmp_path):
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(_case_fixture())

    result = create_subagent_tasks_from_work_order_plan(subagents, plan)

    assert result.dry_run
    assert not result.apply
    assert result.mode == "dry_run"
    assert result.created == []
    assert result.task_ids == []
    assert list((tmp_path / "subagents").glob("*/task.json")) == []


def test_work_order_plan_apply_without_evidence_refuses_and_creates_no_tasks(tmp_path):
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(
        {
            "case_id": "case-no-evidence",
            "title": "Suspicious signal without retained evidence",
        }
    )

    result = create_subagent_tasks_from_work_order_plan(subagents, plan, apply=True)

    assert not result.ready
    assert result.apply
    assert not result.dry_run
    assert result.created == []
    assert result.task_ids == []
    assert "not ready" in result.issues[-1]
    assert list((tmp_path / "subagents").glob("*/task.json")) == []


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
    engine.enqueue_case(_case_fixture())

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
