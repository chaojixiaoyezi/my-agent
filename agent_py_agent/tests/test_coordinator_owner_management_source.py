"""真实HTTP活栈的共用来源合同；内部source fixture不等同agents/memory领域端到端验收。"""

import copy
import json
import stat
import threading
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
from agent_py_agent.tests.test_coordinator_model_menu import post_model


def request_body(message="management-source"):
    return {
        "command": "/agents show",
        "conversation_id": "management-source-session",
        "metadata": {"message_id": message, "channel_chat_type": "p2p"},
    }


def seed_receipt(server, row, state):
    body = request_body()
    scope = GatewayControlScope(
        user_id=row["actor_id"],
        channel="feishu",
        conversation_id=body["conversation_id"],
        metadata=body["metadata"],
        resolved_owner=OwnerIdentity.provider_user("feishu", row["actor_id"]),
    )
    operation_id, digest = ops.gateway_control_operation_identity(scope, body["command"])
    receipt = ops._new_control_operation_receipt(
        scope, "agents", body["command"], operation_id, digest
    )
    receipt = replace(receipt, state=state)
    if state == "completed":
        receipt = replace(
            receipt, result=ConversationControlResult("agents", True, "原领域结果").to_dict()
        )
    if state == "terminal_unknown":
        receipt = replace(receipt, error={"error_code": "ORIGINAL_UNKNOWN"})
    if state == "claimed":
        from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import (
            _control_dispatch_for_receipt,
        )
        from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root

        lease = coordinator_for_root(server.paths.root, context=server.control_plane)
        route = _control_dispatch_for_receipt(receipt, lease, lease.c_run_id, None)
        route.update(
            executor={
                "schema_version": "multiprocess_process_identity.v1",
                "host_id": "fixture-host",
                "pid": 1234,
                "start_time": "fixture-birth",
            },
            instance_id="fixture-instance",
        )
        receipt = replace(receipt, dispatch=route)
    payload = receipt.to_dict()
    ops.GatewayControlOperationReceipt.from_dict(payload)
    path = ops.gateway_control_operation_path(server.paths, operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path, receipt


@pytest.mark.parametrize(
    "state", ["prepared", "claimed", "executing", "completed", "terminal_unknown"]
)
def test_actual_http_existing_management_is_replay_without_source(tmp_path, monkeypatch, state):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        *_, server, runtime, sent = case
        row = case[3]
        path, receipt = seed_receipt(server, row, state)
        before = path.read_bytes()
        status, result = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        assert status == (200 if state == "completed" else 202), result
        assert result == ops.gateway_control_operation_status_payload(receipt)
        assert path.read_bytes() == before and not delivered and server.agent is None


@pytest.mark.parametrize("command", ["/agents show", "/memory candidates"])
def test_actual_http_fresh_connected_agents_and_memory_execute(
    tmp_path, monkeypatch, command
):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        status, result = post_model(
            server, {**request_body(), "command": command}, route="/control", actor=row["actor_id"]
        )
        if command == "/agents show":
            assert status == 200 and result["ok"] and result["details"]["state"] == "loaded"
            receipt = ops.read_gateway_control_operation(server.paths, result["operation_id"], read_only=True)
            assert receipt.state == "completed" and receipt.dispatch is None and receipt.conversation_thread_id
        else:
            assert status == 200 and result["ok"] and result["details"]["state"] == "list"
            assert result["details"]["count"] == 0
            receipt = ops.read_gateway_control_operation(server.paths, result["operation_id"], read_only=True)
            assert receipt.state == "completed" and receipt.dispatch is None and receipt.conversation_thread_id
        assert not delivered and server.agent is None


# LLM: 只把HTTP固定producer入口暂接合同fixture，真实work/G1/C/T均保留；这里不替代任何领域服务或宣称业务成功。
# 函数用途: 在实际handler线程内检查来源及receipt阶段，异常由原HTTP层正常返回。
def install_source_contract_fixture(monkeypatch, exercise):
    from agent_py_agent.agent.gateway_parts import coordinator_owner_management as common

    observed = []

    def run(handler, server, body):
        try:
            with common.coordinator_owner_management_operation(handler, server, body) as carrier:
                observed.append(carrier)
                return exercise(common, carrier)
        except BaseException as error:
            observed.append(error)
            raise

    monkeypatch.setattr(common, "execute_coordinator_owner_management", run)
    return observed


def test_source_fixed_receipt_all_writers_original_bytes_and_retired_clone(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )

    stages, sources, under_lock = [], [], []
    original = json_io.write_private_json_file_atomic_no_newline_unlocked

    def writer(path, payload, **options):
        if "control_operations" in str(path):
            with pytest.raises(BlockingIOError):
                with json_io.locked_json_path(path, blocking=False):
                    raise AssertionError("same JSON lock must already be held")
            stages.append(payload["state"])
            under_lock.append(True)
        return original(path, payload, **options)

    monkeypatch.setattr(json_io, "write_private_json_file_atomic_no_newline_unlocked", writer)

    def exercise(common, carrier):
        source, receipt, scope = carrier
        sources.extend([source, copy.copy(source)])
        assert source.agent is None and source.deadline is None
        assert source.http_origin[7].used and source._receipt_state == "executing"
        return common.complete_owner_management_operation(
            source, ConversationControlResult("agents", True, "合同fixture结果")
        )

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        status, result = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        assert status == 200 and result["control_state"] == "completed", result
        assert stages == ["prepared", "executing", "completed"] and len(under_lock) == 3
        source = sources[0]
        path = ops.gateway_control_operation_path(server.paths, source.receipt.operation_id)
        assert (
            path.read_bytes()
            == json.dumps(
                json.loads(path.read_bytes()), ensure_ascii=False, indent=2, sort_keys=True
            ).encode()
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for stale in sources:
            with pytest.raises(LocalControlCoordinatorUnavailable):
                stale._require_http_current()
        assert not delivered and len(observed) == 1


@pytest.mark.parametrize(
    "mode", ["body", "copy", "thread", "current_permission", "operation", "cas", "json_busy"]
)
def test_source_rejects_changed_objects_and_preserves_actual_receipt(tmp_path, monkeypatch, mode):
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )

    outcomes = []

    def exercise(common, carrier):
        source, receipt, scope = carrier
        path = ops.gateway_control_operation_path(source.paths, receipt.operation_id)
        before = path.read_bytes()
        if mode == "body":
            source.http_origin[2]["metadata"]["message_id"] = "changed"
            with pytest.raises(LocalControlCoordinatorUnavailable):
                source._require_http_current()
            source.http_origin[2]["metadata"]["message_id"] = scope.metadata["message_id"]
        elif mode == "copy":
            with pytest.raises(LocalControlCoordinatorUnavailable):
                copy.copy(source)._require_http_current()
        elif mode == "thread":

            def other():
                try:
                    source._require_http_current()
                except BaseException as error:
                    outcomes.append(error)

            t = threading.Thread(target=other)
            t.start()
            t.join(2)
            assert not t.is_alive() and type(outcomes[0]) is LocalControlCoordinatorUnavailable
        elif mode == "current_permission":
            source.http_origin[7].active = False
            with pytest.raises(OwnerManagementIngressUnavailable):
                source._require_http_current()
            source.http_origin[7].active = True
        elif mode == "operation":
            other_scope = replace(
                scope, metadata={**scope.metadata, "message_id": "other-preview-operation"}
            )
            other_id, other_digest = ops.gateway_control_operation_identity(
                other_scope, source.http_origin[4]
            )
            other = ops._new_control_operation_receipt(
                other_scope, "agents", source.http_origin[4], other_id, other_digest
            )
            ops.GatewayControlOperationReceipt.from_dict(other.to_dict())
            with pytest.raises(LocalControlCoordinatorUnavailable):
                source.commit_control_receipt(other.to_dict())
        elif mode == "cas":
            # 同一严格codec对象，完整raw只多空白，仍不得被receipt writer覆盖。
            path.write_bytes(before + b" ")
            with pytest.raises(LocalControlCoordinatorUnavailable):
                common.complete_owner_management_operation(
                    source, ConversationControlResult("agents", True, "fixture")
                )
            assert path.read_bytes() == before + b" "
            path.write_bytes(before)
        elif mode == "json_busy":
            with json_io.locked_json_path(path):
                with pytest.raises(BlockingIOError):
                    common.complete_owner_management_operation(
                        source, ConversationControlResult("agents", True, "fixture")
                    )
            assert path.read_bytes() == before
        assert path.read_bytes() == before
        return common.complete_owner_management_operation(
            source, ConversationControlResult("agents", True, "fixture")
        )

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        status, result = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        failed = mode in {'body', 'current_permission', 'cas'}
        assert status == (503 if failed else 200), result
        source = observed[0][0]
        receipt = ops.read_gateway_control_operation(server.paths, source.receipt.operation_id, read_only=True)
        assert receipt.state == ('executing' if failed else 'completed')
        if failed:
            assert observed[-1] is source._native_source_failure
        else:
            assert source._native_source_failure is None
        assert not delivered


@pytest.mark.parametrize(
    "kind",
    [
        "context",
        "outcome",
        "bounded",
        "tool",
        "worker",
        "global_commit",
        "global",
        "local",
        "ingress",
        "ordinary",
    ],
)
def test_source_typed_same_object_never_becomes_ordinary_unknown(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.global_write_commit import GlobalWriteCommitUnavailable
    from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )

    errors = {
        "context": RequestExecutionContextUnavailable("fixture"),
        "outcome": RequestExecutionOutcomeUncertainError(
            {
                "id": "req",
                "execution_attempt_id": "attempt",
                "lease_epoch": 1,
                "lease_owner": "worker",
            },
            "fixture",
        ),
        "bounded": BoundedCallExitUnconfirmedError("fixture", "worker_exit_unconfirmed"),
        "tool": ToolWriteContextUnavailable("fixture"),
        "worker": GlobalWriteInWorkerError("fixture"),
        "global_commit": GlobalWriteCommitUnavailable("fixture"),
        "global": GlobalWriteError("fixture", "fixture"),
        "local": LocalControlCoordinatorUnavailable("fixture"),
        "ingress": OwnerManagementIngressUnavailable("fixture"),
        "ordinary": OSError("ordinary fixture"),
    }
    error = errors[kind]

    def exercise(common, carrier):
        raise error

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        # 最外HTTP只投影异常；source合同检查始终保存同一个异常实例。
        status, _ = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        assert status == 503 and observed[-1] is error
        source = observed[0][0]
        receipt = ops.read_gateway_control_operation(
            server.paths, source.receipt.operation_id, read_only=True
        )
        assert receipt.state == ("terminal_unknown" if kind == "ordinary" else "executing")
        assert not delivered


@pytest.mark.parametrize("stage", ["prepared", "executing", "completed", "terminal_unknown"])
@pytest.mark.parametrize("typed", [False, True])
def test_each_fixed_writer_failure_preserves_actual_last_raw(tmp_path, monkeypatch, stage, typed):
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )

    error = (
        RequestExecutionContextUnavailable("writer-fixture") if typed else OSError("writer-fixture")
    )
    primary = OSError("domain partial fixture")
    writer = json_io.write_private_json_file_atomic_no_newline_unlocked
    writes = []

    def fail(path, payload, **options):
        if "control_operations" in str(path):
            writes.append(payload["state"])
            if payload["state"] == stage:
                raise error
        return writer(path, payload, **options)

    monkeypatch.setattr(json_io, "write_private_json_file_atomic_no_newline_unlocked", fail)

    def exercise(common, carrier):
        if stage == "terminal_unknown":
            raise primary
        return common.complete_owner_management_operation(
            carrier[0], ConversationControlResult("agents", True, "fixture")
        )

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        status, result = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        assert status == 503, result
        paths = list((server.paths.root / "control_operations").glob("*.json"))
        expected = {
            "prepared": None,
            "executing": "prepared",
            "completed": "executing" if typed else "terminal_unknown",
            "terminal_unknown": "executing",
        }[stage]
        if expected is None:
            assert paths == []
        else:
            assert len(paths) == 1 and json.loads(paths[0].read_bytes())["state"] == expected
        assert observed[-1] is (primary if stage == "terminal_unknown" and not typed else error)
        assert not delivered


