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


def test_text_adapter_rejects_unclosed_complete_block() -> None:
    # 安全红线(2026-08-09 用户复核):缺 [/TOOL_CALL] 即未形成可执行调用——即使
    # JSON 完整,漏闭合标记的命令/写文件也不得执行(撤销 60289e44 的宽容)。
    cases = [
        '[TOOL_CALL]\n{"tool":"run_command","command":"pytest -q"}',
        "我先看一下。\n[TOOL_CALL]\n{" + '"tool":"run_command","command":"ls"}',
        '[TOOL_CALL]\n{"tool":"run_command","command":"pytest -q"}[/TOOL_CALL]\n'
        '[TOOL_CALL]\n{"tool":"run_command","command":"ls"}',
    ]
    for text in cases:
        result = TextToolProtocolAdapter().tool_calls(
            _request(SimpleNamespace(text=text), "text")
        )
        assert not result.ok, f"未闭合完整块必须拒绝: {text!r} -> {result.calls}"
        assert any("not closed" in v.detail for v in result.violations), text
    # 全坏块:整轮拒绝,一个调用都不执行
    assert TextToolProtocolAdapter().tool_calls(
        _request(SimpleNamespace(text=cases[0]), "text")
    ).calls == ()
    # 好块 + 未闭合坏块:好块执行(不连坐),坏块留痕违规(不执行)
    mixed = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    '[TOOL_CALL]\n{"tool":"run_command","command":"pytest -q"}'
                    "\n[/TOOL_CALL]\n"
                    '[TOOL_CALL]\n{"tool":"run_command","command":"ls"}'
                )
            ),
            "text",
        )
    )
    assert len(mixed.calls) == 1, mixed
    assert mixed.calls[0].arguments == {"command": "pytest -q"}, mixed
    assert any("not closed" in v.detail for v in mixed.violations), mixed


def test_text_adapter_still_rejects_truncated_unclosed_block() -> None:
    # 真截断(参数写到一半)必须维持 violation:apply_patch 的 patch 断在字符串
    # 中途、raise_event 的字段断在引号内——JSON 不完整,半参数绝不执行。
    truncated = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text='[TOOL_CALL]\n{"tool": "run_command", "command": "pytest --'
            ),
            "text",
        )
    )
    assert truncated.calls == ()
    assert truncated.violations
    assert "not closed" in truncated.violations[0].detail
    # 块后仍有 prose(非「块尾即响应尾」)同样维持 violation
    prose_after = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text='[TOOL_CALL]\n{"tool":"run_command","command":"pytest"} 接下来继续看。'
            ),
            "text",
        )
    )
    assert prose_after.calls == ()


def test_text_adapter_accepts_backticks_inside_json_values() -> None:
    # JSON 字符串值里反引号合法(写 Go 代码时 raw string/正则高频);字符串级
    # 反引号检查误杀合法调用(真机铁证 2026-08-08 celery 复刻:补 broker.go
    # 被连拦 3 轮 break)。只保留结构级检查(围栏包裹/json 解析)。
    result = TextToolProtocolAdapter().tool_calls(
        _request(
            SimpleNamespace(
                text=(
                    '[TOOL_CALL]\n{"tool": "run_command", '
                    '"command": "go build ./... `task:*`"}'
                    "\n[/TOOL_CALL]"
                )
            ),
            "text",
        )
    )

    assert len(result.calls) == 1 and result.ok, result
    assert result.calls[0].tool_name == "run_command"
    assert result.violations == ()


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
