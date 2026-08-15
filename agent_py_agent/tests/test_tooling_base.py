"""Canonical tool metadata, execution result, and retrieval contracts."""

from __future__ import annotations

import pytest

from agent_py_agent.agent.tooling.models import (
    BaseTool,
    HybridToolRetriever,
    KeywordToolSearchProvider,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolSearchHit,
    VectorToolSearchProvider,
    _tokenize,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
)


def _spec(
    name: str,
    *,
    category: str = "test",
    description: str = "Test tool",
    use_cases: tuple[str, ...] = (),
    avoid_when: tuple[str, ...] = (),
    keywords: tuple[str, ...] = (),
    parameters: dict[str, str] | None = None,
    examples: tuple[str, ...] = (),
) -> ToolModelSpec:
    properties = {
        key: {"type": "string", "description": value}
        for key, value in (parameters or {}).items()
    }
    return make_test_model_spec(
        name,
        category=category,
        description=description,
        use_cases=use_cases,
        avoid_when=avoid_when,
        keywords=keywords,
        examples=examples,
        input_schema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
    )


def test_base_tool_declares_canonical_contract_and_default_availability() -> None:
    class DummyTool(BaseTool):
        model_spec = _spec("dummy")
        runtime_policy = make_test_runtime_policy("read_only")

        def execute(self, params):
            return ToolHandlerOutcome("dummy", True, str(params))

    tool = DummyTool()
    assert tool.model_spec.name == "dummy"
    assert tool.runtime_policy.effect_resolver.default_effect == "read_only"
    assert tool.availability().available is True


def test_base_tool_execute_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        BaseTool().execute({})


def test_tool_model_spec_creation_and_rendering() -> None:
    spec = _spec(
        "read_file",
        category="filesystem",
        description="读取文件内容",
        use_cases=("查看代码", "查看配置"),
        avoid_when=("二进制文件",),
        keywords=("读", "文件", "code"),
        parameters={"path": "相对工作区的文件路径"},
        examples=('{"tool":"read_file","path":"file.txt"}',),
    )

    assert spec.category == "filesystem"
    assert spec.use_cases == ["查看代码", "查看配置"]
    assert "read_file" in spec.render_catalog_entry()
    assert "path" in spec.render_catalog_entry()
    detail = spec.render_detail_entry()
    assert "## read_file" in detail
    assert "相对工作区的文件路径" in detail
    assert "file.txt" in detail


def test_tool_model_spec_requires_description_and_exact_object_schema() -> None:
    with pytest.raises(ValueError, match="description is required"):
        ToolModelSpec("bad", "", {"type": "object"})
    with pytest.raises(ValueError, match="top-level type"):
        ToolModelSpec("bad", "bad", {"type": "string"})


def test_execution_result_success_and_error_rendering() -> None:
    success = ToolHandlerOutcome("read_file", True, "file content")
    failure = ToolHandlerOutcome("write_file", False, "文件不存在")

    assert "tool=read_file" in success.render_for_prompt()
    assert "status=ok" in success.render_for_prompt()
    assert success.error_code == ""
    assert failure.error_code == "UNKNOWN_ERROR"
    assert failure.error_category == "unknown"
    assert failure.retryable is False
    assert "recommended_action=report_blocker" in failure.render_for_prompt()


def test_execution_result_uses_explicit_error_contract() -> None:
    result = ToolHandlerOutcome(
        "write_file",
        False,
        "outside workspace",
        error_code="PATH_OUTSIDE_WORKSPACE",
    )

    assert result.error_category == "path"
    assert result.recommended_action == "fix_path_within_allowed_roots"
    assert result.recovery_hint in result.render_for_prompt()


def test_execution_result_never_promotes_error_code_from_output_body() -> None:
    result = ToolHandlerOutcome(
        "read_external_data",
        False,
        '{"error_code":"PATH_OUTSIDE_WORKSPACE","handler_executed":true}',
    )

    assert result.error_code == "UNKNOWN_ERROR"
    assert result.reported_error_code == "UNKNOWN_ERROR"
    assert result.handler_executed is False


