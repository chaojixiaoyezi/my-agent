"""W9 human-gate copy-command receipts, subagent model spec guidance, and Linux bwrap installer preflight tests."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ANGLE_PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")


def _assert_clean_single_line_commands(commands: list[str]) -> None:
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    assert commands, "expected at least one copy command"
    for cmd in commands:
        assert isinstance(cmd, str) and cmd.strip()
        assert "\n" not in cmd and "\r" not in cmd
        assert not _ANGLE_PLACEHOLDER_RE.search(cmd), f"unexpected placeholder in {cmd!r}"
        parsed = parse_conversation_control(cmd)
        assert parsed is not None and parsed.valid, f"command should parse cleanly: {cmd!r} ({parsed})"


def test_human_gate_receipts_contain_exact_copy_commands_and_no_placeholders(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import memory_tool, persona_tool, skill_transfer
    from agent_py_agent.agent.capability.agents_write_service import parse_agents_value
    from agent_py_agent.agent.capability.skill_transfer_control import execute_transfer_control
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.goal_tools import GetGoalTool
    from agent_py_agent.agent.gateway_parts import request_errors
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    # 1. persona_tool: AGENTS_WRITE_USER_ONLY without full content uses /agents show, never <JSON字符串>
    persona_dir = tmp_path / "persona"
    persona_dir.mkdir()
    for name, head in (("SOUL.md", "# SOUL\n"), ("USER.md", "# USER\n"), ("AGENTS.md", "# AGENTS\n")):
        (persona_dir / name).write_text(head, encoding="utf-8")
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(
            owner_soul_md=persona_dir / "SOUL.md",
            owner_user_md=persona_dir / "USER.md",
            owner_agents_md=persona_dir / "AGENTS.md",
        ),
    )
    execution = execute_canonical_test_call(
        persona_dir,
        tools={"update_persona": persona_tool.UpdatePersonaTool(agent)},
        tool_name="update_persona",
        arguments={"target": "agents", "content": "产物用 HTML"},
    )
    persona_doc = json.loads(execution.result.output)
    assert persona_doc["copy_command"] == "/agents show"
    assert "/agents show" in persona_doc["copy_commands"]
    assert "<JSON" not in execution.result.output
    assert not _ANGLE_PLACEHOLDER_RE.search(persona_doc["hint"])
    _assert_clean_single_line_commands(persona_doc["copy_commands"])

    # When full AGENTS.md text is explicitly available, emitted /agents write command is single-line and parser-valid
    full_outcome = persona_tool._agents_write_user_only_error("# Rules\n- Keep outputs structured\n")
    full_doc = json.loads(full_outcome.output)
    _assert_clean_single_line_commands([full_doc["copy_command"]])
    parsed_cmd = parse_conversation_control(full_doc["copy_command"])
    assert parsed_cmd.kind == "agents"
    _owner, operation, parsed_text = parse_agents_value(parsed_cmd.value)
    assert operation == "write"
    assert parsed_text == "# Rules\n- Keep outputs structured\n"

    # 2. memory_tool: REVIEW_REQUIRED emits /memory candidates preview <real_id> (validated against real parser)
    candidate_id = "cand_w9_real_42"
    candidate = SimpleNamespace(scope={"scope_type": "personal", "scope_key": ""}, candidate_id=candidate_id)
    promoted_item = memory_tool._promotion_result_with_scope(
        {
            "candidate_id": candidate_id,
            "promoted": False,
            "status": "pending",
            "reason_code": "REVIEW_REQUIRED",
            "promotion_ref": "",
        },
        candidate,
    )
    remember_outcome = memory_tool._remember_write_result("save", [promoted_item])
    remember_doc = json.loads(remember_outcome.output)
    expected_mem_cmd = f"/memory candidates preview {candidate_id}"
    assert remember_doc["results"][0]["copy_command"] == expected_mem_cmd
    assert remember_doc["copy_command"] == expected_mem_cmd
    assert remember_doc["copy_commands"] == [expected_mem_cmd]
    assert remember_doc["hint"].startswith("没记上")
    assert expected_mem_cmd in remember_doc["hint"]
    assert not _ANGLE_PLACEHOLDER_RE.search(remember_doc["hint"])
    _assert_clean_single_line_commands(remember_doc["copy_commands"])
    old_preview = parse_conversation_control(f"/memory preview {candidate_id}")
    assert old_preview is None or old_preview.valid is False

    # 3. skill_transfer & skill_transfer_control: real reference replaces <reference>
    from dataclasses import asdict

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    transfer_root = tmp_path / "transfer"
    source_owner = OwnerIdentity.provider_user("feishu", "ou_src")
    target_owner = OwnerIdentity.provider_group("feishu", "oc_dst")
    source_agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(transfer_root / "home"),
            enable_plugins=False,
            my_agent_owner_provider=source_owner.provider,
            my_agent_owner_kind=source_owner.owner_kind,
            my_agent_owner_id=source_owner.owner_id,
        ),
        transfer_root,
    )
    monkeypatch.setattr(skill_transfer, "read_fresh_capability_switch", lambda *_args: True)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope",
        lambda _base, _scope: source_agent,
    )
    skill_md = source_agent.home_paths.owner_home_dir / "skills" / "demo-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text("---\nname: demo-skill\ndescription: demo\n---\nBody\n", encoding="utf-8")
    entry = source_agent.skills_service.snapshot_for(force_reload=True).resolve("demo-skill")
    assert entry is not None

    offer = skill_transfer.prepare_transfer(
        source_agent,
        asdict(target_owner),
        [{"stable_id": entry.stable_id, "content_sha256": entry.content_sha256}],
    )
    ref = offer["reference"]
    assert "<reference>" not in json.dumps(offer, ensure_ascii=False)
    assert offer["copy_command"] == f"/skills transfer authorize {ref}"
    _assert_clean_single_line_commands(offer["copy_commands"])

    scope = GatewayControlScope(
        "ou_src",
        "feishu",
        "oc_private",
        resolved_owner=OwnerIdentity.provider_user("feishu", "ou_src"),
    )
    auth_res = execute_transfer_control(source_agent, scope, ["authorize", ref])
    assert auth_res.ok, auth_res.message
    assert "<reference>" not in auth_res.message
    assert auth_res.details["copy_command"] == f"/skills transfer accept {ref}"
    assert f"/skills transfer accept {ref}" in auth_res.message
    _assert_clean_single_line_commands(auth_res.details["copy_commands"])

    show_res = execute_transfer_control(source_agent, scope, ["show", ref])
    assert show_res.ok, show_res.message
    _assert_clean_single_line_commands(show_res.details["copy_commands"])

    # 4. goal_tools: ambiguous GOAL_STATE_CONFLICT lists concrete /goal <real_id> resume commands
    g1 = SimpleNamespace(goal_id="goal_alpha_1", status="paused", public_dict=lambda metadata_only=False: {"goal_id": "goal_alpha_1", "status": "paused"})
    g2 = SimpleNamespace(goal_id="goal_beta_2", status="active", public_dict=lambda metadata_only=False: {"goal_id": "goal_beta_2", "status": "active"})
    fake_store = SimpleNamespace(
        goals=SimpleNamespace(
            load=MagicMock(side_effect=ValueError("ambiguous")),
            list=MagicMock(return_value=[g1, g2]),
        )
    )
    goal_tool = GetGoalTool(SimpleNamespace(conversation_store=fake_store))
    conflict = goal_tool._read("thread_w9", "", "", metadata_only=True)
    assert conflict.ok is False and conflict.error_code == "GOAL_STATE_CONFLICT"
    conflict_doc = json.loads(conflict.output)
    assert "目标编号" not in conflict_doc["message"]
    assert conflict_doc["copy_commands"] == ["/goal goal_alpha_1 resume", "/goal goal_beta_2 resume"]
    _assert_clean_single_line_commands(conflict_doc["copy_commands"])

    # 5. request_errors: concrete recovery copy_command / copy_commands and zero placeholder recovery hints
    for code in (
        "MODEL_NOT_CONFIGURED",
        "MODEL_PROFILE_UNAVAILABLE",
        "ACTIVE_TURN_OUTCOME_UNCERTAIN",
        "RUN_RECOVERY_REQUIRED",
        "CONVERSATION_TASK_BINDING_CONFLICT",
        "AGENTS_WRITE_USER_ONLY",
        "PLUGIN_CONFIRMATION_REQUIRED",
        "END_TASK_LIVE_ATTEMPT",
        "END_TASK_STATE_CHANGED",
    ):
        msg = request_errors.gateway_client_error_message(code)
        assert not _ANGLE_PLACEHOLDER_RE.search(msg), f"{code} hint still contains placeholder: {msg!r}"
        cmds = request_errors.gateway_client_copy_commands(code)
        _assert_clean_single_line_commands(cmds)

    scoped_cmds = request_errors.gateway_client_copy_commands("GOAL_STATE_CONFLICT", goal_id="goal_alpha_1")
    assert scoped_cmds == ["/goal goal_alpha_1 resume"]
    _assert_clean_single_line_commands(scoped_cmds)

    worker_err = RuntimeError("storage lock check failed")
    worker_err.error_code = "RUN_RECOVERY_REQUIRED"
    worker_resp = request_errors.gateway_unhandled_worker_error_response(
        tmp_path / "req.json",
        {},
        worker_err,
        request_id="req-w9",
    )
    assert worker_resp["copy_command"] == "/recover"
    assert worker_resp["copy_commands"] == ["/recover"]


def test_subagent_tool_spec_unknown_model_guidance_no_blind_slash_model():
    from agent_py_agent.agent.agent_core.orchestration import tool_spec_data
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.capability import CapabilityRouter

    mock_agent = MagicMock()
    mock_agent.capability_router = CapabilityRouter()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10

    spec = CreateSubagentsTool(mock_agent).model_spec
    model_desc = spec.input_schema["properties"]["items"]["items"]["properties"]["model"]["description"]
    assert "user_config" in model_desc
    assert "self_map" in model_desc
    assert "继承" in model_desc
    assert "/model 新增" not in model_desc

    lines = Path(tool_spec_data.__file__).read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 120


def test_installer_linux_bwrap_preflight_nonblocking_warning(tmp_path, monkeypatch, capsys):
    from scripts.private_release import installer

    prefix = tmp_path / "install-root"
    bundled = (
        prefix
        / "current"
        / "runtime"
        / "lib"
        / "python3.14"
        / "site-packages"
        / "agent_py_agent"
        / "vendor"
        / "bin"
        / "bwrap.linux-x86_64"
    )
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")

    # Non-Linux skips preflight completely
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    calls: list[list[str]] = []
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: calls.append(list(a[0])))
    assert installer._linux_bwrap_preflight(prefix) is None
    assert calls == []

    # Linux with non-zero userns exit prints AppArmor kernel sysctl warning and stays non-blocking
    monkeypatch.setattr(installer.sys, "platform", "linux")

    def fail_bwrap(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=1, stdout="", stderr="bwrap: setting up uid map: Permission denied")

    monkeypatch.setattr(installer.subprocess, "run", fail_bwrap)
    warning = installer._linux_bwrap_preflight(prefix)
    assert warning is not None
    assert "kernel.apparmor_restrict_unprivileged_userns" in warning
    assert calls == [[str(bundled), "--unshare-user", "--", "true"]]

    # OSError / TimeoutExpired also stay non-blocking with diagnostic
    def timeout_bwrap(_cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="bwrap", timeout=3)

    monkeypatch.setattr(installer.subprocess, "run", timeout_bwrap)
    timeout_warn = installer._linux_bwrap_preflight(prefix)
    assert timeout_warn is not None
    assert "kernel.apparmor_restrict_unprivileged_userns" in timeout_warn

    # Full installer main() flow prints warning cleanly and exits 0
    monkeypatch.setattr(installer.subprocess, "run", fail_bwrap)
    monkeypatch.setattr(sys, "argv", ["installer.py", "--prefix", str(prefix)])
    monkeypatch.setattr(installer, "resolve_install_prefix", lambda _p: prefix)
    monkeypatch.setattr(installer, "verify", lambda _b: {"version": "0.0.1-test"})
    monkeypatch.setattr(
        installer,
        "install",
        lambda _b, p: {"version": "0.0.1-test", "prefix": str(p), "data": str(p / "data")},
    )
    assert installer.main() == 0
    stdout = capsys.readouterr().out
    assert "安装完成：0.0.1-test" in stdout
    assert "kernel.apparmor_restrict_unprivileged_userns" in stdout

    # When bundled binary is absent and system bwrap returns 0, no warning is emitted
    bundled.unlink()
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(installer.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert installer._linux_bwrap_preflight(prefix) is None
