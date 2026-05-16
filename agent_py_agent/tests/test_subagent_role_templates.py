"""LLM: tests for broad reusable subagent role templates.

函数/模块用途: 验证子代理角色模板是可复用的广义角色，内置模板有中文说明，用户也能按格式扩展。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentExecutionContext
from agent_py_agent.agent.subagents.role_templates import (
    load_role_template_store,
    role_template_detail_text,
    role_template_index_text,
)

BUILTIN_ROLE_IDS = ["acceptor", "bug_finder", "coordinator", "researcher", "tester", "worker", "writer"]


# LLM: _write_ppt_polisher_template creates a reusable user role fixture.
# 函数用途: 写入带中文说明的 PPT 润色角色模板，供用户模板加载相关测试复用。
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


# LLM: test_builtin_role_templates_are_bilingual_and_broad protects the user-facing role catalog.
# 函数用途: 确保内置找茬、测试、验收模板都有中文说明，并声明能处理多个目标而不是小动作。
def test_builtin_role_templates_are_bilingual_and_broad():
    store = load_role_template_store()

    for template_id in ["bug_finder", "tester", "acceptor"]:
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


# LLM: test_user_role_template_can_extend_catalog covers user-defined role format loading.
# 函数用途: 用户可以在自定义目录按 JSON 模板新增广义角色，且保留中文可读字段。
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
    assert "replace_in_file" not in task.allowed_tools
    assert any("PPT润色子代理" in check for check in task.acceptance_checks)


# LLM: test_user_template_role_can_be_selected_from_natural_name protects external template resolution.
# 函数用途: 用户模板目录新增角色后，LLM 写出带前后缀的自然角色名也能落到对应模板。
def test_user_template_role_can_be_selected_from_natural_name(tmp_path):
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
        role="slide_ppt_polisher_lead",
    )

    assert task.role == "slide_ppt_polisher_lead"
    assert "list_files" in task.allowed_tools
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "replace_in_file" in task.allowed_tools
    assert any("PPT润色子代理" in check for check in task.acceptance_checks)


# LLM: test_tiny_action_template_is_rejected keeps role templates from becoming one-off tasks.
# 函数用途: 防止把“检查某个按钮”这种小动作登记成内置/自定义角色模板。
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


# LLM: test_quality_role_contracts_use_template_defaults verifies core new QA roles.
# 函数用途: 找茬、测试、验收角色默认是可复用的检查型角色，可以写报告/修复建议，但不能自验收。
def test_quality_role_contracts_use_template_defaults(tmp_path):
    manager = SubAgentManager(tmp_path)

    bug_finder = manager.create_run(
        goal="检查多个 worker 的购物网站实现",
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
    acceptor = manager.create_run(
        goal="验收购物网站交付物",
        thought="accept",
        plan=["read criteria", "judge"],
        role="acceptor",
    )

    assert bug_finder.allowed_tools == [
        "list_files",
        "read_file",
        "search_text",
        "read_artifact",
        "fetch_url",
        "http_request",
        "write_file",
        "append_file",
        "replace_in_file",
        "capability_request",
    ]
    assert "write_file" in tester.allowed_tools
    assert "write_file" in acceptor.allowed_tools
    assert bug_finder.quality_contract.cannot_self_accept is True
    assert tester.quality_contract.parent_final_gate is True
    assert acceptor.quality_contract.parent_final_gate is True
    assert any("找茬" in check for check in bug_finder.acceptance_checks)
    assert any("测试" in check for check in tester.acceptance_checks)
    assert any("验收" in check for check in acceptor.acceptance_checks)


# LLM: test_worker_template_supplies_default_write_tools proves automatic policy is useful.
# 函数用途: 默认空工具配置时，执行型子代理仍能获得安全文件读写工具，不需要用户手填几十个工具名。
def test_worker_template_supplies_default_write_tools(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="实现购物网站页面",
        thought="build",
        plan=["implement", "verify"],
        role="worker",
    )

    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "replace_in_file" in task.allowed_tools


# LLM: test_role_template_index_is_compact_catalog_metadata locks the lazy prompt boundary.
# 函数用途: 主代理常驻只需要知道有哪些模板和位置，不应加载完整系统提示词或默认工具细节。
def test_role_template_index_is_compact_catalog_metadata():
    index = role_template_index_text()

    assert "worker" in index
    assert "执行子代理" in index
    assert "适用=" in index
    assert "不适用=" in index
    assert "已有明确目标" in index
    assert "模板位置" in index
    assert "你是执行子代理" not in index
    assert "write_file" not in index


# LLM: test_role_template_detail_text_loads_prompt_contract_on_demand proves details stay available when dispatching.
# 函数用途: 真正派工时能按角色加载完整中文提示片段、输出合同和默认工具，而不是只拿摘要。
def test_role_template_detail_text_loads_prompt_contract_on_demand():
    detail = role_template_detail_text(roles=["worker"])

    assert "worker" in detail
    assert "你是执行子代理" in detail
    assert "默认工具" in detail
    assert "write_file" in detail
    assert "验收子代理" not in detail


def test_coordinator_template_says_parent_authority_covers_children_without_disabling_work():
    detail = role_template_detail_text(roles=["coordinator"])

    assert "上层权限应覆盖下层" in detail
    assert "继承产物写入根" in detail
    assert "可以直接完成" in detail
    assert "需要多人视角" in detail
    assert "child_coordinator" in detail
    assert "worker/writer/leaf_worker" in detail
    assert "subagent_message" in detail
    assert "平级讨论" in detail


# LLM: test_all_builtin_role_templates_are_visible_in_main_index covers role selection discovery.
# 函数用途: 主代理常驻索引必须能看到每个内置模板，但不能提前加载完整提示词和工具清单。
def test_all_builtin_role_templates_are_visible_in_main_index():
    store = load_role_template_store()
    index = role_template_index_text()

    for template_id in BUILTIN_ROLE_IDS:
        template = store.get(template_id)
        assert template is not None
        assert template.id in index
        assert template.name_zh in index
        assert template.summary_zh in index
        assert template.source_path in index
        assert template.prompt_zh not in index
        for tool_name in template.default_tools:
            assert tool_name not in index


# LLM: _assert_other_role_prompts_absent keeps role template scope tests shallow for code-size guard.
# 函数用途: 确认 prompt/detail 只包含当前角色提示，避免测试函数出现多层循环和条件嵌套。
def _assert_other_role_prompts_absent(prompt: str, *, store, template_id: str) -> None:
    other_prompts = [item.prompt_zh for item in store.all() if item.id != template_id]
    assert all(text not in prompt for text in other_prompts)


# LLM: test_all_builtin_role_template_details_are_scoped covers on-demand role prompt loading.
# 函数用途: 每个模板派工时只能加载自己的详细提示，不能把其他角色的系统提示全塞进去。
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
        for tool_name in template.default_tools:
            assert tool_name in detail
        _assert_other_role_prompts_absent(detail, store=store, template_id=template_id)


# LLM: test_all_builtin_role_contracts_are_applied_on_create_run covers effective execution boundaries.
# 函数用途: 每个模板创建 run 后都应拿到对应默认工具、输出合同和父级验收边界。
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
        assert task.allowed_tools == template.default_tools
        assert any(template.output_contract_zh in check for check in task.acceptance_checks)
        assert task.quality_contract.cannot_self_accept is True
        assert task.quality_contract.parent_final_gate is True
        for tool_name in ["write_file", "append_file", "replace_in_file"]:
            assert tool_name in task.allowed_tools
        assert ("schedule_child_subagents" in task.allowed_tools) is template.can_spawn_children


# LLM: test_all_builtin_runner_prompts_load_current_role_template covers execution-time role guidance.
# 函数用途: 每个可执行角色的 runner prompt 都要拿到本角色提示；coordinator 额外拿模板全集用于继续派工。
def test_all_builtin_runner_prompts_load_current_role_template():
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
        if template_id == "coordinator":
            assert "模板详情" in prompt
            assert "你是找茬子代理" in prompt
        else:
            assert "当前角色模板详情" in prompt
            _assert_other_role_prompts_absent(prompt, store=store, template_id=template_id)
