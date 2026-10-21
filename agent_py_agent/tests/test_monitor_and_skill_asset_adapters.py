# LLM: 只用tmp_path的合成Owner/Job/Skill核验适配，不读真实凭据、不启动Scheduler或模型。
# 模块用途: 守住监控绑定、技能提取、元数据更新、标准字典及路径安全。
import json

import pytest

from agent_py_agent.agent.assets import monitor_adapter as monitors
from agent_py_agent.agent.assets import skill_adapter as skills
from agent_py_agent.agent.assets.descriptor import AssetDescriptor


# LLM: 合成Skill只写测试临时目录，正文与元数据独立，便于验证更新不会改正文。
# 函数用途: 准备含YAML头的技能样本。
def _skill(root, name='analyse', header=None):
    directory = root / name
    directory.mkdir(parents=True)
    text = header or 'name: 分析技能\ndescription: 查询并分析\nwhen_to_use: 需要分析时\ntags: [analysis, daily]\n'
    (directory / 'SKILL.md').write_text('---\n' + text + '---\n\n# 方法\n不要执行此正文。\n', encoding='utf-8')
    return directory


# LLM: 仅断言标准公共字段可由共同契约无损恢复，适配器扩展在metadata中也必须保留。
# 函数用途: 检查返回值不是对象或自造的非标准结构。
def _standard(payload, kind):
    assert isinstance(payload, dict)
    assert payload['schema_version'] == 'asset_descriptor.v1' and payload['kind'] == kind
    extra = {'schedule_spec', 'when_to_use', 'category'}
    base = {key: value for key, value in payload.items() if key not in extra}
    assert AssetDescriptor.from_dict(base).to_dict() == base


# LLM: 缺失功能必须先表现为断言失败；公共查询返回空列表，不创建owner目录。
# 函数用途: 固定无资产时的只读行为。
def test_empty_owner_is_read_only(tmp_path):
    home = tmp_path / 'absent'
    assert monitors.list_monitor_descriptors(home) == []
    assert skills.list_owner_skill_descriptors(home) == []
    assert not home.exists()


# LLM: 绑定只保存显式元数据，引用去重，秘密键不进入文件或公开描述符。
# 函数用途: 验证监控落盘位置、三类引用和标准Key往返。
def test_monitor_binding_roundtrip(tmp_path):
    descriptor = monitors.save_monitor_binding(tmp_path, 'job_a', name='日监控', summary='摘要',
        schedule_spec={'kind': 'cron', 'expression': '0 9 * * *', 'timezone': 'UTC', 'enabled': True},
        workflow_ids=['flow_a', 'flow_a'], script_ids=['script_a'], skill_ids=['analyse'],
        access_targets=[{'target_id': 'api', 'type': 'http', 'endpoint': 'https://example.test', 'extra': {'password': 'synthetic'}}],
        data_sources=[{'source_id': 'logs', 'system': 'audit', 'query_scope': {'apps': ['a']}}],
        credential_refs=[{'credential_ref': 'cred_synthetic', 'status': 'configured'}])
    _standard(descriptor, 'monitor')
    assert descriptor['refs']['workflows'] == ['flow_a']
    assert descriptor['refs']['scripts'] == ['script_a'] and descriptor['refs']['skills'] == ['analyse']
    assert descriptor['owner_id'] == tmp_path.name
    assert descriptor['schedule_spec'] == descriptor['metadata']['schedule_spec']
    path = tmp_path / 'data/monitors/job_a.json'
    assert path.is_file() and '"password"' not in path.read_text()
    assert monitors.get_monitor_descriptor(tmp_path, 'job_a') == descriptor
    assert monitors.list_monitor_descriptors(tmp_path) == [descriptor]


