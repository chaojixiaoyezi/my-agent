"""审计 #13(执行路径接线,medium/稳定)真测:特权工具动作落审计,审计接口不再零调用。

原状:AuditLogger 写齐脱敏/UUID/锁,但生产执行路径零调用。真在分发 seam 用 audit_privileged_tool_call
对特权工具(run_command/write_file/web_fetch...)落审计,断言审计文件真有 TOOL_EXECUTE 条目带
actor(owner)/target/成败;读类工具不审计;失败计 error;审计失败不冒泡;audit_enabled=False 不落盘。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import audit_dispatch
from agent_py_agent.agent.agent_core.audit_dispatch import audit_privileged_tool_call


def _config(tmp_path, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        audit_log_path=str(tmp_path / "audit"),
        audit_enabled=enabled,
        my_agent_owner_id="acme-corp",
        my_agent_owner_kind="main",
    )


def _read_audit(tmp_path) -> list[dict]:
    path = tmp_path / "audit" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_privileged_tool_call_is_audited(tmp_path) -> None:
    audit_dispatch.reset_for_test()
    agent = SimpleNamespace(config=_config(tmp_path))
    audit_privileged_tool_call(
        agent, {"tool": "run_command", "command": "ls -la /etc"}, SimpleNamespace(tool="run_command", ok=True)
    )
    entries = _read_audit(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "TOOL_EXECUTE"
    assert entry["target_type"] == "run_command"
    assert entry["target_id"] == "ls -la /etc"  # 命令是审计价值(脱敏白名单刻意不脱)
    assert entry["user_id"] == "acme-corp"  # actor = owner 身份
    assert entry["status"] == "success"
    assert entry["details"]["input_facts"]["field_names"] == ["command"]
    assert "ls -la /etc" not in json.dumps(entry["details"], ensure_ascii=False)


def test_non_privileged_tool_not_audited(tmp_path) -> None:
    audit_dispatch.reset_for_test()
    agent = SimpleNamespace(config=_config(tmp_path))
    audit_privileged_tool_call(
        agent, {"tool": "read_file", "path": "x"}, SimpleNamespace(tool="read_file", ok=True)
    )
    assert _read_audit(tmp_path) == []  # 读类工具不审计(只审特权动作)


def test_failed_privileged_tool_logged_as_error(tmp_path) -> None:
    audit_dispatch.reset_for_test()
    agent = SimpleNamespace(config=_config(tmp_path))
    audit_privileged_tool_call(
        agent,
        {"tool": "write_file", "path": "/tmp/x.txt"},
        SimpleNamespace(
            tool="write_file",
            ok=False,
            error_code="UNKNOWN_ERROR",
            reported_error_code="DISK_QUOTA_EXCEEDED",
        ),
    )
    entry = _read_audit(tmp_path)[0]
    assert entry["status"] == "error" and entry["target_type"] == "write_file" and entry["target_id"] == "/tmp/x.txt"
    assert entry["details"]["error_code"] == "UNKNOWN_ERROR"
    assert entry["details"]["reported_error_code"] == "DISK_QUOTA_EXCEEDED"


def test_web_fetch_url_and_nested_params(tmp_path) -> None:
    audit_dispatch.reset_for_test()
    agent = SimpleNamespace(config=_config(tmp_path))
    audit_privileged_tool_call(
        agent, {"tool": "web_fetch", "url": "https://x.com/api"}, SimpleNamespace(tool="web_fetch", ok=True)
    )
    # 参数嵌套在 params 下也能取到 target
    audit_privileged_tool_call(
        agent, {"tool": "run_command", "params": {"command": "whoami"}}, SimpleNamespace(tool="run_command", ok=True)
    )
    targets = [e["target_id"] for e in _read_audit(tmp_path)]
    assert "https://x.com/api" in targets and "whoami" in targets


def test_audit_failure_never_raises(tmp_path) -> None:
    audit_dispatch.reset_for_test()

    class _BoomConfig:
        @property
        def audit_log_path(self) -> str:
            raise RuntimeError("config explode")

    # agent 无 config / config 抛异常:静默跳过,绝不影响工具执行
    audit_privileged_tool_call(SimpleNamespace(), {"tool": "run_command", "command": "x"}, SimpleNamespace(tool="run_command", ok=True))
    audit_privileged_tool_call(SimpleNamespace(config=_BoomConfig()), {"tool": "run_command"}, SimpleNamespace(tool="run_command", ok=True))


def test_disabled_audit_writes_nothing(tmp_path) -> None:
    audit_dispatch.reset_for_test()
    agent = SimpleNamespace(config=_config(tmp_path, enabled=False))
    audit_privileged_tool_call(
        agent, {"tool": "run_command", "command": "ls"}, SimpleNamespace(tool="run_command", ok=True)
    )
    assert _read_audit(tmp_path) == []  # audit_enabled=False → 不落盘
