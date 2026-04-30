from agent_py_agent.agent.subagent import QualityContract
from agent_py_agent.agent.subagent_workflows.acceptance import plan_parent_acceptance
from agent_py_agent.agent.subagent_workflows.models import WorkflowPhase, WorkflowTemplate


def _template(
    template_id="custom_workflow",
    *,
    parent_acceptance=None,
    phases=None,
):
    return WorkflowTemplate(
        id=template_id,
        name="Custom Workflow",
        solves=["test"],
        fit_for=["test"],
        phases=phases
        if phases is not None
        else [
            WorkflowPhase(
                id="implement",
                kind="worker",
                task="Do the work",
            )
        ],
        parent_acceptance=parent_acceptance or ["Template acceptance"],
    )


def _texts(plan):
    return plan.checklist


def test_merges_template_parent_acceptance():
    plan = plan_parent_acceptance(
        _template(parent_acceptance=["Worker reports files", "Verification is recorded"]),
        goal="Ship scoped change",
    )

    assert plan.template_id == "custom_workflow"
    assert plan.goal == "Ship scoped change"
    assert "Worker reports files" in _texts(plan)
    assert "Verification is recorded" in _texts(plan)
    assert any(item.source == "template.parent_acceptance" for item in plan.items)


def test_merges_quality_contract_checks():
    contract = QualityContract(
        must_check=["real behavior"],
        sampling_plan=["sample changed path"],
        evidence_required=["pytest log"],
        forbidden_delivery=["untested final answer"],
    )

    plan = plan_parent_acceptance(_template(), goal="Verify contract", quality_contract=contract)

    texts = _texts(plan)
    assert "real behavior" in texts
    assert "sample changed path" in texts
    assert "pytest log" in texts
    assert "Reject delivery if it includes forbidden condition: untested final answer" in texts
    assert {
        "quality_contract.must_check",
        "quality_contract.sampling_plan",
        "quality_contract.evidence_required",
        "quality_contract.forbidden_delivery",
    }.issubset({item.source for item in plan.items})


def test_default_anti_acceptance_items_are_present():
    plan = plan_parent_acceptance(_template(), goal="Keep parent honest")

    texts = "\n".join(_texts(plan))
    assert "Do not accept worker self-reported PASS" in texts
    assert "Inspect the real changed artifacts" in texts
    assert "Check that cited evidence paths" in texts
    assert "Review residual risks" in texts
    assert "parent session final gate" in texts


def test_producer_critic_repair_requires_critic_gate():
    template = _template(
        "producer_critic_repair",
        phases=[
            WorkflowPhase(id="produce", kind="worker", task="Produce"),
            WorkflowPhase(id="critic", kind="review", task="Review", depends_on=["produce"]),
            WorkflowPhase(id="repair", kind="worker", task="Repair", depends_on=["critic"]),
        ],
    )

    plan = plan_parent_acceptance(template, goal="High quality change")

    assert any(item.category == "critic_gate" for item in plan.items)
    assert any("critic or reviewer result" in text for text in _texts(plan))


def test_single_worker_verified_does_not_force_critic_gate():
    template = _template(
        "single_worker_verified",
        phases=[
            WorkflowPhase(id="implement", kind="worker", task="Implement"),
            WorkflowPhase(id="verify", kind="verification", task="Verify", depends_on=["implement"]),
        ],
    )

    plan = plan_parent_acceptance(
        template,
        goal="Small scoped change",
        quality_contract={"evidence_required": ["focused pytest"]},
    )

    assert "focused pytest" in _texts(plan)
    assert not any(item.category == "critic_gate" for item in plan.items)
