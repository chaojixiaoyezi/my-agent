"""LLM: tests for tool loop, delegation, max rounds, catalog, and parser.

给人看的解释：
这个文件放所有和"工具循环流程"相关的测试：工具调用闭环、子代理派工和去重、
最大轮数收口、工具目录与推荐渲染、工具调用解析器兼容性。
"""

import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.log_analysis.storage import LocalLogStore
from agent_py_agent.agent.tools import ToolRegistry, ToolRegistryParams

from .backends import (
    DuplicateSubagentDelegationBackend,
    MaxToolRoundBackend,
    RepeatedDispatchBackend,
    SubagentDelegationBackend,
    ToolCallingBackend,
    make_tool_registry,
)


def test_tool_loop_and_prompt_transcript():
    """LLM: verify that a tool call round feeds tool output back to the model for a final answer.

    新手说明:
    模拟一次读文件工具调用，确认工具结果出现在后续 prompt 中，并且最终回答正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello tool world", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = ToolCallingBackend()
        result = agent.run("读取 notes.txt 并总结", save=False)
        assert result.response == "工具执行完成"
        assert result.tool_rounds == 1
        assert "hello tool world" in result.prompt


def test_agent_can_delegate_to_subagents_from_tool_call():
    """LLM: verify that a create_subagents tool call creates tasks and dispatch control works.

    新手说明:
    测试子代理创建、任务板查看、干跑调度和执行调度拦截。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = SubagentDelegationBackend()

        result = agent.run("请创建两个子代理做隔离 coding 场景测试", save=False)
        tasks = agent.subagents.list_runs()
        board = agent.tools.execute_call({"tool": "subagent_board", "limit": 5})
        dry_dispatch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "apply": False, "max_runners": 1}
        )
        blocked_dispatch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "execute_runners": True, "apply": False}
        )

        assert result.response == "已创建子代理任务并等待调度。"
        assert result.tool_rounds == 1
        assert len(tasks) == 2
        assert all("write_file" in task.allowed_tools for task in tasks)
        assert board.ok
        assert tasks[0].id in board.output
        assert dry_dispatch.ok
        assert '"dry_run": true' in dry_dispatch.output
        assert not blocked_dispatch.ok
        assert "必须配合 apply=true" in blocked_dispatch.output


def test_create_subagents_rejects_external_write_target_before_task_creation():
    """LLM: write-capable subagents should fail early for absolute paths outside workspace."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        external_dir = workspace.parent / "external-target"
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)

        result = agent.tools.execute_call(
            {
                "tool": "create_subagents",
                "goal": f"在 {external_dir} 创建一个 txt 文件",
                "allowed_tools": ["read_file", "write_file"],
            }
        )

        assert not result.ok
        assert "工作区外" in result.output
        assert agent.subagents.list_runs() == []


def test_repeated_orchestration_tool_call_is_not_executed_twice():
    """LLM: verify that identical consecutive orchestration tool calls are deduplicated.

    新手说明:
    连续两次 create_subagents 只应执行一次，第二次被拦截。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = DuplicateSubagentDelegationBackend()

        result = agent.run("请只创建一个子代理", save=False)
        tasks = agent.subagents.list_runs()

        assert result.response == "重复派工已被拦截并收口。"
        assert result.tool_rounds == 2
        assert len(tasks) == 1


def test_repeated_dispatch_is_allowed_for_parent_progress_loops():
    """LLM: dispatch_subagents may need repeated identical calls when rate limits leave pending children."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = RepeatedDispatchBackend()

        result = agent.run("继续推进父节点调度", save=False)

        assert result.response == "重复 dispatch 已允许继续推进。"
        assert result.tool_rounds == 2
        assert "阻止重复执行" not in result.prompt


def test_max_tool_rounds_generates_final_response():
    """LLM: verify that hitting max_tool_rounds still produces a final model response.

    新手说明:
    把 max_tool_rounds 设为 0，模型应该收到轮数限制提示并给出回答。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = MaxToolRoundBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == "工具轮数到顶后已正常收口。"
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2


