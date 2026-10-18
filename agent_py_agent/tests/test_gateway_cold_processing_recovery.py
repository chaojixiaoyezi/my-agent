# LLM: 仅在临时目录复验真实ControlPlane/原P3/RuntimeDB恢复，不启动业务Agent、Worker、模型或物理P2；停止输入沿既有合成进程表。
# 模块用途: 守住cold C的纯路径投影与原恢复短门、未知责任和Count0边界。
import shutil
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_processing_recovery as pr
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    _read_reservation,
    coordinator_admission_gate,
    reservation_file,
)
from agent_py_agent.agent.gateway_parts.recovery import (
    recover_gateway_processing_requests_report,
    repair_gateway_terminal_projections,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_processing_recovery_source import recovery_case


# LLM: 既有helper只准备旧canonical/Runtime账；被测宿主始终是本代真实ControlPlane，gate/context同一引用，不动态补Agent字段。
# 函数用途: 在实际C租约内组装原恢复门，并禁止任何业务Agent/foreign pool旁路。
@contextmanager
def cold_case(tmp_path, monkeypatch, *, with_input=False, same_owner=False, override=""):
    with recovery_case(tmp_path, monkeypatch, with_input=with_input, canonical_config=True) as original:
        host = original.case[6]
        config = AgentConfig(my_agent_home=str(host.home_paths.root))
        config.gateway_request_max_attempts = 3
        config.gateway_processing_timeout_seconds = 10
        if same_owner:
            owner = original.case[5][0]
            config.my_agent_owner_provider = owner.provider
            config.my_agent_owner_kind = owner.owner_kind
            config.my_agent_owner_id = owner.owner_id
        if override:
            config.conversation_workspace = override
        control = GatewayControlPlane(
            config, host.home_paths.root, original.gate.paths, 1,
            original.gate.c_run_id, agent_root=host.root, workspace_roots=(host.root,),
        )
        run = GatewayRunContext(
            None, original.gate.paths, tmp_path / "config.yaml", coordinator_worker_count=1,
            control_plane=control,
        )
        gate = coordinator_admission_gate(control, original.gate.paths, run)
        context = replace(original.context, agent=control, admission_gate=gate)
        assert gate.agent is context.agent is control
        assert not hasattr(control, "home_paths") and not hasattr(control, "conversation_store")
        from agent_py_agent.agent import core
        from agent_py_agent.agent.gateway_parts import request_worker

        def forbidden(*_args, **_kwargs):
            pytest.fail("cold recovery cannot construct or resolve a business Agent")

        monkeypatch.setattr(core.SimpleAgent, "__init__", forbidden)
        monkeypatch.setattr(request_worker, "_owner_pool", forbidden)
        monkeypatch.setattr(request_worker, "_resolve_request_agent", forbidden)
        yield original, control, gate, context
        assert not hasattr(control, "home_paths") and not hasattr(control, "conversation_store")


@pytest.mark.parametrize("action", ["requeue", "terminal"])
def test_cold_real_control_uses_original_recovery_gate_and_owner_store(tmp_path, monkeypatch, action):
    with cold_case(tmp_path, monkeypatch, with_input=True) as (original, control, gate, context):
        payload = read_json_file(original.claim.path)
        if action == "terminal":
            payload["processing_failure_count"] = context.max_attempts - 1
            write_json_file_atomic(original.claim.path, payload)
        before_db = original.repo.db_path.read_bytes()
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert report.load_errors == []
        assert report.summary["failed" if action == "terminal" else "requeued"] == 1
        assert not original.claim.path.exists()
        record = _read_reservation(reservation_file(gate.paths, original.claim.request_id))
        assert record["state"] == "occupied"
        if action == "terminal":
            canonical = gate.paths.terminal / original.claim.path.name
            before = canonical.read_bytes()
            assert gate.release_settled()["released"] == 1
            assert canonical.read_bytes() == before
            assert repair_gateway_terminal_projections(
                gate.paths, agent=control, recovery_context=context, full_scan=True,
            )[1] == []
            assert canonical.read_bytes() == before
        else:
            assert gate.release_settled()["released"] == 0
        assert original.repo.db_path.read_bytes() == before_db


def test_cold_foreign_owner_drops_only_cloned_runtime_overrides(tmp_path, monkeypatch):
    poisoned = str(tmp_path / "home/owners/local/main/wrong-conversations")
    with cold_case(tmp_path, monkeypatch, override=poisoned) as (original, control, gate, context):
        before_config = dict(vars(control.config))
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert not report.load_errors and report.summary["requeued"] == 1
        assert dict(vars(control.config)) == before_config
        assert control.config.conversation_workspace == poisoned
        assert not original.claim.path.exists() and not (tmp_path / "home/owners/local/main/wrong-conversations").exists()


def test_cold_same_owner_keeps_explicit_existing_store_and_initialize_false(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch, same_owner=True) as (original, control, gate, context):
        custom = original.store.storage.root.parents[3] / "explicit-conversations"
        shutil.copytree(original.store.storage.root, custom)
        control.config.conversation_workspace = str(custom)
        seen = []
        original_init = pr.ConversationStore.__init__

        def observed(store, root, *, initialize=True, **kwargs):
            seen.append((root, initialize))
            return original_init(store, root, initialize=initialize, **kwargs)

        monkeypatch.setattr(pr.ConversationStore, "__init__", observed)
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert report.load_errors == [] and report.summary["requeued"] == 1
        assert seen and all(root == custom and initialize is False for root, initialize in seen)


def test_cold_control_uses_captured_home_not_environment_or_config_alias(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (_original, control, gate, context):
        control.config.my_agent_home = str(tmp_path / "later-config-alias")
        monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "later-env-home"))
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert not report.load_errors and report.summary["requeued"] == 1
        assert not (tmp_path / "later-env-home").exists() and not (tmp_path / "later-config-alias").exists()


