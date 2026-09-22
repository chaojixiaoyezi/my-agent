"""原管理停用与真实临时进程交错；只有组件验收，不运行产品 TUI 或模型。"""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPStdioClient
from agent_py_agent.agent.tooling.process_registry import _process_instance_terminated
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests.plugin_deactivation_fixtures import activation_component
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _config
from agent_py_agent.tests.test_plugin_activation import publication
from agent_py_agent.tests.test_plugin_activation_ref import activated_request
from agent_py_agent.tests.test_plugin_deactivation import installed_manager
from agent_py_agent.tests.test_plugin_install_store import _request


# LLM: 仅按本次临时文件或持久状态等待明确交错，超时失败，不用长睡眠作为验收结果。
# 函数用途: 同步测试创建的独立进程和管理线程。
def wait_until(check):
    deadline = time.monotonic() + 8
    while not check():
        assert time.monotonic() < deadline, "component did not reach expected state"
        time.sleep(0.01)


def test_disable_does_not_wait_for_blocked_business_call(tmp_path):
    service = installed_manager(tmp_path)
    script = _ECHO_SERVER.replace('elif method == "tools/call":',
        'elif method == "tools/call":\n            open("call-entered", "w").close()\n            import time; time.sleep(20)')
    with activation_component(service, tmp_path, preparation=False, script=script) as state, ThreadPoolExecutor(1) as pool:
        call = pool.submit(state["client"].call_tool, "echo", {"text": "blocked"})
        wait_until(lambda: (tmp_path / "call-entered").exists())
        started = time.monotonic()
        result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert time.monotonic() - started < 6
        assert result["state"] == "succeeded", result
        with pytest.raises(MCPError):
            call.result(timeout=2)


def test_disable_keeps_other_plugin_and_unrelated_task_running(tmp_path):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        record = state["client"].connection().binding.managed.hosted.record
        root = state["client"].connection().binding.managed.hosted.store_root
        other = service.installations.install(_request(tmp_path, plugin_id="other", operation_id="install-other")).installation
        plan = PluginEnvironmentPlan("enable-other", "other", other.package_sha256, other.revision, 0, "a" * 64)
        prepared = service.installations.change_activation(PluginActivationRequest(plan.operation_id, other.revision,
                                                                                    PluginActivation(plan, "preparing")))
        other = service.installations.change_activation(publication(prepared)).installation
        ref = PluginActivationRef.from_owner(service.context.owner, "other", other.activation_id)
        other_client = MCPStdioClient(_config(_ECHO_SERVER, cwd=str(tmp_path)), activation=ref)
        task = launch.start_background_process(managed_request(tmp_path / "independent", store_root=root))
        try:
            other_client.start()
            result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
            assert result["state"] == "succeeded"
            assert [row["session_id"] for row in result["details"]["sessions"]] == [record["session_id"]]
            assert other_client.call_tool("echo", {"text": "still running"})["content"] == "still running"
            assert not _process_instance_terminated(task.record["child_pid"], task.record["child_pid_birth_token"])
            assert not ProcessSessionStore(root).load(task.record["session_id"]).record["stop_requested"]
        finally:
            other_client.stop()
            stop_process_session(ProcessSessionStore(root), task.record, host_process=task.process)
            task.process.wait(timeout=3)


_PAUSED_HOST = '''
import sys, time
from pathlib import Path
from agent_py_agent.agent.tooling import background_process_host as host
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
root, session, spec, gate, go, order = sys.argv[1:]
original = host._require_activation
def paused(specification, record, store):
    if order == "after":
        original(specification, record, store)
    Path(gate).touch()
    deadline = time.monotonic() + 12
    while not Path(go).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test gate")
        time.sleep(.01)
    if order == "before":
        original(specification, record, store)
host._require_activation = paused
raise SystemExit(host.run_background_process_host(ProcessSessionStore(root), session, Path(spec)))
'''


@pytest.mark.parametrize("order", ["before", "after"])
def test_independent_host_admission_and_management_revoke_share_resource_boundary(tmp_path, order):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        request = activated_request(tmp_path / "racing-host", state["reference"])
        store, session = ProcessSessionStore(request.store_root), "bg-admission-race"
        store.write(launch._reservation(request, session))
        spec = store.root / ".launches" / f"{session}.json"
        spec.parent.mkdir(exist_ok=True)
        spec.write_text(json.dumps({"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": session,
            "command_argv": request.argv, "max_log_bytes": request.max_log_bytes,
            "deadline_monotonic": time.monotonic() + 20, "stop_on_launcher_exit": True,
            "io_mode": "log", "activation": request.activation.to_payload()}))
        gate, go = tmp_path / "host-admission", tmp_path / "allow-host"
        host = subprocess.Popen([sys.executable, "-c", _PAUSED_HOST, str(store.root), session, str(spec),
                                 str(gate), str(go), order], start_new_session=True)
        try:
            wait_until(gate.exists)
            with ThreadPoolExecutor(1) as pool:
                revision = service.catalog().revision
                stop = pool.submit(service.command, "/plugins disable sample-peek", revision=revision, request_id="disable")
                try:
                    wait_until(lambda: service.installations.snapshot()[0].activation.phase == "revoked")
                    assert not stop.done()  # 管理已撤销权限，冻结仍等待 host 持有的原锁。
                finally:
                    go.touch()
                result = stop.result(timeout=8)
            host.wait(timeout=3)
            assert result["state"] == "succeeded", result
            current = store.load(session).record
            assert current["stop_requested"] and current["child_launch_started"] is (order == "after")
            assert session in {row["session_id"] for row in result["details"]["sessions"]}
            assert not current["child_pid"] or _process_instance_terminated(current["child_pid"], current["child_pid_birth_token"])
        finally:
            go.touch()
            current = store.load(session).record
            if current:
                stop_process_session(store, current, host_process=host)
            host.wait(timeout=3)
