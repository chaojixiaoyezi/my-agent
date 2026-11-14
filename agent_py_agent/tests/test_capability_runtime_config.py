"""Focused tests for runtime capability config hot reload.

函数/模块用途: 验证 capability_config 热重载与运行快照读取新配置。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.tool_helpers import (
    _dispatch_capability_config,
)
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.runtime_config_reload import (
    capability_config_for_agent,
    capability_config_version,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)


def _write_config(path, *, run_timeout: int = 900, selection: bool = False) -> None:
    path.write_text(
        "\n".join(
            [
                "# runtime config fixture",
                f"enable_capability_package_selection: {'true' if selection else 'false'}",
                f"subagent_run_timeout: {run_timeout}",
                "subagent_heartbeat_timeout: 180",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("selection_enabled", [False, True])
def test_agent_capability_config_keeps_typed_cache_without_loading_file(tmp_path, selection_enabled):
    config = CapabilityConfig(enable_capability_package_selection=selection_enabled)
    snapshot = SimpleNamespace(config=config)
    agent = SimpleNamespace(
        _capability_config_runtime_snapshot=snapshot,
        capability_config_path=tmp_path / "missing.yaml",
    )

    assert capability_config_for_agent(agent) is config
    assert agent._capability_config_runtime_snapshot is snapshot


@pytest.mark.parametrize("selection_enabled", [False, True])
def test_agent_capability_config_ignores_mock_cache_and_uses_file(tmp_path, selection_enabled):
    path = tmp_path / "capability_config.yaml"
    path.write_text(
        f"enable_capability_package_selection: {str(selection_enabled).lower()}\n",
        encoding="utf-8",
    )
    agent = MagicMock()
    agent.capability_config_path = path

    config = capability_config_for_agent(agent)

    assert isinstance(config, CapabilityConfig)
    assert config.enable_capability_package_selection is selection_enabled
    assert agent._capability_config_runtime_snapshot.config is config
    assert agent._capability_config_runtime_snapshot.path == path


def test_agent_capability_config_missing_file_does_not_accept_mock_cache(tmp_path):
    # 替身上自动生成的快照不是 CapabilityConfig，不能被当成配置；缺文件时拿 dataclass 默认实例。
    agent = MagicMock()
    agent.capability_config_path = tmp_path / "missing.yaml"

    assert capability_config_for_agent(agent) == CapabilityConfig()


def test_agent_capability_config_malformed_file_does_not_accept_mock_cache(tmp_path):
    # 格式错误仍按原语义返回 None；替身缓存同样不能顶替。已退役键只告警忽略（a12fe40c5），故用现行字段的非法值。
    agent = MagicMock()
    agent.capability_config_path = tmp_path / "bad.yaml"
    agent.capability_config_path.write_text("owner_default_permission_mode: bogus\n", encoding="utf-8")

    assert capability_config_for_agent(agent) is None


def test_reload_updates_router_config_when_file_changes(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, run_timeout=900)
    snapshot = load_capability_config_snapshot(config_path)
    router = CapabilityRouter(config=snapshot.config)

    _write_config(config_path, run_timeout=1500)
    result = reload_capability_config_if_changed(snapshot, router=router)

    assert result.changed is True
    assert result.snapshot.config.subagent_run_timeout == 1500
    assert router.config.subagent_run_timeout == 1500


def test_dispatch_tool_reads_runtime_capability_config(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, run_timeout=1500)
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subs",
            runner_timeout_seconds=999,
        ),
        tmp_path,
    )
    agent.capability_config_path = config_path

    cfg = _dispatch_capability_config(agent)

    assert cfg.subagent_run_timeout == 1500


def test_automatic_dispatch_config_reports_capability_config_load_error(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    config_path.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.capability_config_path = config_path
    _dispatch_capability_config(agent)

    payload = agent._capability_config_load_error
    assert payload["context"] == "dispatch.capability_config.load"
    assert payload["path"] == str(config_path)


def test_task_config_overlay_ref_loads_as_runtime_layer(tmp_path):
    overlay = tmp_path / "overlays" / "runner.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("runner_timeout_seconds: 123\nrunner_concurrency: 1\n", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    task = agent.subagents.create_run(
        goal="需要运行层配置",
        thought="overlay ref 应该影响 worker config。",
        plan=["加载 overlay"],
        attributes={"config_overlay_ref": "overlays/runner.yaml", "config_scope": "run"},
    )

    effective = apply_task_runtime_config_overlay(agent.config, task, workspace_root=tmp_path)

    assert effective.runner_timeout_seconds == "123"
    assert any(str(item.get("source", "")).endswith("overlays/runner.yaml") for item in effective.config_layers)


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
