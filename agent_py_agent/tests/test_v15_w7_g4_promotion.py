"""G4无损描述符合并与失败不覆盖。"""
import json
from dataclasses import fields

from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    AssetRefs,
    CredentialBindingRef,
    DataSourceSpec,
    IOContract,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.asset_evolution_replay import merge_evolution_candidate
from agent_py_agent.agent.capability.asset_promotion import _promote_script_asset
from agent_py_agent.tests.test_v15_w7_replay_gate import InProcessReplayRunner


# LLM: 合同完整覆盖描述符字段，未来字段新增时此测试也要求保留。
# 函数用途: 验证允许字段以外逐字段无损。
def test_g4_every_descriptor_field_preserved(tmp_path):
    descriptor = AssetDescriptor('sample', 'script', 'Original title', summary='old', version='3.0.0',
        owner_id='owner-a', access_targets=(AccessTarget('http', 'https', 'https://example.invalid', 'GET'),),
        data_sources=(DataSourceSpec('source', 'system', {'limit': 2}),),
        credential_refs=(CredentialBindingRef('credential-ref-opaque'),),
        refs=AssetRefs(skills=('skill-one',), workflows=('workflow-one',)),
        tags=('tag',), enabled=False, metadata={'trigger_matchers': {'monitor_ids': ['job48']},
            'member_of': [{'kind': 'workflow', 'id': 'workflow-one'}], 'custom': {'retained': True}},
        io_contract=IOContract(outputs=('old',)))
    store = ScriptStore(tmp_path)
    old = store.save_script(descriptor, 'def main(inputs):\n return {"old": True}\n', entrypoint='old.py', interpreter='python3', timeout_seconds=17)
    candidate = {'summary': 'new', 'io_contract': {'outputs': ['new']}, 'entrypoint': 'new.py',
                 'interpreter': 'python3.12', 'timeout_seconds': 19,
                 'metadata': {}, 'access_targets': [], 'enabled': True, 'credential_refs': [], 'refs': {}}
    merged, options = merge_evolution_candidate('sample', 'fallback', candidate, old)
    before, after = descriptor.to_dict(), merged.to_dict()
    for item in fields(AssetDescriptor):
        if item.name not in {'summary', 'io_contract', 'revision', 'metadata'}:
            assert after[item.name] == before[item.name], item.name
    assert after['metadata'] == old['metadata']
    assert after['summary'] == 'new' and after['io_contract']['outputs'] == ['new']
    assert options == {'entrypoint': 'new.py', 'interpreter': 'python3.12', 'timeout_seconds': 19}


# LLM: 依赖回放失败须发生在写入前，返回保原签名并留明确机器错误提案。
# 函数用途: 验证G4失败拒绝且不丢原代码或字段。
def test_g4_replay_failure_refuses(tmp_path):
    store = ScriptStore(tmp_path)
    old = store.save_script(AssetDescriptor('sample', 'script', 'sample', metadata={'keep': 'yes'}),
                            'def main(inputs):\n return {"old": 1}\n')

    # LLM: 仅注入已经失败的回放结论，验证发布器不能忽略它。
    # 函数用途: 模拟依赖输出不匹配。
    def failed(home, script_id, code, descriptor, *, options=None):
        return {'verdict': 'failed', 'replayed': 1, 'hashes': []}
    result = _promote_script_asset(tmp_path, 'sample', 'new',
        {'code': 'def main(inputs):\n return {"new": 2}\n', 'expected_revision': old['revision']}, replay_gate=failed)
    assert result is None
    assert store.get_script('sample')['code'] == old['code']
    assert store.get_script('sample')['revision'] == old['revision']
    proposals = list((tmp_path / 'data' / 'asset_evolution' / 'candidates' / 'script' / 'sample').glob('*.json'))
    assert len(proposals) == 1
    assert json.loads(proposals[0].read_text())['reason'] == 'ASSET_DEPENDENT_REPLAY_FAILED'


# LLM: 通过路径使用原commit/CAS与真实store，内存执行器只替换不可移植OS沙箱。
# 函数用途: 验证发布后原metadata与停用开关保留、允许字段生效。
def test_g4_success_preserves_disabled_and_metadata(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows as workflows

    monkeypatch.setattr(workflows, 'ScriptRunner', InProcessReplayRunner)
    store = ScriptStore(tmp_path)
    old = store.save_script(AssetDescriptor('sample', 'script', 'Original', enabled=False,
        refs=AssetRefs(skills=('keep',)), metadata={'trigger_matchers': {'monitor_ids': ['job48']}}),
        'def main(inputs):\n return {"ok": True}\n')
    committed = []

    # LLM: 原回调同步执行真实提交，证明发布器没绕来源写门。
    # 函数用途: 记录一次commit。
    def commit(operation):
        committed.append(True)
        return operation()
    result = _promote_script_asset(tmp_path, 'sample', 'updated',
        {'code': 'def main(inputs):\n return {"ok": True}\n# new\n', 'expected_revision': old['revision'],
         'io_contract': {'outputs': ['ok']}, 'entrypoint': 'updated.py', 'timeout_seconds': 22}, commit=commit)
    # 停用资产的正式binding不可执行，返回None不代表store未写；字段保存仍须精确可见。
    assert result is None and committed == [True]
    after = store.get_script('sample')
    assert after['revision'] == old['revision'] + 1
    assert after['enabled'] is False and after['name'] == 'Original'
    assert after['metadata'] == old['metadata'] and after['refs'] == old['refs']
    assert after['summary'] == 'updated' and after['entrypoint'] == 'updated.py' and after['timeout_seconds'] == 22
