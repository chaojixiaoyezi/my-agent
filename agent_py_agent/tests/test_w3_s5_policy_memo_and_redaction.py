"""S5: deployment/guard policy memo per source fingerprint and one host redaction per outcome."""

from __future__ import annotations

import json
import os
import pickle

import pytest

from agent_py_agent.agent.settings import deployment_policy as policy_module
from agent_py_agent.agent.settings import runtime_guard_config
from agent_py_agent.agent.settings.secret_ref import HostCredentialSnapshot
from agent_py_agent.agent.tooling import host_output_redaction as redaction_module
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.host_output_redaction import (
    HostOutputRedaction,
    redact_host_tool_outcome,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_tool_runtime_unification import _call, _CountingTool, _snapshot

_SECRET = "opaque-host-credential-value"


# LLM: 写一份合法部署策略；atomic=True 走临时文件改名（换 inode），否则原位改写。
# 函数用途: 在隔离宿主下写部署策略文件。
def _write_policy(paths, threshold: int, *, atomic: bool = True) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / "deployment_policy.json"
    body = json.dumps({"schema_version": "deployment-policy.v1", "revision": threshold,
                       "runtime_guards": {"repeat_fail_threshold": threshold}})
    if atomic:
        temp = paths.config_dir / ".deployment_policy.tmp"
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, target)
    else:
        target.write_text(body, encoding="utf-8")


# LLM: 统计真实读取与 typed 校验次数；不改变被测函数行为。
# 函数用途: 安装源文件读取与 __post_init__ 计数器。
def _count_loads(monkeypatch) -> dict[str, int]:
    counts = {"reads": 0, "post_init": 0}
    real_read = policy_module._read_deployment_policy_source
    real_post_init = policy_module._PolicyNode.__post_init__

    # 函数用途: 计数部署策略源文件读取。
    def counting_read(*args, **kwargs):
        counts["reads"] += 1
        return real_read(*args, **kwargs)

    # 函数用途: 计数策略节点校验。
    def counting_post_init(self):
        counts["post_init"] += 1
        return real_post_init(self)

    monkeypatch.setattr(policy_module, "_read_deployment_policy_source", counting_read)
    monkeypatch.setattr(policy_module._PolicyNode, "__post_init__", counting_post_init)
    return counts


# LLM: 源文件离开粗粒度时间窗后，同一工具调用里的 10 次守卫读取只读一次、只校验一次，每次仍返回独立快照与正确来源。
# 函数用途: 验证部署/守卫策略按文件代次指纹复用。
def test_w3_s5_unchanged_policy_is_read_and_validated_once(tmp_path, monkeypatch):
    paths = home_paths(tmp_path / "home")
    _write_policy(paths, 7)
    monkeypatch.setattr(policy_module, "cache_entry_trustworthy", lambda _ns: True)
    counts = _count_loads(monkeypatch)
    policies = [runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths) for _ in range(10)]
    assert counts["reads"] == 1
    assert counts["post_init"] <= 8
    assert len({id(policy) for policy in policies}) == 10
    for policy in policies:
        assert policy.repeat_fail_threshold == 7
        assert policy.sources["repeat_fail_threshold"] == str(paths.config_dir / "deployment_policy.json")
        assert policy.sources["tool_rate_max_calls"] == "python:RuntimeGuardPolicy"
    object.__setattr__(policies[0], "_source_path", "changed-by-caller")
    assert runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths).source_path == str(
        paths.config_dir / "deployment_policy.json")


# LLM: 改名替换、原位同大小改写（恢复 mtime）都必须在下一次调用立即生效；新鲜文件（时间窗内）从不进缓存。
# 函数用途: 验证文件变化在下一次调用被采用。
@pytest.mark.parametrize("trusted", [True, False])
def test_w3_s5_changed_policy_is_picked_up_on_next_call(tmp_path, monkeypatch, trusted):
    paths = home_paths(tmp_path / "home")
    if trusted:
        monkeypatch.setattr(policy_module, "cache_entry_trustworthy", lambda _ns: True)
    _write_policy(paths, 11)
    assert runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths).repeat_fail_threshold == 11
    _write_policy(paths, 22)
    assert runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths).repeat_fail_threshold == 22
    target = paths.config_dir / "deployment_policy.json"
    before = target.stat()
    _write_policy(paths, 33, atomic=False)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths).repeat_fail_threshold == 33
    target.unlink()
    assert runtime_guard_config.runtime_guard_policy(trusted_host_paths=paths).repeat_fail_threshold == 10


