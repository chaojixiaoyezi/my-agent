# LLM: 以临时Owner录制、真实Python执行与假Playwright验证编译合同；不访问真实浏览器或外网。
# 模块用途: 覆盖录制状态机、秘密剥离、URL边界、脚本注入防护与入库。
import ast
import json
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.assets import browser_recorder as recorder
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition


# LLM: 所有URL都是离线样本，不代表外部连通性已验证。
# 函数用途: 建立带查询参数和输入框的停止录制。
def stopped(home, actions=()):
    session = recorder.start_recording_session(home, start_url='https://example.test/search?q=orders', initial_actions=actions)
    assert isinstance(session, dict)
    return recorder.stop_recording_session(home, session['recording_id'])


# LLM: 录制持久状态通过公开接口观察，停止后的追加必须拒绝。
# 函数用途: 检查新增、追加、停止、列表及参数发现。
def test_recording_lifecycle_and_parameter_detection(tmp_path):
    started = recorder.start_recording_session(tmp_path, start_url='https://example.test/', title='订单')
    assert isinstance(started, dict)
    assert started['status'] == 'RECORDING'
    rid = started['recording_id']
    recorder.append_recorded_actions(tmp_path, rid, [{'action_type': 'fill', 'selector': '#keyword', 'value': '手机', 'param_name': 'keyword'}])
    result = recorder.stop_recording_session(tmp_path, rid, final_actions=[{'action_type': 'click', 'selector': '#search'}])
    assert result['status'] == 'STOPPED'
    assert [a['seq'] for a in result['action_trace']] == [1, 2, 3]
    assert [a['action_type'] for a in result['action_trace']] == ['goto', 'fill', 'click']
    assert any(p['param_name'] == 'keyword' and p['default_value'] == '手机' for p in result['detected_parameters'])
    assert recorder.get_recording_session(tmp_path, rid) == result
    assert len(recorder.list_recording_sessions(tmp_path)) == 1
    with pytest.raises(ValueError, match='STATE'):
        recorder.append_recorded_actions(tmp_path, rid, [])


# LLM: 方案拒绝发生在首次持久化前；不得靠URL正文作为可执行代码。
# 函数用途: 覆盖不安全URL及空主机拒绝。
@pytest.mark.parametrize('url', ['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x', 'chrome://settings', 'https:///missing', '//evil.test'])
def test_unsafe_navigation_scheme_rejected(tmp_path, url):
    with pytest.raises(ValueError, match='RECORDER_UNSAFE_URL_SCHEME'):
        recorder.start_recording_session(tmp_path, start_url=url)
    assert not list(tmp_path.rglob('rec_*.json'))


# LLM: 参数重命名或default覆盖不能复活已剥离秘密，编译干跑不返回用户提供的秘密。
# 函数用途: 检查密码、OTP、HTTP头及URL令牌的全链路脱敏。
def test_secrets_stripped_before_storage_and_compile(tmp_path):
    actions = [
        {'action_type': 'fill', 'selector': 'input[type="password"]', 'value': 'fake-pass-42', 'param_name': 'login_password'},
        {'action_type': 'fill', 'selector': '#otp', 'value': '987654', 'param_name': 'otp'},
        {'action_type': 'fill', 'selector': '#auth', 'field_name': 'Authorization', 'value': 'Bearer fake-bearer-42'},
        {'action_type': 'goto', 'url': 'https://example.test/?token=fake-token-42', 'headers': {'Cookie': 'sid=fake-cookie-42'}},
    ]
    session = stopped(tmp_path, actions)
    assert isinstance(session, dict)
    raw = json.dumps(session) + ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    assert all(v not in raw for v in ('fake-pass-42', '987654', 'fake-bearer-42', 'fake-token-42', 'fake-cookie-42'))
    compiled = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'], parameter_overrides={'login_password': {'default_value': 'revived-secret', 'secret': False}})
    assert isinstance(compiled, dict)
    spec = compiled['script_spec']
    assert spec['input_schema']['properties']['login_password']['secret'] is True
    assert 'revived-secret' not in json.dumps(compiled)
    namespace = {}
    exec(compile(spec['entry_source'], '<recording>', 'exec'), namespace)
    with pytest.raises(ValueError, match='SECRET_INPUT_REQUIRED'):
        namespace['main']({'dry_run': True})
    result = namespace['main']({'dry_run': True, 'login_password': 'runtime-secret', 'otp': '456789', 'input_4': 'auth', 'query_5_token': 'runtime-token'})
    assert 'runtime-secret' not in json.dumps(result)


