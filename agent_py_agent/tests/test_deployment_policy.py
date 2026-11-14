# LLM: 所有策略读写只落临时受信 Home；不读真实宿主，不使用旧 YAML 默认源，不联网。
# 模块用途: 验证唯一部署策略的类型、稀疏持久化、来源、错误与 CAS。
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io, nofollow_fs
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings import deployment_policy as policy_module
from agent_py_agent.agent.settings.deployment_policy import DeploymentPolicyError
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths


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


# LLM: 模拟 Windows 没有 dir_fd：缺失仍取 typed 默认，正常文件经 portable 链读回与提交确认；叶为符号链接仍拒绝，不降级跟随。
# 函数用途: 验证无 dir_fd 平台可加载部署策略，且链接拒绝不放宽。
def test_deployment_policy_without_dir_fd_uses_portable_chain(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.settings import deployment_policy as module

    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    paths = _home(tmp_path)
    assert module.load_deployment_policy(trusted_host_paths=paths).revision == 0
    paths.config_dir.mkdir()
    payload = {"schema_version": "deployment-policy.v1", "revision": 3}
    target = paths.config_dir / "deployment_policy.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(payload))
    target.symlink_to(elsewhere)
    with pytest.raises(module.DeploymentPolicyError):
        module.load_deployment_policy(trusted_host_paths=paths)
    target.unlink()
    target.write_text(json.dumps(payload))
    assert module.load_deployment_policy(trusted_host_paths=paths).revision == 3
    assert module._confirm_deployment_policy_commit(trusted_host_paths=paths, payload=payload).revision == 3


_CHANGES = {
    "runtime_guards": {"repeat_fail_threshold": 7, "tool_rate_max_calls": 12},
    "shutdown": {"total_grace_seconds": 6},
    "cache": {"resident_budget_bytes": None},
}
_ALTERATIONS = (
    "missing", "revision_low", "revision_high", "revision_missing", "revision_bool",
    "same_revision_payload", "explicit_key_removed", "malformed", "symlink", "hardlink_metadata",
    "during_read_missing", "during_read_in_place",
)


def _commit_home(tmp_path, revision=0):
    paths = replace(home_paths(tmp_path), owner_provider="local", owner_kind="main", owner_id="main")
    if revision:
        paths.config_dir.mkdir(parents=True)
        (paths.config_dir / "deployment_policy.json").write_text(json.dumps({
            "schema_version": "deployment-policy.v1", "revision": revision,
            "query": {"max_scan_items": 101},
        }), encoding="utf-8")
    return paths


def _expected(revision):
    payload = {"schema_version": "deployment-policy.v1", "revision": revision + 1, **_CHANGES}
    if revision:
        payload["query"] = {"max_scan_items": 101}
    return payload


def _assert_receipt(error, *, committed, revision, path):
    assert isinstance(error, policy_module.DeploymentPolicyCommitError)
    assert error.committed is committed
    assert error.unconfirmed is True
    assert error.durability_confirmed is False
    assert error.expected_revision == revision
    reason = "deployment_policy_durability_unconfirmed" if committed else "deployment_policy_commit_unconfirmed"
    assert error.reason == reason
    assert "DO-NOT-ECHO" not in str(error)
    assert str(path) not in str(error)
    assert error.__cause__ is None


def _alter_target(path, payload, alteration, monkeypatch, calls):
    if alteration == "missing":
        path.unlink()
        return
    if alteration == "malformed":
        path.write_text("{DO-NOT-ECHO", encoding="utf-8")
        return
    if alteration in {"during_read_missing", "during_read_in_place"}:
        original_read = nofollow_fs._read_descriptor

        def read_then_change(descriptor, max_bytes):
            content = original_read(descriptor, max_bytes)
            calls.append("target_changed_during_read")
            monkeypatch.setattr(nofollow_fs, "_read_descriptor", original_read)
            if alteration == "during_read_missing":
                path.unlink()
            else:
                changed = json.loads(json.dumps(payload))
                changed["runtime_guards"]["repeat_fail_threshold"] = 8
                path.write_text(json.dumps(changed), encoding="utf-8")
            return content

        monkeypatch.setattr(nofollow_fs, "_read_descriptor", read_then_change)
        return
    if alteration == "hardlink_metadata":
        target_stat = path.stat()
        original = os.fstat

        def multilink_fstat(descriptor):
            metadata = original(descriptor)
            if (metadata.st_dev, metadata.st_ino) == (target_stat.st_dev, target_stat.st_ino):
                calls.append("multilink_metadata_checked")
                return os.stat_result((*metadata[:3], 2, *metadata[4:]))
            return metadata

        monkeypatch.setattr(os, "fstat", multilink_fstat)
        return
    if alteration == "symlink":
        other = path.parent.parent / "untrusted-copy.json"
        other.write_text(json.dumps(payload), encoding="utf-8")
        path.unlink()
        path.symlink_to(other)
        return
    changed = json.loads(json.dumps(payload))
    if alteration == "revision_low":
        changed["revision"] -= 1
    elif alteration == "revision_high":
        changed["revision"] += 1
    elif alteration == "revision_missing":
        del changed["revision"]
    elif alteration == "revision_bool":
        changed["revision"] = True
    elif alteration == "same_revision_payload":
        changed["runtime_guards"]["repeat_fail_threshold"] = 8
    elif alteration == "explicit_key_removed":
        del changed["cache"]
    else:
        raise AssertionError("未登记的故障注入类型")
    path.write_text(json.dumps(changed), encoding="utf-8")


def _fail_directory_fsync(monkeypatch, path, expected, *, alteration=None):
    original = json_io.os.fsync
    calls = []

    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert json.loads(path.read_text(encoding="utf-8")) == expected
            calls.append("replaced_before_directory_fsync")
            if alteration is not None:
                _alter_target(path, expected, alteration, monkeypatch, calls)
            raise OSError("DO-NOT-ECHO directory fsync failure")
        return original(descriptor)

    monkeypatch.setattr(json_io.os, "fsync", fsync)
    return calls


@pytest.mark.parametrize("revision", [0, 4])
def test_update_confirms_complete_payload_and_target_revision(tmp_path, revision):
    paths = _commit_home(tmp_path, revision)
    result = policy_module.update_deployment_policy(
        trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
    )
    path = paths.config_dir / "deployment_policy.json"
    assert result.to_dict() == _expected(revision)
    assert result.revision == revision + 1
    assert json.loads(path.read_text(encoding="utf-8")) == _expected(revision)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(paths.config_dir.glob(".*.tmp"))


@pytest.mark.parametrize("revision", [0, 4])
def test_directory_fsync_failure_reports_confirmed_target(tmp_path, monkeypatch, revision):
    paths = _commit_home(tmp_path, revision)
    path = paths.config_dir / "deployment_policy.json"
    expected = _expected(revision)
    calls = _fail_directory_fsync(monkeypatch, path, expected)
    with pytest.raises(policy_module.DeploymentPolicyError) as caught:
        policy_module.update_deployment_policy(
            trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
        )
    assert calls == ["replaced_before_directory_fsync"]
    _assert_receipt(caught.value, committed=True, revision=revision + 1, path=path)
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert not list(paths.config_dir.glob(".*.tmp"))
    with pytest.raises(policy_module.DeploymentPolicyError) as conflict:
        policy_module.update_deployment_policy(
            trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
        )
    assert conflict.value.reason == "deployment_policy_conflict"


@pytest.mark.parametrize("alteration", _ALTERATIONS)
def test_post_write_readback_rejects_changed_or_missing_target(tmp_path, monkeypatch, alteration):
    paths = _commit_home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    original = json_io.write_private_json_file_atomic_unlocked
    calls = []

    def write_then_alter(target, payload):
        original(target, payload)
        assert payload == _expected(4)
        calls.append("written")
        _alter_target(target, payload, alteration, monkeypatch, calls)

    monkeypatch.setattr(json_io, "write_private_json_file_atomic_unlocked", write_then_alter)
    with pytest.raises(policy_module.DeploymentPolicyError) as caught:
        policy_module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    expected_calls = ["written", "multilink_metadata_checked"] if alteration == "hardlink_metadata" else ["written"]
    if alteration in {"during_read_missing", "during_read_in_place"}:
        expected_calls.append("target_changed_during_read")
    assert calls == expected_calls
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert not list(paths.config_dir.glob(".*.tmp"))


@pytest.mark.parametrize("alteration", _ALTERATIONS)
def test_directory_fsync_failure_cannot_confirm_altered_target(tmp_path, monkeypatch, alteration):
    paths = _commit_home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    calls = _fail_directory_fsync(monkeypatch, path, _expected(4), alteration=alteration)
    with pytest.raises(policy_module.DeploymentPolicyError) as caught:
        policy_module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    expected_calls = ["replaced_before_directory_fsync"]
    if alteration == "hardlink_metadata":
        expected_calls.append("multilink_metadata_checked")
    if alteration in {"during_read_missing", "during_read_in_place"}:
        expected_calls.append("target_changed_during_read")
    assert calls == expected_calls
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert not list(paths.config_dir.glob(".*.tmp"))


def test_file_fsync_failure_does_not_claim_target_committed(tmp_path, monkeypatch):
    paths = _commit_home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    before = path.read_bytes()
    calls = []

    def fail_file_fsync(descriptor):
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)
        calls.append("file_fsync")
        raise OSError("DO-NOT-ECHO file fsync failure")

    monkeypatch.setattr(json_io.os, "fsync", fail_file_fsync)
    with pytest.raises(policy_module.DeploymentPolicyError) as caught:
        policy_module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    assert calls == ["file_fsync"]
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert path.read_bytes() == before
    assert not list(paths.config_dir.glob(".*.tmp"))