# LLM: Scheduler是真实运行状态与计划的权威，绑定文件只补名称/引用；不改传入Job。
# 函数用途: 验证旧Job与扩展绑定合并及每种计划表达式。
@pytest.mark.parametrize('schedule,expression', [
    ({'kind': 'cron', 'expression': '0 * * * *', 'timezone': 'UTC'}, '0 * * * *'),
    ({'kind': 'every', 'every_seconds': 90, 'timezone': 'UTC'}, '90'),
    ({'kind': 'at', 'at': '2026-11-01T00:00:00Z', 'timezone': 'UTC'}, '2026-11-01T00:00:00Z'),
])
def test_scheduler_fields_override_stale_binding(tmp_path, schedule, expression):
    monitors.save_monitor_binding(tmp_path, 'job_a', name='绑定名', workflow_ids=['flow'],
        schedule_spec={'kind': 'cron', 'expression': 'stale', 'enabled': True})
    job = {'job_id': 'job_a', 'name': '原Job', 'prompt': '任务摘要', 'owner': {'id': 'local/main'},
        'schedule': schedule, 'next_run_at': 2000, 'status': 'paused', 'version': 3,
        'last_run_at': 1000, 'last_run_status': 'failed', 'last_duration_ms': 42,
        'run_count': 5, 'consecutive_failures': 2, 'skill_refs': ['analyse']}
    before = json.dumps(job)
    descriptor = monitors.get_monitor_descriptor(tmp_path, 'job_a', job)
    _standard(descriptor, 'monitor')
    assert descriptor['name'] == '绑定名' and descriptor['summary'] == '任务摘要'
    assert descriptor['schedule_spec']['expression'] == expression
    assert descriptor['schedule_spec']['enabled'] is False and descriptor['schedule_spec']['next_run_at'] == 2000
    assert descriptor['runtime_summary'] == {'last_run_at': '1970-01-01T00:16:40Z',
        'last_status': 'failed', 'last_duration_ms': 42, 'run_count': 5, 'consecutive_failures': 2, 'last_error': ''}
    assert descriptor['owner_id'] == 'local/main' and descriptor['version'] == '3'
    assert descriptor['refs']['skills'] == ['analyse'] and json.dumps(job) == before


# LLM: 列表合并只按准确job_id，已删除Job不复活为绑定节点，重复或错ID显式拒绝。
# 函数用途: 核对去重排序、缺失和损坏记录的处理。
def test_monitor_list_and_invalid_records(tmp_path):
    monitors.save_monitor_binding(tmp_path, 'bound', name='仅绑定')
    jobs = [{'job_id': 'plain', 'name': '普通', 'status': 'active'}, {'job_id': 'bound', 'name': '旧名', 'status': 'deleted'}]
    assert [d['id'] for d in monitors.list_monitor_descriptors(tmp_path, jobs)] == ['plain']
    with pytest.raises(FileNotFoundError):
        monitors.get_monitor_descriptor(tmp_path, 'missing')
    with pytest.raises(ValueError):
        monitors.get_monitor_descriptor(tmp_path, 'bound', {'job_id': 'other'})
    (tmp_path / 'data/monitors/bound.json').write_text('{bad', encoding='utf-8')
    with pytest.raises(ValueError):
        monitors.get_monitor_descriptor(tmp_path, 'bound')


# LLM: frontmatter优先身份，sidecar补提取Key；原正文不是元数据或执行权。
# 函数用途: 验证YAML块描述、标签和JSON扩展合并。
def test_skill_extract_and_update_preserves_source(tmp_path):
    directory = _skill(tmp_path, header='name: 分析\ndescription: >\n  第一行\n  第二行\ncategory: ops\nversion: "2.0"\nwhen_to_use: 按需\ntags: [a, b]\n')
    original = (directory / 'SKILL.md').read_bytes()
    (directory / 'skill_meta.json').write_text(json.dumps({'refs': {'scripts': ['collect']}, 'custom_note': '保留'}))
    descriptor = skills.extract_skill_descriptor(directory, 'owner-a')
    _standard(descriptor, 'skill')
    assert descriptor['id'] == 'analyse' and descriptor['name'] == '分析'
    assert descriptor['summary'] == '第一行 第二行' and descriptor['version'] == '2.0'
    assert descriptor['when_to_use'] == '按需' and descriptor['category'] == 'ops' and descriptor['tags'] == ['a', 'b']
    updated = skills.update_skill_extraction_keys(directory, tags=[], credential_refs=[{'credential_ref': 'cred_fake'}],
        io_contract={'inputs': [{'name': 'log'}], 'outputs': []},
        access_targets=[{'target_id': 'api', 'type': 'http', 'endpoint': 'https://example.test', 'extra': {'token': 'synthetic'}}])
    assert updated['tags'] == [] and updated['credential_refs'][0]['credential_ref'] == 'cred_fake'
    assert updated['refs']['scripts'] == ['collect']
    assert (directory / 'SKILL.md').read_bytes() == original
    meta = json.loads((directory / 'skill_meta.json').read_text())
    assert meta['custom_note'] == '保留' and 'synthetic' not in str(meta)
    assert skills.extract_skill_descriptor(directory / 'SKILL.md')['tags'] == []