# LLM: 注入片段只作字面值；默认编译是内存预览，干跑不运行selector或改变录制修订。
# 函数用途: 证明编译可解析且动作字符串不能注入Python。
def test_compiler_escapes_injection_and_resolves_parameters(tmp_path):
    malicious = "'); __import__('pathlib').Path('PWNED').touch(); #\n"
    session = stopped(tmp_path, [{'action_type': 'fill', 'selector': malicious, 'value': malicious, 'param_name': 'keyword'},
                                 {'action_type': 'select', 'selector': '#region', 'value': 'CN', 'param_name': 'region'}])
    compiled = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'], generate_workflow=True, generate_skill=True)
    assert isinstance(compiled, dict)
    spec = compiled['script_spec']
    ast.parse(spec['entry_source'])
    namespace = {}
    exec(compile(spec['entry_source'], '<recording>', 'exec'), namespace)
    result = namespace['main']({'dry_run': True, 'keyword': 'new value', 'region': 'US'})
    assert result['ok'] is True and result['steps_completed'] == 0
    assert result['planned_steps'][1]['selector'] == malicious
    assert result['planned_steps'][1]['value'] == 'new value'
    assert result['planned_steps'][2]['value'] == 'US'
    assert 'example.test' in json.dumps(spec['access_contract']['network_targets'])
    WorkflowDefinition.from_any(compiled['workflow_spec'])
    assert compiled['skill_markdown'].startswith('---\n')
    assert recorder.get_recording_session(tmp_path, session['recording_id']) == session


# LLM: 运行时覆盖URL也须通过录制域名白名单，不能仅编译时验证。
# 函数用途: 拒绝参数化引入的跨域与非HTTP导航。
@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://evil.test/', 'http://127.0.0.1/admin', 'https://example.test.evil.test/'])
def test_runtime_url_cannot_escape_recorded_hosts(tmp_path, url):
    session = stopped(tmp_path)
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])
    assert isinstance(result, dict)
    namespace = {}
    exec(result['script_spec']['entry_source'], namespace)
    with pytest.raises(ValueError, match='RECORDER_UNSAFE_URL'):
        namespace['main']({'dry_run': True, 'start_url': url})


# LLM: 保存使用真实现有ScriptStore，workspace文件不写到Owner资产库。
# 函数用途: 验证可保存且可通过正式脚本读口发现。
@pytest.mark.parametrize('scope', ['owner', 'workspace'])
def test_save_to_real_script_store(tmp_path, scope):
    home, project = tmp_path / 'owner', tmp_path / 'project'
    project.mkdir()
    session = stopped(home)
    result = recorder.compile_recording_to_script(home, recording_id=session['recording_id'], script_id='browser_orders', save_to_store=True, scope=scope, workspace_root=project)
    assert isinstance(result, dict)
    root = home if scope == 'owner' else project / '.agent'
    stored = ScriptStore(root).get_script('browser_orders')
    assert stored is not None and stored['code'] == result['script_spec']['entry_source']
    assert stored['io_contract']['outputs'] == ['ok', 'visited_urls', 'extracted_data', 'steps_completed']