def _policy(root, threshold, *, revision=1):
    paths = ensure_my_agent_home(root)
    path = paths.root / "config" / "deployment_policy.json"
    path.write_text(json.dumps({"schema_version": "deployment-policy.v1", "revision": revision, "runtime_guards": {
        "repeat_fail_threshold": threshold,
        "main_agent_auto_resume_attempt_limit": threshold,
        "provider_transient_redispatch_limit": threshold,
    }}), encoding="utf-8")
    return path


def _agent(home, workspace):
    return SimpleAgent(AgentConfig(my_agent_home=str(home), model_backend="echo", prompt_files=[],
                                   enable_tools=False), workspace, defer_mcp_connect=True)


def test_simple_agent_guard_uses_configured_host_home(tmp_path, monkeypatch):
    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7)
    _policy(other, 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    agent = _agent(first, tmp_path / "workspace")
    assert agent.home_paths.root == first
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    assert getattr(agent.config, "runtime_guard_policy", None) is agent.runtime_guard_policy
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "changed"))
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    assert replace(agent.config).runtime_guard_policy is agent.runtime_guard_policy
    _policy(first, 11, revision=2)
    rebuilt = SimpleAgent(replace(agent.config), tmp_path / "reloaded-workspace", defer_mcp_connect=True)
    assert rebuilt.home_paths.root == first
    assert rebuilt.runtime_guard_policy.repeat_fail_threshold == 11
    assert rebuilt.config.runtime_guard_policy is rebuilt.runtime_guard_policy
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    _policy(first, 17, revision=3).write_text("{broken", encoding="utf-8")
    with pytest.raises(DeploymentPolicyError):
        SimpleAgent(replace(rebuilt.config), tmp_path / "invalid-reload", defer_mcp_connect=True)


