"""V03-02：真实 resolver/原路由行为回归；分数输入不是在线模型语义验收。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.business_capabilities import (
    BusinessCapabilityRequest,
    BusinessCapabilityService,
    resolve_business_capabilities,
)
from agent_py_agent.agent.capability.router import CapabilityCard, CapabilitySearchHit, score_card
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.tests.test_business_v12_f6_capabilities import _host, _package
from agent_py_agent.tests.test_tool_registry import registry

_FIXTURES = (registry,)


def _mount(package, position, **changes):
    return {
        'package_ref': package.stable_id, 'position': position, 'enabled': True,
        'version_ref': None,
        'availability': {'ref': package.stable_id, 'state': 'available',
                         'reason_code': '', 'required_capabilities': []},
        **changes,
    }


def _hit(host, package, score):
    card = next(card for card in host.capability_router.cards(kinds={'capability_package'})
                if card.metadata['stable_id'] == package.stable_id)
    return CapabilitySearchHit(card, score, ['query_match'])


def _resolve(host, context, hits, limit=0):
    return resolve_business_capabilities(
        context=context, host=host,
        request=BusinessCapabilityRequest('batch', limit=limit, candidates=tuple(hits)),
    )


@pytest.mark.parametrize('reverse_hits', [False, True])
@pytest.mark.parametrize('reverse_mounts', [False, True])
def test_relevance_precedes_mount_position(registry, tmp_path, reverse_hits, reverse_mounts):
    host, context, packages = _host(tmp_path, registry)
    mounts = [_mount(packages[0], 0), _mount(packages[1], 1)]
    host.business_spaces.mounts = mounts[::-1] if reverse_mounts else mounts
    hits = [_hit(host, packages[0], 1), _hit(host, packages[1], 100)]
    if reverse_hits:
        hits.reverse()
    resolved = _resolve(host, context, hits)
    assert [row['method_ref'] for row in resolved.candidates] == [
        packages[1].stable_id, packages[0].stable_id,
    ]
    assert [hit.score for hit in resolved.hits] == [100, 1]
    assert all(row['reason_codes'] == ['query_match', 'mounted_first']
               for row in resolved.candidates)
    assert all(row['origin']['source_ref'] == context.space_id for row in resolved.candidates)


@pytest.mark.parametrize('reverse_hits', [False, True])
def test_equal_relevance_uses_position_not_list_or_hit_order(registry, tmp_path, reverse_hits):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts = [_mount(packages[0], 8), _mount(packages[1], 2)]
    hits = [_hit(host, packages[0], 7), _hit(host, packages[1], 7)]
    if reverse_hits:
        hits.reverse()
    resolved = _resolve(host, context, hits)
    assert [hit.card.id for hit in resolved.hits] == [packages[1].stable_id, packages[0].stable_id]
    assert [hit.score for hit in resolved.hits] == [7, 7]


def test_unmounted_candidates_keep_original_order_and_are_not_added(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts = []
    hits = [_hit(host, packages[0], 1), _hit(host, packages[1], 100)]
    resolved = _resolve(host, context, hits)
    assert resolved.hits == tuple(hits)
    assert all(row['reason_codes'] == ['query_match', 'automatic_route']
               and row['origin']['source_ref'] is None for row in resolved.candidates)
    host.business_spaces.mounts = [_mount(packages[1], 0)]
    assert _resolve(host, context, hits[:1]).hits == tuple(hits[:1])


def test_relevant_mount_still_precedes_unmounted_higher_score(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    hits = [_hit(host, packages[1], 100), _hit(host, packages[0], 1)]
    resolved = _resolve(host, context, hits)
    assert resolved.hits == (hits[1], hits[0])
    assert resolved.candidates[1]['reason_codes'] == ['query_match', 'automatic_route']


@pytest.mark.parametrize(('changes', 'state', 'reason'), [
    ({'enabled': False}, 'disabled', 'mount_disabled'),
    ({'version_ref': 'different-version'}, 'missing', 'version_unavailable'),
    ({'availability': {'ref': 'capability:mounted', 'state': 'not_ready',
                      'reason_code': 'tools_unavailable', 'required_capabilities': []}},
     'not_ready', 'tools_unavailable'),
])
def test_unavailable_mount_falls_back_without_grant_or_false_priority(
    registry, tmp_path, changes, state, reason,
):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts = [_mount(packages[0], 0, **changes), _mount(packages[1], 1)]
    hits = [_hit(host, packages[0], 100), _hit(host, packages[1], 1)]
    resolved = _resolve(host, context, hits)
    assert resolved.hits == (hits[1], hits[0])
    assert resolved.candidates[1]['reason_codes'] == ['query_match', 'automatic_route']
    assert resolved.unavailable == ({'ref': packages[0].stable_id, 'state': state,
                                    'reason_code': reason, 'required_capabilities': []},)


@pytest.mark.parametrize('mutation', ['remove', 'activation', 'content'])
def test_live_package_recheck_rejects_old_high_score(registry, tmp_path, mutation):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts = [_mount(packages[0], 0), _mount(packages[1], 1)]
    hits = [_hit(host, packages[1], 100), _hit(host, packages[0], 1)]
    assert {hit.card.id for hit in _resolve(host, context, hits).hits} == {
        package.stable_id for package in packages
    }
    if mutation == 'remove':
        packages.pop(1)
    elif mutation == 'activation':
        packages[1] = replace(packages[1], activation_id='e' * 64)
    else:
        packages[1] = replace(packages[1], package_sha256='c' * 64)
    resolved = _resolve(host, context, hits)
    assert resolved.hits == (hits[1],)
    assert resolved.unavailable[0]['reason_code'] == 'package_unavailable'


def test_new_live_package_cannot_expand_frozen_candidates(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    from agent_py_agent.agent.capability.router import from_capability_package

    new_package = _package('new-live-only', 'c')
    packages.append(new_package)
    host.business_spaces.mounts.append(_mount(new_package, 1))
    hits = [CapabilitySearchHit(from_capability_package(new_package), 1000, ['query_match']),
            _hit(host, packages[0], 1)]
    assert _resolve(host, context, hits).hits == (hits[1],)


def test_space_revocation_and_owner_mismatch_still_refuse(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    hits = [_hit(host, packages[0], 100)]
    assert _resolve(host, context, hits).hits == tuple(hits)
    with pytest.raises(PermissionError, match='Owner'):
        _resolve(host, replace(context, owner_id='local/other'), hits)
    host.business_spaces.revoked = True
    with pytest.raises(PermissionError, match='scope revoked'):
        _resolve(host, context, hits)


def test_high_scores_do_not_bypass_frozen_tool_or_workflow_permissions(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    host._current_run_params.tool_runtime_snapshot = registry.runtime_snapshot(allowed_tools=[], run_id='run')
    tool = next(card for card in host.capability_router.cards(kinds={'tool'})
                if card.name == 'audit_group_query')
    workflow = CapabilityCard('workflow:requires-tool', 'workflow', 'requires-tool', 'batch',
                              source='owner', metadata={'tools_required': ['audit_group_query']})
    package_hit = _hit(host, packages[0], 1)
    hits = [CapabilitySearchHit(tool, 1000, ['query_match']),
            CapabilitySearchHit(workflow, 1000, ['query_match']), package_hit]
    assert _resolve(host, context, hits).hits == (package_hit,)


@pytest.mark.parametrize('limit', [0, 1, 2])
def test_limit_applies_after_relevance_sort_without_mutating_inputs(registry, tmp_path, limit):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts.append(_mount(packages[1], 1))
    hits = [_hit(host, packages[0], 1), _hit(host, packages[1], 100)]
    frozen_tools = host._current_run_params.tool_runtime_snapshot
    resolved = _resolve(host, context, hits, limit)
    expected = (hits[1], hits[0])
    assert resolved.hits == (expected[:limit] if limit else expected)
    assert [hit.score for hit in hits] == [1, 100]
    assert host._current_run_params.tool_runtime_snapshot is frozen_tools
    assert business_action_spec('spaces.capabilities.resolve').validate_result(resolved.to_dict()) == resolved.to_dict()


def test_original_router_and_read_action_use_original_scoring(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    host.business_spaces.mounts.append(_mount(packages[1], 1))
    cards = host.capability_router.cards(kinds={'capability_package'})
    scores = {card.id: score_card('batch', card)[0] for card in cards}
    assert scores[packages[1].stable_id] > scores[packages[0].stable_id] > 0
    hits = host.capability_router.search('batch', limit=1, kinds={'capability_package'})
    assert [hit.card.id for hit in hits] == [packages[1].stable_id]
    result = BusinessCapabilityService(host).execute(
        'spaces.capabilities.resolve', {'space_id': context.space_id, 'query': 'batch'}, context=context,
    )
    assert [row['method_ref'] for row in result['candidates'] if row['kind'] == 'package'] == [
        packages[1].stable_id, packages[0].stable_id,
    ]
