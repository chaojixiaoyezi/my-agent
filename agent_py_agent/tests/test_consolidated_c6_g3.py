"""Wave 3 分片 c6 第 3 组整合测试（源清单、资产演化晋升、lesson 引用、变更账本与监控调度）。"""
import hashlib
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timezone
from threading import Event, current_thread, local
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap as bootstrap
from agent_py_agent.agent.assets import source_manifest as manifests
from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    AssetRefs,
    CredentialBindingRef,
    DataSourceSpec,
    IOContract,
)
from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    list_monitor_descriptors,
    save_monitor_binding,
)
from agent_py_agent.agent.assets.monitor_composition import save_monitor_composition
from agent_py_agent.agent.assets.mutation_ledger import AssetMutationLedger, freeze_asset_mutations
from agent_py_agent.agent.assets.pitfalls import read_known_pitfalls
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    latest_version,
    list_versions,
    record_version,
    save_draft,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.capability.asset_evolution import route_evolution_lesson
from agent_py_agent.agent.capability.asset_evolution_replay import merge_evolution_candidate
from agent_py_agent.agent.capability.asset_promotion import _promote_script_asset
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryCandidate,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.lessons import LessonRepository
from agent_py_agent.agent.scheduler import schedule
from agent_py_agent.agent.scheduler.monitor_pool import MonitorPool
from agent_py_agent.agent.scheduler.pinned_targets import resolve_monitor_target
from agent_py_agent.agent.tooling.datasources_tool import DatasourcesTool
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.workflows.analysis_isolation import (
    WorkflowIsolationUnavailable,
    resolve_analysis_isolation,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_acceptance_workflow_native_ports import flow, local_run
from agent_py_agent.tests.test_acceptance_workflow_native_ports import host as native_workflow_host
from agent_py_agent.tests.test_v15_w7_evolution_routes import verified_target
from agent_py_agent.tests.test_v15_w7_replay_gate import InProcessReplayRunner

# === test_v15_w6_source_manifest.py: W6清单：仅临时Owner，覆盖确认、CAS、原子发布和告警窗口。 ===

# LLM: 全部值为合成的不透明引用，不访问真实凭据。
# 函数用途: 构造一个清单来源。
def source(source_id='events-a', *, core=False):
    return {'source_id': source_id, 'core': core, 'credential_ref': 'cref-a', 'endpoint_ref': 'endpoint-a'}


# LLM: 读口必须不创建任何目录，包括锁文件。
# 函数用途: 验证缺失清单的只读默认值。
def test_missing_manifest_read_has_no_filesystem_effect(tmp_path):
    home = tmp_path / 'owner'
    result = manifests.load_manifest(home, 'events')
    assert isinstance(result, dict)
    assert result['schema_version'] == 'datasource_manifest.v1'
    assert result['revision'] == 0 and result['sources'] == []
    assert not home.exists()


# LLM: 旧观察不能覆盖新状态；prune复用同一版本门。
# 函数用途: 验证增量登记、CAS冲突和明确移除。
def test_save_merge_and_prune_are_compare_and_swap(tmp_path):
    first = manifests.save_manifest(tmp_path, 'events', [source()], expected_revision=0)
    assert isinstance(first, dict)
    assert first['revision'] == 1
    second = manifests.save_manifest(tmp_path, 'events', [source('events-b')], expected_revision=1)
    assert second['revision'] == 2
    assert {s['source_id'] for s in second['sources']} == {'events-a', 'events-b'}
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.save_manifest(tmp_path, 'events', [source('events-c')], expected_revision=1)
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.save_manifest(tmp_path, 'events', [source('events-c')])
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.prune_sources(tmp_path, 'events', ['events-a'], expected_revision=1)
    pruned = manifests.prune_sources(tmp_path, 'events', ['events-a'], expected_revision=2)
    assert pruned['revision'] == 3 and pruned['sources'] == [source('events-b')]


# LLM: 写叶失败不得发表新revision，复用真实原子写口的替身。
# 函数用途: 验证模拟IO失败保持旧清单。
def test_atomic_write_failure_keeps_prior_revision(tmp_path, monkeypatch):
    old = manifests.save_manifest(tmp_path, 'events', [source()])
    assert isinstance(old, dict)
    before = (tmp_path / 'data/source_manifests/events.json').read_bytes()

    # LLM: 只替换原子写叶，不篡改读取或CAS结果。
    # 函数用途: 模拟持久化失败。
    def unavailable(root, parts, text, *, file_mode=0o600):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(manifests, 'write_text_atomic_beneath', unavailable)
    with pytest.raises(OSError):
        manifests.save_manifest(tmp_path, 'events', [source('events-b')], expected_revision=1)
    assert manifests.load_manifest(tmp_path, 'events') == old
    assert (tmp_path / 'data/source_manifests/events.json').read_bytes() == before


# LLM: 必须在工具入口显式确认，不能用truthy字符串冒充。
# 函数用途: 验证未确认的登记和移除零写入。
@pytest.mark.parametrize('action', ['manifest_register', 'manifest_prune'])
@pytest.mark.parametrize('confirmed', [None, False, 'true', 1])
def test_manifest_mutations_require_literal_confirmation(tmp_path, action, confirmed):
    tool = DatasourcesTool(tmp_path / 'owner')
    params = {'action': action, 'source_system': 'events', 'sources': [source()],
              'source_ids': ['events-a'], 'expected_revision': 0}
    if confirmed is not None:
        params['confirmed'] = confirmed
    result = tool.execute(params)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert not (tmp_path / 'owner/data/source_manifests').exists()


# LLM: 从公开工具执行真实读写；不把内部helper成功当作工具接通。
# 函数用途: 验证清单动作、CAS错误和只读投影。
def test_manifest_tool_roundtrip_and_conflict(tmp_path):
    tool = DatasourcesTool(tmp_path / 'owner')
    got = tool.execute({'action': 'manifest_get', 'source_system': 'events'})
    assert got.ok
    assert json.loads(got.output)['manifest']['revision'] == 0
    assert not tool.owner_home.exists()
    saved = tool.execute({'action': 'manifest_register', 'source_system': 'events',
        'sources': [source()], 'expected_revision': None, 'confirmed': True})
    assert saved.ok and json.loads(saved.output)['manifest']['revision'] == 1
    conflict = tool.execute({'action': 'manifest_prune', 'source_system': 'events',
        'source_ids': ['events-a'], 'expected_revision': 0, 'confirmed': True})
    assert not conflict.ok and conflict.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert json.loads(conflict.output)['error_code'] == 'ASSET_REVISION_CONFLICT'
    pruned = tool.execute({'action': 'manifest_prune', 'source_system': 'events',
        'source_ids': ['events-a'], 'expected_revision': 1, 'confirmed': True})
    assert pruned.ok and json.loads(pruned.output)['manifest']['sources'] == []


# LLM: 同一观察版本只能有一个提交者；线程共用既有锁原语。
# 函数用途: 验证并发CAS不会丢失已发表清单。
def test_concurrent_registration_has_one_cas_winner(tmp_path):
    first = manifests.save_manifest(tmp_path, 'events', [source()])
    assert isinstance(first, dict)

    # LLM: 只吞明确的版本冲突，不掩盖其他IO失败。
    # 函数用途: 竞争同一清单revision。
    def contender(index):
        try:
            return manifests.save_manifest(tmp_path, 'events', [source(f'events-{index}')], expected_revision=1)
        except ValueError as error:
            assert 'ASSET_REVISION_CONFLICT' in str(error)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(contender, range(2)))
    assert sum(value is not None for value in values) == 1
    assert manifests.load_manifest(tmp_path, 'events')['revision'] == 2


