# LLM: 实际C/P3/RuntimeDB和common端口组件测试；P2进程表是合成停止输入，不能当作真实物理P2/Gateway供应商验收。
# 模块用途: 核原processing恢复的停止/政策/CAS/共享失效边界与Count0原语义。
import copy
import json
import threading
import time
from contextlib import contextmanager
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
    gateway_short_operation,
    request_execution_ports_scope,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import multiprocess_coordinator as mc
from agent_py_agent.agent.gateway_parts import multiprocess_processing_recovery as pr
from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as rt
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    _read_reservation,
    reservation_file,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.recovery import (
    _ensure_recovery_dirs,
    _RecoveryContext,
    recover_gateway_processing_requests_report,
)
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.support.multiprocess_processing_cases import (
    current_gate,
    register,
    setup_case,
)


# LLM: 持久准备都在Count0实际API完成，原C只持冷元数据/原P2门；所有home路径隔离，不构造业务Agent或模型。
# 函数用途: 为已停止旧域的同一原请求准备真实Runtime绑定与会话账。
@contextmanager
def recovery_case(
    tmp_path, monkeypatch, *, with_input=False, canonical_config=False, ambiguous_origin=False
):
    case = setup_case(
        tmp_path, previous_generations=(("other-old-c", 1),) if ambiguous_origin else ()
    )
    paths, ledger, _scope, original, path, ownership, agent = case
    _ensure_recovery_dirs(paths)
    from agent_py_agent.agent.core import configure_canonical_background_contracts

    configure_canonical_background_contracts()
    agent.home_paths = home_paths(tmp_path / "home")
    owner_home = resolve_owner_home(agent.home_paths.root, ownership[0])
    assert owner_home.home_dir.is_relative_to(tmp_path)
    owner_home.home_dir.mkdir(parents=True)
    agent.root = tmp_path / "project"
    agent.config.conversation_workspace = str(owner_home.workspace_dir / "conversations")
    if canonical_config:
        from agent_py_agent.agent.settings import AgentConfig
        from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
        from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

        agent.config = AgentConfig(my_agent_home=str(agent.home_paths.root))
        scoped = home_paths_with_owner(agent.home_paths, owner_home)
        agent.config.conversation_workspace = str(
            runtime_paths_for_agent(agent.config, owner_home.home_dir, scoped)[
                "conversation_workspace"
            ]
        )
    agent.config.gateway_request_max_attempts = 3
    agent.config.gateway_processing_timeout_seconds = 10
    store = ConversationStore(agent.config.conversation_workspace)
    thread = store.threads.get_or_create(
        {
            "channel": "synthetic",
            "channel_conversation_id": "test-chat",
            "channel_user_id": "p2-user",
            "canonical_user_id": "synthetic:p2-user",
            "owner_id": owner_home.owner_id,
            "owner_home": str(owner_home.home_dir),
        }
    )
    repo = RuntimeRepository(runtime_db_path(owner_home.home_dir))
    binding = repo.record_run_creation(
        owner_id=owner_home.owner_id, run_id="original-run", role="main", thread_id=thread.thread_id
    )
    claim = claim_request(paths, path)
    assert claim is not None
    original = dict(
        original, execution_attempt_id=claim.execution_attempt_id, lease_epoch=claim.lease_epoch
    )
    register(ledger, original)
    if ambiguous_origin:
        from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
        from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition

        other = ExecutorLedger(ledger.root, WorkerPartition(0, 1, "other-old-c"))
        register(other, dict(original, reservation_id="second-executor", c_run_id="other-old-c"))
    writer = GatewayTaskBindingWriter(
        claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id
    )
    assert writer.bind_conversation_claim(thread.thread_id)
    assert writer.bind_runtime_authority(
        {**binding, "run_id": "original-run", "invocation_run_id": claim.request_id}
    )
    assert repo.agent_run_recovery_block_for_run_id("original-run") is None
    if with_input:
        prepare_bound_input(paths, store, claim.request_id, thread.thread_id, owner_home.owner_id)
    if canonical_config:
        from agent_py_agent.agent.user_space.runtime_paths import (
            apply_runtime_paths_to_config,
            resolve_runtime_paths_for_agent,
        )

        agent.config.conversation_workspace = ""
        agent.runtime_path_resolution = resolve_runtime_paths_for_agent(
            agent.config, agent.home_paths.owner_home_dir, agent.home_paths
        )
        apply_runtime_paths_to_config(agent.config, agent.runtime_path_resolution)
    monkeypatch.setattr(
        p2,
        "observe_processes",
        lambda: {"platform": "darwin", "host_id": "host-one", "rows": {}, "failure": ""},
    )
    monkeypatch.setattr(rt, "start_takeover_rechecks", lambda _control: None)
    with mc.coordinator_guard(paths.root, 1):
        control = rt.prepare_coordinator_takeover(paths, 1, "new-c")
        gate = current_gate(case)
        rt.bind_admission_takeover(gate, control)
        context = _RecoveryContext(
            time.time() + 100, 3, 10, False, agent, None, admission_gate=gate
        )
        yield SimpleNamespace(
            case=case,
            gate=gate,
            context=context,
            claim=claim,
            repo=repo,
            binding=binding,
            store=store,
        )


