"""第 16 条：owner 隔离（普通 IM 用户都是这个模式）的 Shell 碰到沙箱边界时，让模型知道并换个做法。

来源：owner 隔离的 Shell 跑在系统沙箱里。macOS 上递归扫描等操作碰到被拒读的目录会报 EPERM 中途退出，模型只看到命令
失败，不知道是沙箱边界。修法只用结构化事实、不解析 stderr、不放宽沙箱：
- 回执的 sandbox 信封带 sandbox_active 与本次允许读写的目录（与启动沙箱同一份根）；
- 命令在沙箱里以非零码退出时再带 boundary_hint（"可能越界"与下一步建议码），渲染成模型可见的 [runtime-sandbox-facts]；
- run_command 说明里多一条静态边界提示；开关 shell_sandbox_boundary_facts 关掉则全部恢复旧样子。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_shell_sandbox_boundary_facts.py
"""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.tooling.runtime_facts import render_tool_runtime_facts
from agent_py_agent.agent.tooling.shell import (
    _OWNER_SANDBOX_BOUNDARY_HINT,
    ShellTool,
    ShellToolOptions,
)

needs_macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt 沙箱")
_ACTIONS = ["limit_to_allowed_roots", "request_capability", "ask_user"]


# 函数用途: 造一个 my-agent 数据根：本 owner（带工作区和一份笔记）与另一个 owner（带一份别人的笔记），返回各路径。
def _homes(tmp_path: Path) -> dict[str, Path]:
    root = (tmp_path / "home" / ".my-agent").resolve()
    owner = root / "owners" / "providers" / "feishu" / "users" / "me"
    other = root / "owners" / "providers" / "feishu" / "users" / "other"
    (owner / "workspace").mkdir(parents=True)
    (owner / "note.md").write_text("mine", encoding="utf-8")
    other.mkdir(parents=True)
    (other / "secret.md").write_text("not yours", encoding="utf-8")
    return {"root": root, "owner": owner, "workspace": owner / "workspace", "other": other}


# 函数用途: 装一个 owner 隔离的 run_command（拒读 my-agent 根，本 owner 放行），命令执行可按需替换。
def _owner_tool(paths: dict[str, Path], **options) -> ShellTool:
    return ShellTool(paths["workspace"], options=ShellToolOptions(
        owner_scope_root=str(paths["owner"]), host_private_roots=(str(paths["root"]),), **options,
    ))


def _stub(monkeypatch, tool: ShellTool, *, returncode: int = 0, raises: BaseException | None = None) -> None:
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=["bash"], returncode=returncode, stdout="", stderr="denied\n")

    monkeypatch.setattr(tool, "_run_command", run)


# ---------------------------------------------------------------- 回执事实（命令执行替换，平台无关）


def test_failed_command_carries_allowed_roots_and_a_boundary_hint(tmp_path, monkeypatch):
    paths = _homes(tmp_path)
    tool = _owner_tool(paths)
    _stub(monkeypatch, tool, returncode=1)

    outcome = tool.execute({"command": "ls -R ..", "working_dir": str(paths["workspace"])})

    sandbox = outcome.result_envelope["sandbox"]
    assert outcome.ok is False and outcome.error_code == "COMMAND_FAILED"
    assert sandbox["sandbox_active"] is True
    # H3：工作区里的 workspace/runtime 是宿主托管文件（会话与事件库），落在可写根里，所以列进只读。
    assert sandbox["allowed_roots"] == {"read_write": [str(paths["workspace"])],
                                        "read_only": [str(paths["owner"]), str(paths["workspace"] / "runtime")]}
    assert sandbox["boundary_hint"] == {"may_be_sandbox_boundary": True, "suggested_actions": _ACTIONS}
    # 旧字段不变；拒读根（my-agent 数据根）不交给模型。
    assert sandbox["file_scope"] == "owner_workspace_only" and sandbox["host_path_absence_proven"] is False
    assert str(paths["root"]) not in sandbox["allowed_roots"]["read_write"] + sandbox["allowed_roots"]["read_only"]
    facts = render_tool_runtime_facts(outcome.result_envelope)
    assert "[runtime-sandbox-facts]" in facts and str(paths["workspace"]) in facts
    assert json.loads(facts.split("\n", 2)[-1].splitlines()[-1])["sandbox"]["boundary_hint"]["suggested_actions"] == _ACTIONS


def test_successful_command_lists_roots_but_shows_the_model_nothing_new(tmp_path, monkeypatch):
    paths = _homes(tmp_path)
    tool = _owner_tool(paths)
    _stub(monkeypatch, tool, returncode=0)

    outcome = tool.execute({"command": "ls", "working_dir": str(paths["workspace"])})

    assert outcome.ok
    assert "boundary_hint" not in outcome.result_envelope["sandbox"]
    assert outcome.result_envelope["sandbox"]["allowed_roots"]["read_write"] == [str(paths["workspace"])]
    assert "[runtime-sandbox-facts]" not in render_tool_runtime_facts(outcome.result_envelope)


