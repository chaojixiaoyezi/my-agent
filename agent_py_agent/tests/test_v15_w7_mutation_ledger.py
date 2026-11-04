"""四种真实捕获源、预算与不污染回执的回归。"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.mutation_ledger import AssetMutationLedger, freeze_asset_mutations
from agent_py_agent.agent.assets.revision_history import blob_get
from agent_py_agent.agent.assets.script_store import ScriptStore


# LLM: 临时Owner与workspace严格分离，不访问用户真实目录。
# 函数用途: 准备捕获环境。
@pytest.fixture
def capture(tmp_path):
    home, work = tmp_path / 'owner', tmp_path / 'work'
    home.mkdir()
    work.mkdir()
    return AssetMutationLedger(), {}, home, work


# LLM: 捕获后的state不得包含源码，最终blob则须逐字节等于原文件。
# 函数用途: 验证写改补丁加执行的完整链路。
@pytest.mark.parametrize('tool', ['write_file', 'edit_file', 'apply_patch'])
def test_workspace_sources_and_freeze(capture, tool):
    ledger, state, home, work = capture
    code = '# state invariant check\n' * 40 + 'print("verified")\n'
    (work / 'sample.py').write_text(code)
    args = {'path': 'sample.py'} if tool != 'apply_patch' else {'patch': '*** Begin Patch\n*** Update File: sample.py\n*** End Patch'}
    ledger.observe(state, tool, args, {'ok': True}, owner_home=home, workspace_root=work, call_id='write')
    ledger.observe(state, 'run_command', {'command': 'python3 sample.py'},
                   {'ok': True, 'exit_code': 0, 'stdout': 'verified'}, owner_home=home, workspace_root=work, call_id='run')
    assert code not in json.dumps(state)
    target = next(iter(state['asset_mutations']['targets'].values()))
    assert [row['schema_version'] for row in target['facts']] == ['asset_mutation.v1', 'asset_execution.v1']
    request = freeze_asset_mutations(home, state, run_id='run1')
    assert len(request['targets']) == 1
    assert blob_get(home, request['targets'][0]['final_sha256']) == code.encode()
    assert freeze_asset_mutations(home, state, run_id='run1') == request


# LLM: register/update从准确参数捕获；scripts run引用来自执行回执，不能由参数伪造。
# 函数用途: 验证正式脚本的两种变更动作及执行来源。
@pytest.mark.parametrize('action', ['register', 'update'])
def test_scripts_sources(capture, action):
    ledger, state, home, work = capture
    code = 'print("state check")\n'
    ScriptStore(home).save_script(AssetDescriptor('sample', 'script', 'sample'), code)
    ledger.observe(state, 'scripts', {'action': action, 'script_id': 'sample', 'code': code}, {'ok': True},
                   owner_home=home, workspace_root=work, call_id='save')
    ledger.observe(state, 'scripts', {'action': 'run', 'script_id': 'sample'},
                   {'ok': True, 'output_data': {'ok': True}, 'execution_definition_ref': 'receipt-ref'},
                   owner_home=home, workspace_root=work, call_id='exec')
    target = state['asset_mutations']['targets']['script:sample']
    assert target['last_mutation']['sha256'] == hashlib.sha256(code.encode()).hexdigest()
    assert target['last_execution']['execution_definition_ref'] == 'receipt-ref'
    assert freeze_asset_mutations(home, state, run_id='official')['targets'][0]['verified'] is True


# LLM: 大文件不读全文；目标上限不会靠追加oversize对象变成无界账。
# 函数用途: 验证字节和目标双帽。
def test_caps_and_oversize(capture):
    ledger, state, home, work = capture
    (work / 'large.py').write_bytes(b'x' * (ledger.max_capture_bytes + 1))
    ledger.observe(state, 'write_file', {'path': 'large.py'}, {'ok': True}, owner_home=home, workspace_root=work)
    fact = next(iter(state['asset_mutations']['targets'].values()))['last_mutation']
    assert fact['status'] == 'oversize' and fact['sha256'] is None
    for i in range(40):
        path = work / f'sample{i}.py'
        path.write_text('pass\n')
        ledger.observe(state, 'write_file', {'path': path.name}, {'ok': True}, owner_home=home, workspace_root=work)
    assert len(state['asset_mutations']['targets']) == 32
    assert state['asset_mutations']['oversize']['reason'] == 'target_cap'
    assert freeze_asset_mutations(home, state, run_id='oversized') is None


# LLM: 假定shell退出、失败重跑、复合命令及后改文件都不能成为成功证明。
# 函数用途: 验证失败与未启动不冻结。
@pytest.mark.parametrize('receipt', [{'ok': False, 'exit_code': 1}, {'ok': True, 'status': 'started'},
                                   {'ok': True, 'exit_code': 3}])
def test_failed_or_background_run_not_verified(capture, receipt):
    ledger, state, home, work = capture
    (work / 'a.py').write_text('pass\n')
    ledger.observe(state, 'write_file', {'path': 'a.py'}, {'ok': True}, owner_home=home, workspace_root=work)
    ledger.observe(state, 'run_command', {'command': 'python3 a.py'}, receipt, owner_home=home, workspace_root=work)
    assert freeze_asset_mutations(home, state, run_id='failed') is None


# LLM: receipt按字节比较；observe内部任意普通异常必须吞掉，不能影响原工具。
# 函数用途: 验证观察无副作用与最佳努力边界。
def test_receipt_unchanged_and_observe_exception(capture, monkeypatch):
    ledger, state, home, work = capture
    receipt = {'ok': True, 'result': {'secret': 'not persisted', 'values': [1, 2]}}
    before = json.dumps(receipt, sort_keys=True).encode()
    ledger.observe(state, 'read_file', {}, receipt, owner_home=home, workspace_root=work)
    assert json.dumps(receipt, sort_keys=True).encode() == before

    # LLM: 有意模拟观察器内部错误，不修改原工具行为。
    # 函数用途: 提供一个失败的内部映射。
    class BrokenState(dict):
        # LLM: 对所有state初始化失败，要求外层观察吞掉。
        # 函数用途: 模拟账本I/O前错误。
        def setdefault(self, key, default=None):
            raise RuntimeError('simulated I/O error')
    ledger.observe(BrokenState(), 'read_file', {}, receipt, owner_home=home, workspace_root=work)
    assert json.dumps(receipt, sort_keys=True).encode() == before


# LLM: 真正接线也必须吞观察异常，并保持IR/发现持久化入口的原有调用。
# 函数用途: 验证工具post archive seam。
def test_post_archive_hook_preserves_receipt(capture, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as loop
    from agent_py_agent.agent.conversation import tool_discovery

    _, state, home, work = capture
    (work / 'a.py').write_text('pass\n')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), root=work,
                            home_paths=SimpleNamespace(owner_home_dir=home))
    record = SimpleNamespace(params=SimpleNamespace(live_archive_state=state, run_id='run', attempt_id='a',
                                                    in_use_method_ids=('owner:carried',)),
        call=SimpleNamespace(tool_name='write_file', arguments={'path': 'a.py'}, call_id='c'),
        result=SimpleNamespace(ok=True, output={'written': True}, error_code=''))
    monkeypatch.setattr(loop, '_record_tool_call_ir_if_native', lambda record, rendered: None)
    monkeypatch.setattr(tool_discovery, 'persist_discovered_tools', lambda agent, params, archive: None)
    before = json.dumps(record.result.output).encode()
    loop._record_tool_post_archive(agent, record, {'tool': 'write_file'}, 'unchanged')
    assert state['asset_mutations']['targets']
    assert state['asset_mutations']['member_of'] == [{'kind': 'skill', 'id': 'carried'}]
    monkeypatch.setattr(AssetMutationLedger, 'observe', None)
    loop._record_tool_post_archive(agent, record, {'tool': 'write_file'}, 'unchanged')
    assert json.dumps(record.result.output).encode() == before


# LLM: 带入的在用方法与本轮成功读取只记归属提示，不写正文或授予资产执行权。
# 函数用途: 验证活跃Skill和Workflow归属随冻结保留。
def test_active_membership_is_captured(capture):
    ledger, state, home, work = capture
    ledger.observe(state, 'skill_search', {'action': 'get', 'skill_id': 'owner:check'}, {'ok': True},
                   owner_home=home, workspace_root=work)
    ledger.observe(state, 'workflows', {'action': 'run', 'workflow_id': 'flow'}, {'ok': True},
                   owner_home=home, workspace_root=work)
    (work / 'a.py').write_text('pass\n')
    ledger.observe(state, 'write_file', {'path': 'a.py'}, {'ok': True}, owner_home=home, workspace_root=work)
    ledger.observe(state, 'run_command', {'command': 'python3 a.py'}, {'ok': True, 'exit_code': 0},
                   owner_home=home, workspace_root=work)
    request = freeze_asset_mutations(home, state, run_id='members')
    assert request['member_of'] == [{'kind': 'skill', 'id': 'check'}, {'kind': 'workflow', 'id': 'flow'}]
    assert next(iter(state['asset_mutations']['targets'].values()))['last_mutation']['path'] == str(work / 'a.py')