# LLM: live动作分派使用假浏览器和显式公网DNS替身，不跳过生产网络门，也不宣称真实浏览器验收。
# 函数用途: 检查Playwright上下文与提取返回值。
def test_generated_live_branch_with_fake_playwright(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    session = stopped(tmp_path, [
        {'action_type': 'fill', 'selector': '#query', 'param_name': 'keyword', 'value': 'old'},
        {'action_type': 'select', 'selector': '#region', 'param_name': 'region', 'value': 'CN'},
        {'action_type': 'press', 'selector': '#query', 'value': 'Enter'},
        {'action_type': 'click', 'selector': '#submit'},
        {'action_type': 'wait_for_selector', 'selector': 'h1'},
        {'action_type': 'extract_text', 'selector': 'h1', 'param_name': 'heading'},
        {'action_type': 'extract_table', 'selector': 'table', 'param_name': 'grid'},
        {'action_type': 'screenshot'},
    ])
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])
    assert isinstance(result, dict)
    calls = []

    # LLM: 只替代外部浏览器依赖，脚本本身仍原样执行。
    # 类用途: 提供最小Playwright行为记录器。
    class Fake:
        # LLM: 记录未知方法调用并返回同一个链式对象。
        # 函数用途: 模拟page、browser与context接口。
        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append((name, args)) or self

        # LLM: Python上下文管理协议，无外部副作用。
        # 函数用途: 进入假Playwright。
        def __enter__(self):
            return SimpleNamespace(chromium=self)

        # LLM: 正常传播异常。
        # 函数用途: 退出假Playwright。
        def __exit__(self, *args):
            return False

        # LLM: 固定测试输出不是外部页面观察。
        # 函数用途: 提供提取文本样本。
        def inner_text(self):
            return 'Orders'

        # LLM: 固定表格只验证静态JS被传入，不执行真实浏览器JS。
        # 函数用途: 提供表格提取样本。
        def evaluate(self, source):
            assert 'querySelectorAll' in source
            return [['订单', '数量']]

        # LLM: 假截图字节不能写任意文件。
        # 函数用途: 提供截图摘要样本。
        def screenshot(self):
            return b'PNG'

    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=Fake))
    namespace = {}
    exec(result['script_spec']['entry_source'], namespace)
    actual = namespace['main']({'keyword': 'new', 'region': 'US', 'query_1_q': 'updated'})
    assert actual['extracted_data']['heading'] == 'Orders'
    assert actual['extracted_data']['grid'] == [['订单', '数量']]
    assert actual['extracted_data']['screenshot_9'] == {'bytes': 3}
    assert actual['steps_completed'] == 9
    assert ('fill', ('new',)) in calls and ('select_option', ('US',)) in calls
    assert actual['visited_urls'] == ['https://example.test/search?q=updated']
    assert any(name == 'route' for name, _ in calls)


# LLM: 持久读口严格约束单叶ID。
# 函数用途: 拒绝录制路径穿越。
def test_recording_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        recorder.get_recording_session(tmp_path, '../escape')


# LLM: 网络声明必须映射原资产AccessTarget，否则正式runner可能错误地认为脚本不联网。
# 函数用途: 验证浏览器网络范围进入权威描述符。
def test_network_contract_is_in_real_descriptor(tmp_path):
    session = stopped(tmp_path)
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])['script_spec']
    assert spec['descriptor']['access_targets']
    assert all('example.test' in target['endpoint'] for target in spec['descriptor']['access_targets'])


# LLM: 用户升级参数为secret时，原URL和普通参数默认值也不能在生成常量中残留。
# 函数用途: 验证秘密升级及URL编码输出的脱敏。
def test_secret_upgrade_removes_all_defaults_and_encoded_outputs(tmp_path):
    session = recorder.start_recording_session(tmp_path, start_url='https://example.test/?q=private-search-value')
    recorder.stop_recording_session(tmp_path, session['recording_id'])
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'],
        parameter_overrides={'query_1_q': {'secret': True}})
    source = result['script_spec']['entry_source']
    assert 'private-search-value' not in source
    namespace = {}
    exec(source, namespace)
    output = namespace['main']({'dry_run': True, 'query_1_q': 'sensitive word'})
    raw = json.dumps(output)
    assert 'sensitive+word' not in raw and 'sensitive%20word' not in raw and 'sensitive word' not in raw