@pytest.mark.parametrize("raised", [subprocess.TimeoutExpired("bash", 1), OSError("spawn failed")], ids=["timeout", "spawn"])
def test_failures_that_never_exited_in_the_sandbox_get_no_hint(tmp_path, monkeypatch, raised):
    paths = _homes(tmp_path)
    tool = _owner_tool(paths)
    _stub(monkeypatch, tool, raises=raised)

    outcome = tool.execute({"command": "sleep 9", "working_dir": str(paths["workspace"]), "timeout": 1})

    assert outcome.ok is False
    assert "boundary_hint" not in outcome.result_envelope["sandbox"]
    assert "[runtime-sandbox-facts]" not in render_tool_runtime_facts(outcome.result_envelope)


def test_explicit_write_roots_are_what_the_model_is_told(tmp_path, monkeypatch):
    paths = _homes(tmp_path)
    granted = (tmp_path / "granted").resolve()
    granted.mkdir()
    tool = _owner_tool(paths)
    _stub(monkeypatch, tool, returncode=2)

    outcome = tool.execute({"command": "false", "working_dir": str(paths["workspace"]),
                            "__sandbox_write_roots": [str(paths["workspace"]), str(granted)]})

    assert outcome.result_envelope["sandbox"]["allowed_roots"]["read_write"] == [str(paths["workspace"]), str(granted)]


def test_switch_off_and_full_access_keep_the_old_receipt(tmp_path, monkeypatch):
    paths = _homes(tmp_path)
    off = _owner_tool(paths, sandbox_boundary_facts=False)
    _stub(monkeypatch, off, returncode=1)
    full = ShellTool(paths["workspace"], options=ShellToolOptions())
    _stub(monkeypatch, full, returncode=1)

    for tool in (off, full):
        outcome = tool.execute({"command": "false", "working_dir": str(paths["workspace"])})
        assert set(outcome.result_envelope["sandbox"]) == {"file_scope", "external_host_paths_hidden",
                                                            "host_path_absence_proven"}
        assert "[runtime-sandbox-facts]" not in render_tool_runtime_facts(outcome.result_envelope)


def test_description_hint_only_for_owner_isolation_with_the_switch_on(tmp_path):
    paths = _homes(tmp_path)

    def hinted(tool: ShellTool) -> bool:
        return _OWNER_SANDBOX_BOUNDARY_HINT in tool.model_spec.hints.avoid_when

    assert hinted(_owner_tool(paths))
    assert not hinted(_owner_tool(paths, sandbox_boundary_facts=False))
    assert not hinted(ShellTool(paths["workspace"], options=ShellToolOptions()))


def test_render_ignores_malformed_or_non_boundary_sandbox_facts():
    assert render_tool_runtime_facts({"sandbox": {"sandbox_active": "true", "boundary_hint": {}}}) == ""
    assert render_tool_runtime_facts({"sandbox": {"sandbox_active": True, "allowed_roots": {"read_write": ["/w"]}}}) == ""
    facts = render_tool_runtime_facts({"sandbox": {
        "sandbox_active": True, "allowed_roots": {"read_write": ["/w", 7, "x" * 2000], "read_only": "nope"},
        "boundary_hint": {"may_be_sandbox_boundary": True, "suggested_actions": ["ask_user", {"bad": 1}]},
    }})
    projected = json.loads(facts.splitlines()[-1])["sandbox"]
    assert projected["allowed_roots"] == {"read_write": ["/w"], "read_only": []}
    assert projected["boundary_hint"]["suggested_actions"] == ["ask_user"]


# ---------------------------------------------------------------- 配置开关