def test_simple_agent_bad_configured_policy_fails_closed(tmp_path, monkeypatch):
    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7).write_text("{broken", encoding="utf-8")
    _policy(other, 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    with pytest.raises(DeploymentPolicyError):
        _agent(first, tmp_path / "workspace")


def test_runner_candidate_consumes_frozen_guard(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runner.dispatch import (
        RunnerCandidatePolicy,
        _is_dispatch_runner_candidate,
    )
    from agent_py_agent.agent.settings.deployment_policy import RuntimeGuardPolicy

    _policy(tmp_path / "home", 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    task = SimpleNamespace(id="test", attributes={}, status="FAILED", failure_type="transient_error",
                           runner_attempts=8, channel_status="", capability_requests=[], capability_gaps=[],
                           runner_active_attempt_id="", runner_session_pid=0)
    policy = RunnerCandidatePolicy()
    object.__setattr__(policy, "runtime_guard_policy", RuntimeGuardPolicy(provider_transient_redispatch_limit=7))
    assert not _is_dispatch_runner_candidate(task, policy=policy)


def test_planner_and_capability_sweep_prefilters_consume_agent_frozen_guard(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.service import (
        update_pending_work_state,
    )
    from agent_py_agent.agent.agent_core.planner_service import (
        PlannerStateParams,
        _collect_parent_planner_context,
    )
    from agent_py_agent.agent.capability.config import CapabilityConfig

    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7)
    _policy(other, 0)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    agent = _agent(first, tmp_path / "workspace")
    # 冻结后把磁盘和环境都改成 0（收紧）或 20（放宽），验证真实预筛入口始终只认 agent 冻结额度 7。
    _policy(first, 0, revision=2)
    _policy(other, 0, revision=2)

    from agent_py_agent.tests.test_subagent_listruns_cache import _selected_task_file

    request_id = 'frozen-guard-request'
    agent._current_run_params = SimpleNamespace(request_id=request_id)
    tasks = []
    for name, attempts in (('run-eligible', 6), ('run-exhausted', 8)):
        path = _selected_task_file(agent.subagents, name, 'BLOCKED')
        payload = json.loads(path.read_text())
        payload.update(failure_type='transient_error', runner_attempts=attempts,
                       provider_retry_count=attempts - 1, failure_retry_count=0,
                       no_progress_count=0, recovery_history_unknown_count=0)
        payload['attributes']['conversation_request_id'] = request_id
        path.write_text(json.dumps(payload))
        task = agent.subagents.load(name)
        agent.subagents.persistence.run_index.upsert(task)
        tasks.append(task)
    eligible_task, _exhausted_task = tasks
    monkeypatch.setattr(
        agent.subagents,
        "board",
        SimpleNamespace(
            build_board=lambda **_kw: {},
            due_check=lambda **_kw: {},
            plan_actions=lambda *_a, **_kw: [],
        ),
    )
    _, ctx = _collect_parent_planner_context(
        agent,
        PlannerStateParams(cfg=CapabilityConfig(), limit=10, max_runners=10, reviewer="", note=""),
    )
    assert [t.id for t in ctx.runner_candidates] == [eligible_task.id]

    _policy(first, 20, revision=3)
    _policy(other, 20, revision=3)
    assert update_pending_work_state(agent) is True