# LLM: 清单和健康账不能经链接写入Owner外目录。
# 函数用途: 验证路径身份及受控父链。
@pytest.mark.parametrize('system', ['../outside', '/outside', 'events.health'])
def test_invalid_manifest_system_is_rejected(tmp_path, system):
    with pytest.raises(ValueError):
        manifests.save_manifest(tmp_path, system, [source()])
    assert not (tmp_path / 'data').exists()


# LLM: 注入固定UTC时间，不等待真实一天；不同Owner和引用独立。
# 函数用途: 验证24小时窗口边界、持久化及Owner隔离。
def test_alert_dedupe_window_and_identity(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now) is True
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now + 86399) is False
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now + 86400) is True
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-b', now=now) is True
    assert manifests.should_emit_alert(tmp_path / 'other', 'events', 'events-a', 'cref-a', now=now) is True
    state = json.loads((tmp_path / 'data/source_manifests/events.health.json').read_text())
    assert state['schema_version'] == 'datasource_health.v1'
    assert set(state['alerts']) == {'events-a|cref-a', 'events-a|cref-b'}


# === test_v15_w7_g4_promotion.py: G4无损描述符合并与失败不覆盖。 ===

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


# === test_v15_w7_lessons_asset_refs.py: lesson引用往返、旧记录与确定性pitfall sidecar。 ===

# LLM: 从原CandidateService观察及读回，不手写候选JSON假装往返。
# 函数用途: 验证引用限制与候选持久化。
def test_candidate_asset_refs_roundtrip(tmp_path):
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    observation = CandidateObservation('lesson', '状态核对要固定输入与输出。', 'state-check', MemoryScope('personal', 'personal'),
        'tool_verified', evidence_refs=({'call_id': 'call'},), source_run_ids=('run-a',),
        promotion_target='lesson', asset_refs=('script:owner:sample', 'workflow:owner:flow', 'data_source:system', '/bad/path'))
    candidate = service.observe(observation)
    assert candidate.asset_refs == ['script:owner:sample', 'workflow:owner:flow', 'data_source:system']
    loaded = MemoryCandidate.from_record(candidate.to_record())
    assert loaded.asset_refs == candidate.asset_refs
    legacy = service.observe(CandidateObservation('lesson', '旧记录没有资产引用。', 'legacy-check', MemoryScope('personal', 'personal'),
        'tool_verified', evidence_refs=({'call_id': 'legacy'},), promotion_target='lesson'))
    raw = legacy.to_record()
    raw.pop('asset_refs')
    assert MemoryCandidate.from_record(raw).asset_refs == []


# LLM: 正式lesson仍需approved和原证据门，不用资产引用降低门槛。
# 函数用途: 验证正式lesson附带refs及旧文件读取。
def test_lesson_asset_refs_and_old_records_load(tmp_path):
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    candidate = service.observe(CandidateObservation('lesson', '固定脚本变更后核对原输入。', 'check', MemoryScope('personal', 'personal'),
        'user_explicit', source_message_refs=({'message_id': 'confirmed'},), evidence_refs=({'call_id': 'checked'},),
        promotion_target='lesson', asset_refs=('script:owner:sample',)))
    approved = replace(candidate, status='approved')
    repo = LessonRepository(tmp_path / 'memory' / 'lessons', tmp_path / 'memory' / 'routing' / 'INDEX.md')
    lesson = repo.promote(approved)
    assert repo.get(lesson.lesson_id).asset_refs == ('script:owner:sample',)
    path = tmp_path / lesson.path
    first, rest = path.read_text().split('\n', 1)
    meta = json.loads(first.removeprefix('<!-- my-agent-lesson-meta:').removesuffix(' -->'))
    meta.pop('asset_refs')
    path.write_text('<!-- my-agent-lesson-meta:' + json.dumps(meta) + ' -->\n' + rest)
    assert repo.get(lesson.lesson_id).asset_refs == ()


