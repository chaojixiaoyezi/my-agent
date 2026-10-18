"""只验证真实启动载体及拒绝门组件，不启动 C/W 或替代 P1/ready 事实。"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.common import executor_registration as executors
from agent_py_agent.agent.common import global_write_guard as writes
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition


@pytest.fixture(autouse=True)
def isolated_scope(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    monkeypatch.delenv("MY_AGENT_GATEWAY_WORKER", raising=False)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    monkeypatch.setattr(writes, "_restricted_executor", False)
    monkeypatch.setattr(executors, "_coordinator_required", lambda: None)
    assert target != (Path.home() / ".my-agent").resolve()
    yield
    assert not target.exists()


@pytest.fixture
def launch(tmp_path):
    return mode.WorkerLaunch(WorkerPartition(0, 2, "c-one"), tmp_path / "gateway", tmp_path / "home", 101, "instance-one")


def test_environment_roundtrip_freezes_original_launch_without_materialization(launch, monkeypatch, tmp_path):
    original = {"SYNTHETIC": "preserved"}
    environment = mode.worker_environment(launch, original)
    assert original == {"SYNTHETIC": "preserved"}
    assert environment["SYNTHETIC"] == "preserved"
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", environment["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    assert mode.current_worker_partition() == launch.partition
    assert freeze_worker_bootstrap(launch).launch is launch
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("marker,code", [(None, "WORKER_MODE_REQUIRED"), ("{", "WORKER_MODE_INVALID"),
                                       ("{}", "WORKER_MODE_INVALID"), ("null", "WORKER_MODE_INVALID")])
def test_missing_or_wrong_environment_cannot_establish_worker(launch, monkeypatch, marker, code):
    if marker is not None:
        monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", marker)
    assert mode.establish_worker_mode(launch) == {"ok": False, "error_code": code}
    assert mode.current_worker_partition() is None


@pytest.mark.parametrize("field,value", [("owner_root", Path("/other-home")), ("gateway_root", Path("/other-queue")),
                                        ("parent_pid", 102), ("instance_id", "other-instance"),
                                        ("partition", WorkerPartition(1, 2, "c-one")),
                                        ("partition", WorkerPartition(0, 2, "c-old"))])
def test_environment_and_requested_launch_must_match_all_original_fields(launch, monkeypatch, field, value):
    marker = mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"]
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", marker)
    assert mode.establish_worker_mode(replace(launch, **{field: value})) == {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    assert mode.current_worker_partition() is None


def test_established_partition_cannot_be_replaced_by_another_valid_marker(launch, monkeypatch):
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch)["ok"] is True
    other = replace(launch, partition=WorkerPartition(1, 2, "c-one"))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(other, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(other) == {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    assert mode.current_worker_partition() == launch.partition


def test_count0_queries_have_no_environment_or_filesystem_reads(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Count0 不得读取环境或文件")

    monkeypatch.setattr(Path, "open", unexpected)
    monkeypatch.setattr(mode.os.environ, "get", unexpected)
    assert mode.current_worker_partition() is None
    assert executors.executor_registration_enabled() is False
    assert executors.managed_request_execution_required() is False
    assert writes.restricted_executor_global_writes() is False
    assert writes.require_global_writer("synthetic_global_fact") is None


def test_worker_missing_scope_cannot_fall_back_to_default_execution(launch, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    assert executors.executor_registration_enabled() is True
    assert executors.managed_request_execution_required() is True
    with executors.executor_scope(None):
        assert executors.executor_registration_enabled() is True
        with pytest.raises(executors.ExecutorRegistrationError) as error:
            executors.registered_popen("synthetic", ["not-executed"], executors.ExecutorProcessOptions())
    assert error.value.error == {"error_code": "EXECUTOR_CONTEXT_REQUIRED", "stage": "binding", "automatic_replay": False}
    with pytest.raises(writes.GlobalWriteInWorkerError):
        writes.require_global_writer("synthetic_global_fact")


def test_scope_preserves_tool_binding_and_retires_it_without_launching_process(launch, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    calls = []
    receipt = object()

    def registered(kind, argv, options):
        calls.append((kind, argv, options, executors.current_executor_tool_call()))
        return receipt

    options = executors.ExecutorProcessOptions(cwd=launch.owner_root)
    with executors.executor_scope(registered, launch.partition):
        assert executors.capture_executor_launcher() is registered
        assert executors.current_executor_tool_call() is None
        with executors.executor_tool_call_scope("call-synthetic"):
            assert executors.registered_popen("synthetic", ["not-executed"], options) is receipt
        assert executors.current_executor_tool_call() is None
    assert calls == [("synthetic", ["not-executed"], options, "call-synthetic")]
    with pytest.raises(executors.ExecutorRegistrationError):
        executors.registered_popen("synthetic", [], options)


def test_wrong_partition_and_copied_process_scope_never_invoke_launcher(launch, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    calls = []
    with pytest.raises(executors.ExecutorRegistrationError):
        with executors.executor_scope(lambda *_args: calls.append(True), WorkerPartition(1, 2, "c-one")):
            pytest.fail("冲突分区不应进入 scope")
    with executors.executor_scope(lambda *_args: calls.append(True), launch.partition):
        pid = executors.os.getpid()
        monkeypatch.setattr(executors.os, "getpid", lambda: pid + 1)
        with pytest.raises(executors.ExecutorRegistrationError):
            executors.current_executor_tool_call()
        with pytest.raises(executors.ExecutorRegistrationError):
            executors.capture_executor_launcher()
        with pytest.raises(executors.ExecutorRegistrationError):
            executors.registered_popen("synthetic", [], executors.ExecutorProcessOptions())
    assert calls == []


def test_restricted_host_is_not_worker_and_cannot_write_global_facts():
    writes.restrict_executor_global_writes()
    assert mode.current_worker_partition() is None
    assert executors.managed_request_execution_required() is True
    with pytest.raises(writes.GlobalWriteInExecutorError) as error:
        writes.require_global_writer("synthetic_global_fact")
    assert error.value.error_code == "GLOBAL_WRITE_IN_EXECUTOR"
    assert error.value.resource == "synthetic_global_fact"


@pytest.mark.parametrize("call_id", ["", " ", None, True, "x" * 513])
def test_invalid_tool_identity_never_invokes_original_launcher(launch, monkeypatch, call_id):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    calls = []
    with executors.executor_scope(lambda *_args: calls.append(True), launch.partition):
        with pytest.raises(executors.ExecutorRegistrationError) as error:
            with executors.executor_tool_call_scope(call_id):
                pytest.fail("无效工具身份不应进入执行范围")
        assert error.value.error == {"error_code": "EXECUTOR_TOOL_CALL_INVALID", "stage": "binding", "automatic_replay": False}
        assert executors.current_executor_tool_call() is None
    assert calls == []


def test_explicit_platform_refusal_never_becomes_default_execution():
    with pytest.raises(executors.ExecutorRegistrationError) as error:
        executors.reject_executor_platform()
    assert error.value.error == {"error_code": "EXECUTOR_PLATFORM_UNSUPPORTED", "stage": "binding", "automatic_replay": False}


@pytest.mark.parametrize("code,category,origin", [
    ("GLOBAL_WRITE_IN_EXECUTOR", "permission", "source_contract"),
    ("GLOBAL_WRITE_IN_WORKER", "permission", "source_contract"),
    ("WORKER_BOOTSTRAP_INVALID", "contract", "source_contract"),
    ("WORKER_SHARED_RESOURCE_INVALID", "contract", "source_contract"),
    ("WORKER_READY_REJECTED", "state", "source_contract"),
    ("WORKER_REGISTRY_REJECTED", "state", "source_contract"),
    ("WORKER_MODE_REQUIRED", "contract", "source_contract"),
    ("WORKER_MODE_INVALID", "contract", "source_contract"),
    ("EXECUTOR_CONTEXT_REQUIRED", "contract", "source_missing_contract"),
    ("EXECUTOR_CONTEXT_CONFLICT", "contract", "source_missing_contract"),
    ("EXECUTOR_TOOL_CALL_INVALID", "contract", "source_missing_contract"),
    ("EXECUTOR_PLATFORM_UNSUPPORTED", "contract", "source_missing_contract"),
])
def test_all_component_error_codes_keep_explicit_nonreplay_policy(code, category, origin):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    assert origin in {"source_contract", "source_missing_contract"}
    contract = error_contract(code)
    assert contract.code == code and contract.category == category
    assert contract.retryable is False
    assert contract.recommended_action == RecoveryAction.REPORT_BLOCKER.value
