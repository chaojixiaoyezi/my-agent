# LLM: 以真实临时ConversationStore与资产库验证搜索、来源边界、脱敏、CAS和失败回滚；不访问网络。
# 模块用途: 覆盖回合提取与确认入库合同。
import ast
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets import turn_extractor as extract
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 仅临时Owner中生成两线程及原生工具信封；最后用户回合用于检查边界。
# 函数用途: 提供真实消息、工具执行与时间检索样本。
@pytest.fixture
def history(tmp_path):
    home = tmp_path / 'owner'
    store = ConversationStore(home / 'conversations')
    a = store.threads.get_or_create({'channel': 'tui', 'channel_conversation_id': 'a', 'title': '日报'})
    b = store.threads.get_or_create({'channel': 'tui', 'channel_conversation_id': 'b', 'title': '其它'})
    store.threads.update_atomic(a.thread_id, lambda row: replace(row, title='日报', metadata={'workspace_root': str(tmp_path / 'project')}))
    first = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': '整理中文订单', 'now': 1792886400})
    native = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'call1', 'name': 'run_command', 'input': {'command': 'printf hello'}},
            {'type': 'tool_use', 'id': 'call2', 'name': 'write_file', 'input': {'path': 'output/report.txt', 'content': '订单'}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call1', 'content': 'hello'},
            {'type': 'tool_result', 'tool_use_id': 'call2', 'content': 'saved'}]},
    ]
    store.messages.append({'thread_id': a.thread_id, 'role': 'assistant', 'content': '订单整理完毕',
                           'metadata': {'canonical_native_messages': canonical_native_messages_envelope(native)}, 'now': 1792886401})
    second = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': '另一轮', 'now': 1792972800})
    store.messages.append({'thread_id': a.thread_id, 'role': 'assistant', 'content': '不属于第一轮', 'now': 1792972801})
    third = store.messages.append({'thread_id': b.thread_id, 'role': 'user', 'content': '跨会话提取', 'now': 1792800000})
    return home, store, a, b, first, second, third


# LLM: 分页按全部匹配计数，零基turn_index在单线程内稳定；中文查询不是空条件。
# 函数用途: 验证多线程检索、工具聚合与日期目录筛选。
def test_search_filters_and_paginates(history, tmp_path):
    _, store, a, _, first, _, _ = history
    result = extract.search_user_turns(store, query='RUN_COMMAND', has_tool_calls=True,
                                     workspace_dir=str(tmp_path / 'project'), date_from='2026-10-25', date_to='2026-10-25')
    assert isinstance(result, dict)
    assert result['total'] == 1
    item = result['items'][0]
    assert (item['thread_id'], item['message_id'], item['turn_index']) == (a.thread_id, first.message_id, 0)
    assert item['tool_call_count'] == 2
    assert item['tool_names'] == ['run_command', 'write_file']
    assert extract.search_user_turns(store, query='中文')['total'] == 1
    page = extract.search_user_turns(store, limit=1)
    assert page['total'] == 3 and page['next_cursor'] == 1
    assert extract.search_user_turns(store, limit=1, cursor=1)['items'] != page['items']
    assert extract.search_user_turns(store, thread_ids=[a.thread_id], has_tool_calls=False)['total'] == 1


# LLM: 选择顺序保留，工具只来自本回合，不解析助手文字伪造执行事实。
# 函数用途: 验证跨线程回合的完整打包与工具结果配对。
def test_collect_selected_turns_and_commands(history):
    home, store, a, b, first, _, third = history
    refs = [{'thread_id': b.thread_id, 'message_id': third.message_id}, {'thread_id': a.thread_id, 'turn_index': 0}]
    bundles = extract.collect_turn_execution_bundles(store, refs, owner_home=home)
    assert isinstance(bundles, list)
    assert [row['message_id'] for row in bundles] == [third.message_id, first.message_id]
    assert bundles[1]['commands_executed'] == ['printf hello']
    assert bundles[1]['files_written'] == ['output/report.txt']
    assert bundles[1]['assistant_messages'] == ['订单整理完毕']
    assert bundles[1]['tool_invocations'][0]['result_preview'] == 'hello'
    assert bundles[1]['tool_invocations'][0]['status'] == 'succeeded'


