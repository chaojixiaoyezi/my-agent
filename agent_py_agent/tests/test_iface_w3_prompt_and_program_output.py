"""IFACE-W3: optional host seams preserve frozen prompts and the canonical tool wall."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    project_tool_loop_request,
)
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    PromptContributor,
    ToolSections,
    render_prepared_prompt,
)
from agent_py_agent.agent.prompting_parts.runtime_injection import (
    RUNTIME_GOAL_SOURCE,
    RuntimeInjectionFragment,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.nested_dispatch import (
    NestedToolDispatcher,
    ProgramOutputContract,
    ProgramOutputContractError,
)
from agent_py_agent.agent.tooling.output_projection import redact_tool_output_text
from agent_py_agent.agent.tooling.program_data import DataBudget
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.agent.tooling.tool_program import ToolProgramTool
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


# LLM: Tests use an isolated owner and fixed workspace text, with no model or network I/O.
# 函数用途: 构造只读取测试临时目录的提示准备器。
def _builder(tmp_path):
    return PromptBuilder(AgentConfig(prompt_files=[], home_context_enabled=False,
        assistant_progress_updates_enabled=False), tmp_path)


# LLM: The probe goes through the real executor; the contract is a host attribute, never schema input.
# 类用途: 记录实际 handler 次数并返回可选择的原始测试正文。
class Probe(BaseTool):
    # LLM: Only synthetic output and policies are installed; no external tool is called.
    # 函数用途: 安装测试工具声明和计数器。
    def __init__(self, text='{"n": 3}', contract=None, *, expose_contract=True, **policy):
        self.model_spec = make_test_model_spec('probe', input_schema={
            'type': 'object', 'properties': {'path': {'type': 'string'}},
            'additionalProperties': False})
        self.runtime_policy = make_test_runtime_policy(**policy)
        self.text, self.calls = text, []
        if expose_contract:
            self.program_output_contract = contract

    # LLM: Invocation count proves decode failures never replay an executed handler.
    # 函数用途: 返回原始正文并留下调用事实。
    def execute(self, params):
        self.calls.append(params)
        return ToolHandlerOutcome('probe', True, self.text)


# LLM: All calls retain the canonical request, permissions, cancellation, and nested dispatch.
# 函数用途: 执行一个真实工具程序并返回结构化输出。
def _run(tmp_path, probe, *, steps=None, **options):
    result = execute_canonical_test_call(tmp_path,
        tools={'probe': probe, 'tool_program': ToolProgramTool(lambda: True)},
        tool_name='tool_program', arguments={'steps': steps or [{'id': 'read', 'tool': 'probe'}]},
        **options).result
    return result, result.metadata['handler_details']['program']


@pytest.mark.parametrize('native', [False, True])
# LLM: Explicit empty contributors must match both flat legacy calls and request-based builds byte for byte.
# 函数用途: 锁住不传新参数时的提示格式。
def test_default_contributors_preserve_legacy_prompt_bytes(tmp_path, native):
    builder = _builder(tmp_path)
    tools = ToolSections(native_tool_use=native)
    request = PromptBuildRequest('question', [], ['', ' raw\n'], [], tools,
        'system', 'isolated', 'workspace')
    expected = builder.build('question', [], inject=request.inject, prompt_files=[], tools=tools,
        system_prompt_override='system', context_scope='isolated', workspace_context_override='workspace')
    assert request.contributors == ()
    assert builder.build(request=request) == expected
    assert builder.build(request=replace(request, contributors=())) == expected


@pytest.mark.parametrize('native', [False, True])
# LLM: Each provider is called exactly once per preparation; render and projection only consume frozen values.
# 函数用途: 验证贡献顺序、快照撤回和准备后不再重读。
def test_contributors_freeze_once_before_render_and_projection(tmp_path, native):
    builder, calls, live = _builder(tmp_path), [], ['snapshot A']

    # LLM: The provider sees the original host request and returns an immutable injection tuple.
    # 函数用途: 模拟可变宿主事实的一次采集。
    def contribute(request):
        calls.append(request)
        return ('', RuntimeInjectionFragment(live[0], RUNTIME_GOAL_SOURCE))

    contributor: PromptContributor = contribute
    request = PromptBuildRequest('question', [], inject=['base\n'],
        tools=ToolSections(native_tool_use=native), context_scope='isolated',
        workspace_context_override='workspace', contributors=(contributor, lambda _: ('tail',)))
    frozen = builder.prepare_render_input(request)
    assert calls == [request]
    assert frozen.injection_fragments == ('base\n', '',
        RuntimeInjectionFragment('snapshot A', RUNTIME_GOAL_SOURCE), 'tail')
    before = render_prepared_prompt(frozen)
    live[0] = 'snapshot B'
    request.inject.append('LATE-INJECTION-SENTINEL-IFACE-W3')
    builder.config = None
    assert render_prepared_prompt(frozen) == before
    assert 'snapshot B' not in before and 'LATE-INJECTION-SENTINEL-IFACE-W3' not in before
    assert frozen.injection_fragments == ('base\n', '',
        RuntimeInjectionFragment('snapshot A', RUNTIME_GOAL_SOURCE), 'tail')
    if native:
        projected_input = ToolLoopRequestInput(prompt_input=frozen, system_instruction='system',
            tool_protocol_snapshot=make_test_protocol_snapshot(), tool_choice=ToolChoice(mode='auto'),
            native_tools=(), tool_ir_history=(), provider_history_messages=(), tool_context=(),
            forwarded_guidance=frozenset(), conversation_state='')
        first = project_tool_loop_request(projected_input)
        assert first.status == 'ready' and first.prompt == before
        assert project_tool_loop_request(projected_input) == first
        assert 'snapshot A' in str(first.messages)
    assert calls == [request]


@pytest.mark.parametrize('values', ['bare string', b'bytes', (object(),)])
# LLM: Contributor values pass through the same strict freezer as existing injection values.
# 函数用途: 拒绝错误贡献类型而不是逐字拆开或强转。
def test_contributors_reuse_injection_validation(tmp_path, values):
    request = PromptBuildRequest('question', [], contributors=(lambda _: values,))
    with pytest.raises(TypeError):
        _builder(tmp_path).prepare_render_input(request)


# LLM: Duplicate sources across old and contributed inputs remain invalid; withdrawals retain their source.
# 函数用途: 验证统一来源去重和显式撤回不被新接缝绕过。
def test_contributors_keep_duplicate_source_and_withdrawal_rules(tmp_path):
    fragment = RuntimeInjectionFragment(None, RUNTIME_GOAL_SOURCE)
    builder = _builder(tmp_path)
    request = PromptBuildRequest('question', [], contributors=(lambda _: (fragment,),))
    assert builder.prepare_render_input(request).injection_fragments == (fragment,)
    assert request.contributors == () and request.inject == [fragment]
    with pytest.raises(ValueError, match='duplicate'):
        builder.prepare_render_input(replace(request, inject=[fragment], contributors=(lambda _: (fragment,),)))


@pytest.mark.parametrize('text', ['{"n": 3}', '[1, null, true]', '17', 'null', '"string"', 'not-json'])
@pytest.mark.parametrize('expose_contract', [False, True])
# LLM: Missing and explicit None contracts preserve the original redacted json.loads fallback, including scalars.
# 函数用途: 锁住默认 JSON 与非 JSON 工具结果。
def test_default_output_contract_keeps_exact_json_fallback(tmp_path, text, expose_contract):
    import json

    probe = Probe(text, expose_contract=expose_contract)
    result, payload = _run(tmp_path, probe)
    value = payload['result']
    assert result.ok and len(probe.calls) == 1
    assert value['output'] == redact_tool_output_text(text, redaction='default')
    if text == 'not-json':
        assert 'json' not in value
    else:
        assert value['json'] == json.loads(value['output'])
    assert 'program_output_error' not in payload


# LLM: A custom non-JSON decoder and validator run once, and their value supports the existing reference path.
# 函数用途: 验证宿主自定义协议可以被后续步骤引用。
def test_custom_output_contract_decodes_validates_and_feeds_next_step(tmp_path):
    events = []

    # LLM: This pure callback handles only redacted bounded text.
    # 函数用途: 把测试键值协议转成数组。
    def decode(text):
        events.append(('decode', text))
        return {'items': list(range(int(text.split('=')[1])))}

    contract = ProgramOutputContract(decode, lambda value: events.append(('validate', value)))
    probe = Probe('items=3', contract)
    result, payload = _run(tmp_path, probe, steps=[{'id': 'read', 'tool': 'probe'},
        {'id': 'select', 'data': {'$ref': 'read#/json/items'}}])
    assert result.ok and payload['result'] == [0, 1, 2]
    assert events == [('decode', 'items=3'), ('validate', {'items': [0, 1, 2]})]
    assert len(probe.calls) == 1
    with pytest.raises(FrozenInstanceError):
        contract.decode = str


# LLM: Structured redaction must precede both callbacks; raw secrets cannot reach the decoder.
# 函数用途: 验证新协议不会看到原始敏感正文。
def test_custom_contract_receives_only_redacted_display_text(tmp_path):
    import json

    seen = []
    raw = '{"password": "SYNTHETIC-SECRET", "n": 3}'
    contract = ProgramOutputContract(lambda text: seen.append(text) or json.loads(text))
    result, payload = _run(tmp_path, Probe(raw, contract))
    assert result.ok and seen == [redact_tool_output_text(raw, redaction='default')]
    assert 'SYNTHETIC-SECRET' not in seen[0]
    assert payload['result']['json']['n'] == 3
    assert 'SYNTHETIC-SECRET' not in str(payload)


@pytest.mark.parametrize('phase', ['decode', 'validate'])
@pytest.mark.parametrize('exception', [ValueError, TypeError, RuntimeError, AssertionError])
# LLM: Callback errors fail closed with safe structured facts, preserving trace and never retrying the handler.
# 函数用途: 验证解码或校验失败不会泄漏异常正文或重执行。
def test_contract_failure_is_structured_and_never_reexecutes(tmp_path, phase, exception):
    seen = []

    # LLM: The synthetic exception body must never enter the model-visible error.
    # 函数用途: 产生可控的协议失败。
    def fail(value):
        seen.append(value)
        raise exception('SYNTHETIC-PRIVATE-EXCEPTION')

    contract = ProgramOutputContract(fail if phase == 'decode' else lambda text: {'text': text},
        fail if phase == 'validate' else lambda _: None)
    probe = Probe('payload', contract)
    result, payload = _run(tmp_path, probe)
    assert not result.ok and result.error_code == 'TOOL_PROGRAM_INCOMPLETE'
    assert payload['program_error'] == 'ProgramOutputContractError'
    assert payload['program_output_error'] == {
        'code': 'TOOL_PROGRAM_OUTPUT_INVALID', 'exception_type': exception.__name__}
    assert payload['result'] is None
    assert len(probe.calls) == len(seen) == len(payload['calls']) == 1
    assert payload['calls'][0]['status'] == 'succeeded'
    assert 'SYNTHETIC-PRIVATE-EXCEPTION' not in result.output


# LLM: This direct archiver probe uses the same runtime binding and real projection, without executing a handler.
# 函数用途: 精确测量解码前后的共享数据预算。
def _archive(probe, budget):
    snapshot = runtime_snapshot_for_tools({'probe': probe})
    child = canonical_test_call(snapshot, 'probe', {})
    request = SimpleNamespace(runtime_snapshot=snapshot, output_archiver=None)
    captured = {}
    NestedToolDispatcher(request, child)._archive(child,
        ToolHandlerOutcome('probe', True, probe.text), captured=captured, capture_budget=budget)
    return captured


@pytest.mark.parametrize('text,limit', [('abcdef', 5), ('中文', 5)])
# LLM: DataBudget rejects UTF-8 overflow before decode or validate is entered.
# 函数用途: 验证过大原文不进入自定义回调。
def test_contract_never_runs_on_over_budget_text(text, limit):
    seen = []
    contract = ProgramOutputContract(lambda value: seen.append(value), lambda value: seen.append(value))
    captured = _archive(Probe(text, contract), DataBudget(limit))
    assert captured == {'budget_exceeded': True}
    assert not seen


# LLM: A decoder cannot retain an expanded object beyond the shared intermediate-data budget.
# 函数用途: 验证自定义解码值也受预算约束。
def test_contract_expansion_is_bounded_before_validation():
    validated = []
    contract = ProgramOutputContract(lambda _: 'x' * 100, validated.append)
    captured = _archive(Probe('tiny', contract), DataBudget(20))
    assert captured['budget_exceeded'] and 'json' not in captured
    assert not validated


# LLM: A shared budget must reject the second capture before invoking its decoder, not restart a fresh budget.
# 函数用途: 验证多个输出共用累计预算。
def test_contract_captures_share_cumulative_budget():
    seen, budget = [], DataBudget(10)
    probe = Probe('123456', ProgramOutputContract(lambda text: seen.append(text) or 1))
    assert _archive(probe, budget)['json'] == 1
    assert _archive(probe, budget)['budget_exceeded']
    assert seen == ['123456']


# LLM: _value raises a typed error carrying safe structured data, never rerunning dispatch.
# 函数用途: 验证协议失败在取值入口明确拒绝。
def test_value_raises_typed_structured_contract_error():
    error = {'code': 'TOOL_PROGRAM_OUTPUT_INVALID', 'exception_type': 'RuntimeError'}
    result = SimpleNamespace(metadata={'program_data': {'contract_error': error}})
    with pytest.raises(ProgramOutputContractError) as raised:
        ToolProgramTool._value(result)
    assert raised.value.args == (error,)


@pytest.mark.parametrize('wall', ['unloaded', 'mutating', 'dangerous', 'approval', 'internal', 'path', 'schema'])
# LLM: Custom protocol hooks cannot authorize a tool or bypass scope, approval, or input checks.
# 函数用途: 验证宿主权限拒绝发生在 handler 与解码前。
def test_custom_contract_cannot_bypass_host_walls(tmp_path, wall):
    seen, policy, options, arguments = [], {}, {}, {}
    workspace = tmp_path / 'owner'
    workspace.mkdir()
    if wall == 'unloaded':
        options['allowed_tools'] = ['tool_program']
    elif wall in {'mutating', 'dangerous'}:
        policy['effect'] = wall
    elif wall == 'approval':
        policy['approval_mode'] = 'always'
    elif wall == 'internal':
        policy['internal_parameters'] = ('secret_scope',)
        arguments = {'secret_scope': 'forged'}
    elif wall == 'path':
        policy['resource_parameters'] = ('path',)
        arguments = {'path': str(tmp_path / 'foreign.txt')}
        options['owner_scope_root'] = str(workspace)
    elif wall == 'schema':
        arguments = {'program_output_contract': 'forged-host-provider'}
    probe = Probe('payload', ProgramOutputContract(lambda text: seen.append(text) or {}), **policy)
    result, payload = _run(workspace, probe,
        steps=[{'id': 'denied', 'tool': 'probe', 'arguments': arguments}], **options)
    assert not result.ok and not probe.calls and not seen
    assert payload['result'] is None or payload['calls'][0]['status'] != 'succeeded'


# LLM: Pre-cancelled requests never enter the outer program or custom decoder.
# 函数用途: 锁住共享取消入口。
def test_custom_contract_keeps_cancellation(tmp_path):
    token, seen = CancellationToken(), []
    token.cancel('test')
    probe = Probe('payload', ProgramOutputContract(lambda text: seen.append(text)))
    execution = execute_canonical_test_call(tmp_path,
        tools={'probe': probe, 'tool_program': ToolProgramTool(lambda: True)},
        tool_name='tool_program', arguments={'steps': [{'id': 'read', 'tool': 'probe'}]},
        cancellation_token=token)
    assert execution.result.status == 'cancelled' and not probe.calls and not seen


# LLM: Batch failure retains every attempted child fact and no callback or handler is retried.
# 函数用途: 验证批次协议失败仍保留完整调用轨迹。
def test_contract_failure_retains_all_batch_call_facts(tmp_path):
    seen = []

    # LLM: Fixed failure is independent of thread order.
    # 函数用途: 模拟每项都会失败的批次解码。
    def fail(text):
        seen.append(text)
        raise RuntimeError('private')

    probe = Probe('payload', ProgramOutputContract(fail))
    result, payload = _run(tmp_path, probe, steps=[{'id': 'reads',
        'calls': [{'tool': 'probe'} for _ in range(3)]}])
    assert not result.ok and len(probe.calls) == len(seen) == 3
    assert len(payload['calls']) == payload['stats']['tool_calls'] == 3
    assert all(row['status'] == 'succeeded' for row in payload['calls'])


# LLM: Host-only seams do not add provider/factory fields to any model-visible schema.
# 函数用途: 检查新协议只存在于宿主对象上。
def test_host_contract_does_not_expand_tool_json_schemas():
    probe = Probe(contract=ProgramOutputContract(str))
    assert set(probe.model_spec.input_schema['properties']) == {'path'}
    assert set(ToolProgramTool.model_spec.input_schema['properties']) == {'steps', 'return_step'}
    assert ToolProgramTool.model_spec.input_schema['additionalProperties'] is False


@pytest.mark.parametrize('refresh', [False, True])
# LLM: The real first-request record/refresh/candidate flow must not replay a host contributor.
# 函数用途: 覆盖候选重准备链路，固定贡献快照与来源集合。
def test_first_request_candidate_reuses_materialized_contributions(tmp_path, monkeypatch, refresh):
    from agent_py_agent.agent.agent_core import native_tool_protocol
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.subagent import model_selection

    seen, live = [], ['snapshot A']
    builder = _builder(tmp_path)
    protocol = make_test_protocol_snapshot()
    agent = SimpleNamespace(prompts=builder, backend=SimpleNamespace(project_generate_payload=lambda: None))
    params = ToolLoopExecuteParams(user_prompt='question', memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section='', tool_recommendations_section='', tool_context=[], effective_on_chunk=None,
        allowed_tools=None, write_boundary=None, task_attributes=None, request_id='request', run_id='run',
        task_id='task', one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[], attempt_id='attempt')
    request = PromptBuildRequest('question', [], tools=ToolSections(native_tool_use=True),
        context_scope='isolated', workspace_context_override='workspace',
        contributors=(lambda _: seen.append(live[0]) or (RuntimeInjectionFragment(live[0], RUNTIME_GOAL_SOURCE),),))
    preparation = model_selection.SubagentFirstRequestPreparation(agent, 'run', 'attempt')
    token = model_selection._PREPARATION.set(preparation)
    try:
        frozen = builder.prepare_render_input(request)
        model_selection.record_first_request_prompt(agent, params, request, frozen)
        request_input = ToolLoopRequestInput(prompt_input=frozen, system_instruction='system',
            tool_protocol_snapshot=protocol, tool_choice=ToolChoice(mode='auto'), native_tools=(),
            tool_ir_history=(), provider_history_messages=(), tool_context=(),
            forwarded_guidance=frozenset(), conversation_state='')
        if refresh:
            model_selection.refresh_first_request_prompt(agent, params, request_input)
        preparation.request_input = request_input
        live[0] = 'snapshot B'
        monkeypatch.setattr(native_tool_protocol, 'select_tool_protocol', lambda *args, **kwargs: protocol)
        monkeypatch.setattr(native_tool_protocol, 'resolve_native_tools', lambda *args: [])
        monkeypatch.setattr(native_tool_protocol, 'model_turn_tool_choice', lambda *args: ToolChoice(mode='auto'))
        monkeypatch.setattr('agent_py_agent.agent.model_guidance.provider_system_instruction', lambda _: 'system')
        probe = SimpleNamespace(dependencies=SimpleNamespace(config=SimpleNamespace(model_input_modalities=['text'])),
            params=params)
        candidate = model_selection._candidate_projection(agent, preparation, probe)
        assert candidate.request_input.prompt_input.injection_fragments == frozen.injection_fragments
        assert 'snapshot A' in str(candidate.projected.messages)
        assert 'snapshot B' not in str(candidate.projected.messages)
        assert seen == ['snapshot A'] and preparation.prompt_request.contributors == ()
    finally:
        model_selection._PREPARATION.reset(token)


# LLM: An already-recorded validation error must survive later result-metadata budget exhaustion.
# 函数用途: 覆盖合法大解码值校验失败后的错误优先级与不重执行。
def test_contract_error_survives_later_result_budget_cleanup(tmp_path):
    # LLM: Only a synthetic exception type may enter the visible error.
    # 函数用途: 在解码预算即将耗尽时制造校验失败。
    def fail(_):
        raise RuntimeError('PRIVATE-VALIDATOR-MESSAGE')

    probe = Probe('tiny', ProgramOutputContract(lambda _: 'x' * 1_999_990, fail))
    result, payload = _run(tmp_path, probe)
    assert not result.ok and payload['program_error'] == 'ProgramOutputContractError'
    assert payload['program_output_error'] == {'code': 'TOOL_PROGRAM_OUTPUT_INVALID', 'exception_type': 'RuntimeError'}
    assert payload['result'] is None and len(probe.calls) == len(payload['calls']) == 1
    assert payload['calls'][0]['status'] == 'succeeded'
    assert 'PRIVATE-VALIDATOR-MESSAGE' not in result.output


# LLM: A validator may mutate its argument; the retained value still must fit the shared budget.
# 函数用途: 防止校验器原地放大对象后绕过留存预算。
def test_validator_expansion_is_bounded_before_capture():
    contract = ProgramOutputContract(lambda _: {}, lambda value: value.update(payload='x' * 100))
    captured = _archive(Probe('tiny', contract), DataBudget(40))
    assert captured == {'budget_exceeded': True}


# LLM: Non-JSON custom objects cannot leak through a successful protocol projection.
# 函数用途: 对不可序列化的解码值明确报告结构化错误。
def test_non_json_decoded_value_is_structured_failure(tmp_path):
    validated = []
    probe = Probe('tiny', ProgramOutputContract(lambda _: object(), validated.append))
    result, payload = _run(tmp_path, probe)
    assert not result.ok and not validated and len(probe.calls) == 1
    assert payload['program_output_error'] == {'code': 'TOOL_PROGRAM_OUTPUT_INVALID', 'exception_type': 'TypeError'}


# LLM: Real concurrent dispatch uses one cumulative capture budget and retains every attempted call.
# 函数用途: 在主派发链验证自定义协议批次累计预算与失败轨迹。
def test_custom_contract_batch_uses_shared_budget(tmp_path):
    probe = Probe('tiny', ProgramOutputContract(lambda _: 'x' * 600_000))
    result, payload = _run(tmp_path, probe, steps=[{'id': 'reads',
        'calls': [{'tool': 'probe'} for _ in range(3)]}])
    assert not result.ok and payload['program_error'] == 'ValueError'
    assert payload['result'] is None
    assert len(probe.calls) == len(payload['calls']) == payload['stats']['tool_calls'] == 3
    assert all(row['status'] == 'succeeded' for row in payload['calls'])


# LLM: The original executor gate remains authoritative for each nested child, before decode.
# 函数用途: 验证宿主前置门没有被新协议替代。
def test_custom_contract_keeps_per_child_host_gate(tmp_path):
    seen = []
    probe = Probe('tiny', ProgramOutputContract(lambda text: seen.append(text)))

    # LLM: The outer program is allowed, but its child remains denied by the original executor.
    # 函数用途: 模拟宿主逐子调用拒绝而不触碰业务handler。
    def gate(call):
        return (ToolHandlerOutcome('probe', False, 'denied', error_code='PATH_ACCESS_DENIED')
            if call.tool_name == 'probe' else None)

    result, payload = _run(tmp_path, probe, pre_handler_gate=gate)
    assert not result.ok and not probe.calls and not seen
    assert payload['calls'][0]['error_code'] == 'PATH_ACCESS_DENIED'


@pytest.mark.parametrize('protocol_first', [False, True])
@pytest.mark.parametrize('failure_phase', ['validate', 'decoded_value'])
# LLM: Events deterministically interleave a validator exception with a sibling exhausting the same budget.
# 函数用途: 验证并发预算溢出不能覆盖另一调用的真实协议错误，结果顺序不影响优先级。
def test_callback_failure_survives_concurrent_budget_exhaustion(tmp_path, monkeypatch, protocol_first, failure_phase):
    from threading import Event

    entered, exhausted = Event(), Event()
    large = 'x' * 2_000_000
    original_reserve = DataBudget.reserve

    # LLM: Signal only after the real shared reserve has rejected the large sibling value.
    # 函数用途: 为确定性交错记录真实预算耗尽时点。
    def reserve(self, value, *, text=False):
        try:
            return original_reserve(self, value, text=text)
        except ValueError:
            if self.remaining < 0:
                exhausted.set()
            raise

    monkeypatch.setattr(DataBudget, 'reserve', reserve)

    # LLM: Both decoders are invoked after the real executor has returned their distinct synthetic output.
    # 函数用途: 安排小值先进入校验，再让另一个值实际撑爆预算。
    def decode(text):
        if text == 'protocol':
            if failure_phase == 'decoded_value':
                entered.set()
                assert exhausted.wait(1), 'sibling did not exhaust budget'
                return object()
            return 1
        assert entered.wait(1), 'validator did not start'
        return large

    # LLM: The RuntimeError occurs strictly after a different child has exhausted the budget.
    # 函数用途: 固定协议失败发生在并发预算失败之后。
    def validate(value):
        assert value == 1
        entered.set()
        assert exhausted.wait(1), 'sibling did not exhaust budget'
        raise RuntimeError('PRIVATE-CONCURRENT-VALIDATOR')

    # LLM: This tool only echoes the selected test label; no file access or external call occurs.
    # 类用途: 为同一真实并发批次返回两种确定输出。
    class ConcurrentProbe(Probe):
        # LLM: Count each actual handler invocation exactly once.
        # 函数用途: 返回当前调用自己的标签，避免共享可变正文。
        def execute(self, params):
            self.calls.append(params)
            return ToolHandlerOutcome('probe', True, params['path'])

    names = ['protocol', 'overflow'] if protocol_first else ['overflow', 'protocol']
    probe = ConcurrentProbe(contract=ProgramOutputContract(decode, validate))
    result, payload = _run(tmp_path, probe, steps=[{'id': 'reads', 'calls': [
        {'tool': 'probe', 'arguments': {'path': name}} for name in names]}])
    assert entered.is_set() and exhausted.is_set()
    assert not result.ok and payload['program_error'] == 'ProgramOutputContractError'
    assert payload['program_output_error'] == {'code': 'TOOL_PROGRAM_OUTPUT_INVALID',
        'exception_type': 'RuntimeError' if failure_phase == 'validate' else 'TypeError'}
    assert len(probe.calls) == len(payload['calls']) == 2
    assert all(row['status'] == 'succeeded' for row in payload['calls'])
    assert 'PRIVATE-CONCURRENT-VALIDATOR' not in result.output
