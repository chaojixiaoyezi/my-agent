# LLM: 仅验证真实规格生成的规则；本机不能施加Seatbelt时不把字符串断言冒充原生隔离。
# 模块用途: 回归合法Git初始化、owner祖先metadata及解释器链接链的最小读取面。
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt import sandbox


# LLM: 空工作区不应出现按任意.git名字全局拒写的规则；已有控制路径仍由独立精确投影保护。
# 函数用途: 防止Git初始化被隐式全局hooks/config正则截断。
def test_fresh_workspace_does_not_get_implicit_git_regex(tmp_path):
    spec = sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path)
    rules = sandbox._macos_git_write_denies(spec)
    assert not any('(regex ' in line for line in rules)


# LLM: 三种owner布局均用宿主路径事实生成规则，不执行shell或访问真实owner。
# 函数用途: 验证跨owner拒读之后只恢复通向当前home的祖先metadata。
@pytest.mark.parametrize('layout', ['local/main', 'local/users/u', 'providers/p/users/u'])
def test_cross_owner_rules_restore_only_own_ancestor_metadata(tmp_path, layout):
    owners = tmp_path / 'owners'
    home = owners / layout
    home.mkdir(parents=True)
    spec = sandbox.AttemptSandboxSpec(home, home, home, home,
        protected_write_patterns=('^/host-only-control$',), extra_write_roots=(home,),
        implicit_attempt_write_roots=False)
    rules = sandbox._macos_cross_owner_rules(spec)
    deny = next(i for i, rule in enumerate(rules) if rule.startswith('(deny file-read* file-write*'))
    metadata = [(i, rule) for i, rule in enumerate(rules) if rule.startswith('(allow file-read-metadata ')]
    assert metadata and all(i > deny for i, _ in metadata)
    for ancestor in home.parents:
        if ancestor == owners or owners in ancestor.parents:
            literal = '(literal ' + sandbox._seatbelt_string_literal(str(ancestor)) + ')'
            assert any(literal in rule for _, rule in metadata)
    assert all('subpath' not in rule and 'file-read-data' not in rule for _, rule in metadata)
    assert not any(str(owners / 'local/foreign') in rule for _, rule in metadata)


# LLM: 构造真实双层解释器链接，canonical程序位于runtime/bin，另一个bin仅是解析链节点，不授其内容读取。
# 函数用途: 复现解释器跨安装前缀时普通bin祖先缺metadata，且兄弟目录不被放行。
def test_interpreter_link_chain_gets_metadata_without_parent_data_access(tmp_path):
    prefix = tmp_path / 'installation'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'runtime/bin').mkdir(parents=True)
    executable = prefix / 'runtime/bin/python'
    executable.write_text('fixture executable')
    (prefix / 'bin/python').symlink_to('../runtime/bin/python')
    alias = tmp_path / 'python'
    alias.symlink_to(prefix / 'bin/python')
    rules = sandbox._symlink_metadata_rules((alias,))
    literal = '(literal ' + sandbox._seatbelt_string_literal(str(prefix / 'bin')) + ')'
    assert any(literal in rule for rule in rules)
    assert all(rule.startswith('(allow file-read-metadata ') and '(subpath ' not in rule for rule in rules)
    assert not any(str(prefix / 'bin/secret') in rule for rule in rules)
    assert sandbox._symlink_path_nodes(alias) == (alias, prefix / 'bin/python')