# LLM: 路由检查真实生成代码，公网DNS显式替代以保持离线；跨域或错误端口仍须拒绝。
# 函数用途: 直接验证编译后route回调的允许与拒绝分支。
@pytest.mark.parametrize('url,expected', [('https://evil.test/', 'abort'), ('https://example.test:444/', 'abort'),
                                         ('https://example.test:0/', 'abort'), ('https://example.test/ok', 'continue')])
def test_live_route_origin_guard(tmp_path, monkeypatch, url, expected):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    session = stopped(tmp_path)
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])['script_spec']
    namespace, called = {}, []
    exec(spec['entry_source'], namespace)
    route = SimpleNamespace(request=SimpleNamespace(url=url), abort=lambda: called.append('abort'), continue_=lambda: called.append('continue'))
    namespace['_route'](route)
    assert called == [expected]


# LLM: 整批新动作只有校验后才保存，坏URL不应导致前面的合法click已追加。
# 函数用途: 验证失败追加保持原记录不变。
def test_append_invalid_action_batch_does_not_partially_persist(tmp_path):
    session = recorder.start_recording_session(tmp_path, start_url='https://example.test/')
    with pytest.raises(ValueError):
        recorder.append_recorded_actions(tmp_path, session['recording_id'], [
            {'action_type': 'click', 'selector': '#ok'}, {'action_type': 'goto', 'url': 'file:///etc/passwd'}])
    assert recorder.get_recording_session(tmp_path, session['recording_id']) == session


# LLM: 子进程使用生成文件的真实入口，干跑无需安装Playwright或网络。
# 函数用途: 证明录制脚本能按正式runner的stdin/stdout合同运行。
def test_compiled_script_runs_as_entrypoint(tmp_path):
    session = stopped(tmp_path)
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])['script_spec']
    script = tmp_path / 'recorded.py'
    script.write_text(spec['entry_source'])
    actual = subprocess.run([sys.executable, str(script)], input='{"dry_run":true}', capture_output=True, text=True, check=True)
    assert actual.stdout.strip(), '正式执行入口没有输出JSON'
    assert json.loads(actual.stdout)['steps_completed'] == 0


# LLM: 参数编辑新增凭据形状时也应升级为秘密，不能因原参数普通就把新凭据编进源码。
# 函数用途: 验证编译默认值和动作说明不会旁路秘密剥离。
def test_new_secret_defaults_and_description_copies_are_stripped(tmp_path):
    session = stopped(tmp_path, [
        {'action_type': 'fill', 'selector': '#keyword', 'value': 'old', 'param_name': 'keyword'},
        {'action_type': 'fill', 'selector': 'input[type="password"]', 'value': 'description-secret-42',
         'param_name': 'password', 'description': '登录值 description-secret-42'},
    ])
    assert 'description-secret-42' not in json.dumps(session)
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'],
        parameter_overrides={'keyword': {'default_value': 'Bearer override-secret-42'}})
    assert 'override-secret-42' not in json.dumps(result)
    assert result['script_spec']['input_schema']['properties']['keyword']['secret'] is True