def test_tool_catalog_and_recommended_sections():
    """LLM: verify that catalog and recommended-tools sections render correctly.

    新手说明:
    检查工具目录里有 http_request，推荐工具区能根据自然语言选到 http_request。
    """
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )

    catalog = registry.render_catalog_section()
    recommended = registry.render_recommended_tools_section("帮我测试一个 REST API 接口并查看返回")

    assert "# Tool Catalog" in catalog
    assert "http_request [api]" in catalog
    assert "适用场景" in catalog
    assert "## http_request" in recommended
    assert "推荐理由" in recommended


def test_tool_catalog_format_example_does_not_bias_to_path_param():
    """LLM: Tool call instructions should not teach all tools to pass a fake path parameter."""
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )

    catalog = registry.render_catalog_section()

    assert '{"tool": "tool_name", "path": "example"}' not in catalog
    assert '"actual_parameter_name": "actual_value"' in catalog
    assert '"param_name": "param_value"' not in catalog
    assert "不要写 param_name" in catalog


def test_tool_call_parser_unwraps_model_param_name_bundle():
    """LLM: tolerate models that wrap real tool parameters in a literal param_name bundle."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","param_name":{"path":"README.md"}}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_executor_unwraps_model_param_name_bundle(tmp_path: Path):
    """LLM: direct execution should also recover literal param_name bundles before tool dispatch."""
    (tmp_path / "notes.txt").write_text("bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call(
        {"tool": "read_file", "param_name": {"path": "notes.txt"}}
    )

    assert result.ok
    assert "bundle recovered" in result.output


def test_tool_call_parser_unwraps_model_category_bundle():
    """LLM: tolerate models that wrap params by tool category such as orchestration or filesystem."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"subagent_board","orchestration":{"limit":20,"status":"running"}}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "subagent_board", "limit": 20, "status": "running"}]


def test_tool_executor_unwraps_model_filesystem_bundle(tmp_path: Path):
    """LLM: filesystem category wrappers should be flattened before file tool execution."""
    (tmp_path / "notes.txt").write_text("category bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call(
        {"tool": "read_file", "filesystem": {"path": "notes.txt"}}
    )

    assert result.ok
    assert "category bundle recovered" in result.output


def test_tool_call_parser_unwraps_model_memory_bundle():
    """LLM: memory/read_artifact wrappers should flatten to stable tool params."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"read_artifact","memory":{"artifact_ref":"/tmp/out.json","offset":0,"max_chars":4000}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [
        {"tool": "read_artifact", "artifact_ref": "/tmp/out.json", "offset": 0, "max_chars": 4000}
    ]


def test_tool_spec_catalog_entry_includes_first_example():
    """LLM: Compact catalog entries should show tool-specific JSON when examples are available."""
    from agent_py_agent.agent.tools import ToolSpec

    spec = ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
        description="create child runs",
        use_cases=["split hierarchy"],
        avoid_when=[],
        keywords=[],
        parameters={"children": "child specs", "apply": "write"},
        examples=['{"tool":"schedule_child_subagents","apply":true,"children":[]}'],
    )

    entry = spec.render_catalog_entry()

    assert "示例" in entry
    assert '"children":[]' in entry


def test_tool_call_parser_accepts_subagent_call_alias():
    """LLM: verify that [SUBAGENT_CALL] opening tag is accepted as an alias for [TOOL_CALL].

    新手说明:
    有些模型输出 [SUBAGENT_CALL]，解析器应该和 [TOOL_CALL] 一视同仁。
    """
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )
    calls = registry.parse_tool_calls(
        '[SUBAGENT_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_read_call():
    """LLM: verify that Qwen-style XML-ish function call for read is parsed correctly.

    新手说明:
    Qwen 模型可能输出 <function=read> 格式的工具调用，需要正确映射到 read_file。
    """
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )
    calls = registry.parse_tool_calls(
        "\n"
        "<function=read>\n"
        "<parameter=file_path>\nREADME.md\n</parameter>\n"
        "</function>\n"
        ""
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_write_call():
    """LLM: verify that Qwen-style XML-ish function call for write handles HTML entities.

    新手说明:
    Qwen 写文件调用里 &amp; 应该被解码成 &。
    """
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )
    calls = registry.parse_tool_calls(
        '<function name="write">'
        '<parameter name="file_path">notes.txt</parameter>'
        '<parameter name="content">hello &amp; hi</parameter>'
        "</function>"
    )

    assert calls == [{"tool": "write_file", "path": "notes.txt", "content": "hello & hi"}]


def test_tool_call_parser_reports_incomplete_qwen_xmlish_call():
    """LLM: verify that an incomplete Qwen XML-ish call is reported as a parse error.

    新手说明:
    第二个调用缺少闭合标签，解析器应返回 __parse_error__ 而不是崩溃。
    """
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )
    calls = registry.parse_tool_calls(
        "<function=read><parameter=file_path>A.md</parameter>\n"
        "<function=read><parameter=file_path>B.md</parameter>"
    )

    assert calls[0] == {"tool": "read_file", "path": "A.md"}
    assert calls[1]["tool"] == "__parse_error__"
    assert "missing a closing " in calls[1]["error"]
    assert "B.md" in calls[1]["raw"]


def test_tool_call_parser_and_executor_reject_non_object_payloads():
    """LLM: verify that non-object JSON payloads are rejected at parse and execute time.

    新手说明:
    传入数组或列表应该报错，提示必须是 JSON 对象。
    """
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls("[TOOL_CALL]\n[1, 2, 3]\n[/TOOL_CALL]")
    parsed_result = registry.execute_call(calls[0])
    direct_result = registry.execute_call(["not", "a", "dict"])

    assert calls[0]["tool"] == "__parse_error__"
    assert "JSON 对象" in calls[0]["error"]
    assert not parsed_result.ok
    assert "JSON 对象" in parsed_result.output
    assert not direct_result.ok
    assert "JSON 对象" in direct_result.output


def test_tool_allowlist_limits_prompt_and_execution():
    """LLM: verify that allowed_tools filters both the prompt catalog and tool execution.

    新手说明:
    只允许 read_file 时，write_file 不应出现在 prompt 里，执行也应被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        result = agent.run("读取 notes.txt", save=False, allowed_tools=["read_file"])
        blocked = agent.tools.execute_call(
            {"tool": "write_file", "path": "x.txt", "content": "x"},
            allowed_tools=["read_file"],
        )

        assert "read_file [filesystem]" in result.prompt
        assert "write_file [filesystem]" not in result.prompt
        assert not blocked.ok
        assert "未授权" in blocked.output


