"""原进程身份三态：不可观测不会变成原后台接手/死亡许可。"""
import errno
import os

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata as metadata


@pytest.mark.parametrize("error", [errno.EPERM, errno.EIO])
def test_original_process_identity_unobservable_is_unknown_not_dead(tmp_path, monkeypatch, error):
    identity = {"host_id": metadata.process_host_id(), "pid": os.getpid(), "start_time": "observed-birth"}
    def unavailable(pid, sig):
        raise OSError(error, "original-observer-unavailable")
    monkeypatch.setattr(metadata.os, "kill", unavailable)
    assert metadata.process_identity_is_live(identity) is None


def test_original_process_identity_missing_birth_is_unknown_without_pid_probe(monkeypatch):
    identity = {"host_id": metadata.process_host_id(), "pid": os.getpid()}
    monkeypatch.setattr(metadata.os, "kill", lambda *args: pytest.fail("missing_birth_is_not_exact_identity"))
    assert metadata.process_identity_is_live(identity) is None


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("state", ["same", "different", "unavailable"])
def test_darwin_kernel_record_always_uses_same_kernel_origin(monkeypatch, managed, state):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: managed)
    monkeypatch.setattr(metadata.sys, "platform", "darwin")
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: None)
    monkeypatch.setattr(native, "process_birth", lambda _pid: {"same": "123:000456", "different": "124:000456", "unavailable": None}[state])
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("kernel origin cannot be compared with PS"))
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": "123:000456"}
    assert metadata.process_identity_is_live(identity) is {"same": True, "different": False, "unavailable": None}[state]


@pytest.mark.parametrize("birth", ["old PS observation", "1:1", ":123456", "0:123456", "١:000001"])
def test_darwin_managed_legacy_or_malformed_birth_remains_unknown(monkeypatch, birth):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: True)
    monkeypatch.setattr(metadata.sys, "platform", "darwin")
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: None)
    monkeypatch.setattr(native, "process_birth", lambda _pid: pytest.fail("legacy PS is not a kernel observation"))
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("managed identity must not spawn PS"))
    assert metadata.process_identity_is_live({"host_id": metadata.process_host_id(), "pid": 123, "start_time": birth}) is None


@pytest.mark.parametrize("platform,birth", [("darwin", "123:000456"), ("linux", 1234)])
def test_managed_identity_producer_uses_original_kernel_birth_without_process_spawn(monkeypatch, platform, birth):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: True)
    monkeypatch.setattr(metadata.sys, "platform", platform)
    monkeypatch.setattr(native, "process_birth", lambda pid: birth if pid == 123 else pytest.fail("foreign pid"))
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("managed production cannot spawn PS"))
    assert metadata.build_process_identity(123) == {"host_id": metadata.process_host_id(), "pid": 123, "start_time": birth}


def test_count_zero_identity_producer_keeps_original_observer(monkeypatch):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: False)
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda pid: "original-count-zero" if pid == 123 else pytest.fail("foreign pid"))
    monkeypatch.setattr(native, "process_birth", lambda _pid: pytest.fail("Count0 production must retain original observer"))
    assert metadata.build_process_identity(123)["start_time"] == "original-count-zero"


@pytest.mark.parametrize("damage", ["bool_pid", "string_pid", "float_pid", "missing_birth", "bool_birth", "empty_birth", "foreign_host"])
def test_invalid_or_foreign_original_identity_does_not_probe_pid(monkeypatch, damage):
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": 42}
    if damage == "bool_pid":
        identity["pid"] = True
    elif damage == "string_pid":
        identity["pid"] = "123"
    elif damage == "float_pid":
        identity["pid"] = 123.0
    elif damage == "missing_birth":
        identity.pop("start_time")
    elif damage == "bool_birth":
        identity["start_time"] = True
    elif damage == "empty_birth":
        identity["start_time"] = ""
    else:
        identity["host_id"] = "foreign-host"
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: pytest.fail("invalid identity is not a process observation"))
    assert metadata.process_identity_is_live(identity) is None


def test_only_exact_esrch_proves_original_pid_missing(monkeypatch):
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": 42}
    def missing(*_args):
        raise OSError(errno.ESRCH, "synthetic process missing")
    monkeypatch.setattr(metadata.os, "kill", missing)
    assert metadata.process_identity_is_live(identity) is False
