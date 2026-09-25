"""原安装权威的跨进程引用与启动复查；临时组件验证，不是插件装卸或真实 TUI 验收。"""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.tooling import background_process_host as host
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests.test_plugin_activation import publication, revocation
from agent_py_agent.tests.test_plugin_install_store import _request


# LLM: 测试临时表中的准备/发布只验证准入，不证明环境和 tools/list 匹配；无用户配置和真实模型。
# 函数用途: 为跨进程引用和 MCP 组件提供同一规范 owner、激活与原安装表。
def plugin_reference(tmp_path, *, plugin_id="sample-peek", phase="preparing"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    owner = resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    entry = store.install(_request(tmp_path, plugin_id=plugin_id, operation_id="install-" + plugin_id)).installation
    plan = PluginEnvironmentPlan("enable-" + plugin_id, plugin_id, entry.package_sha256,
                                 entry.revision, entry.settings_revision, "a" * 64)
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, PluginActivation(plan, "preparing")))
    if phase == "active":
        prepared = store.change_activation(publication(prepared))
    entry = prepared.installation
    return store, entry, PluginActivationRef.from_owner(owner, plugin_id, entry.activation_id), owner


# LLM: 启动参数只定位临时用户的原资源目录；测试收尾只处理返回的精确 session，不发模型请求。
# 函数用途: 构造需要真实激活准入的托管请求，可测试 log 和 stdio 共用的 host 检查。
def activated_request(tmp_path, reference, **changes):
    scope = reference.scope
    return managed_request(tmp_path, activation=reference,
        access_scope=ProcessAccessScope(scope.owner_id, "", scope.owner_home),
        execution_scope=ProcessExecutionScope(owner_home=scope.owner_home),
        store_root=process_session_store_root(scope.owner_home, scope.owner_home), **changes)


def test_reference_reads_same_authority_across_prepare_publish_revoke(tmp_path):
    store, entry, ref, _ = plugin_reference(tmp_path)
    restored = PluginActivationRef.from_payload(json.loads(json.dumps(ref.to_payload())))
    assert restored == ref and restored.require(allow_preparing=True) == entry
    with pytest.raises(PluginInstallationError):
        restored.require()
    prepared = store.change_activation(PluginActivationRequest(entry.activation.plan.operation_id, 1, entry.activation))
    active = store.change_activation(publication(prepared)).installation
    assert restored.require() == restored.require(allow_preparing=True) == active
    store.change_activation(revocation(active))
    for allow in (True, False):
        with pytest.raises(PluginInstallationError):
            restored.require(allow_preparing=allow)


@pytest.mark.parametrize("change", [
    {"root": "relative"}, {"identity": {"provider": "local", "owner_kind": "main", "owner_id": "other"}},
    {"identity": {"provider": "../local", "owner_kind": "user", "owner_id": "a"}},
    {"identity": {"provider": "test", "owner_kind": "unknown", "owner_id": "a"}},
    {"identity": {"provider": "test", "owner_kind": "user", "owner_id": "../a"}},
    {"identity": {}}, {"scope": {}}, {"extra": 1},
])
def test_reference_refuses_malformed_or_renormalized_identity(tmp_path, change):
    _, _, ref, _ = plugin_reference(tmp_path)
    with pytest.raises(ValueError):
        PluginActivationRef.from_payload({**ref.to_payload(), **change})


def test_reference_does_not_create_missing_owner_or_accept_other_owner_home(tmp_path):
    owner = resolve_owner_home(tmp_path / "missing", OwnerIdentity.provider_user("test", "one"))
    ref = PluginActivationRef.from_owner(owner, "plugin", "a" * 64)
    with pytest.raises(PluginInstallationError):
        ref.require()
    assert not owner.root.exists()
    with pytest.raises(ValueError):
        replace(ref, scope=replace(ref.scope, owner_home=str(tmp_path / "other")))


@pytest.mark.parametrize("phases", [frozenset(), frozenset({"revoked"}), frozenset({"active", "revoked"}), {"active"}])
def test_installation_read_cannot_be_told_to_authorize_revoked(tmp_path, phases):
    store, entry, _, _ = plugin_reference(tmp_path)
    with pytest.raises(ValueError):
        store.require_activation(entry.manifest.plugin_id, entry.activation_id, phases=phases)


