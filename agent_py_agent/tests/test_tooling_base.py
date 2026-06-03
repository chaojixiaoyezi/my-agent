"""工具基类和注册机制测试 - 工具基类、注册机制、权限模型。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBaseTool:
    """测试 BaseTool 基类。"""

    def test_base_tool_has_spec(self):
        """BaseTool 子类应有 spec 属性。"""
        from agent_py_agent.agent.tooling.models import BaseTool

        class DummyTool(BaseTool):
            spec = MagicMock()

            def execute(self, params):
                pass

        tool = DummyTool()
        assert tool.spec is not None

    def test_base_tool_execute_not_implemented(self):
        """BaseTool.execute 应抛出 NotImplementedError。"""
        from agent_py_agent.agent.tooling.models import BaseTool

        tool = BaseTool()
        with pytest.raises(NotImplementedError):
            tool.execute({})


class TestToolSpec:
    """测试 ToolSpec 数据类。"""

    def test_tool_spec_basic_creation(self):
        """基本 ToolSpec 创建。"""
        from agent_py_agent.agent.tooling.models import ToolSpec

        spec = ToolSpec(
            name="test_tool",
            category="test",
            description="A test tool",
            use_cases=["testing"],
            avoid_when=["production"],
            keywords=["test", "demo"],
            parameters={"param1": "description"},
        )

        assert spec.name == "test_tool"
        assert spec.category == "test"
        assert spec.description == "A test tool"
        assert len(spec.use_cases) == 1
        assert len(spec.avoid_when) == 1
        assert len(spec.keywords) == 2

    def test_tool_spec_render_catalog_entry(self):
        """渲染工具目录条目。"""
        from agent_py_agent.agent.tooling.models import ToolSpec

        spec = ToolSpec(
            name="read_file",
            category="filesystem",
            description="读取文件内容",
            use_cases=["查看代码", "查看配置"],
            avoid_when=["二进制文件"],
            keywords=["读", "文件", "code"],
            parameters={"path": "文件路径"},
        )

        entry = spec.render_catalog_entry()

        assert "read_file" in entry
        assert "filesystem" in entry
        assert "读取文件内容" in entry
        assert "path" in entry

    def test_tool_spec_render_detail_entry(self):
        """渲染工具详细条目。"""
        from agent_py_agent.agent.tooling.models import ToolSpec

        spec = ToolSpec(
            name="read_file",
            category="filesystem",
            description="读取文件内容",
            use_cases=["查看代码", "查看配置"],
            avoid_when=["二进制文件"],
            keywords=["读", "文件"],
            parameters={"path": "文件路径"},
            parameter_details={"path": "相对工作区的文件路径"},
            examples=['{"tool": "read_file", "path": "file.txt"}'],
        )

        detail = spec.render_detail_entry()

        assert "## read_file" in detail
        assert "文件路径" in detail
        assert "file.txt" in detail


class TestToolExecutionResult:
    """测试 ToolExecutionResult 数据类。"""

    def test_execution_result_basic(self):
        """基本执行结果创建。"""
        from agent_py_agent.agent.tooling.models import ToolExecutionResult

        result = ToolExecutionResult(
            tool="test_tool",
            ok=True,
            output="执行成功",
        )

        assert result.tool == "test_tool"
        assert result.ok is True
        assert result.output == "执行成功"

    def test_execution_result_render_for_prompt(self):
        """渲染执行结果用于 prompt。"""
        from agent_py_agent.agent.tooling.models import ToolExecutionResult

        result = ToolExecutionResult(
            tool="read_file",
            ok=True,
            output="file content",
        )

        rendered = result.render_for_prompt()

        assert "tool=read_file" in rendered
        assert "status=ok" in rendered
        assert "file content" in rendered

    def test_execution_result_error_render(self):
        """错误执行结果渲染。"""
        from agent_py_agent.agent.tooling.models import ToolExecutionResult

        result = ToolExecutionResult(
            tool="write_file",
            ok=False,
            output="文件不存在",
        )

        rendered = result.render_for_prompt()

        assert "status=error" in rendered
        assert "文件不存在" in rendered

    def test_execution_result_error_contract_auto_classifies_failure(self):
        from agent_py_agent.agent.tooling.models import ToolExecutionResult

        result = ToolExecutionResult(
            tool="write_file",
            ok=False,
            output="写入被阻止: 当前路径 outside workspace /tmp/outside.txt",
        )

        assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
        assert result.error_category == "path"
        assert result.retryable is False
        assert result.recommended_action == "fix_path_within_allowed_roots"
        assert "修正路径" in result.recovery_hint
        rendered = result.render_for_prompt()
        assert "error_code=PATH_OUTSIDE_WORKSPACE" in rendered
        assert "recommended_action=fix_path_within_allowed_roots" in rendered

    def test_execution_result_ok_has_no_error_contract(self):
        from agent_py_agent.agent.tooling.models import ToolExecutionResult

        result = ToolExecutionResult(tool="read_file", ok=True, output="hello")

        assert result.error_code == ""
        assert result.error_category == ""
        assert result.recommended_action == ""
        assert "error_code=" not in result.render_for_prompt()


class TestToolSearchHit:
    """测试 ToolSearchHit 数据类。"""

    def test_search_hit_basic(self):
        """基本搜索命中创建。"""
        from agent_py_agent.agent.tooling.models import ToolSearchHit

        hit = ToolSearchHit(
            name="read_file",
            score=5.0,
            reasons=["命中工具名"],
        )

        assert hit.name == "read_file"
        assert hit.score == 5.0
        assert len(hit.reasons) == 1


class TestKeywordToolSearchProvider:
    """测试关键词工具检索器。"""

    def test_search_by_name(self):
        """按工具名搜索。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name="read_file",
                category="filesystem",
                description="读取文件",
                use_cases=["读文件"],
                avoid_when=[],
                keywords=["读", "文件"],
                parameters={},
            ),
            ToolSpec(
                name="write_file",
                category="filesystem",
                description="写入文件",
                use_cases=["写文件"],
                avoid_when=[],
                keywords=["写", "文件"],
                parameters={},
            ),
        ]

        hits = provider.search("read", specs, limit=10)

        assert len(hits) >= 1
        assert any(h.name == "read_file" for h in hits)

    def test_search_by_keyword(self):
        """按关键词搜索。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name="shell",
                category="system",
                description="执行 shell 命令",
                use_cases=["运行命令"],
                avoid_when=[],
                keywords=["shell", "命令", "终端"],
                parameters={},
            ),
        ]

        hits = provider.search("命令", specs, limit=10)

        assert len(hits) >= 1

    def test_search_by_category(self):
        """按类别搜索。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name="web_tool",
                category="web",
                description="Web 请求工具",
                use_cases=["发请求"],
                avoid_when=[],
                keywords=["http", "请求"],
                parameters={},
            ),
        ]

        hits = provider.search("web", specs, limit=10)

        assert len(hits) >= 1

    def test_search_limit(self):
        """搜索结果数量限制。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name=f"tool_{i}",
                category="test",
                description=f"Tool {i}",
                use_cases=["test"],
                avoid_when=[],
                keywords=["test"],
                parameters={},
            )
            for i in range(20)
        ]

        hits = provider.search("test", specs, limit=5)

        assert len(hits) == 5

    def test_search_no_match(self):
        """无匹配时返回空。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name="read_file",
                category="filesystem",
                description="读取文件",
                use_cases=["读文件"],
                avoid_when=[],
                keywords=["读"],
                parameters={},
            ),
        ]

        hits = provider.search("xyz_not_exist", specs, limit=10)

        assert len(hits) == 0

    def test_search_scores_sorted(self):
        """搜索结果按分数排序。"""
        from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider, ToolSpec

        provider = KeywordToolSearchProvider()

        specs = [
            ToolSpec(
                name="shell",
                category="system",
                description="执行命令",
                use_cases=["运行"],
                avoid_when=[],
                keywords=["shell", "cmd"],
                parameters={},
            ),
            ToolSpec(
                name="shell_tool",
                category="system",
                description="Shell 工具",
                use_cases=["命令行"],
                avoid_when=[],
                keywords=["shell"],
                parameters={},
            ),
        ]

        hits = provider.search("shell", specs, limit=10)

        # shell_tool 同时命中 name 和 keyword，应该排在前面
        # 或者至少结果应该是排序的
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)


