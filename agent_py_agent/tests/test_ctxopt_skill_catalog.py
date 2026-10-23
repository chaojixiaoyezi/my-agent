# LLM: 使用临时Owner与真实发现/快照，不调用模型或修改真实资料。
# 模块用途: 回归统一引用、codec、来源优先级及快照授权。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import assets
from agent_py_agent.agent.assets import skill_adapter as adapter
from agent_py_agent.agent.capability import skills
from agent_py_agent.agent.capability.skill_service import SkillsService


# LLM: 所有合成正文只写pytest临时目录。
# 函数用途: 创建明确来源和内容的技能。
def skill(root, name, description='fixture', extra=''):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {description}\n{extra}---\n正文。\n')
    return path


# LLM: Owner策略只授权测试中的根，不读取真实用户策略。
# 函数用途: 构造正式技能目录服务。
def service(home, workspace):
    paths = SimpleNamespace(owner_home_dir=home, shared_skills_dir=home/'shared', shared_builtin_dir=home/'builtin')
    policy = SimpleNamespace(owner_id='test', skills_enabled=True,
                             enabled_skill_sources=('workspace', 'owner', 'shared', 'builtin'),
                             enabled_shared_skills=(), disabled_skills=())
    return SkillsService(home_paths=paths, workspace_root=workspace, policy_provider=lambda: policy)


# LLM: 逻辑来源引用绝不能放宽文件名的校验。
# 函数用途: 验证两类ID合同相互独立。
@pytest.mark.parametrize('reference', ['demo', 'owner:demo', 'workspace:paper-digest', 'shared:x',
                                     'builtin:writing-skills', 'learned:x', 'plugin:pkg:demo'])
def test_reference_contract(reference):
    assert assets.valid_skill_reference(reference) == reference
    assert assets.canonical_skill_name(reference) == reference.rsplit(':', 1)[-1]
    if ':' in reference:
        with pytest.raises(ValueError):
            assets.safe_asset_id(reference)


# LLM: 非法来源和路径不得被清洗为合法引用。
# 函数用途: 拒绝逃逸与畸形逻辑引用。
@pytest.mark.parametrize('reference', ['', '../demo', '/demo', 'a/b', '.', '..', 'owner:', 'owner:a:b',
                                     'unknown:demo', 'plugin:demo', 'plugin:..:demo', 'plugin:pkg:../demo', None])
def test_invalid_reference(reference):
    with pytest.raises(ValueError):
        assets.valid_skill_reference(reference)


# LLM: Card和资产描述符必须解释相同原文，不能各自修补井号和标签。
# 函数用途: 对比两种公开读取入口的codec结果。
@pytest.mark.parametrize('tags', ['tags: 安全, 分析\n', 'tags: [安全, 分析]\n', 'tags:\n  - 安全\n  - 分析\n'])
def test_shared_codec(tmp_path, tags):
    path = skill(tmp_path, 'demo', '"C# 代码审核"', tags)
    card = skills.parse_skill_file(path)
    asset = adapter.extract_skill_descriptor(path)
    assert card.description == asset['summary'] == 'C# 代码审核'
    assert card.tags == asset['tags'] == ['安全', '分析']


# LLM: 块内井号及空行是正文，不是YAML注释。
# 函数用途: 校验折叠与literal描述完整往返。
@pytest.mark.parametrize(('description', 'expected'), [
    ('>\n  第一行\n  第二行', '第一行 第二行'),
    ('>-\n  第一段\n\n  第二段', '第一段\n第二段'),
    ('|\n  第一行 # 文本\n\n  第二行', '第一行 # 文本\n\n第二行'),
    ('|-\n  C#\n  下一行', 'C#\n下一行'),
])
def test_block_scalars(tmp_path, description, expected):
    path = skill(tmp_path, 'demo', description)
    assert skills.parse_skill_file(path).description == expected
    assert adapter.extract_skill_descriptor(path)['summary'] == expected


# LLM: YAML仅解析安全数据，拒绝tag、anchor及无界结构。
# 函数用途: 校验危险语法和大小/深度预算。
def test_codec_limits(tmp_path):
    for extra in ('tags: [!!python/object:bad {}]\n', 'tags: [&alias x, *alias]\n', 'io_contract: &alias {}\n'):
        path = skill(tmp_path, 'broken', extra=extra)
        with pytest.raises(ValueError):
            skills.parse_skill_file(path)
        with pytest.raises(ValueError):
            adapter.extract_skill_descriptor(path)
    with pytest.raises(ValueError):
        skills.split_skill_frontmatter('x' * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        skills.split_skill_frontmatter('---\nio_contract: ' + '['*40 + '0' + ']'*40 + '\n---\n')


# LLM: 空或受限快照是唯一权限来源，不能补扫owner根。
# 函数用途: 对齐目录顺序并验证准确来源、正文版本和空集合权限。
def test_roots_sources_and_snapshot_authority(tmp_path, monkeypatch):
    home, workspace = tmp_path/'owner', tmp_path/'custom'
    roots = [workspace/'.agents/skills', workspace/'.agent/skills', home/'workspace/.agents/skills',
             home/'workspace/.agent/skills', home/'skills', home/'skills/learned', home/'learned']
    for index, root in enumerate(roots):
        skill(root, f'demo-{index}')
    skill(roots[0], 'same', 'workspace')
    skill(roots[4], 'same', 'owner')
    skill(roots[5], 'same', 'learned')
    catalog = service(home, workspace)
    snapshot = catalog.snapshot_for()
    assert [root.path for root in catalog.base_roots(workspace)[:4]] == roots[:4]
    assert {entry.name for entry in snapshot.enabled_entries()} == {
        row['name'] for row in adapter.list_owner_skill_descriptors(home, workspace_root=workspace)}
    assert snapshot.resolve('same').description == 'workspace'
    assert adapter.get_owner_skill_descriptor(home, 'owner:same', workspace_root=workspace)['summary'] == 'owner'
    restricted = snapshot.restricted(('workspace:same',))
    monkeypatch.setattr(adapter, '_catalog_paths', lambda *a, **kw: pytest.fail('快照不得补扫'))
    assert [row['id'] for row in adapter.list_owner_skill_descriptors(home, skill_snapshot=restricted)] == ['same']
    with pytest.raises(FileNotFoundError):
        adapter.get_owner_skill_descriptor(home, 'owner:same', skill_snapshot=restricted)
    empty = replace(restricted, entries=())
    assert adapter.list_owner_skill_descriptors(home, skill_snapshot=empty) == []
    with pytest.raises(FileNotFoundError):
        adapter.get_owner_skill_descriptor(home, 'same', skill_snapshot=empty)
    skill(roots[0], 'same', 'changed')
    with pytest.raises(ValueError):
        adapter.get_owner_skill_descriptor(home, 'workspace:same', skill_snapshot=restricted)


# LLM: 损坏无关文件只影响自身，指定坏文件不能被当作未发现而吞掉。
# 函数用途: 验证批量发现隔离和单项显式报错。
def test_bad_file_isolation(tmp_path):
    home = tmp_path/'owner'
    skill(home/'skills', 'good')
    broken = skill(home/'skills', 'broken')
    broken.with_name('skill_meta.json').write_text('{bad')
    assert [row['id'] for row in adapter.list_owner_skill_descriptors(home)] == ['good']
    with pytest.raises(ValueError):
        adapter.get_owner_skill_descriptor(home, 'owner:broken')
