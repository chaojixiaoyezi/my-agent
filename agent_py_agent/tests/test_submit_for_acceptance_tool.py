from __future__ import annotations


# LLM: submit_for_acceptance is a generic delivery handoff tool, not a task-specific template.
# 函数用途: 验证工具目录中存在显式提交验收工具，模型能主动交卷但不能绕过系统验收。
def test_submit_for_acceptance_tool_is_registered(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=3,
            catalog_limit=50,
            retrieval_limit=5,
            vector_search_enabled=False,
        )
    )

    assert "submit_for_acceptance" in registry.tools
    spec = registry.tools["submit_for_acceptance"].spec
    assert spec.category == "delivery"
    assert spec.effect == "read_only"
    assert "通用" in spec.description


# LLM: the tool itself only records intent; machine closeout remains the authority after the tool round.
# 函数用途: 验证提交工具不会自称验收通过，只返回等待系统验收的结构化提示。
def test_submit_for_acceptance_tool_returns_submission_intent(tmp_path):
    from agent_py_agent.agent.tooling.delivery_acceptance import SubmitForAcceptanceTool

    result = SubmitForAcceptanceTool().execute({})

    assert result.ok is True
    assert result.tool == "submit_for_acceptance"
    assert "已提交系统验收" in result.output
    assert result.result_envelope["submission"] == "acceptance_requested"