# LLM: 原guidance与InputReceipt producer仅准备未消费输入，Count0阶段落账，不预造queued/模型成功。
# 函数用途: 保存同owner/peer/turn的真实非空待结算回执。
def prepare_bound_input(paths, store, turn_id, thread_id, owner_id):
    from agent_py_agent.agent.gateway_parts.input_delivery_service import (
        bind_gateway_input_active_locked,
        gateway_input_transition,
        load_or_prepare_gateway_input_locked,
    )

    input_id, key = "input-original", "input-key-original"
    metadata = {
        "expected_turn_id": turn_id,
        "gateway_input_request_id": input_id,
        "client_input_digest": key,
        "channel_message_id": key,
        "channel": "synthetic",
        "user_id": "p2-user",
        "thread_id": thread_id,
    }
    store.guidance.append_once(
        {
            "target_type": "request",
            "target_id": turn_id,
            "sender": owner_id,
            "message": "真实原输入合同测试",
            "metadata": metadata,
        },
        dedupe_key=key,
    )
    prepared = {
        "id": input_id,
        "request_id": input_id,
        "kind": "ask",
        "goal": "真实原输入合同测试",
        "user_id": "p2-user",
        "metadata": metadata,
        "conversation": {"channel": "synthetic", "channel_conversation_id": "test-chat"},
    }
    with gateway_input_transition(paths, input_id):
        receipt, created = load_or_prepare_gateway_input_locked(
            paths,
            request_id=input_id,
            client_input_digest=key,
            client_message_id=key,
            guidance_dedupe_key=key,
            prepared_request=prepared,
        )
        assert created
        bind_gateway_input_active_locked(
            paths, receipt, target_turn_id=turn_id, guidance_dedupe_key=key
        )


@pytest.mark.parametrize("disposition", ["requeue", "failure"])
def test_original_stopped_source_reads_real_owner_null_then_commits(
    tmp_path, monkeypatch, disposition
):
    with recovery_case(tmp_path, monkeypatch) as c:
        original = read_json_file(c.claim.path)
        if disposition == "failure":
            original["processing_failure_count"] = c.context.max_attempts - 1
            write_json_file_atomic(c.claim.path, original)
        p3 = _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.load_errors == [], report.load_errors
        assert report.summary["requeued" if disposition == "requeue" else "failed"] == 1
        assert not c.claim.path.exists()
        after = _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))
        assert after["state"] == "occupied" and after["reservation_id"] == p3["reservation_id"]
        assert after["route"]["c_run_id"] == "new-c"
        if disposition == "failure":
            terminal = read_json_file(c.gate.paths.terminal / c.claim.path.name)
            assert (
                terminal["status"] == "failed"
                and terminal["error_code"] == "GATEWAY_PROCESSING_TIMEOUT"
            )
            assert terminal["runtime_authority"] == original["runtime_authority"]
            assert c.gate.release_settled()["released"] == 1
        else:
            pending = read_json_file(c.gate.paths.inbox / c.claim.path.name)
            assert (
                pending["status"] == "pending"
                and pending["runtime_authority"] == original["runtime_authority"]
            )
            assert (
                pending["active_turn_recovery"]["dead_execution_attempt_id"]
                == c.claim.execution_attempt_id
            )
            assert c.gate.release_settled()["released"] == 0


