# LLM: 只写临时Owner和合成Skill；真实codec/SkillsService不mock，快照读盘计数不代表在线性能测量。
# 模块用途: 回归冻结审计的YAML兼容、安全边界和快照stat缓存失效。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_service as service_module
from agent_py_agent.agent.capability import skills as skills_module
from agent_py_agent.agent.capability.skills import MAX_FRONTMATTER_DEPTH, split_skill_frontmatter


# LLM: 标量输入来自真实合法YAML形状；值里的引号不是新标量开始，注释只在非引号区生效。
# 函数用途: 同时锁定行中引号、多行引号、普通续行和流容器成员的解析结果。
@pytest.mark.parametrize('header,expected', [
    ("description: use 'em when needed", "use 'em when needed"),
    ('description: use "em when needed', 'use "em when needed'),
    ("description: 用法：sed -e 's/[a-z]/X/ # note", "用法：sed -e 's/[a-z]/X/"),
    ('description: "first line\n  second line" # comment', 'first line second line'),
    ("description: 'first line\n  second line'", 'first line second line'),
    ("description: 'it''s the first\n  second line'", "it's the first second line"),
    ('description: "C# first\n  second # line"', 'C# first second # line'),
    ('description: First line\n  second line\n  third line # comment', 'First line second line third line'),
    ('description: "first\\\n  second"', 'firstsecond'),
    ("tags: [use 'em when needed, review]", ["use 'em when needed", 'review']),
    ('tags: [2024, review, 1.5, true, false]', ['2024', 'review', '1.5', 'True', 'False']),
    ('tags:\n  - 2024\n  - review\n  - false', ['2024', 'review', 'False']),
    ('tags: 2024', ['2024']),
    ("tags:\n  - use 'em:today", ["use 'em:today"]),
    ("tags:\n  - 'a:b\n    c'", ['a:b c']),
    ('tags:\n  - "a:b\n    c"', ['a:b c']),
    ("description: 'a\n\n  b'", 'a\nb'),
    ('description: "a\n\n  b"', 'a\nb'),
    ('description: a\n  # note\n  b', 'a b'),
    ('description: a\n  # note\n\n  b', 'a\nb'),
])
def test_frontmatter_valid_scalar_edges(header, expected):
    meta, body = split_skill_frontmatter(f'---\nname: probe\n{header}\n---\nbody\n')
    assert meta[header.split(':', 1)[0]] == expected
    assert body == 'body'


# LLM: 兼容修复不能把多行内容解释成对象标签、合并键、别名或容器列表。
# 函数用途: 保留明确引号外危险YAML语法与超深容器的拒绝行为。
@pytest.mark.parametrize('header', [
    'description: !!python/object:x {}', 'description: &anchor value',
    'description: *anchor', 'tags: [review, null]', 'tags: [[review]]',
    'tags: [{key: value}]', 'description: "unterminated\ntags: review',
    'description: First line\n  unexpected: mapping',
    'description: First line\n  - unexpected',
    'description: ["a", !!python/object:x {}]',
    'description: first\n  \tsecond', 'description: "first\n  \tsecond"',
    'nested: ' + '[' * (MAX_FRONTMATTER_DEPTH + 1) + 'x' + ']' * (MAX_FRONTMATTER_DEPTH + 1),
])
def test_frontmatter_security_stays_fail_closed(header):
    with pytest.raises(ValueError):
        split_skill_frontmatter(f'---\nname: probe\n{header}\n---\nbody\n')


# LLM: 只记录标量起点定位次数，不依赖墙钟阈值；大量普通引号仍是文本，不能逐引号重扫整行。
# 函数用途: 避免新引号识别让有界长头部产生平方级前缀扫描。
def test_plain_quotes_do_not_repeat_mapping_prefix_scan(monkeypatch):
    offset, calls = skills_module._yaml_value_offset, []

    # LLM: 原定位实现保持，观测次数不改变解析语义。
    # 函数用途: 检查一行是否被反复定位。
    def observed(line):
        calls.append(1)
        return offset(line)

    monkeypatch.setattr(skills_module, '_yaml_value_offset', observed)
    line = "description: use" + " 'em" * 100
    assert skills_module._yaml_uncomment(line) == line
    assert len(calls) <= 2


# LLM: 三类标准根和插件provider全部绑定临时目录，不读取真实Owner或安装资源。
# 函数用途: 构造支持策略切换的真实快照服务。
def _catalog(tmp_path):
    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    paths = SimpleNamespace(owner_home_dir=home, shared_skills_dir=tmp_path / 'shared',
                            shared_builtin_dir=tmp_path / 'builtin')
    policy = SimpleNamespace(owner_id='one', skills_enabled=True,
        enabled_skill_sources=('owner', 'workspace', 'shared', 'builtin'),
        disabled_skills=(), enabled_shared_skills=())
    service = service_module.SkillsService(home_paths=paths, workspace_root=workspace,
        policy_provider=lambda: policy, plugin_roots=lambda: [(tmp_path / 'plugin', 'plugin:probe')])
    return service, policy