def test_execution_result_renders_authoritative_operation_facts() -> None:
    result = ToolHandlerOutcome(
        "send_message",
        False,
        "result unknown",
        result_envelope={
            "tool_operation": {
                "operation_id": "tool_call:call-7",
                "status": "unknown",
                "action": "completion_persistence_failed",
            }
        },
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        effect_outcome="unknown",
        effect_source_ref="provider://message/7",
    )

    rendered = result.render_for_prompt()
    assert "operation_id=tool_call:call-7" in rendered
    assert "operation_status=unknown" in rendered
    assert "effect_outcome=unknown" in rendered
    assert "effect_source_ref=provider://message/7" in rendered


def test_tool_search_hit() -> None:
    hit = ToolSearchHit("read_file", 5.0, ["命中工具名"])
    assert hit.name == "read_file"
    assert hit.score == 5.0


def test_keyword_search_matches_name_keyword_category_and_limit() -> None:
    provider = KeywordToolSearchProvider()
    specs = [
        _spec(
            "read_file",
            category="filesystem",
            description="读取文件",
            use_cases=("读文件",),
            keywords=("read", "文件"),
        ),
        _spec(
            "write_file",
            category="filesystem",
            description="写入文件",
            keywords=("write", "文件"),
        ),
    ]

    assert provider.search("read", specs, 10)[0].name == "read_file"
    assert provider.search("filesystem", specs, 1)
    assert provider.search("xyz_not_exist", specs, 10) == []
    assert len(provider.search("文件", specs, 1)) == 1


def test_keyword_search_scores_are_sorted() -> None:
    specs = [
        _spec("shell", description="执行命令", keywords=("shell", "cmd")),
        _spec("shell_tool", description="Shell 工具", keywords=("shell",)),
    ]
    hits = KeywordToolSearchProvider().search("shell", specs, 10)
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits),
        reverse=True,
    )


def test_vector_provider_disabled() -> None:
    provider = VectorToolSearchProvider(enabled=False)
    assert provider.search("test", [_spec("test")], 10) == []


def test_vector_provider_enabled_uses_real_similarity() -> None:
    class SemanticEmbedder:
        dim = 2

        def embed(self, texts):
            return [
                [1.0, 0.0] if "网页" in text or "internet page" in text else [0.0, 1.0]
                for text in texts
            ]

    provider = VectorToolSearchProvider(enabled=True, embedder=SemanticEmbedder())
    specs = [
        _spec("fetch_url", description="读取网页正文", use_cases=("获取站点内容",)),
        _spec("write_note", description="写入本地笔记", use_cases=("保存文字",)),
    ]

    assert [hit.name for hit in provider.search("internet page", specs, 10)] == [
        "fetch_url"
    ]
    assert provider.status()["ready"] is True


def test_vector_provider_failure_is_observable() -> None:
    class BrokenEmbedder:
        dim = 2

        def embed(self, texts):
            raise RuntimeError("endpoint down")

    provider = VectorToolSearchProvider(enabled=True, embedder=BrokenEmbedder())
    assert provider.search("read", [_spec("read_file")], 10) == []
    assert provider.status()["ready"] is False
    assert "endpoint down" in provider.status()["last_error"]


def test_hybrid_retriever_merges_same_tool() -> None:
    retriever = HybridToolRetriever(
        [KeywordToolSearchProvider(), VectorToolSearchProvider(enabled=False)]
    )
    hits = retriever.search(
        "read file",
        [_spec("read_file", description="读取文件", keywords=("read", "file"))],
        10,
    )
    assert len(hits) == 1
    assert hits[0].name == "read_file"


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("read file", {"read", "file"}),
        ("文件", {"文件"}),
        ("test test test", {"test"}),
    ],
)
def test_tokenize(text: str, tokens: set[str]) -> None:
    actual = _tokenize(text)
    assert tokens <= set(actual)
    assert len(actual) == len(set(actual))