@pytest.mark.parametrize(
    "drift", ["missing_binding", "missing_db", "runtime_unknown", "wrong_owner", "wrong_attempt"]
)
def test_policy_refusal_after_p3_publication_preserves_processing_occupied(
    tmp_path, monkeypatch, drift
):
    with recovery_case(tmp_path, monkeypatch) as c:
        original = read_json_file(c.claim.path)
        if drift == "missing_binding":
            original.pop("runtime_authority")
            write_json_file_atomic(c.claim.path, original)
        elif drift == "missing_db":
            c.repo.db_path.unlink()
        else:
            with c.repo.transaction() as db:
                if drift == "runtime_unknown":
                    db.execute(
                        "UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",
                        (c.binding["attempt_id"],),
                    )
                elif drift == "wrong_owner":
                    db.execute(
                        "UPDATE tasks SET owner_id='providers/synthetic/users/other' WHERE task_id=?",
                        (c.binding["task_id"],),
                    )
                else:
                    db.execute(
                        "UPDATE agent_runs SET current_attempt_id='other' WHERE agent_run_id=?",
                        (c.binding["agent_run_id"],),
                    )
        p3 = _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.load_errors and report.summary["requeued"] == report.summary["failed"] == 0
        assert c.claim.path.exists() and not (c.gate.paths.terminal / c.claim.path.name).exists()
        after = read_json_file(c.claim.path)
        expected = copy.deepcopy(original)
        expected["route"]["c_run_id"] = "new-c"
        assert after == expected
        reserved = _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))
        assert (
            reserved["reservation_id"] == p3["reservation_id"] and reserved["state"] == "occupied"
        )
        assert reserved["route"]["c_run_id"] == "new-c"
        assert c.gate.release_settled()["released"] == 0


@pytest.mark.parametrize(
    "invalid", ["copy", "thread", "fork", "closed", "wrong_payload", "constructed"]
)
def test_recovery_metadata_source_canonical_cell_rejects_invalid_lifetime(
    tmp_path, monkeypatch, invalid
):
    with recovery_case(tmp_path, monkeypatch) as c:
        saved, effects = [], []
        original = pr._require_runtime_policy
        monkeypatch.setattr(pr, "_require_runtime_policy", original)
        payload = read_json_file(c.claim.path)

        def action(_paths, _path, _payload, _context):
            source = pr.current_processing_recovery()
            saved.append((source, copy_context()))
            assert (
                gateway_short_operation("metadata_probe", lambda: effects.append("valid")) is None
            )
            if invalid == "closed":
                return ""
            rejected = []
            if invalid == "copy":
                candidate = copy.copy(source)
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    with request_execution_ports_scope(candidate):
                        effects.append("copy")
            elif invalid == "thread":

                def worker():
                    try:
                        source.require_writer("metadata_probe")
                    except RequestExecutionOutcomeUncertainError:
                        rejected.append(True)

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(5)
                assert not thread.is_alive() and rejected == [True]
            elif invalid == "fork":
                with monkeypatch.context() as m:
                    m.setattr(pr.os, "getpid", lambda: source._pid + 1)
                    with pytest.raises(RequestExecutionOutcomeUncertainError):
                        source.require_writer("metadata_probe")
            elif invalid == "wrong_payload":
                source.payload["execution_attempt_id"] = "other"
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    source.require_writer("metadata_probe")
            else:
                candidate = pr.OriginalProcessingRecoverySource(
                    source.gate, source.controller, source.authority, source.payload, c.case[5][0]
                )
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    with request_execution_ports_scope(candidate):
                        effects.append("constructed")
            return ""

        assert (
            pr.recover_stale_processing(c.gate.paths, c.claim.path, payload, c.context, action)
            == ""
        )
        assert effects == ["valid"]
        source, inherited = saved[0]
        assert pr.current_processing_recovery() is None
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            inherited.run(source.require_writer, "metadata_probe")
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            inherited.run(gateway_short_operation, "metadata_probe", lambda: effects.append("late"))
        assert effects == ["valid"]


def test_default_recovery_source_port_has_no_added_io(tmp_path, monkeypatch):
    context = _RecoveryContext(time.time(), 3, 10, False, None, None)
    monkeypatch.setattr(
        pr, "_frozen_ownership", lambda *_args: pytest.fail("Count0 must not read route")
    )
    monkeypatch.setattr(
        pr, "RuntimeRepository", lambda *_a, **_k: pytest.fail("Count0 must not query RuntimeDB")
    )
    result = pr.recover_stale_processing(None, None, {}, context, lambda *_args: "original")
    assert result == "original" and not list(tmp_path.iterdir())