# LLM: 只生成一个规范Skill，正文修改与stat变动均来自真实文件系统。
# 函数用途: 准备缓存验证使用的临时技能。
def _write_skill(root, name='demo', description='first'):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {description}\n---\nbody\n', encoding='utf-8')
    return path


# LLM: 仅统计实际manifest构建，空快照也要命中；force和显式清缓存仍必须重新读盘。
# 函数用途: 验证未变快照不会反复构建manifest，显式刷新不被吞掉。
@pytest.mark.parametrize('populated', [False, True])
def test_snapshot_reuses_manifest_until_forced(tmp_path, monkeypatch, populated):
    service, _ = _catalog(tmp_path)
    if populated:
        _write_skill(tmp_path / 'owner/skills')
    build, calls = service_module._build_manifest, []

    # LLM: 委托原实现以保留真实读取和guard，只记录次数。
    # 函数用途: 区分快照命中和重复全量读取。
    def observed(roots):
        calls.append(roots)
        return build(roots)

    monkeypatch.setattr(service_module, '_build_manifest', observed)
    initial = service.snapshot_for()
    assert service.snapshot_for() is initial
    assert service.snapshot_for() is initial
    assert len(calls) == 1
    assert service.snapshot_for(force_reload=True) is not initial
    assert len(calls) == 2
    service.clear_cache()
    service.snapshot_for()
    assert len(calls) == 3


# LLM: 源集合变化来自实际新增/改写/重命名/删除；旧快照保持不可变，新快照权限仍由当前策略计算。
# 函数用途: 覆盖每类根的即时失效以及policy变化不能被stat缓存掩盖。
@pytest.mark.parametrize('root', ['workspace/.agents/skills', 'workspace/.agent/skills',
    'owner/workspace/.agents/skills', 'owner/skills', 'owner/skills/learned',
    'owner/learned', 'shared', 'builtin', 'plugin'])
def test_snapshot_stat_cache_invalidates_changes(tmp_path, root):
    service, policy = _catalog(tmp_path)
    empty = service.snapshot_for()
    path = _write_skill(tmp_path / root)
    added = service.snapshot_for()
    assert added is not empty and added.resolve('demo') is not None
    assert empty.resolve('demo') is None
    _write_skill(tmp_path / root, description='updated')
    modified = service.snapshot_for()
    assert modified.resolve('demo').description == 'updated'
    assert modified.fingerprint != added.fingerprint
    policy.disabled_skills = ('demo',)
    disabled = service.snapshot_for()
    assert disabled.resolve('demo') is None and disabled.resolve('demo', enabled_only=False) is not None
    policy.disabled_skills = ()
    assert service.snapshot_for().resolve('demo') is not None
    renamed = path.parent.with_name('renamed')
    path.parent.rename(renamed)
    moved = service.snapshot_for()
    assert moved.resolve('demo').path == str(renamed / 'SKILL.md')
    (renamed / 'SKILL.md').unlink()
    assert service.snapshot_for().resolve('demo') is None
    assert added.resolve('demo') is not None


# LLM: 目录或guard资源变化时即使SKILL正文hash相同也须重建；不把stat命中和公开正文指纹混为一谈。
# 函数用途: 防止manifest已失效却仍返回未重做安全检查的旧快照。
def test_directory_stat_change_rebuilds_snapshot(tmp_path):
    service, _ = _catalog(tmp_path)
    path = _write_skill(tmp_path / 'owner/skills')
    first = service.snapshot_for()
    (path.parent / 'references').mkdir()
    second = service.snapshot_for()
    assert second is not first
    assert second.entries == first.entries
    assert service.snapshot_for() is second


# LLM: 不同工作区与策略Owner必须隔离；切换来源/推荐上限仍走原指纹，受限快照不因缓存扩大。
# 函数用途: 验证stat缓存不替代既有授权与工作区选择。
def test_snapshot_cache_preserves_scope_and_restrictions(tmp_path):
    service, policy = _catalog(tmp_path)
    _write_skill(tmp_path / 'owner/skills', 'keep')
    _write_skill(tmp_path / 'workspace/.agents/skills', 'private')
    first = service.snapshot_for()
    other = service.snapshot_for(tmp_path / 'another')
    assert other.resolve('private') is None and first.resolve('private') is not None
    assert {entry.name for entry in first.restricted(['keep']).enabled_entries()} == {'keep'}
    policy.owner_id = 'two'
    assert service.snapshot_for().owner_id == 'two'
    policy.enabled_skill_sources = ('builtin',)
    assert not service.snapshot_for().enabled_entries()
