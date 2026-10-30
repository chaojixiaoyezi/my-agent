# LLM: 所有故障只注入临时受信宿主的真实写入链；先复现目录同步与回读缺陷，不访问正式配置。
# 模块用途: 验证部署策略原子替换后的明确提交回执、完整目标校验与安全失败边界。
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace

import pytest

from agent_py_agent.agent.common import json_io, nofollow_fs
from agent_py_agent.agent.settings import deployment_policy as module
from agent_py_agent.agent.user_space.home_layout import home_paths

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


# LLM: 身份来自测试宿主绑定；非零初始版本含已有覆盖，以验证不是只比本次变更字段。
# 函数用途: 创建独立管理员路径并按需写入旧版本策略。
def _home(tmp_path, revision=0):
    paths = replace(home_paths(tmp_path), owner_provider="local", owner_kind="main", owner_id="main")
    if revision:
        paths.config_dir.mkdir(parents=True)
        (paths.config_dir / "deployment_policy.json").write_text(json.dumps({
            "schema_version": "deployment-policy.v1", "revision": revision,
            "query": {"max_scan_items": 101},
        }), encoding="utf-8")
    return paths


# LLM: 目标对象独立于被测回读计算，必须同时保留原覆盖、显式空值和新版本。
# 函数用途: 返回本次更新唯一应确认的完整稀疏载荷。
def _expected(revision):
    payload = {"schema_version": "deployment-policy.v1", "revision": revision + 1, **_CHANGES}
    if revision:
        payload["query"] = {"max_scan_items": 101}
    return payload


# LLM: 提交回执必须区分目标已确认与未知；未知不能伪称未写入，错误也不能带入正文或宿主路径。
# 函数用途: 严格核对专用异常的结构化状态、目标版本与脱敏信息。
def _assert_receipt(error, *, committed, revision, path):
    assert isinstance(error, module.DeploymentPolicyCommitError)
    assert error.committed is committed
    assert error.unconfirmed is True
    assert error.durability_confirmed is False
    assert error.expected_revision == revision
    reason = "deployment_policy_durability_unconfirmed" if committed else "deployment_policy_commit_unconfirmed"
    assert error.reason == reason
    assert "DO-NOT-ECHO" not in str(error)
    assert str(path) not in str(error)
    assert error.__cause__ is None


# LLM: 真实替换后制造干扰；本机硬链接创建被拒，额外用精确目标描述符的多链接元数据注入验证拒门，不声称实测创建。
# 函数用途: 按固定案例移除、篡改或替换目标文件，保留测试断言的独立输入。
def _alter_target(path, payload, alteration, monkeypatch, calls):
    if alteration == "missing":
        path.unlink()
        return
    if alteration == "malformed":
        path.write_text("{DO-NOT-ECHO", encoding="utf-8")
        return
    if alteration in {"during_read_missing", "during_read_in_place"}:
        original_read = nofollow_fs._read_descriptor

        # LLM: 读出目标后但返回前真实删除或原位改写；旧描述符已有正确字节不能证明当前目标仍有效。
        # 函数用途: 注入发生在安全打开之后的竞争，要求提交回读核对读取后的目标身份及元数据。
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

        # LLM: 只给实际目标 inode 的打开描述符报告两个链接，其余目录、锁和临时描述符保持真实元数据。
        # 函数用途: 精确注入多链接边界并记录原安全读链确实执行了校验。
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


# LLM: 补丁落在实际目录描述符的 fsync，不以模拟 writer 返回替代原子替换的事实。
# 函数用途: 注入目录同步失败并记录失败前目标已经落盘的证据。
def _fail_directory_fsync(monkeypatch, path, expected, *, alteration=None):
    original = json_io.os.fsync
    calls = []

    # LLM: 文件描述符刷盘照常执行；仅真实目录同步前核对完整载荷，再触发一次固定错误。
    # 函数用途: 区分临时文件与父目录刷盘，确保故障发生在替换之后。
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


# LLM: 正常提交必须返回真实读回的完整目标，初始零版本和已有非零版本都不能丢覆盖。
# 函数用途: 验证正常更新的版本、内容、私有权限与临时文件清理。
@pytest.mark.parametrize("revision", [0, 4])
def test_update_confirms_complete_payload_and_target_revision(tmp_path, revision):
    paths = _home(tmp_path, revision)
    result = module.update_deployment_policy(
        trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
    )
    path = paths.config_dir / "deployment_policy.json"
    assert result.to_dict() == _expected(revision)
    assert result.revision == revision + 1
    assert json.loads(path.read_text(encoding="utf-8")) == _expected(revision)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(paths.config_dir.glob(".*.tmp"))