@pytest.mark.parametrize("failure", ["unknown_runtime", "wrong_runtime_owner", "closed_c", "wrong_context", "missing_store", "outside_store"])
def test_cold_actual_original_refusal_keeps_processing_and_p3(tmp_path, monkeypatch, failure):
    with cold_case(tmp_path, monkeypatch, same_owner=failure == "outside_store") as (original, control, gate, context):
        if failure == "unknown_runtime":
            with original.repo.transaction() as db:
                db.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (original.binding["attempt_id"],))
        elif failure == "wrong_runtime_owner":
            with original.repo.transaction() as db:
                db.execute("UPDATE tasks SET owner_id='providers/synthetic/users/other' WHERE task_id=?", (original.binding["task_id"],))
        elif failure == "closed_c":
            gate._runtime_takeover._lease._closing = True
        elif failure == "wrong_context":
            context = replace(context, agent=original.case[6])
        elif failure == "missing_store":
            shutil.rmtree(original.store.storage.root)
        else:
            control.config.conversation_workspace = str(tmp_path / "outside-owner")
        original_claim = original.claim.path.read_bytes()
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert report.load_errors and report.summary["requeued"] == report.summary["failed"] == 0
        assert original.claim.path.exists()
        assert _read_reservation(reservation_file(gate.paths, original.claim.request_id))["state"] == "occupied"
        assert gate.release_settled()["released"] == 0
        assert not (gate.paths.inbox / original.claim.path.name).exists()
        if failure in {"closed_c", "wrong_context"}:
            assert original.claim.path.read_bytes() == original_claim
        if failure == "missing_store":
            assert not original.store.storage.root.exists()


def test_cold_close_between_canonical_commit_and_input_post_retains_debt(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_projection_marker_path

    with cold_case(tmp_path, monkeypatch, with_input=True) as (original, _control, gate, context):
        payload = read_json_file(original.claim.path)
        payload["processing_failure_count"] = context.max_attempts - 1
        write_json_file_atomic(original.claim.path, payload)
        receipt = delivery.gateway_input_receipt_path(gate.paths, "input-original")
        before = receipt.read_bytes()
        original_transition = delivery.gateway_turn_transition

        @contextmanager
        def close_before_post(paths, request_id):
            gate._runtime_takeover._lease._closing = True
            with original_transition(paths, request_id):
                yield

        monkeypatch.setattr(delivery, "gateway_turn_transition", close_before_post)
        report = recover_gateway_processing_requests_report(gate.paths, params=context)
        assert report.load_errors
        assert (gate.paths.terminal / original.claim.path.name).exists()
        assert receipt.read_bytes() == before and not (gate.paths.inbox / "input-original.json").exists()
        assert read_json_file(_terminal_projection_marker_path(gate.paths, original.claim.request_id))["settlement_pending"]
        assert _read_reservation(reservation_file(gate.paths, original.claim.request_id))["state"] == "occupied"
        assert gate.release_settled()["released"] == 0
