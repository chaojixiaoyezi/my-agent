"""W9: first archive failure survives the canonical execution and record paths."""
import errno
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.tests.test_tool_runtime_unification import _call, _CountingTool, _execute


@pytest.mark.parametrize('number', [errno.ENOSPC, errno.EIO])
def test_archive_failure_reports_original_errno_once_without_secrets(tmp_path, number):
    tool = _CountingTool()

    def fail(call, outcome):
        raise OSError(number, 'token=private-token', '/private/secret-file')

    execution = _execute(tmp_path, tool, _call(tool, {'value': 'private-output'}), output_archiver=fail)
    result = execution.result
    assert tool.executions == 1
    assert result.error_code == 'TOOL_OUTPUT_ARCHIVE_FAILED'
    assert result.failure_stage == 'persistence' and result.handler_executed is True
    assert result.effect_outcome == 'confirmed'
    diagnostic = result.metadata.get('diagnostic') or {}
    assert diagnostic.get('errno') == number
    assert diagnostic.get('operation') == 'tool_output.archive'
    assert result.metadata.get('diagnostic_storage') == 'volatile'
    assert diagnostic.get('diagnostic_ref', '').startswith('volatile:')
    visible = json.dumps(result.to_dict(), ensure_ascii=False)
    for secret in ('private-token', 'secret-file', 'private-output'):
        assert secret not in visible


def test_explicit_cause_chain_is_bounded_and_keeps_the_first_oserror(tmp_path):
    tool = _CountingTool()

    def fail(call, outcome):
        root = OSError(errno.EIO, 'password=private-password', '/private/file')
        wrapper = RuntimeError('private wrapper')
        wrapper.__cause__ = root
        root.__cause__ = wrapper
        raise wrapper

    result = _execute(tmp_path, tool, _call(tool, {'value': 'private-output'}), output_archiver=fail).result
    assert (result.metadata.get('diagnostic') or {}).get('errno') == errno.EIO
    causes = (result.metadata.get('archive_diagnostic') or {}).get('causes', [])
    assert 1 <= len(causes) <= 4
    assert 'private' not in json.dumps(result.metadata)


def test_record_does_not_retry_a_failed_archive_or_overwrite_its_unique_diagnostic(tmp_path, monkeypatch):
    tool = _CountingTool()

    def fail(call, outcome):
        raise OSError(errno.ENOSPC, 'full')

    execution = _execute(tmp_path, tool, _call(tool, {'value': 'private-output'}), output_archiver=fail)
    calls = []

    def second_archive(request):
        calls.append(request)
        raise OSError(errno.EIO, 'secondary failure must not replace ENOSPC')

    monkeypatch.setattr(archive, 'externalize_tool_output_record', second_archive)
    # The result already owns this exact test scope; no new run/owner lookup is necessary.
    execution.result.metadata['handler_details']['scope'] = {'run_id': execution.call.run_id, 'depth': 0}
    params = SimpleNamespace(request_id='request-one', task_id='', run_id=execution.call.run_id,
                             attempt_id=execution.call.attempt_id, live_archive_state={})
    agent = SimpleNamespace(config=SimpleNamespace(tool_output_preview_chars=1200))
    record = ToolCallRecordParams(params, 1, 0, execution.call, execution.result, execution.states)
    try:
        row = archive.archive_tool_call_record(agent, record)
    except OSError:
        row = {}
    assert calls == [], 'recording a known failed projection must not re-enter the broken disk'
    assert row.get('diagnostic') == execution.result.metadata.get('diagnostic')
    assert row.get('diagnostic_storage') == 'volatile'
    assert row.get('handler_executed') is True
    assert row.get('failure_stage') == 'persistence'
    assert tool.executions == 1