# LLM: 一次失败修复成功链可生成候选，但不能改正式描述符或伪造第二次独立观察。
# 函数用途: 验证单次例外、sidecar位置与重复后台幂等。
def test_route3_one_occurrence_sidecar_only(tmp_path):
    store = ScriptStore(tmp_path)
    before = store.save_script(AssetDescriptor('sample', 'script', 'sample', metadata={'keep': True}),
                               'def main(inputs):\n return {"result": "04:27"}\n')
    target = verified_target(before['code'], bug_fix=True)
    target.update(asset_id='sample', bug_fix=True)
    request = {'run_id': 'run-one', 'thread_id': 'thread-one', 'created_at': datetime.now(timezone.utc).isoformat(), 'evidence_tags': []}
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    observed = []

    # LLM: sink只接入唯一CandidateService，避免测试绕过原候选持久化。
    # 函数用途: 记录真实候选。
    def sink(observation):
        candidate = service.observe(observation)
        observed.append(candidate)
        return candidate
    result = route_evolution_lesson(tmp_path, request, target, lesson_sink=sink)
    assert result['status'] == 'lesson_candidate'
    assert observed[0].occurrence_count == 1 and observed[0].asset_refs == ['script:owner:sample']
    route_evolution_lesson(tmp_path, request, target, lesson_sink=sink)
    assert observed[-1].occurrence_count == 1
    after = store.get_script('sample')
    assert after['revision'] == before['revision'] and after['metadata'] == before['metadata']
    pitfalls = read_known_pitfalls(tmp_path, 'script', 'sample')
    assert len(pitfalls) == 1 and pitfalls[0]['source'] == 'route3'
    assert route_evolution_lesson(tmp_path, {**request, 'evidence_tags': ['query_failed']}, target, lesson_sink=sink) is None


# === test_v15_w7_mutation_ledger.py: 四种真实捕获源、预算与不污染回执的回归。 ===

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
    code = 'print(\'{"ok": true}\')\n'
    ScriptStore(home).save_script(AssetDescriptor('sample', 'script', 'sample'), code)
    ledger.observe(state, 'scripts', {'action': action, 'script_id': 'sample', 'code': code}, {'ok': True},
                   owner_home=home, workspace_root=work, call_id='save')
    runner = ScriptRunner(home, workspace_root=work, use_sandbox=False, restrict_to_roots=True)
    receipt = ScriptsTool(home, workspace_root=work, runner=runner).execute({'action': 'run', 'script_id': 'sample'})
    value = json.loads(receipt.output)
    assert receipt.ok and value['ok']
    ledger.observe(state, 'scripts', {'action': 'run', 'script_id': 'sample'}, value,
                   owner_home=home, workspace_root=work, call_id='exec')
    target = state['asset_mutations']['targets']['script:sample']
    assert target['last_mutation']['sha256'] == hashlib.sha256(code.encode()).hexdigest()
    assert target['last_execution']['execution_definition_ref'] == value['execution_definition_ref']
    assert target['last_execution']['verified'] is True
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


# === test_v15_w8_analysis_isolation.py: 无状态研判的输入边界及轻量子代理条件降级。 ===

# LLM: 纯选择器不发起调用，未知模式保持安全的无状态缺省。
# 函数用途: 核对默认、显式及条件降级的机器事实。
@pytest.mark.parametrize('params,requested,note', [
    ({}, 'stateless_skill', ''),
    ({'analysis_isolation': 'stateless_skill'}, 'stateless_skill', ''),
    ({'analysis_isolation': 'future'}, 'stateless_skill', ''),
])
def test_mode_selection_records_fallback(params, requested, note):
    assert resolve_analysis_isolation(params) == {
        'requested': requested, 'mode': 'stateless_skill', 'note': note}


# LLM: 宿主提供准确run身份；模拟模型只返回业务JSON，不执行网络或工具。
# 函数用途: 验证最终请求没有主会话、人格、记忆或工具循环。
def test_analysis_call_contains_only_step_material(tmp_path, monkeypatch):
    sent = []

    # LLM: 请求观察在真实handler边界，不替代隔离材料的构造。
    # 函数用途: 捕获最终辅助请求并返回一个合法业务结果。
    def generate(request):
        sent.append(request)
        return SimpleNamespace(text='{"value":7}')

    monkeypatch.setattr(bootstrap, 'generate_auxiliary_model_response', generate)
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda prompt: None),
        session_history=['PRIVATE_HISTORY'], memory_recall=['PRIVATE_MEMORY'], soul='PRIVATE_PERSONA')
    params = {'run_id': 'run-one', 'task_id': 'task-one', 'analysis_isolation': 'stateless_skill'}
    context = SimpleNamespace(prior_outputs={'one': {'value': 7}}, run_inputs={}, step_inputs={},
                              skill_details=[{'body': '绑定方法'}], data_sources=[])
    result = bootstrap._workflow_analysis_handler(agent, run_params=params)({'config': {}}, context, ['owner:analysis'])
    assert result == {'value': 7}
    assert len(sent) == 1 and sent[0].run_id == 'run-one'
    assert sent[0].messages is None and sent[0].tools is None
    assert not any(word in sent[0].prompt for word in ('PRIVATE_HISTORY', 'PRIVATE_MEMORY', 'PRIVATE_PERSONA'))
    assert json.loads(sent[0].prompt)['skill_details'] == [{'body': '绑定方法'}]
    assert context.analysis_isolation['note'] == ''
    assert context.analysis_attempts[0]['analysis_isolation']['mode'] == 'stateless_skill'


def test_explicit_lightweight_request_rejects_before_model_call(monkeypatch):
    monkeypatch.setattr(bootstrap, 'generate_auxiliary_model_response', lambda request: pytest.fail('模型不应执行'))
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda prompt: None))
    params = {'run_id': 'run-one', 'analysis_isolation': 'lightweight_subagent'}
    handler = bootstrap._workflow_analysis_handler(agent, run_params=params)
    with pytest.raises(WorkflowIsolationUnavailable) as caught:
        handler.preflight({'config': {}})
    assert caught.value.details['effect_outcome'] == 'not_started'
    with pytest.raises(WorkflowIsolationUnavailable):
        handler({'config': {}}, SimpleNamespace(), [])


