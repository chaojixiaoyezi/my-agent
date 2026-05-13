"""Focused tests for runtime capability config patching and hot reload.

函数/模块用途: 验证 agent 可以通过正式补丁服务安全调整 capability_config，并让后续调度读取新配置。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.capability_config_patch_tool import CapabilityConfigPatchTool
from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import _dispatch_capability_config
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability.runtime_config import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    apply_capability_config_patch,
    capability_config_version,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: _write_config creates a tiny capability config fixture with comments preserved by patching.
# 函数用途: 写入测试用 capability_config.yaml，避免依赖仓库里的真实配置文件。
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


# LLM: test_safe_patch_applies_with_audit locks the safe auto-change path.
# 函数用途: 安全字段能在版本匹配时自动写入、重新加载，并留下审计和通知文件。
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


# LLM: test_manual_only_patch_returns_suggestion prevents risky config flips from auto-applying.
# 函数用途: enable_capability_routing 这种全局行为开关只生成建议，不偷偷改用户配置。
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


# LLM: test_version_mismatch_blocks_write protects user or other-agent config edits.
# 函数用途: 文件版本和请求期望不一致时拒绝写入，避免覆盖并行修改。
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


# LLM: test_reload_updates_router_config_when_file_changes proves new dispatch cycles see new knobs.
# 函数用途: 配置文件变化后，reload 会更新 snapshot 和 router.config，已在跑的子代理不被直接改动。
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


# LLM: test_capability_config_patch_tool_applies_safe_patch proves the model has a formal self-heal lane.
# 函数用途: SimpleAgent 注册 capability_config_patch 后，模型可通过工具请求安全配置变更。
def test_capability_config_patch_tool_applies_safe_patch(tmp_path):
    config_path = tmp_path / "capability_config.yaml"
    _write_config(config_path, run_timeout=900)
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.capability_config_path = config_path

    result = CapabilityConfigPatchTool(agent).execute(
        {
            "apply": True,
            "patches": [
                {
                    "field": "subagent_run_timeout",
                    "value": 1800,
                    "reason": "真实 E2E runner 需要更长时间。",
                }
            ],
        }
    )

    assert result.ok is True
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert payload["changed_fields"] == ["subagent_run_timeout"]
    assert "subagent_run_timeout: 1800" in config_path.read_text(encoding="utf-8")


# LLM: test_capability_config_patch_tool_is_registered keeps tool discovery automatic.
# 函数用途: 主代理初始化后能在 Tool Catalog 看到配置补丁工具，用户不用手写 YAML。
def test_capability_config_patch_tool_is_registered(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    specs = {spec.name: spec for spec in agent.tools.specs(include_orchestration=True)}

    assert "capability_config_patch" in specs
    assert specs["capability_config_patch"].category == "orchestration"


# LLM: test_dispatch_tool_reads_runtime_capability_config proves future dispatch sees patched config.
# 函数用途: dispatch_subagents 工具构建内部配置时会读取 agent.capability_config_path 的最新文件。
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
