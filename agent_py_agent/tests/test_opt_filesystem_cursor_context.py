# LLM: 仅用pytest工作区和真实文件工具验证快照及页上下文；不绕过路径策略，不用耗时替代I/O事实。
# 模块用途: 防止隐式扩大忽略范围、游标串线，以及逐命中重复扫描日志前缀。
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import _filesystem_find as find
from agent_py_agent.agent.tooling import _filesystem_helpers as helpers
from agent_py_agent.agent.tooling import _filesystem_search as search
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_read import (
    FileSystemAccessOptions,
    FileSystemTool,
    PathAccessError,
)


# LLM: 使用真实owner墙及存在/缺失/策略拒绝的候选，提示不能替代原路径的结构化权限拒绝。
# 函数用途: 验证工作区前缀猜测只附带经核实的候选，不改变拒绝码或自动读取目标。
@pytest.mark.parametrize('candidate_state', ['exists', 'missing', 'denied'])
def test_path_denial_keeps_code_and_only_verified_hint(tmp_path, candidate_state):
    owner = tmp_path / 'owner'
    workspace = owner / 'project'
    workspace.mkdir(parents=True)
    target = workspace / ('secret' if candidate_state == 'denied' else 'report.txt')
    if candidate_state == 'denied':
        outside = tmp_path / 'outside.txt'
        outside.write_text('not automatically read')
        target.symlink_to(outside)
    elif candidate_state != 'missing':
        target.write_text('not automatically read')
    tool = FileSystemTool(workspace, access_options=FileSystemAccessOptions(
        owner_scope_root=str(owner),
    ))
    if candidate_state == 'denied':
        assert not tool.check_path_access(target).allowed
    wrong = tmp_path / 'wrong-prefix' / 'project' / target.name
    decision = tool.check_path_access(wrong.resolve())
    assert not decision.allowed and decision.code
    with pytest.raises(PathAccessError) as caught:
        tool.resolve_path(wrong)
    assert caught.value.access_code == decision.code
    assert decision.message in str(caught.value)
    assert ('suggested_target=' in str(caught.value)) == (candidate_state == 'exists')
    assert '不是权限缺口' not in str(caught.value)


# LLM: 省略include_ignored与false同义，显式true才可见普通缓存目录；三者均沿原工具权限入口。
# 函数用途: 守住不因零结果自动扩大查询范围的新发现合同。
def test_find_omitted_ignored_is_false(tmp_path):
    hidden = tmp_path / '__pycache__' / 'unique.pyc'
    hidden.parent.mkdir()
    hidden.write_text('sample')
    tool = FindFilesTool(tmp_path, max_matches=20)
    result = tool.execute({'pattern': '*.pyc'})
    assert result.ok and 'unique.pyc' not in result.output
    assert 'unique.pyc' in tool.execute({'pattern': '*.pyc', 'include_ignored': True}).output


# LLM: 显式snapshot冻结有界清单，cursor为不透明同实例凭证，普通可变目录只能声明best_effort。
# 函数用途: 核对嵌套路径的全局定序与无漏重续页，不将到尾等同于原子文件系统快照。
def test_find_snapshot_cursor_stable_pages(tmp_path):
    (tmp_path / 'a').mkdir()
    for name in ('z.py', 'a/first.py', 'b.py', 'a/second.py'):
        (tmp_path / name).write_text('sample')
    tool = FindFilesTool(tmp_path, max_matches=2)
    params = {'pattern': '*.py', 'snapshot': True, 'limit': 2}
    first = tool.execute(params)
    assert first.ok and 'snapshot' in first.result_envelope
    snapshot = first.result_envelope['snapshot']
    assert snapshot['consistency'] == 'best_effort' and snapshot['next_cursor']
    second = tool.execute({**params, 'cursor': snapshot['next_cursor']})
    assert second.ok and second.result_envelope['snapshot']['snapshot_id'] == snapshot['snapshot_id']
    assert not second.result_envelope['snapshot']['next_cursor']
    rows = [line for output in (first.output, second.output) for line in output.splitlines() if line.endswith('.py')]
    assert rows == ['a/first.py', 'a/second.py', 'b.py', 'z.py']