# LLM: 这些都是离线URL样本，拒绝发生在任何录制文件创建之前，不发网络请求。
# 函数用途: 拒绝初始导航中的私网、本机、特殊用途IP及本地域名。
@pytest.mark.parametrize('url', [
    'http://127.0.0.1/admin', 'http://localhost/admin', 'http://169.254.169.254/latest',
    'http://192.168.1.1/admin', 'http://10.0.0.1/', 'http://172.16.0.1/', 'http://0.0.0.0/',
    'http://224.0.0.1/', 'http://240.0.0.1/', 'http://[::1]/', 'http://[::]/', 'http://[fe80::1]/',
    'http://[fc00::1]/', 'http://[ff02::1]/', 'http://[::ffff:127.0.0.1]/',
    'http://LOCALHOST.LOCALDOMAIN/', 'http://app.localhost/', 'http://printer.local/',
    'http://localhost./', 'http://printer.local./',
])
def test_initial_private_navigation_is_rejected_before_storage(tmp_path, url):
    with pytest.raises(ValueError, match='^RECORDER_UNSAFE_URL$'):
        recorder.start_recording_session(tmp_path, start_url=url)
    assert list(tmp_path.iterdir()) == []


# LLM: 旧版录制可已含私网首个goto；真实编译入口必须重验而非只依赖新版start校验。
# 函数用途: 验证历史录制在编译期拒绝内网导航且保留STOPPED原态。
@pytest.mark.parametrize('url', ['http://127.0.0.1/admin', 'http://localhost/admin',
                                  'http://169.254.169.254/latest', 'http://192.168.1.1/admin'])
def test_compile_rejects_legacy_recording_with_private_first_goto(tmp_path, url):
    session = stopped(tmp_path)
    session['start_url'] = url
    session['action_trace'][0]['url'] = url
    recorder._write_record(tmp_path, recorder._RECORDINGS, session, 'recording_id')
    with pytest.raises(ValueError, match='^RECORDER_UNSAFE_URL$'):
        recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'], save_to_store=True)
    assert recorder.get_recording_session(tmp_path, session['recording_id']) == session
    assert not (tmp_path / 'scripts').exists()


# LLM: 公开IP和普通域名不做DNS或外网探测，允许只说明静态URL验证通过。
# 函数用途: 防止私网拒绝误伤公开录制目标。
@pytest.mark.parametrize('url', ['https://example.test/', 'https://8.8.8.8/', 'https://[2606:4700:4700::1111]/'])
def test_public_recording_navigation_remains_allowed(tmp_path, url):
    session = recorder.start_recording_session(tmp_path, start_url=url)
    recorder.stop_recording_session(tmp_path, session['recording_id'])
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])
    assert result['script_spec']['access_contract']['network_targets']


# LLM: 原录制和编译代码真实运行，仅网络DNS与浏览器在各用例显式替代。
# 函数用途: 返回可直接检验的生成脚本命名空间。
def compiled_namespace(home):
    session = stopped(home)
    spec = recorder.compile_recording_to_script(home, recording_id=session['recording_id'])['script_spec']
    namespace = {}
    exec(spec['entry_source'], namespace)
    return namespace


# LLM: 合成密码既可能来自value，也可能只藏在CSS属性中；所有状态文件与生成源码都必须无该值。
# 函数用途: 验证密码selector清理及不同秘密标记都不能被显式False取消。
@pytest.mark.parametrize('selector,flags,value', [
    ('input[type="password"][value="fixture-pass-42"]', {}, 'fixture-pass-42'),
    ("input[type='password'][value='fixture-pass-42']", {}, ''),
    ('input[value="fixture-pass-42"]', {'is_password': True}, 'fixture-pass-42'),
    ('input[value="fixture-pass-42"]', {'secret_key': 'vault-password'}, 'fixture-pass-42'),
    ('input[value="fixture-pass-42"]', {'type': 'password'}, 'fixture-pass-42'),
])
def test_password_selectors_never_persist_secret_literals(tmp_path, selector, flags, value):
    session = stopped(tmp_path, [{'action_type': 'fill', 'selector': selector, 'value': value,
        'is_secret': False, 'secret': False, **flags}])
    action = session['action_trace'][1]
    assert action['is_secret'] is True and action['value'] == ''
    assert '[value=' not in action['selector']
    assert 'fixture-pass-42' not in json.dumps(session)
    assert all('fixture-pass-42' not in p.read_text() for p in tmp_path.rglob('rec_*.json'))
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'])['script_spec']
    assert 'fixture-pass-42' not in spec['entry_source']