@pytest.mark.parametrize("existing", [False, True])
def test_original_operation_t_busy_does_not_block_or_create_source(tmp_path, monkeypatch, existing):
    import time

    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        if existing:
            path, receipt = seed_receipt(server, row, "executing")
            operation = receipt.operation_id
            before = path.read_bytes()
        else:
            scope = GatewayControlScope(
                user_id=row["actor_id"],
                channel="feishu",
                conversation_id=request_body()["conversation_id"],
                metadata=request_body()["metadata"],
                resolved_owner=OwnerIdentity.provider_user("feishu", row["actor_id"]),
            )
            operation = ops.gateway_control_operation_identity(scope, "/agents show")[0]
        held, release = threading.Event(), threading.Event()

        def owner():
            with ops.gateway_control_operation_transition(server.paths, operation):
                held.set()
                release.wait(3)

        t = threading.Thread(target=owner)
        t.start()
        assert held.wait(2)
        try:
            started = time.monotonic()
            status, result = post_model(
                server, request_body(), route="/control", actor=row["actor_id"]
            )
            assert time.monotonic() - started < 2
            assert status == (202 if existing else 503), result
            if existing:
                assert path.read_bytes() == before
            else:
                assert not list((server.paths.root / "control_operations").glob("*.json"))
        finally:
            release.set()
            t.join(2)
        assert not t.is_alive() and not delivered