def test_cold_missing_gate_keeps_then_real_dispatcher_original_gate_recovers(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.cli.gateway_loops import _RequestDispatcher
    from agent_py_agent.cli.models import GatewayRunContext

    with recovery_case(tmp_path, monkeypatch) as c:
        original = read_json_file(c.claim.path)
        original["lease_heartbeat_at"] = time.time() - 100
        write_json_file_atomic(c.claim.path, original)
        before = (
            c.claim.path.read_bytes(),
            reservation_file(c.gate.paths, c.claim.request_id).read_bytes(),
        )
        blocked = recover_gateway_processing_requests_report(
            c.gate.paths, params=replace(c.context, admission_gate=None)
        )
        assert blocked.load_errors and blocked.summary["requeued"] == blocked.summary["failed"] == 0
        assert before == (
            c.claim.path.read_bytes(),
            reservation_file(c.gate.paths, c.claim.request_id).read_bytes(),
        )
        context = GatewayRunContext(
            c.case[6], c.gate.paths, tmp_path / "config.yaml", coordinator_worker_count=1
        )
        dispatcher = _RequestDispatcher(context, c.gate.paths)
        try:
            assert dispatcher._admission_gate._runtime_takeover is c.gate._runtime_takeover
            dispatcher._recover_once()
            assert not c.claim.path.exists()
            assert read_json_file(c.gate.paths.inbox / c.claim.path.name)["status"] == "pending"
            assert (
                _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))["state"]
                == "occupied"
            )
        finally:
            dispatcher._executor.shutdown(wait=True)


def test_readonly_runtime_policy_never_initializes_schema_or_db(tmp_path, monkeypatch):
    with recovery_case(tmp_path, monkeypatch) as c:
        db_before = c.repo.db_path.read_bytes()
        monkeypatch.setattr(
            RuntimeRepository,
            "_init_runtime_schema",
            lambda _self: pytest.fail("RO recovery must not initialize or migrate DB"),
        )
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert not report.load_errors and report.summary["requeued"] == 1
        assert c.repo.db_path.read_bytes() == db_before


@pytest.mark.parametrize("fault", ["unknown_p2", "lease_closing", "publisher_attempt_drift"])
def test_original_recovery_last_source_refusal_never_creates_new_execution(
    tmp_path, monkeypatch, fault
):
    with recovery_case(tmp_path, monkeypatch) as c:
        original = c.claim.path.read_bytes()
        p3 = reservation_file(c.gate.paths, c.claim.request_id)
        original_p3 = p3.read_bytes()
        if fault == "unknown_p2":
            monkeypatch.setattr(
                p2,
                "observe_processes",
                lambda: {
                    "platform": "darwin",
                    "host_id": "host-one",
                    "rows": {},
                    "failure": "unreadable",
                },
            )
        elif fault == "lease_closing":
            c.gate._runtime_takeover._lease._closing = True
        else:
            publish = c.gate._publish_stamped

            def raced(path, payload):
                publish(path, payload)
                current = read_json_file(path)
                current["execution_attempt_id"] = "other-attempt"
                write_json_file_atomic(path, current)

            monkeypatch.setattr(c.gate, "_publish_stamped", raced)
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.load_errors and report.summary["requeued"] == report.summary["failed"] == 0
        assert c.claim.path.exists() and not (c.gate.paths.terminal / c.claim.path.name).exists()
        assert not (c.gate.paths.inbox / c.claim.path.name).exists()
        if fault != "publisher_attempt_drift":
            assert c.claim.path.read_bytes() == original and p3.read_bytes() == original_p3
        else:
            assert read_json_file(c.claim.path)["execution_attempt_id"] == "other-attempt"
            assert _read_reservation(p3)["state"] == "occupied"
        assert c.gate.release_settled()["released"] == 0


