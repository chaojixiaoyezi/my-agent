from __future__ import annotations


def test_runtime_config_contract_rejects_missing_required_fields() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config({})

    assert result.ok is False
    assert result.error_codes == (
        "CONFIG_WORKSPACE_ROOT_MISSING",
        "CONFIG_ARTIFACT_DIR_MISSING",
        "CONFIG_TOOL_TIMEOUT_MISSING",
        "CONFIG_MAX_STEPS_MISSING",
    )


def test_runtime_config_contract_rejects_invalid_types() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config(
        {
            "workspace_root": "/tmp/workspace",
            "artifact_dir": "artifacts",
            "tool_timeout": "slow",
            "max_steps": 0,
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CONFIG_TOOL_TIMEOUT_INVALID", "CONFIG_MAX_STEPS_INVALID")


def test_runtime_config_contract_reports_dangerous_combinations() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config(
        {
            "workspace_root": "/",
            "artifact_dir": "artifacts",
            "tool_timeout": 30,
            "max_steps": 20,
            "allow_shell": True,
            "allow_dangerous_actions": True,
            "approval_required": False,
        }
    )

    assert result.ok is False
    assert result.error_codes == (
        "CONFIG_WORKSPACE_ROOT_DANGEROUS",
        "CONFIG_ALLOW_SHELL_ENABLED",
        "CONFIG_DANGEROUS_ACTIONS_WITHOUT_APPROVAL",
    )


def test_runtime_source_copy_keeps_live_metadata_without_mutating_base(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import live_switches as live
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.runtime_scope_config import (
        RuntimeConfigLayer,
        merge_runtime_config_layers,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))
    path = tmp_path / "agent.yaml"
    path.write_text("enable_tools: false\n", encoding="utf-8")
    base = load_config(path)
    live.register_live_config(base)
    before = base.config_source_snapshot()
    provenance = base.config_sources
    for scope in ("owner", "task", "run"):
        effective = merge_runtime_config_layers(base, [
            RuntimeConfigLayer(scope, f"{scope}-settings", {"tool_read_max_chars": 2000}),
        ])
        assert isinstance(effective.sources, live._ConfigSources)
        assert effective.sources is not provenance
        assert effective.sources.path == provenance.path
        assert effective.sources.applied is provenance.applied
        assert effective.sources.guard_applied is provenance.guard_applied
        assert effective.sources["enable_tools"] == provenance["enable_tools"]
        assert effective.sources["tool_read_max_chars"]["source"] == f"{scope}-settings"
        assert base.config_source_snapshot() == before


def test_task_projection_adopts_file_edit_before_first_refresh(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings import live_switches as live
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.services.runtime_config_task import (
        project_task_runtime_config_overlay,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))
    path = tmp_path / "agent.yaml"
    path.write_text("enable_tools: true\n", encoding="utf-8")
    base = load_config(path)
    path.write_text("enable_tools: false\n", encoding="utf-8")
    overlay = tmp_path / "task.yaml"
    overlay.write_text("tool_read_max_chars: 2000\n", encoding="utf-8")
    task = SimpleNamespace(runtime_identity=SimpleNamespace(config_overlay_ref=str(overlay), config_scope="task"))
    projected = project_task_runtime_config_overlay(base, task, workspace_root=tmp_path)
    source = projected.config_source_for("tool_read_max_chars")
    for _ in range(3):
        live.refresh_live_switches(projected, force=True)
        assert projected.enable_tools is False
        assert projected.tool_read_max_chars == 2000
        assert projected.config_source_for("tool_read_max_chars") == source
    assert getattr(base, "_live_switch_binding", None) is None
