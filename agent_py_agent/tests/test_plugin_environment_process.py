"""环境命令的取消与退出回执替身测试，不启动插件或真实模型。"""

import os
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_environment_process as module
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.plugin_environment_fixtures import environment_operation
from agent_py_agent.tests.plugin_wheel_fixtures import make_wheel, package_wheels


def test_preparation_scrubs_secrets_and_all_runtime_injection_variables(tmp_path, monkeypatch):
    supplied = {
        "MINIMAX_API_KEY": "placeholder", "SOME_TOKEN": "placeholder", "HOME": str(tmp_path),
        "PATH": "/usr/bin", "PYTHONPATH": "external", "PIP_INDEX_URL": "https://example.invalid",
        "LD_PRELOAD": "external", "DYLD_INSERT_LIBRARIES": "external", "CONDA_PREFIX": "external",
        "UV_INDEX": "external", "VIRTUAL_ENV": "external", "__PYVENV_LAUNCHER__": "external",
    }
    monkeypatch.setattr(module.os, "environ", supplied)
    result = module.preparation_environment(tmp_path / "temporary")
    assert result == {
        "HOME": str(tmp_path), "PATH": "/usr/bin", "PIP_CONFIG_FILE": os.devnull,
        "TMPDIR": str(tmp_path / "temporary"), "TEMP": str(tmp_path / "temporary"), "TMP": str(tmp_path / "temporary"),
    }
    assert supplied["MINIMAX_API_KEY"] == "placeholder"


@pytest.mark.parametrize("confirmed", [True, False])
def test_cancel_after_handoff_stops_only_original_persistent_session(tmp_path, monkeypatch, confirmed):
    operation = environment_operation(resolve_owner_home(tmp_path), package_wheels(make_wheel()))
    process = SimpleNamespace()
    record = {"session_id": "exact-session"}
    hosted = SimpleNamespace(record=record, process=process, store_root=tmp_path / "original-store")
    store, calls = object(), []
    monkeypatch.setattr(module, "start_background_process", lambda *a, **kw: hosted)
    monkeypatch.setattr(module, "ProcessSessionStore", lambda root: store)

    def interrupted(*_):
        raise ToolCancelled("test interrupted")

    def stop(given_store, selected, **kwargs):
        calls.append((given_store, selected, kwargs))
        return SimpleNamespace(confirmed=confirmed)

    monkeypatch.setattr(module, "_wait_for_preparation", interrupted)
    monkeypatch.setattr(module, "stop_process_session", stop)
    with pytest.raises(ToolCancelled if confirmed else module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={},
                                       deadline=module.time.monotonic() + 5, operation=operation, stage="venv")
    assert calls == [(store, record, {"host_process": process})]
    if not confirmed:
        assert error.value.reason == "process_exit_unknown" and not error.value.exit_confirmed


def test_cancelled_before_spawn_creates_no_process_or_log(tmp_path, monkeypatch):
    token = CancellationToken()
    token.cancel()
    monkeypatch.setattr(module, "start_background_process", lambda *a, **kw: pytest.fail("不得启动"))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=100,
                                       operation=None, stage="venv")
    assert not (tmp_path / "venv.log").exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "unknown", "host_gone"])
def test_wait_never_treats_missing_or_unknown_authority_as_success(kind):
    record = {"status": "running"}
    if kind == "unknown":
        record["status"] = "unknown"
    store = SimpleNamespace(load=lambda _: SimpleNamespace(
        record={} if kind == "missing" else record, load_error={"test": True} if kind == "corrupt" else None,
    ))
    hosted = SimpleNamespace(record={"session_id": "original"}, process=SimpleNamespace(poll=lambda: 0))
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module._wait_for_preparation(hosted, store, SimpleNamespace(authorize=lambda: None), module.time.monotonic() + 5)
    assert not error.value.exit_confirmed