def test_runtime_gate_busy_is_try_and_current_permission_is_rechecked(tmp_path, monkeypatch):
    from agent_py_agent.agent.auth.models import Permission, Role
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )

    def exercise(common, carrier):
        source = carrier[0]
        gate = source.http_origin[9]._gate
        held, release = threading.Event(), threading.Event()

        def owner():
            with gate:
                held.set()
                release.wait(3)

        t = threading.Thread(target=owner)
        t.start()
        assert held.wait(2)
        try:
            with pytest.raises(BlockingIOError):
                common.complete_owner_management_operation(
                    source, ConversationControlResult("agents", True, "fixture")
                )
        finally:
            release.set()
            t.join(2)
        assert source._native_source_failure is None  # 普通runtime锁忙可恢复，不是native死亡。
        middleware = source.http_origin[13]
        original = middleware.auth_manager.authenticate
        try:
            monkeypatch.setattr(
                middleware.auth_manager,
                "authenticate",
                lambda channel, user: Permission(Role.USER, user, []),
            )
            with pytest.raises(OwnerManagementIngressUnavailable):
                source._require_http_current()
        finally:
            monkeypatch.setattr(middleware.auth_manager, "authenticate", original)
        return common.complete_owner_management_operation(
            source, ConversationControlResult("agents", True, "fixture")
        )

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        assert post_model(server, request_body(), route="/control", actor=row["actor_id"])[0] == 503
        source = observed[0][0]
        assert observed[-1] is source._native_source_failure
        assert type(observed[-1]) is OwnerManagementIngressUnavailable
        assert ops.read_gateway_control_operation(server.paths, source.receipt.operation_id, read_only=True).state == 'executing'
        assert not delivered


