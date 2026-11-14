"""同一真实Agent的本地业务入口复用只读服务，但不复用调用者权限。"""
from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_mega_background_lifecycle_controls import ready_index


# LLM: 使用真实注册、canonical线程和任务；不注入观察reader或替换cursor验证。
# 函数用途: 为两条本地入口提供隔离的live Agent与101条规范任务。
@pytest.fixture
def live_agent(tmp_path, monkeypatch):
    return _live_agent(tmp_path, monkeypatch, ready=True)


def _live_agent(tmp_path, monkeypatch, *, ready):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    store = agent.conversation_store
    threads = [store.threads.create(owner_id=agent.home_paths.owner_id,
        owner_home=str(agent.home_paths.owner_home_dir)) for _ in range(2)]
    tasks = [store.session_tasks.create(SessionTaskDraft(threads[0].thread_id,
        threads[1].thread_id, f'canonical task {number}')) for number in range(101)]
    assert len({task.task_id for task in tasks}) == 101
    # 此组核稳定快照的服务寿命；先让原有界冷回填追平，不能把跨批revision变化当缓存失效。
    if ready:
        ready_index(store)
    return agent, tasks


@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_cold_index_reports_partial_then_requires_fresh_snapshot(tmp_path, monkeypatch, entrypoint):
    agent, tasks = _live_agent(tmp_path, monkeypatch, ready=False)
    first = call_tasks(agent, entrypoint)
    assert first['error'] is None and first['data']['coverage'] == 'partial', first
    if first['data']['next_cursor']:
        assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    # 回填不固定旧cursor；从真实入口请求新快照，不能把首次partial写成完整结果。
    ready_index(agent.conversation_store)
    result = call_tasks(agent, entrypoint, limit=100)
    assert result['error'] is None and result['data']['coverage'] == 'partial', result
    assert len(result['data']['items']) == 100 and result['data']['next_cursor']
    last = call_tasks(agent, entrypoint, cursor=result['data']['next_cursor'], limit=100)
    identifiers = [row['task_id'] for row in result['data']['items'] + last['data']['items']]
    assert last['error'] is None and last['data']['next_cursor'] is None
    assert last['data']['coverage'] == 'complete'
    assert len(identifiers) == len(set(identifiers)) == 101
    assert set(identifiers) == {task.task_id for task in tasks}


# LLM: 直接调用生产命令入口或原Registry中已注册工具；不手工持有服务来绕过寿命缺陷。
# 函数用途: 从实际入口发出同一冻结tasks.list请求并保留真实业务错误。
def call_tasks(agent, entrypoint, **params):
    request = {'action': 'tasks.list', 'request_id': 'local-pagination', 'params': params}
    if entrypoint == 'command':
        return api.run_business_command(agent, 'space', 'call ' + json.dumps(request))
    outcome = agent.tools.tools['business_tasks_list'].execute(request)
    result = json.loads(outcome.output)
    assert outcome.ok is (result['error'] is None), outcome
    if not outcome.ok and result.get('effect_outcome') == 'none':
        assert outcome.effect_outcome == 'not_started', outcome
    return result


# LLM: 每页均从实际入口重新进入，不接受静默从第一页重启或重复ID。
# 函数用途: 证明两个本地入口可完整遍历原101条任务的50/50/1三页。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_actual_local_entrypoints_traverse_101_canonical_tasks(live_agent, entrypoint):
    agent, tasks = live_agent
    cursor, pages = None, []
    for _ in range(3):
        result = call_tasks(agent, entrypoint, **({'cursor': cursor} if cursor else {}))
        assert result['error'] is None, result
        pages.append(result['data']['items'])
        cursor = result['data']['next_cursor']
        assert (cursor is not None) is (len(pages) < 3)
    identifiers = [row['task_id'] for page in pages for row in page]
    assert [len(page) for page in pages] == [50, 50, 1]
    assert len(identifiers) == len(set(identifiers)) == 101
    assert set(identifiers) == {task.task_id for task in tasks}
    assert result['data']['coverage'] == 'complete'
    print(f'{entrypoint}: page_sizes={[len(page) for page in pages]}, unique_ids={len(set(identifiers))}')


# LLM: 拒绝必须是原业务错误且不披露数据，不接受重启第一页冒充游标恢复。
# 函数用途: 统一检查失效游标及当前权限拒绝的真实返回。
def assert_rejected(result, code='CURSOR_EXPIRED', *, effect='not_started'):
    assert result['error']['code'] == code, result
    assert not result.get('data'), result
    assert result['effect_outcome'] == effect, result


