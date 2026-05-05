from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.prompting import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig


class TestPromptBuilderInit:
    def test_init_with_valid_config_and_root(self):
        config = AgentConfig()
        root = Path("/tmp/test")
        builder = PromptBuilder(config, root)
        assert builder.config is config
        assert builder.root == root

    def test_init_config_is_agent_config_instance(self):
        config = AgentConfig(system_prompt="custom prompt")
        builder = PromptBuilder(config, Path("/tmp"))
        assert isinstance(builder.config, AgentConfig)
        assert builder.config.system_prompt == "custom prompt"


class TestReadPromptFiles:
    def test_read_prompt_files_empty_list(self, tmp_path):
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert result == []

    def test_read_prompt_files_none_extra(self, tmp_path):
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(None)
        assert result == []

    def test_read_prompt_files_extra_files_none(self, tmp_path):
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(extra_files=None)
        assert result == []

    def test_read_prompt_files_nonexistent(self, tmp_path):
        config = AgentConfig(prompt_files=["nonexistent.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert result == []

    def test_read_prompt_files_absolute_path_not_exists(self, tmp_path):
        config = AgentConfig(prompt_files=["/nonexistent/absolute.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert result == []

    def test_read_prompt_files_absolute_path_exists(self, tmp_path):
        test_file = tmp_path / "absolute.txt"
        test_file.write_text("absolute prompt content", encoding="utf-8")
        config = AgentConfig(prompt_files=[str(test_file)])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 1
        assert "# Prompt File:" in result[0]
        assert "absolute prompt content" in result[0]

    def test_read_prompt_files_relative_path_resolved(self, tmp_path):
        prompt_file = tmp_path / "rules.txt"
        prompt_file.write_text("dynamic rules here", encoding="utf-8")
        config = AgentConfig(prompt_files=["rules.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 1
        assert "dynamic rules here" in result[0]

    def test_read_prompt_files_multiple_files(self, tmp_path):
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("content1", encoding="utf-8")
        f2.write_text("content2", encoding="utf-8")
        config = AgentConfig(prompt_files=["f1.txt", "f2.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 2
        assert "content1" in result[0]
        assert "content2" in result[1]

    def test_read_prompt_files_extra_files_param(self, tmp_path):
        extra = tmp_path / "extra.txt"
        extra.write_text("extra content", encoding="utf-8")
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(extra_files=["extra.txt"])
        assert len(result) == 1
        assert "extra content" in result[0]

    def test_read_prompt_files_combined_config_and_extra(self, tmp_path):
        cf1 = tmp_path / "config.txt"
        cf1.write_text("config content", encoding="utf-8")
        ef1 = tmp_path / "extra.txt"
        ef1.write_text("extra content", encoding="utf-8")
        config = AgentConfig(prompt_files=["config.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(extra_files=["extra.txt"])
        assert len(result) == 2


class TestBuildBasic:
    def test_build_empty_user_prompt(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("", [])
        assert "# System" in result
        assert config.system_prompt in result
        assert "# Related Memory" in result
        assert "（无相关记忆）" in result

    def test_build_no_memories(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [])
        assert "hello" in result
        assert "（无相关记忆）" in result

    def test_build_with_single_memory(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [MemoryRecord(role="user", content="my task")]
        result = builder.build("continue", memories)
        assert "# Related Memory" in result
        assert "[dialogue] user: my task" in result

    def test_build_with_multiple_memories(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [
            MemoryRecord(role="user", content="first"),
            MemoryRecord(role="assistant", content="second"),
        ]
        result = builder.build("continue", memories)
        assert "[dialogue] user: first" in result
        assert "[dialogue] assistant: second" in result

    def test_build_memory_with_kind(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [MemoryRecord(role="system", content="rule content", kind="rule")]
        result = builder.build("test", memories)
        assert "[rule] system: rule content" in result


class TestBuildInject:
    def test_build_inject_none(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], inject=None)
        assert "# Runtime Injection" in result
        assert "（无）" in result

    def test_build_inject_empty_list(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], inject=[])
        assert "# Runtime Injection" in result
        assert "（无）" in result

    def test_build_inject_single_item(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], inject=["injected content"])
        assert "injected content" in result

    def test_build_inject_multiple_items(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], inject=["line1", "line2"])
        assert "line1" in result
        assert "line2" in result


class TestBuildToolSections:
    def test_build_no_tool_sections(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [])
        assert "# Tools" in result or "（当前未启用工具）" in result
        assert "# Recommended Tools" in result or "（当前无候选工具详情）" in result

    def test_build_with_tool_catalog_section(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_catalog_section="# Tools\n- read_file\n- write_file")
        result = builder.build("hello", [], tools=tools)
        assert "# Tools" in result
        assert "read_file" in result
        assert "write_file" in result

    def test_build_with_tool_recommendations_section(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_recommendations_section="# Recommended Tools\n- read_file")
        result = builder.build("hello", [], tools=tools)
        assert "# Recommended Tools" in result
        assert "read_file" in result


class TestBuildToolContext:
    def test_build_no_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_context=None)
        result = builder.build("hello", [], tools=tools)
        assert "（无）" in result
        assert "# User Task" in result
        assert "hello" in result

    def test_build_empty_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_context=[])
        result = builder.build("hello", [], tools=tools)
        assert "（无）" in result

    def test_build_single_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_context=["[TOOL_CALL] read_file ... [/TOOL_CALL]"])
        result = builder.build("hello", [], tools=tools)
        assert "# Tool Transcript" in result
        assert "Continue From Tool Transcript" in result
        assert "read_file" in result

    def test_build_multiple_tool_context_entries(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        entries = [
            "[TOOL_CALL] tool1 ... [/TOOL_CALL]",
            "[TOOL_CALL] tool2 ... [/TOOL_CALL]",
        ]
        tools = ToolSections(tool_context=entries)
        result = builder.build("hello", [], tools=tools)
        assert "tool1" in result
        assert "tool2" in result

    def test_build_tool_context_has_continuation_instruction(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_context=["[TOOL_CALL] tool ... [/TOOL_CALL]"])
        result = builder.build("hello", [], tools=tools)
        assert "Continue From Tool Transcript" in result
        assert "不要重新开始任务" in result


class TestBuildFullPrompt:
    def test_build_full_prompt_structure(self, tmp_path):
        config = AgentConfig(system_prompt="you are a helpful agent")
        builder = PromptBuilder(config, tmp_path)
        memories = [MemoryRecord(role="user", content="remember this")]
        tools = ToolSections(
            tool_catalog_section="# Tools\n- tool1",
            tool_recommendations_section="# Recommended Tools\n- tool1",
        )
        result = builder.build(
            "do the task",
            memories,
            inject=["runtime injection"],
            tools=tools,
        )
        assert "# System" in result
        assert "you are a helpful agent" in result
        assert "# Related Memory" in result
        assert "remember this" in result
        assert "# Runtime Injection" in result
        assert "runtime injection" in result
        assert "# Tools" in result
        assert "# User Task" in result
        assert "do the task" in result

    def test_build_without_tool_context_uses_user_task_directly(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("my task", [])
        assert "# User Task" in result
        assert "my task" in result

    def test_build_with_tool_context_uses_continuation_format(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        tools = ToolSections(tool_context=["[TOOL_CALL] call [/TOOL_CALL]"])
        result = builder.build("my task", [], tools=tools)
        assert "# Tool Transcript" in result
        assert "# User Task" in result
        assert "Continue From Tool Transcript" in result


class TestBuildDynamicPromptFiles:
    def test_build_with_prompt_files_param(self, tmp_path):
        pf = tmp_path / "dynamic.txt"
        pf.write_text("dynamic rule content", encoding="utf-8")
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], prompt_files=["dynamic.txt"])
        assert "dynamic rule content" in result
        assert "# Dynamic Prompt Files" in result

    def test_build_dynamic_files_section_empty_when_no_files(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [])
        assert "# Dynamic Prompt Files" in result
        assert "（无）" in result
