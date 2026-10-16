"""Independent activation/permission-window probes using the real PromptBuilder."""
import json

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.tests.test_agents_source_cache import remote as remote
from agent_py_agent.tests.test_agents_source_prefix import scene as scene
from agent_py_agent.tests.test_agents_source_prefix import select
from agent_py_agent.tests.test_agents_source_repository import source as source
from agent_py_agent.tests.test_thread_persona_freeze import prefix_and_updates, request


@pytest.mark.parametrize('replacement', ['leaf', 'parent'])
def test_activation_cannot_rebind_equal_body_replacement(scene, monkeypatch, replacement):
    source, _, builder = scene
    (source.owner_home/'docs').mkdir()
    path = select(source, 'docs/rules.bin', '# Rules\n- IDENTITY_WINDOW_MARKER\n')
    original = source.load
    done = []
    def load_then_replace():
        result = original()
        if not done:
            done.append(True)
            raw = path.read_bytes()
            if replacement == 'leaf':
                path.rename(path.with_name('old-rules'))
            else:
                path.parent.rename(source.owner_home/'old-docs')
                path.parent.mkdir()
            path.write_bytes(raw)
        return result
    monkeypatch.setattr(source, 'load', load_then_replace)
    first = prefix_and_updates(builder, request())
    monkeypatch.setattr(source, 'load', original)
    rejected = source.load()
    assert rejected.snapshot.failure_code == 'AGENTS_SOURCE_IDENTITY_CHANGED'
    second = prefix_and_updates(builder, request())
    assert 'IDENTITY_WINDOW_MARKER' not in ''.join(first + second), {'first': first, 'second': second, 'source_failure': rejected.snapshot.failure_code}


def test_activation_missing_source_is_diagnostic_not_prompt_failure(scene, monkeypatch):
    source, _, builder = scene
    path = select(source, 'gone.bin', '# Rules\n- DISAPPEARING_MARKER\n')
    original = source.load
    def load_then_remove():
        result = original()
        path.rename(path.with_name('saved.bin'))
        return result
    monkeypatch.setattr(source, 'load', load_then_remove)
    try:
        prefix, updates = prefix_and_updates(builder, request())
    except FileNotFoundError as exc:
        pytest.fail('PromptBuilder propagated missing source instead of unavailable diagnostic: ' + str(exc))
    assert 'DISAPPEARING_MARKER' not in prefix + updates


@pytest.mark.parametrize('revoke', [False, True])
def test_same_epoch_new_redirect_chain_rechecked_before_updates(scene, remote, monkeypatch, revoke):
    source, state = remote
    _, _, builder = scene
    start = 'https://example.com/rules'
    final = 'https://example.org/final'
    state['body'] = b'# Rules\n- OLD_VALID_MARKER\n'
    first, _ = prefix_and_updates(builder, request())
    assert 'OLD_VALID_MARKER' in first
    state['body'] = b'# Rules\n- NEW_REDIRECT_MARKER\n'
    state['urls'] = (start, final)
    allowed = {start, final}
    source._url_allowed = lambda url: url in allowed
    from agent_py_agent.agent.prompting_parts import builder as module
    original = module._render_persona_sections
    current_metadata = []
    def render_then_revoke(*args, **kwargs):
        rendered = original(*args, **kwargs)
        current_metadata.append(rendered.agents_source_metadata)
        if revoke:
            allowed.discard(final)
        return rendered
    monkeypatch.setattr(module, '_render_persona_sections', render_then_revoke)
    prefix, updates = prefix_and_updates(builder, request())
    assert prefix == first
    if revoke:
        with pytest.raises(AgentsContractError, match='PERMISSION_REVOKED'):
            source.require_active_permission(current_metadata[-1])
        assert 'NEW_REDIRECT_MARKER' not in updates, updates
    else:
        assert 'NEW_REDIRECT_MARKER' in updates
