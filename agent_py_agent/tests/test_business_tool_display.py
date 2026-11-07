"""真实工具结果经公共进度、TUI reducer 和 renderer 后只显示摘要。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _payload_progress_detail,
    _structured_tool_progress,
)
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.tooling.artifact import ReadArtifactTool
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import _tool_payload
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore
from agent_py_agent.tests.test_memory_artifact_read import _write_externalized_tool_output
from agent_py_agent.tests.test_tool_display_archive import _event
from agent_py_agent.tests.test_tool_round_execution import _canonical_calls


@pytest.mark.parametrize('tool_name', ['workflows', 'read_artifact'])
@pytest.mark.parametrize('show_all', [False, True])
def test_real_results_render_summary_and_keep_original_archive(tmp_path, tool_name, show_all):
    if tool_name == 'workflows':
        tool = WorkflowsTool(tmp_path / 'owner')
        params = {'action': 'list'}
        outcome = tool.execute(params)
    else:
        original = json.dumps({'items': ['归档原文', 'call_00_internal_test']}, ensure_ascii=False) * 20
        _write_externalized_tool_output(tmp_path, content=original, call_id='call_00_internal_test')
        tool = ReadArtifactTool(tmp_path)
        params = {'artifact_ref': 'run-artifact:call_00_internal_test', 'run_id': 'run-artifact',
                  'task_id': 'task-artifact', 'request_id': 'req-artifact', 'max_chars': 4000}
        outcome = tool.execute(params)
        assert json.loads(outcome.output)['content'] == original
    assert outcome.ok, outcome.output
    event, _ = _event(tmp_path)
    call = _canonical_calls([{'tool': tool_name, **params}])[0]
    result = ToolResult.succeeded(call, outcome.output, facts=ToolSuccessFacts(
        metadata={'handler_details': outcome.result_envelope}))
    event = replace(event, call=call, result=result)
    detail = _payload_progress_detail(call)
    assert 'call_00_' not in detail
    progress = _structured_tool_progress(event, tool_name, detail)
    store = TuiStateStore()
    seq = TuiEventSequencer('business-display', clock=lambda: 10.0)
    store.publish(seq.emit('tool_started', 'started', 'tool:1', {'tool': tool_name, 'detail': detail}))
    store.publish(seq.emit('tool_completed', 'completed', 'tool:1', _tool_payload(progress)))
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=120, show_all=show_all))
    text = '\n'.join(fragments_text(line) for line in frame.transcript_lines)
    assert ('工作流' if tool_name == 'workflows' else '已读取归档') in text
    assert 'Ctrl+E' in text
    assert 'call_00_' not in text and '"items"' not in text and '"workflows"' not in text
    reference = progress['display_archive_ref']
    archived = '\n'.join(row['text'] for row in read_display_archive_page(
        event.request.agent.conversation_store, reference, 0)['rows'])
    assert outcome.output in archived
    assert event.result.output == outcome.output


def test_workflow_summary_preserves_error_state(tmp_path):
    result = WorkflowsTool(tmp_path / 'owner').execute({'action': 'workflows.list', 'space_id': 'missing'})
    assert not result.ok
    assert '失败' in result.result_envelope['display']['summary']
    assert result.error_code in result.result_envelope['display']['summary']


def test_workflow_host_error_preserves_unknown_effect(tmp_path):
    def unavailable():
        raise HostAPIError('HOST_NOT_READY', 503, 'unknown')

    result = WorkflowsTool(tmp_path / 'owner', business_binding_provider=unavailable).execute({'action': 'workflows.list'})
    assert not result.ok and result.effect_outcome == 'unknown'