# LLM: 预览不进入可创建文件的记录锁，不改变STOPPED/revision；比较目录、权限、mtime和字节。
# 函数用途: 验证默认及显式False的编译均为纯内存预览。
@pytest.mark.parametrize('options', [{}, {'save_to_store': False}])
def test_compile_preview_is_read_only_and_keeps_session(tmp_path, monkeypatch, options):
    session = stopped(tmp_path)

    # LLM: 读造成的atime不作业务写入，显式写入的文件和目录变化均保留。
    # 函数用途: 捕获整个临时录制根快照。
    def snapshot():
        return {str(p.relative_to(tmp_path)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in [tmp_path, *tmp_path.rglob('*')]}

    # LLM: 预览误用写锁或持久化入口应立即失败，不用空写入替身掩盖副作用。
    # 函数用途: 断言纯预览不进入写路径。
    def forbidden(*args, **kwargs):
        pytest.fail('COMPILE_PREVIEW_MUST_NOT_LOCK_OR_WRITE')

    before = snapshot()
    monkeypatch.setattr(recorder, '_record_lock', forbidden)
    monkeypatch.setattr(recorder, '_write_record', forbidden)
    result = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'],
        generate_workflow=True, generate_skill=True, **options)
    assert result['script_spec'] and result['workflow_spec'] and result['skill_markdown']
    assert recorder.get_recording_session(tmp_path, session['recording_id']) == session
    assert snapshot() == before


# LLM: 只有真实保存才改变会话状态；预览之后保存仍只增加一次修订。
# 函数用途: 守住显式保存的原COMPILED状态合同。
def test_compile_save_commits_state_once_after_preview(tmp_path):
    session = stopped(tmp_path)
    recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'], save_to_store=False)
    assert recorder.get_recording_session(tmp_path, session['recording_id']) == session
    recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'], save_to_store=True)
    saved = recorder.get_recording_session(tmp_path, session['recording_id'])
    assert saved['status'] == 'COMPILED' and saved['revision'] == session['revision'] + 1


# LLM: 任一个解析结果不安全都必须拒绝；DNS为确定性替身，不连接这些地址。
# 函数用途: 验证生成脚本在真实请求边界阻止DNS指向私网及IPv6特殊地址。
@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '0.0.0.0',
    '224.0.0.1', '240.0.0.1', '::1', 'fe80::1', 'fc00::1', '::ffff:127.0.0.1'])
def test_generated_runtime_rejects_private_dns_answers(tmp_path, monkeypatch, address):
    namespace = compiled_namespace(tmp_path)
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
               (socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: answers)
    with pytest.raises(ValueError, match='^RECORDER_UNSAFE_URL$'):
        namespace['_check_url']('https://example.test/')


# LLM: 查询错误和空结果都不能降级为放行；干跑仍不做DNS。
# 函数用途: 验证DNS失败关闭和离线干跑隔离。
@pytest.mark.parametrize('failure', ['empty', 'error'])
def test_runtime_dns_fail_closed_but_dry_run_is_offline(tmp_path, monkeypatch, failure):
    namespace = compiled_namespace(tmp_path)

    # LLM: 仅模拟解析错误，不发网络请求。
    # 函数用途: 提供DNS失败或无地址的结果。
    def resolver(*args, **kwargs):
        if failure == 'error':
            raise socket.gaierror('fixture DNS failure')
        return []

    monkeypatch.setattr(socket, 'getaddrinfo', resolver)
    assert namespace['main']({'dry_run': True})['steps_completed'] == 0
    with pytest.raises(ValueError, match='^RECORDER_UNSAFE_URL$'):
        namespace['_check_url']('https://example.test/')


