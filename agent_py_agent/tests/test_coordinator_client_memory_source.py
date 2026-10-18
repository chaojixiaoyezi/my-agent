"""真实HTTP/work/G1/C组件；临时producer故障与已接通的原领域入口分别验证。"""

import copy
import threading
from contextvars import copy_context

import pytest

from agent_py_agent.tests.test_coordinator_model_menu import post_model
from agent_py_agent.tests.test_coordinator_private_credential_transactions import original_case


def body():
    return {"operation": "search", "conversation_id": "memory-source-session", "query": "temporary"}


# LLM: 仅临时替换handler分派到待集成固定source组件；真实G1/原HTTP work/lease/controller全保，不调用不安全的领域writer。
# 函数用途: 在实际服务线程核组件并保存原异常对象，不能作为search/remember已支持的证据。
def install(monkeypatch, exercise):
    from agent_py_agent.agent.gateway_parts import http_service
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )

    observed = []

    def handle(handler, server):
        from agent_py_agent.agent.gateway_parts.http_handlers import require_trusted_source

        if require_trusted_source(handler):
            return
        data = handler._read_json()
        try:
            with _LocalControlWriteSource.http_client_memory_scope(handler, server, data) as source:
                observed.append(source)
                source.bind_client_memory_owner(source.http_origin.home)
                exercise(source)
            handler._send_json(200, {"ok": True})
        except BaseException as error:
            observed.append(error)
            handler._send_json(503, {"error_type": type(error).__name__})

    monkeypatch.setattr(http_service, "handle_client_memory", handle)
    return observed


@pytest.mark.parametrize("healthy", [False, True])
def test_actual_source_current_owner_no_management_receipt_and_no_held_source(
    tmp_path, monkeypatch, healthy
):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    calls = []

    def exercise(source):
        assert not hasattr(source, "receipt") and source.deadline is None
        assert source.http_origin.owner == source.owner
        assert source.resource_home is source.http_origin.home
        assert source.perform(lambda: calls.append(True) or 7) == 7
        with pytest.raises(Exception, match="no control receipt"):
            source.commit_control_receipt({})

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (agent, _path, row, _body, server, runtime):
        if healthy:
            server.agent = agent  # 已存在真实Agent引用；source不得据此改owner或另建实例。
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 200 and result["ok"] and calls == [True], (result, observed)
        source = observed[0]
        assert not list((server.paths.root / "control_operations").glob("*.json"))
        assert not source.lease._commit_pins
        assert all(
            value is not source for value in vars(server.coordinator_memory_resources).values()
        )
        with pytest.raises(LocalControlCoordinatorUnavailable):
            source.perform(lambda: pytest.fail("已退出source不可写"))
        assert runtime is server.drain_runtime


@pytest.mark.parametrize("change", ["body", "holder", "lifetime", "middleware", "agent"])
def test_actual_source_first_native_persists_after_condition_restored(
    tmp_path, monkeypatch, change
):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    errors = []

    def exercise(source):
        facts = source.http_origin
        if change == "body":
            old = facts.body["query"]
            facts.body["query"] = "changed"

            def restore():
                return facts.body.__setitem__("query", old)
        elif change == "holder":
            old = facts.server.coordinator_memory_resources
            facts.server.coordinator_memory_resources = None

            def restore():
                return setattr(facts.server, "coordinator_memory_resources", old)
        elif change == "lifetime":
            source.lifetime.close()

            def restore():
                return None
        elif change == "middleware":
            old = facts.handler._auth_middleware
            facts.handler._auth_middleware = None

            def restore():
                return setattr(facts.handler, "_auth_middleware", old)
        else:
            old = facts.server.agent
            facts.server.agent = object()

            def restore():
                return setattr(facts.server, "agent", old)

        try:
            with pytest.raises(LocalControlCoordinatorUnavailable) as first:
                source.perform(lambda: pytest.fail("失效后不得进writer"))
        finally:
            restore()
        with pytest.raises(LocalControlCoordinatorUnavailable) as second:
            source.perform(lambda: pytest.fail("恢复条件不得洗掉首失败"))
        assert first.value is second.value is source._native_source_failure
        errors.append(first.value)

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 503 and observed[-1] is errors[0], (result, observed)