# LLM: 快照源变化必须明确stale，不能静默用旧offset重扫；字段筛选也不能跨cursor改变。
# 函数用途: 对页间嵌套目录新增和跨筛选复用游标守住拒绝边界。
def test_find_snapshot_change_and_filter_mismatch_are_stale(tmp_path):
    (tmp_path / 'sub').mkdir()
    for index in range(4):
        (tmp_path / 'sub' / f'{index}.py').write_text('sample')
    tool = FindFilesTool(tmp_path, max_matches=2)
    params = {'pattern': '*.py', 'snapshot': True, 'limit': 2}
    first = tool.execute(params)
    cursor = (first.result_envelope or {}).get('snapshot', {}).get('next_cursor')
    assert cursor
    mismatched = tool.execute({**params, 'pattern': '*.txt', 'cursor': cursor})
    assert not mismatched.ok and mismatched.error_code == 'STALE_VERSION'
    assert mismatched.result_envelope['snapshot']['consistency'] == 'stale'
    (tmp_path / 'sub' / 'new.py').write_text('sample')
    changed = tool.execute({**params, 'cursor': cursor})
    assert not changed.ok and changed.error_code == 'STALE_VERSION'
    assert changed.result_envelope['snapshot']['consistency'] == 'stale'


# LLM: 强制原Python匹配后备但不模拟命中；只计真实打开，offset前命中和哨兵不应各自补上下文。
# 函数用途: 同一页同一文件最多一次匹配扫描和一次合并窗口读取。
def test_log_context_reads_current_page_once(tmp_path, monkeypatch):
    path = tmp_path / 'events.log'
    path.write_text(''.join(f'match {index}\n' for index in range(40)))
    monkeypatch.setattr(search, '_collect_hits_with_rg', lambda *_args: None)
    original = Path.open
    opens = []

    # LLM: 仅旁听目标文件打开，不改文件对象、解析或工具结果。
    # 函数用途: 把旧的每命中重开前缀扫描变成可复现失败。
    def observed(self, *args, **kwargs):
        if self == path:
            opens.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed)
    result = search.SearchTextTool(tmp_path, max_matches=4).execute(
        {'query': 'match', 'path': 'events.log', 'offset': 12, 'limit': 4, 'context': 2})
    assert result.ok and 'events.log:13:' in result.output
    assert len(opens) <= 2


# LLM: 非正文模式只需命中位置，即使显式context也不得重开文件读取窗口。
# 函数用途: 避免行号模式暗中执行无用的逐命中上下文I/O。
def test_line_numbers_do_not_read_context(tmp_path, monkeypatch):
    path = tmp_path / 'events.log'
    path.write_text('match\n' * 20)
    monkeypatch.setattr(search, '_collect_hits_with_rg', lambda *_args: None)
    original = Path.open
    opens = []

    # LLM: 同前一测试只计量真实文件打开，不代替搜索结果。
    # 函数用途: 为非正文请求测出多余上下文打开次数。
    def observed(self, *args, **kwargs):
        if self == path:
            opens.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed)
    result = search.SearchTextTool(tmp_path, max_matches=4).execute(
        {'query': 'match', 'path': 'events.log', 'limit': 4, 'context': 2, 'output_mode': 'line_numbers'})
    assert result.ok and len(opens) == 1


