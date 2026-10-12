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
