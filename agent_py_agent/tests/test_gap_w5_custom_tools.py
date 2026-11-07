"""W5 自定义工具：真实包检查、原生接线形状和确定性交错，不联网。"""
from __future__ import annotations

import hashlib
import io
import json
import shlex
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import gap158_w5_custom_tools as tools
from agent_py_agent.agent.business_spaces import v13_w11_plugins as plugins
from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_management import PluginManagement, PluginManagementContext
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.settings import parameter_changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.tooling.models import ToolModelHints, ToolModelSpec
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)

native_management = plugins.management


class _Catalog:
    """只替代宿主目录后端；字段与 ToolRegistry.runtime_snapshot 一致。"""
    def __init__(self):
        self.tools = {}
        self.catalog_categories = []
        self.catalog_deferred_categories = []
        self._mcp_prepare_lock = threading.RLock()
        for name, category in (("read_a", "files"), ("read_b", "files"), ("status", "system")):
            spec = ToolModelSpec(name, "目录工具", {"type": "object"}, hints=ToolModelHints(category=category))
            self.tools[name] = SimpleNamespace(model_spec=spec)

    def runtime_snapshot(self):
        specs = tuple(tool.model_spec for tool in self.tools.values())
        return SimpleNamespace(catalog_specs=specs, available_tool_names=frozenset(self.tools))


class _Manager(PluginManagement):
    """只替代安装/启停 I/O；调用线程、HostCommand 身份仍使用生产实现。"""
    def __init__(self, context):
        super().__init__(context)
        self.entries, self.calls = {}, []
        self.installations = SimpleNamespace(snapshot=lambda: tuple(self.entries.values()))
        self.authorization = True
        self.problem = ""
        self.block_install = None
        self.install_error = None

    def install_package_bytes(self, content, *, operation_id, expected_revision=0):
        package = inspect_plugin_package(content)
        self.calls.append(("install", operation_id, expected_revision, package))
        if self.block_install:
            entered, release = self.block_install
            entered.set()
            assert release.wait(5), "测试交错没有释放"
        if self.install_error:
            raise self.install_error
        old = self.entries.get(package.manifest.plugin_id)
        assert expected_revision == (old.revision if old else 0)
        assert old is None or not old.enabled
        entry = SimpleNamespace(manifest=package.manifest, package_sha256=package.sha256,
                                revision=expected_revision + 1, enabled=False, activation_id="",
                                installation_ref=hashlib.sha256(operation_id.encode()).hexdigest(),
                                installation_scope='private')
        self.entries[package.manifest.plugin_id] = entry
        return SimpleNamespace(installation=entry)

    def package_lifecycle(self, plugin_id, action, *, operation_id):
        self.calls.append((action, operation_id))
        entry = self.entries[plugin_id]
        if action == "enable" and not self.authorization:
            return entry, {"state": "failed", "ok": False, "error_code": "PLUGIN_CONFIRMATION_REQUIRED",
                           "details": {"reason": "confirmation_required"}}
        if action == "uninstall":
            del self.entries[plugin_id]
            return None, {"state": "succeeded", "ok": True}
        entry.enabled = action == "enable"
        entry.revision += 1
        entry.activation_id = "activation-" + str(entry.revision) if entry.enabled else ""
        return entry, {"state": "succeeded", "ok": True}

    def command(self, text, *, revision, request_id):
        # 替身也消费原命令的冻结目录修订；真实 Store/卸载 CAS 另有原生交错用例。
        if revision != self.catalog().revision:
            return {'state': 'failed', 'ok': False, 'error_code': 'PLUGIN_CATALOG_STALE',
                    'effect_outcome': 'not_started'}
        _, action, plugin_id = shlex.split(text)
        _, outcome = self.package_lifecycle(plugin_id, 'uninstall' if action == 'remove' else action,
                                            operation_id=request_id.removesuffix('-0'))
        if outcome.get('state') == 'succeeded' and action != 'remove':
            thread = self._thread(create=True)
            self.entries[plugin_id].last_commit = SimpleNamespace(
                operation_id=self._identity(thread.thread_id, request_id).operation_id)
        return outcome

    def _business_sandbox_problem(self, entry):
        return self.problem


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    managers = {}

    def access(call, record=None):
        if not getattr(call.context.host, "allowed", True):
            raise HostAPIError("BUSINESS_ACCESS_DENIED", 403)
        return True

    def admin(call):
        access(call)
        if not call.context.host.admin:
            raise HostAPIError("BUSINESS_ACCESS_DENIED", 403)
        return True

    monkeypatch.setattr(v13, "v13_require_access", access)
    monkeypatch.setattr(v13, "v13_require_admin", admin)

    def factory(action="create", params=None, *, owner=None, key=None, host=None):
        owner = owner or resolve_owner_home(root)
        if host is None:
            config_path = owner.home_dir / "config" / "agent_config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if not config_path.exists():
                config_path.write_text("# 隔离测试配置\n")
            config = load_config(config_path)
            config.custom_tools_enabled = True
            config.enable_tools = config.enable_plugins = True
            store = ConversationStore(owner.home_dir / "conversations", initialize=False)
            paths = home_paths_with_owner(home_paths(root), owner)
            host = SimpleNamespace(config=config, tools=_Catalog(), admin=True, allowed=True,
                                   home_paths=paths, conversation_store=store, business_native_admin=lambda: True)
        if owner.owner_id not in managers:
            context = PluginManagementContext(
                owner, "tester", "client_business", "test", host.conversation_store.threads,
                owner.home_dir, PathAccessPolicy.from_values(mode="full"), True, True)
            managers[owner.owner_id] = _Manager(context)
        call = v13.V13Call("custom_tools." + action, params or {}, "request-" + uuid4().hex,
                           owner.owner_id, "tester", SimpleNamespace(host=host),
                           SimpleNamespace(owner_id=owner.owner_id, host=host), owner.home_dir,
                           key or "key-" + uuid4().hex, clock=lambda: 100.0)
        return call

    monkeypatch.setattr(plugins, "management", lambda call: managers[call.owner_id])
    return SimpleNamespace(call=factory, managers=managers, root=root, monkeypatch=monkeypatch)