# LLM: 真实rg只许接收共同策略准入文件；旁听argv与scandir，不模拟rg输出或搜索命中。
# 函数用途: 验证两种原生入口在遍历前排除被拒目录，且不让rg再次自行遍历根目录。
@pytest.mark.parametrize('kind', ['find', 'search'])
def test_rg_only_receives_admitted_files(tmp_path, monkeypatch, kind):
    import subprocess

    denied = tmp_path / 'private'
    denied.mkdir()
    (denied / 'hidden.txt').write_text('needle')
    good = tmp_path / 'visible.txt'
    good.write_text('needle')
    tool = FindFilesTool(tmp_path, 20) if kind == 'find' else search.SearchTextTool(tmp_path, 20)
    original_resolve = tool.resolve_path
    original_scandir = helpers.os.scandir
    original_popen = subprocess.Popen
    calls = []

    # LLM: 保留工具原策略，只额外模拟一个明确拒绝的子树，不扩大任何路径权限。
    # 函数用途: 使准入拒绝可在普通pytest目录复现。
    def resolve(value):
        path = original_resolve(value)
        if path == denied or denied in path.parents:
            raise ValueError('test_path_denied')
        return path

    # LLM: 只观察真实目录下降；拒绝子树不能先被扫描后才从结果删除。
    # 函数用途: 钉住先遍历后过滤的安全缺口。
    def scandir(value):
        assert Path(value) != denied
        return original_scandir(value)

    # LLM: 原样执行真实子进程，不伪造返回值；argv是实际交给rg的扫描范围。
    # 函数用途: 检查原生程序没有收到目录或拒绝文件。
    def popen(args, **kwargs):
        calls.append(args)
        return original_popen(args, **kwargs)

    monkeypatch.setattr(tool, 'resolve_path', resolve)
    monkeypatch.setattr(helpers.os, 'scandir', scandir)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    params = {'pattern': '*.txt'} if kind == 'find' else {'query': 'needle', 'literal': True}
    result = tool.execute(params)
    assert result.ok and 'visible.txt' in result.output and 'hidden.txt' not in result.output
    assert calls and result.result_envelope['page_window']['complete']
    for args in calls:
        targets = args[args.index('--') + (1 if kind == 'find' else 2):]
        assert targets and all(Path(item).is_file() for item in targets)
        assert all(denied not in Path(item).parents for item in targets)


# LLM: 同一真实fixture切换后端，包含gitignore、显式噪声目录及根级双星glob，不能只比较模拟argv。
# 函数用途: 守住搜索后端与共同发现政策的文件集合一致性。
@pytest.mark.parametrize('include_ignored', [False, True])
def test_rg_python_ignore_and_glob_parity(tmp_path, monkeypatch, include_ignored):
    (tmp_path / '.gitignore').write_text('root.txt\n')
    (tmp_path / 'root.txt').write_text('needle')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'child.txt').write_text('needle')
    (tmp_path / 'nested' / 'not.txt.py').write_text('needle')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__' / 'cache.txt').write_text('needle')
    tool = search.SearchTextTool(tmp_path, 30)
    params = {'query': 'needle', 'literal': True, 'file_glob': '**/*.txt',
              'include_ignored': include_ignored, 'output_mode': 'line_numbers'}
    native = tool.execute(params)
    monkeypatch.setattr(search, '_collect_hits_with_rg', lambda *_args: None)
    python = tool.execute(params)
    expected = ['nested/child.txt:1', 'root.txt:1']
    if include_ignored:
        expected.insert(0, '__pycache__/cache.txt:1')
    assert native.ok and python.ok
    assert native.output.splitlines() == python.output.splitlines() == expected
    assert native.result_envelope['page_window']['complete']
    assert python.result_envelope['page_window']['complete']


# LLM: 文本碰巧等于旧标记不构成链接证据；仅实际链接专用异常允许跳过。
# 函数用途: 防止真实I/O错误被字符串分类吞成完整零结果。
def test_discovery_does_not_classify_io_error_by_message(tmp_path, monkeypatch):
    tool = FindFilesTool(tmp_path, 20)

    # LLM: 注入真实OSError而非链接类型，拒绝依赖其任意消息内容裁决完整性。
    # 函数用途: 复现错误文字碰撞。
    def failed(_path):
        raise OSError('discovery_symlink')

    monkeypatch.setattr(helpers, '_discovery_nofollow_stat', failed)
    with pytest.raises(OSError):
        helpers._safe_discovery_path(tool, tmp_path, root=tmp_path,
                                    include_ignored=False, ignored_names=())


