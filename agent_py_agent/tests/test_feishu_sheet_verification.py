"""原表格写入身份/后端读回隔离验收；不访问真实租户。"""
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from agent_py_agent.agent.adapter.feishu_cloud import FeishuCloudError
from agent_py_agent.agent.user_space.feishu_sheet_verification import verify_sheet_write
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_feishu_cloud_tool import _agent, _call, _grant


def test_same_tool_identity_accepts_write_then_mechanically_verifies(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import feishu_cloud_tool

    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    calls = []
    def request(adapter, method, path, body=None):
        calls.append((adapter, method, path))
        if method == 'POST':
            return {'updates': {'updatedRange': 'sid!A17:B17'}}
        assert unquote(path).endswith('/values/sid!A17:B17')
        return {'valueRange': {'range': 'sid!A17:B17', 'values': [['准确行', 123]]}}
    monkeypatch.setattr(feishu_cloud_tool, 'request_cloud_api', request)
    result = _call(agent, 'sheet_append', range='sid!A1:B9', values=[['准确行', 123]])
    facts = json.loads(result.output)['write_verification']
    assert result.ok and facts['status'] == 'verified' and facts['write_accepted']
    assert [call[1] for call in calls] == ['POST', 'GET'] and calls[0][0] is calls[1][0]


@pytest.mark.parametrize('mode', ['mismatch', 'read-failed', 'transformed', 'missing-range'])
def test_accepted_write_remains_accepted_without_replay_when_readback_not_verified(tmp_path, monkeypatch, mode):
    from agent_py_agent.agent.tooling import feishu_cloud_tool

    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    calls = []
    def request(adapter, method, path, body=None):
        calls.append(method)
        if method == 'PUT':
            return {'updatedRange': 'sid!A1:A1'}
        if mode == 'read-failed':
            raise FeishuCloudError('FEISHU_CLOUD_RESOURCE_FORBIDDEN', '读取被拒绝。', provider_code=91403)
        block = {'values': [[456 if mode == 'mismatch' else '123' if mode == 'transformed' else 123]]}
        if mode != 'missing-range':
            block['range'] = 'sid!A1:A1'
        return {'valueRange': block}
    monkeypatch.setattr(feishu_cloud_tool, 'request_cloud_api', request)
    result = _call(agent, 'sheet_update', range='sid!A1', values=[[123]])
    facts = json.loads(result.output)['write_verification']
    assert result.ok and facts['write_accepted']
    assert facts['status'] == ('mismatch' if mode == 'mismatch' else 'unverifiable')
    assert calls == ['PUT', 'GET'] and result.effect_outcome != 'not_started'


@pytest.mark.parametrize('mode', ['unknown-append', 'cross-sheet', 'wrong-size', 'formula', 'rich-value', 'disabled'])
def test_verification_does_not_guess_range_representations_or_disabled_reads(mode):
    params = {'action': 'sheet_append', 'resource_token': 'resource', 'range': 'sid!A1:B9', 'values': [[123]]}
    result = {'updates': {'updatedRange': 'sid!A10:A10'}}
    if mode == 'unknown-append':
        result = {}
    elif mode == 'cross-sheet':
        result['updates']['updatedRange'] = 'other!A10:A10'
    elif mode == 'wrong-size':
        result['updates']['updatedRange'] = 'sid!A10:B10'
    elif mode == 'formula':
        params['values'] = [['=1+1']]
    elif mode == 'rich-value':
        params['values'] = [[{'type': 'url', 'text': 'private'}]]
    reads = []
    facts = verify_sheet_write(None, lambda *_: reads.append(1), params, result, enabled=mode != 'disabled')
    assert facts['status'] == ('disabled' if mode == 'disabled' else 'unverifiable')
    assert facts['write_accepted'] and not reads and 'private' not in json.dumps(facts)


def test_update_reads_only_actual_written_extent_and_ragged_input_cells():
    params = {'action': 'sheet_update', 'resource_token': 'resource', 'range': 'sid!Z4:ZZ99', 'values': [[1, 2], [3]]}
    calls = []
    def read(adapter, method, path, body):
        calls.append((adapter, method, unquote(path)))
        return {'valueRange': {'range': 'sid!Z4:AA5', 'values': [[1, 2], [3, None]]}}
    adapter = SimpleNamespace(actor_id='exact-actor')
    facts = verify_sheet_write(adapter, read, params, {}, enabled=True)
    assert facts['status'] == 'verified' and facts['range'] == 'sid!Z4:AA5'
    assert calls[0][0] is adapter and calls[0][2].endswith('/values/sid!Z4:AA5')


def test_config_disabled_stays_explicit_and_sends_no_readback(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import feishu_cloud_tool

    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    snapshot = agent._capability_config_runtime_snapshot
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=replace(
        snapshot.config, feishu_cloud_verify_sheet_writes_enabled=False))
    calls = []
    def request(adapter, method, path, body=None):
        calls.append(method)
        return {'updatedRange': 'sid!A1:A1'}
    monkeypatch.setattr(feishu_cloud_tool, 'request_cloud_api', request)
    result = _call(agent, 'sheet_update', range='sid!A1', values=[[123]])
    assert result.ok and json.loads(result.output)['write_verification']['status'] == 'disabled'
    assert calls == ['PUT']


@pytest.mark.parametrize('reported', ['other!A1:A1', 'sid!B1:B1', 'sid!A2:A2', 'sid!A1:B', None])
def test_update_does_not_verify_conflicting_provider_range(reported):
    params = {'action': 'sheet_update', 'resource_token': 'resource', 'range': 'sid!A1', 'values': [[123]]}
    reads = []
    facts = verify_sheet_write(None, lambda *_: reads.append(1), params, {'updatedRange': reported}, enabled=True)
    assert facts['status'] == 'unverifiable' and not reads