# LLM: 同域名第二次解析改为回环时也须abort，不得复用首次DNS安全结论。
# 函数用途: 验证路由在每次真实请求前重新检查DNS。
def test_route_rechecks_dns_after_rebinding(tmp_path, monkeypatch):
    namespace = compiled_namespace(tmp_path)
    addresses = iter(['8.8.8.8', '127.0.0.1'])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (next(addresses), 443))])
    called = []
    route = SimpleNamespace(request=SimpleNamespace(url='https://example.test/'),
        abort=lambda: called.append('abort'), continue_=lambda: called.append('continue'))
    namespace['_route'](route)
    namespace['_route'](route)
    assert called == ['continue', 'abort']


# LLM: 浏览器被替代，但生成main和启动参数真实运行；不把参数检查当真实Chromium网络验收。
# 函数用途: 验证Chromium固定到已核公网IP且禁用系统代理，缩小检查与连接间重解析窗口。
def test_live_browser_launch_pins_validated_dns(tmp_path, monkeypatch):
    namespace = compiled_namespace(tmp_path)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    manager = MagicMock()
    playwright = manager.__enter__.return_value
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: manager))
    result = namespace['main']({})
    assert result['steps_completed'] == 1
    args = playwright.chromium.launch.call_args.kwargs['args']
    assert any('--host-resolver-rules=' in arg and 'MAP example.test 8.8.8.8' in arg for arg in args)
    assert '--no-proxy-server' in args


# LLM: 升级短默认值q只能清对应查询槽位；路径、查询键、参数名和goto类型不能被子串替换。
# 函数用途: 复现并拒绝秘密升级破坏结构字段的问题。
def test_secret_upgrade_preserves_query_identifiers_and_unrelated_literals(tmp_path):
    session = recorder.start_recording_session(tmp_path, start_url='https://example.test/q?q=q&keep=public')
    recorder.stop_recording_session(tmp_path, session['recording_id'])
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'],
        parameter_overrides={'query_1_q': {'secret': True}})['script_spec']
    properties = spec['input_schema']['properties']
    assert properties['query_1_q'] == {'type': 'string', 'secret': True}
    assert properties['start_url']['default'] == 'https://example.test/q?q=&keep=public'
    namespace = {}
    exec(spec['entry_source'], namespace)
    assert namespace['STEPS'][0]['action_type'] == 'goto'
    assert namespace['STEPS'][0]['query_parameters'][0]['param_name'] == 'query_1_q'
    assert namespace['STEPS'][0]['query_parameters'][0]['query_name'] == 'q'
    result = namespace['main']({'dry_run': True, 'query_1_q': 'runtime-secret'})
    assert result['planned_steps'][0]['action_type'] == 'goto'


# LLM: 普通输入的原值恰为goto时不能清掉另一个导航动作的类型字符串。
# 函数用途: 验证fill参数秘密升级只清理自身值槽。
def test_secret_upgrade_does_not_erase_action_types(tmp_path):
    session = stopped(tmp_path, [{'action_type': 'fill', 'selector': '#keyword', 'value': 'goto', 'param_name': 'keyword'}])
    spec = recorder.compile_recording_to_script(tmp_path, recording_id=session['recording_id'],
        parameter_overrides={'keyword': {'secret': True}})['script_spec']
    namespace = {}
    exec(spec['entry_source'], namespace)
    assert [step['action_type'] for step in namespace['STEPS']] == ['goto', 'fill']
    assert namespace['STEPS'][1]['value'] == ''


# LLM: CSS值属性必须按真实token边界识别，别的属性字符串和伪类文本不是value属性。
# 函数用途: 防止密码selector清理破坏无关字符串、引号与嵌套括号。
@pytest.mark.parametrize('selector', [
    'input[type="password"][data-note="[value=public]"]',
    "input[type='password'][data-note='[value=public]']",
    'input[type="password"]:has-text("[value=public]")',
])
def test_secret_selector_preserves_non_attribute_value_text(selector):
    assert recorder._safe_selector({'selector': selector, 'value': 's3cr3t'}, True) == selector