# LLM: 缺引用与伪造非用户消息都必须有结构化拒绝；不跨Owner找相似ID。
# 函数用途: 拒绝未知线程、消息和回合索引。
@pytest.mark.parametrize('ref', [{'thread_id': 'unknown', 'message_id': 'x'}, {'message_id': 'x'}, {'thread_id': 'known', 'turn_index': -1}])
def test_unknown_turn_reference_is_rejected(history, ref):
    _, store, a, *_ = history
    ref = {**ref, 'thread_id': a.thread_id if ref.get('thread_id') == 'known' else ref.get('thread_id', '')}
    with pytest.raises(ValueError, match='TURN_REF_NOT_FOUND'):
        extract.collect_turn_execution_bundles(store, [ref])


# LLM: 草稿只写Owner提案目录；默认确定性脚本不执行历史命令，正式发布另行确认。
# 函数用途: 检查草稿载荷、工作流合同和只预览不入库。
def test_proposal_is_durable_valid_and_not_published(history):
    home, store, a, _, first, *_ = history
    result = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert isinstance(result, dict)
    assert result['status'] == 'DRAFT' and result['revision'] == 1
    assert len(result['candidate_scripts']) == len(result['candidate_skills']) == len(result['candidate_workflows']) == 1
    ast.parse(result['candidate_scripts'][0]['entry_source'])
    WorkflowDefinition.from_any(result['candidate_workflows'][0])
    skill = result['candidate_skills'][0]['body']
    assert skill.startswith('---\n') and all(heading in skill for heading in ('## 适用场景', '## 输入参数', '## 标准执行步骤', '## 验收条件'))
    assert extract.get_extraction_proposal(home, result['proposal_id']) == result
    assert ScriptStore(home).list_scripts() == []


# LLM: 分析回调也只能接收脱敏数据，返回结果再次脱敏，不允许把秘密写回草稿。
# 函数用途: 检查文本、嵌套JSON和回调边界上的秘密清理。
def test_analyzer_receives_redacted_bundle(history):
    home, store, a, *_ = history
    msg = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': 'password=hunter-test-42 Authorization: Bearer tok-test-42 Cookie: sid=private'})
    seen = []

    # LLM: 纯测试回调捕获其输入，不执行外部分析。
    # 函数用途: 验证分析器收到的数据已经脱敏。
    def analyzer(bundles, target_types):
        seen.append((bundles, target_types))
        return {'candidate_skills': [], 'candidate_scripts': [], 'candidate_workflows': [], 'bundle_summary': 'password=hunter-test-42'}

    result = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': msg.message_id}], analyzer=analyzer)
    assert isinstance(result, dict)
    raw = json.dumps([seen, result])
    assert all(secret not in raw for secret in ('hunter-test-42', 'tok-test-42', 'sid=private'))


# LLM: 保存前校验revision，幂等重放只接受相同选项，workspace落点不得污染owner。
# 函数用途: 真实入库并验证脚本工作流可用、依赖及重复提交行为。
@pytest.mark.parametrize('scope', ['owner', 'workspace'])
@pytest.mark.timeout(5)
def test_commit_batch_and_idempotent_replay(history, tmp_path, scope):
    home, store, a, _, first, *_ = history
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    draft = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}], scope=scope, workspace_root=project)
    assert isinstance(draft, dict)
    with pytest.raises(ValueError, match='REVISION'):
        extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], expected_revision=2)
    receipt = extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], expected_revision=1)
    assert receipt['ok'] is True and len(receipt['committed_assets']) == 3
    assert extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], expected_revision=1) == receipt
    root = project / '.agent' if scope == 'workspace' else home
    scripts = ScriptStore(root).list_scripts()
    workflows = WorkflowStore(root).list_workflows()
    assert len(scripts) == len(workflows) == 1
    assert workflows[0].descriptor.refs.scripts == (scripts[0]['id'],)
    assert extract.get_extraction_proposal(home, draft['proposal_id'])['revision'] == 2


