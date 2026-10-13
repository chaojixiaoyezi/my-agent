from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.private_release import build, configure, installer


def _bundle(root: Path, version="test.1") -> Path:
    package = root / "runtime/lib/python3.11/site-packages/agent_py_agent/config"
    package.mkdir(parents=True)
    (package / "agent_config.yaml").write_text('workspace_root: ""\ngateway_port: 8420\n', encoding="utf-8")
    files = {str(path.relative_to(root)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in root.rglob("*") if path.is_file()}
    (root / "release.json").write_text(json.dumps({"schema": installer.SCHEMA, "architecture": os.uname().machine, "version": version, "files": files}))
    return root


def test_install_and_upgrade_keep_explicit_data_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    prefix = tmp_path / "app" / "custom install"
    installer.install(_bundle(tmp_path / "first"), prefix)
    config = prefix / "data/config/agent_config.yaml"
    assert 'gateway_port: 18420' in config.read_text()
    assert 'workspace_root: ""' in config.read_text()
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.workspace_resolution import (
        resolve_workspace_roots,
        validate_requested_workspace_roots,
    )

    monkeypatch.setenv("MY_AGENT_HOME", str(prefix / "data"))
    loaded = load_config(config)
    roots = resolve_workspace_roots(loaded, config)
    validate_requested_workspace_roots(loaded, roots)
    assert roots == [installer.workspace_path(prefix)]
    config.write_text("private configuration\n", encoding="utf-8")
    history = prefix / "data/history.keep"
    history.write_bytes(b"persistent\x00history")
    work = installer.workspace_path(prefix) / "keep.txt"
    work.write_text("user work")
    installer.install(_bundle(tmp_path / "second", "test.2"), prefix)
    assert (prefix / "current").resolve() == prefix / "releases/test.2"
    assert config.read_text() == "private configuration\n"
    assert history.read_bytes() == b"persistent\x00history"
    assert work.read_text() == "user work"
    launcher = (prefix / "bin/my-agent-preview").read_text()
    assert "$HOME" not in launcher
    assert "-X utf8" in launcher
    assert " -I -B " in launcher


def test_bad_payload_does_not_switch_installed_release(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    prefix = tmp_path / "install"
    installer.install(_bundle(tmp_path / "first"), prefix)
    damaged = _bundle(tmp_path / "second", "test.2")
    (damaged / "runtime/lib/python3.11/site-packages/agent_py_agent/config/agent_config.yaml").write_text("corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        installer.install(damaged, prefix)
    assert (prefix / "current").resolve().name == "test.1"


def test_busy_installation_is_not_stopped_or_switched(tmp_path, monkeypatch):
    prefix = tmp_path / "install"
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    installer.install(_bundle(tmp_path / "first"), prefix)
    monkeypatch.setattr(installer, "active_processes", lambda _: [12345])
    with pytest.raises(ValueError, match="12345"):
        installer.install(_bundle(tmp_path / "second", "test.2"), prefix)
    assert (prefix / "current").resolve().name == "test.1"


def test_unmanaged_or_extra_payload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    bundle = _bundle(tmp_path / "bundle")
    prefix = tmp_path / "existing"
    prefix.mkdir()
    (prefix / "keep").write_text("private")
    with pytest.raises(ValueError, match="非空"):
        installer.install(bundle, prefix)
    (bundle / "unexpected.py").write_text("bad")
    with pytest.raises(ValueError, match="额外文件"):
        installer.verify(bundle)
    assert (prefix / "keep").read_text() == "private"


def test_model_configuration_preserves_key_privacy_and_real_field(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text('model_backend: ""\nmodel_name: ""\napi_base: ""\napi_key: ""\n')
    replies = iter(["https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-max"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(configure.getpass, "getpass", lambda _: "test-placeholder-private")
    monkeypatch.setattr(sys, "argv", ["configure", str(config)])
    assert configure.main() == 0
    text = config.read_text()
    assert 'model_name: qwen3.7-max' in text or 'model_name: "qwen3.7-max"' in text
    assert "openai_compatible" in text
    assert "test-placeholder-private" not in capsys.readouterr().out
    assert config.stat().st_mode & 0o777 == 0o600


def test_sourceless_standalone_launcher_executes_utf8(tmp_path):
    script = tmp_path / "launch.py"
    script.write_text("print('中文测试')\n", encoding="utf-8")
    target = script.with_suffix(".pyc")
    py_compile.compile(str(script), cfile=str(target), doraise=True)
    script.unlink()
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(target)], capture_output=True, check=True)
    assert result.stdout.decode("utf-8") == "中文测试\n"


def test_parameter_readers_snapshot_matches_source(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import parameter_metadata as metadata

    metadata.field_readers.cache_clear()
    original = metadata.field_readers()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/parameter_readers.json").write_text(json.dumps({"schema": "my-agent.parameter-readers.v1", "readers": original}))
    monkeypatch.setattr(metadata, "_PACKAGE", tmp_path)
    metadata.field_readers.cache_clear()
    try:
        assert metadata.field_readers() == original
    finally:
        metadata.field_readers.cache_clear()


def test_relocated_console_script_runs_after_directory_move(tmp_path):
    runtime = tmp_path / "build-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3").symlink_to(sys.executable)
    script = runtime / "bin/example"
    script.write_text("#!/unavailable/build/python3\nprint('中文 relocatable')\n", encoding="utf-8")
    script.chmod(0o755)
    build.relocate_console_scripts(runtime)
    moved = tmp_path / "final runtime"
    runtime.rename(moved)
    result = subprocess.run([str(moved / "bin/example")], capture_output=True, check=True)
    assert result.stdout.decode("utf-8") == "中文 relocatable\n"


def test_auto_update_uses_original_marker_and_rejects_typo(tmp_path, monkeypatch):
    monkeypatch.setattr(installer,'active_processes',lambda _:[])
    prefix=tmp_path/'app'/'.my-agent'
    installer.install(_bundle(tmp_path/'bundle'),prefix)
    assert installer.resolve_install_prefix(None,roots=[tmp_path/'app'])==prefix
    with pytest.raises(ValueError,match='第二个 Gateway'):
        installer.resolve_install_prefix(tmp_path/'app'/'my-agent',roots=[tmp_path/'app'])
    assert not (tmp_path/'app'/'my-agent').exists()


def test_multiple_installations_require_explicit_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(installer,'active_processes',lambda _:[])
    bundle=_bundle(tmp_path/'bundle')
    root=tmp_path/'app'
    for name in ('one','two'):
        installer.install(bundle,root/name)
    with pytest.raises(ValueError,match='多个安装'):
        installer.resolve_install_prefix(None,roots=[root])
    assert installer.resolve_install_prefix(root/'two',roots=[root])==root/'two'


def test_explicit_registered_nested_prefix_updates_without_environment_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'active_processes', lambda _: [])
    shallow = tmp_path / 'app' / '.my-agent'
    nested = tmp_path / 'custom' / 'deep' / 'install'
    bundle = _bundle(tmp_path / 'bundle')
    installer.install(bundle, shallow)
    installer.install(bundle, nested)
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'unrelated-home'))
    assert installer.resolve_install_prefix(nested, roots=[tmp_path / 'app']) == nested
    with pytest.raises(ValueError, match='第二个 Gateway'):
        installer.resolve_install_prefix(tmp_path / 'custom' / 'typo', roots=[tmp_path / 'app'])


@pytest.mark.parametrize("workspace", ["", "custom/gateway", "/tmp/explicit-update-service"])
@pytest.mark.parametrize("protocol", [None, "drain.v1"])
def test_update_services_uses_canonical_prefix_paths_and_restores_old_channel(tmp_path, monkeypatch, workspace, protocol):
    from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record

    prefix = tmp_path / "install"
    monkeypatch.setattr(installer, "active_processes", lambda _: [])
    installer.install(_bundle(tmp_path / "bundle"), prefix)
    config = prefix / "data/config/agent_config.yaml"
    config.write_text('my_agent_home: ""\ngateway_workspace: ' + json.dumps(workspace) + '\n')
    owner = installer.workspace_path(prefix)
    service = Path(workspace) if Path(workspace).is_absolute() else owner / "workspace" / (workspace or "runtime/services/gateway")
    # 绝对覆盖使用 tmp_path，不能让测试往真正 /tmp 公共服务路径写 PID。
    if workspace.startswith("/"):
        service = tmp_path / "absolute-service"
        config.write_text('my_agent_home: ""\ngateway_workspace: ' + json.dumps(str(service)) + '\n')
    service.mkdir(parents=True)
    write_pid_record(service / "gateway.pid")
    write_pid_record(service / "adapter.pid")
    (service / "adapter_state.json").write_text(json.dumps({"channels": [{"name": "feishu"}]}))
    manifest = prefix / "current/release.json"
    release = json.loads(manifest.read_text())
    if protocol:
        release["protocols"] = {"offline_update": protocol}
    manifest.write_text(json.dumps(release))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "other-home"))
    commands = []
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == str(prefix / "bin/my-agent-preview"):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    original = installer.update_services(prefix)
    assert original == (True, True, "feishu")
    assert [row[1:3] for row in commands] == [["adapter", "stop"], ["gateway", "stop"]]
    assert ("--drain" in commands[-1]) == bool(protocol)
    installer.update_services(prefix, restore=original)
    assert commands[-2][1:3] == ["gateway", "start"]
    assert commands[-1][1:] == ["adapter", "start", "--daemon", "--channel", "feishu"]


@pytest.mark.parametrize("state,expected", [
    ({"channels": [{"name": "feishu"}]}, "feishu"),
    ({"channels": [{"name": "qq"}]}, "qq"),
    ({"channels": [{"name": "feishu"}, {"name": "qq"}]}, "all"),
    ({"requested_channel": "all", "channels": [{"name": "feishu"}]}, "all"),
    ({"channels": []}, None),
    ({"channels": [{"name": "unknown"}]}, None),
    ({"channels": [{"name": "feishu"}, {"name": "feishu"}]}, None),
    ({"requested_channel": "qq", "channels": [{"name": "feishu"}]}, None),
    ({"requested_channel": "invalid", "channels": [{"name": "feishu"}]}, None),
    ({"channels": [{"name": []}]}, None),
    ({"requested_channel": [], "channels": [{"name": "feishu"}]}, None),
])
def test_update_channel_uses_structured_old_and_current_state(state, expected):
    if expected is None:
        with pytest.raises(ValueError, match="通道记录"):
            installer.restore_channel(state)
    else:
        assert installer.restore_channel(state) == expected
