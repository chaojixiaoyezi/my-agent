# LLM: 所有策略读写只落临时受信 Home；不读真实宿主，不使用旧 YAML 默认源，不联网。
# 模块用途: 验证唯一部署策略的类型、稀疏持久化、来源、错误与 CAS。
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.user_space.home_layout import home_paths


# LLM: 模拟宿主已解析的本机管理员身份，不从请求正文授予权限。
# 函数用途: 为每个测试创建独立的受信部署路径。
def _home(tmp_path):
    return replace(home_paths(tmp_path), owner_provider="local", owner_kind="main", owner_id="main")


# LLM: loader 必须只读 typed 默认值，首次安装不写满一个默认配置文件。
# 函数用途: 检查空安装的完整默认策略与无写入。
def test_deployment_policy_missing_uses_typed_defaults_without_writing(tmp_path):
    from agent_py_agent.agent.settings.deployment_policy import load_deployment_policy

    paths = _home(tmp_path)
    policy = load_deployment_policy(trusted_host_paths=paths)
    assert policy.runtime_guards.repeat_fail_threshold == 10
    assert policy.runtime_guards.tool_circuit_backoff_seconds == (1, 2, 4, 8, 16, 30)
    assert policy.cache.candidates.source_budget_bytes == 128 * 1024 * 1024
    assert policy.cache.tasks.item_source_max_bytes == 256 * 1024
    assert policy.cache.resident_budget_bytes is None
    assert policy.query.max_scan_items is None
    assert policy.query.filesystem_fallback.max_files == 20000
    assert policy.shutdown.total_grace_seconds == 5.0
    assert policy.to_dict() == {"schema_version": "deployment-policy.v1", "revision": 0}
    assert not paths.config_dir.exists()


# LLM: 显式写成默认值也是部署者意图，必须保留该覆盖；其它默认值不跟着落盘。
# 函数用途: 检查稀疏覆盖的规范回读与独立快照。
def test_deployment_policy_sparse_roundtrip_preserves_explicit_default(tmp_path):
    from agent_py_agent.agent.settings.deployment_policy import load_deployment_policy

    paths = _home(tmp_path)
    paths.config_dir.mkdir()
    raw = {"schema_version": "deployment-policy.v1", "revision": 7,
           "runtime_guards": {"tool_rate_max_calls": 60}, "cache": {"tasks": {"max_entries": 0}}}
    path = paths.config_dir / "deployment_policy.json"
    path.write_text(json.dumps(raw))
    policy = load_deployment_policy(trusted_host_paths=paths)
    assert policy.runtime_guards.tool_rate_max_calls == 60
    assert policy.cache.tasks.max_entries == 0
    assert policy.to_dict() == raw
    result = policy.to_dict()
    result["runtime_guards"]["tool_rate_max_calls"] = 1
    assert policy.runtime_guards.tool_rate_max_calls == 60 and policy.to_dict() == raw


# LLM: 坏文件、未知版本/键、bool 冒数字、非有限数和非法保护预算必须报错，不转空字典回默认。
# 函数用途: 参数化验证严格解析和错误脱敏。
@pytest.mark.parametrize("patch", [
    {"schema_version": "future"}, {"revision": True}, {"unexpected": "DO-NOT-ECHO"},
    {"runtime_guards": {"tool_rate_max_calls": True}}, {"runtime_guards": {"tool_rate_max_calls": -1}},
    {"runtime_guards": {"tool_rate_max_calls": "60"}}, {"runtime_guards": {"terminal_block_enabled": 1}},
    {"runtime_guards": {"tool_circuit_backoff_seconds": [float("nan")]}},
    {"runtime_guards": {"provider_transient_auto_resume_delays_seconds": ["invalid"]}},
    {"query": {"max_scan_items": 0}}, {"query": {"max_wall_seconds": float("inf")}},
    {"query": {"filesystem_fallback": {"max_files": 0}}}, {"cache": {"tasks": {"max_entries": -1}}},
])
def test_deployment_policy_invalid_payload_fails_closed(tmp_path, patch):
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        load_deployment_policy,
    )

    paths = _home(tmp_path)
    paths.config_dir.mkdir()
    path = paths.config_dir / "deployment_policy.json"
    path.write_text(json.dumps({"schema_version": "deployment-policy.v1", "revision": 0, **patch}))
    before = path.read_bytes()
    with pytest.raises(DeploymentPolicyError) as caught:
        load_deployment_policy(trusted_host_paths=paths)
    assert "DO-NOT-ECHO" not in str(caught.value) and path.read_bytes() == before