# LLM: CSS转义只是同一属性的可逆拼法，不得让value和秘密值绕过清理。
# 函数用途: 验证转义属性名及密码type标记也不能落盘。
@pytest.mark.parametrize('selector', [
    r'input[type="password"][v\61 lue="\73 \33 \63 \72 \33 \74 "]',
    r'input[type="\70 assword"][value="fixture-pass-42"]',
])
def test_escaped_password_selector_attributes_are_removed(tmp_path, selector):
    session = stopped(tmp_path, [{'action_type': 'fill', 'selector': selector, 'value': ''}])
    action = session['action_trace'][1]
    assert action['is_secret'] is True
    assert 'value=' not in action['selector'] and r'v\61 lue=' not in action['selector']


# LLM: CDP事件表示浏览器每一跳真正发请求前的暂停点；替身仅承载协议，校验运行原生成代码。
# 函数用途: 验证安装请求暂停门并在重定向和私网DNS时发送阻断而非继续。
@pytest.mark.parametrize('url,address,method', [
    ('https://example.test/final', '8.8.8.8', 'Fetch.continueRequest'),
    ('http://127.0.0.1/private', '8.8.8.8', 'Fetch.failRequest'),
    ('https://example.test/final', '127.0.0.1', 'Fetch.failRequest'),
])
def test_live_request_pause_guard_checks_every_hop(tmp_path, monkeypatch, url, address, method):
    namespace = compiled_namespace(tmp_path)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    manager = MagicMock()
    playwright = manager.__enter__.return_value
    context = playwright.chromium.launch.return_value.new_context.return_value
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: manager))
    namespace['main']({})
    assert context.new_cdp_session.called
    client = context.new_cdp_session.return_value
    assert client.on.call_args.args[0] == 'Fetch.requestPaused'
    assert client.send.call_args.args == ('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
    callback = client.on.call_args.args[1]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))])
    callback({'requestId': 'redirect-request', 'request': {'url': url}})
    assert client.send.call_args.args[0] == method
    assert client.send.call_args.args[1]['requestId'] == 'redirect-request'


# LLM: 注释只可影响属性名识别，不能因其位于方括号内就漏掉value字段。
# 函数用途: 验证注释前缀和命名空间后注释都不能携带密码值。
@pytest.mark.parametrize('attribute', [r'[/**/v\61 lue="canary-P7x"]', r'[*|/**/value="canary-P7x"]'])
def test_password_value_attribute_with_css_comments_is_stripped(attribute):
    selector = 'input[type=password]' + attribute
    assert recorder._safe_selector({'selector': selector, 'value': ''}, True) == 'input[type=password]'


# LLM: CSS字符串中的反斜杠续行不会结束字符串，里面的[value]文本仍不是属性。
# 函数用途: 验证LF/CRLF/FF续行保持整个非敏感属性不变。
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\f'])
def test_selector_string_continuations_preserve_literal_attributes(newline):
    selector = 'input[type=password][data-note="prefix' + '\\' + newline + '[value=public]"]'
    assert recorder._safe_selector({'selector': selector, 'value': ''}, True) == selector


# LLM: 秘密实际出现在其它属性中时，CSS续行解码后也应失败关闭，不允许可逆残留。
# 函数用途: 验证已知密码不能通过非value属性续行逃逸。
def test_selector_secret_copy_with_string_continuation_is_rejected():
    selector = 'input[type=password][data-copy="s3c' + '\\' + '\n' + 'r3t"]'
    with pytest.raises(ValueError, match='RECORDER_SELECTOR_CONTAINS_SECRET'):
        recorder._safe_selector({'selector': selector, 'value': 's3cr3t'}, True)