# LLM: 故障注入在真实写叶，校验先写资产及提案状态都回到旧态，不靠mock返回成功。
# 函数用途: 验证批量提交中途异常时无半套资产。
def test_commit_rolls_back_after_partial_publication(history, monkeypatch):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert isinstance(draft, dict)
    original = extract.write_text_atomic_beneath

    # LLM: 仅拦截工作流正式叶，确保已有脚本/Skill先发生真实写入。
    # 函数用途: 注入批量事务中途写入故障。
    def fail_workflow(root, parts, text, **kwargs):
        if parts[0] == 'workflows':
            raise OSError('injected publication failure')
        return original(root, parts, text, **kwargs)

    monkeypatch.setattr(extract, 'write_text_atomic_beneath', fail_workflow)
    with pytest.raises(OSError, match='injected'):
        extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'])
    assert ScriptStore(home).list_scripts() == []
    assert WorkflowStore(home).list_workflows() == []
    assert not list((home / 'skills').rglob('SKILL.md'))
    assert extract.get_extraction_proposal(home, draft['proposal_id'])['status'] == 'DRAFT'


# LLM: 文件名和资产ID不能成为路径；不允许跟随提案目录符号链接。
# 函数用途: 检查提案读取的路径穿越拒绝。
def test_proposal_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        extract.get_extraction_proposal(tmp_path, '../outside')


# LLM: 用户编辑会改变依赖身份，必须同步工作流；提交顺序不受勾选顺序影响。
# 函数用途: 验证改名、代码编辑和拓扑发布顺序。
def test_edits_rebind_dependencies_and_publish_in_order(history, monkeypatch):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    workflow, script, skill = draft['candidate_workflows'][0], draft['candidate_scripts'][0], draft['candidate_skills'][0]
    written, original = [], extract.write_text_atomic_beneath

    # LLM: 记录真实写叶顺序，不替代发布实现。
    # 函数用途: 观察工作流是否晚于其依赖发表。
    def capture(root, parts, text, **kwargs):
        written.append(parts[0])
        return original(root, parts, text, **kwargs)

    monkeypatch.setattr(extract, 'write_text_atomic_beneath', capture)
    extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[
        {'kind': 'workflow', 'id': workflow['descriptor']['id']},
        {'kind': 'script', 'id': script['script_id'], 'new_id': 'renamed_script', 'title': '改名脚本', 'body': 'def main(inputs: dict) -> dict:\n    return {"ok": True}\n'},
        {'kind': 'skill', 'id': skill['name'], 'new_id': 'renamed_skill'}])
    saved = WorkflowStore(home).list_workflows()[0]
    assert saved.descriptor.refs.scripts == ('renamed_script',)
    assert saved.descriptor.refs.skills == ('renamed_skill',)
    assert written.index('workflows') > written.index('scripts')
    assert written.index('workflows') > written.index('skills')


# LLM: 编辑后重名不能覆盖字典中的前一个候选，失败前不得发表任何脚本。
# 函数用途: 拒绝两个所选脚本被改成同一ID。
def test_edited_duplicate_ids_are_rejected(history):
    home, store, a, _, first, second, _ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, target_types=['script'],
        turn_refs=[{'thread_id': a.thread_id, 'message_id': m.message_id} for m in (first, second)])
    with pytest.raises(ValueError, match='DUPLICATE'):
        extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[
            {'kind': 'script', 'id': script['script_id'], 'new_id': 'same_id'} for script in draft['candidate_scripts']])
    assert ScriptStore(home).list_scripts() == []


# LLM: 新编辑正文也必须脱敏；合法AST不能替代敏感信息检查。
# 函数用途: 防止确认时把秘密重新写回正式脚本。
def test_commit_edits_do_not_reintroduce_secrets(history):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, target_types=['script'], turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[{
        'kind': 'script', 'id': draft['candidate_scripts'][0]['script_id'],
        'body': 'def main(inputs: dict) -> dict:\n    password = "secret-edit-42"\n    return {"ok": True}\n'}])
    assert 'secret-edit-42' not in ScriptStore(home).list_scripts()[0].get('code', '')
    assert all('secret-edit-42' not in path.read_text() for path in (home / 'scripts').rglob('*.py'))