def _params(**changes):
    return {"name": "echo", "description": "回显 JSON", "language": "python",
            "entry_script": "import sys\nprint(sys.stdin.read())\n",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                             "required": ["text"], "additionalProperties": False}, **changes}


def _created(env, **changes):
    call = env.call(params=_params(**changes))
    return call, tools.create(call)["tool"]


def _same(env, call, action, params, **kwargs):
    return env.call(action, params, host=call.context.host, **kwargs)


def _error(code, fn):
    with pytest.raises((HostAPIError, BusinessError)) as caught:
        fn()
    assert caught.value.code == code
    return caught.value


@pytest.mark.parametrize("language,member", [("python", "entry.py"), ("shell", "entry.sh")])
def test_generation_native_package_contract_and_install_delegation(env, language, member):
    call, result = _created(env, language=language)
    manager = env.managers[call.owner_id]
    assert result["state"] == "installed"
    assert [row[0] for row in manager.calls] == ["install", "enable"]
    package = manager.calls[0][3]
    assert result["sha256"] == package.sha256
    manifest = package.manifest
    assert manifest.entry.command == "server.py" and manifest.entry.interpreter == "python3"
    assert manifest.tools[0].requested_effect == "dangerous"
    assert manifest.tools[0].input_schema == _params()["input_schema"]
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as archive:
        assert set(archive.namelist()) == {"plugin.json", "server.py", member}
        assert archive.read(member).decode() == _params()["entry_script"]
        assert all(info.external_attr >> 16 & 0o777 == 0o644 for info in archive.infolist())
    record = v13_store_for(call, "owner").get("custom_tools", result["tool_id"])
    assert record["owner_id"] == call.owner_id and "_busy" not in record
    assert tools._package(result["plugin_id"], record["definition"]) == package.archive_bytes


def test_create_pending_authorization_never_constructs_confirmation(env):
    call = env.call(params=_params())
    manager = env.managers[call.owner_id]
    manager.authorization = False
    result = tools.create(call)["tool"]
    assert result["state"] == "pending_authorization"
    assert not manager.entries[result["plugin_id"]].enabled
    assert manager.calls[1] == ("enable", tools._operation(call) + "-enable")
    assert tools.create(call)["tool"] == result
    assert len(manager.calls) == 2
    _error("STATE_CONFLICT", lambda: tools.test(_same(env, call, "test", {"tool_id": result["tool_id"], "args": {"text": "x"}})))


def test_create_idempotency_collision_limits_and_owner_isolation(env, monkeypatch):
    call, result = _created(env)
    assert tools.create(call)["tool"] == result
    assert len(env.managers[call.owner_id].calls) == 2
    _error("IDEMPOTENCY_CONFLICT", lambda: tools.create(replace(call, params=_params(description="其他说明"))))
    _error("STATE_CONFLICT", lambda: tools.create(_same(env, call, "create", _params())))
    monkeypatch.setattr(tools, "CUSTOM_TOOLS_MAX_PER_OWNER", 1)
    _error("LIMIT_EXCEEDED", lambda: tools.create(_same(env, call, "create", _params(name="another"))))
    other = resolve_owner_home(env.root, OwnerIdentity.provider_user("test", "other"))
    other_call = env.call(params=_params(), owner=other)
    other_result = tools.create(other_call)["tool"]
    assert other_result["tool_id"] != result["tool_id"]
    _error("RESOURCE_NOT_FOUND", lambda: tools.update(replace(other_call, action="custom_tools.update", params={
        "tool_id": result["tool_id"], "expected_sha256": result["sha256"]}, idempotency_key="foreign-update-1234")))
    assert tools.delete(replace(other_call, action="custom_tools.delete", params={
        "tool_id": result["tool_id"], "expected_sha256": result["sha256"]}, idempotency_key="foreign-delete-1234")) == {"deleted": False}