@pytest.mark.parametrize("admin", [False, True])
def test_receipt_and_target_p2_remain_separate_and_binding_is_once(tmp_path, monkeypatch, admin):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    selected = []
    actor_holder = []
    target_holder = []

    def exercise(common, carrier):
        source, receipt, scope = carrier
        subject = (source.http_origin[7], source.http_origin[3], scope)
        before = source.receipt_owner
        if admin:
            writer = source.bind_resource_owner(
                target_holder[0], actor=actor_holder[0], subject=subject
            )
            assert source.receipt_owner is before and source.owner is before
            assert (
                writer(lambda: "fixed local fixture", owner_home=target_holder[0].owner_home_dir)
                == "fixed local fixture"
            )
            with pytest.raises(LocalControlCoordinatorUnavailable):
                source.bind_resource_owner(target_holder[0], actor=actor_holder[0], subject=subject)
            config = source.context.config
            old = config.access_mode
            config.access_mode = "workspace-write"
            try:
                with pytest.raises(OwnerManagementIngressUnavailable):
                    writer(
                        lambda: pytest.fail("revoked permission cannot reach writer"),
                        owner_home=target_holder[0].owner_home_dir,
                    )
            finally:
                config.access_mode = old
        else:
            with pytest.raises(OwnerManagementIngressUnavailable):
                source.bind_resource_owner(target_holder[0], actor=actor_holder[0], subject=subject)
        completed = common.complete_owner_management_operation(
            source, ConversationControlResult("agents", True, "source contract fixture")
        )
        assert completed.owner_id == before.owner_id
        return completed

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch, kind="main" if admin else "user") as (
        case,
        delivered,
    ):
        server, row, host = case[5], case[3], case[1]
        actor_holder.append(host)
        config = server.control_plane.config
        config.access_mode = "full-access"
        if admin:
            server.auth_middleware.auth_manager.admin_user_id = row["actor_id"]
        target = OwnerIdentity.provider_user("feishu", "other-resource-owner")
        target_holder.append(
            home_paths_with_owner(
                host.home_paths,
                resolve_owner_home(config_home := server.control_plane.home_root, target),
            )
        )
        assert target_holder[0].root == config_home
        controller = server.control_plane.controller()
        original = controller.launch_owner_writer

        def launch(owner, operation):
            selected.append(owner)
            return original(owner, operation)

        monkeypatch.setattr(controller, "launch_owner_writer", launch)
        status, result = post_model(server, request_body(), route="/control", actor=row["actor_id"])
        assert status == (503 if admin else 200), result
        source = observed[0][0]
        persisted = ops.read_gateway_control_operation(server.paths, source.receipt.operation_id, read_only=True)
        assert persisted.state == ('executing' if admin else 'completed')
        assert persisted.owner_id == source.receipt_owner.owner_id
        if admin:
            assert observed[-1] is source._native_source_failure
            assert type(observed[-1]) is OwnerManagementIngressUnavailable
        else:
            assert source._native_source_failure is None
        receipt_owner = (
            OwnerIdentity.local_main()
            if admin
            else OwnerIdentity.provider_user("feishu", row["actor_id"])
        )
        expected = [owner_digest(receipt_owner)] * 2
        if admin:
            # 撤权的尝试仍先选同target P2，但原当前权限末核阻止实际callback。
            expected += [owner_digest(target)] * 2
        if not admin:
            expected += [owner_digest(receipt_owner)]
        assert selected == expected
        assert not delivered