# LLM: 明确来源边界不允许用其它Owner的会话生成本Owner资产。
# 函数用途: 拒绝跨Owner提炼。
def test_collect_rejects_different_owner(history, tmp_path):
    _, store, a, _, first, *_ = history
    with pytest.raises(ValueError, match='OWNER_SCOPE'):
        extract.collect_turn_execution_bundles(store, [{'thread_id': a.thread_id, 'message_id': first.message_id}], owner_home=tmp_path / 'other_owner')


# LLM: 正式ScriptRunner调用脚本文件而非导入main，必须能读stdin并向stdout输出单行JSON。
# 函数用途: 验证提炼脚本具有真实可执行入口而非只有函数定义。
def test_extracted_script_runs_as_entrypoint(history, tmp_path):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, target_types=['script'], turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    script = tmp_path / 'extracted.py'
    script.write_text(draft['candidate_scripts'][0]['entry_source'])
    result = subprocess.run([sys.executable, str(script)], input='{"dry_run":true}', capture_output=True, text=True, check=True)
    assert result.stdout.strip(), '正式执行入口没有输出JSON'
    assert json.loads(result.stdout)['executed'] is False


# LLM: 部分勾选不能自动发布未选择依赖，拒绝发生在任何资产叶写入之前。
# 函数用途: 验证只选择Skill可行、只选悬空工作流拒绝。
def test_selective_commit_and_dangling_dependency(history):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    with pytest.raises(ValueError, match='DEPENDENCY_MISSING'):
        extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[{
            'kind': 'workflow', 'id': draft['candidate_workflows'][0]['descriptor']['id']}])
    assert ScriptStore(home).list_scripts() == [] and WorkflowStore(home).list_workflows() == []
    result = extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[{
        'kind': 'skill', 'id': draft['candidate_skills'][0]['name']}])
    assert [item['kind'] for item in result['committed_assets']] == ['skill']


# LLM: proposal叶替换后抛错也必须恢复原DRAFT，不能资产已删但状态残留COMMITTED。
# 函数用途: 验证最终提交回执写入失败的整批回滚。
def test_proposal_publication_failure_restores_draft(history, monkeypatch):
    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    original, raised = extract._write_record, []

    # LLM: 先真实替换提案再抛一次异常，确保覆盖提交后报错窗口。
    # 函数用途: 注入最终回执发表异常。
    def fail_after_replace(owner, collection, value, key):
        original(owner, collection, value, key)
        if value['status'] == 'COMMITTED' and not raised:
            raised.append(True)
            raise OSError('after proposal replace')

    monkeypatch.setattr(extract, '_write_record', fail_after_replace)
    with pytest.raises(OSError, match='after proposal'):
        extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'])
    assert extract.get_extraction_proposal(home, draft['proposal_id']) == draft
    assert ScriptStore(home).list_scripts() == [] and WorkflowStore(home).list_workflows() == []


