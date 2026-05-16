from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.settings import AgentConfig


class TestPromptBuilderInit:
    def test_init_with_valid_config_and_root(self):
        config = AgentConfig()
        root = Path("/tmp/test")
        builder = PromptBuilder(config, root)
        assert builder.config is config
        assert builder.root == root

    def test_init_custom_system_prompt(self):
        config = AgentConfig(system_prompt="custom system prompt")
        builder = PromptBuilder(config, Path("/tmp"))
        assert builder.config.system_prompt == "custom system prompt"


class TestReadPromptFiles:
    def test_read_prompt_files_empty(self, tmp_path):
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert result == []

    def test_read_prompt_files_none_extra(self, tmp_path):
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(None)
        assert result == []

    def test_read_prompt_files_nonexistent(self, tmp_path):
        config = AgentConfig(prompt_files=["nonexistent.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert result == []

    def test_read_prompt_files_absolute_exists(self, tmp_path):
        test_file = tmp_path / "absolute.txt"
        test_file.write_text("absolute content", encoding="utf-8")
        config = AgentConfig(prompt_files=[str(test_file)])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 1
        assert "absolute content" in result[0]

    def test_read_prompt_files_relative(self, tmp_path):
        pf = tmp_path / "rules.txt"
        pf.write_text("rules content", encoding="utf-8")
        config = AgentConfig(prompt_files=["rules.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 1
        assert "rules content" in result[0]

    def test_read_prompt_files_multiple(self, tmp_path):
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("c1", encoding="utf-8")
        f2.write_text("c2", encoding="utf-8")
        config = AgentConfig(prompt_files=["f1.txt", "f2.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert len(result) == 2

    def test_read_prompt_files_extra_files(self, tmp_path):
        ef = tmp_path / "extra.txt"
        ef.write_text("extra", encoding="utf-8")
        config = AgentConfig(prompt_files=[])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files(extra_files=["extra.txt"])
        assert len(result) == 1
        assert "extra" in result[0]

    def test_read_prompt_files_includes_header(self, tmp_path):
        pf = tmp_path / "prompt.txt"
        pf.write_text("content", encoding="utf-8")
        config = AgentConfig(prompt_files=["prompt.txt"])
        builder = PromptBuilder(config, tmp_path)
        result = builder.read_prompt_files()
        assert "# Prompt File:" in result[0]


class TestBuildBasic:
    def test_build_empty_prompt(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("", [])
        assert "# System" in result
        assert config.system_prompt in result

    def test_build_uses_system_prompt_override(self, tmp_path):
        """LLM: subagent runner turns can replace parent/root system identity without mutating config."""
        config = AgentConfig(system_prompt="root system prompt")
        builder = PromptBuilder(config, tmp_path)

        result = builder.build("hello", [], system_prompt_override="subagent system prompt")

        assert "# System\nsubagent system prompt" in result
        assert "root system prompt" not in result

    def test_build_no_memories(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [])
        assert "hello" in result
        assert "（无相关记忆）" in result

    def test_build_includes_workspace_context(self, tmp_path):
        """主代理每轮都能看到真实 workspace，避免模型猜 /workspace 这类假路径。"""
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)

        result = builder.build("把报告写到 artifacts", [])

        assert "# Workspace Context" in result
        assert f"primary_workspace_root: {tmp_path}" in result
        assert "不要把 /workspace 当作真实路径" in result

    def test_build_includes_current_local_date(self, tmp_path):
        """主代理每轮都能看到当前日期，报告日期不要从旧文件里猜。"""
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)

        result = builder.build("写报告", [])

        assert f"current_local_date: {date.today().isoformat()}" in result
        assert "写报告日期时优先使用 current_local_date" in result

    def test_build_single_memory(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [MemoryRecord(role="user", content="test memory")]
        result = builder.build("continue", memories)
        assert "[dialogue] user: test memory" in result

    def test_build_multiple_memories(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [
            MemoryRecord(role="user", content="first"),
            MemoryRecord(role="assistant", content="second"),
        ]
        result = builder.build("task", memories)
        assert "first" in result
        assert "second" in result


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
        assert "（无）" in result

    def test_build_inject_single_item(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], inject=["injected line"])
        assert "injected line" in result

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

    def test_build_with_tool_catalog(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "hello",
            [],
            tools=ToolSections(
                tool_catalog_section="# Tools\n- read_file\n- write_file",
            ),
        )
        assert "read_file" in result
        assert "write_file" in result

    def test_build_with_tool_recommendations(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "hello",
            [],
            tools=ToolSections(
                tool_recommendations_section="# Recommended\n- tool1",
            ),
        )
        assert "tool1" in result


class TestBuildToolContext:
    def test_build_no_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], tools=ToolSections(tool_context=None))
        assert "（无）" in result
        assert "# User Task" in result

    def test_build_empty_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], tools=ToolSections(tool_context=[]))
        assert "（无）" in result

    def test_build_single_tool_context(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "hello",
            [],
            tools=ToolSections(
                tool_context=["[TOOL_CALL] read_file...[/TOOL_CALL]"],
            ),
        )
        assert "# Tool Transcript" in result
        assert "Continue From Tool Transcript" in result

    def test_build_multiple_tool_context_entries(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "task",
            [],
            tools=ToolSections(
                tool_context=["[TOOL_CALL] tool1 [/TOOL_CALL]", "[TOOL_CALL] tool2 [/TOOL_CALL]"],
            ),
        )
        assert "tool1" in result
        assert "tool2" in result

    def test_build_continuation_instruction(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "task",
            [],
            tools=ToolSections(
                tool_context=["[TOOL_CALL] tool...[/TOOL_CALL]"],
            ),
        )
        assert "不要重新开始任务" in result
        assert "不要重复调用同一个工具" in result


class TestBuildPromptFilesParam:
    def test_build_with_prompt_files_param(self, tmp_path):
        pf = tmp_path / "dynamic.txt"
        pf.write_text("dynamic rule", encoding="utf-8")
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [], prompt_files=["dynamic.txt"])
        assert "dynamic rule" in result
        assert "# Dynamic Prompt Files" in result

    def test_build_dynamic_files_empty_when_none(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("hello", [])
        assert "# Dynamic Prompt Files" in result
        assert "（无）" in result

    def test_task_local_context_suppresses_owner_memory_and_home_files(self, tmp_path):
        """子代理 task-local prompt 不能混入主代理长期记忆、家目录制度或全局 prompt 文件。"""
        global_prompt = tmp_path / "GLOBAL.md"
        global_prompt.write_text("GLOBAL SECRET", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        agents = home / "AGENTS.md"
        agents.write_text("HOME SECRET", encoding="utf-8")
        lessons = home / "lessons"
        lessons.mkdir()
        home_paths = SimpleNamespace(
            agents_md=agents,
            soul_md=home / "SOUL.md",
            user_md=home / "USER.md",
            memory_md=home / "memory.md",
            memory_lessons_dir=lessons,
        )
        config = AgentConfig(system_prompt="System", prompt_files=[str(global_prompt)], home_context_enabled=True)
        builder = PromptBuilder(config, tmp_path, home_paths=home_paths)
        request = PromptBuildRequest(
            user_prompt="subagent task",
            memories=[MemoryRecord(role="user", content="MEMORY SECRET", kind="dialogue")],
            context_scope="task_local",
        )

        result = builder.build(request=request)

        assert "MEMORY SECRET" not in result
        assert "GLOBAL SECRET" not in result
        assert "HOME SECRET" not in result
        assert "（无相关记忆）" in result

    def test_control_plane_context_suppresses_owner_memory_and_home_files(self, tmp_path):
        """控制面调用也不能混入主代理长期记忆、家目录制度或全局 prompt 文件。"""
        global_prompt = tmp_path / "GLOBAL.md"
        global_prompt.write_text("GLOBAL SECRET", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        agents = home / "AGENTS.md"
        agents.write_text("HOME SECRET", encoding="utf-8")
        home_paths = SimpleNamespace(
            agents_md=agents,
            soul_md=home / "SOUL.md",
            user_md=home / "USER.md",
            memory_md=home / "memory.md",
            memory_lessons_dir=home / "lessons",
        )
        config = AgentConfig(system_prompt="System", prompt_files=[str(global_prompt)], home_context_enabled=True)
        builder = PromptBuilder(config, tmp_path, home_paths=home_paths)
        request = PromptBuildRequest(
            user_prompt="planner task",
            memories=[MemoryRecord(role="user", content="MEMORY SECRET", kind="dialogue")],
            context_scope="control_plane",
        )

        result = builder.build(request=request)

        assert "MEMORY SECRET" not in result
        assert "GLOBAL SECRET" not in result
        assert "HOME SECRET" not in result


class TestBuildFullPrompt:
    def test_build_full_prompt_order(self, tmp_path):
        config = AgentConfig(system_prompt="system prompt content")
        builder = PromptBuilder(config, tmp_path)
        memories = [MemoryRecord(role="user", content="memory content")]
        result = builder.build(
            "user task",
            memories,
            inject=["injection"],
            tools=ToolSections(
                tool_catalog_section="# Tools\n- tool",
                tool_recommendations_section="# Recommended\n- tool",
            ),
        )
        assert result.index("# System") < result.index("# Related Memory")
        assert result.index("# Related Memory") < result.index("# Dynamic Prompt Files")
        assert result.index("# Runtime Injection") < result.index("# Tools")

    def test_build_with_user_task_section(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build("my user task", [])
        assert "# User Task" in result
        assert "my user task" in result

    def test_build_with_tool_transcript_section(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        result = builder.build(
            "task",
            [],
            tools=ToolSections(
                tool_context=["[TOOL_CALL] tool...[/TOOL_CALL]"],
            ),
        )
        assert "# Tool Transcript" in result
        assert "# User Task" in result


class TestBuildMemoryKinds:
    def test_build_memory_with_different_kinds(self, tmp_path):
        config = AgentConfig()
        builder = PromptBuilder(config, tmp_path)
        memories = [
            MemoryRecord(role="system", content="rule", kind="rule"),
            MemoryRecord(role="user", content="note", kind="note"),
        ]
        result = builder.build("test", memories)
        assert "[rule] system: rule" in result
        assert "[note] user: note" in result
