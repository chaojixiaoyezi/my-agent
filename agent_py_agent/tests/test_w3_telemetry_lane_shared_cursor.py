"""The two bindings of the task_call_telemetry storage lane share one cursor and must accept each other's cursor.

Live clone (2026-11-17): storage maintenance of local/main failed every cycle with
"invalid task telemetry maintenance phase". LANE_PROJECTION_BINDINGS runs maintain_tool_call_projection_lane
(phases active/sealed) while retention deletion is unsafe and LANE_BINDINGS runs maintain_task_call_telemetry_lane
(phases tasks/tools) once it is safe, both with the same task_call_telemetry cursor; the retire lane got 'sealed'.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.observability import task_call_retention as retention
from agent_py_agent.agent.observability.telemetry_ledger import (
    PARTS,
    ToolCallRecord,
    maintain_tool_call_projection_lane,
)
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests.test_admin_task_telemetry import _request, _task


def _projection(owner, cursor):
    return validate_lane_receipt(maintain_tool_call_projection_lane(StorageLaneRequest(
        kind='task_call_telemetry', home=SimpleNamespace(owner_home_dir=owner), now=200_000, cursor=cursor,
        budget=StorageLaneBudget(4, 65536, None), legal_hold=True, coordinator=True, pressure=None,
        commit=lambda op: op())))


def test_retire_lane_restarts_from_a_projection_cursor(tmp_path):
    task = _task(tmp_path)
    task.complete_task_call('task', 'succeeded', ended_at=2)
    foreign = {'phase': 'sealed', 'scan': {'offset': 2147483647}, 'source_membership_revision': [1, 2, 3]}
    result = retention.maintain_task_call_telemetry_lane(_request(tmp_path, cursor=foreign))
    assert result['cursor'].get('phase') in {'tasks', 'tools'}
    assert 'source_membership_revision' not in result['cursor']


def test_projection_lane_restarts_from_a_retire_cursor(tmp_path):
    owner = tmp_path / 'owner'
    source = owner.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True)
    source.write_text(''.join(json.dumps(ToolCallRecord(call_id=str(i), tool_name='read_file').to_dict()) + '\n'
                              for i in range(3)))
    receipt = _projection(owner, {'phase': 'tools', 'scan': {}, 'repair_summaries': False})
    assert receipt['cursor'].get('phase') in {'active', 'sealed'}
    assert 'repair_summaries' not in receipt['cursor']


def test_unknown_phases_still_fail_closed(tmp_path):
    _task(tmp_path)
    with pytest.raises(ValueError):
        retention.maintain_task_call_telemetry_lane(_request(tmp_path, cursor={'phase': 'bogus'}))
    with pytest.raises(ValueError):
        _projection(tmp_path / 'owner', {'phase': 'bogus'})