# LLM: 只篡改真实生产签名的字节，不用测试签名器替代原HMAC。
# 函数用途: 证明两个入口仍拒绝伪造游标。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_tampered_cursor_is_rejected(live_agent, entrypoint):
    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    cursor = first['data']['next_cursor']
    value = bytearray(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    value[-1] ^= 1
    tampered = base64.urlsafe_b64encode(value).decode().rstrip('=')
    assert_rejected(call_tasks(agent, entrypoint, cursor=tampered))
    assert call_tasks(agent, entrypoint, cursor=cursor)['error'] is None


# LLM: 只推进观察层时钟；原cursor签发、过期判断及生产入口保持不变。
# 函数用途: 证明一小时过期规则仍在两个入口生效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_expired_cursor_is_rejected(live_agent, entrypoint, monkeypatch):
    from agent_py_agent.agent.conversation import business_observability as observation

    agent, _ = live_agent
    now = [1000.0]
    monkeypatch.setattr(observation, 'time', SimpleNamespace(time=lambda: now[0]))
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    now[0] += 3601
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))


# LLM: 所有任务均queued，两过滤器选择相同记录仍须因scope不同而拒绝；limit不属于scope。
# 函数用途: 保持过滤器绑定，同时证明合法的分页大小变化没有被额外禁止。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_wrong_filter_rejected_but_limit_change_allowed(live_agent, entrypoint):
    agent, _ = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor, filter={'include_completed': False}))
    result = call_tasks(agent, entrypoint, cursor=cursor, limit=1)
    assert result['error'] is None, result
    assert len(result['data']['items']) == 1


# LLM: 相等但不同的配置对象、原库包装对象和只读源组件都不能沿用旧服务签名寿命。
# 函数用途: 分别覆盖配置身份、Store身份、storage身份及canonical reader身份失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
@pytest.mark.parametrize('change', ['config', 'store', 'storage', 'task_source', 'source_root'])
def test_configuration_and_source_identity_changes_invalidate(live_agent, entrypoint, change):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage

    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    _, before = api.local_business_context(agent)
    store = agent.conversation_store
    if change == 'config':
        agent.config = replace(agent.config)
    elif change == 'store':
        agent.conversation_store = ConversationStore(store.storage.root, initialize=False)
    elif change == 'storage':
        store.storage = ConversationStorage(store.storage.root, initialize=False)
    elif change == 'task_source':
        store.session_tasks = SessionTaskStore(store.storage)
    else:
        store.storage.root = store.storage.root / 'changed-source'
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    _, after = api.local_business_context(agent)
    assert after is not before
    fresh = call_tasks(agent, entrypoint)
    assert fresh['error'] is None, fresh
    assert fresh['data']['items'] == first['data']['items']


# LLM: 停用时必须拒绝当前请求；重新启用后新服务不能接受停用前的旧游标。
# 函数用途: 保持实时功能开关并验证同配置对象的enabled变化也会失效缓存。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_live_enabled_state_invalidates_without_restarting_host(live_agent, entrypoint):
    agent, _ = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    agent.config.business_spaces_enabled = False
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor), 'HOST_NOT_READY')
    agent.config.business_spaces_enabled = True
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    assert len(call_tasks(agent, entrypoint)['data']['items']) == 50


# LLM: 观察投影身份由宿主注入且仍使用原签名器；缓存不私自替换端口或给新端口续签旧页。
# 函数用途: 验证显式注入投影的保留、复用及替换失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_injected_projection_is_preserved_and_replacement_invalidates(live_agent, entrypoint):
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService

    agent, _ = live_agent
    projections = [BusinessObservationService(agent.home_paths.owner_home_dir,
        owner_id=agent.home_paths.owner_id, store=agent.conversation_store) for _ in range(2)]
    agent.business_projection = projections[0]
    first = call_tasks(agent, entrypoint)
    _, services = api.local_business_context(agent)
    assert services.observation is projections[0]
    assert call_tasks(agent, entrypoint, cursor=first['data']['next_cursor'])['error'] is None
    agent.business_projection = projections[1]
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    assert api.local_business_context(agent)[1].observation is projections[1]


# LLM: 签名有效不代表源数据未变；不缓存任务列表或弱化原revision检查。
# 函数用途: 证明canonical任务变化仍通过原观察层使旧页失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_canonical_task_revision_change_invalidates_without_replacing_services(live_agent, entrypoint):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    agent, tasks = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    _, services = api.local_business_context(agent)
    changed = agent.conversation_store.session_tasks.advance(tasks[0].task_id,
        SessionTaskUpdate('failed', failure_code='HOST_NOT_READY'))
    assert changed.status == 'failed'
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    assert api.local_business_context(agent)[1] is services


