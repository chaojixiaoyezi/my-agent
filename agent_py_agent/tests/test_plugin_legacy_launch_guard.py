"""B7 第四段：老格式插件四个启动入口的授权事实复核，与 restricted 经统一底座施加。

只使用临时 owner、手工环境目录与替身就绪检查；不启动真实插件进程，也不把替身当 OS 验收。
"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_permissions.grants import permission_details, select_permissions
from agent_py_agent.agent.plugin_permissions.sandbox import LegacyLaunchDenied, legacy_launch_policy
from agent_py_agent.agent.plugin_permissions.state import canonical_permission_json
from agent_py_agent.agent.plugin_runtime import PluginMCPClient
from agent_py_agent.agent.plugin_sandbox import plugin_sandbox_spec, sandboxed_plugin_argv
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_activation import publication
from agent_py_agent.tests.test_plugin_install_store import _request
from agent_py_agent.tests.test_plugin_legacy_management import fake_sandbox_ready


# LLM: 夹具手工建环境目录（跳过 venv 准备）并发布带固定 permission_json 的 active 激活；不执行插件。
# 函数用途: 为四个启动入口提供同一份可复核的 restricted/wide 安装记录与受控程序文件。
def restricted_activation(tmp_path, *, mode="restricted"):
    owner = resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    entry = store.install(_request(tmp_path)).installation
    plan = plan_plugin_environment(entry, "guard-op")
    program = tmp_path / "guard-program"
    program.write_bytes(b"guard program v1, never executed")
    grant = permission_details(entry, plan, select_permissions({"program_roots": [str(program)]}), mode)
    prepared = store.change_activation(PluginActivationRequest(
        plan.operation_id, entry.revision,
        PluginActivation(plan, "preparing", permission_json=canonical_permission_json(grant))))
    active = store.change_activation(publication(prepared)).installation
    (owner.plugins_dir / "environments" / plan.environment_ref / "python" / "bin").mkdir(parents=True)
    return store, owner, active, program


# LLM: 候选入口在 _candidate 里构造 PluginMCPClient（复核先于任何进程）；构造成功也证明变化前预检通过。
# 函数用途: 候选启动前程序内容变化时，构造必须结构化拒绝。
def test_candidate_entry_rejects_program_content_change(tmp_path, monkeypatch):
    _, owner, entry, program = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    PluginMCPClient(owner, entry).stop()
    program.write_bytes(b"changed guard program")
    with pytest.raises(LegacyLaunchDenied) as excinfo:
        PluginMCPClient(owner, entry)
    assert excinfo.value.reason == "legacy_permission_changed"


# LLM: 业务入口是 registry 构造客户端后在新运行边界 start；构造后、启动前的窗口必须被 start 复核覆盖。
# 函数用途: 构造后事实变化时，start 必须拒绝且没有进程被拉起。
def test_business_entry_rejects_change_between_construct_and_start(tmp_path, monkeypatch):
    _, owner, entry, program = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    client = PluginMCPClient(owner, entry)
    program.write_bytes(b"changed between construct and start")
    with pytest.raises(LegacyLaunchDenied) as excinfo:
        client.start()
    assert excinfo.value.reason == "legacy_permission_changed"
    assert not client.is_running()


# LLM: 面板入口由 plugin_display_client 构造、连接池后台 start；两条路径与业务共用同一客户端类。
# 函数用途: 面板启动前事实变化时，start 必须拒绝且零启动。
def test_panel_entry_rejects_change_before_pool_start(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_display.service import plugin_display_client

    _, owner, entry, program = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    client = plugin_display_client(owner, entry)
    program.write_bytes(b"changed before panel start")
    with pytest.raises(LegacyLaunchDenied):
        client.start()
    assert not client.is_running()


# LLM: 重连入口走 reconnect → start；未运行态也必须在真正拉起进程前复核固定授权。
# 函数用途: 重连前事实变化时，reconnect 必须拒绝且不重启进程。
def test_reconnect_entry_rejects_change_before_restart(tmp_path, monkeypatch):
    _, owner, entry, program = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    client = PluginMCPClient(owner, entry)
    program.write_bytes(b"changed before reconnect")
    with pytest.raises(LegacyLaunchDenied):
        client.reconnect()
    assert not client.is_running()


# LLM: 固定激活被撤销或换代后，任何入口的启动复核都必须先读鲜活安装表再拒绝；不追随新代次。
# 函数用途: 撤销后 start 必须拒绝且零启动。
def test_start_rejects_revoked_generation(tmp_path, monkeypatch):
    store, owner, entry, _ = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    client = PluginMCPClient(owner, entry)
    store.change_activation(PluginActivationRequest(
        "guard-revoke", entry.revision, replace(entry.activation, phase="revoked")))
    with pytest.raises(LegacyLaunchDenied) as excinfo:
        client.start()
    assert excinfo.value.reason == "legacy_permission_changed"
    assert not client.is_running()


# LLM: restricted 的启动命令必须等于统一 B7 底座的包装输出（同一 spec 工厂），收窄读/写/网络。
# 函数用途: 断言客户端命令与 plugin_sandbox_spec + sandboxed_plugin_argv 完全一致。
def test_restricted_launch_uses_b7_sandbox_entry(tmp_path, monkeypatch):
    _, owner, entry, _ = restricted_activation(tmp_path)
    fake_sandbox_ready(monkeypatch)
    client = PluginMCPClient(owner, entry)
    env_dir = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    data_dir = owner.plugins_dir / "data" / entry.manifest.plugin_id
    policy = legacy_launch_policy(owner, entry)
    assert policy is not None
    spec = plugin_sandbox_spec(cwd=env_dir, data_dir=data_dir, owner_home=owner.home_dir, sandbox_policy=policy)
    expected = sandboxed_plugin_argv([str(env_dir / "python" / "bin" / "python"), "-I", "-m", entry.manifest.entry_module], spec)
    assert [client.config.command, *client.config.args] == expected
    assert spec.private_read_roots == (Path.home().resolve(strict=True),)
    assert data_dir in spec.extra_write_roots
    assert spec.network_access is False


# LLM: wide 只是显式测试模式：不进受限沙箱、不声称隔离；process_sandbox 关时命令保持原样。
# 函数用途: 断言 wide 启动不包平台沙箱命令。
def test_wide_mode_never_enters_restricted_sandbox(tmp_path, monkeypatch):
    _, owner, entry, _ = restricted_activation(tmp_path, mode="wide")
    fake_sandbox_ready(monkeypatch)
    assert legacy_launch_policy(owner, entry) is None
    client = PluginMCPClient(owner, entry, process_sandbox=False)
    env_dir = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    assert client.config.command == str(env_dir / "python" / "bin" / "python")
    assert client.config.args == ["-I", "-m", entry.manifest.entry_module]
