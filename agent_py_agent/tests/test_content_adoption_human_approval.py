"""内容采用复用原安全输入和工具批准绑定，普通业务凭据不能签出批准。"""
import json
import threading
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import write_gateway_permission_decision
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_business_model_tools import agent as agent
from agent_py_agent.tests.test_mega_business_api_actions import native_request


def upload(agent, tmp_path, sid):
    from io import BytesIO

    from agent_py_agent.agent.gateway_parts.business_artifacts_http import upload_artifact
    context, services = local_business_context(agent, model=True)
    value = upload_artifact(BytesIO(b'original material'), {'space_id': sid, 'idempotency_key': 'content-upload-key'},
                            'material.txt', context, services)
    return {key: value[key] for key in ('artifact_id', 'version_ref', 'content_sha256')}


def setup_adoption(agent, tmp_path):
    agent.config.business_host_ports_enabled = True
    context, services = local_business_context(agent, model=True)
    actor = services.actor(context)
    sid = services.spaces.create({'name': '采用测试'}, actor=actor, idempotency_key='content-space-key')['space_id']
    ref = upload(agent, tmp_path, sid)
    asset = services.spaces.call('assets.register', {'space_id': sid, 'name': '资料', 'kind': 'document',
        'artifact_ref': ref}, actor=actor, idempotency_key='content-register-key')
    params = {'action': 'assets.versions.accept', 'request_id': 'content-request',
        'idempotency_key': 'content-adoption-key', 'expected_revision': 0,
        'params': {'space_id': sid, 'asset_id': asset['asset_id'], 'version_ref': ref['version_ref']}}
    from agent_py_agent.agent.gateway_parts.business_api import register_business_tools
    register_business_tools(agent)
    tool = agent.tools.tools['business_assets_versions_accept']
    return services, actor, params, tool


def execute(agent, tool, params, approved=(), **options):
    return execute_canonical_test_call(agent.root, tools={tool.model_spec.name: tool},
        tool_name=tool.model_spec.name, arguments=params, write_boundary={'approved_actions': list(approved)},
        operation_id='content-operation', idempotency_key='content-operation-key',
        operation_owner_id=agent.home_paths.owner_id, **options)


def approve_through_original_input(tmp_path, call, owner_id, decision_kind="approved"):
    request = build_tool_approval_request(call, request_id='content-request', round_number=1,
        call_index=0, description='确认指定资料版本', require_human=True, owner_id=owner_id)
    path = tmp_path / 'content-chunks.jsonl'
    writer = BufferedChunkStreamWriter(path, interactive_approvals=True,
        approval_mode_decision_provider=lambda request: pytest.fail("人工确认不能调用自动批准器"))
    result = {}
    worker = threading.Thread(target=lambda: result.update(writer.request_permission(request.to_dict())))
    worker.start()
    deadline = time.monotonic() + 3
    while not path.exists() or 'permission_requested' not in path.read_text():
        assert time.monotonic() < deadline
        time.sleep(.01)
    write_gateway_permission_decision(path, request, ToolApprovalDecision(request.permission_id, decision_kind))
    worker.join(3)
    assert not worker.is_alive()
    decision = ToolApprovalDecision.from_mapping(result)
    return request.approved_binding(decision) if decision.approved else None


def test_original_human_input_adopts_and_replays_exact_version(agent, tmp_path):
    services, actor, params, tool = setup_adoption(agent, tmp_path)
    pending = execute(agent, tool, params)
    assert pending.decision.status == 'ask' and pending.decision.evidence['human_confirmation_required']
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools
    snapshot = runtime_snapshot_for_tools({tool.model_spec.name: tool}, run_id=pending.call.run_id)
    assert ActionPolicy().decide(ActionPolicyRequest(pending.call, snapshot, agent.root,
        approval_mode='auto', owner_id=agent.home_paths.owner_id)).status == 'ask'
    assert not tool.execute(params).ok  # 直接调用或普通JSON不拥有执行器的人工批准证明。

    binding = approve_through_original_input(tmp_path, pending.call, agent.home_paths.owner_id)
    completed = execute(agent, tool, params, [binding])
    assert completed.result.ok, completed.result.output
    view = services.spaces.call(
        'assets.get', {k: params['params'][k] for k in ('space_id','asset_id')}, actor=actor)
    assert view['accepted_version'] == params['params']['version_ref']
    assert execute(agent, tool, params, [binding]).result.ok
    assert services.spaces.call('assets.get', {k: params['params'][k] for k in ('space_id','asset_id')}, actor=actor)['asset']['revision'] == 1