# LLM: 内容损坏与不存在分开；无论 cwd 有同名 JSON 还是旧 YAML，都不能成为后备。
# 函数用途: 确认坏文件不会静默改来源。
def test_deployment_policy_corruption_and_cwd_do_not_fall_back(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        load_deployment_policy,
    )

    paths = _home(tmp_path / "host")
    paths.config_dir.mkdir(parents=True)
    path = paths.config_dir / "deployment_policy.json"
    path.write_text("{bad-json")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deployment_policy.json").write_text('{"runtime_guards":{"repeat_fail_threshold":999}}')
    with pytest.raises(DeploymentPolicyError):
        load_deployment_policy(trusted_host_paths=paths)


# LLM: 写前先验身份/类型，锁内核 revision；失败保持原件，成功只保存显式覆盖。
# 函数用途: 核对真实稀疏写入、并发冲突及普通 owner 无写权限。
def test_deployment_policy_atomic_sparse_cas_and_permissions(tmp_path):
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        update_deployment_policy,
    )

    paths = _home(tmp_path)
    policy = update_deployment_policy(trusted_host_paths=paths, expected_revision=0,
                                      changes={"runtime_guards": {"tool_rate_max_calls": 12}})
    path = paths.config_dir / "deployment_policy.json"
    assert policy.revision == 1
    assert json.loads(path.read_text()) == {"schema_version": "deployment-policy.v1", "revision": 1,
                                          "runtime_guards": {"tool_rate_max_calls": 12}}
    before = path.read_bytes()
    for identity, revision in [(paths, 0), (replace(paths, owner_kind="user", owner_id="alice"), 1)]:
        with pytest.raises(DeploymentPolicyError):
            update_deployment_policy(trusted_host_paths=identity, expected_revision=revision,
                                     changes={"runtime_guards": {"tool_rate_max_calls": 13}})
        assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600


# LLM: 不只验证新对象序列化，必须证明真实守卫与工具限流读取同一宿主 JSON。
# 函数用途: 检查新部署策略已到达实际消费者且快照独立。
def test_deployment_policy_reaches_real_guard_and_rate_consumers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
        repeated_success_hint_threshold,
    )
    from agent_py_agent.agent.settings.deployment_policy import update_deployment_policy
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy
    from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy

    paths = _home(tmp_path)
    update_deployment_policy(trusted_host_paths=paths, expected_revision=0,
                             changes={"runtime_guards": {"tool_rate_max_calls": 12, "repeated_success_hint_threshold": 7}})
    monkeypatch.setenv("MY_AGENT_HOME", str(paths.root))
    policy = runtime_guard_policy()
    assert policy.tool_rate_max_calls == 12
    assert repeated_success_hint_threshold(SimpleNamespace(runtime_guard_policy=policy, task_attributes={})) == 7
    assert tool_rate_limit_policy(None).max_calls == 12
    assert policy.source_path == str(paths.config_dir / "deployment_policy.json")
    values = policy.values
    values["tool_rate_max_calls"] = 999
    assert policy.tool_rate_max_calls == 12


# LLM: 真正运行读口遇损坏同样失败，不能在 façade 中吞错误回旧 YAML 或 Python 默认。
# 函数用途: 验证错误贯穿旧公共读口而没有第二个权威。
def test_deployment_policy_corruption_reaches_runtime_guard_reader(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.deployment_policy import DeploymentPolicyError
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy

    paths = _home(tmp_path)
    paths.config_dir.mkdir()
    (paths.config_dir / "deployment_policy.json").write_text("{invalid")
    monkeypatch.setenv("MY_AGENT_HOME", str(paths.root))
    with pytest.raises(DeploymentPolicyError):
        runtime_guard_policy()


# LLM: 原合同明确空重试表关闭自动续跑，不能把空表当非法值或回落默认阶梯。
# 函数用途: 保留显式禁用供应商短链重试的语义。
def test_deployment_policy_empty_transient_schedule_disables_retries(tmp_path):
    from agent_py_agent.agent.settings.deployment_policy import update_deployment_policy

    policy = update_deployment_policy(trusted_host_paths=_home(tmp_path), expected_revision=0,
                                      changes={"runtime_guards": {"provider_transient_auto_resume_delays_seconds": []}})
    assert policy.runtime_guards.provider_transient_auto_resume_delays_seconds == ()
    assert policy.to_dict()["runtime_guards"]["provider_transient_auto_resume_delays_seconds"] == []


# LLM: 仅 provider/kind 标签不足以成为完整本机管理员；拒绝必须早于文件及 revision 变化。
# 函数用途: 从真实写入 API 验证不完整管理员身份不能改策略。
@pytest.mark.parametrize("owner_id", ["", None, False])
def test_deployment_policy_requires_complete_local_admin(tmp_path, owner_id):
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        update_deployment_policy,
    )

    paths = _home(tmp_path)
    update_deployment_policy(trusted_host_paths=paths, expected_revision=0, changes={"shutdown": {"total_grace_seconds": 6}})
    path = paths.config_dir / "deployment_policy.json"
    before = path.read_bytes()
    with pytest.raises(DeploymentPolicyError):
        update_deployment_policy(trusted_host_paths=replace(paths, owner_id=owner_id), expected_revision=1,
                                 changes={"shutdown": {"total_grace_seconds": 7}})
    assert path.read_bytes() == before