# LLM: 缺失文件复用 typed 默认；文件出现、叶被换成符号链接时绝不由缓存兜底，链接仍由原 loader 拒绝。
# 函数用途: 验证缺失与链接两种来源变化不被缓存掩盖。
def test_w3_s5_missing_then_created_or_linked_policy_is_not_served_from_cache(tmp_path, monkeypatch):
    paths = home_paths(tmp_path / "home")
    monkeypatch.setattr(policy_module, "cache_entry_trustworthy", lambda _ns: True)
    counts = _count_loads(monkeypatch)
    for _ in range(3):
        assert policy_module.load_deployment_policy(trusted_host_paths=paths).revision == 0
    assert counts["reads"] == 1
    _write_policy(paths, 5)
    assert policy_module.load_deployment_policy(trusted_host_paths=paths).revision == 5
    assert policy_module.load_deployment_policy(trusted_host_paths=paths).revision == 5
    target = paths.config_dir / "deployment_policy.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    target.symlink_to(elsewhere)
    with pytest.raises(policy_module.DeploymentPolicyError):
        policy_module.load_deployment_policy(trusted_host_paths=paths)


# LLM: 读取失败与解码失败从不进缓存；修好文件后下一次调用读到新内容。
# 函数用途: 验证失败结果不被缓存。
def test_w3_s5_failed_loads_are_never_cached(tmp_path, monkeypatch):
    paths = home_paths(tmp_path / "home")
    monkeypatch.setattr(policy_module, "cache_entry_trustworthy", lambda _ns: True)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "deployment_policy.json").write_text("{bad-json", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(policy_module.DeploymentPolicyError):
            policy_module.load_deployment_policy(trusted_host_paths=paths)
    _write_policy(paths, 4)
    assert policy_module.load_deployment_policy(trusted_host_paths=paths).revision == 4


# 函数用途: 构造一份各受脱敏字段都含凭据的工具结果。
def _secret_outcome() -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "echo_tool", False, f"prefix {_SECRET} suffix", error_code="EXACT_FAILURE",
        recommended_action=f"retry without {_SECRET}", recovery_hint="password=plain-secret",
        result_envelope={"nested": [_SECRET, {"token": _SECRET}], "ok": "fine"},
        component_receipts=({"detail": _SECRET},),
    )


# LLM: 计数实际逐字符串脱敏次数，不改变脱敏结果。
# 函数用途: 安装 redact_host_text 计数器。
def _count_text_redactions(monkeypatch) -> list[int]:
    calls = [0]
    real = redaction_module.redact_host_text

    # 函数用途: 计数并转发真实文本脱敏。
    def counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(redaction_module, "redact_host_text", counting)
    return calls


# LLM: 同一快照下连续三层脱敏（handler 出口、执行器、最终投影）只有第一层真正逐字段处理，结果与三次完整脱敏完全一致。
# 函数用途: 验证每个结果只脱敏一次且输出不变。
def test_w3_s5_unchanged_outcome_is_redacted_once_across_passes(monkeypatch):
    snapshot = HostCredentialSnapshot(secret_values=(_SECRET,))
    calls = _count_text_redactions(monkeypatch)
    first = redact_host_tool_outcome(_secret_outcome(), snapshot=snapshot)
    per_pass = calls[0]
    assert per_pass > 0
    second = redact_host_tool_outcome(first, snapshot=snapshot)
    third = redact_host_tool_outcome(second, snapshot=snapshot)
    assert calls[0] == per_pass
    assert third is first
    assert _SECRET not in repr(third) and "plain-secret" not in repr(third)
    repeated = _secret_outcome()
    for _ in range(3):
        repeated = redaction_module._redact_host_tool_outcome(repeated, snapshot=snapshot, force_default=False)
    assert repeated == third


