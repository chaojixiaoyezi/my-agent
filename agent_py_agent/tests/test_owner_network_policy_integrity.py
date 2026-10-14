"""合成普通 owner 的原网络工具不得因权限文件损坏而获得网络授权。"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests import test_feishu_cloud_collaborator_authorization as cloud_cases
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_feishu_cloud_collaborator_authorization import (
    _call,
    _member,
    _self_agent,
)


@pytest.fixture
def server(monkeypatch):
    yield from cloud_cases.server.__wrapped__(monkeypatch)


@pytest.mark.parametrize("damage", ["{damaged-json", "{}", '{"schema_version":"permissions.unknown","network":{"enabled":true}}',
                                    '{"schema_version":"permissions.v1","network":{"enabled":"false"}}',
                                    '{"schema_version":"permissions.v1","network":{"enabled":1}}',
                                    '{"schema_version":"permissions.v1","network":{}}', '[]'])
def test_corrupt_permissions_never_open_real_owner_cloud_network(tmp_path, server, damage):
    identity = OwnerIdentity.provider_user("feishu", "ou_integrity")
    agent = _self_agent(tmp_path, identity)
    path = agent.home_paths.owner_permissions_json
    policy = json.loads(path.read_text())
    policy["network"] = {"enabled": False}
    path.write_text(json.dumps(policy))
    server.members["sheet_one"] = [_member("openid", "ou_integrity", "view")]
    denied = _call(agent, "sheet_read")
    assert not denied.ok and denied.reported_error_code == "NETWORK_DISABLED" and not server.calls
    path.write_text(damage)
    rebuilt = _self_agent(tmp_path, identity)
    result = _call(rebuilt, "sheet_read")
    assert not rebuilt.owner_policy.network_enabled
    assert not result.ok and result.reported_error_code == "NETWORK_DISABLED" and not server.calls
    assert any(error["context"] == "owner_policy.permissions" for error in rebuilt.owner_policy.load_errors)
    assert any(error["error_code"] == "OWNER_PERMISSIONS_INVALID" and error["recoverable"] is False
               for error in rebuilt.owner_policy.load_errors)
    assert path.read_text() == damage


def test_permission_io_failure_cannot_be_treated_as_missing_or_open(tmp_path, server, monkeypatch):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user("feishu", "ou_integrity"))
    path = agent.home_paths.owner_permissions_json
    original = Path.read_text

    # LLM: 仅注入本测试 owner 的权限读取错误，其余原配置/授权/HTTP 路径保持正式实现。
    # 函数用途: 覆盖宿主读取权限异常而不是靠 root 下 chmod 得出错误的平台结论。
    def read(target, *args, **kwargs):
        if target == path:
            raise PermissionError("synthetic permission failure")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    policy = resolve_effective_owner_policy(agent.home_paths)
    assert not policy.network_enabled and policy.load_errors
    assert policy.load_errors[0]["error_code"] == "OWNER_PERMISSIONS_UNAVAILABLE"
    result = _call(agent, "sheet_read")
    assert not result.ok and result.reported_error_code == "NETWORK_DISABLED" and not server.calls


@pytest.mark.parametrize("kind", ["directory", "symlink", "lstat_io"])
def test_existing_nonregular_or_unavailable_policy_is_closed_without_repair(tmp_path, server, monkeypatch, kind):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user("feishu", "ou_integrity"))
    path = agent.home_paths.owner_permissions_json
    if kind == "lstat_io":
        original = Path.lstat

        def unavailable(target, *args, **kwargs):
            if target == path:
                raise PermissionError("synthetic lstat failure")
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", unavailable)
    else:
        good = tmp_path / "synthetic-good-policy.json"
        good.write_bytes(path.read_bytes())
        path.unlink()
        path.mkdir() if kind == "directory" else path.symlink_to(good)
    policy = resolve_effective_owner_policy(agent.home_paths)
    assert not policy.network_enabled
    code = "OWNER_PERMISSIONS_UNAVAILABLE" if kind == "lstat_io" else "OWNER_PERMISSIONS_INVALID"
    assert any(row["error_code"] == code for row in policy.load_errors)
    result = _call(agent, "sheet_read")
    assert not result.ok and result.reported_error_code == "NETWORK_DISABLED" and not server.calls


def test_policy_restore_and_missing_bootstrap_keep_explicit_network_behavior(tmp_path, server):
    identity = OwnerIdentity.provider_user("feishu", "ou_integrity")
    agent = _self_agent(tmp_path, identity)
    path = agent.home_paths.owner_permissions_json
    policy = json.loads(path.read_text())
    assert policy["schema_version"] == "permissions.v1" and policy["network"]["enabled"] is True
    server.members["sheet_one"] = [_member("openid", "ou_integrity", "view")]
    assert _call(agent, "sheet_read").ok
    path.write_text("{broken")
    before = len(server.calls)
    assert not _call(agent, "sheet_read").ok and len(server.calls) == before
    path.write_text(json.dumps(policy))
    assert _call(agent, "sheet_read").ok
    assert resolve_effective_owner_policy(agent.home_paths).load_errors == ()
    path.unlink()
    missing = resolve_effective_owner_policy(agent.home_paths)
    assert missing.network_enabled and missing.load_errors == ()
    rebuilt = _self_agent(tmp_path, identity)
    assert rebuilt.owner_policy.network_enabled and path.exists()


def test_closed_owner_policy_caps_child_grandchild_and_background_restoration(tmp_path):
    identity = OwnerIdentity.provider_user("feishu", "ou_integrity")
    agent = _self_agent(tmp_path, identity)
    good = agent.home_paths.owner_permissions_json.read_bytes()
    agent.home_paths.owner_permissions_json.write_text("{broken")
    root = resolve_effective_owner_policy(agent.home_paths)
    assert not root.network_enabled
    agent = _self_agent(tmp_path, identity)
    child = agent.subagents.create_run(goal="合成任务", thought="核对权限", plan=["不发请求"])
    assert child.effective_permissions["network_enabled"] is False
    grandchild = agent.subagents.create_run(goal="合成孙任务", thought="核对继承", plan=["不发请求"],
                                            parent_id=child.id, root_id=child.root_id, depth=1)
    assert grandchild.effective_permissions["network_enabled"] is False
    # 原 parent-capped 合同必须让孙级和恢复的父级投影都保持关闭，不接受新配置默认 true 覆盖。
    parent = replace(root, owner_id="synthetic-parent")
    capped = resolve_effective_owner_policy(agent.home_paths, parent_policy=parent)
    assert not capped.network_enabled
    restored = _self_agent(tmp_path, identity)
    assert not restored.owner_policy.network_enabled
    for task in (child, grandchild):
        context = restored.subagents.runner_context.build_execution_context(task.id)
        assert context.effective_permissions["network_enabled"] is False
        bound = write_boundary_with_runtime_ledger(restored, SimpleNamespace(write_boundary=context.write_boundary, run_id=""))
        assert bound["network_access_mode"] == "disabled"
    agent.home_paths.owner_permissions_json.write_bytes(good)
    repaired = _self_agent(tmp_path, identity)
    assert repaired.owner_policy.network_enabled
    for task in (child, grandchild):
        context = repaired.subagents.runner_context.build_execution_context(task.id)
        bound = write_boundary_with_runtime_ledger(repaired, SimpleNamespace(write_boundary=context.write_boundary, run_id=""))
        assert bound["network_access_mode"] == "disabled"


def test_original_disabled_instance_and_real_owner_missing_home_never_expand(tmp_path, server):
    identity = OwnerIdentity.provider_user("feishu", "ou_integrity")
    initial = _self_agent(tmp_path, identity)
    path = initial.home_paths.owner_permissions_json
    good = path.read_bytes()
    disabled = json.loads(good)
    disabled["network"]["enabled"] = False
    path.write_text(json.dumps(disabled))
    agent = _self_agent(tmp_path, identity)
    assert not agent.owner_policy.network_enabled
    path.write_bytes(good)
    bound = write_boundary_with_runtime_ledger(agent, SimpleNamespace(write_boundary={}, run_id=""))
    assert bound["network_access_mode"] == "disabled"
    assert not _call(agent, "sheet_read", boundary=bound).ok and not server.calls
    missing_home = SimpleNamespace(owner_policy=resolve_effective_owner_policy(initial.home_paths))
    assert write_boundary_with_runtime_ledger(missing_home, SimpleNamespace(write_boundary={}, run_id=""))["network_access_mode"] == "disabled"


class _ToyNetworkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"synthetic")


@pytest.fixture
def toy_http():
    service = ThreadingHTTPServer(("127.0.0.1", 0), _ToyNetworkHandler)
    service.hits = 0
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service
    service.shutdown()
    thread.join(timeout=2)
    service.server_close()


@pytest.mark.parametrize("executor", ["main", "child", "grandchild", "background"])
def test_live_agent_original_executor_rechecks_owner_network_before_spawn(tmp_path, toy_http, executor):
    if sys.platform == "darwin" and not shutil.which("sandbox-exec"):
        pytest.skip("原 macOS 沙箱不可用")
    if sys.platform == "linux" and not shutil.which("bwrap"):
        pytest.skip("原 Linux 沙箱不可用")
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("本回归需要原实际 OS 网络沙箱")
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user("feishu", "ou_integrity"))
    path = agent.home_paths.owner_permissions_json
    good = path.read_bytes()
    child = agent.subagents.create_run(goal="合成任务", thought="核对权限", plan=["连接隔离服务"])
    grandchild = agent.subagents.create_run(goal="合成孙任务", thought="核对权限", plan=["连接隔离服务"],
                                            parent_id=child.id, root_id=child.root_id, depth=1)
    task = child if executor == "child" else grandchild if executor == "grandchild" else None
    base = agent.subagents.runner_context.build_execution_context(task.id).write_boundary if task else {}
    code = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{toy_http.server_port}/', timeout=2).status)"
    command = "/usr/bin/python3 -c " + shlex.quote(code)
    for phase in ("valid", "damaged", "disabled", "repaired"):
        if phase == "damaged":
            path.write_text("{damaged-json")
        elif phase == "disabled":
            policy = json.loads(good)
            policy["network"]["enabled"] = False
            path.write_text(json.dumps(policy))
        elif phase == "repaired":
            path.write_bytes(good)
        params = SimpleNamespace(write_boundary=base, run_id="", source="background" if executor == "background" else "run")
        bound = write_boundary_with_runtime_ledger(agent, params)
        before = toy_http.hits
        result = execute_registry_test_call(agent.tools, "run_command", {"command": command, "timeout": 5},
                                           write_boundary=bound, run_id=task.id if task else "test-run",
                                           register_with=agent, call_id=uuid.uuid4().hex)
        if phase in {"valid", "repaired"}:
            assert result.ok, (result.reported_error_code, result.output)
            assert toy_http.hits - before == 1
        else:
            assert not result.ok
            assert bound["network_access_mode"] == "disabled"
            assert toy_http.hits == before
    # 创建期快照仍是上界，实时读取只影响实际入口，不伪称持久投影已经刷新。
    assert agent.owner_policy.network_enabled and child.effective_permissions["network_enabled"]
