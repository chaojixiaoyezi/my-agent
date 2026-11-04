"""登记表继承原语的持久化及隔离测试，不冒充工具输出索引的端到端验证。"""

from dataclasses import replace

import pytest

from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    ArtifactRegistryAttempt,
    inherit_artifact_registry,
    latest_artifact_records,
    register_artifact,
    registry_path,
    resolve_artifact_record,
)


# LLM: 所有文件都在测试 home 下，每次 Attempt 使用独立工作目录。
# 函数用途: 建立同一请求的两次 Attempt 及真实已登记文件。
def _attempts(tmp_path):
    home = tmp_path / 'owner-a'
    previous = ArtifactRegistryAttempt(home, home / 'attempt-1', 'owner-a', 'request-a', 'attempt-1')
    current = replace(previous, workspace_root=home / 'attempt-2', attempt_id='attempt-2')
    previous.workspace_root.mkdir(parents=True)
    artifact = previous.workspace_root / 'result.txt'
    artifact.write_text('verified output', encoding='utf-8')
    record = register_artifact(ArtifactRegistration(previous.workspace_root, artifact, artifact_id='result', run_id='original-run'))
    return previous, current, record


# LLM: 通过原公开解析入口读取继承后的引用，保留旧产物的路径、哈希和来源。
# 函数用途: 下一 Attempt 可解析原登记，第三次继承及重试不重复追加。
def test_same_request_owner_inherits_and_attempt_three_is_idempotent(tmp_path):
    previous, current, original = _attempts(tmp_path)
    assert inherit_artifact_registry(previous, current) == 1
    assert resolve_artifact_record(current.workspace_root, 'result') == original
    assert inherit_artifact_registry(previous, current) == 0
    third = replace(current, workspace_root=current.owner_home / 'attempt-3', attempt_id='attempt-3')
    assert inherit_artifact_registry(current, third) == 1
    assert inherit_artifact_registry(previous, third) == 0
    assert latest_artifact_records(third.workspace_root) == {'result': original}
    assert len(registry_path(third.workspace_root).read_text().splitlines()) == 1


# LLM: 两个 owner 即使误用相同 home 也不能借身份；空身份、其它请求及同一 Attempt 都不能继承。
# 函数用途: 不匹配的结构化身份在读写前拒绝，不产生目标登记表。
@pytest.mark.parametrize('field,value', [('owner_id', 'owner-b'), ('request_id', 'request-b'),
                                        ('request_id', ''), ('owner_id', ''), ('attempt_id', 'attempt-1')])
def test_scope_mismatch_inherits_nothing(tmp_path, field, value):
    previous, current, _ = _attempts(tmp_path)
    current = replace(current, **{field: value})
    assert inherit_artifact_registry(previous, current) == 0
    assert not registry_path(current.workspace_root).exists()


# LLM: 请求编号相同不授予跨 home 读取权。
# 函数用途: 另一个 owner home 不继承任何记录。
def test_different_owner_home_inherits_nothing(tmp_path):
    previous, current, _ = _attempts(tmp_path)
    current = replace(current, owner_home=tmp_path / 'owner-b', workspace_root=tmp_path / 'owner-b' / 'attempt-2')
    assert inherit_artifact_registry(previous, current) == 0
    assert latest_artifact_records(current.workspace_root) == {}


# LLM: 目标 Attempt 已登记的新版本优先，不能被旧来源覆盖。
# 函数用途: 相同产物编号在目标存在时不写回旧事实。
def test_current_attempt_record_is_not_overwritten(tmp_path):
    previous, current, _ = _attempts(tmp_path)
    current.workspace_root.mkdir(parents=True)
    artifact = current.workspace_root / 'new-result.txt'
    artifact.write_text('new output')
    new = register_artifact(ArtifactRegistration(current.workspace_root, artifact, artifact_id='result'))
    assert inherit_artifact_registry(previous, current) == 0
    assert resolve_artifact_record(current.workspace_root, 'result') == new


# LLM: 已有 no-follow 读口报告损坏或链接时不能把不完整来源当成完整继承。
# 函数用途: 损坏来源拒绝继承且不污染目标。
def test_corrupt_source_does_not_publish_partial_inheritance(tmp_path):
    previous, current, _ = _attempts(tmp_path)
    with registry_path(previous.workspace_root).open('a') as handle:
        handle.write('{invalid-json\n')
    with pytest.raises(ValueError, match='source'):
        inherit_artifact_registry(previous, current)
    assert not registry_path(current.workspace_root).exists()


# LLM: 相同身份不等于任意目录授权，两个工作目录都须在该 owner home 内。
# 函数用途: 拒绝工作目录逃出 owner home。
def test_workspace_scope_validation_rejects_outside_root(tmp_path):
    previous, current, _ = _attempts(tmp_path)
    current = replace(current, workspace_root=tmp_path / 'outside')
    with pytest.raises(ValueError):
        inherit_artifact_registry(previous, current)