# LLM: Owner技能优先extra_roots，同目录或同ID不重复；learned层也要扫描，不读取任意正文资产。
# 函数用途: 固定技能发现范围和稳定去重顺序。
def test_skill_owner_learned_and_extra_roots(tmp_path):
    owner, extra = tmp_path / 'owner', tmp_path / 'loaded'
    _skill(owner / 'skills', 'a')
    _skill(owner / 'skills/learned', 'b')
    _skill(extra, 'a', 'name: 外部重复\ndescription: 重复\n')
    third = _skill(extra, 'c')
    found = skills.list_owner_skill_descriptors(owner, (extra, third / 'SKILL.md'))
    assert [d['id'] for d in found] == ['a', 'b', 'c']
    assert found[0]['name'] == '分析技能' and all(d['owner_id'] == 'owner' for d in found)


# LLM: 非对象sidecar、坏JSON与非法提取字段不能覆盖旧元数据；同层非法YAML不静默变空。
# 函数用途: 检查写前验证与损坏来源的显式失败。
def test_skill_invalid_metadata_does_not_write(tmp_path):
    directory = _skill(tmp_path)
    path = directory / 'skill_meta.json'
    path.write_text('{"custom_note": "keep"}')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        skills.update_skill_extraction_keys(directory, credential_refs=['not-a-binding'])
    assert path.read_bytes() == before
    path.write_text('[]')
    with pytest.raises(ValueError):
        skills.extract_skill_descriptor(directory)


# LLM: traversal与符号链接都不能成为Owner边界内的读写路径；外部哨兵必须保持原字节。
# 函数用途: 防止监控ID、技能路径和sidecar链接越界。
@pytest.mark.parametrize('identifier', ['../outside', '/abs', '.', '..', 'x/y'])
def test_monitor_rejects_unsafe_id(tmp_path, identifier):
    with pytest.raises(ValueError):
        monitors.save_monitor_binding(tmp_path, identifier)


# LLM: 所有测试链接均指向tmp_path内哨兵，不访问真实文件；读取和更新都必须拒绝。
# 函数用途: 验证父目录与叶链接不能带出资产边界。
def test_adapter_symlinks_fail_closed(tmp_path):
    owner, outside = tmp_path / 'owner', tmp_path / 'outside'
    owner.mkdir()
    outside.mkdir()
    (owner / 'data').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        monitors.save_monitor_binding(owner, 'job_a')
    assert list(outside.iterdir()) == []
    directory = _skill(tmp_path, 'safe')
    sentinel = outside / 'meta.json'
    sentinel.write_text('{"private": "unchanged"}')
    (directory / 'skill_meta.json').symlink_to(sentinel)
    with pytest.raises(OSError):
        skills.update_skill_extraction_keys(directory, tags=['x'])
    assert sentinel.read_text() == '{"private": "unchanged"}'
    (tmp_path / 'linked').symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        skills.extract_skill_descriptor(tmp_path / 'linked')


# LLM: 样本由真实Scheduler仓库创建，不把手写字符串列表误当作已归一的Skill版本引用。
# 函数用途: 复现Scheduler实际skill_refs结构，校验适配器能展示依赖和原版本。
def test_real_scheduler_record_and_pinned_skill_refs(tmp_path):
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRepository,
    )
    repository = SchedulerRepository(tmp_path / 'data/scheduler', owner_provider='local', owner_kind='main', owner_id='local/main')
    reference = {'stable_id': 'owner:analyse', 'content_sha256': 'a' * 64}
    job, _ = repository.create_job(SchedulerJobCreateRequest(name='真实格式', prompt='测试', thread_id='thread_fixture',
        schedule={'kind': 'every', 'every_seconds': 90, 'anchor_at': 1000, 'timezone': 'UTC'}, skill_refs=[reference], now=1000))
    descriptor = monitors.get_monitor_descriptor(tmp_path, job['job_id'])
    assert descriptor['refs']['skills'] == ['analyse']
    assert descriptor['metadata']['scheduler_skill_refs'] == [reference]
    assert monitors.list_monitor_descriptors(tmp_path) == [descriptor]


# LLM: 复合Job不能把第一条规则冒充整个监控计划；每条规则的enabled与时间独立展示。
# 函数用途: 验证多规则计划保留及顶层暂停约束。
def test_composite_schedule_is_explicit(tmp_path):
    job = {'job_id': 'multi', 'name': '多规则', 'status': 'paused', 'rules': [
        {'id': 'r1', 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 90}, 'next_run_at': 2000},
        {'id': 'r2', 'enabled': False, 'schedule': {'kind': 'cron', 'expression': '0 * * * *'}}]}
    result = monitors.get_monitor_descriptor(tmp_path, 'multi', job)
    assert result['schedule_spec']['kind'] == 'composite'
    assert [r['id'] for r in result['schedule_spec']['rules']] == ['r1', 'r2']
    assert not any(r['enabled'] for r in result['schedule_spec']['rules'])