# LLM: 封记只证明“内容未变”：替换字段、原位改写容器、换快照或改 force_default 都必须重新完整脱敏。
# 函数用途: 验证任何变化都会触发重新脱敏。
def test_w3_s5_changed_outcome_or_policy_is_redacted_again():
    snapshot = HostCredentialSnapshot(secret_values=(_SECRET,))
    sealed = redact_host_tool_outcome(_secret_outcome(), snapshot=snapshot)
    sealed.output = f"late {_SECRET}"
    assert _SECRET not in redact_host_tool_outcome(sealed, snapshot=snapshot).output
    sealed = redact_host_tool_outcome(_secret_outcome(), snapshot=snapshot)
    sealed.result_envelope["late"] = _SECRET
    assert _SECRET not in repr(redact_host_tool_outcome(sealed, snapshot=snapshot).result_envelope)
    sealed = redact_host_tool_outcome(_secret_outcome(), snapshot=snapshot)
    sealed.component_receipts = ({"late": _SECRET},)
    assert _SECRET not in repr(redact_host_tool_outcome(sealed, snapshot=snapshot).component_receipts)
    other = "second-opaque-credential"
    sealed = redact_host_tool_outcome(
        ToolHandlerOutcome("echo_tool", True, f"{_SECRET} {other}"), snapshot=snapshot)
    assert other in sealed.output
    assert other not in redact_host_tool_outcome(
        sealed, snapshot=HostCredentialSnapshot(secret_values=(_SECRET, other))).output
    code = ToolHandlerOutcome("echo_tool", True, 'password = "illustrative literal"',
                              result_envelope={"tool_output_policy": {"redaction": "source_code"}})
    kept = redact_host_tool_outcome(code, snapshot=snapshot)
    assert "illustrative literal" in kept.output
    assert "illustrative literal" not in redact_host_tool_outcome(kept, snapshot=snapshot, force_default=True).output


# LLM: 封记只含进程内随机令牌、布尔与内容摘要，可随结果序列化；序列化内容不含凭据，换进程后令牌不再匹配。
# 函数用途: 验证封记不携带凭据且对序列化安全。
def test_w3_s5_redaction_seal_carries_no_credential_material():
    snapshot = HostCredentialSnapshot(secret_values=(_SECRET,))
    sealed = redact_host_tool_outcome(_secret_outcome(), snapshot=snapshot)
    seal = getattr(sealed, redaction_module._REDACTION_SEAL_ATTR)
    assert len(seal) == 3 and seal[1] is False
    assert _SECRET not in repr(seal)
    restored = pickle.loads(pickle.dumps(sealed))
    assert restored == sealed and _SECRET.encode() not in pickle.dumps(sealed)


# LLM: 真实执行链（handler 出口 + 执行器 + 最终投影，基线共 5 次整轮）对未再改动的结果只做一次逐字段脱敏，模型可见结果不含凭据。
# 函数用途: 验证执行器端到端只脱敏一次。
def test_w3_s5_executor_redacts_each_outcome_once(tmp_path, monkeypatch):
    class SecretTool(_CountingTool):
        # 函数用途: 返回带凭据的结果。
        def execute(self, params):
            self.executions += 1
            return ToolHandlerOutcome(self.model_spec.name, True, f"value {_SECRET}",
                                      result_envelope={"detail": _SECRET})

    snapshot = HostCredentialSnapshot(secret_values=(_SECRET,))
    probe = _count_text_redactions(monkeypatch)
    redact_host_tool_outcome(
        ToolHandlerOutcome("echo_tool", True, f"value {_SECRET}", result_envelope={"detail": _SECRET}),
        snapshot=snapshot)
    single_pass = probe[0]
    probe[0] = 0
    tool = SecretTool()
    result = ToolExecutor().execute(ToolExecutorRequest(
        call=_call(tool, {"value": "x"}), runtime_snapshot=_snapshot(tool), workspace_root=tmp_path,
        operation_store_required=False, host_output_redaction=HostOutputRedaction(snapshot=snapshot),
    ))
    assert tool.executions == 1
    assert _SECRET not in repr(result.result)
    assert probe[0] == single_pass