@pytest.mark.parametrize('change', ['expired', 'version', 'revision', 'ownerless', 'other_owner', 'raw_json'])
def test_changed_or_missing_human_binding_cannot_adopt(agent, tmp_path, change):
    services, actor, params, tool = setup_adoption(agent, tmp_path)
    pending = execute(agent, tool, params)
    binding = approve_through_original_input(tmp_path, pending.call, agent.home_paths.owner_id)
    if change == 'expired':
        binding['approval_expires_at'] = '1'
    elif change == 'version':
        params = {**params, 'params': {**params['params'], 'version_ref': 'another-version'}}
    elif change == 'revision':
        params = {**params, 'expected_revision': 1}
    elif change == 'other_owner':
        binding['owner_id'] = 'local/other'
    elif change == 'ownerless':
        binding.pop('human_confirmation_required')
    else:
        params = {**params, 'human_approval_check': True}
    result = execute(agent, tool, params, [binding])
    assert not result.result.ok
    assert services.spaces.call('assets.get', {k: params['params'][k] for k in ('space_id','asset_id')}, actor=actor)['accepted_version'] is None


def test_original_denial_and_plain_http_never_adopt(agent, tmp_path):
    services, actor, params, tool = setup_adoption(agent, tmp_path)
    pending = execute(agent, tool, params)
    assert approve_through_original_input(tmp_path, pending.call, agent.home_paths.owner_id, 'denied') is None
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'deny-gateway'),
                               params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        status, _, raw = native_request((server, agent), 'POST', '/client/business', params,
            user='admin', headers={'X-Channel': 'chat'})
        value = json.loads(raw)
        assert status == 403 and value['error']['code'] == 'HUMAN_RESPONSE_REQUIRED'
        assert value['effect_outcome'] == 'not_started'
    finally:
        server.stop()
    assert services.spaces.call('assets.get', {k: params['params'][k] for k in ('space_id', 'asset_id')},
                                actor=actor)['accepted_version'] is None


@pytest.mark.parametrize('effects', [(), ('read_only',)])
def test_human_mcp_policy_overrides_admin_auto_and_effect_filter(tmp_path, effects):

    from agent_py_agent.agent.tooling.models import ApprovalPolicy
    from agent_py_agent.tests.test_admin_full_access_mcp import invoke, mcp_case

    _, registry, client, tool = mcp_case(tmp_path)
    tool.runtime_policy = replace(tool.runtime_policy,
        approval_policy=ApprovalPolicy('always', always_effects=effects, require_human=True))
    outcome = invoke(registry, tool)
    assert outcome.decision.status == 'ask'
    assert outcome.decision.evidence['human_confirmation_required'] is True
    assert client.calls == []


def test_human_prompt_shows_exact_targets_and_only_one_time_choice(agent, tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_loop.round_execution import _approval_description

    _, _, params, tool = setup_adoption(agent, tmp_path)
    params['params'].update(space_id='space-' + 's' * 100, asset_id='asset-' + 'a' * 100,
                            version_ref='version-' + 'v' * 100)
    pending = execute(agent, tool, params)
    description = _approval_description(SimpleNamespace(agent=agent), 0, pending.call, require_human=True)
    assert len(description) > 240
    for value in params['params'].values():
        assert value in description
    assert 'expected_revision' in description and 'assets.versions.accept' in description
    assert '内容过长' not in description
    request = build_tool_approval_request(pending.call, request_id='display', round_number=1, call_index=0,
        description=description, require_human=True, owner_id=agent.home_paths.owner_id)
    assert [item['decision'] for item in request.options] == ['approved', 'denied']
    with pytest.raises(ValueError, match='one-time'):
        request.approved_binding(ToolApprovalDecision(request.permission_id, 'approved_session'))