def test_launch_request_carries_exact_identity_and_finite_host_lifetime(tmp_path):
    operation = environment_operation(resolve_owner_home(tmp_path), package_wheels(make_wheel()))
    deadline = module.time.monotonic() + 5
    request = operation.launch_request(("host-python",), tmp_path, {}, deadline, tmp_path / "probe.log")
    assert request.deadline_monotonic == deadline and request.stop_on_launcher_exit
    assert request.completion_target == {}
    assert request.execution_scope.run_id == operation.binding.run_id
    assert request.execution_scope.attempt_id == operation.binding.attempt_id
    assert request.execution_scope.root_task_id == operation.binding.task_id
    assert request.execution_scope.thread_id == operation.binding.request.thread_id
    assert request.access_scope.owner_id == operation.owner.owner_id
    assert request.execution_scope.owner_home == str(operation.owner.home_dir)
    request.authority_check()


@pytest.mark.parametrize("second", ["exited", "killed", "running", "unknown"])
def test_host_exit_between_read_and_poll_rereads_same_original_authority(second):
    records = iter(({"status": "running"}, {"status": second, "exit_code": 0}))
    seen = []

    def load(session_id):
        seen.append(session_id)
        return SimpleNamespace(record=next(records), load_error=None)

    hosted = SimpleNamespace(record={"session_id": "original"}, process=SimpleNamespace(poll=lambda: 0))
    arguments = (hosted, SimpleNamespace(load=load), SimpleNamespace(authorize=lambda: None), module.time.monotonic() + 5)
    if second in {"exited", "killed"}:
        assert module._wait_for_preparation(*arguments)["status"] == second
    else:
        with pytest.raises(module.EnvironmentPreparationError):
            module._wait_for_preparation(*arguments)
    assert seen == ["original", "original"]


def test_deadline_after_handoff_keeps_started_fact(tmp_path, monkeypatch):
    operation = environment_operation(resolve_owner_home(tmp_path), package_wheels(make_wheel()))
    hosted = SimpleNamespace(record={"session_id": "original"}, process=object(), store_root=tmp_path)
    monkeypatch.setattr(module, "start_background_process", lambda *a, **kw: hosted)
    monkeypatch.setattr(module, "_wait_for_preparation", lambda *_: (_ for _ in ()).throw(
        module.EnvironmentPreparationError("preparation_timeout"),
    ))
    monkeypatch.setattr(module, "stop_process_session", lambda *a, **kw: SimpleNamespace(confirmed=True))
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={},
                                       deadline=module.time.monotonic() + 5, operation=operation, stage="venv")
    assert error.value.reason == "preparation_timeout" and error.value.started and error.value.exit_confirmed


def test_real_handoff_then_operation_revocation_stops_exact_child(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.tooling.process_registry import _process_instance_terminated

    operation = environment_operation(resolve_owner_home(tmp_path), package_wheels(make_wheel()))
    repo = RuntimeRepository(runtime_db_path(operation.owner.home_dir))
    launch, observed = module.start_background_process, []

    def revoke_after_handoff(*args, **kwargs):
        hosted = launch(*args, **kwargs)
        observed.append(hosted)
        with repo._runtime_connection() as conn:
            conn.execute("UPDATE agent_attempts SET status = 'cancelled' WHERE attempt_id = ?",
                         (operation.binding.attempt_id,))
            conn.commit()
        return hosted

    monkeypatch.setattr(module, "start_background_process", revoke_after_handoff)
    with pytest.raises(AuthorityContextMissing):
        module.run_environment_process((sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                                       cwd=tmp_path, environment=module.preparation_environment(tmp_path),
                                       deadline=module.time.monotonic() + 10, operation=operation, stage="venv")
    assert len(observed) == 1
    hosted = observed[0]
    record = module.ProcessSessionStore(hosted.store_root).load(hosted.record["session_id"]).record
    assert record["status"] == "killed" and record["stop_requested"]
    assert _process_instance_terminated(record["child_pid"], record["child_pid_birth_token"])