# LLM: YAML引号中的井号是普通值，常见嵌套映射/列表必须保留为结构化提取Key，不默默丢失。
# 函数用途: 覆盖含访问目标和依赖的标准YAML头。
def test_skill_yaml_structured_keys_and_quoted_hash(tmp_path):
    directory = _skill(tmp_path, header='''name: Analyse
summary: ignored
description: "Use #one and #two"
refs:
  scripts: [collect]
access_targets:
  - target_id: api
    type: http
    endpoint: https://example.test/api
    extra:
      password: synthetic
io_contract:
  inputs: [log]
  outputs: []
tags: [daily]
''')
    result = skills.extract_skill_descriptor(directory)
    assert result['summary'] == 'Use #one and #two'
    assert result['refs']['scripts'] == ['collect']
    assert result['access_targets'][0]['endpoint'] == 'https://example.test/api'
    assert 'password' not in str(result) and result['io_contract']['inputs'] == ['log']


# LLM: 特殊目录名只作为只读来源说明，不成为路径授权；标准引用仍须能经AssetDescriptor往返。
# 函数用途: 复现中文/空格目录炸掉Owner列表及缺少双向查找的问题。
@pytest.mark.parametrize('name', ['安全分析', 'audit notes', 'foo+bar'])
def test_skill_special_directory_has_stable_id_and_dual_lookup(tmp_path, name):
    home = tmp_path / 'owner'
    directory = _skill(home / 'skills/learned', name)
    found = skills.list_owner_skill_descriptors(home)
    assert len(found) == 1
    descriptor = found[0]
    assert descriptor['id'].startswith('skill_') and descriptor['name'] == name
    assert descriptor['refs']['skill_dir'] == name
    _standard(descriptor, 'skill')
    assert skills.list_owner_skill_descriptors(home) == found
    getter = getattr(skills, 'get_owner_skill_descriptor', None)
    assert callable(getter), '缺少Owner技能详情读口'
    assert getter(home, descriptor['id']) == getter(home, name) == descriptor
    original = (directory / 'SKILL.md').read_bytes()
    assert skills.update_skill_extraction_keys(directory, tags=['updated'])['id'] == descriptor['id']
    assert (directory / 'SKILL.md').read_bytes() == original


# LLM: 同名优先级不能依赖DFS字典序；目录同名而头部id不同仍须以普通Owner路径为准。
# 函数用途: 复现learned目录先被遍历导致普通技能被遮蔽的问题。
def test_regular_skill_wins_over_learned_with_same_directory_name(tmp_path):
    home = tmp_path / 'owner'
    _skill(home / 'skills', 'zeta', 'name: regular\ndescription: regular\n')
    _skill(home / 'skills/learned', 'zeta', 'id: alternate\nname: learned\ndescription: learned\n')
    _skill(home / 'skills/learned', 'alpha', 'name: learned-only\n')
    found = skills.list_owner_skill_descriptors(home)
    assert [d['id'] for d in found] == ['alpha', 'zeta']
    assert found[1]['name'] == 'regular'
    getter = getattr(skills, 'get_owner_skill_descriptor', None)
    assert callable(getter), '缺少Owner技能详情读口'
    assert getter(home, 'zeta') == found[1]
    assert getter(home, 'alpha') == found[0]
    with pytest.raises(FileNotFoundError):
        getter(home, '../outside')


# LLM: 非ASCII目录身份来自实际目录，不可被侧元数据或头部id重定位；原名和派生id共指同一目录。
# 函数用途: 复现独审指出的metadata.id覆盖目录派生身份。
def test_special_directory_id_does_not_change_with_metadata_id(tmp_path):
    home = tmp_path / 'owner'
    directory = _skill(home / 'skills', '中文 技能', 'id: custom-skill\nname: shown\n')
    first = skills.list_owner_skill_descriptors(home)[0]
    assert first['id'].startswith('skill_') and first['id'] != 'custom-skill'
    (directory / 'skill_meta.json').write_text('{"id":"different-skill"}', encoding='utf-8')
    second = skills.list_owner_skill_descriptors(home)[0]
    assert first['id'] == second['id']
    assert skills.get_owner_skill_descriptor(home, first['id']) == second
    assert skills.get_owner_skill_descriptor(home, '中文 技能') == second