class TestVectorToolSearchProvider:
    """测试向量工具检索器。"""

    def test_vector_provider_disabled(self):
        """禁用时返回空。"""
        from agent_py_agent.agent.tooling.models import ToolSpec, VectorToolSearchProvider

        provider = VectorToolSearchProvider(enabled=False)

        specs = [
            ToolSpec(
                name="test",
                category="test",
                description="Test",
                use_cases=[],
                avoid_when=[],
                keywords=[],
                parameters={},
            ),
        ]

        hits = provider.search("test", specs, limit=10)

        assert len(hits) == 0

    def test_vector_provider_enabled(self):
        """启用时（当前是占位实现）返回空。"""
        from agent_py_agent.agent.tooling.models import ToolSpec, VectorToolSearchProvider

        provider = VectorToolSearchProvider(enabled=True)

        specs = [
            ToolSpec(
                name="test",
                category="test",
                description="Test",
                use_cases=[],
                avoid_when=[],
                keywords=[],
                parameters={},
            ),
        ]

        # 向量检索是占位实现，应该返回空
        hits = provider.search("test", specs, limit=10)

        assert len(hits) == 0


class TestHybridToolRetriever:
    """测试混合检索器。"""

    def test_hybrid_combines_providers(self):
        """混合检索器合并多个提供者结果。"""
        from agent_py_agent.agent.tooling.models import (
            HybridToolRetriever,
            KeywordToolSearchProvider,
            ToolSpec,
            VectorToolSearchProvider,
        )

        keyword_provider = KeywordToolSearchProvider()
        vector_provider = VectorToolSearchProvider(enabled=False)

        retriever = HybridToolRetriever([keyword_provider, vector_provider])

        specs = [
            ToolSpec(
                name="read_file",
                category="filesystem",
                description="读取文件",
                use_cases=["读文件"],
                avoid_when=[],
                keywords=["read", "file"],
                parameters={},
            ),
        ]

        hits = retriever.search("read", specs, limit=10)

        assert len(hits) >= 1
        assert hits[0].name == "read_file"

    def test_hybrid_merges_same_tool(self):
        """混合检索器合并同一工具的多个命中。"""
        from agent_py_agent.agent.tooling.models import (
            HybridToolRetriever,
            KeywordToolSearchProvider,
            ToolSpec,
            VectorToolSearchProvider,
        )

        keyword_provider = KeywordToolSearchProvider()
        vector_provider = VectorToolSearchProvider(enabled=False)

        retriever = HybridToolRetriever([keyword_provider, vector_provider])

        specs = [
            ToolSpec(
                name="read_file",
                category="filesystem",
                description="读取文件内容",
                use_cases=["读文件"],
                avoid_when=[],
                keywords=["read", "file"],
                parameters={},
            ),
        ]

        hits = retriever.search("read file", specs, limit=10)

        # 同一工具应该合并分数
        assert len(hits) == 1


class TestTokenize:
    """测试分词函数。"""

    def test_tokenize_english(self):
        """英文分词。"""
        from agent_py_agent.agent.tooling.models import _tokenize

        tokens = _tokenize("read file")
        assert "read" in tokens
        assert "file" in tokens

    def test_tokenize_mixed(self):
        """中英文混合分词。"""
        from agent_py_agent.agent.tooling.models import _tokenize

        tokens = _tokenize("读取文件 read")
        assert any("读" in t for t in tokens)
        assert any("read" in t for t in tokens)

    def test_tokenize_chinese_subtokens(self):
        """中文子词分词。"""
        from agent_py_agent.agent.tooling.models import _tokenize

        tokens = _tokenize("文件")
        assert "文件" in tokens
        # 2-4字的子词
        assert any(len(t) == 2 for t in tokens)

    def test_tokenize_deduplication(self):
        """分词去重。"""
        from agent_py_agent.agent.tooling.models import _tokenize

        tokens = _tokenize("test test test")
        assert tokens.count("test") == 1