# LLM: 目录刷盘错误不能把已替换目标说成未提交，也不能伪称耐久性已确认。
# 函数用途: 验证真实目录刷盘故障提供已提交且同步未确认的专用回执。
@pytest.mark.parametrize("revision", [0, 4])
def test_directory_fsync_failure_reports_confirmed_target(tmp_path, monkeypatch, revision):
    paths = _home(tmp_path, revision)
    path = paths.config_dir / "deployment_policy.json"
    expected = _expected(revision)
    calls = _fail_directory_fsync(monkeypatch, path, expected)
    with pytest.raises(module.DeploymentPolicyError) as caught:
        module.update_deployment_policy(
            trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
        )
    assert calls == ["replaced_before_directory_fsync"]
    _assert_receipt(caught.value, committed=True, revision=revision + 1, path=path)
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert not list(paths.config_dir.glob(".*.tmp"))
    with pytest.raises(module.DeploymentPolicyError) as conflict:
        module.update_deployment_policy(
            trusted_host_paths=paths, expected_revision=revision, changes=_CHANGES,
        )
    assert conflict.value.reason == "deployment_policy_conflict"


# LLM: 即使 writer 已正常返回，目标被删除、换版、改正文或替换成链接也必须失败关闭。
# 函数用途: 验证回读必须匹配完整稀疏载荷及版本，不能返回默认对象或同版本篡改对象。
@pytest.mark.parametrize("alteration", _ALTERATIONS)
def test_post_write_readback_rejects_changed_or_missing_target(tmp_path, monkeypatch, alteration):
    paths = _home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    original = json_io.write_private_json_file_atomic_unlocked
    calls = []

    # LLM: 原写入器必须真实完成一次，再进行外部干扰；不吞原始写入失败。
    # 函数用途: 在写入与正式回读之间精确插入目标变化。
    def write_then_alter(target, payload):
        original(target, payload)
        assert payload == _expected(4)
        calls.append("written")
        _alter_target(target, payload, alteration, monkeypatch, calls)

    monkeypatch.setattr(json_io, "write_private_json_file_atomic_unlocked", write_then_alter)
    with pytest.raises(module.DeploymentPolicyError) as caught:
        module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    expected_calls = ["written", "multilink_metadata_checked"] if alteration == "hardlink_metadata" else ["written"]
    if alteration in {"during_read_missing", "during_read_in_place"}:
        expected_calls.append("target_changed_during_read")
    assert calls == expected_calls
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert not list(paths.config_dir.glob(".*.tmp"))


# LLM: 同步失败后的核实同样不能只看 revision，或从链接副本与缺省对象推断已经提交。
# 函数用途: 验证目录同步故障与目标变化叠加时保守返回未知状态。
@pytest.mark.parametrize("alteration", _ALTERATIONS)
def test_directory_fsync_failure_cannot_confirm_altered_target(tmp_path, monkeypatch, alteration):
    paths = _home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    calls = _fail_directory_fsync(monkeypatch, path, _expected(4), alteration=alteration)
    with pytest.raises(module.DeploymentPolicyError) as caught:
        module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    expected_calls = ["replaced_before_directory_fsync"]
    if alteration == "hardlink_metadata":
        expected_calls.append("multilink_metadata_checked")
    if alteration in {"during_read_missing", "during_read_in_place"}:
        expected_calls.append("target_changed_during_read")
    assert calls == expected_calls
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert not list(paths.config_dir.glob(".*.tmp"))


# LLM: 共用 writer 不提供阶段回执，目标不匹配时不能凭异常文案断言从未替换；原件仍须保留。
# 函数用途: 验证替换前文件刷盘失败不会被误报已提交或泄露原异常正文。
def test_file_fsync_failure_does_not_claim_target_committed(tmp_path, monkeypatch):
    paths = _home(tmp_path, 4)
    path = paths.config_dir / "deployment_policy.json"
    before = path.read_bytes()
    calls = []

    # LLM: 实际临时文件刷盘时抛错，目录同步若先发生就让测试失败。
    # 函数用途: 注入确定发生在替换之前的文件同步错误。
    def fail_file_fsync(descriptor):
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)
        calls.append("file_fsync")
        raise OSError("DO-NOT-ECHO file fsync failure")

    monkeypatch.setattr(json_io.os, "fsync", fail_file_fsync)
    with pytest.raises(module.DeploymentPolicyError) as caught:
        module.update_deployment_policy(trusted_host_paths=paths, expected_revision=4, changes=_CHANGES)
    assert calls == ["file_fsync"]
    _assert_receipt(caught.value, committed=None, revision=5, path=path)
    assert path.read_bytes() == before
    assert not list(paths.config_dir.glob(".*.tmp"))