def test_update_and_delete_real_lifecycle_shape_and_sha_cas(env):
    call, result = _created(env)
    update = _same(env, call, "update", {"tool_id": result["tool_id"], "expected_sha256": result["sha256"],
                                       "entry_script": "print('new')"})
    changed = tools.update(update)["tool"]
    assert changed["sha256"] != result["sha256"]
    manager = env.managers[call.owner_id]
    assert [row[0] for row in manager.calls] == ["install", "enable", "disable", "install", "enable"]
    assert manager.calls[3][2] == 3
    assert tools.update(update)["tool"] == changed and len(manager.calls) == 5
    stale = _same(env, call, "delete", {"tool_id": result["tool_id"], "expected_sha256": result["sha256"]})
    _error("REVISION_CONFLICT", lambda: tools.delete(stale))
    assert len(manager.calls) == 5
    delete = _same(env, call, "delete", {"tool_id": result["tool_id"], "expected_sha256": changed["sha256"]})
    assert tools.delete(delete) == {"deleted": True}
    assert tools.delete(delete) == {"deleted": True}
    assert [row[0] for row in manager.calls[-2:]] == ["disable", "uninstall"]
    assert v13_store_for(call, "owner").list("custom_tools") == []
    assert tools.delete(replace(delete, idempotency_key="delete-missing-1234")) == {"deleted": False}


@pytest.mark.parametrize("native_ok", [True, False])
def test_test_execution_door_identity_arguments_result_mapping_and_replay(env, monkeypatch, native_ok):
    call, result = _created(env)
    passed = []

    def invoke(context, repo, request, select):
        selection = select()
        assert selection.tool_name == plugin_tool_name(result["plugin_id"], "run")
        assert selection.arguments == {"text": "hello"}
        assert request.owner_id == call.owner_id and request.actor_id == call.subject_id
        assert request.command_name == selection.tool_name
        assert context is env.managers[call.owner_id].context
        passed.append(request)
        return {"state": "succeeded" if native_ok else "failed", "ok": native_ok,
                "output": "x" * 5000, "error_code": "" if native_ok else "TOOL_EXECUTION_FAILED"}

    monkeypatch.setattr(tools, "execute_plugin_invocation", invoke)
    testing = _same(env, call, "test", {"tool_id": result["tool_id"], "args": {"text": "hello"}})
    mapped = tools.test(testing)
    assert mapped["ok"] is native_ok and len(mapped["result_preview"]) == 4000
    assert mapped["duration_ms"] >= 0
    assert mapped["error_code"] == ("" if native_ok else "TOOL_EXECUTION_FAILED")
    assert tools.test(testing) == mapped and len(passed) == 1


@pytest.mark.parametrize("constraint,code", [("schema", "INVALID_SCHEMA"), ("disabled", "BUSINESS_ACCESS_DENIED"),
                                             ("sandbox", "HOST_NOT_READY")])
def test_test_checks_schema_policy_sandbox_before_execution(env, monkeypatch, constraint, code):
    call, result = _created(env)
    manager = env.managers[call.owner_id]
    args = {"text": "x"}
    if constraint == "schema":
        args = {"text": 1}
    elif constraint == "disabled":
        manager.context = replace(manager.context, business_allowed=False)
    else:
        manager.problem = "sandbox_unavailable"
    monkeypatch.setattr(tools, "execute_plugin_invocation", lambda *a, **kw: pytest.fail("拒绝后不能执行"))
    _error(code, lambda: tools.test(_same(env, call, "test", {"tool_id": result["tool_id"], "args": args})))
    record = v13_store_for(call, "owner").get("custom_tools", result["tool_id"])
    assert "_busy" not in record


def test_unknown_execution_is_not_repeated_and_blocks_mutation(env, monkeypatch):
    call, result = _created(env)
    received = []
    monkeypatch.setattr(tools, "execute_plugin_invocation", lambda *a: received.append(a) or {
        "state": "outcome_unknown", "ok": False})
    testing = _same(env, call, "test", {"tool_id": result["tool_id"], "args": {"text": "x"}})
    _error("OUTCOME_UNKNOWN", lambda: tools.test(testing))
    _error("OUTCOME_UNKNOWN", lambda: tools.test(testing))
    _error("OUTCOME_UNKNOWN", lambda: tools.delete(_same(env, call, "delete", {
        "tool_id": result["tool_id"], "expected_sha256": result["sha256"]})))
    assert len(received) == 1


