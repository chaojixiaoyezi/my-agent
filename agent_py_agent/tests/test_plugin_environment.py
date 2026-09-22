"""独立环境组件验证；真实 venv/pip 仅在测试临时目录，不替代实际 TUI 的装卸验收。"""

import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent import plugin_environment as module
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.tooling.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.plugin_wheel_fixtures import change_wheel, make_wheel, package_wheels


def test_real_offline_environment_does_not_import_plugin_or_publish_activation(tmp_path, monkeypatch):
    owner_root = tmp_path / "含 空格的私有目录"
    owner_root.mkdir()
    owner = resolve_owner_home(owner_root)
    trap = tmp_path / "unexpected-import"
    package = package_wheels(make_wheel(requires=("helper>=1",), files={
        "startup-trap.pth": f"import pathlib; pathlib.Path({str(trap)!r}).write_text('executed')\n".encode(),
        "peek-1.0.dist-info/entry_points.txt": b"[console_scripts]\npeek-tool = peek:main\n",
        "peek-1.0.data/scripts/peek-script": b"#!python\nraise RuntimeError('must not run')\n",
        "peek-1.0.data/headers/peek.h": b"/* component fixture */\n",
    }), make_wheel("helper"))
    store = PluginInstallStore(owner)
    store.install(PluginInstallRequest(package, "install-first", 0))
    original = (store.root / "installations.json").read_bytes()
    result = module.prepare_plugin_environment(owner, package, "prepare-one")
    candidate = owner.plugins_dir / "environments" / result.environment_ref
    assert (candidate / result.python_relative_path).is_file()
    assert result.distributions == (("peek", "1.0"), ("helper", "1.0"))
    assert not trap.exists()
    assert store.snapshot()[0].enabled is False
    assert (store.root / "installations.json").read_bytes() == original
    assert str(tmp_path) not in str(asdict(result))
    monkeypatch.setattr(module, "run_environment_process", lambda *a, **kw: pytest.fail("同操作不得重建环境"))
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.prepare_plugin_environment(owner, package, "prepare-one")
    assert error.value.reason == "environment_exists"


def test_invalid_dependency_does_not_create_owner_files(tmp_path):
    owner = resolve_owner_home(tmp_path)
    package = package_wheels(make_wheel(requires=("missing>=1",)))
    with pytest.raises(PluginPackageError):
        module.prepare_plugin_environment(owner, package, "bad-dependencies")
    assert not owner.plugins_dir.exists()


@pytest.mark.parametrize("count,short_names", [(200, False), (4000, True)])
def test_real_pip_record_growth_for_many_entry_points_has_a_separate_budget(tmp_path, count, short_names):
    owner = resolve_owner_home(tmp_path)
    names = [f"s{number:04d}" if short_names else f"peek-command-{number:04d}" for number in range(count)]
    entries = "[console_scripts]\n" + "".join(f"{name}=m:f\n" for name in names)
    wheel = make_wheel(files={
        "peek-1.0.dist-info/entry_points.txt": entries.encode(),
        "m.py": b"raise RuntimeError('entry points must not run')\n",
    })
    package = package_wheels(change_wheel(wheel, changes={"peek-1.0.dist-info/RECORD.jws": b"{}"}))
    result = module.prepare_plugin_environment(owner, package, "many-entry-points")
    candidate = owner.plugins_dir / "environments" / result.environment_ref
    installed_record = next((candidate / "python" / "lib").glob("python*/site-packages/peek-1.0.dist-info/RECORD"))
    assert installed_record.stat().st_size > 8192
    assert all((candidate / "python" / "bin" / name).is_file() for name in names)