@pytest.mark.parametrize("drift", ["actor", "target", "thread"])
def test_foreign_original_input_receipt_is_retained_without_queue(tmp_path, monkeypatch, drift):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery

    with recovery_case(tmp_path, monkeypatch, with_input=True) as c:
        path = delivery.gateway_input_receipt_path(c.gate.paths, "input-original")
        row = read_json_file(path)
        if drift == "actor":
            row["prepared_request"]["metadata"]["user_id"] = "other-peer"
            row["prepared_request"]["user_id"] = "other-peer"
            row["prepared_payload_digest"] = delivery._prepared_payload_digest(
                row["prepared_request"]
            )
        elif drift == "target":
            row["target_turn_id"] = "other-turn"
        else:
            row["prepared_request"]["metadata"]["thread_id"] = "other-thread"
            row["prepared_payload_digest"] = delivery._prepared_payload_digest(
                row["prepared_request"]
            )
        write_json_file_atomic(path, row)
        old = path.read_bytes()
        by_turn = c.gate.paths.root / "input_receipts" / "by_turn"
        original_index = {p.relative_to(by_turn): p.read_bytes() for p in by_turn.rglob("*.json")}
        payload = read_json_file(c.claim.path)
        payload["processing_failure_count"] = c.context.max_attempts - 1
        write_json_file_atomic(c.claim.path, payload)
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.summary["failed"] == 1 and len(report.load_errors) == 1
        failure = report.load_errors[0]
        assert failure["error_type"] == "RequestExecutionOutcomeUncertainError"
        assert failure["error_code"] == "ACTIVE_TURN_OUTCOME_UNCERTAIN"
        assert failure["category"] == "state" and failure["recoverable"] is False
        assert failure["recommended_action"] == "manual_review"
        assert failure["terminal_committed"] is True and failure["settlement_pending"] is True
        assert failure["request_id"] == c.claim.request_id and failure["operation_entered"] is False
        assert failure["phase"] == (
            "processing_recovery.receipt_guidance" if drift == "thread" else "processing_recovery.receipt_owner"
        )
        assert (
            path.read_bytes() == old and not (c.gate.paths.inbox / "input-original.json").exists()
        )
        assert {p.relative_to(by_turn): p.read_bytes() for p in by_turn.rglob("*.json")} == original_index
        canonical = read_json_file(c.gate.paths.terminal / c.claim.path.name)
        assert canonical["schema_version"] == "gateway_terminal_request.v1"
        assert canonical["id"] == c.claim.request_id and canonical["status"] == "failed"
        assert canonical["turn_phase"] == "closed" and canonical["terminal_response"]["ok"] is False
        assert canonical["request_fingerprint"] == payload["request_fingerprint"]
        assert not c.claim.path.exists()
        assert _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))["state"] == "occupied"
        from agent_py_agent.agent.gateway_parts.recovery import _terminal_projection_marker_path

        marker = _terminal_projection_marker_path(c.gate.paths, c.claim.request_id)
        assert read_json_file(marker)["settlement_pending"] is True


def test_real_default_remote_store_clears_only_original_main_resolved_paths(tmp_path, monkeypatch):
    with recovery_case(tmp_path, monkeypatch, canonical_config=True) as c:
        base_config = c.case[6].config
        main_path = base_config.conversation_workspace
        assert str(c.store.storage.root) != main_path
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.load_errors == [] and report.summary["requeued"] == 1
        assert base_config.conversation_workspace == main_path
        assert not (c.case[6].home_paths.owner_home_dir / "runtime.db").exists()
        assert c.repo.db_path.is_relative_to(
            resolve_owner_home(c.case[6].home_paths.root, c.case[5][0]).home_dir
        )


def test_closing_between_hot_commit_and_post_keeps_input_and_p3_debt(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_projection_marker_path

    with recovery_case(tmp_path, monkeypatch, with_input=True) as c:
        original = delivery.gateway_turn_transition
        closing = []

        @contextmanager
        def withdraw(paths, request_id):
            if not closing:
                closing.append(True)
                c.gate._runtime_takeover._lease._closing = True
            with original(paths, request_id):
                yield

        monkeypatch.setattr(delivery, "gateway_turn_transition", withdraw)
        payload = read_json_file(c.claim.path)
        payload["processing_failure_count"] = c.context.max_attempts - 1
        write_json_file_atomic(c.claim.path, payload)
        receipt = delivery.gateway_input_receipt_path(c.gate.paths, "input-original")
        old = receipt.read_bytes()
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert closing and report.load_errors
        assert not c.claim.path.exists() and (c.gate.paths.terminal / c.claim.path.name).exists()
        assert (
            receipt.read_bytes() == old
            and not (c.gate.paths.inbox / "input-original.json").exists()
        )
        assert (
            read_json_file(_terminal_projection_marker_path(c.gate.paths, c.claim.request_id))[
                "settlement_pending"
            ]
            is True
        )
        assert (
            _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))["state"]
            == "occupied"
        )
        assert c.gate.release_settled()["released"] == 0