import json


def _make_tool_registry(workspace: Path) -> ToolRegistry:
    from agent_py_agent.agent.tools import ToolRegistryParams
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=12000,
            max_entries=100,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )


def test_security_tools_are_hidden_by_default_and_require_authorization():
    """LLM: verify security tools are hidden from catalog and blocked without authorization.

    新手说明:
    安全工具默认不出现在工具目录和推荐列表中，调用时会被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)

        catalog = registry.render_catalog_section()
        recommended = registry.render_recommended_tools_section("investigate security logs for attacker ip")
        blocked = registry.execute_call(
            {
                "tool": "security_query",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            }
        )

        assert "security_query [log_analysis]" not in catalog
        assert "security_query" not in recommended
        assert not blocked.ok
        assert "not authorized" in blocked.output


def test_security_tools_are_exposed_for_security_capability_or_tool_grant():
    """LLM: verify security tools appear and work when capability or tool grant is provided.

    新手说明:
    授予 logs/security 能力或显式允许 security_query 工具后，安全工具可正常使用。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)
        store = LocalLogStore(workspace)
        store.upsert_event(
            {
                "event_id": "evt-1",
                "event_time": "2026-04-30T10:00:00Z",
                "source_id": "waf-prod",
                "alert_type": "web_attack",
                "attacker_ip": "198.51.100.10",
                "payload": "A" * 500,
            }
        )

        capability_catalog = registry.render_catalog_section(granted_capabilities=["logs/security"])
        allowed_catalog = registry.render_catalog_section(allowed_tools=["security_query"])
        result = registry.execute_call(
            {
                "tool": "security_query",
                "attacker_ip": "198.51.100.10",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            },
            granted_capabilities=["logs/security"],
        )
        payload = json.loads(result.output)

        assert "security_query [log_analysis]" in capability_catalog
        assert "security_query [log_analysis]" in allowed_catalog
        assert result.ok
        assert payload["tool"] == "security_query"
        assert payload["row_count"] == 1
        assert payload["evidence_refs"]
        assert "rows" not in payload
        assert "preview_rows" in payload