# LLM: 运行时覆写 API 没有生产调用方，直接结束该入口，不按不同方向的守卫字段猜 min/max。
# 函数用途: 确保不能用临时覆盖放大宿主守卫。
def test_deployment_policy_runtime_override_is_not_an_input_source(tmp_path):
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy

    with pytest.raises(TypeError):
        runtime_guard_policy(trusted_host_paths=_home(tmp_path), overrides={"tool_rate_max_calls": 999})


# LLM: 写入格式含缩进和 UTF-8 字节，超出实际读取帽必须提交前失败，不可先写出不可读文件。
# 函数用途: 验证最终合并策略过大时原件和 revision 不变。
def test_deployment_policy_oversized_update_is_rejected_before_commit(tmp_path):
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        update_deployment_policy,
    )

    paths = _home(tmp_path)
    update_deployment_policy(trusted_host_paths=paths, expected_revision=0, changes={"shutdown": {"total_grace_seconds": 6}})
    path = paths.config_dir / "deployment_policy.json"
    before = path.read_bytes()
    with pytest.raises(DeploymentPolicyError):
        update_deployment_policy(trusted_host_paths=paths, expected_revision=1,
                                 changes={"runtime_guards": {"unknown_command_allowlist": ["x" * 1024] * 1024}})
    assert path.read_bytes() == before


# LLM: JSON 解码递归错误也属于输入损坏，不能穿透为另一种不可预测异常或触发后备来源。
# 函数用途: 验证深层 JSON 从真实 loader 统一失败。
def test_deployment_policy_deep_json_is_a_structured_error(tmp_path):
    import sys

    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        load_deployment_policy,
    )

    paths = _home(tmp_path)
    paths.config_dir.mkdir()
    path = paths.config_dir / "deployment_policy.json"
    depth = min(sys.getrecursionlimit() + 100, 100000)
    path.write_text("[" * depth + "0" + "]" * depth)
    with pytest.raises(DeploymentPolicyError):
        load_deployment_policy(trusted_host_paths=paths)


# LLM: 两个调用者带同一 revision，只能一次提交；该验证不靠睡眠或吞异常伪造竞争。
# 函数用途: 验证线程内真实 CAS 和锁没有丢失更新。
def test_deployment_policy_concurrent_same_revision_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        load_deployment_policy,
        update_deployment_policy,
    )

    paths, barrier = _home(tmp_path), Barrier(2)

    # LLM: 工作线程在调用前同步到达；只捕获明确 revision 冲突，其它错误照常使测试失败。
    # 函数用途: 提交一个带相同初始版本的写入。
    def commit(seconds):
        barrier.wait(timeout=5)
        try:
            update_deployment_policy(trusted_host_paths=paths, expected_revision=0, changes={"shutdown": {"total_grace_seconds": seconds}})
            return "committed"
        except DeploymentPolicyError as exc:
            assert exc.reason == "deployment_policy_conflict"
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(commit, (6, 7)))
    assert sorted(outcomes) == ["committed", "conflict"]
    assert load_deployment_policy(trusted_host_paths=paths).revision == 1


# LLM: 注入旧解释器可能抛出的解码递归错误；只证明异常归一，不冒充本机原始输入或跨平台实测。
# 函数用途: 验证 JSON 解码器递归失败不会逃出配置错误接口。
def test_deployment_policy_decoder_recursion_error_is_normalized(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import deployment_policy as module

    paths = _home(tmp_path)
    paths.config_dir.mkdir()
    path = paths.config_dir / "deployment_policy.json"
    path.write_text('{"schema_version":"deployment-policy.v1"}')

    # LLM: 只替换本次解码故障，作用域退出立即还原，不修改解释器全局递归限制。
    # 函数用途: 模拟一个没有输入正文的解析器递归错误。
    def decode_failure(*args, **kwargs):
        raise RecursionError("synthetic decoder failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.json, "loads", decode_failure)
        with pytest.raises(module.DeploymentPolicyError):
            module.load_deployment_policy(trusted_host_paths=paths)