# LLM: 符号链接不能把提案目录变成Owner外的写入口。
# 函数用途: 验证草稿落盘拒绝链接目录且外部没有新文件。
def test_proposal_rejects_symlinked_directory(history, tmp_path):
    home, store, a, _, first, *_ = history
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'data').mkdir(exist_ok=True)
    (home / 'data' / 'asset_extractions').symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        extract.propose_assets_from_turns(store, owner_home=home, turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert list(outside.iterdir()) == []


# LLM: 新字段由真实ThreadStore持久化，旧metadata冲突时不能抢占搜索范围。
# 函数用途: 验证工作目录筛选和摘要都优先读取线程workspace_root。
def test_search_workspace_field_precedes_legacy_metadata(history, tmp_path):
    _, store, a, *_ = history
    project, old = str(tmp_path / 'bound'), str(tmp_path / 'old')
    store.threads.update_atomic(a.thread_id, lambda row: replace(row, workspace_root=project, metadata={'workspace_root': old}))
    result = extract.search_user_turns(store, thread_ids=[a.thread_id], workspace_dir=project)
    assert result['total'] == 2
    assert all(item['workspace_root'] == project for item in result['items'])
    assert extract.search_user_turns(store, thread_ids=[a.thread_id], workspace_dir=old)['total'] == 0


# LLM: 旧线程可能没有新字段或metadata；只模拟值对象，消息仍从真实会话库读取。
# 函数用途: 验证metadata、cwd和空目录的回退兼容性。
@pytest.mark.parametrize('metadata,cwd,expected', [({'workspace_root': '/legacy'}, '/cwd', '/legacy'), (None, '/cwd', '/cwd'), ({}, '', '')])
def test_search_legacy_workspace_fallbacks(history, monkeypatch, metadata, cwd, expected):
    _, store, a, *_ = history
    thread = SimpleNamespace(thread_id=a.thread_id, title=a.title, metadata=metadata, cwd=cwd)
    monkeypatch.setattr(store.threads, 'list_report', lambda **kwargs: ([thread], []))
    result = extract.search_user_turns(store, **({'workspace_dir': expected} if expected else {}))
    assert result['total'] == 2
    assert all(item['workspace_root'] == expected for item in result['items'])


# LLM: PREVIEW不获取记录锁、不创建提案；同时比较完整临时Owner的目录、权限、mtime和正文。
# 函数用途: 验证纯预览没有自身的磁盘写副作用。
def test_preview_only_never_locks_or_writes(history, monkeypatch):
    home, store, a, _, first, *_ = history

    # LLM: 不比较读取可能改变的atime，保留显式写入会改变的内容与mtime。
    # 函数用途: 对临时Owner取只读文件快照。
    def snapshot():
        return {str(p.relative_to(home)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in [home, *home.rglob('*')]}

    # LLM: 预览误入锁或写入口立即失败，不能用空写入伪装纯读。
    # 函数用途: 拦截预览不该发生的持久化步骤。
    def forbid_write(*args, **kwargs):
        pytest.fail('PREVIEW_MUST_NOT_LOCK_OR_WRITE')

    before = snapshot()
    monkeypatch.setattr(extract, '_record_lock', forbid_write)
    monkeypatch.setattr(extract, '_write_record', forbid_write)
    result = extract.propose_assets_from_turns(store, owner_home=home, preview_only=True,
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert result['status'] in {'PREVIEW', 'DRAFT'} and result['redacted_secrets'] == []
    assert result['candidate_scripts'] and result['candidate_workflows'] and result['candidate_skills']
    assert snapshot() == before


# LLM: 默认值与显式False都应保存DRAFT，新增预览选项不能改变原持久化合同。
# 函数用途: 验证无脱敏命中的普通草稿返回空清单。
@pytest.mark.parametrize('options', [{}, {'preview_only': False}])
def test_draft_reports_empty_redactions_and_remains_durable(history, options):
    home, store, a, _, first, *_ = history
    result = extract.propose_assets_from_turns(store, owner_home=home, **options,
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert result['redacted_secrets'] == []
    assert result['status'] == 'DRAFT'
    assert extract.get_extraction_proposal(home, result['proposal_id']) == result


# LLM: 只报告实际替换后的占位符，不返回秘密原值；覆盖历史、标题与分析器的嵌套输出。
# 函数用途: 验证预览和持久化草稿都携带去重脱敏清单。
@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('source', ['history', 'title', 'analyzer'])
def test_proposal_reports_redacted_placeholders(history, preview, source):
    home, store, a, _, first, *_ = history
    secret = 'unit-wave13-redaction-42'
    options = {'preview_only': preview}
    if source == 'history':
        first = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': 'password=' + secret})
    elif source == 'title':
        options['title_hint'] = 'password=' + secret
    else:
        options['analyzer'] = lambda bundles, kinds: {'bundle_summary': {'nested': [{'password': secret}]}}
    result = extract.propose_assets_from_turns(store, owner_home=home, **options,
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert result['redacted_secrets'] == ['<redacted>']
    assert secret not in json.dumps(result)
    if not preview:
        assert extract.get_extraction_proposal(home, result['proposal_id']) == result


# LLM: 真值字符串或整数不是bool，误用不能退化成会落盘的普通提案。
# 函数用途: 拒绝无效preview_only类型且不创建提案。
@pytest.mark.parametrize('value', [None, 1, 'true'])
def test_preview_only_requires_boolean(history, value):
    home, store, a, _, first, *_ = history
    with pytest.raises(ValueError, match='EXTRACTION_OPTIONS_INVALID'):
        extract.propose_assets_from_turns(store, owner_home=home, preview_only=value,
            turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert not (home / 'data' / 'asset_extractions').exists()


# LLM: 既有脚本依赖在已持根锁与集合锁内读取，不能调用再次取锁的公开方法。
# 函数用途: 验证部分选择工作流时能安全复用已入库脚本。
@pytest.mark.timeout(5)
def test_commit_workflow_reuses_existing_script_without_nested_root_lock(history):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor

    home, store, a, _, first, *_ = history
    draft = extract.propose_assets_from_turns(store, owner_home=home,
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    script = draft['candidate_scripts'][0]
    ScriptStore(home).save_script(AssetDescriptor.from_dict(script['descriptor']), script['entry_source'])
    receipt = extract.commit_extracted_assets(home, proposal_id=draft['proposal_id'], selected_assets=[
        {'kind': 'workflow', 'id': draft['candidate_workflows'][0]['descriptor']['id']},
        {'kind': 'skill', 'id': draft['candidate_skills'][0]['name']}])
    assert receipt['ok'] is True
    assert WorkflowStore(home).list_workflows()[0].descriptor.refs.scripts == (script['script_id'],)


# LLM: 原有占位符和tuple转list不代表新脱敏；敏感键下的数字或整个对象同样要报告。
# 函数用途: 验证脱敏清单只记录真实替换，不收集原秘密或误计容器归一。
@pytest.mark.parametrize('value,expected', [
    (('<redacted>',), []), ({'password': '<redacted>'}, []),
    ({'password': 42}, ['<redacted>']), ({'password': {'nested': 'unit-private'}}, ['<redacted>']),
])
def test_redaction_report_distinguishes_existing_placeholders(value, expected):
    markers = []
    cleaned = extract._redact(value, redacted_secrets=markers)
    assert markers == expected
    assert 'unit-private' not in json.dumps(cleaned)


# LLM: 命令只作为结构化历史证据保存，测试不执行它；引号内空格也属于整个秘密参数。
# 函数用途: 验证空格分隔的长敏感标志及有上下文的短标志脱敏。
@pytest.mark.parametrize('command,secret', [
    ('curl --password fixture-pass-42 https://example.test/', 'fixture-pass-42'),
    ('tool --passwd "fixture pass 42" --next ok', 'fixture pass 42'),
    ("tool --token 'fixture token 42'", 'fixture token 42'),
    ('tool --secret fixture-secret-42', 'fixture-secret-42'),
    ('tool --api-key fixture-key-42', 'fixture-key-42'),
    ('tool --apikey fixture-key-42', 'fixture-key-42'),
    ('tool --access-key fixture-key-42', 'fixture-key-42'),
    ('tool --auth-token fixture-token-42', 'fixture-token-42'),
    ('mysql -p fixture-pass-42 -e "SELECT 1"', 'fixture-pass-42'),
    ('/usr/bin/psql -p fixture-pass-42', 'fixture-pass-42'),
    ('sshpass -p "fixture pass 42" ssh example.test', 'fixture pass 42'),
    ('login -p fixture-pass-42', 'fixture-pass-42'),
])
def test_space_separated_cli_secrets_never_enter_proposals(history, command, secret):
    home, store, a, *_ = history
    message = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': '处理一条命令证据'})
    store.messages.append({'thread_id': a.thread_id, 'role': 'assistant', 'content': '已记录',
        'metadata': {'tool_calls': [{'id': 'cli-call', 'name': 'run_command', 'arguments': {'command': command}}]}})
    proposal = extract.propose_assets_from_turns(store, owner_home=home,
        turn_refs=[{'thread_id': a.thread_id, 'message_id': message.message_id}])
    assert secret not in json.dumps(proposal)
    assert proposal['redacted_secrets'] == ['<redacted>']
    assert all(secret not in path.read_text() for path in (home / 'data/asset_extractions').glob('*.json'))
    tree = ast.parse(proposal['candidate_scripts'][0]['entry_source'])
    evidence = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'COMMAND_EVIDENCE' for t in node.targets))
    assert '<redacted>' in evidence[0] and secret not in evidence[0]


# LLM: 无密码语义的-p是端口等普通选项；不能把不相关短参数当秘密。
# 函数用途: 保留普通短标志及非敏感长参数。
@pytest.mark.parametrize('command', ['ssh -p 2222 example.test', 'tool -p report.txt', 'tool --token-count 42'])
def test_cli_redaction_preserves_unrelated_flags(command):
    markers = []
    assert extract._redact(command, redacted_secrets=markers) == command
    assert markers == []


# LLM: workflow单独请求必须连带脚本候选并能一起发布，参数缺省仍只干跑不执行历史命令。
# 函数用途: 验证现有target_types和asset_types别名都生成完整步骤依赖。
@pytest.mark.parametrize('selector', ['target_types', 'asset_types'])
def test_workflow_only_includes_executable_companion_scripts(history, selector):
    home, store, a, _, first, *_ = history
    proposal = extract.propose_assets_from_turns(store, owner_home=home, **{selector: ['workflow']},
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    assert proposal['candidate_scripts'] and proposal['candidate_workflows'] and not proposal['candidate_skills']
    definition = WorkflowDefinition.from_any(proposal['candidate_workflows'][0])
    assert definition.steps and definition.descriptor.refs.scripts
    assert definition.steps[0].script_id == proposal['candidate_scripts'][0]['script_id']
    result = extract.commit_extracted_assets(home, proposal_id=proposal['proposal_id'])
    assert {item['kind'] for item in result['committed_assets']} == {'script', 'workflow'}


# LLM: 只有闲聊或仅含非命令工具调用（无commands_executed）的回合不能合成空命令工作流；拒绝前不写草稿。
# 函数用途: 验证workflow提炼源没有可执行命令证据时明确报错（覆盖target_types与asset_types）。
@pytest.mark.parametrize('selector', ['target_types', 'asset_types'])
def test_workflow_only_rejects_empty_execution_evidence(history, selector):
    home, store, a, _, _, second, _ = history
    with pytest.raises(ValueError, match='^TURN_EXTRACTION_EMPTY_CANDIDATES$'):
        extract.propose_assets_from_turns(store, owner_home=home, **{selector: ['workflow']},
            turn_refs=[{'thread_id': a.thread_id, 'message_id': second.message_id}])
    non_cmd_user = store.messages.append({'thread_id': a.thread_id, 'role': 'user', 'content': '只读文件不跑命令', 'now': 1793000000})
    non_cmd_native = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r1', 'name': 'read_file', 'input': {'path': 'a.txt'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'r1', 'content': 'ok'}]},
    ]
    store.messages.append({'thread_id': a.thread_id, 'role': 'assistant', 'content': '已读完',
        'metadata': {'canonical_native_messages': canonical_native_messages_envelope(non_cmd_native)}, 'now': 1793000001})
    with pytest.raises(ValueError, match='^TURN_EXTRACTION_EMPTY_CANDIDATES$'):
        extract.propose_assets_from_turns(store, owner_home=home, **{selector: ['workflow']},
            turn_refs=[{'thread_id': a.thread_id, 'message_id': non_cmd_user.message_id}])
    assert not (home / 'data/asset_extractions').exists()


# LLM: Workflow显式None不能覆盖argv空列表或安全干跑默认值；不执行历史命令。
# 函数用途: 通过真实stdin/stdout入口验证Workflow缺省输入的安全回退。
@pytest.mark.parametrize('inputs', [{'argv': None, 'dry_run': True}, {'argv': None, 'dry_run': None, 'timeout': None}])
def test_extracted_entrypoint_accepts_null_argv_in_dry_run(history, tmp_path, inputs):
    home, store, a, _, first, *_ = history
    proposal = extract.propose_assets_from_turns(store, owner_home=home, target_types=['script'],
        turn_refs=[{'thread_id': a.thread_id, 'message_id': first.message_id}])
    path = tmp_path / 'null_argv.py'
    path.write_text(proposal['candidate_scripts'][0]['entry_source'])
    result = subprocess.run([sys.executable, str(path)], input=json.dumps(inputs),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['argv'] == [] and output['executed'] is False