def test_concurrent_owner_cas_prevents_update_delete_crossing_native_effects(env):
    call, result = _created(env)
    entered, release = threading.Event(), threading.Event()
    manager = env.managers[call.owner_id]
    manager.block_install = (entered, release)
    updating = _same(env, call, "update", {"tool_id": result["tool_id"], "expected_sha256": result["sha256"],
                                         "entry_script": "print('changed')"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(tools.update, updating)
        try:
            assert entered.wait(5), "没有到达生产安装接线入口"
            _error("OUTCOME_UNKNOWN", lambda: tools.delete(_same(env, call, "delete", {
                "tool_id": result["tool_id"], "expected_sha256": result["sha256"]})))
        finally:
            release.set()
        changed = running.result(timeout=5)["tool"]
    assert changed["sha256"] != result["sha256"]
    assert "uninstall" not in [row[0] for row in manager.calls]
    assert "_busy" not in v13_store_for(call, "owner").get("custom_tools", result["tool_id"])


def test_concurrent_duplicate_creation_runs_native_install_once(env):
    call = env.call(params=_params())
    entered, release = threading.Event(), threading.Event()
    manager = env.managers[call.owner_id]
    manager.block_install = (entered, release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(tools.create, call)
        try:
            assert entered.wait(5)
            _error("OUTCOME_UNKNOWN", lambda: tools.create(call))
        finally:
            release.set()
        result = running.result(timeout=5)
    assert tools.create(call) == result
    assert [row[0] for row in manager.calls] == ["install", "enable"]


def test_feature_access_gates_are_live_and_do_not_write(env):
    call = env.call(params=_params())
    call.context.host.config.custom_tools_enabled = False
    _error("HOST_NOT_READY", lambda: tools.create(call))
    assert env.managers[call.owner_id].calls == []
    assert v13_store_for(call, "owner").list("custom_tools") == []
    call.context.host.config.custom_tools_enabled = True
    call.context.host.allowed = False
    _error("BUSINESS_ACCESS_DENIED", lambda: tools.create(call))
    assert env.managers[call.owner_id].calls == []


def test_generation_invalid_schema_is_rejected_before_native_install(env):
    call = env.call(params=_params(input_schema={"type": "array"}))
    _error("INVALID_SCHEMA", lambda: tools.create(call))
    assert env.managers[call.owner_id].calls == []
    assert v13_store_for(call, "owner").list("custom_tools") == []


def test_native_plugin_install_and_authorization_flow_without_network(env, monkeypatch):
    call = env.call(params=_params())
    # 该用例专验 main 的明确 wide 候选预览，不把平台沙箱不可用误作确认结果；仍不提交确认。
    call.context.host.config.plugin_legacy_sandbox_default = False
    # 这里只替代业务鉴权夹具；原生 manager、安装 Store、审批工具和 runtime.db 全部真实。
    monkeypatch.setattr(plugins, "management", native_management)
    observed = []
    command = PluginManagement.command

    def observe(manager, *args, **kwargs):
        result = command(manager, *args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(PluginManagement, "command", observe)
    result = tools.create(call)["tool"]
    assert observed
    assert result["state"] == "pending_authorization", observed
    manager = native_management(call)
    entries = manager.installations.snapshot()
    assert len(entries) == 1 and entries[0].package_sha256 == result["sha256"]
    assert entries[0].enabled is False and not entries[0].activation_id
    record = v13_store_for(call, "owner").get("custom_tools", result["tool_id"])
    assert record["pending_authorization"] is True and "_busy" not in record
    assert tools.create(call)["tool"] == result


def test_python_detect_fixed_argv_bounded_timeout_dedup_and_bad_probe(env, monkeypatch):
    call = env.call("python_detect")
    monkeypatch.setattr(tools.sys, "executable", "/fixed/python")
    candidates = {"python3": "/fixed/python", "python3.11": "/fixed/timeout", "python3.12": "/fixed/invalid",
                  "python3.13": "/fixed/fail", "python3.14t": "/fixed/free"}
    monkeypatch.setattr(tools.shutil, "which", lambda name: candidates.get(name))
    probes = []

    def run(argv, **kwargs):
        probes.append((argv, kwargs))
        assert argv[1:3] == ["-I", "-c"] and kwargs["timeout"] == tools.CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS
        assert "shell" not in kwargs and kwargs["check"] is False
        if argv[0].endswith("timeout"):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if argv[0].endswith("invalid"):
            return SimpleNamespace(returncode=0, stdout="not json")
        return SimpleNamespace(returncode=int(argv[0].endswith("fail")), stdout=json.dumps({
            "version": "3.14.0", "implementation": "CPython", "free_threaded": argv[0].endswith("free")}))

    monkeypatch.setattr(tools.subprocess, "run", run)
    result = tools.python_detect(call)["interpreters"]
    assert [row["path"] for row in result] == ["/fixed/python", "/fixed/free"]
    assert result[-1]["free_threaded"] is True and result[0]["free_threaded"] is False
    assert len(probes) == 5


@pytest.fixture
def batch_center(monkeypatch):
    """参数中心端口替身：严格核两字段、原路径及一次 SHA CAS，不冒充真实参数事务验收。"""
    calls = []

    def batch(values, *, paths, origin):
        assert set(values) == {"tool_catalog_categories", "tool_catalog_deferred_categories"}
        before = paths.user_path.read_bytes()
        if paths.expected_revision != hashlib.sha256(before).hexdigest():
            return {"ok": False, "code": "CONFIG_REVISION_CONFLICT"}
        calls.append((values, paths, origin))
        paths.user_path.write_text("\n".join(key + ": " + repr(value) for key, value in values.items()) + "\n")
        return {"ok": True, "revision": hashlib.sha256(paths.user_path.read_bytes()).hexdigest()}

    monkeypatch.setattr(parameter_changes, "set_parameters", batch, raising=False)
    return calls


def test_configure_and_reset_control_actual_category_affect_siblings_in_one_batch(env, batch_center):
    call = env.call("configure", {"tool": "read_a", "category": "files", "deferred": True})
    changed = tools.configure(call)
    assert changed["row"]["category"] == "files" and changed["row"]["deferred"] is True
    rows = {row["tool"]: row for row in tools.list_tools(_same(env, call, "list", {}))["items"]}
    assert rows["read_a"]["deferred"] is rows["read_b"]["deferred"] is True
    assert rows["read_a"]["visible"] is rows["read_b"]["visible"] is False
    assert rows["status"]["visible"] is False
    assert len(batch_center) == 1
    assert batch_center[0][0] == {"tool_catalog_categories": ["files"],
                                  "tool_catalog_deferred_categories": [*AgentConfig().tool_catalog_deferred_categories, "files"]}
    assert tools.configure(call) == changed and len(batch_center) == 1
    resetting = _same(env, call, "reset", {"tool": "read_a", "expected_config_revision": changed["config_revision"]})
    reset = tools.reset(resetting)
    assert reset["row"]["category"] == "files" and reset["row"]["deferred"] is False
    after = {row["tool"]: row for row in tools.list_tools(_same(env, call, "list", {}))["items"]}
    assert after["read_b"]["deferred"] is False and after["read_b"]["visible"] is True
    assert after["status"]["visible"] is True
    assert len(batch_center) == 2


def test_configuration_cas_admin_missing_and_category_not_a_rename(env, batch_center):
    call = env.call("configure", {"tool": "read_a", "category": "renamed"})
    _error("INVALID_SCHEMA", lambda: tools.configure(call))
    _error("RESOURCE_NOT_FOUND", lambda: tools.configure(_same(env, call, "configure", {"tool": "absent"})))
    _error("REVISION_CONFLICT", lambda: tools.configure(_same(env, call, "configure", {
        "tool": "read_a", "deferred": True, "expected_config_revision": "stale"})))
    call.context.host.admin = False
    _error("BUSINESS_ACCESS_DENIED", lambda: tools.reset(_same(env, call, "reset", {"tool": "read_a"})))
    assert batch_center == []


def test_catalog_pagination_source_and_cursor_owner_binding(env):
    call, result = _created(env)
    first_call = _same(env, call, "list", {"limit": 1})
    first = tools.list_tools(first_call)
    assert len(first["items"]) == 1 and first["next_cursor"] and first["coverage"] == "partial"
    second = tools.list_tools(replace(first_call, params={"limit": 100, "cursor": first["next_cursor"]}))
    assert len(first["items"] + second["items"]) == 4
    assert second["coverage"] == "complete" and second["next_cursor"] == ""
    custom = tools.list_tools(_same(env, call, "list", {"source": "custom"}))["items"]
    assert custom == [{"tool": plugin_tool_name(result["plugin_id"], "run"), "source": "custom",
                       "category": "plugins", "deferred": False, "visible": False}]
    other = resolve_owner_home(env.root, OwnerIdentity.provider_user("test", "second"))
    foreign = env.call("list", {"cursor": first["next_cursor"]}, owner=other)
    _error("CURSOR_EXPIRED", lambda: tools.list_tools(foreign))
    _error("CURSOR_EXPIRED", lambda: tools.list_tools(replace(first_call, params={"cursor": "invalid"})))


def test_generated_plugin_stdio_wire_smoke_without_host_execution_claim(env, tmp_path):
    """只验生成包协议和 JSON stdin 脚本约定；原生执行门另由接线测试覆盖。"""
    call, _result = _created(env)
    package = env.managers[call.owner_id].calls[0][3]
    directory = tmp_path / "protocol-unit"
    directory.mkdir()
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as archive:
        for name in ("server.py", "entry.py"):
            (directory / name).write_bytes(archive.read(name))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run", "arguments": {"text": "wire"}}},
    ]
    completed = subprocess.run([sys.executable, "-I", str(directory / "server.py")],
                               input="\n".join(json.dumps(row) for row in requests) + "\n",
                               capture_output=True, text=True, check=True, timeout=5)
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [row["id"] for row in responses] == [1, 2, 3]
    assert responses[1]["result"]["tools"][0]["inputSchema"] == _params()["input_schema"]
    assert json.loads(responses[2]["result"]["content"][0]["text"]) == {"text": "wire"}
    assert responses[2]["result"]["isError"] is False


def test_real_parameter_center_batch_roundtrip_and_two_field_ledger(env):
    call = env.call("configure", {"tool": "read_a", "category": "files", "deferred": True})
    changed = tools.configure(call)
    path = Path(call.context.host.config.config_path)
    loaded = load_config(path)
    assert loaded.tool_catalog_categories == ["files"]
    assert loaded.tool_catalog_deferred_categories == [*AgentConfig().tool_catalog_deferred_categories, "files"]
    assert changed["config_revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    ledger = parameter_changes.parameter_history(user_path=path, limit=10)
    assert len(ledger) == 2
    assert {row["key"] for row in ledger} == {"tool_catalog_categories", "tool_catalog_deferred_categories"}
    assert tools.configure(call) == changed
    assert len(parameter_changes.parameter_history(user_path=path, limit=10)) == 2
    resetting = _same(env, call, "reset", {"tool": "read_a", "expected_config_revision": changed["config_revision"]})
    tools.reset(resetting)
    assert load_config(path).tool_catalog_categories == AgentConfig().tool_catalog_categories
    assert load_config(path).tool_catalog_deferred_categories == AgentConfig().tool_catalog_deferred_categories
    assert len(parameter_changes.parameter_history(user_path=path, limit=10)) == 4


def test_known_native_install_rejection_releases_reservation(env):
    call, result = _created(env)
    manager = env.managers[call.owner_id]
    manager.install_error = PluginInstallationError("permission_denied", "测试拒绝")
    changing = _same(env, call, "update", {"tool_id": result["tool_id"], "expected_sha256": result["sha256"],
                                         "entry_script": "print('new')"})
    _error("BUSINESS_ACCESS_DENIED", lambda: tools.update(changing))
    record = v13_store_for(call, "owner").get("custom_tools", result["tool_id"])
    assert record["sha256"] == result["sha256"] and "_busy" not in record
    manager.install_error = None
    changed = tools.update(replace(changing, idempotency_key="retry-new-key-12345"))["tool"]
    assert changed["sha256"] != result["sha256"]


def test_cold_main_catalog_holds_and_releases_exact_owner_lease(env):
    call = env.call("list")
    hot = call.context.host
    cold = SimpleNamespace(home_paths=hot.home_paths, config=hot.config, tools=None, allowed=True)
    events = []

    def provider(stack):
        events.append("borrow")
        stack.callback(lambda: events.append("release"))
        return hot

    bound = replace(call, context=SimpleNamespace(host=cold, host_provider=provider))
    assert len(tools.list_tools(bound)["items"]) == 3
    assert events == ["borrow", "release"]
    other = resolve_owner_home(env.root, OwnerIdentity.provider_user("test", "cold"))
    other_call = env.call("list", owner=other)
    other_call.context.host.tools = None
    other_call.context.host_provider = lambda stack: pytest.fail("普通 owner 被动读不能创建 Agent")
    _error("HOST_NOT_READY", lambda: tools.list_tools(other_call))


def test_failed_native_state_cannot_be_promoted_by_ok_boolean(env, monkeypatch):
    call, result = _created(env)
    monkeypatch.setattr(tools, "execute_plugin_invocation", lambda *args: {
        "state": "failed", "ok": True, "output": "not confirmed", "error_code": "TOOL_EXECUTION_FAILED"})
    tested = tools.test(_same(env, call, "test", {"tool_id": result["tool_id"], "args": {"text": "x"}}))
    assert tested["ok"] is False and tested["error_code"] == "TOOL_EXECUTION_FAILED"


def test_metadata_is_not_a_second_tool_registry_and_history_stays_outside_entity_snapshot(env):
    call, result = _created(env)
    for _ in range(3):
        assert tools.create(call)["tool"] == result
    state = v13_store_for(call, "owner")._read("custom_tools")
    assert set(state["records"]) == {result["tool_id"]}
    assert state["metadata"]["custom_tool_operations"] == {}
    assert not state["receipts"] and state["history_rows"] is True
    env.managers[call.owner_id].entries.clear()
    listing = _same(env, call, "list", {"source": "custom"})
    assert tools.list_tools(listing)["items"] == []


def test_install_succeeded_but_enable_failed_preserves_committed_effect(env, monkeypatch):
    call = env.call(params=_params())
    manager = env.managers[call.owner_id]
    original = manager.package_lifecycle

    def lifecycle(plugin_id, action, *, operation_id):
        if action == 'enable':
            raise HostAPIError('HOST_NOT_READY', 503)
        return original(plugin_id, action, operation_id=operation_id)

    monkeypatch.setattr(manager, 'package_lifecycle', lifecycle)
    for _ in range(2):
        error = _error('HOST_NOT_READY', lambda: tools.create(call))
        assert error.outcome == 'committed'
    assert [row[0] for row in manager.calls] == ['install']
    entry = next(iter(manager.entries.values()))
    assert entry.enabled is False
    record = v13_store_for(call, 'owner').get('custom_tools', entry.manifest.plugin_id)
    assert record['sha256'] == entry.package_sha256 and '_busy' not in record


def test_update_install_rejection_after_disable_is_not_not_started(env):
    call, result = _created(env)
    manager = env.managers[call.owner_id]
    manager.install_error = PluginInstallationError('permission_denied', '测试拒绝')
    changing = _same(env, call, 'update', {'tool_id': result['tool_id'],
        'expected_sha256': result['sha256'], 'entry_script': "print('new')"})
    error = _error('BUSINESS_ACCESS_DENIED', lambda: tools.update(changing))
    assert error.outcome == 'committed'
    assert manager.entries[result['plugin_id']].enabled is False
    assert tools._view(v13_store_for(call, 'owner').get('custom_tools', result['tool_id']),
                       manager.entries[result['plugin_id']])['sha256'] == result['sha256']


def test_delete_failure_after_disable_preserves_committed_effect(env, monkeypatch):
    call, result = _created(env)
    manager = env.managers[call.owner_id]
    original = manager.package_lifecycle

    def lifecycle(plugin_id, action, *, operation_id):
        if action == 'uninstall':
            raise HostAPIError('HOST_NOT_READY', 503)
        return original(plugin_id, action, operation_id=operation_id)

    monkeypatch.setattr(manager, 'package_lifecycle', lifecycle)
    deleting = _same(env, call, 'delete', {'tool_id': result['tool_id'], 'expected_sha256': result['sha256']})
    error = _error('HOST_NOT_READY', lambda: tools.delete(deleting))
    assert error.outcome == 'committed'
    assert result['plugin_id'] in manager.entries and not manager.entries[result['plugin_id']].enabled
    assert '_busy' not in v13_store_for(call, 'owner').get('custom_tools', result['tool_id'])


def test_delete_sha_binding_survives_concurrent_native_package_replacement(env, monkeypatch):
    call = env.call(params=_params())
    call.context.host.config.plugin_legacy_sandbox_default = False
    monkeypatch.setattr(plugins, 'management', native_management)
    result = tools.create(call)['tool']
    manager = native_management(call)
    original = tools._native_entry
    entered, release = threading.Event(), threading.Event()

    def bound(manager, record, **kwargs):
        entry = original(manager, record, **kwargs)
        entered.set()
        assert release.wait(10), '原生替换交错未释放'
        return entry

    monkeypatch.setattr(tools, '_native_entry', bound)
    deleting = _same(env, call, 'delete', {'tool_id': result['tool_id'], 'expected_sha256': result['sha256']})
    replacement = tools._package(result['plugin_id'], tools._definition(_params(entry_script="print('native B')")))
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(lambda: _error('REVISION_CONFLICT', lambda: tools.delete(deleting)))
        try:
            assert entered.wait(10)
            entry = manager.installations.snapshot()[0]
            manager.install_package_bytes(replacement, operation_id='native-replace-during-delete',
                                          expected_revision=entry.revision)
        finally:
            release.set()
        running.result(timeout=10)
    entries = manager.installations.snapshot()
    assert len(entries) == 1 and entries[0].package_sha256 == hashlib.sha256(replacement).hexdigest()
    assert entries[0].package_sha256 != result['sha256']


@pytest.mark.parametrize('action', ['create', 'update', 'delete', 'test'])
def test_native_effect_then_entity_finish_store_failure_is_unknown_and_not_reexecuted(env, monkeypatch, action):
    if action == 'create':
        call = env.call(params=_params())
    else:
        initial, result = _created(env)
        params = {'tool_id': result['tool_id'], 'expected_sha256': result['sha256']}
        if action == 'update':
            params['entry_script'] = "print('new')"
        elif action == 'test':
            params = {'tool_id': result['tool_id'], 'args': {'text': 'x'}}
        call = _same(env, initial, action, params)
    native_store = tools.v13_store_for
    failures, executed = [], []

    class Store:
        def __init__(self, delegate):
            self.delegate = delegate

        def transaction(self, *args, **kwargs):
            if kwargs.get('idempotency_key', '').endswith('-finish') and not failures:
                failures.append(True)
                raise BusinessError('STORE_UNAVAILABLE')
            return self.delegate.transaction(*args, **kwargs)

    def invoke(*args):
        executed.append(True)
        return {'state': 'succeeded', 'ok': True, 'output': 'done'}

    monkeypatch.setattr(tools, 'execute_plugin_invocation', invoke)
    monkeypatch.setattr(tools, 'v13_store_for', lambda *args: Store(native_store(*args)))
    handler = tools.HANDLERS[call.action]
    error = _error('OUTCOME_UNKNOWN', lambda: handler(call))
    assert error.outcome == 'unknown' and failures == [True]
    calls = len(env.managers[call.owner_id].calls)
    assert _error('OUTCOME_UNKNOWN', lambda: handler(call)).outcome == 'unknown'
    assert len(env.managers[call.owner_id].calls) == calls
    assert executed == ([True] if action == 'test' else [])
    records = v13_store_for(call, 'owner').list('custom_tools')
    assert len(records) == 1 and records[0]['_busy'] == tools._operation(call)


@pytest.mark.parametrize('action,stage,authorized', [
    ('delete', 'disable', True), ('update', 'disable', True),
    ('create', 'enable', True), ('update', 'enable', True), ('create', 'enable', False),
])
def test_lifecycle_readback_rejects_same_bytes_reinstall_and_preserves_unknown(env, monkeypatch, action, stage, authorized):
    """原生命令 I/O 替身交错；真实身份算法和 owner 收口仓库不替换，不冒充批准后插件运行。"""
    if action == 'create':
        call = env.call(params=_params())
    else:
        initial, result = _created(env)
        params = {'tool_id': result['tool_id'], 'expected_sha256': result['sha256']}
        if action == 'update':
            params['entry_script'] = "print('new')"
        call = _same(env, initial, action, params)
    manager = env.managers[call.owner_id]
    manager.authorization = authorized
    original = manager.command
    entered, release = threading.Event(), threading.Event()
    selected = []

    def paused(text, **kwargs):
        outcome = original(text, **kwargs)
        if shlex.split(text)[1] == stage:
            selected.append(shlex.split(text)[2])
            entered.set()
            assert release.wait(10), '同包重装交错未释放'
        return outcome

    monkeypatch.setattr(manager, 'command', paused)
    handler = tools.HANDLERS[call.action]
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(lambda: _error('OUTCOME_UNKNOWN', lambda: handler(call)))
        try:
            assert entered.wait(10), '未进入目标原生命令读回窗口'
            prior = manager.entries[selected[0]]
            # 同 SHA、相同 enabled 甚至相同数值 revision，也不能代替另一安装提交的身份。
            replacement = SimpleNamespace(**{**vars(prior),
                'installation_ref': hashlib.sha256(b'foreign-installation').hexdigest(),
                'last_commit': SimpleNamespace(operation_id='foreign-native-installation')})
            manager.entries[selected[0]] = replacement
            calls_at_swap = len(manager.calls)
        finally:
            release.set()
        assert running.result(timeout=10).outcome == 'unknown'
    assert manager.entries[selected[0]] is replacement
    assert _error('OUTCOME_UNKNOWN', lambda: handler(call)).outcome == 'unknown'
    assert len(manager.calls) == calls_at_swap
    record = v13_store_for(call, 'owner').get('custom_tools', selected[0])
    assert record['_busy'] == tools._operation(call)


@pytest.mark.parametrize('action', ['create', 'update', 'delete'])
def test_lifecycle_readback_failure_after_native_effect_stays_unknown(env, monkeypatch, action):
    if action == 'create':
        call = env.call(params=_params())
    else:
        initial, result = _created(env)
        params = {'tool_id': result['tool_id'], 'expected_sha256': result['sha256']}
        if action == 'update':
            params['entry_script'] = "print('new')"
        call = _same(env, initial, action, params)
    manager = env.managers[call.owner_id]
    original = manager.command

    def unreadable():
        raise PluginInstallationError('table_corrupt', '提交后的只读表故障')

    def command(*args, **kwargs):
        outcome = original(*args, **kwargs)
        manager.installations.snapshot = unreadable
        return outcome

    monkeypatch.setattr(manager, 'command', command)
    handler = tools.HANDLERS[call.action]
    assert _error('OUTCOME_UNKNOWN', lambda: handler(call)).outcome == 'unknown'
    commands = len(manager.calls)
    assert _error('OUTCOME_UNKNOWN', lambda: handler(call)).outcome == 'unknown'
    assert len(manager.calls) == commands
    record = v13_store_for(call, 'owner').list('custom_tools')[0]
    assert record['_busy'] == tools._operation(call)
