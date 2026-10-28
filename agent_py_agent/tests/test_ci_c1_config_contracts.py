"""C1：浮点归一、恢复合同和常量同步入口的回归钉子。"""
from __future__ import annotations

import subprocess
import sys

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config


# LLM: 通过真实归一入口检查字符串及数字，不用改YAML默认值躲开类型转换。
# 函数用途: 验证空闲owner退避秒数始终转为非负浮点数。
@pytest.mark.parametrize("raw, expected", [("10.0", 10.0), ("1.25", 1.25), (0, 0.0)])
def test_idle_owner_backoff_is_coerced_to_float(raw, expected):
    values, warnings = normalize_agent_config({"background_idle_owner_max_backoff_seconds": raw})
    value = values["background_idle_owner_max_backoff_seconds"]
    assert type(value) is float and value == expected
    assert not any("background_idle_owner_max_backoff_seconds" in item for item in warnings)


# LLM: 非法输入必须沿既有告警/默认策略处理，不把负值或坏字符串留给后台循环。
# 函数用途: 验证退避配置非法时回退浮点默认并给出字段告警。
@pytest.mark.parametrize("raw", ["bad", -1])
def test_invalid_idle_owner_backoff_reports_warning(raw):
    values, warnings = normalize_agent_config({"background_idle_owner_max_backoff_seconds": raw})
    assert values["background_idle_owner_max_backoff_seconds"] == AgentConfig().background_idle_owner_max_backoff_seconds
    assert any("background_idle_owner_max_backoff_seconds" in item for item in warnings)


# LLM: 每个真实产生码必须精确命中；权限、配额、未知副作用不能自动重放。
# 函数用途: 验证C1漏登记码和Broker四个动态码的恢复语义。
@pytest.mark.parametrize("code, category, retryable, action", [
    ("ASSET_DEPENDENCY_MISSING", "state", False, RecoveryAction.CHANGE_STRATEGY),
    ("BOT_TOOL_BUDGET_EXCEEDED", "resource", False, RecoveryAction.CHANGE_STRATEGY),
    ("BOT_TOOL_DENIED", "permission", False, RecoveryAction.REQUEST_PERMISSION),
    ("COMMAND_ADMIN_REQUIRED", "permission", False, RecoveryAction.REQUEST_PERMISSION),
    ("DATASOURCE_CONNECT_FAILED", "tool", False, RecoveryAction.CHANGE_STRATEGY),
    ("GATEWAY_RESTART_DRAINING", "state", True, RecoveryAction.RETRY),
    ("HOST_NOT_READY", "state", False, RecoveryAction.REPORT_BLOCKER),
    ("SCHEDULER_ASSET_RECOVERY_REQUIRED", "state", False, RecoveryAction.MANUAL_REVIEW),
    ("SCRIPT_REVISION_DRIFT", "state", False, RecoveryAction.REQUEST_USER_INPUT),
    ("WORKFLOW_OUTPUT_CONTRACT_VIOLATION", "contract", False, RecoveryAction.CHANGE_STRATEGY),
    ("WORKSPACE_CONTROL_INVALID", "contract", False, RecoveryAction.CHANGE_STRATEGY),
    ("WORKSPACE_DIR_INVALID", "contract", False, RecoveryAction.CHANGE_STRATEGY),
    ("WORKSPACE_REVISION_CONFLICT", "state", False, RecoveryAction.CHANGE_STRATEGY),
    ("TOOL_EXECUTION_DENIED", "permission", False, RecoveryAction.REQUEST_PERMISSION),
    ("TOOL_EXECUTION_TIMEOUT", "tool", False, RecoveryAction.MANUAL_REVIEW),
    ("TOOL_EXECUTION_ERROR", "tool", False, RecoveryAction.MANUAL_REVIEW),
    ("TOOL_EXECUTION_INCOMPLETE", "state", False, RecoveryAction.CHANGE_STRATEGY),
])
def test_c1_error_contracts_preserve_recovery_semantics(code, category, retryable, action):
    contract = error_contract(code)
    assert contract.code == code
    assert contract.category == category
    assert contract.retryable is retryable
    assert contract.recommended_action == action.value
    assert contract.recovery_hint


# LLM: 只在临时目录生成目录样本，真实命令必须正常退出，不用忽略失败包装。
# 函数用途: 验证任务要求的显式write入口可用且生成真实目录。
def test_constants_catalog_explicit_write_entrypoint(tmp_path):
    target = tmp_path / "catalog.json"
    result = subprocess.run([sys.executable, "scripts/build_constants_catalog.py", "--write", "--output", str(target)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert target.is_file()


# LLM: 同步检查必须是真实只读检查；源变化后必须非零退出，不能刷新目录掩盖过期。
# 函数用途: 验证同步脚本既能接受一致样本也能拒绝源码漂移。
def test_constants_sync_entrypoint_detects_drift_without_writing(tmp_path):
    import json

    from scripts.build_constants_catalog import build_catalog

    source = tmp_path / "agent_py_agent/agent/sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("# 请求超时秒数。\nREQUEST_TIMEOUT_SECONDS = 3\n", encoding="utf-8")
    target = tmp_path / "agent_py_agent/config/constants_catalog.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"constants": build_catalog(tmp_path)}, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, "scripts/build_constants_catalog.py", "--check", "--root", str(tmp_path)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr + first.stdout
    original = target.read_bytes()
    source.write_text("# 请求超时秒数。\nREQUEST_TIMEOUT_SECONDS = 4\n", encoding="utf-8")
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0
    assert target.read_bytes() == original


# LLM: 仅截获真实Shell构造的spec，不启动命令或冒称内核挂载已验证；同owner墙不得由此配置解除。
# 函数用途: 核对read_only/isolate实际进入沙箱而非只添加配置读取字符串。
@pytest.mark.parametrize("mode, scoped", [("read_only", False), ("isolate", False), ("isolate", True)])
def test_other_owner_policy_reaches_shell_sandbox(tmp_path, monkeypatch, mode, scoped):
    from types import SimpleNamespace

    from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    own = tmp_path / "owners/local/main"
    others = (tmp_path / "owners/local/users/alice", tmp_path / "owners/providers/feishu/users/bob")
    for path in (own, *others):
        path.mkdir(parents=True)
    seen = []
    monkeypatch.setattr(AttemptExecutionSandbox, "build_argv", lambda self, argv: seen.append(self.spec) or argv)
    policy = SimpleNamespace(private_roots=(), network_access=False, host_credential_env_names=frozenset(),
                             full_access_other_owner_homes=mode)
    _sandbox_exec("true", own, own if scoped else None, own, host_policy=policy)
    spec = seen[0]
    assert spec.full_access is not scoped
    assert spec.network_access is False
    assert not any(own.is_relative_to(path) for path in (*spec.protected_write_paths, *spec.hidden_paths))
    for other in others:
        assert any(other.is_relative_to(path) for path in spec.protected_write_paths) is (not scoped and mode == "read_only")
        assert any(other.is_relative_to(path) for path in spec.hidden_paths) is (not scoped and mode == "isolate")


# LLM: 新宿主字段只能是keyword-only，不能把原PTY第4位置的人格根误当其他owner策略。
# 函数用途: 锁定原宿主DTO/继承DTO位置参数兼容性。
def test_other_owner_host_policy_preserves_pty_positional_contract():
    from agent_py_agent.agent.tooling.pty_sessions import PtyHostPolicy

    policy = PtyHostPolicy(("private",), False, frozenset({"SECRET_NAME"}), "/persona")
    assert policy.protected_persona_root == "/persona"
    assert getattr(policy, "full_access_other_owner_homes", None) == "read_only"