# LLM: 身份仅来自原HostChannelActor专有参数与canonical通道绑定，不改授权函数。
# 函数用途: 为当前模型调用建立可被真实actor解析器复核的会话身份。
def set_current_actor(agent, thread_id, request_id):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.models import ChannelBinding

    binding = ChannelBinding('feishu', 'pagination-chat', 'pagination-person',
        canonical_user_id='pagination-person', thread_id=thread_id)
    agent.conversation_store.threads.update_atomic(thread_id,
        lambda thread: replace(thread, channel_bindings=[binding]))
    actor = HostChannelActor(agent.home_paths.owner_id, thread_id, request_id,
        'feishu', 'pagination-chat', 'pagination-person')
    agent._current_run_params = SimpleNamespace(host_channel_actor=actor, task_id='',
        task_attributes={'conversation_thread_id': thread_id})
    return actor


# LLM: 切换真实owner解析结果后旧游标不能披露原owner任务；测试不用payload字段授予身份。
# 函数用途: 覆盖两入口的foreign-owner游标拒绝及新owner的canonical任务隔离。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_foreign_owner_cursor_is_rejected(live_agent, entrypoint):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    agent, tasks = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    home = resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_user('pagination-bob'))
    agent.home_paths = home_paths_with_owner(agent.home_paths, home)
    store = agent.conversation_store
    source, target = [store.threads.create(owner_id=home.owner_id,
        owner_home=str(home.home_dir)) for _ in range(2)]
    own_task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, 'bob task'))
    set_current_actor(agent, source.thread_id, 'bob-request')
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    fresh = call_tasks(agent, entrypoint)
    assert fresh['error'] is None, fresh
    assert [row['task_id'] for row in fresh['data']['items']] == [own_task.task_id]
    assert not ({task.task_id for task in tasks} & {row['task_id'] for row in fresh['data']['items']})


# LLM: 每次发新上下文/actor/回调；旧context仍保留本次actor，不能借后来的合法actor续权。
# 函数用途: 证明缓存只复用服务，并保留当前canonical通道撤权和逐调用身份检查。
def test_model_tool_uses_fresh_actor_and_current_canonical_authority(live_agent, monkeypatch):
    agent, tasks = live_agent
    source = tasks[0].sender_thread_id
    actor_one = set_current_actor(agent, source, 'first-request')
    contexts = []
    original = api.bind_business_context

    # LLM: 只记录真实issuer返回值，不替换认证、领域调用或任何返回数据。
    # 函数用途: 观察两个生产调用分别发行的上下文对象。
    def record_context(*args, **kwargs):
        context = original(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(api, 'bind_business_context', record_context)
    first = call_tasks(agent, 'model_tool')
    _, services = api.local_business_context(agent)
    actor_two = set_current_actor(agent, source, 'second-request')
    second = call_tasks(agent, 'model_tool', cursor=first['data']['next_cursor'])
    assert second['error'] is None, second
    issued_one, issued_two = contexts[0], contexts[-1]
    assert issued_one is not issued_two
    assert issued_one.authorize is not issued_two.authorize
    assert issued_one.actor == actor_one and issued_two.actor == actor_two
    assert api.local_business_context(agent)[1] is services
    assert services.observation_actor(issued_two) is not services.observation_actor(issued_two)
    with pytest.raises(api.HostAPIError) as denied:
        issued_one.check('tasks.list', {})
    assert denied.value.code == 'CAPABILITY_DENIED'
    agent.conversation_store.threads.update_atomic(source,
        lambda thread: replace(thread, channel_bindings=[]))
    assert_rejected(call_tasks(agent, 'model_tool', cursor=second['data']['next_cursor']), 'CHANNEL_ACTOR_SCOPE_MISMATCH')
    with pytest.raises(api.HostAPIError) as denied:
        issued_two.check('tasks.list', {})
    assert denied.value.code == 'CAPABILITY_DENIED'


# LLM: 查询过程中原绑定被撤销时，末核仍须隐藏数据；复用服务不意味着复用首次授权决定。
# 函数用途: 覆盖已进入观察查询后的实时撤权。
def test_model_tool_rechecks_authority_after_observation(live_agent, monkeypatch):
    agent, tasks = live_agent
    source = tasks[0].sender_thread_id
    set_current_actor(agent, source, 'revoked-during-query')
    _, services = api.local_business_context(agent, model=True)
    original = services.observation.query
    calls = []

    # LLM: 先执行原查询，再通过原线程存储撤销绑定；不伪造业务结果或错误。
    # 函数用途: 在生产末核之前制造真实当前权限变化。
    def revoke_after_query(*args, **kwargs):
        calls.append(args[0])
        result = original(*args, **kwargs)
        agent.conversation_store.threads.update_atomic(source,
            lambda thread: replace(thread, channel_bindings=[]))
        return result

    monkeypatch.setattr(services.observation, 'query', revoke_after_query)
    # 原业务边界已经完成只读查询，JSON必须保留none；外层工具合同另核not_started。
    assert_rejected(call_tasks(agent, 'model_tool'), 'CAPABILITY_DENIED', effect='none')
    assert calls == ['tasks.list']


# LLM: 同canonical home的不同拼写不是新owner；规范化也必须与execute和观察actor的归属核验一致。
# 函数用途: 证明同一home的等价路径保留游标，而真正换home会失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_owner_home_uses_canonical_path_and_invalidates_on_change(live_agent, entrypoint, tmp_path):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    _, services = api.local_business_context(agent)
    home = agent.home_paths.owner_home_dir
    agent.home_paths = replace(agent.home_paths, owner_home_dir=home / '..' / home.name)
    assert api.local_business_context(agent)[1] is services
    assert call_tasks(agent, entrypoint, cursor=first['data']['next_cursor'])['error'] is None
    root = tmp_path / 'another-home'
    agent.home_paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root))
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    assert api.local_business_context(agent)[1] is not services