def test_metadata_ports_do_not_install_p5_or_executor_launch_permission(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.executor_registration import (
        ExecutorRegistrationError,
        registered_popen,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import current_worker_execution

    with recovery_case(tmp_path, monkeypatch) as c:
        payload = read_json_file(c.claim.path)

        def metadata_only(_paths, _path, _payload, _context):
            assert current_worker_execution() is None
            with pytest.raises(ExecutorRegistrationError) as error:
                registered_popen("shell", ["unused"], None)
            assert error.value.error["error_code"] == "EXECUTOR_CONTEXT_REQUIRED"
            return ""

        assert (
            pr.recover_stale_processing(
                c.gate.paths, c.claim.path, payload, c.context, metadata_only
            )
            == ""
        )
        assert c.claim.path.exists() and not (c.gate.paths.terminal / c.claim.path.name).exists()


def test_policy_block_cleared_in_actual_db_recovers_again_with_fresh_c(tmp_path, monkeypatch):
    with recovery_case(tmp_path, monkeypatch) as c:
        original_status = c.repo.get_attempt(c.binding["attempt_id"])["status"]
        with c.repo.transaction() as db:
            db.execute(
                "UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",
                (c.binding["attempt_id"],),
            )
        first = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert first.load_errors and c.claim.path.exists()
        assert read_json_file(c.claim.path)["route"]["c_run_id"] == "new-c"
        p3id = _read_reservation(reservation_file(c.gate.paths, c.claim.request_id))[
            "reservation_id"
        ]
    with c.repo.transaction() as db:
        db.execute(
            "UPDATE agent_attempts SET status=? WHERE attempt_id=?",
            (original_status, c.binding["attempt_id"]),
        )
    with mc.coordinator_guard(c.gate.paths.root, 1):
        control = rt.prepare_coordinator_takeover(c.gate.paths, 1, "third-c")
        gate = current_gate(c.case, run="third-c")
        rt.bind_admission_takeover(gate, control)
        from dataclasses import replace

        report = recover_gateway_processing_requests_report(
            gate.paths, params=replace(c.context, admission_gate=gate)
        )
        assert report.load_errors == [] and report.summary["requeued"] == 1
        record = _read_reservation(reservation_file(gate.paths, c.claim.request_id))
        assert record["reservation_id"] == p3id and record["state"] == "occupied"
        assert (
            read_json_file(gate.paths.inbox / c.claim.path.name)["route"]["c_run_id"] == "third-c"
        )


@pytest.mark.parametrize(
    "marker", ["full_route", "partial_route", "route_owner_only", "binding_only"]
)
def test_original_mp_queue_in_plain_count0_never_releases_or_requeues(
    tmp_path, monkeypatch, marker
):
    from dataclasses import replace

    with recovery_case(tmp_path, monkeypatch) as c:
        payload = read_json_file(c.claim.path)
    if marker == "partial_route":
        payload["route"] = {"c_run_id": payload["route"]["c_run_id"]}
    elif marker == "route_owner_only":
        payload.pop("route")
        payload["metadata"].pop("gateway_admin_binding_version")
    elif marker == "binding_only":
        payload.pop("route")
        payload["metadata"].pop("host_request_owner.v1")
    write_json_file_atomic(c.claim.path, payload)
    before = c.claim.path.read_bytes()
    context = replace(c.context, admission_gate=None, agent=None)
    monkeypatch.setattr(
        pr,
        "RuntimeRepository",
        lambda *_a, **_k: pytest.fail("No P2/DB lookup without actual source"),
    )
    report = recover_gateway_processing_requests_report(c.gate.paths, params=context)
    assert report.load_errors and report.summary["failed"] == report.summary["requeued"] == 0
    assert (
        c.claim.path.read_bytes() == before
        and not (c.gate.paths.inbox / c.claim.path.name).exists()
    )


@pytest.mark.parametrize(
    "failure", ["missing_p1", "other_attempt", "other_owner", "other_epoch", "two_generations"]
)
def test_original_p1_locator_missing_or_ambiguous_never_adopts_current_p3(
    tmp_path, monkeypatch, failure
):
    with recovery_case(tmp_path, monkeypatch, ambiguous_origin=failure == "two_generations") as c:
        ledger = c.case[1]
        original = ledger.read("exec-one")
        # 故障仅在合成规范P1输入；不是为真实旧executor伪造当前停止证明。
        if failure == "missing_p1":
            path = ledger.root.joinpath(*ledger._parts("exec-one"))
            path.unlink()
        elif failure == "two_generations":
            pass  # 两个完整原P1代次已在Count0原producer阶段准备。
        else:
            row = dict(original)
            if failure == "other_attempt":
                row["execution_attempt_id"] = "another-actual-attempt"
            elif failure == "other_epoch":
                row["lease_epoch"] += 1
            else:
                row["owner_sha"] = "0" * 64
            from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record

            ledger.root.joinpath(*ledger._parts("exec-one")).write_bytes(serialize_record(row))
        before = c.claim.path.read_bytes()
        report = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert report.load_errors and report.summary["requeued"] == report.summary["failed"] == 0
        assert c.claim.path.read_bytes() == before


@pytest.mark.parametrize("drift", ["none", "wrong_owner", "unknown_runtime"])
def test_pending_post_next_fresh_c_rechecks_policy_owner_and_is_idempotent(
    tmp_path, monkeypatch, drift
):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts import recovery as r
    from agent_py_agent.cli.gateway_loops import _RequestDispatcher
    from agent_py_agent.cli.models import GatewayRunContext

    with recovery_case(tmp_path, monkeypatch, with_input=True) as c:
        payload = read_json_file(c.claim.path)
        payload["processing_failure_count"] = c.context.max_attempts - 1
        write_json_file_atomic(c.claim.path, payload)
        with monkeypatch.context() as m:

            def fail_write(*_args, **_kwargs):
                raise OSError("synthetic actual post consumer interruption")

            m.setattr(delivery, "settle_gateway_inputs_for_turn", fail_write)
            first = recover_gateway_processing_requests_report(c.gate.paths, params=c.context)
        assert first.summary["failed"] == 1 and not first.load_errors
        canonical = c.gate.paths.terminal / c.claim.path.name
        original_canonical = canonical.read_bytes()
        receipt = delivery.gateway_input_receipt_path(c.gate.paths, "input-original")
        original_receipt = receipt.read_bytes()
        assert (
            read_json_file(r._terminal_projection_marker_path(c.gate.paths, c.claim.request_id))[
                "settlement_pending"
            ]
            is True
        )
        assert c.gate.release_settled()["released"] == 1
    if drift != "none":
        with c.repo.transaction() as db:
            if drift == "wrong_owner":
                db.execute(
                    "UPDATE tasks SET owner_id='providers/synthetic/users/other' WHERE task_id=?",
                    (c.binding["task_id"],),
                )
            else:
                db.execute(
                    "UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",
                    (c.binding["attempt_id"],),
                )
    with mc.coordinator_guard(c.gate.paths.root, 1):
        control = rt.prepare_coordinator_takeover(c.gate.paths, 1, "third-c")
        context = GatewayRunContext(
            c.case[6], c.gate.paths, tmp_path / "config.yaml", coordinator_worker_count=1
        )
        dispatcher = _RequestDispatcher(context, c.gate.paths)
        assert dispatcher._admission_gate._runtime_takeover is control
        try:
            dispatcher._project_terminals_once()
            marker = read_json_file(
                r._terminal_projection_marker_path(c.gate.paths, c.claim.request_id)
            )
            if drift != "none":
                assert (
                    receipt.read_bytes() == original_receipt
                    and marker["settlement_pending"] is True
                )
                assert not (c.gate.paths.inbox / "input-original.json").exists()
            else:
                assert (
                    read_json_file(receipt)["state"] == "queued"
                    and marker["settlement_pending"] is False
                )
                queued = (c.gate.paths.inbox / "input-original.json").read_bytes()
                history = c.gate.paths.history.read_bytes()
                dispatcher._project_terminals_once()
                assert (c.gate.paths.inbox / "input-original.json").read_bytes() == queued
                assert c.gate.paths.history.read_bytes() == history
                assert (
                    sum(
                        json.loads(line).get("id") == c.claim.request_id
                        for line in history.splitlines()
                    )
                    == 1
                )
                assert dispatcher._admission_gate.release_settled()["released"] == 0
            assert canonical.read_bytes() == original_canonical
        finally:
            dispatcher._executor.shutdown(wait=True)
