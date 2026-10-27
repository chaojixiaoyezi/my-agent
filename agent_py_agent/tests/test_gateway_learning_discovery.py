"""原FIFO和无FIFO自主维护同一registry来源；显式临时home，不调用真实模型。"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    BackgroundSourceChanged,
    freeze_source,
    read_source,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_skill_learning import _ctx, _runtime


# LLM: 此对象仅调用只读发现method，不代替真实C/Root/claim；consumer另由完整claim用例验证。
# 函数用途: 从原临时owner布局提取学习refs，禁止构造业务Agent或补FIFO。
def learning_sources(context, *, mode="autonomous", enabled=True):
    discovery = SimpleNamespace(
        control=SimpleNamespace(
            home_root=context.home.root,
            config=SimpleNamespace(
                memory_curator_enabled=False,
                enable_self_learning=enabled,
                self_learning_maintenance_mode=mode,
                owner_maintenance_scan_interval_seconds=0,
            ),
        ),
        _source_cursors={},
    )
    target = SimpleNamespace(
        identity=OwnerIdentity.local_main(), home_dir=context.home.owner_home_dir
    )
    return [
        ref
        for ref in CoordinatorBackgroundRequests._owner_sources(discovery, target)
        if ref["kind"] == "learning"
    ]


# LLM: 原store锁和writer生成真实registry，只有时钟事实被设为到期；不创建虚构学习request。
# 函数用途: 准备一个到期但FIFO为空的原登记表。
def due_registry(context):
    with context.store.state_lock():
        context.store.save_registry(context.store.load_registry())
    os.utime(context.store.registry_path, (1, 1))
    return context.store.registry_path


def test_original_pending_requests_remains_fifo_source(tmp_path):
    context = _runtime(tmp_path)
    context.service.enqueue_from_finalize(_ctx("first"))
    context.service.enqueue_from_finalize(_ctx("second"))
    refs = learning_sources(context)
    assert len(refs) == 1
    assert (
        context.home.owner_home_dir / refs[0]["source_ref"] == context.store.pending_requests()[0]
    )
    assert read_source(context.home.root, refs[0])["request_key"] == refs[0]["source_id"]


@pytest.mark.parametrize(
    "mode,enabled,due,expected",
    [
        ("autonomous", True, True, 1),
        ("observe", True, True, 0),
        ("autonomous", False, True, 0),
        ("autonomous", True, False, 0),
    ],
)
# LLM: 到期事实来自registry.last_maintenance_at；mtime保留给原运输代次测试，不作为维护时钟。
# 函数用途: 覆盖原策略开关与冷热维护来源，不创建虚构FIFO。
def test_registry_maintenance_is_discovered_without_synthetic_fifo(
    tmp_path, mode, enabled, due, expected
):
    context = _runtime(tmp_path)
    path = due_registry(context)
    if not due:
        with context.store.state_lock():
            context.store.save_registry(replace(context.store.load_registry(), last_maintenance_at=time.time()))
    before = path.read_bytes()
    refs = learning_sources(context, mode=mode, enabled=enabled)
    assert len(refs) == expected
    assert context.store.pending_requests() == [] and path.read_bytes() == before
    if refs:
        assert refs[0]["source_ref"] == "data/skill_learning/registry.json"
        assert (
            read_source(context.home.root, refs[0])["schema_version"]
            == "my-agent.skill-learning-registry.v1"
        )


@pytest.mark.parametrize("change", ["rewrite_same_bytes", "replace_bytes", "removed"])
def test_registry_transport_rejects_new_generation_not_new_fifo(tmp_path, change):
    context = _runtime(tmp_path)
    path = due_registry(context)
    ref = freeze_source(context.home.root, OwnerIdentity.local_main(), path, "learning")
    if change == "rewrite_same_bytes":
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    elif change == "replace_bytes":
        path.write_text(path.read_text() + " ")
    else:
        path.unlink()
    with pytest.raises(BackgroundSourceChanged):
        read_source(context.home.root, ref)
    assert context.store.pending_requests() == []


def test_maintenance_only_never_consumes_later_fifo_or_calls_model(tmp_path):
    context = _runtime(tmp_path)
    due_registry(context)
    service = SkillLearningService(
        context.store,
        replace(context.service.settings, maintenance_mode="autonomous"),
        context.service.runtime,
    )
    assert service.has_pending()
    service.enqueue_from_finalize(_ctx("later"))
    [path] = context.store.pending_requests()
    before = path.read_bytes()
    result = service.run_pending(maintenance_only=True)
    assert result.status == "idle" and context.backend.prompts == []
    assert path.read_bytes() == before and context.store.pending_requests() == [path]
    assert context.store.registry_path.stat().st_mtime > time.time() - 60


def test_registry_source_rejects_links_and_corruption(tmp_path):
    context = _runtime(tmp_path)
    path = due_registry(context)
    original = path.read_bytes()
    path.unlink()
    other = path.with_name("other.json")
    other.write_bytes(original)
    path.symlink_to(other)
    with pytest.raises((OSError, ValueError)):
        freeze_source(context.home.root, OwnerIdentity.local_main(), path, "learning")
    path.unlink()
    path.write_text('{"schema_version":"wrong"}')
    with pytest.raises(ValueError):
        freeze_source(context.home.root, OwnerIdentity.local_main(), path, "learning")


@pytest.mark.parametrize(
    "fault", ["normal", "later_fifo", "changed_registry", "closed", "mode_changed"]
)
def test_actual_c_claim_consumes_only_registry_maintenance_source(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        reservation_file,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import enqueue_source
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.request_worker import (
        AdmissionLimits,
        _process_claimed_gateway_request_path,
    )
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch, source_kind="learning") as (
        agent,
        service,
        backend,
        gate,
        first,
        close,
    ):
        backend.release.set()
        with coordinator_execution_scope(gate, first, agent):
            assert _process_claimed_gateway_request_path(
                agent, gate.paths, first.path, "original-C", claim=first
            )
        assert service.store.pending_requests() == [] and backend.calls == 1
        assert len(list_learning_proposals(service.store)) == 1
        assert list_learning_proposals(service.store)[0]["status"] == "pending"
        gate.release_settled()
        service.settings = replace(service.settings, maintenance_mode="autonomous")
        agent.config.self_learning_maintenance_mode = "autonomous"
        path = service.store.registry_path
        os.utime(path, (1, 1))
        ref = freeze_source(agent.home_paths.root, OwnerIdentity.local_main(), path, "learning")
        queued, _ = enqueue_source(gate.paths, agent.home_paths.root, ref)
        request = read_json_file(queued)
        decision = gate.admit(
            queued,
            (OwnerIdentity.local_main(), session_scope_for_request(request)),
            AdmissionLimits(8, 8),
        )
        assert decision["status"] == "admitted"
        claim = claim_request(
            gate.paths, queued, coordinator_snapshot=freeze_coordinator_claim(gate, decision)
        )
        assert claim is not None
        if fault == "later_fifo":
            # 真实入队用当前原C短写端口；新FIFO不属于registry维护源，不能被本claim消费。
            from agent_py_agent.agent.gateway_parts.request_transactions import (
                request_transaction_ports_scope,
            )

            with (
                coordinator_execution_scope(gate, claim, agent),
                request_transaction_ports_scope(agent),
            ):
                assert service.enqueue_from_finalize(_ctx("later-request")) == "queued"
        if fault == "changed_registry":
            path.write_text(path.read_text() + " ")
        if fault == "closed":

            def revoke_before_write():
                close()
                return {"protection_complete": False}

            service.runtime = replace(service.runtime, protection_provider=revoke_before_write)
        if fault == "mode_changed":
            service.settings = replace(service.settings, maintenance_mode="observe")
        before = {p: p.read_bytes() for p in service.store.pending_requests()}
        proposals = list_learning_proposals(service.store)
        p3 = reservation_file(gate.paths, claim.request_id)
        p3_before = p3.read_bytes()
        if fault == "closed":
            from agent_py_agent.agent.common.request_execution_ports import (
                RequestExecutionOutcomeUncertainError,
            )

            with (
                coordinator_execution_scope(gate, claim, agent),
                pytest.raises(RequestExecutionOutcomeUncertainError) as raised,
            ):
                _process_claimed_gateway_request_path(
                    agent, gate.paths, claim.path, "original-C", claim=claim
                )
            assert raised.value.operation_entered is False
            actual = read_json_file(claim.path)
            assert actual["status"] == "processing"
            assert actual.get("execution_outcome", {}).get("status") in (None, "unknown")
            assert not (gate.paths.terminal / claim.path.name).exists()
            assert path.stat().st_mtime == 1 and backend.calls == 1
            assert list_learning_proposals(service.store) == proposals
            assert p3.read_bytes() == p3_before
            return
        with coordinator_execution_scope(gate, claim, agent):
            assert _process_claimed_gateway_request_path(
                agent, gate.paths, claim.path, "original-C", claim=claim
            )
        result = read_json_file(gate.paths.terminal / claim.path.name)["terminal_response"][
            "background_result"
        ]
        assert result["source_kind"] == "learning" and result["source_id"] == ref["source_id"]
        assert result["outcome"] == ("source_changed" if fault == "changed_registry" else "handled")
        assert backend.calls == 1 and list_learning_proposals(service.store) == proposals
        assert {p: p.read_bytes() for p in service.store.pending_requests()} == before
        assert p3.read_bytes() == p3_before
        if fault == "mode_changed":
            assert result["status"] == "disabled" and path.stat().st_mtime == 1
        elif fault != "changed_registry":
            assert path.stat().st_mtime > time.time() - 60


@pytest.mark.parametrize("age,expected", [(86399.999, False), (86400, True), (86400.001, True)])
def test_shared_due_boundary_is_exact_and_recomputed(age, expected):
    from agent_py_agent.agent.capability.skill_learning_maintenance import learning_maintenance_due

    assert learning_maintenance_due(100000.0 - age, "autonomous", 100000.0) is expected
    assert not learning_maintenance_due(100000.0 - age, "observe", 100000.0)
    assert learning_maintenance_due(100000.0 - age, "autonomous", 100002.0)


def test_same_registry_bytes_new_mtime_get_new_transport_identity(tmp_path):
    context = _runtime(tmp_path)
    path = due_registry(context)
    first = freeze_source(context.home.root, OwnerIdentity.local_main(), path, "learning")
    os.utime(path, (-2, -2))
    second = freeze_source(context.home.root, OwnerIdentity.local_main(), path, "learning")
    assert first["source_sha256"] == second["source_sha256"]
    assert first["source_id"] != second["source_id"] and second["input_revision"] == "-2000000000"
    assert read_source(context.home.root, second)


@pytest.mark.parametrize("fault", ["budget", "during_read"])
def test_registry_capture_requires_complete_stable_same_fd(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.gateway_parts.multiprocess_background_sources import (
        learning_registry_snapshot,
    )

    context = _runtime(tmp_path)
    path = due_registry(context)
    before = path.read_bytes()
    if fault == "during_read":
        original_open, original_stat = nofollow_fs.open_readonly_file_beneath, os.fstat
        observed = []

        def open_original(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            observed[:] = [descriptor, 0]
            return descriptor

        def changed_stat(descriptor):
            actual = original_stat(descriptor)
            if observed and descriptor == observed[0]:
                observed[1] += 1
                if observed[1] == 2:
                    return SimpleNamespace(
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_size=actual.st_size,
                        st_mtime_ns=actual.st_mtime_ns + 1,
                        st_ctime_ns=actual.st_ctime_ns,
                    )
            return actual

        monkeypatch.setattr(nofollow_fs, "open_readonly_file_beneath", open_original)
        monkeypatch.setattr(os, "fstat", changed_stat)
    with pytest.raises(ValueError):
        learning_registry_snapshot(
            context.home.owner_home_dir, 1 if fault == "budget" else 1_048_576
        )
    assert path.read_bytes() == before and context.store.pending_requests() == []