# LLM: 实际文件批次触顶是局部观察，不得因零命中给出完整没有文件的结论。
# 函数用途: 核对原生候选字节预算的真实截断披露。
@pytest.mark.parametrize('kind', ['find', 'search'])
def test_native_candidate_budget_is_partial(tmp_path, monkeypatch, kind):
    for index in range(4):
        (tmp_path / f'{index}.txt').write_text('needle')
    monkeypatch.setattr(helpers, '_DISCOVERY_ARG_MAX_BYTES', 1)
    tool = FindFilesTool(tmp_path, 20) if kind == 'find' else search.SearchTextTool(tmp_path, 20)
    params = {'pattern': '*.txt'} if kind == 'find' else {'query': 'needle', 'literal': True}
    result = tool.execute(params)
    assert result.ok and not result.result_envelope['page_window']['complete']
    facts = result.result_envelope['file_search_result' if kind == 'find' else 'search_result']
    assert not facts['scan_complete']
    assert result.result_envelope['page_window'].get('scan_limit_reason',
        facts.get('scan_limit_reason')) == 'candidate_byte_limit'


# LLM: 命中收集与上下文补全之间真改文件，既有命中与哨兵仍保留，但来源必须变stale。
# 函数用途: 防止把新旧两代日志内容拼成完整证据。
def test_context_change_between_collection_and_hydration_is_partial(tmp_path, monkeypatch):
    path = tmp_path / 'events.log'
    path.write_text('needle\n' * 8)
    monkeypatch.setattr(search, '_collect_hits_with_rg', lambda *_args: None)
    original = search._hydrate_search_page

    # LLM: 只在真实两阶段边界写入新版本，不伪造来源指纹或覆盖信封。
    # 函数用途: 稳定复现页上下文来源漂移。
    def changed(tool, page, request):
        path.write_text('new-generation\n' * 8)
        return original(tool, page, request)

    monkeypatch.setattr(search, '_hydrate_search_page', changed)
    result = search.SearchTextTool(tmp_path, 2).execute({'query': 'needle', 'literal': True, 'context': 1})
    assert result.ok and not result.result_envelope['page_window']['complete']
    assert result.result_envelope['page_window']['next_offset'] == 2
    facts = result.result_envelope['search_result']
    assert facts['consistency'] == 'stale' and facts['source_error_code'] == 'FILE_SOURCE_STALE'
    assert 'new-generation' not in result.output


# LLM: 建议器可在两次调用间变化或抛路径异常；可选提示不能吞掉原PathAccessError或使用未核实的新候选。
# 函数用途: 验证提示只格式化已经核实的同一目标，建议失败时保留原拒绝。
@pytest.mark.parametrize('failed', [False, True])
def test_path_hint_does_not_recompute_or_mask_denial(tmp_path, monkeypatch, failed):
    from agent_py_agent.agent.tooling import _filesystem_read as reads

    owner = tmp_path / 'owner'
    owner.mkdir()
    target = owner / 'report.txt'
    target.write_text('sample')
    tool = FileSystemTool(owner, access_options=FileSystemAccessOptions(owner_scope_root=str(owner)))
    wrong = tmp_path / 'outside' / 'report.txt'
    expected = tool.check_path_access(wrong)
    calls = []

    # LLM: 第一次给真实允许目标，之后给不存在目标；失败分支模拟建议生成阶段的真实OSError。
    # 函数用途: 暴露重复推导与可选建议异常覆盖原裁决。
    def suggest(*_args):
        calls.append(True)
        if failed:
            raise OSError('suggestion unavailable')
        return str(target if len(calls) == 1 else owner / 'missing.txt')

    monkeypatch.setattr(reads, 'suggest_workspace_typo_target', suggest)
    with pytest.raises(PathAccessError) as caught:
        tool.resolve_path(wrong)
    assert caught.value.access_code == expected.code
    assert expected.message in str(caught.value)
    assert len(calls) == 1
    assert ('suggested_target=' in str(caught.value)) is not failed
    assert 'missing.txt' not in str(caught.value)
