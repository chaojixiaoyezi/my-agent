"""LLM: tests for broad reusable subagent role templates.

函数/模块用途: 验证子代理角色模板是可复用的广义角色，内置模板有中文说明，用户也能按格式扩展。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentExecutionContext
from agent_py_agent.agent.subagents.role_templates import (
    load_role_template_store,
    role_template_detail_text,
    role_template_index_text,
)

BUILTIN_ROLE_IDS = ["bug_finder", "coordinator", "researcher", "tester", "worker", "writer"]


def test_reviewer_does_not_expand_diagnosis_to_unsolicited_repairs():
    from agent_py_agent.agent.model_guidance import (
        ACTION_AUTHORIZATION_GUIDANCE,
        VERIFICATION_EVIDENCE_GUIDANCE,
    )

    template = load_role_template_store().get("bug_finder")
    assert "仅要求检查或诊断时保持只读" in template.prompt_zh
    assert "不强制新建报告文件" in template.prompt_zh
    assert "如果问题很小" not in template.prompt_zh
    assert "不能把查找问题改写成发现后直接修复" in ACTION_AUTHORIZATION_GUIDANCE
    assert "当前版本而非旧进程或旧产物" in VERIFICATION_EVIDENCE_GUIDANCE
    # 软职责不是硬权限，不砍掉明确要求修复时需要的工具。
    assert template.can_write is True
    assert "edit_file" in template.default_tools


def _write_ppt_polisher_template(template_dir) -> None:
    template_dir.mkdir()
    (template_dir / "ppt_polisher.json").write_text(
        json.dumps(
            {
                "id": "ppt_polisher",
                "name": "PPT Polisher",
                "name_zh": "PPT润色子代理",
                "summary": "Review and polish presentation decks.",
                "summary_zh": "检查并润色演示文稿，可以同时看多个页面或多个文件。",
                "scope": "role",
                "handles_multiple_targets": True,
                "use_when_zh": ["需要统一风格、措辞、结构或讲述节奏。"],
                "do_not_use_when_zh": ["只需要改一个错别字时不用单独创建该角色。"],
                "default_tools": ["list_files", "read_file", "read_artifact"],
                "can_write": False,
                "can_accept": False,
                "output_contract": {"type": "findings"},
                "output_contract_zh": "输出问题清单、证据路径和建议修改方向。",
                "prompt_zh": "你是PPT润色子代理，先看整体叙事，再看页面细节。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_builtin_role_templates_are_bilingual_and_broad():
    store = load_role_template_store()

    for template_id in ["bug_finder", "tester"]:
        template = store.get(template_id)
        assert template is not None
        assert template.scope == "role"
        assert template.handles_multiple_targets is True
        assert template.name_zh
        assert template.summary_zh
        assert template.use_when_zh
        assert template.output_contract_zh
        assert template.source == "builtin"
        assert template.source_path.endswith(".json")


def test_user_role_template_can_extend_catalog(tmp_path):
    template_dir = tmp_path / "templates"
    _write_ppt_polisher_template(template_dir)

    store = load_role_template_store(user_template_dir=template_dir)
    template = store.get("ppt_polisher")

    assert template is not None
    assert template.source == "user"
    assert template.name_zh == "PPT润色子代理"
    assert template.handles_multiple_targets is True
    assert not store.issues

    manager = SubAgentManager(tmp_path / "workspace", role_template_dirs=[template_dir])
    task = manager.create_run(
        goal="检查三份 PPT 草稿",
        thought="polish",
        plan=["read", "report"],
        role="ppt_polisher",
        allowed_tools=["read_file", "write_file"],
    )
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "apply_patch" not in task.allowed_tools
    assert task.acceptance_checks == []


def test_user_template_role_can_be_selected_by_explicit_id(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "ppt_polisher.json").write_text(
        json.dumps(
            {
                "id": "ppt_polisher",
                "name": "PPT Polisher",
                "name_zh": "PPT润色子代理",
                "summary": "Review and polish presentation decks.",
                "summary_zh": "检查并润色演示文稿，可以同时看多个页面或多个文件。",
                "scope": "role",
                "handles_multiple_targets": True,
                "use_when_zh": ["需要统一风格、措辞、结构或讲述节奏。"],
                "default_tools": ["list_files", "read_file", "read_artifact"],
                "can_write": False,
                "output_contract": {"type": "findings"},
                "output_contract_zh": "输出问题清单、证据路径和建议修改方向。",
                "prompt_zh": "你是PPT润色子代理，先看整体叙事，再看页面细节。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = SubAgentManager(tmp_path / "workspace", role_template_dirs=[template_dir])
    task = manager.create_run(
        goal="检查三份 PPT 草稿",
        thought="polish",
        plan=["read", "report"],
        role="ppt_polisher",
    )

    assert task.role == "ppt_polisher"
    assert "list_files" in task.allowed_tools
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "apply_patch" in task.allowed_tools
    assert task.acceptance_checks == []


def test_tiny_action_template_is_rejected(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "check_login_button.json").write_text(
        json.dumps(
            {
                "id": "check_login_button",
                "name": "Check Login Button",
                "name_zh": "检查登录按钮",
                "summary": "Check one button.",
                "summary_zh": "只检查一个登录按钮。",
                "scope": "action",
                "handles_multiple_targets": False,
                "use_when_zh": ["只检查一个按钮。"],
                "default_tools": ["read_file"],
                "output_contract": {"type": "finding"},
                "output_contract_zh": "输出按钮问题。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = load_role_template_store(user_template_dir=template_dir)

    assert store.get("check_login_button") is None
    assert any("broad role" in issue.message for issue in store.issues)


def test_quality_role_contracts_use_template_defaults(tmp_path):
    manager = SubAgentManager(tmp_path)

    bug_finder = manager.create_run(
        goal="检查多个 worker 的示例网站实现",
        thought="find issues",
        plan=["scan outputs", "report findings"],
        role="bug_finder",
    )
    tester = manager.create_run(
        goal="对多个页面做回归测试",
        thought="test",
        plan=["plan tests", "report evidence"],
        role="tester",
    )
    expected_bug_finder_tools = [
        "list_files",
        "read_file",
        "search_text",
        "read_artifact",
        "web_search",
        "web_fetch",
        "write_file",
        "edit_file",
        "apply_patch",
        "capability_request",
    ]
    assert bug_finder.allowed_tools[:len(expected_bug_finder_tools)] == expected_bug_finder_tools
    assert "write_file" in tester.allowed_tools
    assert bug_finder.acceptance_checks == []
    assert tester.acceptance_checks == []


def test_worker_template_supplies_default_write_tools(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="实现示例网站页面",
        thought="build",
        plan=["implement", "verify"],
        role="worker",
    )

    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "edit_file" in task.allowed_tools
    assert "apply_patch" in task.allowed_tools


def test_role_template_index_is_compact_catalog_metadata():
    index = role_template_index_text()

    assert "worker" in index
    assert "执行子代理" in index
    assert "适用=" in index
    assert "不适用=" in index
    assert "已有明确目标" in index
    assert "模板位置" not in index
    assert "你是执行子代理" not in index
    assert "write_file" not in index


def test_role_template_index_can_include_source_path_for_debug_only():
    index = role_template_index_text(include_source_path=True)

    assert "模板位置" in index
    assert "role_template_catalog" in index


def test_role_template_detail_text_loads_prompt_contract_on_demand():
    detail = role_template_detail_text(roles=["worker"])

    assert "worker" in detail
    assert "你是执行子代理" in detail
    assert "默认工具" in detail
    assert "write_file" in detail
    assert "验收子代理" not in detail


def test_coordinator_template_says_parent_authority_covers_children_without_disabling_work():
    detail = role_template_detail_text(roles=["coordinator"])

    assert "当前上下文的权限是上界" in detail
    assert "下级继续继承或缩小" in detail
    assert "可以直接完成" in detail
    assert "需要多人视角" in detail
    assert "coordinator" in detail
    assert "worker 或 writer" in detail
    assert "child_coordinator" not in detail
    assert "leaf_worker" not in detail
    assert "send_guidance" in detail


def test_all_builtin_role_templates_are_visible_in_main_index():
    store = load_role_template_store()
    index = role_template_index_text()

    for template_id in BUILTIN_ROLE_IDS:
        template = store.get(template_id)
        assert template is not None
        assert template.id in index
        assert template.name_zh in index
        assert template.summary_zh in index
        assert template.source_path not in index
        assert template.prompt_zh not in index
        for tool_name in template.default_tools:
            assert tool_name not in index


def _assert_other_role_prompts_absent(prompt: str, *, store, template_id: str) -> None:
    other_prompts = [item.prompt_zh for item in store.all() if item.id != template_id]
    assert all(text not in prompt for text in other_prompts)


def test_all_builtin_role_template_details_are_scoped():
    store = load_role_template_store()

    for template_id in BUILTIN_ROLE_IDS:
        template = store.get(template_id)
        assert template is not None
        detail = role_template_detail_text(roles=[template_id])
        assert template.id in detail
        assert template.name_zh in detail
        assert template.prompt_zh in detail
        assert template.output_contract_zh in detail
        assert template.source_path not in detail
        for tool_name in template.default_tools:
            assert tool_name in detail
        _assert_other_role_prompts_absent(detail, store=store, template_id=template_id)


def test_all_builtin_role_contracts_are_applied_on_create_run(tmp_path):
    store = load_role_template_store()
    manager = SubAgentManager(tmp_path)

    for template_id in BUILTIN_ROLE_IDS:
        template = store.get(template_id)
        assert template is not None
        task = manager.create_run(
            goal=f"验证 {template.name_zh} 的模板执行边界",
            thought="role coverage",
            plan=["read contract", "report evidence"],
            role=template_id,
        )

        assert task.role == template_id
        assert task.allowed_tools[:len(template.default_tools)] == list(template.default_tools)
        assert task.acceptance_checks == []
        assert task.attributes["role_template"]["prompt_zh"] == template.prompt_zh
        for tool_name in ["write_file", "edit_file", "apply_patch"]:
            assert tool_name in task.allowed_tools
        assert ("create_subagents" in task.allowed_tools) is template.can_spawn_children


def test_runner_prompt_loads_current_role_behavior_without_other_leaf_details():
    store = load_role_template_store()

    for template_id in BUILTIN_ROLE_IDS:
        template = store.get(template_id)
        assert template is not None
        context = SubAgentExecutionContext(
            run_id=f"{template_id}-run",
            generated_at=1.0,
            goal=f"执行 {template.name_zh} 覆盖测试",
            thought="role prompt coverage",
            plan=["follow contract"],
            role=template_id,
            agent_name=template_id,
            allowed_tools=template.default_tools,
            acceptance_checks=[template.output_contract_zh],
        )
        prompt = _build_subagent_runner_prompt(context)

        assert template.prompt_zh in prompt
        assert "当前角色行为" in prompt
        assert "模板详情" not in prompt
        _assert_other_role_prompts_absent(prompt, store=store, template_id=template_id)
