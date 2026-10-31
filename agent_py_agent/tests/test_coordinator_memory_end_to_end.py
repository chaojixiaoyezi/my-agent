"""实际临时C HTTP/work/G1和原记忆领域闭包；仅loopback与fake外部embedding，无真实模型/平台。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.gateway_parts import coordinator_client_memory as client_memory
from agent_py_agent.agent.gateway_parts.multiprocess_local_control import _LocalControlWriteSource
from agent_py_agent.tests.test_coordinator_model_menu import post_model
from agent_py_agent.tests.test_coordinator_private_credential_transactions import original_case


# LLM: 原handler按真实G1/owner/body裁决；本helper不补业务产物或替代source，只生成原client请求。
# 函数用途: 给隔离C实例发送一条search/remember/recent原协议请求。
def memory_post(server, actor, *, operation="search", query="", content=""):
    return post_model(server, {"conversation_id": "j3-current-memory", "operation": operation,
        "query": query, "content": content}, route="/client/memory", actor=actor)


@pytest.mark.parametrize("healthy", [False, True])
def test_original_http_remember_search_reuses_actual_domain_and_flushes_once(tmp_path, monkeypatch, healthy):
    with original_case(tmp_path, monkeypatch) as (agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        if healthy:
            server.agent = agent
        actor = row["actor_id"]
        status, saved = memory_post(server, actor, operation="remember", content="J3 current owner fact")
        assert status == 200 and saved["ok"], saved
        entry = saved["records"][0]["entry_id"]
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        long_term, quota = domain.long_term, domain.long_term.quota_enforcer
        assert long_term.coordinator_io and long_term.local_store.coordinator_io
        assert domain.authorities.candidates.coordinator_io and domain.promotion.coordinator_io
        assert quota is domain.authorities.persona.quota_enforcer
        assert quota is domain.authorities.candidates.quota_enforcer
        assert all(not value for key, value in vars(long_term).items() if key == "_pending_access")
        status, found = memory_post(server, actor, query="current owner")
        assert status == 200 and found["ok"], found
        assert found["records"][0]["entry_id"] == entry
        assert not long_term._pending_access
        same = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        assert same is domain and same.long_term is long_term
        assert server.agent is (agent if healthy else None)
        status, replay = memory_post(server, actor, operation="remember", content="J3 current owner fact")
        assert status == 200 and replay["ok"] and replay["records"][0]["entry_id"] == entry
        assert len(long_term.all()) == 1


def test_successful_http_finishes_original_source_before_response(tmp_path, monkeypatch):
    sources = []
    perform = _LocalControlWriteSource.perform
    def observe(source, operation):
        if not any(item is source for item in sources):
            sources.append(source)
        return perform(source, operation)
    monkeypatch.setattr(_LocalControlWriteSource, "perform", observe)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        status, result = memory_post(server, row["actor_id"], operation="remember", content="actual source exits")
        assert status == 200 and result["ok"], result
        assert sources and all(not source.lifetime.active for source in sources)
        assert all(not source.lease._commit_pins and source._native_source_failure is None for source in sources)
        assert all(value is not source for source in sources for value in vars(server.coordinator_memory_resources).values())


def test_real_source_native_after_formal_keeps_first_object_and_scope_exit_before_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl

    sources, errors = [], []
    perform = _LocalControlWriteSource.perform
    def observe(source, operation):
        if not any(item is source for item in sources):
            sources.append(source)
        try:
            return perform(source, operation)
        except BaseException as error:
            errors.append(error)
            raise
    monkeypatch.setattr(_LocalControlWriteSource, "perform", observe)
    write = jsonl.write_private_text_file_atomic_unlocked
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, runtime):
        server.control_plane.config.memory_semantic_recall = False
        def close_after_formal(path, raw, **kwargs):
            result = write(path, raw, **kwargs)
            if path.name == "memory.jsonl":
                runtime._closing = True
            return result
        monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", close_after_formal)
        status, result = memory_post(server, row["actor_id"], operation="remember", content="formal partial before native")
        assert status == 200 and not result["ok"] and result["records"] == [], result
        assert result["error_code"] == "LocalControlCoordinatorUnavailable"
        source = sources[-1]
        error = source._native_source_failure
        assert error is errors[-1] and source.lifetime.active is False
        assert source.lease._commit_pins == set()
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        assert domain.long_term.all()[0].content == "formal partial before native"
        with pytest.raises(type(error)) as caught:
            source.perform(lambda: pytest.fail("afterexit首native不得复活"))
        assert caught.value is error


def test_actual_query_flush_failure_is_once_preserves_pending_and_http_is_not_success(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl

    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        actor = row["actor_id"]
        assert memory_post(server, actor, operation="remember", content="needle real search")[1]["ok"]
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        calls, search = [], domain.long_term.search
        def observe(*args, **kwargs):
            calls.append(True)
            return search(*args, **kwargs)
        monkeypatch.setattr(domain.long_term, "search", observe)
        error = OSError("ordinary final flush failure")
        write = jsonl.write_private_text_file_atomic_unlocked
        def fail_flush(path, raw, **kwargs):
            if path == domain.long_term.path:
                raise error
            return write(path, raw, **kwargs)
        monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_flush)
        before = domain.long_term.path.read_bytes()
        status, result = memory_post(server, actor, query="needle")
        assert status == 200 and not result["ok"] and not result["records"]
        assert result["error_code"] == "OSError" and calls == [True]
        assert domain.long_term._pending_access and domain.long_term.path.read_bytes() == before


def test_same_borrow_quota_policy_updates_original_object_and_rejects_bad_or_disabled(tmp_path, monkeypatch):
    import json

    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        actor = row["actor_id"]
        assert memory_post(server, actor, operation="remember", content="needle policy")[1]["ok"]
        holder = server.coordinator_memory_resources
        domain = next(iter(holder._entries.values())).future.result()
        quota = domain.long_term.quota_enforcer
        for max_mb in [0, 1, 3, 1, 0]:
            domain.home.owner_quota_json.write_text(json.dumps({"max_disk_mb": max_mb}), encoding="utf-8")
            status, result = memory_post(server, actor, query="needle")
            assert status == 200 and result["ok"], result
            assert domain.long_term.quota_enforcer is quota and quota.max_bytes == max_mb * 1024 * 1024
            assert quota.policy_available is True
        domain.home.owner_quota_json.write_text("{invalid-json", encoding="utf-8")
        before = domain.long_term.path.read_bytes()
        status, result = memory_post(server, actor, operation="remember", content="cannot save bad policy")
        assert status == 200 and not result["ok"] and not result["records"]
        assert domain.long_term.path.read_bytes() == before
        domain.home.owner_quota_json.write_text('{"max_disk_mb":0}', encoding="utf-8")
        domain.home.owner_memory_policy_json.write_text('{"enabled":false}', encoding="utf-8")
        status, result = memory_post(server, actor, operation="remember", content="cannot save disabled")
        assert status == 200 and not result["ok"] and domain.long_term.path.read_bytes() == before
        assert next(iter(holder._entries.values())).future.result() is domain


def test_same_owner_successful_search_n_plus_one_can_evict_clean_resource(tmp_path, monkeypatch):
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        holder = server.coordinator_memory_resources
        for number in range(holder._capacity + 2):
            actor = "j3-lru-owner-" + str(number)
            status, result = memory_post(server, actor, operation="remember", content="needle " + actor)
            assert status == 200 and result["ok"], result
            status, result = memory_post(server, actor, query="needle")
            assert status == 200 and result["ok"], result
        assert len(holder._entries) == holder._capacity
        assert all(not entry.future.result().long_term._pending_access for entry in holder._entries.values())
        assert server.agent is None


# LLM: 只替换外部embedding客户端factory，原C1配置/身份/语义通道和JSONL算法全部实际执行。
# 函数用途: 为HTTP链提供可计数的无网络provider，不禁用语义召回来伪造成功。
def semantic_provider(server, monkeypatch):
    from agent_py_agent.agent.retrieval import embedding_factory
    from agent_py_agent.tests.test_coordinator_jsonl_write_ports import Embedder

    value = Embedder()
    value.protocol = "openai_compatible"
    server.control_plane.config.memory_semantic_recall = True
    server.control_plane.config.embedding_model_profile = "j3-profile"
    monkeypatch.setattr(embedding_factory, "embedding_client_for_resources", lambda *_args, **_kwargs: value)
    return value


def test_nonempty_original_semantic_http_keeps_identity_ranking_and_paid_count(tmp_path, monkeypatch):
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        embedder = semantic_provider(server, monkeypatch)
        actor = row["actor_id"]
        expected = set()
        for content in ["needle alpha fact", "needle beta fact", "needle gamma fact"]:
            status, result = memory_post(server, actor, operation="remember", content=content)
            assert status == 200 and result["ok"], result
            expected.add(result["records"][0]["entry_id"])
        assert len(embedder.calls) == 3
        status, result = memory_post(server, actor, query="needle")
        assert status == 200 and result["ok"], result
        assert {item["entry_id"] for item in result["records"]} == expected
        assert len(embedder.calls) == 4 and embedder.calls[-1] == ["needle"]
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        assert domain.long_term._embedder is embedder and domain.long_term._vector_store_cache.coordinator_io
        assert domain.long_term._vector_store_cache._identity == domain.long_term._vector_identity
        assert not domain.long_term._pending_access


# LLM: 788f9b847起remember不再写旧LocalStore文本投影；正式写入成功后的新撤权仍须挡在首次付费embedding之前。
# 函数用途: 验证实际HTTP记忆写在正式提交后撤权时零embedding且首个native同对象保留。
def test_actual_http_new_revocation_after_formal_success_blocks_first_paid_embedding(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl

    sources, errors = [], []
    perform = _LocalControlWriteSource.perform
    def observe(source, operation):
        sources.append(source)
        try:
            return perform(source, operation)
        except BaseException as error:
            errors.append(error)
            raise
    monkeypatch.setattr(_LocalControlWriteSource, "perform", observe)
    write = jsonl.write_private_text_file_atomic_unlocked
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, runtime):
        embedder = semantic_provider(server, monkeypatch)
        def close_after_formal(path, raw, **kwargs):
            result = write(path, raw, **kwargs)
            if path.name == "memory.jsonl":
                runtime._closing = True
            return result
        monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", close_after_formal)
        status, result = memory_post(server, row["actor_id"], operation="remember", content="previous formal success")
        assert status == 200 and not result["ok"] and not result["records"], result
        assert result["error_code"] == "LocalControlCoordinatorUnavailable" and not embedder.calls
        source = sources[-1]
        assert errors[-1] is source._native_source_failure
        assert not source.lifetime.active and not source.lease._commit_pins
        with pytest.raises(type(errors[-1])) as caught:
            source.perform(lambda: pytest.fail("first native不得复活"))
        assert caught.value is errors[-1]


def test_actual_http_physical_unknown_keeps_outer_original_and_no_second_paid_call(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    observed, sources = [], []
    execute = client_memory.execute_coordinator_client_memory
    perform = _LocalControlWriteSource.perform
    def observe_source(source, operation):
        sources.append(source)
        return perform(source, operation)
    def observe_outer(*args, **kwargs):
        try:
            return execute(*args, **kwargs)
        except BaseException as error:
            observed.append(error)
            raise
    monkeypatch.setattr(_LocalControlWriteSource, "perform", observe_source)
    monkeypatch.setattr(client_memory, "execute_coordinator_client_memory", observe_outer)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        embedder = semantic_provider(server, monkeypatch)
        error = BoundedCallExitUnconfirmedError("original-paid-call", "physical exit unknown")
        cause = OSError("original outer cause")
        error.__cause__ = cause
        embedder.error = error
        status, result = memory_post(server, row["actor_id"], operation="remember", content="formal before physical unknown")
        assert status == 200 and not result["ok"] and result["records"] == [], result
        assert result["error_code"] == "BoundedCallExitUnconfirmedError"
        assert observed == [error] and observed[0].__cause__ is cause and len(embedder.calls) == 1
        source = sources[-1]
        assert not source.lifetime.active and not source.lease._commit_pins
        # physical outer不冒充source producer首槽，原primary已完整穿出scope再HTTP安全投影。
        assert source._native_source_failure is None


def test_actual_http_zero_to_positive_quota_enforces_original_cached_enforcer(tmp_path, monkeypatch):
    import json

    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        actor = row["actor_id"]
        assert memory_post(server, actor, operation="remember", content="before quota")[1]["ok"]
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        quota = domain.long_term.quota_enforcer
        assert quota.max_bytes == 0
        (domain.home.owner_home_dir / "quota-fixture.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        domain.home.owner_quota_json.write_text(json.dumps({"max_disk_mb": 1}), encoding="utf-8")
        before = domain.long_term.path.read_bytes()
        status, result = memory_post(server, actor, operation="remember", content="cannot borrow old unlimited")
        assert status == 200 and not result["ok"] and result["records"] == [], result
        assert domain.long_term.quota_enforcer is quota and quota.max_bytes == 1024 * 1024
        assert domain.long_term.path.read_bytes() == before