def test_no_new_five_second_total_budget_and_effect_runs_outside_root(tmp_path, monkeypatch):
    import http.client
    import time

    def exercise(common, carrier):
        source = carrier[0]
        assert source.deadline is None
        assert not source.controller._mutex._is_owned()
        assert not source.lease._lifecycle_gate._is_owned()
        assert not source.http_origin[9]._gate._is_owned()
        started = time.monotonic()
        threading.Event().wait(5.05)
        assert time.monotonic() - started > 5
        return common.complete_owner_management_operation(
            source, ConversationControlResult("agents", True, "source contract fixture")
        )

    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server.server_address[1], timeout=10
        )
        try:
            connection.request(
                "POST",
                "/control",
                json.dumps(request_body()),
                {
                    "Content-Type": "application/json",
                    "X-Channel": "feishu",
                    "X-User-Id": row["actor_id"],
                    "X-Gateway-Token": server._local_client_credential,
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            assert response.status == 200 and result["control_state"] == "completed", result
        finally:
            connection.close()
        assert not delivered


@pytest.mark.parametrize("case_name", ["corrupt", "conflict", "unknown_op", "missing_credential"])
def test_actual_http_negative_binding_does_not_issue_new_receipt(tmp_path, monkeypatch, case_name):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row = case[5], case[3]
        body = request_body()
        path = None
        if case_name in {"corrupt", "conflict"}:
            path, receipt = seed_receipt(server, row, "prepared")
            if case_name == "corrupt":
                path.write_bytes(b"{}")
            else:
                body["command"] = "/agents refresh"
            before = path.read_bytes()
        if case_name == "unknown_op":
            body["command"] = "/agents unknown"
        status, result = post_model(
            server,
            body,
            route="/control",
            actor=row["actor_id"],
            credential=case_name != "missing_credential",
        )
        expected = {"corrupt": 503, "conflict": 409, "unknown_op": 200, "missing_credential": 403}[
            case_name
        ]
        assert status == expected, result
        if path is not None:
            assert path.read_bytes() == before
        else:
            assert not list((server.paths.root / "control_operations").glob("*.json"))
        assert not delivered


def test_actual_upstream_deadline_is_same_absolute_value(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.backends.request_scope import _DEADLINE, provider_request_budget
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )

    original = _LocalControlWriteSource.http_management_scope

    @contextmanager
    def inherited(handler, server, body):
        with provider_request_budget(20):
            expected = _DEADLINE.get()
            with original(handler, server, body) as carrier:
                assert carrier[0].deadline == expected == _DEADLINE.get()
                yield carrier

    monkeypatch.setattr(_LocalControlWriteSource, "http_management_scope", inherited)

    def exercise(common, carrier):
        return common.complete_owner_management_operation(
            carrier[0], ConversationControlResult("agents", True, "source contract fixture")
        )

    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        assert (
            post_model(case[5], request_body(), route="/control", actor=case[3]["actor_id"])[0]
            == 200
        )
        assert not delivered


def test_cleanup_failure_cannot_replace_original_typed_object(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.gateway_parts import multiprocess_local_control as local

    original = local.retire_coordinator_commit
    primary = RequestExecutionContextUnavailable("original fixture")

    def retire(lease, pin):
        original(lease, pin)
        raise OSError("cleanup fixture")

    monkeypatch.setattr(local, "retire_coordinator_commit", retire)

    def exercise(common, carrier):
        raise primary

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        assert (
            post_model(case[5], request_body(), route="/control", actor=case[3]["actor_id"])[0]
            == 503
        )
        assert (
            observed[-1] is primary
            and "owner_management_pin_cleanup_unconfirmed" in primary.__notes__
        )
        assert not observed[0][0].lease._commit_pins
        assert not delivered