def test_independent_process_checks_original_revoked_table(tmp_path):
    store, entry, ref, _ = plugin_reference(tmp_path)
    store.change_activation(revocation(entry))
    code = """import json,sys
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.plugin_installation import PluginInstallationError
ref = PluginActivationRef.from_payload(json.load(sys.stdin))
try:
    ref.require(allow_preparing=True)
except PluginInstallationError as error:
    print(error.reason)
else:
    raise SystemExit('unexpected authorization')
"""
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps(ref.to_payload()),
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0 and result.stdout.strip() == "activation_unavailable"


def test_launch_refuses_noncanonical_resource_store_before_creating_it(tmp_path):
    _, _, ref, _ = plugin_reference(tmp_path)
    with pytest.raises(ValueError, match="store conflict"):
        replace(activated_request(tmp_path, ref), store_root=tmp_path / "unrelated")
    assert not (tmp_path / "unrelated").exists()


def test_host_fresh_check_rejects_revocation_after_launcher_precheck(tmp_path, monkeypatch):
    installations, entry, ref, _ = plugin_reference(tmp_path)
    request = activated_request(tmp_path, ref)
    store = ProcessSessionStore(request.store_root)
    session = "bg-pre-spawn-revoked"
    store.write(launch._reservation(request, session))
    spec_path = store.root / ".launches" / f"{session}.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps({"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": session,
        "command_argv": request.argv, "max_log_bytes": 0, "deadline_monotonic": 0,
        "stop_on_launcher_exit": False, "io_mode": "log", "listen_scope": "loopback", "listen_scope_enforce": True, "activation": ref.to_payload()}))
    installations.change_activation(revocation(entry))
    original_popen = host.subprocess.Popen
    def guarded_popen(argv, *args, **kwargs):
        if argv == request.argv:
            pytest.fail("revoked child spawned")
        return original_popen(argv, *args, **kwargs)
    monkeypatch.setattr(host.subprocess, "Popen", guarded_popen)
    assert host.run_background_process_host(store, session, spec_path) == 1
    record = store.load(session).record
    assert record["status"] == "not_started" and not record["child_launch_started"]
    assert record["child_pid"] == 0 and not spec_path.exists()


@pytest.mark.parametrize("kind", ["missing", "wrong-generation", "wrong-owner"])
def test_host_refuses_missing_or_conflicting_activation_reference(tmp_path, kind):
    _, _, ref, _ = plugin_reference(tmp_path)
    request = activated_request(tmp_path, ref)
    payload = ref.to_payload()
    if kind == "missing":
        payload = None
    elif kind == "wrong-generation":
        payload["scope"]["activation_id"] = "b" * 64
    else:
        payload["scope"]["owner_id"] = "other"
    with pytest.raises(ValueError):
        host._require_activation({"activation": payload}, launch._reservation(request, "bg-test"),
                                  ProcessSessionStore(request.store_root))


@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_launcher_revocation_at_each_admission_prevents_handoff(tmp_path, checkpoint):
    installations, entry, ref, _ = plugin_reference(tmp_path)
    checks = []
    def authority():
        checks.append(1)
        if len(checks) == checkpoint:
            installations.change_activation(revocation(entry))
    request = activated_request(tmp_path, ref, authority_check=authority)
    with pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(request)
    error = caught.value
    assert error.cleanup_confirmed and not error.record["handoff_confirmed"]
    assert isinstance(error.cause, PluginInstallationError)
    if checkpoint == 1:
        assert ProcessSessionStore(request.store_root).list_records()[0] == []
    elif checkpoint == 2:
        assert error.record["status"] == "not_started" and error.record["child_pid"] == 0
    else:
        from agent_py_agent.agent.tooling.process_registry import _process_instance_terminated
        assert error.record["child_pid"] > 0
        assert _process_instance_terminated(error.record["child_pid"], error.record["child_pid_birth_token"])