# LLM: 即便home、owner、config和Store全相同，另一host也不共享签名器或缓存对象。
# 函数用途: 保持独立宿主/CLI生命周期之间没有全局cursor key。
def test_services_and_cursors_do_not_escape_the_live_host(live_agent):
    agent, _ = live_agent
    cursor = call_tasks(agent, 'command')['data']['next_cursor']
    other = SimpleNamespace(home_paths=agent.home_paths, config=agent.config,
        conversation_store=agent.conversation_store)
    first, second = api.local_business_context(agent), api.local_business_context(other)
    assert first[0] is not second[0] and first[1] is not second[1]
    assert first[1].observation is not second[1].observation
    assert_rejected(call_tasks(other, 'command', cursor=cursor))


# LLM: bool为False的显式宿主端口仍是供应对象，不等于缺失，不应静默生成另一服务。
# 类用途: 仅用来验证constructor的None与显式供应值边界。
class FalseyPort(SimpleNamespace):
    # LLM: 本测试值故意为False，不影响其作为供应端口的身份。
    # 函数用途: 暴露不应由or表达式覆盖的合法显式注入。
    def __bool__(self):
        return False


# LLM: 构造器显式参数和宿主注入都保留原对象，不走本地缓存来覆盖供应服务。
# 函数用途: 回归显式spaces/workflow/background/capability/projection供应合同。
def test_supplied_services_are_preserved(live_agent):
    agent, _ = live_agent
    provided = {name: FalseyPort(enabled=True) for name in
        ('spaces', 'workflows', 'background', 'capabilities', 'projection')}
    services = api.BusinessServices(agent, **provided)
    for name, port in provided.items():
        assert getattr(services, name) is port
    agent.business_background = provided['background']
    agent.business_capabilities = provided['capabilities']
    agent.business_projection = provided['projection']
    context, cached = api.local_business_context(agent)
    again, reused = api.local_business_context(agent)
    assert reused is cached and context is not again
    for name in ('background', 'capabilities', 'projection'):
        assert getattr(cached, name) is provided[name]


# LLM: 同一宿主并发首次进入也只发表一个服务实例；锁不保存调用身份或授权结果。
# 函数用途: 覆盖服务缓存的并发发行与每次context唯一性。
def test_concurrent_contexts_share_only_one_service(live_agent):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    agent, _ = live_agent
    agent.config = replace(agent.config)
    barrier = Barrier(4)

    # LLM: 同步启动多个真实本地context发行，不替换服务构造。
    # 函数用途: 同时观察缓存失效后第一次重建的结果。
    def enter(_):
        barrier.wait(timeout=10)
        return api.local_business_context(agent)

    with ThreadPoolExecutor(max_workers=4) as pool:
        issued = list(pool.map(enter, range(4)))
    assert len({id(context) for context, _ in issued}) == 4
    assert len({id(services) for _, services in issued}) == 1
