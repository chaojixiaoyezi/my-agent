"""原C1已构造资源的算法委托；C3构造writer未闭，因此不当作C endpoint验收。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import coordinator_client_memory as module
from agent_py_agent.agent.gateway_parts.owner_resources import resolve_owner_resource_configuration
from agent_py_agent.agent.memory_store import owner_resources
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def resources(tmp_path):
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), memory_semantic_recall=False)
    control = SimpleNamespace(config=config, home_root=tmp_path / "home")
    home, config, paths = resolve_owner_resource_configuration(control, OwnerIdentity.local_main())
    authority = owner_resources.build_owner_memory_authorities(
        config,
        home,
        paths,
        None,
        embedder=None,
        semantic_status={"state": "disabled"},
        vector_identity=None,
    )
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import (
        owner_conversation_store_from_config,
    )

    conversation = owner_conversation_store_from_config(config, home, initialize=False)
    promotion = owner_resources.build_owner_memory_promotion(
        config,
        home,
        authority,
        conversation_store=conversation,
        lessons=owner_resources.build_owner_lessons(home),
        hot=owner_resources.build_owner_hot(home),
    )
    return module._OwnerMemoryDomain(config, home, paths, authority, promotion)


def test_remember_uses_original_explicit_identity_and_replay(tmp_path):
    value = resources(tmp_path)
    first = module._remember_current_owner(value, "  ordinary explicit fact  ", "fact")
    second = module._remember_current_owner(value, "ordinary explicit fact", "fact")
    assert first[0]["entry_id"] == second[0]["entry_id"]
    assert first[0]["content"] == "ordinary explicit fact"
    assert len(value.long_term.all()) == 1


@pytest.mark.parametrize("failed_flush", [False, True])
def test_search_calls_original_once_and_does_not_retry_after_flush_failure(
    tmp_path, monkeypatch, failed_flush
):
    value = resources(tmp_path)
    module._remember_current_owner(value, "ordinary explicit fact", "fact")
    calls, original = [], value.long_term.search

    def search(query, limit):
        calls.append((query, limit))
        return original(query, limit)

    error = OSError("synthetic flush failure")

    def flush():
        calls.append("flush")
        if failed_flush:
            raise error

    monkeypatch.setattr(value.long_term, "search", search)
    monkeypatch.setattr(value.long_term, "flush_access_events_from_source", flush)
    if failed_flush:
        with pytest.raises(OSError) as caught:
            module._search_current_owner(value, "ordinary", 3)
        assert caught.value is error
    else:
        assert (
            module._search_current_owner(value, "ordinary", 3)[0]["content"]
            == "ordinary explicit fact"
        )
    assert calls == [("ordinary", 3), "flush"]