def test_copy_thread_and_copied_context_cannot_poison_original_source(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    def exercise(source):
        with pytest.raises(LocalControlCoordinatorUnavailable):
            copy.copy(source).perform(lambda: pytest.fail("copy不得写"))
        errors = []

        def call():
            try:
                source.perform(lambda: pytest.fail("跨Thread不得写"))
            except BaseException as error:
                errors.append(error)

        for target in (call, lambda: copy_context().run(call)):
            thread = threading.Thread(target=target)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
        assert len(errors) == 2 and all(
            isinstance(error, LocalControlCoordinatorUnavailable) for error in errors
        )
        assert source._native_source_failure is None
        assert source.perform(lambda: 19) == 19

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 200, (result, observed)


def test_current_owner_only_even_admin_target_body_and_foreign_bind(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    owners = []

    def exercise(source):
        owners.append(source.owner)
        foreign = home_paths_with_owner(
            home_paths(source.context.home_root),
            resolve_owner_home(
                source.context.home_root, OwnerIdentity.provider_user("feishu", "not-the-actor")
            ),
        )
        with pytest.raises(LocalControlCoordinatorUnavailable):
            source.bind_client_memory_owner(foreign)
        assert not foreign.owner_home_dir.exists()
        assert source.perform(lambda: True)

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        data = {
            **body(),
            "owner_id": "not-the-actor",
            "target": "not-the-actor",
            "user_id": "not-the-actor",
        }
        status, result = post_model(server, data, route="/client/memory", actor=row["actor_id"])
        assert status == 200 and owners[0].owner_id == row["actor_id"], (result, observed)


def test_close_admission_and_actual_outer_preserve_original_error(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    outer = OSError("synthetic outer")

    def exercise(source):
        source.lifetime.close()
        try:
            source.perform(lambda: pytest.fail("closed不得写"))
        except LocalControlCoordinatorUnavailable as native:
            raise outer from native

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 503 and observed[-1] is outer
        assert (
            outer.__cause__ is observed[0]._native_source_failure
            and outer.__context__ is outer.__cause__
        )
        assert result["error_type"] == "OSError"


@pytest.mark.parametrize("operation", ["search", "remember"])
def test_real_endpoint_uses_current_owner_after_c3_closure(tmp_path, monkeypatch, operation):
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        assert server.coordinator_memory_resources._entries == {}
        status, result = post_model(
            server,
            {**body(), "operation": operation, "content": "source current owner fact"},
            route="/client/memory",
            actor=row["actor_id"],
        )
        assert status == 200 and result["ok"], result
        assert len(server.coordinator_memory_resources._entries) == 1
        domain = next(iter(server.coordinator_memory_resources._entries.values())).future.result()
        assert domain.long_term.coordinator_io and server.agent is None
        if operation == "remember":
            assert domain.long_term.all()[0].content == "source current owner fact"
        else:
            assert result["records"] == []


def test_missing_credential_never_enters_client_source(tmp_path, monkeypatch):
    observed = install(monkeypatch, lambda _source: pytest.fail("G1未通过不可捕获source"))
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(
            server, body(), route="/client/memory", actor=row["actor_id"], credential=False
        )
        assert status == 403 and not observed, result


@pytest.mark.parametrize("after_write_close", [False, True])
def test_actual_http_snapshot_flush_reuses_original_memory_but_never_old_source(
    tmp_path, monkeypatch, after_write_close
):
    from types import SimpleNamespace

    from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.tests import test_coordinator_private_credential_transactions as seed

    original_card, memories, sources, writes = seed._card, [], [], []

    def prepared(*args):
        result = original_card(*args)
        memory = result[0].memory
        record = memory.add("fact", "temporary snapshot fact")
        memories.append((memory, record))
        return result

    monkeypatch.setattr(seed, "_card", prepared)

    def exercise(source):
        memory, record = memories[0]
        assert memory.path == source.http_origin.home.owner_memory_long_term_jsonl
        sources.append(source)
        value = SimpleNamespace(long_term=memory)  # 纯holder引用束，不含身份/权限/回调。
        with source.http_origin.holder.borrow_current_owner(
            source.resource_home, lambda: value
        ) as actual:
            assert actual.long_term is memory
            memory._pending_access[record.entry_id] = 3.0 + len(sources)
            original_write = jsonl.write_private_text_file_atomic_unlocked

            def write(path, text, **options):
                writes.append(path)
                result = original_write(path, text, **options)
                if after_write_close:
                    source.lifetime.close()
                return result

            with monkeypatch.context() as local:
                local.setattr(jsonl, "write_private_text_file_atomic_unlocked", write)
                with global_write_commit_scope(source.perform):
                    assert memory.flush_access_events_from_source() == 1
            assert memory._pending_access == {}
            if after_write_close:
                source._require_current()

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        for _ in range(2):
            status, result = post_model(
                server, body(), route="/client/memory", actor=row["actor_id"]
            )
            assert status == (503 if after_write_close else 200), (result, observed)
        assert len(writes) == 2 and len(sources) == 2 and sources[0] is not sources[1]
        assert not list((server.paths.root / "control_operations").glob("*.json"))
        with pytest.raises(LocalControlCoordinatorUnavailable):
            sources[0].perform(lambda: pytest.fail("旧source不可借新HTTP续权"))


def test_client_runtime_gate_busy_is_try_and_does_not_poison_source(tmp_path, monkeypatch):
    def exercise(source):
        held, release = threading.Event(), threading.Event()

        def hold():
            with source.http_origin.runtime._gate:
                held.set()
                assert release.wait(3)

        thread = threading.Thread(target=hold)
        thread.start()
        assert held.wait(3)
        try:
            with pytest.raises(BlockingIOError):
                source.perform(lambda: pytest.fail("busy不得进writer"))
            assert source._native_source_failure is None
        finally:
            release.set()
            thread.join(3)
        assert not thread.is_alive() and source.perform(lambda: 11) == 11

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 200, (result, observed)


def test_client_native_global_failure_same_object_and_callback_error_not_slot(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
    from agent_py_agent.agent.gateway_parts import multiprocess_http_transport as transport
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    native = GlobalWriteInWorkerError("synthetic resource")

    def exercise(source):
        callback_error = LocalControlCoordinatorUnavailable(
            "callback-created error is not a source producer"
        )

        def fail_callback():
            raise callback_error

        with pytest.raises(LocalControlCoordinatorUnavailable) as callback:
            source.perform(fail_callback)
        assert callback.value is callback_error and source._native_source_failure is None
        original = transport.require_global_writer

        def reject(_resource):
            raise native

        with monkeypatch.context() as local:
            local.setattr(transport, "require_global_writer", reject)
            with pytest.raises(GlobalWriteInWorkerError) as caught:
                source.perform(lambda: pytest.fail("受限native不得进writer"))
        assert caught.value is native and transport.require_global_writer is original
        with pytest.raises(GlobalWriteInWorkerError) as retained:
            source.perform(lambda: pytest.fail("恢复不能洗首native"))
        assert retained.value is source._native_source_failure is native

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        status, result = post_model(server, body(), route="/client/memory", actor=row["actor_id"])
        assert status == 503 and observed[-1] is native, (result, observed)