def test_failed_candidate_is_not_overwritten_or_treated_as_ready(tmp_path, monkeypatch):
    owner = resolve_owner_home(tmp_path)
    package = package_wheels(make_wheel())
    started = []

    def fail(candidate, *_):
        started.append(candidate)
        (candidate / "partial").write_text("incomplete")
        raise module.EnvironmentPreparationError("preparation_command", started=True)

    monkeypatch.setattr(module, "_prepare_candidate", fail)
    with pytest.raises(module.EnvironmentPreparationError):
        module.prepare_plugin_environment(owner, package, "same-operation")
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.prepare_plugin_environment(owner, package, "same-operation")
    assert error.value.reason == "environment_exists"
    assert len(started) == 1
    assert (started[0] / "partial").read_text() == "incomplete"
    assert not (owner.plugins_dir / "installations.json").exists()


def test_budget_fails_before_candidate_or_process(tmp_path, monkeypatch):
    owner = resolve_owner_home(tmp_path)
    package = package_wheels(make_wheel())
    monkeypatch.setattr(module, "run_environment_process", lambda *a, **kw: pytest.fail("不得启动"))
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.prepare_plugin_environment(owner, package, "too-small", limits=module.EnvironmentBuildLimits(max_bytes=1))
    assert error.value.reason == "environment_budget"
    assert not owner.plugins_dir.exists()


def test_original_owner_quota_blocks_preparation_before_first_candidate(tmp_path):
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    owner.quota_json.write_text(json.dumps({"max_disk_mb": 1}))
    with pytest.raises(OwnerQuotaExceeded):
        module.prepare_plugin_environment(owner, package_wheels(make_wheel()), "quota-blocked")
    assert not owner.plugins_dir.exists()


def test_busy_original_owner_quota_returns_without_waiting_or_creating_candidate(tmp_path):
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    owner.quota_json.write_text(json.dumps({"max_disk_mb": 1024}))
    started = time.monotonic()
    with locked_json_path(owner.home_dir / ".owner-quota"), pytest.raises(OwnerQuotaUnavailable):
        module.prepare_plugin_environment(owner, package_wheels(make_wheel()), "quota-busy")
    assert time.monotonic() - started < 2
    assert not owner.plugins_dir.exists()


def test_cancellation_during_readonly_preflight_prevents_candidate(tmp_path, monkeypatch):
    owner = resolve_owner_home(tmp_path)
    token = CancellationToken()
    inspect = module.inspect_plugin_wheels

    def cancel_after_read(package):
        result = inspect(package)
        token.cancel("during-validation")
        return result

    monkeypatch.setattr(module, "inspect_plugin_wheels", cancel_after_read)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        module.prepare_plugin_environment(owner, package_wheels(make_wheel()), "cancelled-read")
    assert not owner.plugins_dir.exists()


def test_expired_preflight_does_not_begin_filesystem_writes(tmp_path):
    owner = resolve_owner_home(tmp_path)
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.prepare_plugin_environment(
            owner, package_wheels(make_wheel()), "expired", limits=module.EnvironmentBuildLimits(timeout_seconds=0.000001),
        )
    assert error.value.reason == "preparation_timeout"
    assert not owner.plugins_dir.exists()


def test_candidate_parent_symlink_is_not_followed(tmp_path):
    owner = resolve_owner_home(tmp_path)
    owner.plugins_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (owner.plugins_dir / "environments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        module.prepare_plugin_environment(owner, package_wheels(make_wheel()), "linked")
    assert not list(outside.iterdir())


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"timeout_seconds": True},
    {"max_bytes": 0}, {"max_bytes": True},
])
def test_invalid_environment_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        module.EnvironmentBuildLimits(**kwargs)


def test_interpreter_fingerprint_is_content_bound_and_does_not_expose_path(monkeypatch, tmp_path):
    executable = tmp_path / "host-python"
    executable.write_bytes(b"one")
    monkeypatch.setattr(module.sys, "executable", str(executable))
    original = module._interpreter_fingerprint()
    executable.write_bytes(b"two")
    assert module._interpreter_fingerprint() != original
    assert len(original) == 64
    assert str(Path(tmp_path)) not in original
