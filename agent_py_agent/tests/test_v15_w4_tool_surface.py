"""使用真实临时Store验证资产工具公开动作和读取投影。"""
import json

import pytest

from agent_py_agent.agent.assets.revision_history import latest_version
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool


# LLM: 仅构造临时测试资产，不读取当前用户资产。
# 函数用途: 提供满足标准Key的脚本参数。
def script_params(*, metadata=None, code='print(1)\n'):
    return {'action': 'register', 'script_id': 'demo', 'code': code, 'descriptor': {
        'name': 'demo', 'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {}, 'metadata': metadata or {}}}


# LLM: 步骤绑定是真实结构化引用，不运行脚本。
# 函数用途: 提供工作流创建参数。
def workflow_params(*, metadata=None):
    return {'action': 'save', 'workflow_id': 'flow', 'definition': {'descriptor': {
        'name': 'flow', 'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {}, 'metadata': metadata or {}}, 'steps': [
            {'step_id': 'fetch', 'name': 'fetch', 'mode': 'deterministic_script', 'script_id': 'demo'}]}}


# LLM: 断言工具实际成功，禁止将调用本身当作业务通过。
# 函数用途: 调用公开工具并解析原回执。
def call(tool, params):
    result = tool.execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


# LLM: 非规范提示经W1纯函数处理，重存不产生新发布版本。
# 函数用途: 验证两个资产的归一化、L1字段和版本去重。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_normalized_metadata_and_l1_projection(tmp_path, kind):
    meta = {'member_of': ['skill:missing', 'skill:missing'],
            'trigger_matchers': {'intent_keywords': ['CHECK', 'check'], 'indicator_types': ['ipv4', 'unknown']},
            'source_system': 'source-a'}
    scripts = ScriptsTool(tmp_path)
    call(scripts, script_params(metadata=meta if kind == 'script' else {}))
    tool = scripts if kind == 'script' else WorkflowsTool(tmp_path)
    if kind == 'workflow':
        call(tool, workflow_params(metadata=meta))
    asset_id = 'demo' if kind == 'script' else 'flow'
    key = kind + '_id'
    data = call(tool, {'action': 'get', key: asset_id})
    assert data['trigger_matchers']['intent_keywords'] == ['check']
    assert data['trigger_matchers']['indicator_types'] == ['ipv4']
    assert data['member_of'] == [{'kind': 'skill', 'id': 'missing'}]
    assert data['source_system'] == 'source-a'
    assert data['known_pitfalls'] == [] and data['used_by'] == []
    assert data['versions']['latest'] == data['versions']['count'] == 1
    assert len(data['versions']['recent']) == 1
    if kind == 'workflow':
        assert data['bound_methods'][0]['id'] == 'demo'
        assert data['draft']['exists'] is False
        call(tool, workflow_params(metadata=meta))
    else:
        call(tool, {'action': 'update', key: asset_id, 'descriptor': script_params(metadata=meta)['descriptor']})
    assert latest_version(tmp_path, kind, asset_id)['version'] == 1


# LLM: 草稿只在W1独立草稿根，发布失败不能清掉可重试草稿。
# 函数用途: 验证draft/publish的版本、摘要及删除行为。
def test_workflow_draft_publish_and_missing_draft(tmp_path):
    call(ScriptsTool(tmp_path), script_params())
    tool = WorkflowsTool(tmp_path)
    params = workflow_params()
    call(tool, {**params, 'action': 'draft'})
    assert tool._store().get_workflow('flow') is None
    assert latest_version(tmp_path, 'workflow', 'flow') is None
    data = call(tool, {'action': 'publish', 'workflow_id': 'flow', 'summary_line': '首次发布'})
    assert data['version_recording']['version'] == 1
    assert latest_version(tmp_path, 'workflow', 'flow')['summary_line'] == '首次发布'
    assert call(tool, {'action': 'get', 'workflow_id': 'flow'})['draft']['exists'] is False
    result = tool.execute({'action': 'publish', 'workflow_id': 'flow'})
    assert not result.ok and json.loads(result.output)['error_code'] == 'ASSET_DRAFT_NOT_FOUND'
    assert result.effect_outcome != 'confirmed'


# LLM: 版本回滚必须追加版本，旧不可变记录和旧代码不能被替换。
# 函数用途: 验证两个工具的rollback及versions动作。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_rollback_appends_version_and_lists_history(tmp_path, kind):
    if kind == 'workflow':
        call(ScriptsTool(tmp_path), script_params())
    tool = ScriptsTool(tmp_path) if kind == 'script' else WorkflowsTool(tmp_path)
    first = script_params() if kind == 'script' else workflow_params()
    call(tool, first)
    changed = {**first, 'action': 'update', 'code': 'print(2)\n'} if kind == 'script' else workflow_params()
    if kind == 'workflow':
        changed['definition']['body_markdown'] = '第二版'
    call(tool, changed)
    key, asset_id = kind + '_id', 'demo' if kind == 'script' else 'flow'
    call(tool, {'action': 'rollback', key: asset_id, 'to_version': 1, 'expected_revision': 2})
    record = latest_version(tmp_path, kind, asset_id)
    assert record['version'] == 3 and record['restored_from'] == 1
    assert [x['version'] for x in call(tool, {'action': 'versions', key: asset_id})['versions']] == [3, 2, 1]
    if kind == 'script':
        assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(1)\n'


# LLM: 历史读取动作没有当前asset也能读取；执行不猜最新版。
# 函数用途: 验证删除后versions仍可用且无效版本结构化失败。
def test_history_after_delete_and_missing_version(tmp_path):
    tool = ScriptsTool(tmp_path)
    call(tool, script_params())
    call(tool, {'action': 'delete', 'script_id': 'demo'})
    assert call(tool, {'action': 'versions', 'script_id': 'demo'})['versions'][0]['version'] == 1
    result = tool.execute({'action': 'rollback', 'script_id': 'demo', 'to_version': 99, 'expected_revision': 0})
    assert not result.ok and result.effect_outcome != 'confirmed'


# LLM: W5是同波次合同；独立替身只检查tool到engine的明确keyword，不冒称真实执行链通过。
# 函数用途: 验证version只在请求时透传，且原并发策略保持。
def test_workflow_run_version_keyword_contract(tmp_path):
    from types import SimpleNamespace

    calls = []

    # LLM: 精确签名而非宽泛kwargs，确保漏接/错拼keyword会失败。
    # 函数用途: 捕获W5原执行入口的版本参数。
    def run(definition, inputs, *, concurrency_policy='allow', definition_version=None):
        calls.append((concurrency_policy, definition_version))
        return {'status': 'succeeded'}

    call(ScriptsTool(tmp_path), script_params())
    call(WorkflowsTool(tmp_path), workflow_params())
    tool = WorkflowsTool(tmp_path, engine=SimpleNamespace(owner_home=tmp_path, run_workflow=run))
    call(tool, {'action': 'run', 'workflow_id': 'flow', 'version': 1, 'concurrency_policy': 'skip_if_running'})
    assert calls == [('skip_if_running', 1)]


# LLM: 状态只读注入run-store；替身不准创建重型engine，旧活动run不应被终态挤走。
# 函数用途: 验证monitor来源、版本、步骤和防御性批次读取。
def test_status_reads_active_runs_and_batch_projection(tmp_path):
    from types import SimpleNamespace

    rows = [{'workflow_run_id': 'run-a', 'workflow_id': 'flow', 'status': 'running', 'started_at': 1,
        'definition_version': 3, 'trigger_kind': 'monitor', 'steps': [
            {'step_id': 'fetch', 'status': 'running', 'batches_summary': {'total': 5, 'completed': 2}}]}]

    # LLM: reader明确按状态过滤，确保工具不是先截全历史再筛活动。
    # 函数用途: 提供有界活动运行记录。
    def list_runs(workflow_id=None, status=None, limit=50):
        return [row for row in rows if row['status'] == status and (workflow_id is None or row['workflow_id'] == workflow_id)][:limit]

    tool = WorkflowsTool(tmp_path, engine=SimpleNamespace(owner_home=tmp_path, store=SimpleNamespace(list_runs=list_runs)))
    data = call(tool, {'action': 'status', 'workflow_id': None})
    row = data['runs'][0]
    assert row['workflow_run_id'] == 'run-a' and row['definition_version'] == 3
    assert row['current_step'] == 'fetch' and row['batch_progress'] == 'Batch 3/5'
    assert row['trigger_kind'] == 'monitor' and row['duration'] > 0
    assert call(tool, {'action': 'status', 'include_monitors': False})['runs'] == []


# LLM: publish的最后一步必须等历史发表成功，失败不能丢草稿或宣称confirmed。
# 函数用途: 反证历史失败时保留可重试的草稿。
def test_publish_history_failure_retains_draft(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import revision_history
    from agent_py_agent.agent.assets.revision_history import get_draft

    call(ScriptsTool(tmp_path), script_params())
    tool = WorkflowsTool(tmp_path)
    call(tool, {**workflow_params(), 'action': 'draft'})

    # LLM: 真实canonical写仍执行，只让历史记录出现模拟IO错误。
    # 函数用途: 使发布在旁账阶段失败。
    def fail_record(owner_home, kind, asset_id, *, payload, code=None, side_files=None,
                    execution_definition_ref=None, storage_revision=None, pins=None,
                    summary_line='', published_at=None, legacy=False, restored_from=None, expected_latest=None):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(revision_history, 'record_version', fail_record)
    result = tool.execute({'action': 'publish', 'workflow_id': 'flow'})
    assert get_draft(tmp_path, 'workflow', 'flow') is not None
    assert tool._store().get_workflow('flow') is not None
    assert not result.ok and result.effect_outcome != 'confirmed'
    assert json.loads(result.output)['version_recording']['recorded'] is False