@pytest.mark.parametrize('mode', ['stateless_skill', 'lightweight_subagent'])
def test_real_native_read_step_then_declared_analysis(native_workflow_host, mode):  # noqa: F811
    host = native_workflow_host
    source = host.home_paths.owner_home_dir / 'analysis-input.txt'
    source.write_text('native source evidence', encoding='utf-8')
    prompts = []

    def generate(prompt, response_schema, **kwargs):
        prompts.append(json.loads(prompt))
        assert 'native source evidence' in json.dumps(prompts[-1]['prior_outputs'])
        assert not kwargs.get('tools') and not kwargs.get('messages')
        return SimpleNamespace(text='{"summary":"read-complete"}')

    host.backend.generate_structured = generate
    definition = flow('read-analysis-' + mode.replace('_', '-'), [
        {'step_id': 'read', 'name': 'read', 'mode': 'tool_call', 'on_error': 'fail',
         'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': str(source)}}},
        {'step_id': 'analyze', 'name': 'analyze', 'mode': 'llm_skill_analysis', 'on_error': 'fail',
         'config': {'analysis_isolation': mode}},
    ])
    _, record = local_run(host, definition)
    assert record.steps[0].status == 'succeeded'
    assert source.read_text() == 'native source evidence'
    if mode == 'stateless_skill':
        assert record.status == 'succeeded' and len(prompts) == 1
        assert record.steps[1].output_snapshot == {'summary': 'read-complete'}
    else:
        assert record.status == 'failed' and prompts == []
        assert record.steps[1].error_code == 'WORKFLOW_ISOLATION_UNAVAILABLE'
        assert record.steps[1].llm_involved is False
        assert record.steps[1].escalation_result['analysis_isolation']['effect_outcome'] == 'not_started'


# === test_v15_w8_composition_pins.py: 监控以发布版本绑定；隐藏组合在公共工作流之外承载追加步骤。 ===

# LLM: 用真实版本原语模拟尚未合入的W4发布hook，不替换历史读取函数。
# 函数用途: 创建一份正式发布的基线工作流。
def _publish(home, *, step_id='one'):
    store = WorkflowStore(home)
    definition = store.save_workflow(WorkflowDefinition(AssetDescriptor(id='base', kind='workflow', name='基础流程'),
        (WorkflowStepSpec(step_id, step_id, 'llm_skill_analysis'),)))
    record_version(home, 'workflow', 'base', payload=definition.to_dict(),
                   execution_definition_ref=definition.execution_definition_ref)
    return store, definition


# LLM: 仅显式升级修改pin；后续发布和草稿都不能改变现有绑定。
# 函数用途: 验证默认固定、版本提示、手动升级和原ID去重。
def test_binding_pins_published_version_until_manual_upgrade(tmp_path):
    store, base = _publish(tmp_path)
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'])
    assert first['metadata'].get('pinned_version') == 1
    assert first['metadata']['upgrade_policy'] == 'pinned'
    updated = store.save_workflow(replace(base, steps=base.steps + (WorkflowStepSpec('two', 'two', 'llm_skill_analysis'),)))
    record_version(tmp_path, 'workflow', 'base', payload=updated.to_dict(), execution_definition_ref=updated.execution_definition_ref)
    save_draft(tmp_path, 'workflow', 'base', payload={'steps': []}, base_version=2)
    current = get_monitor_descriptor(tmp_path, 'job48')
    assert current['metadata']['pinned_version'] == 1
    assert current['metadata']['version_projection'] == '当前 v1 · 最新 v2'
    upgraded = save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert upgraded['metadata']['pinned_version'] == 2
    assert upgraded['refs'] == [{'kind': 'workflow', 'id': 'base'}]
    assert len(list_monitor_descriptors(tmp_path)) == 1


# LLM: 基础步骤来自不可变payload，不从公共当前定义复制；所有宿主写入保留hidden。
# 函数用途: 验证组合元数据、追加顺序、版本链及重复保存去重。
def test_hidden_composition_uses_pinned_base_and_rewrites_on_upgrade(tmp_path):
    store, base = _publish(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert binding['metadata']['composition']['base_version'] == 1
    hidden = store.get_workflow('monitor-job48')
    assert hidden.descriptor.metadata['hidden'] is True
    assert hidden.descriptor.metadata['composition'] == {'monitor_id': 'job48', 'base_workflow_id': 'base',
        'base_version': 1, 'appended_step_ids': ['tail']}
    assert [step.step_id for step in hidden.steps] == ['one', 'tail']
    first_version = latest_version(tmp_path, 'workflow', 'monitor-job48')
    assert first_version['pins']['workflows'] == {'base': {'version': 1}}
    save_monitor_binding(tmp_path, 'job48')
    assert latest_version(tmp_path, 'workflow', 'monitor-job48')['version'] == first_version['version']
    changed = store.save_workflow(replace(base, steps=(WorkflowStepSpec('new', 'new', 'llm_skill_analysis'),)))
    record_version(tmp_path, 'workflow', 'base', payload=changed.to_dict(), execution_definition_ref=changed.execution_definition_ref)
    save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert [step.step_id for step in store.get_workflow('monitor-job48').steps] == ['new', 'tail']
    version = latest_version(tmp_path, 'workflow', 'monitor-job48')
    assert version['version'] == first_version['version'] + 1
    assert version['pins']['workflows']['base']['version'] == 2
    payload = json.loads(blob_get(tmp_path, version['payload_blob']))
    # W1有意剔除host-managed hidden/composition；规范存储每次写入仍必须hidden。
    assert payload.get('descriptor', payload)['metadata']['base_workflow_pin'] == {'id': 'base', 'version': 2}
    assert store.get_workflow('monitor-job48').descriptor.metadata['hidden'] is True


# LLM: 草稿没有发布编号，不能通过绑定把它变成正式版本。
# 函数用途: 验证显式不存在的pin在写绑定前拒绝。
def test_draft_only_workflow_cannot_be_pinned(tmp_path):
    save_draft(tmp_path, 'workflow', 'draft', payload={'steps': []})
    with pytest.raises(ValueError):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['draft'], pinned_version=1)
    assert not (tmp_path / 'data/monitors/job48.json').exists()


# LLM: 脚本及其发布记录均来自真实Store，不替换历史、钩子或版本解析器。
# 函数用途: 发布一个带真实脚本依赖的基础流程。
def _script_base(home):
    scripts = ScriptStore(home)
    descriptor = AssetDescriptor(id='collect', kind='script', name='采集')
    scripts.save_script(descriptor, 'print(\'{"value":1}\')')
    store = WorkflowStore(home)
    base = store.save_workflow(WorkflowDefinition(AssetDescriptor(id='base', kind='workflow', name='基础流程'),
        (WorkflowStepSpec('one', '采集', 'deterministic_script', script_id='collect'),)))
    return scripts, store, base, latest_version(home, 'workflow', 'base')


