from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    TextToolProtocolAdapter,
    anthropic_tool_choice,
    canonical_tool_calls_from_response,
    openai_tool_choice,
)
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    ToolAvailability,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolChoice,
    ToolProtocolSnapshot,
)


def _runtime_snapshot() -> ToolRuntimeSnapshot:
    model_spec = ToolModelSpec(
        name="run_command",
        description="运行一个命令",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    return ToolRuntimeSnapshot(
        run_id="run-1",
        runtimes=(
            ToolRuntime(
                model_spec=model_spec,
                runtime_policy=ToolRuntimePolicy(EffectResolverPolicy("mutating")),
                handler=object(),
                availability=ToolAvailability.ready(),
            ),
        ),
        available_tool_names=frozenset({"run_command"}),
        unavailable_tools=(),
        allowed_tools=None,
    )


def _protocol(source: str) -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        run_id="run-1",
        source_protocol=source,
        capability=ProviderToolCapability(
            provider="test",
            endpoint="local://test",
            model="fake",
            stream=False,
            native_supported=source == "native",
            evidence="test_contract",
        ),
    )


def _request(response: object, source: str) -> ProviderToolCallRequest:
    return ProviderToolCallRequest(
        response=response,
        protocol=_protocol(source),
        runtime_snapshot=_runtime_snapshot(),
        turn_id="turn-1",
        attempt_id="attempt-1",
    )


def test_native_structured_block_becomes_canonical_call() -> None:
    result = canonical_tool_calls_from_response(
        _request(
            SimpleNamespace(
                text="",
                tool_use_blocks=[
                    {
                        "id": "call-1",
                        "name": "run_command",
                        "input": {"command": "pytest -q"},
                    }
                ],
            ),
            "native",
        )
    )

    assert result.ok
    assert result.calls[0].tool_name == "run_command"
    assert result.calls[0].source_protocol == "native"
    assert result.calls[0].schema_hash.startswith("sha256:")
    assert result.calls[0].operation_id.startswith("tool_operation:")
    assert result.calls[0].idempotency_key
    assert result.calls[0].arguments == {"command": "pytest -q"}


def test_native_textual_tool_block_is_violation_and_never_promoted() -> None:
    result = canonical_tool_calls_from_response(
        _request(
            SimpleNamespace(
                text='[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]',
                tool_use_blocks=[],
            ),
            "native",
        )
    )

    assert result.calls == ()
    assert [item.code for item in result.violations] == ["PROTOCOL_VIOLATION"]


def test_native_xml_pseudo_tool_block_is_violation_and_never_promoted() -> None:
    result = canonical_tool_calls_from_response(
        _request(
            SimpleNamespace(
                text=(
                    '<tool_call><function name="run_command">'
                    '{"command":"pytest -q"}</function></tool_call>'
                ),
                tool_use_blocks=[],
            ),
            "native",
        )
    )

    assert result.calls == ()
    assert [item.code for item in result.violations] == ["PROTOCOL_VIOLATION"]


def test_native_structured_call_is_not_executed_when_same_response_has_pseudo_call() -> None:
    result = canonical_tool_calls_from_response(
        _request(
            SimpleNamespace(
                text='[TOOL_CALL]{"tool":"run_command","command":"other"}[/TOOL_CALL]',
                tool_use_blocks=[
                    {
                        "id": "call-1",
                        "name": "run_command",
                        "input": {"command": "pytest -q"},
                    }
                ],
            ),
            "native",
        )
    )

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"


def test_host_boundary_violation_prevents_any_text_call() -> None:
    result = canonical_tool_calls_from_response(
        _request(
            SimpleNamespace(
                text='[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]',
                tool_protocol_violations=[
                    {
                        "code": "TOOL_CALL_UNCLOSED",
                        "detail": "stream ended before the complete response boundary",
                        "evidence_preview": '{"sha256":"abc"}',
                    }
                ],
            ),
            "text",
        )
    )

    assert result.calls == ()
    assert [item.code for item in result.violations] == ["TOOL_CALL_UNCLOSED"]


def test_text_adapter_accepts_blocks_with_prose_prefix() -> None:
    # 长期助手 式宽容解析(真机 2026-08-08 scrapy 复刻):弱模型工具轮必然先输出
    # prose("我需要先看一下…")再写块,严格纯块会把已成功执行的工具轮整轮判死。
    valid = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    '[TOOL_CALL]\n{"tool":"run_command","command":"pytest -q"}'
                    "\n[/TOOL_CALL]"
                )
            ),
            "text",
        )
    )
    prose_prefix = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    "我先看一下项目结构。\n"
                    '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]\n'
                    "执行完继续。"
                )
            ),
            "text",
        )
    )
    prose_between = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    "第一步：\n"
                    '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]\n'
                    "第二步：\n"
                    '[TOOL_CALL]{"tool":"run_command","command":"ls"}[/TOOL_CALL]\n'
                    "以上完成。"
                )
            ),
            "text",
        )
    )
    fenced = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    "```\n"
                    '[TOOL_CALL]{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]'
                    "\n```"
                )
            ),
            "text",
        )
    )
    plain_prose = TextToolProtocolAdapter().tool_calls(
        _request(SimpleNamespace(text="目录里暂时没有文件。"), "text")
    )

    assert len(valid.calls) == 1 and valid.ok
    assert len(prose_prefix.calls) == 1 and prose_prefix.ok
    assert len(prose_between.calls) == 2 and prose_between.ok
    assert fenced.calls == () and fenced.violations
    assert plain_prose.calls == () and plain_prose.violations == ()


def test_text_adapter_has_one_tool_name_field() -> None:
    result = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    '[TOOL_CALL]\n{"tool_name":"run_command",'
                    '"command":"pytest -q"}\n[/TOOL_CALL]'
                )
            ),
            "text",
        )
    )

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"


def test_tool_choice_maps_without_prompt_strings() -> None:
    assert anthropic_tool_choice(ToolChoice.auto()) == {"type": "auto"}
    assert anthropic_tool_choice(ToolChoice.required()) == {"type": "any"}
    assert anthropic_tool_choice(ToolChoice.specific("run_command")) == {
        "type": "tool",
        "name": "run_command",
    }
    assert openai_tool_choice(ToolChoice.none()) == "none"
    assert openai_tool_choice(ToolChoice.specific("run_command")) == {
        "type": "function",
        "function": {"name": "run_command"},
    }