def test_switch_defaults_on_and_reaches_the_shell_tool(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent

    shipped = load_config(Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml")
    normalized, warnings = normalize_agent_config({"shell_sandbox_boundary_facts": "false"})
    assert AgentConfig().shell_sandbox_boundary_facts is True and shipped.shell_sandbox_boundary_facts is True
    assert normalized["shell_sandbox_boundary_facts"] is False and warnings == []
    on = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path / "a")
    off = SimpleAgent(AgentConfig(model_backend="echo", shell_sandbox_boundary_facts=False), tmp_path / "b")
    assert on.tools.tools["run_command"].sandbox_boundary_facts is True
    assert off.tools.tools["run_command"].sandbox_boundary_facts is False


# ---------------------------------------------------------------- macOS 真实 Seatbelt


def _seatbelt_ready(paths: dict[str, Path]) -> bool:
    probe = AttemptExecutionSandbox(AttemptSandboxSpec(
        attempt_view=paths["workspace"], staging_root=paths["workspace"],
        shared_workspace=paths["owner"], owner_home=paths["owner"],
    ))
    return probe.probe().ready


@needs_macos
def test_real_seatbelt_denial_comes_back_as_boundary_facts(tmp_path):
    paths = _homes(tmp_path)
    if not _seatbelt_ready(paths):
        pytest.skip("sandbox-exec 不可用")
    tool = _owner_tool(paths)

    denied = tool.execute({"command": f"cat {paths['other'] / 'secret.md'}", "working_dir": str(paths["workspace"])})
    scan = tool.execute({"command": f"find {paths['root']} -name '*.md'", "working_dir": str(paths["workspace"])})
    allowed = tool.execute({"command": f"cat {paths['owner'] / 'note.md'}", "working_dir": str(paths["workspace"])})

    for outcome in (denied, scan):
        assert outcome.ok is False and outcome.result_envelope["process"]["status"] == "exited"
        sandbox = outcome.result_envelope["sandbox"]
        assert sandbox["boundary_hint"]["may_be_sandbox_boundary"] is True
        assert sandbox["allowed_roots"]["read_only"] == [str(paths["owner"]), str(paths["workspace"] / "runtime")]
        assert "[runtime-sandbox-facts]" in render_tool_runtime_facts(outcome.result_envelope)
    assert "not yours" not in denied.output  # 沙箱本身不放宽
    assert allowed.ok and "mine" in allowed.output
    assert "boundary_hint" not in allowed.result_envelope["sandbox"]


# ---------------------------------------------------------------- 真实链路：模型下一次请求里看得到边界事实


# 类用途: 只替换供应商传输的假模型：第一次调用让 run_command 读别的 owner 的文件，之后收尾；记下每次请求。
class _Wire:
    def __init__(self, target: Path) -> None:
        self._ids = itertools.count(1)
        self._target = target
        self.payloads: list[dict] = []

    def __call__(self, request) -> dict:
        from agent_py_agent.agent.backends import gateway_helpers

        payload = json.loads(json.dumps(request.payload))
        gateway_helpers._emit_provider_attempt({"attempt_id": f"sb-{next(self._ids)}", "status": "started"})
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            import re

            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return self._reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}))
        self.payloads.append(payload)
        if not any(m.get("role") == "tool" for m in payload.get("messages") or [] if isinstance(m, dict)):
            return self._reply(payload, None, ("run_command", {"command": f"cat {self._target}"}))
        return self._reply(payload, "SB-DONE 读不到，范围外。", None)

    def _reply(self, payload: dict, text: str | None, call: tuple[str, dict] | None) -> dict:
        message: dict = {"role": "assistant", "content": text}
        if call is not None:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_sb_{next(self._ids)}", "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)},
            }]}
        return {"id": f"chatcmpl-sb-{next(self._ids)}", "object": "chat.completion", "created": int(time.time()),
                "model": str(payload.get("model") or ""),
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}}


@needs_macos
def test_model_sees_the_boundary_facts_on_its_next_request(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.concurrency.interrupt import register_interruptible
    from agent_py_agent.agent.conversation.control_commands import (
        conversation_request_interrupt_name,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths

    agent = SimpleAgent(AgentConfig(
        model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1", model_name="sb-scripted",
        api_key="sb-fake-key-not-a-credential", stream_enabled=False, model_context_window_tokens=200_000,
        enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
        gateway_workspace="gateway", orphan_supervision_interval_seconds=0, memory_curator_enabled=False,
        enable_self_learning=False,
    ), tmp_path / "root")
    owner = Path(agent.home_paths.owner_home_dir)
    other = Path(agent.home_paths.root) / "owners" / "providers" / "feishu" / "users" / "other"
    other.mkdir(parents=True)
    (other / "secret.md").write_text("not yours", encoding="utf-8")
    tool = agent.tools.tools["run_command"]
    if tool.path_access_policy.owner_scope_root is None or not _seatbelt_ready(
        {"workspace": owner, "owner": owner}
    ):
        pytest.skip("需要 owner 隔离形态与可用的 sandbox-exec")
    wire = _Wire(other / "secret.md")
    monkeypatch.setattr(http, "post_json", wire)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    request = {"id": "sb-1", "kind": "ask", "prompt": "SB 看看那份笔记", "status": "processing", "turn_phase": "open",
               "execution_attempt_id": "sb-attempt-1",
               "conversation": {"canonical_user_id": agent.home_paths.owner_id, "channel": "chat",
                                "channel_conversation_id": "sb-session", "channel_user_id": agent.home_paths.owner_id}}
    path = paths.processing / "sb-1.json"
    path.write_text(json.dumps(request), encoding="utf-8")

    with register_interruptible(conversation_request_interrupt_name("sb-1")):
        response = request_execution._handle_gateway_request(agent, path)

    assert response.get("ok") is True, response
    tool_messages = [m for m in wire.payloads[-1]["messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    text = "\n".join(str(m.get("content") or "") for m in tool_messages)
    assert "[runtime-sandbox-facts]" in text
    assert "limit_to_allowed_roots" in text and str(owner) in text
    assert str(other) not in text.split("[runtime-sandbox-facts]", 1)[1]