# LLM: 使用绑定返回的目标和编号走真实解析器；编辑当前脚本不能重定义基础历史。
# 函数用途: 复现组合的空pin后继，并要求每份发表记录保持可解析且历史不变。
def test_composition_returned_version_resolves_inherited_script_pin(tmp_path):
    scripts, store, base, publication = _script_base(tmp_path)
    inherited = publication['pins']['scripts']['collect']
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(2)')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}])
    metadata = binding['metadata']
    target = resolve_monitor_target(tmp_path, 'job48', {'kind': 'workflow', 'asset_id': 'base'})
    assert target['pinned_version'] == metadata['composition_version']
    assert target['asset_id'] == metadata['execution_target']['asset_id']
    definition, frozen = resolve_definition(tmp_path, target['asset_id'], target['pinned_version'])
    assert frozen['one']['code_sha256'] == inherited['code_sha256']
    assert [step['step_id'] for step in definition['steps']] == ['one', 'tail']
    assert get_version(tmp_path, 'workflow', 'base', publication['version']) == publication
    records = list_versions(tmp_path, 'workflow', target['asset_id'])
    assert len(records) == 1
    exact = get_version(tmp_path, 'workflow', target['asset_id'], target['pinned_version'])
    assert exact == latest_version(tmp_path, 'workflow', target['asset_id'])
    assert exact['pins']['scripts']['collect'] == inherited
    assert exact['storage_revision'] == store.get_workflow(target['asset_id']).descriptor.revision
    assert base.steps[0].script_id == 'collect'


# LLM: 原引擎及脚本runner实际执行历史字节，仅分析端口由本地函数提供，不请求模型或调度服务。
# 函数用途: 验证真实绑定返回的准确组合版本可以跑完，编辑当前脚本不改变执行结果。
def test_exact_composition_version_runs_historical_script(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(\'{"value":2}\')')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}])
    target = resolve_monitor_target(tmp_path, 'job48', {'kind': 'workflow', 'asset_id': 'base'})
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'tail': True})
    run = engine.run_workflow(target['asset_id'], definition_version=target['pinned_version'])
    assert not isinstance(run, dict), run
    assert run.status == 'succeeded'
    assert run.definition_version == binding['metadata']['composition_version']
    assert run.steps[0].output_snapshot == {'value': 1}
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['_frozen_scripts']['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 独立工作流先占用生成ID；允许显式拒绝，但不得改变其正文、可见性或发布历史。
# 函数用途: 复现合法CAS覆盖无关流程，核验冲突失败没有留下监控绑定。
def test_composition_never_overwrites_independent_generated_id(tmp_path):
    store, _ = _publish(tmp_path)
    independent = store.save_workflow(WorkflowDefinition(
        AssetDescriptor(id='monitor-job48', kind='workflow', name='独立可见流程'),
        (WorkflowStepSpec('independent', '独立', 'llm_skill_analysis'),)))
    publication = latest_version(tmp_path, 'workflow', 'monitor-job48')
    try:
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    except ValueError as exc:
        assert 'COMPOSITION' in str(exc)
    assert store.get_workflow('monitor-job48').to_dict() == independent.to_dict()
    assert not store.get_workflow('monitor-job48').descriptor.metadata.get('hidden')
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') == publication
    assert not (tmp_path / 'data/monitors/job48.json').exists()


# LLM: 普通保存不是宿主组合入口；同名metadata既不能认领独立资产，也不能撤销既有宿主归属。
# 函数用途: 验证隐藏字段不能充当替换另一份工作流的授权令牌。
def test_ordinary_save_cannot_claim_or_remove_composition_provenance(tmp_path):
    store, _ = _publish(tmp_path)
    forged = {'hidden': True, 'composition': {'monitor_id': 'job48'}}
    independent = store.save_workflow(WorkflowDefinition(
        AssetDescriptor(id='monitor-job48', kind='workflow', name='普通流程', metadata=forged), ()))
    assert not independent.descriptor.metadata.get('hidden')
    assert 'composition' not in independent.descriptor.metadata
    store.delete_workflow('monitor-job48')
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    owned = store.get_workflow('monitor-job48')
    replaced = store.save_workflow(replace(owned, descriptor=replace(owned.descriptor, metadata={})))
    assert replaced.descriptor.metadata['hidden'] is True
    assert replaced.descriptor.metadata['composition'] == owned.descriptor.metadata['composition']


# LLM: 新脚本依赖只经真实发布钩子冻结；当前依赖编辑后重复保存不得推新号或换掉旧SHA。
# 函数用途: 验证尾部新增脚本的冻结、无重复后继、真实解析及重复创建幂等。
def test_appended_only_script_is_frozen_once_and_resolves(tmp_path):
    store, _ = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    descriptor = AssetDescriptor(id='tail-script', kind='script', name='追加脚本')
    scripts.save_script(descriptor, 'print(3)')
    script_publication = latest_version(tmp_path, 'script', 'tail-script')
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    target = binding['metadata']['execution_target']['asset_id']
    version = binding['metadata']['composition_version']
    _, pins = resolve_definition(tmp_path, target, version)
    assert pins['tail']['code_sha256'] == script_publication['code_sha256']
    previous = get_version(tmp_path, 'workflow', target, version)
    revision = store.get_workflow(target).descriptor.revision
    scripts.save_script(descriptor, 'print(4)')
    again = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert again['metadata']['execution_target']['asset_id'] == target
    assert again['metadata']['composition_version'] == version
    assert get_version(tmp_path, 'workflow', target, version) == previous
    assert store.get_workflow(target).descriptor.revision == revision
    assert len(list_versions(tmp_path, 'workflow', target)) == 1


# LLM: pin表以script_id为键，同ID继承同一历史身份，不得再读可编辑脚本替换基础SHA。
# 函数用途: 验证基础和尾部共享脚本时采用唯一、可表达的继承身份。
def test_appended_shared_script_keeps_inherited_identity(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(2)')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '再采集', 'mode': 'deterministic_script', 'script_id': 'collect'}])
    metadata = binding['metadata']
    _, pins = resolve_definition(tmp_path, metadata['execution_target']['asset_id'], metadata['composition_version'])
    assert pins['one']['code_sha256'] == pins['tail']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 错误依赖必须在canonical写入前拒绝；异常回收通过实际FD计数而非finally文本推断。
# 函数用途: 验证缺失追加脚本不发表不可用组合且没有FD泄漏。
def test_missing_appended_script_rejected_before_composition_commit(tmp_path):
    store, _ = _publish(tmp_path)
    before = _fd_count()
    with pytest.raises((FileNotFoundError, ValueError)):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '缺失', 'mode': 'deterministic_script', 'script_id': 'missing'}])
    assert _fd_count() - before == 0
    assert store.get_workflow('monitor-job48') is None
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') is None
    assert not (tmp_path / 'data/monitors/job48.json').exists()


