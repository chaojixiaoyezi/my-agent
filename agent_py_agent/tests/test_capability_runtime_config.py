"""Focused tests for runtime capability config patching and hot reload.

函数/模块用途: 验证后台补丁服务可以安全调整 capability_config，并让后续调度读取新配置。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import (
    DispatchSubagentsTool,
    _dispatch_capability_config,
)
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.runtime_config import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    apply_capability_config_patch,
    capability_config_version,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)


def _write_config(path, *, run_timeout: int = 900, routing: bool = False) -> None:
    path.write_text(
        "\n".join(
            [
                "# runtime config fixture",
                f"enable_capability_routing: {'true' if routing else 'false'}",
                f"subagent_run_timeout: {run_timeout}",
                "subagent_heartbeat_timeout: 180",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_safe_patch_applies_with_audit(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    audit_path = tmp_path / "audit.jsonl"
    notice_path = tmp_path / "notice.md"
    _write_config(config_path)
    before = capability_config_version(config_path)

    result = apply_capability_config_patch(
        CapabilityConfigPatchRequest(
            config_path=config_path,
            patches=[
                CapabilityConfigPatch(
                    field="subagent_run_timeout",
                    value=1200,
                    reason="真实 E2E 需要更长 runner 时间。",
                )
            ],
            apply=True,
            expected_version=before,
            actor="agent:self_heal",
            reason="avoid false timeout",
            audit_path=audit_path,
            notice_path=notice_path,
            scope={"run_id": "root-1"},
        )
    )

    assert result.ok is True
    assert result.applied is True
    assert result.changed_fields == ["subagent_run_timeout"]
    assert result.version_before == before
    assert result.version_after != before
    assert result.config.subagent_run_timeout == 1200
    assert "subagent_run_timeout: 1200" in config_path.read_text(encoding="utf-8")
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert audit["changed_fields"] == ["subagent_run_timeout"]
    assert audit["scope"]["run_id"] == "root-1"
    assert "subagent_run_timeout" in notice_path.read_text(encoding="utf-8")


def test_manual_only_patch_returns_suggestion_without_mutating(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, routing=False)
    before_text = config_path.read_text(encoding="utf-8")

    result = apply_capability_config_patch(
        CapabilityConfigPatchRequest(
            config_path=config_path,
            patches=[CapabilityConfigPatch(field="enable_capability_routing", value=True)],
            apply=True,
        )
    )

    assert result.ok is True
    assert result.applied is False
    assert result.changed_fields == []
    assert result.suggestions[0]["field"] == "enable_capability_routing"
    assert result.suggestions[0]["reason"] == "manual_approval_required"
    assert config_path.read_text(encoding="utf-8") == before_text


def test_version_mismatch_blocks_write(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, run_timeout=900)
    stale_version = capability_config_version(config_path)
    _write_config(config_path, run_timeout=901)

    result = apply_capability_config_patch(
        CapabilityConfigPatchRequest(
            config_path=config_path,
            patches=[CapabilityConfigPatch(field="subagent_run_timeout", value=1200)],
            apply=True,
            expected_version=stale_version,
        )
    )

    assert result.ok is False
    assert result.applied is False
    assert result.message == "capability_config_version_mismatch"
    assert "subagent_run_timeout: 901" in config_path.read_text(encoding="utf-8")


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


def test_capability_config_patch_service_applies_safe_patch(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, run_timeout=900)

    result = apply_capability_config_patch(
        CapabilityConfigPatchRequest(
            config_path=config_path,
            apply=True,
            patches=[
                CapabilityConfigPatch(
                    field="subagent_run_timeout",
                    value=1800,
                    reason="真实 E2E runner 需要更长时间。",
                )
            ],
        )
    )

    assert result.ok is True
    assert result.applied is True
    assert result.changed_fields == ["subagent_run_timeout"]
    assert "subagent_run_timeout: 1800" in config_path.read_text(encoding="utf-8")


def test_capability_config_patch_tool_is_not_model_visible(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    specs = {spec.name: spec for spec in agent.tools.specs(include_orchestration=True)}

    assert "capability_config_patch" not in specs


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


def test_dispatch_tool_reports_capability_config_load_error(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    config_path.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.capability_config_path = config_path
    agent.dispatch_subagents = lambda *_args, **_kwargs: type(
        "Report",
        (),
        {"dry_run": True, "summary": {}, "records": []},
    )()

    result = DispatchSubagentsTool(agent).execute({"dry_run": True})

    payload = json.loads(result.output)
    assert payload["capability_config_load_error"]["context"] == "dispatch.capability_config.load"
    assert payload["capability_config_load_error"]["path"] == str(config_path)


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

    assert effective.runner_timeout_seconds == 123
    assert any(str(item.get("source", "")).endswith("overlays/runner.yaml") for item in effective.config_layers)
