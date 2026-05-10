"""LLM: tests for broad reusable subagent role templates.

函数/模块用途: 验证子代理角色模板是可复用的广义角色，内置模板有中文说明，用户也能按格式扩展。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_templates import load_role_template_store


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
    assert task.allowed_tools == ["read_file"]
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
# 函数用途: 找茬、测试、验收角色默认是可复用的检查型角色，不能直接写业务产物，也不能自验收。
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

    assert bug_finder.allowed_tools == ["list_files", "read_file", "search_text", "read_artifact"]
    assert "write_file" not in tester.allowed_tools
    assert "write_file" not in acceptor.allowed_tools
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