# LLM: 只统计本进程实际打开的FD；无目录接口的平台用CRT表，不跳过资源断言。
# 函数用途: 给新成功和错误路径提供独立资源观察点。
def _fd_count():
    if os.name == 'nt':
        import msvcrt
        count = 0
        for descriptor in range(2048):
            try:
                msvcrt.get_osfhandle(descriptor)
                count += 1
            except OSError:
                pass
        return count
    return len(os.listdir('/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'))


# LLM: 透明同步仅控制取得真实存储锁的时刻，不替代Store、发布记录或版本解析器。
# 函数用途: 验证竞争创建者提交后，组合写者在同一事务边界拒绝无关canonical。
def test_competing_create_is_rechecked_at_storage_boundary(tmp_path, monkeypatch):
    store, _ = _publish(tmp_path)
    ready, proceed = Event(), Event()
    original = WorkflowStore._locked

    # LLM: 等待发生在根锁外，独立写者可先提交；实际锁与全部读写仍由原实现负责。
    # 函数用途: 确定性安排竞争创建先于组合存储检查。
    @contextmanager
    def gated(self):
        if current_thread().name.startswith('composition'):
            ready.set()
            assert proceed.wait(10)
        with original(self):
            yield

    monkeypatch.setattr(WorkflowStore, '_locked', gated)
    before = _fd_count()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='composition') as executor:
        future = executor.submit(save_monitor_composition, tmp_path, 'job48', 'base', 1,
            [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
        try:
            assert ready.wait(10)
            independent = store.save_workflow(WorkflowDefinition(
                AssetDescriptor(id='monitor-job48', kind='workflow', name='竞争创建'), ()))
        finally:
            proceed.set()
        with pytest.raises(ValueError, match='COMPOSITION'):
            future.result(timeout=10)
    assert store.get_workflow('monitor-job48').to_dict() == independent.to_dict()
    assert _fd_count() - before == 0


# LLM: 相同监控的显式升级改变基础版本，尾部正常替换每次只添一个历史，旧记录不改。
# 函数用途: 验证保存、换尾、升级及成功路径FD守恒。
def test_composition_replacement_upgrade_and_success_fd_balance(tmp_path):
    scripts, store, base, publication = _script_base(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}]
    before = _fd_count()
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert _fd_count() - before == 0
    identifier = first['metadata']['execution_target']['asset_id']
    old_record = get_version(tmp_path, 'workflow', identifier, first['metadata']['composition_version'])
    replacement = save_monitor_binding(tmp_path, 'job48', appended_steps=[{**appended[0], 'name': '新尾部'}])
    assert replacement['metadata']['composition_version'] == first['metadata']['composition_version'] + 1
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(5)')
    store.save_workflow(replace(base, body_markdown='新版说明'))
    upgraded = save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert upgraded['metadata']['execution_target']['asset_id'] == identifier
    assert upgraded['metadata']['composition_version'] == replacement['metadata']['composition_version'] + 1
    assert get_version(tmp_path, 'workflow', identifier, old_record['version']) == old_record
    assert get_version(tmp_path, 'workflow', 'base', 1) == publication
    latest = latest_version(tmp_path, 'workflow', identifier)
    assert latest['pins']['scripts'] == latest_version(tmp_path, 'workflow', 'base')['pins']['scripts']
    assert resolve_definition(tmp_path, identifier, latest['version'])[0]['body_markdown'] == '新版说明'
    assert _fd_count() - before == 0


# LLM: 可编辑脚本删除后历史仍有效；不能让发布钩子的当前读取偷偷成为继承依赖。
# 函数用途: 验证完全没有当前脚本时仍可组合已发布基础版本。
def test_deleted_current_script_does_not_redefine_inherited_pin(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.delete_script('collect')
    saved = save_monitor_composition(tmp_path, 'job48', 'base', 1, [
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    _, pins = resolve_definition(tmp_path, saved['workflow_id'], saved['version'])
    assert pins['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 已持有的名字不能替代精确监控归属，即使对象是另一监控的隐藏组合也拒绝。
# 函数用途: 核验同名错误归属及FD平衡，且不可变记录未被覆盖。
def test_other_monitor_provenance_is_rejected(tmp_path):
    store, _ = _publish(tmp_path)
    saved = save_monitor_composition(tmp_path, 'job48', 'base', 1, [
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    with store._locked():
        current = store._get(saved['workflow_id'])
        metadata = {**current.descriptor.metadata, 'composition': {**current.descriptor.metadata['composition'], 'monitor_id': 'other'}}
        store._save(replace(current, descriptor=replace(current.descriptor, metadata=metadata)))
    previous = store.get_workflow(saved['workflow_id']).to_dict()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_ID_CONFLICT'):
        save_monitor_composition(tmp_path, 'job48', 'base', 1, [
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    assert store.get_workflow(saved['workflow_id']).to_dict() == previous
    assert _fd_count() - before == 0


# LLM: FD观察器先经真实open/close对照，不能用一个恒定返回值假证明资源平衡。
# 函数用途: 独立核实资源计数器确实看得到文件描述符。
def test_fd_observer_primitive_control(tmp_path):
    before = _fd_count()
    descriptor = os.open(tmp_path / 'fd-control', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert _fd_count() == before + 1
    finally:
        os.close(descriptor)
    assert _fd_count() == before


# LLM: 普通流程编辑形成同正文但不同依赖的后继时，组合重存仍必须核对基础发布的pin。
# 函数用途: 验证内容去重不会借同指纹接受重新钉到当前脚本的基础步骤。
def test_repeat_save_checks_inherited_pin_even_if_content_matches(tmp_path):
    scripts, store, _, publication = _script_base(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    identifier = binding['metadata']['execution_target']['asset_id']
    saved = store.get_workflow(identifier)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(8)')
    store.save_workflow(replace(saved, body_markdown='暂时编辑'))
    store.save_workflow(saved)
    again = save_monitor_binding(tmp_path, 'job48')
    _, pins = resolve_definition(tmp_path, identifier, again['metadata']['composition_version'])
    assert pins['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 历史正文缺失时不能把解析器仅恢复元数据的成功称为可执行，重复去重也须核验必需blob。
# 函数用途: 验证重复保存遇缺失的历史代码明确拒绝且FD不泄漏。
def test_repeat_save_rejects_missing_script_code_blob(tmp_path):
    _, _, _, publication = _script_base(tmp_path)
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    digest = publication['pins']['scripts']['collect']['code_sha256']
    (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
        save_monitor_binding(tmp_path, 'job48')
    assert _fd_count() == before


# LLM: 首次追加依赖也必须核验原发布记录的完整blob，不能仅凭SHA索引行宣称冻结成功。
# 函数用途: 验证新尾部脚本历史代码缺失在canonical提交前拒绝。
@pytest.mark.parametrize('blob_key,corrupt', [
    ('payload_blob', False), ('code_sha256', False), ('payload_blob', True), ('code_sha256', True),
])
def test_new_composition_rejects_missing_appended_history_blob(tmp_path, blob_key, corrupt):
    store, _ = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    scripts.save_script(AssetDescriptor(id='tail-script', kind='script', name='尾部'), 'print(7)')
    publication = latest_version(tmp_path, 'script', 'tail-script')
    digest = publication[blob_key]
    blob = tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest
    if corrupt:
        blob.write_bytes(b'corrupt')
    else:
        blob.unlink()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}])
    assert store.get_workflow('monitor-job48') is None
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') is None
    assert latest_version(tmp_path, 'script', 'tail-script') == publication
    assert not (tmp_path / 'data/monitors/job48.json').exists()
    assert _fd_count() == before


# LLM: 同一个workflow依赖键不能同时表达两个版本；冲突只允许明确拒绝，不覆盖继承版本。
# 函数用途: 验证真实发布的依赖身份冲突没有canonical及监控绑定写入。
def test_unrepresentable_inherited_workflow_pin_is_rejected(tmp_path):
    store, base = _publish(tmp_path)
    # 普通发布入口拒绝自身基础引用；用真实历史原语构造合法旧记录里的额外继承pin。
    publication = record_version(tmp_path, 'workflow', 'base', payload=base.to_dict(),
        execution_definition_ref=base.execution_definition_ref,
        pins={'scripts': {}, 'skills': {}, 'workflows': {'base': {'version': 1}}})['record']
    assert publication['version'] == 2
    assert publication['pins']['workflows'] == {'base': {'version': 1}}
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_CONFLICTING_PINS'):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    assert store.get_workflow('monitor-job48') is None
    assert not (tmp_path / 'data/monitors/job48.json').exists()
    assert get_version(tmp_path, 'workflow', 'base', 2) == publication
    assert _fd_count() == before


# LLM: 透明Event把晚到更新排在真实绑定事务内；两个请求共用原CAS，不能丢写或多发表一份组合。
# 函数用途: 验证同监控竞争更新仅一个提交，晚到旧revision拒绝且FD平衡。
def test_competing_binding_update_keeps_cas_and_history(tmp_path, monkeypatch):
    store, _ = _publish(tmp_path)
    appended = [{'step_id': 'tail', 'name': '原尾部', 'mode': 'llm_skill_analysis'}]
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    identifier = first['metadata']['execution_target']['asset_id']
    publication = latest_version(tmp_path, 'workflow', identifier)
    entered, late_ready = Event(), Event()
    original = WorkflowStore._get

    # LLM: 只观察原锁内读取，不替换返回定义或落盘逻辑；晚到者仍须取得原绑定协调锁。
    # 函数用途: 安排首更新持锁时另一请求带旧revision到达。
    def observed_get(self, workflow_id):
        result = original(self, workflow_id)
        if current_thread().name.startswith('early') and workflow_id == identifier:
            entered.set()
            assert late_ready.wait(10)
        return result

    # LLM: 仅发起公开绑定更新，预期版本取真实首个回执，不制造存储替身。
    # 函数用途: 模拟另一客户端持旧revision竞争更新。
    def late_update():
        late_ready.set()
        return save_monitor_binding(tmp_path, 'job48', expected_revision=first['revision'],
            appended_steps=[{**appended[0], 'name': '晚到尾部'}])

    monkeypatch.setattr(WorkflowStore, '_get', observed_get)
    before = _fd_count()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='early') as early, ThreadPoolExecutor(max_workers=1) as late:
        winner = early.submit(save_monitor_binding, tmp_path, 'job48', expected_revision=first['revision'],
            appended_steps=[{**appended[0], 'name': '首个更新'}])
        try:
            assert entered.wait(10)
            loser = late.submit(late_update)
            result = winner.result(timeout=10)
            with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
                loser.result(timeout=10)
        finally:
            late_ready.set()
    assert get_monitor_descriptor(tmp_path, 'job48')['revision'] == first['revision'] + 1
    assert result['metadata']['composition_version'] == publication['version'] + 1
    assert store.get_workflow(identifier).steps[-1].name == '首个更新'
    assert get_version(tmp_path, 'workflow', identifier, publication['version']) == publication
    assert _fd_count() == before


# LLM: record_version真实允许额外pin；尾部启用该引用时必须检查历史，不读当前脚本来替换它。
# 函数用途: 复现评审指出的未用继承pin在追加步骤使用时漏验代码blob。
@pytest.mark.parametrize('missing_blob', [False, True])
def test_appended_dependency_already_in_unused_base_pins(tmp_path, missing_blob):
    store, base = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    descriptor = AssetDescriptor(id='tail-script', kind='script', name='尾部')
    scripts.save_script(descriptor, 'print(7)')
    script_publication = latest_version(tmp_path, 'script', 'tail-script')
    pin = {'version': script_publication['version'], 'code_sha256': script_publication['code_sha256']}
    record_version(tmp_path, 'workflow', 'base', payload=base.to_dict(),
        execution_definition_ref=base.execution_definition_ref,
        pins={'scripts': {'tail-script': pin}, 'skills': {}, 'workflows': {}})
    scripts.save_script(descriptor, 'print(9)')
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}]
    if missing_blob:
        digest = pin['code_sha256']
        (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
        before = _fd_count()
        with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
            save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
        assert store.get_workflow('monitor-job48') is None
        assert _fd_count() == before
    else:
        binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
        metadata = binding['metadata']
        _, frozen = resolve_definition(tmp_path, metadata['execution_target']['asset_id'], metadata['composition_version'])
        assert frozen['tail']['code_sha256'] == pin['code_sha256']


# === test_v15_w8_monitor_pool_jitter.py: 监控并发、非阻塞准入及重启稳定错峰的确定性交错测试。 ===

# LLM: 时间用纯输入，无真实sleep；摘要公式必须与跨进程口径一致。
# 函数用途: 核验抖动的稳定、边界及分散。
def test_jitter_is_exact_stable_hash_offset():
    jitter = getattr(schedule, 'monitor_jitter_seconds', None)
    assert callable(jitter)
    offsets = [jitter('owner', f'job{i}', window_seconds=30) for i in range(20)]
    assert offsets == [int(hashlib.sha256(f'owner:job{i}'.encode()).hexdigest(), 16) % 30 for i in range(20)]
    assert len(set(offsets)) > 1 and all(0 <= offset < 30 for offset in offsets)
    assert offsets == [jitter('owner', f'job{i}', window_seconds=30) for i in range(20)]
    assert jitter('owner', 'job', window_seconds=0) == 0
    assert schedule.compute_next_run({'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
        after=999, owner_id='owner', monitor_id='job') == 1000 + jitter('owner', 'job')


# LLM: Event控制业务持有时刻，排队和跳过不依赖机器速度。
# 函数用途: 验证运行数和队列有界、同监控重入跳过及满载不等待。
def test_pool_bound_and_duplicate_trigger_skip_without_blocking(tmp_path):
    started, release, dispatched = Event(), Event(), Event()
    pool = MonitorPool(max_workers=1, queue_limit=1)

    # LLM: 只有测试显式释放后才结束，不使用时间猜测并发状态。
    # 函数用途: 保持首个业务运行在途。
    def blocking():
        started.set()
        assert release.wait(5)
        return {'status': 'succeeded'}

    try:
        first = pool.submit(tmp_path, 'job1', 'run1', blocking)
        assert isinstance(first, Future)
        assert started.wait(3)
        duplicate = pool.submit(tmp_path, 'job1', 'run2', lambda: {'status': 'unexpected'})
        queued = pool.submit(tmp_path, 'job2', 'run3', lambda: {'status': 'succeeded'})
        full = pool.submit(tmp_path, 'job3', 'run4', lambda: {'status': 'unexpected'})
        dispatched.set()
        assert dispatched.is_set() and full.done() and duplicate.done()
        assert duplicate.result()['error_code'] == full.result()['error_code'] == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
        assert full.result()['reason'] == 'queue_full'
        assert not queued.done()
        release.set()
        assert first.result(timeout=3)['status'] == queued.result(timeout=3)['status'] == 'succeeded'
    finally:
        release.set()
        pool.shutdown()


# LLM: ContextVar复制不能冒充thread-local，必须实际进入参数绑定作用域。
# 函数用途: 验证每个worker看到自己的run参数且结束后不串身份。
def test_pool_workers_bind_explicit_thread_local_run_params(tmp_path):
    state = local()
    pool = MonitorPool(max_workers=1, queue_limit=2)

    # LLM: 与宿主current_prompt_scope同样保存和恢复原线程参数。
    # 函数用途: 给测试worker绑定一个准确run身份。
    @contextmanager
    def bind(params):
        previous = getattr(state, 'params', None)
        state.params = params
        try:
            yield
        finally:
            state.params = previous

    try:
        results = [pool.submit(tmp_path, f'job{i}', f'run{i}', lambda: state.params,
            run_params=f'run{i}', bind_run_params=bind) for i in range(2)]
        assert all(isinstance(result, Future) for result in results)
        assert [result.result(timeout=3) for result in results] == ['run0', 'run1']
        assert getattr(state, 'params', None) is None
    finally:
        pool.shutdown()


# LLM: 原周期身份不改写，派发和due索引必须使用同一偏移，否则会丢唤醒或提前执行。
# 函数用途: 验证真实调度预约后，错峰门不阻塞并最终发布原Wake。
def test_dispatch_and_due_index_share_jitter_deadline(tmp_path):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at
    from agent_py_agent.agent.scheduler.service import SchedulerService
    from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case

    case = asset_case(tmp_path)
    service = SchedulerService(case.agent.scheduler_repository, conversation_store=case.agent.conversation_store)
    offset = schedule.monitor_jitter_seconds(case.agent.scheduler_repository.owner['id'], case.job['job_id'])
    assert getattr(service, 'monitor_jitter_window_seconds', None) == 30
    case.agent.scheduler_repository.reserve_due_runs(now=1000)
    assert service.enqueue_ready_runs(now=1000 + offset - 0.5) == []
    runs = case.agent.scheduler_repository.queued_runs(now=1000 + offset)
    assert len(runs) == 1 and runs[0]['scheduled_for'] == 1000
    store = {'owner': case.agent.scheduler_repository.owner, 'jobs': {case.job['job_id']: case.job},
             'runs': {runs[0]['run_id']: runs[0]}}
    assert _earliest_due_at(store, now=1000) == 1000 + offset
    assert len(service.enqueue_ready_runs(now=1000 + offset)) == 1
    service.monitor_pool.shutdown()
