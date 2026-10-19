"""内部最终段与真实公开出口分责；只用fake后端、内存TUI和合成标签。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.assistant_messages import final_response_text
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.run_output import _unstreamed_final_response
from agent_py_agent.tests.test_assistant_message_phases import _message, _response

_SECRET = 'synthetic-only-secret-554'
_LABELLED = 'PASSWORD=' + _SECRET


@pytest.mark.parametrize('text', ['[SUBAGENT_RESULT]\n{"status":"DONE"}\n[/SUBAGENT_RESULT]', '自然句\n[SUBAGENT_RESULT]\n{"status":"BLOCKED"}\n[/SUBAGENT_RESULT]'])
def test_no_phase_internal_selection_retains_original_model_body(text):
    response = ModelResponse(text=text, backend='fake')
    assert final_response_text(response, project=lambda value: value) == text
    assert final_response_text(response) == project_user_reply(text).content
    assert response.text == text and response.assistant_content_blocks == []


@pytest.mark.parametrize('phase', ['final_answer', None, 'future_phase'])
def test_internal_phase_selection_preserves_cross_message_security_context(phase):
    response = _response([_message('PASSWORD=', 'commentary', message_id='m-context'),
                          _message(_SECRET, phase, message_id='m-answer')])
    internal = final_response_text(response, project=lambda value: value)
    assert internal == response.text
    assert _SECRET not in project_user_reply(internal).content
    assert final_response_text(response) == project_user_reply(response.text).content
    assert response.assistant_content_blocks[1]['assistant_message']['phase'] == phase


def test_real_phase_final_selects_only_final_and_commentary_only_has_no_candidate():
    response = _response([_message('过程。', 'commentary'), _message('终答。', 'final_answer', message_id='m-final')])
    assert final_response_text(response, project=lambda value: value) == '终答。'
    assert final_response_text(_response([_message('仅过程。', 'commentary')]), project=lambda value: value) == ''


def test_direct_cli_final_projects_before_streamed_comparison():
    result = SimpleNamespace(response=_LABELLED, model_response=_LABELLED)
    assert _unstreamed_final_response(result, '') == project_user_reply(_LABELLED).content
    assert _unstreamed_final_response(result, project_user_reply(_LABELLED).content) == ''
    assert _SECRET not in _unstreamed_final_response(result, '')


def test_tui_summary_projects_whole_final_and_preserves_same_active_identity():
    runtime = TuiRuntime('public-final-554')
    turn = runtime.begin_turn('request-554')
    turn.commit_model_response({'schema':'assistant_messages.v1', 'source':{'call_id':'call-554'},
        'boundary':'terminal', 'messages':[{'text':_LABELLED, 'phase':'final_answer'}], 'final_text':_LABELLED})
    candidate = next(block for block in runtime.store.snapshot().active_blocks if block.role == 'assistant')
    runtime.complete_turn('request-554', TuiTurnSummary(response_text=_LABELLED, ok=True))
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.role == 'assistant']
    assert len(blocks) == 1 and blocks[0].block_id == candidate.block_id
    assert blocks[0].text == project_user_reply(_LABELLED).content
    assert _SECRET not in blocks[0].text and not runtime.store.snapshot().active_blocks


def test_plain_final_and_expand_never_reveal_raw_body(capsys):
    from agent_py_agent.cli.chat_parts.plain_ui import (
        AssistantResponseRenderRequest,
        _handle_expand_command,
        _render_assistant_response,
    )
    history = []
    _render_assistant_response(AssistantResponseRenderRequest(_LABELLED, history, 'agent'))
    _handle_expand_command('/expand last', history)
    assert _SECRET not in capsys.readouterr().out
    assert history == [project_user_reply(_LABELLED).content]


def test_plain_local_candidate_waits_commit_and_keeps_full_security_context(capsys):
    from agent_py_agent.cli.chat_parts.plain_ui import (
        AssistantResponseRenderRequest,
        _make_chunk_handler,
        _render_assistant_response,
    )
    callback, streamed = _make_chunk_handler('agent', 1, committed_model_only=True)
    callback('PASS')
    callback('WORD=' + _SECRET)
    assert not capsys.readouterr().out
    payload = {'schema':'assistant_messages.v1', 'source':{'call_id':'call-plain-554'}, 'boundary':'tools',
               'messages':[{'text':'PASS'}, {'text':'WORD=' + _SECRET}], 'final_text':''}
    callback.commit_model_response(payload)
    callback.commit_model_response(payload)
    callback.write_progress({'phase':'started'}, '[工具] read_file\n')
    output = capsys.readouterr().out
    assert _SECRET not in output and output.count('[REDACTED]') == 1 and '[工具]' in output
    assert streamed == [False]
    _render_assistant_response(AssistantResponseRenderRequest('最终正文。', [], 'agent'))
    assert capsys.readouterr().out.count('最终正文。') == 1


def test_scenario_direct_command_uses_public_projection(tmp_path, monkeypatch, capsys):
    from agent_py_agent.cli import scenario
    result = SimpleNamespace(response=_LABELLED, backend='fake', tool_rounds=0)
    def stop():
        raise RuntimeError('stop-after-public-output')
    agent = SimpleNamespace(run=lambda *_args, **_kwargs: result, subagents=SimpleNamespace(list_runs=stop))
    monkeypatch.setattr(scenario, 'load_scenario_agent', lambda *_args: agent)
    paths = SimpleNamespace(run_root=tmp_path, fixture_root=tmp_path, config=tmp_path/'fake.yaml')
    with pytest.raises(RuntimeError, match='stop-after-public-output'):
        scenario._cmd_scenario_happy_path(SimpleNamespace(count=1, direct=True), paths)
    assert _SECRET not in capsys.readouterr().out


def test_tui_live_chunks_do_not_reveal_split_assignment_label():
    runtime = TuiRuntime('public-live-554')
    turn = runtime.begin_turn('request-live-554')
    turn.write_model('PASS')
    turn.write_model('WORD=' + _SECRET)
    snapshot = runtime.store.snapshot()
    blocks = [row for row in (*snapshot.stable_blocks, *snapshot.active_blocks) if row.role == 'assistant']
    assert _SECRET not in ''.join(row.text for row in blocks)
    # 本地raw只计量，原最终公开门仍须输出完整安全终答。
    assert blocks == []
    runtime.complete_turn('request-live-554', TuiTurnSummary(response_text=_LABELLED))
    blocks = [row for row in runtime.store.snapshot().stable_blocks if row.role == 'assistant']
    assert ''.join(row.text for row in blocks) == project_user_reply(_LABELLED).content


@pytest.mark.parametrize('boundary', ['tools', 'input', 'resume'])
def test_tui_candidate_context_is_cleared_at_original_segment_boundaries(boundary):
    runtime = TuiRuntime('public-boundary-554')
    turn = runtime.begin_turn('request-boundary-554')
    # 本用例核公开候选生命周期，来源显式使用原Gateway typed入口。
    turn.on_gateway_event({'kind':'assistant_final', 'text':'PASSWORD=', 'model_source':{'call_id':'accepted-before-boundary'}})
    if boundary == 'tools':
        turn.write_progress({'phase':'started', 'tool':'read_file', 'round':1, 'call_index':0})
    elif boundary == 'input':
        turn.begin_active_turn_input(())
    else:
        turn.on_gateway_event({'kind':'turn_resumed', 'cause':'gateway_restart'})
    turn.on_gateway_event({'kind':'assistant_final', 'text':'下一段普通正文。', 'model_source':{'call_id':'accepted-after-boundary'}})
    candidate = next(row for row in runtime.store.snapshot().active_blocks if row.role == 'assistant')
    assert candidate.text == '下一段普通正文。'
    if boundary == 'tools':
        turn.write_progress({'phase':'completed', 'tool':'read_file', 'round':1, 'call_index':0})
    runtime.complete_turn('request-boundary-554', TuiTurnSummary(response_text='最终正文。'))
    assert not runtime.store.snapshot().active_blocks


@pytest.mark.parametrize('interrupted', [False, True])
def test_tui_current_terminal_text_never_restores_private_candidate(interrupted):
    runtime = TuiRuntime('public-current-554')
    turn = runtime.begin_turn('request-current-554')
    turn.write_model('PASS')
    turn.write_model('WORD=' + _SECRET)
    current = '超时前确认正文。\n后续终态正文。'
    runtime.complete_turn('request-current-554', TuiTurnSummary(response_text=current, ok=False, interrupted=interrupted))
    rows = [row for row in runtime.store.snapshot().stable_blocks if row.role == 'assistant']
    assert _SECRET not in ''.join(row.text for row in rows)
    if not interrupted:
        assert [row.text for row in rows] == [current]
    assert not runtime.store.snapshot().active_blocks


def test_tui_empty_public_terminal_cannot_retain_an_old_candidate():
    runtime = TuiRuntime('public-empty-554')
    turn = runtime.begin_turn('request-empty-554')
    turn.on_gateway_event({'kind':'assistant_final', 'text':'旧候选不能当终答。', 'model_source':{'call_id':'accepted-old-final'}})
    runtime.complete_turn('request-empty-554', TuiTurnSummary(response_text='[SUBAGENT_RESULT]\n{"status":"DONE"}\n[/SUBAGENT_RESULT]'))
    snapshot = runtime.store.snapshot()
    assert not [row for row in snapshot.stable_blocks if row.role == 'assistant' and row.text]
    assert not snapshot.active_blocks


def test_plain_real_local_handler_uses_committed_callback_and_public_final(tmp_path, capsys):
    from agent_py_agent.agent.conversation.local_run_control import LocalRunControl
    from agent_py_agent.cli.chat_parts.plain_handlers import PlainJobContext, _plain_local_handle
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend
    agent = _agent(tmp_path)
    class LabelledBackend(_StaticBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            on_chunk('PASS')
            on_chunk('WORD=' + _SECRET)
            return ModelResponse(text=_LABELLED, backend=self.name)
    agent.backend = LabelledBackend()
    outputs = []
    job = ChatJob(user='核对当前正文', show_prompt=False, inject=[], prompt_files=[], request_id='plain-public-554')
    response, streamed = _plain_local_handle(PlainJobContext(job, agent, SimpleNamespace(no_save=True), None,
        outputs, lambda:'', local_run=LocalRunControl(job.request_id)))
    assert response == _LABELLED and streamed is False and agent.backend.calls == 1
    assert _SECRET not in capsys.readouterr().out
    assert outputs == [project_user_reply(_LABELLED).content]


def test_fake_loop_keeps_raw_phase_context_but_real_cli_and_tui_stay_public(tmp_path, capsys):
    from agent_py_agent.cli.run_output import make_run_chunk_writer
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend
    agent = _agent(tmp_path)
    response = _response([_message('PASSWORD=', 'commentary'), _message(_SECRET, 'final_answer', message_id='final-554')])
    class LabelledPhaseBackend(_StaticBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            on_chunk('PASS')
            on_chunk('WORD=' + _SECRET)
            return response
    agent.backend = LabelledPhaseBackend()
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda:None), {})
    result = agent.run('核对原始来源', on_chunk=writer, save=False)
    assert result.response == result.model_response == _LABELLED
    assert result.runtime_status == 'ok' and agent.backend.calls == 1
    visible = _unstreamed_final_response(result, '')
    assert _SECRET not in visible and '[REDACTED]' in visible
    assert _SECRET not in capsys.readouterr().out
    runtime = TuiRuntime('public-core-554')
    runtime.begin_turn('request-core-554')
    runtime.complete_turn('request-core-554', TuiTurnSummary(response_text=result.response))
    assert _SECRET not in ''.join(row.text for row in runtime.store.snapshot().stable_blocks)


def test_progress_and_query_guidance_share_override_without_duplicate_instructions(tmp_path):
    from agent_py_agent.agent.prompting_parts.builder import PromptBuilder, PromptBuildRequest
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(prompt_files=[], assistant_progress_updates_enabled=True,
                         query_zero_result_review_enabled=True)
    builder = PromptBuilder(config, tmp_path)
    request = PromptBuildRequest('核对来源', [], system_prompt_override='当前系统说明')
    combined = builder.prepare_render_input(request).system_prompt
    assert combined.startswith('当前系统说明')
    assert combined.count('执行中沟通：') == combined.count('查询结果纪律') == 1
    config.query_zero_result_review_enabled = False
    progress_only = builder.prepare_render_input(request).system_prompt
    assert progress_only.count('执行中沟通：') == 1 and '查询结果纪律' not in progress_only
    config.assistant_progress_updates_enabled = False
    assert builder.prepare_render_input(request).system_prompt == '当前系统说明'
