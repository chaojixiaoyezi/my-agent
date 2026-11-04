"""通过临时根的真实提交验证版本、旧快照与提交后失败隔离。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets import _AssetStore
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    latest_version,
    list_versions,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool


# LLM: 只提供临时资产值，不执行代码或发现真实Owner。
# 函数用途: 构造同一脚本的完整描述符。
def descriptor(asset_id='demo', *, metadata=None):
    return AssetDescriptor(asset_id, 'script', asset_id, metadata=metadata or {})


# LLM: 版本断言来自W1公开读取口，不将返回revision误当发布版本。
# 函数用途: 验证首存、空白保存及实质变更的两个计数。
def test_first_save_whitespace_and_substantive_version(tmp_path):
    store = ScriptStore(tmp_path)
    first = store.save_script(descriptor(), 'print(1)\n')
    version = latest_version(tmp_path, 'script', 'demo')
    assert version is not None, '统一提交路径未记录首个正式版本'
    assert version['version'] == 1 and version['storage_revision'] == first['revision']
    same = store.save_script(descriptor(), 'print(1)  \r\n\r\n')
    assert same['revision'] == first['revision'] + 1
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1
    store.save_script(descriptor(), 'print(2)\n')
    assert [row['version'] for row in list_versions(tmp_path, 'script', 'demo')] == [2, 1]
    assert blob_get(tmp_path, get_version(tmp_path, 'script', 'demo', 1)['code_sha256']) == b'print(1)\n'


# LLM: 测试原始旧资产不经新钩子；只在临时Owner写入合法canonical旧格式。
# 函数用途: 首次覆盖无历史资产时保留legacy v1及新v2。
def test_legacy_preimage_including_renamed_entrypoint(tmp_path):
    root = tmp_path / 'scripts/demo'
    root.mkdir(parents=True)
    payload = descriptor().to_dict() | {'entrypoint': 'old.py', 'interpreter': 'python3',
                                      'timeout_seconds': 120, 'revision': 7}
    (root / 'script.json').write_text(json.dumps(payload))
    (root / 'old.py').write_text('print("old")\n')
    saved = ScriptStore(tmp_path).save_script(descriptor(), 'print("new")\n', entrypoint='new.py', expected_revision=7)
    previous = get_version(tmp_path, 'script', 'demo', 1)
    assert previous is not None, '旧descriptor pre-image尚未冻结'
    assert previous['legacy'] is True and previous['storage_revision'] == 7
    assert blob_get(tmp_path, previous['code_sha256']) == b'print("old")\n'
    assert get_version(tmp_path, 'script', 'demo', 2)['storage_revision'] == saved['revision'] == 8


# LLM: workflow保存使用完整步骤；对脚本的固定来源只能来自已提交代码SHA。
# 函数用途: 验证workflow版本保存完整定义、正文及脚本pin。
def test_workflow_pins_script_and_keeps_side_file(tmp_path):
    ScriptStore(tmp_path).save_script(descriptor(), 'print(1)\n')
    definition = WorkflowDefinition(AssetDescriptor('flow', 'workflow', 'flow'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),), '# 正文\n')
    saved = WorkflowStore(tmp_path).save_workflow(definition)
    record = latest_version(tmp_path, 'workflow', 'flow')
    assert record is not None, '工作流统一提交路径缺少版本钩子'
    assert record['pins']['scripts']['demo']['code_sha256'] == latest_version(tmp_path, 'script', 'demo')['code_sha256']
    assert record['pins']['scripts']['demo']['version'] == 1
    assert record['execution_definition_ref'] == saved.execution_definition_ref
    assert json.loads(blob_get(tmp_path, record['payload_blob']))['steps'][0]['script_id'] == 'demo'
    assert blob_get(tmp_path, record['side_blobs']['WORKFLOW.md']) == '# 正文\n'.encode()


# LLM: workspace与全局管理视图不生成Owner历史；晋升复用原正式提交路径。
# 函数用途: 验证scope隔离和promote_to_owner的版本。
def test_workspace_unversioned_and_promotion_versioned(tmp_path):
    from agent_py_agent.agent.assets.workspace_assets import promote_to_owner

    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    workspace.mkdir()
    ScriptStore(home, workspace_root=workspace).save_script(descriptor(), 'pass\n', target_scope='workspace')
    assert not (workspace / '.agent/data/asset_evolution').exists()
    assert latest_version(home, 'script', 'demo') is None
    promote_to_owner('script', 'demo', workspace_root=workspace, owner_home=home)
    assert latest_version(home, 'script', 'demo') is not None, '晋升未写Owner版本'
    assert latest_version(home, 'script', 'demo')['version'] == 1


# LLM: 正式历史只能写独立data根；删除仍沿原store目录算法且保留不可变快照。
# 函数用途: 验证历史不污染资产目录及delete软标记。
def test_delete_retains_history_and_asset_directory_is_unchanged(tmp_path):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert {path.name for path in (tmp_path / 'scripts/demo').iterdir()} == {'main.py', 'script.json'}
    assert latest_version(tmp_path, 'script', 'demo') is not None, '缺少正式历史'
    assert store.delete_script('demo') is True
    index = json.loads((tmp_path / 'data/asset_evolution/versions/script/demo.json').read_text())
    assert index['deleted_at']
    assert get_version(tmp_path, 'script', 'demo', 1) is not None


# LLM: 只向W1版本写入注入模拟I/O错误，不能使已提交Store保存回滚或冒充失败。
# 函数用途: 验证工具能见recorded=false且下次保存补录。
def test_hook_io_error_does_not_fail_commit_and_next_save_retries(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import revision_history

    original = revision_history.record_version

    # LLM: 错误发生在权威资产提交之后，参数签名与W1相同必要字段兼容。
    # 函数用途: 确定性模拟历史记录写入故障。
    def fail_record(owner_home, kind, asset_id, *, payload, code=None, side_files=None,
                    execution_definition_ref=None, storage_revision=None, pins=None,
                    summary_line='', published_at=None, legacy=False, restored_from=None, expected_latest=None):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(revision_history, 'record_version', fail_record)
    tool = ScriptsTool(tmp_path)
    result = tool.execute({'action': 'register', 'script_id': 'demo', 'descriptor': {
        'name': 'demo', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'code': 'pass\n'})
    assert result.ok, result.output
    receipt = json.loads(result.output)
    assert receipt.get('version_recording', {}).get('recorded') is False, '钩子失败未通过上下文暴露给工具'
    assert ScriptStore(tmp_path).get_script('demo')['code'] == 'pass\n'
    monkeypatch.setattr(revision_history, 'record_version', original)
    assert tool.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'}).ok
    assert latest_version(tmp_path, 'script', 'demo')['version'] >= 1


# LLM: 旧集合仍走原_write，但只有明确Owner的两类资产被版本化。
# 函数用途: 全局admin保存不在其根生成Owner历史。
def test_global_admin_save_is_not_owner_history(tmp_path):
    store = ScriptStore.for_global_admin(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert not (tmp_path / 'data/asset_evolution').exists()


# LLM: 一般运行摘要不是定义变更；不允许在私有spool元数据变化时产生新正式版本。
# 函数用途: 验证摘要fold与入口未变保存保持发布版本。
def test_run_summary_does_not_create_version(tmp_path):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert latest_version(tmp_path, 'script', 'demo') is not None
    store.record_run_summary('demo', 'succeeded', 1)
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1


# LLM: 原_write异常必须保持资产与版本不变，不能先写历史再失败提交。
# 函数用途: 反证提交前失败不会形成虚构正式版本。
def test_store_commit_failure_does_not_record_new_version(tmp_path, monkeypatch):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'old\n')
    assert latest_version(tmp_path, 'script', 'demo') is not None
    original = store._put

    # LLM: 只拒绝新正文，原undo仍能恢复。
    # 函数用途: 在canonical提交前制造可恢复失败。
    def fail_new(asset_id, filename, content):
        if filename == 'main.py' and content == 'new\n':
            raise OSError('simulated I/O error')
        return original(asset_id, filename, content)

    monkeypatch.setattr(store, '_put', fail_new)
    with pytest.raises(OSError):
        store.save_script(replace(descriptor(), summary='new'), 'new\n')
    assert store.get_script('demo')['code'] == 'old\n'
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1


# LLM: 测试必须覆盖两种原始batch目标，Owner提交历史不能在整个batch回滚之前发表。
# 函数用途: 验证batch提交与workspace批次不会漏录或错误升版。
def test_owner_batch_records_versions_only_after_success(tmp_path):
    from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch

    commit_asset_batch([{'kind': 'script', 'id': 'demo', 'descriptor': descriptor().to_dict(),
                         'code': 'pass\n'}], owner_home=tmp_path)
    assert latest_version(tmp_path, 'script', 'demo') is not None, 'batch未接版本钩子'


# LLM: 明确测试单提交接线，不以工具wrapper代替公共store入口。
# 函数用途: 保留原基座签名以守住W7保存合同。
def test_original_store_signatures_remain():
    import inspect

    assert list(inspect.signature(_AssetStore._locked).parameters) == ['self']
    assert list(inspect.signature(ScriptStore.save_script).parameters)[:3] == ['self', 'descriptor', 'code']


# LLM: 指纹不变不允许新建版本，但workflow pin必须落到同一条版本记录的原始代码SHA。
# 函数用途: 反证空白保存后的版本与SHA不会错配。
def test_whitespace_script_pin_matches_published_record(tmp_path):
    scripts = ScriptStore(tmp_path)
    scripts.save_script(descriptor(), 'print(1)\n')
    scripts.save_script(descriptor(), 'print(1)  \r\n\r\n')
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', 'flow'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),)))
    pin = latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts']['demo']
    record = get_version(tmp_path, 'script', 'demo', pin['version'])
    assert pin['code_sha256'] == record['code_sha256']
    assert len(list_versions(tmp_path, 'script', 'demo')) == 1


# LLM: 版本钩子自身的异常也发生在canonical提交之后；不能只防W1内部的IO异常。
# 函数用途: 反证提交后钩子异常不会让保存或删除失败。
@pytest.mark.parametrize('operation', ['save', 'delete'])
def test_hook_callback_failure_never_changes_commit_result(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.assets import version_hook

    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'old\n')

    # LLM: 故障来自钩子调用边界，不能进入原事务undo。
    # 函数用途: 模拟导入/回调初始化异常。
    def fail_hook(store, payload, previous=None, updates=None):
        raise ImportError('simulated hook initialization error')

    monkeypatch.setattr(version_hook, 'record_committed' if operation == 'save' else 'record_deleted', fail_hook)
    with version_hook.version_operation() as state:
        if operation == 'save':
            saved = store.save_script(descriptor(), 'new\n')
            assert saved['code'] == 'new\n'
        else:
            assert store.delete_script('demo') is True
            assert store.get_script('demo') is None
        assert state['recording']['recorded'] is False


# LLM: legacy快照的执行身份同样可供W8精确查找，不从新内容推导旧引用。
# 函数用途: 验证旧脚本pre-image保留原执行引用。
def test_legacy_preimage_has_execution_reference(tmp_path):
    root = tmp_path / 'scripts/demo'
    root.mkdir(parents=True)
    payload = descriptor().to_dict() | {'entrypoint': 'main.py', 'interpreter': 'python3',
                                      'timeout_seconds': 120, 'revision': 4}
    (root / 'script.json').write_text(json.dumps(payload))
    (root / 'main.py').write_text('old\n')
    store = ScriptStore(tmp_path)
    previous = store.get_script('demo')['execution_definition_ref']
    store.save_script(descriptor(), 'new\n')
    assert get_version(tmp_path, 'script', 'demo', 1)['execution_definition_ref'] == previous


def test_unchanged_legacy_workflow_save_records_complete_script_pins(tmp_path):
    from agent_py_agent.agent.assets.workflow_store import with_workflow_step_refs

    ScriptStore(tmp_path).save_script(descriptor(), 'print(1)\n')
    definition = with_workflow_step_refs(WorkflowDefinition(
        AssetDescriptor('flow', 'workflow', '旧工作流', revision=7),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),)))
    root = tmp_path / 'workflows/flow'
    root.mkdir(parents=True)
    payload = definition.descriptor.to_dict() | {'steps': [step.to_dict() for step in definition.steps],
                                               'body_markdown': ''}
    (root / 'workflow.json').write_text(json.dumps(payload))
    (root / 'WORKFLOW.md').write_text('')
    assert latest_version(tmp_path, 'workflow', 'flow') is None
    saved = WorkflowStore(tmp_path).save_workflow(definition, expected_revision=7)
    assert saved.descriptor.revision == 8
    legacy = get_version(tmp_path, 'workflow', 'flow', 1)
    assert legacy['legacy'] is True and legacy['pins']['scripts']['demo']['version'] == 1
    assert latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts'] == legacy['pins']['scripts']


def composition_fixture(home):
    scripts, workflows = ScriptStore(home), WorkflowStore(home)
    scripts.save_script(descriptor(), 'print(1)\n')
    base = workflows.save_workflow(WorkflowDefinition(AssetDescriptor('base', 'workflow', '基础流程'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),), '# 历史正文'))
    published = latest_version(home, 'workflow', 'base')
    scripts.save_script(descriptor(), 'print(2)\n')
    hidden = replace(base, descriptor=AssetDescriptor('monitor-watch', 'workflow', '隐藏组合', metadata={
        'hidden': True, 'base_workflow_pin': {'id': 'base', 'version': published['version']},
        'composition': {'monitor_id': 'watch', 'base_workflow_id': 'base', 'base_version': published['version'],
                        'appended_step_ids': ['tail']}}))
    return scripts, workflows, hidden, published


def test_composition_hook_keeps_historical_base_pins_and_appended_dependencies(tmp_path):
    scripts, workflows, hidden, base = composition_fixture(tmp_path)
    scripts.save_script(descriptor('other'), 'print(3)\n')
    hidden = replace(hidden, steps=hidden.steps + (
        WorkflowStepSpec('tail', '追加查询', 'deterministic_script', script_id='other'),))
    workflows.save_workflow(hidden)
    published = latest_version(tmp_path, 'workflow', 'monitor-watch')
    assert published['pins']['scripts']['demo'] == base['pins']['scripts']['demo']
    assert published['pins']['scripts']['other']['version'] == 1
    assert published['pins']['workflows'] == {'base': {'version': base['version']}}
    assert blob_get(tmp_path, published['side_blobs']['WORKFLOW.md']) == '# 历史正文'.encode()
    workflows.save_workflow(hidden)
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') == published


def test_composition_hook_does_not_publish_conflicting_dependency_identities(tmp_path):
    from agent_py_agent.agent.assets.version_hook import version_operation

    _, workflows, hidden, _ = composition_fixture(tmp_path)
    hidden = replace(hidden, steps=hidden.steps + (
        WorkflowStepSpec('tail', '同名但不同版本查询', 'deterministic_script', script_id='demo'),))
    with version_operation() as operation:
        workflows.save_workflow(hidden)
    assert operation['recording']['recorded'] is False
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') is None


@pytest.mark.parametrize('missing', ['base_payload', 'script_code'])
def test_composition_hook_rejects_incomplete_base_snapshots(tmp_path, missing):
    from agent_py_agent.agent.assets.version_hook import version_operation

    _, workflows, hidden, base = composition_fixture(tmp_path)
    digest = (base['payload_blob'] if missing == 'base_payload'
              else get_version(tmp_path, 'script', 'demo', base['pins']['scripts']['demo']['version'])['code_sha256'])
    (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    with version_operation() as operation:
        workflows.save_workflow(hidden)
    assert operation['recording']['recorded'] is False
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') is None
