"""实际HTTP/source/holder末核；只改普通临时policy和关闭位，外部provider为原fake。"""

from contextlib import contextmanager

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_local_control import _LocalControlWriteSource
from agent_py_agent.tests.test_coordinator_memory_end_to_end import memory_post, semantic_provider
from agent_py_agent.tests.test_coordinator_private_credential_transactions import original_case


@pytest.mark.parametrize("change", ["quota", "memory"])
def test_policy_changed_after_actual_borrow_cannot_use_captured_policy(
    tmp_path, monkeypatch, change
):
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        embedder = semantic_provider(server, monkeypatch)
        actor = row["actor_id"]
        assert memory_post(
            server, actor, operation="remember", content="original policy before wait"
        )[1]["ok"]
        h = server.coordinator_memory_resources
        domain = next(iter(h._entries.values())).future.result()
        quota = domain.long_term.quota_enforcer
        original = h.borrow_current_owner
        before = domain.long_term.path.read_bytes()
        calls = len(embedder.calls)

        @contextmanager
        def change_after_borrow(home, factory):
            with original(home, factory) as resources:
                if change == "quota":
                    resources.home.owner_quota_json.write_text('{"max_disk_mb":1}')
                else:
                    resources.home.owner_memory_policy_json.write_text('{"enabled":false}')
                yield resources

        monkeypatch.setattr(h, "borrow_current_owner", change_after_borrow)
        status, result = memory_post(
            server, actor, operation="remember", content="must not use stale policy"
        )
        assert (
            status == 200
            and not result["ok"]
            and result["error_code"] == "LocalControlCoordinatorUnavailable"
        )
        assert domain.long_term.path.read_bytes() == before and len(embedder.calls) == calls
        assert domain.long_term.quota_enforcer is quota and quota.max_bytes == 0
        assert all(entry.borrowers == 0 for entry in h._entries.values())


def test_query_revocation_after_actual_local_search_stops_paid_query_and_flush(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.local_storage import LocalStore

    observed = []
    perform = _LocalControlWriteSource.perform
    original = LocalStore.search

    def track(source, operation):
        observed.append(source)
        return perform(source, operation)

    monkeypatch.setattr(_LocalControlWriteSource, "perform", track)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, runtime):
        embedder = semantic_provider(server, monkeypatch)
        actor = row["actor_id"]
        assert memory_post(server, actor, operation="remember", content="query edge needle")[1][
            "ok"
        ]
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        before = domain.long_term.path.read_bytes()
        calls = len(embedder.calls)

        def close_after_search(store, *args, **kwargs):
            hits = original(store, *args, **kwargs)
            runtime._closing = True
            return hits

        monkeypatch.setattr(LocalStore, "search", close_after_search)
        status, result = memory_post(server, actor, query="needle")
        assert (
            status == 200
            and not result["ok"]
            and result["error_code"] == "LocalControlCoordinatorUnavailable"
        )
        assert (
            len(embedder.calls) == calls
            and domain.long_term.path.read_bytes() == before
            and not domain.long_term._pending_access
        )
        source = observed[-1]
        assert (
            source._native_source_failure is not None
            and not source.lifetime.active
            and not source.lease._commit_pins
        )
