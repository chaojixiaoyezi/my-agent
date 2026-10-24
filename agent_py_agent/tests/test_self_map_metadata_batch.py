# LLM: 元信息夹具使用真实登记表和活动路径；正文读取另测即时撤回，不缓存授权。
# 模块用途: 锁住learned显示批每store一次加载与跨调用实时状态。
# ruff: noqa: F811
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.capability.skill_learning_store import (
    LearnedSkill,
    SkillLearningRegistry,
    SkillLearningStore,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from agent_py_agent.tests.test_self_map_runtime import _read, make_agent  # noqa: F401


# LLM: 仅在临时owner中构造已发布条目，不伪造正文读取或远端服务成功。
# 函数用途: 建一组共享登记根且具有不同layout的冻结方法。
def _learned(agent, count, *, root=None):
    store = SkillLearningStore.for_home(agent.home_paths) if root is None else SkillLearningStore(root / 'data/skill_learning', root / 'skills')
    entries, records = [], {}
    for index in range(count):
        name = f'learned-{index}'
        record = LearnedSkill(name, 1, 'a' * 64, '2026-10-23', '2026-10-23', layout='owner' if index % 2 else 'learned')
        active = (store.skills_root if record.layout == 'owner' else store.learned_root) / name / 'SKILL.md'
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text('active body')
        frozen = store.versions_dir / name / 'v1.md'
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text('frozen body')
        records[name] = record
        entries.append(SkillSnapshotEntry('owner:' + name, name, '用途', str(active), 'owner', 'future', 'a' * 64, frozen_path=str(frozen)))
    registry = SkillLearningRegistry(skills=records)
    store.registry_path.write_text(json.dumps(registry.to_record()))
    return store, tuple(entries), registry


# LLM: 统计原load_registry真实调用；limit只限输出，仍须对整个授权列表批量投影。
# 函数用途: N=1/100/1000均只读同一登记表一次且不读正文。
@pytest.mark.parametrize('count', [1, 100, 1000])
def test_metadata_list_loads_registry_once_per_store(make_agent, monkeypatch, count):
    agent = make_agent()
    store, entries, _ = _learned(agent, count)
    agent._current_skill_snapshot = SkillSnapshot(entries, (), 'batch', agent.home_paths.owner_id, '')
    calls, original = [], SkillLearningStore.load_registry
    monkeypatch.setattr(SkillLearningStore, 'load_registry', lambda self: calls.append(self.directory) or original(self))
    monkeypatch.setattr('agent_py_agent.agent.capability.skill_learning_versions.read_text_beneath', lambda *a, **k: pytest.fail('body read'))
    value = _read(agent, 'skills', limit=1)
    assert calls == [store.directory]
    assert value['total'] == count and value['items'][0]['lifecycle']['authorized']


# LLM: 总览只用本轮已授权快照的类别和数量，不给逐项生命周期结论。
# 函数用途: skills总览不读登记表或正文。
def test_skills_overview_uses_snapshot_only(make_agent, monkeypatch):
    agent = make_agent()
    _, entries, _ = _learned(agent, 2)
    agent._current_skill_snapshot = SkillSnapshot(entries, (), 'summary', agent.home_paths.owner_id, '')
    monkeypatch.setattr(SkillLearningStore, 'load_registry', lambda *_: pytest.fail('lifecycle read'))
    value = _read(agent)
    row = next(row for row in value['sections'] if row['section'] == 'skills')
    assert row['count'] == 2 and value['capability_families']['skills'] == {'future': 2}


# LLM: 原正文入口必须重新加载登记表，显示投影不得成为跨调用授权缓存。
# 函数用途: 下一调用停用立即revoked，正文仍实时拒绝。
@pytest.mark.parametrize('state', ['disabled', 'archived', 'removed'])
def test_metadata_revocation_and_body_reauthorization(make_agent, state):
    from agent_py_agent.agent.capability.skill_learning_versions import read_frozen_learned_body
    agent = make_agent()
    store, entries, registry = _learned(agent, 1)
    agent._current_skill_snapshot = SkillSnapshot(entries, (), 'revoke', agent.home_paths.owner_id, '')
    assert _read(agent, 'skills')['items'][0]['readable'] is True
    record = registry.skills[entries[0].name]
    registry = replace(registry, skills={} if state == 'removed' else {record.name: replace(record, maintenance={'state': state})})
    store.registry_path.write_text(json.dumps(registry.to_record()))
    row = _read(agent, 'skills')['items'][0]
    assert row['state'] == 'revoked' and row['readable'] is False and row['lifecycle']['state'] == state
    with pytest.raises(FileNotFoundError):
        read_frozen_learned_body(entries[0])


# LLM: 同名条目仍按受信冻结根分开批读，错误store不污染其它store。
# 函数用途: 多store每根只读一次且不串生命周期。
def test_metadata_groups_distinct_stores(make_agent, tmp_path, monkeypatch):
    agent = make_agent()
    store, entries, _ = _learned(agent, 1)
    other, extras, _ = _learned(agent, 1, root=tmp_path / 'trusted-second')
    extras = (replace(extras[0], stable_id='second:learned-0'),)
    agent._current_skill_snapshot = SkillSnapshot(entries + extras, (), 'multi', agent.home_paths.owner_id, '')
    calls, original = [], SkillLearningStore.load_registry
    with patch.object(SkillLearningStore, 'load_registry', lambda self: calls.append(self.directory) or original(self)):
        assert len(_read(agent, 'skills')['items']) == 2
    assert sorted(calls) == sorted([store.directory, other.directory])


# LLM: 显示批保留活动路径与is_file事实；登记损坏必须不可用而非假revoked/空库。
# 函数用途: 活动文件缺失、layout迁移和坏登记不产生readable真值。
@pytest.mark.parametrize('change', ['missing', 'layout', 'corrupt'])
def test_metadata_keeps_path_and_registry_validation(make_agent, change):
    agent = make_agent()
    store, entries, registry = _learned(agent, 1)
    agent._current_skill_snapshot = SkillSnapshot(entries, (), 'invalid', agent.home_paths.owner_id, '')
    if change == 'missing':
        from pathlib import Path
        Path(entries[0].path).rename(store.skills_root / 'moved.md')
    elif change == 'layout':
        record = registry.skills[entries[0].name]
        store.registry_path.write_text(json.dumps(replace(registry, skills={record.name: replace(record, layout='owner')}).to_record()))
    else:
        store.registry_path.write_text('{bad')
    row = _read(agent, 'skills')['items'][0]
    assert row['readable'] is not True
    assert row['state'] == ('unavailable' if change == 'corrupt' else 'revoked')
