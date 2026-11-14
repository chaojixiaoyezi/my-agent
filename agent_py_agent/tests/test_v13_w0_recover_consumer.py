"""W0参考消费者：Node真实fetch经Gateway读取规范运行事件，不手写响应。"""
import re
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.space_commands import business_client_for_agent
from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime
from agent_py_agent.tests.test_rework_r8_business_wire_contract import (
    ALICE,
    APP,
    MAIN,
    run_node,
    thin_client,
)
from agent_py_agent.tests.test_rework_r8_business_wire_contract import (
    gateway as gateway,
)


# LLM: 身份经真实客户端创建，任务与事件由原canonical生产者建立；不需要触发调度唤醒。
# 函数用途: 准备同owner两个空间及其运行，并保存目标过滤器的合法扫描续点。
def seed_case(gateway):
    api = business_client_for_agent(thin_client(gateway, ALICE))
    agents, spaces = [], []
    for name in ('target', 'other'):
        created = api.call('spaces.create', {'name': name})
        assert created['ok'] is True, created
        space = created['data']['space_id']
        member = api.call('background_agents.create', {'space_id': space, 'name': name})
        assert member['ok'] is True, member
        agents.append(member['data']['agent']['agent_id'])
        spaces.append(space)
    [services] = [value for value in gateway.server._business_services.values()
        if value.owner_id.endswith('/business-alice')]
    runs = []
    for space, agent in zip(spaces, agents):
        task = seed_task(services, space, agent)
        runtime, run = seed_observed_runtime(services, task)
        runs.append(run)
    page = api.call('events.list', {'filter': {'space_id': spaces[0]}})
    assert page['ok'] is True, page
    return SimpleNamespace(api=api, runtime=runtime, runs=runs, spaces=spaces, cursor=page['data']['next_cursor'])


# LLM: 此处只准备规范观察来源，不测试任务派发或绕过其发布锁；没有替换HTTP响应。
# 函数用途: 通过原SessionTaskStore创建具有冻结业务上下文的观察测试任务。
def seed_task(services, space, agent):
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    store = services.host.conversation_store
    thread = store.threads.require(agent)
    controller = thread.metadata['background_agent']['managed_by_thread_id']
    return store.session_tasks.create(SessionTaskDraft(controller, agent, '规范观察测试', dedupe_key='w0-' + agent),
        business_context_intent=BusinessContextIntent(services.owner_id, space, None, None),
        business_context_factory=lambda task_id: BusinessExecutionContext(
            services.owner_id, space, None, task_id, 1, None, (), (), 'business-context.v1'))


# LLM: 对准同一canonical运行关系，不绕过观察投影构造事件。
# 函数用途: 追加原runtime事件并返回稳定event_id。
def append(case, index):
    run = case.runs[index]
    return case.runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
        agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])['event_id']


# LLM: 六种场景都经过真实Node、HTTP Handler及原事件投影，不替换响应。
# 函数用途: 覆盖过滤空页、连续空页、当前尾部、有界预算、过期恢复和断线补拉。
@pytest.mark.parametrize('scenario,nonmatching', [
    ('filtered', 50), ('consecutive', 100), ('tail', 0), ('budget', 500),
    ('reset_expired', 0), ('disconnect', 0),
])
def test_round_trip_matrix(gateway, scenario, nonmatching):
    case = seed_case(gateway)
    for _ in range(nonmatching):
        append(case, 1)
    expected = None if scenario == 'tail' else append(case, 0)
    run_node(gateway, r'''
 const served = [], updates = [];
 const network = new AbortController(), stop = new AbortController();
 const api = new ui.BusinessClient(async (path, options) => {
   const response = await live(path, path.endsWith('/stream') && SCENARIO === 'disconnect'
     ? {...options, signal: network.signal} : options);
   if (path === '/client/business') {
     const body = JSON.parse(options.body), result = await response.clone().json();
     assert.deepEqual(Object.keys(body).sort(), ['action', 'params', 'request_id']);
     assert.equal(body.action, 'events.list'); assert.ok(!Object.hasOwn(body.params, 'limit'));
     assert.equal(response.status, 200); assert.equal(result.state, 'completed'); assert.equal(result.error, null);
     served.push({body, data: result.data});
   }
   return response;
 });
 const observation = new ui.BusinessObservation(api, item => {
   updates.push(item);
   if (SCENARIO === 'disconnect' && item.stream_item === 'snapshot') network.abort();
   if (SCENARIO === 'reset_expired' && item.stream_item === 'recovered') stop.abort();
 }, {space_id: SPACE});
 assert.equal(observation.maxCatchUpPages, 10);
 observation.cursor = CURSOR;
 if (SCENARIO === 'reset_expired') {
   observation.cursor = 'invalid-opaque-cursor'; observation.seen.add('stale-before-reset');
   await observation.run(stop.signal);
   assert.ok(updates.some(item => item.stream_item === 'cursor_expired' && item.payload.reset_required));
   assert.ok(!observation.seen.has('stale-before-reset'));
   assert.ok(!Object.hasOwn(served[0].body.params, 'cursor'), '过期后真正从无游标请求恢复');
 } else if (SCENARIO === 'disconnect') {
   await observation.run();
   assert.ok(updates.some(item => item.stream_item === 'disconnected'));
   assert.equal(served[0].body.params.cursor, CURSOR);
 } else {
   await observation.recover();
   assert.equal(served.length, 10, '不以过滤空页或持续非空游标猜测尾部');
   assert.equal(served[0].body.params.cursor, CURSOR);
 }
 assert.equal(observation.coverage, 'partial', '非空续点耗尽预算必须保持partial');
 assert.equal(observation.cursor, served.at(-1).data.next_cursor);
 for (let page = 1; page < served.length; page += 1)
   assert.equal(served[page].body.params.cursor, served[page - 1].data.next_cursor);
 const ids = () => updates.filter(item => item.stream_item === 'event').map(item => item.event_id);
 if (SCENARIO === 'filtered' || SCENARIO === 'consecutive') {
   const emptyCount = SCENARIO === 'filtered' ? 1 : 2;
   for (let page = 0; page < emptyCount; page += 1) assert.deepEqual(served[page].data.events, []);
   assert.deepEqual(served[emptyCount].data.events.map(item => item.event_id), [EXPECTED]);
   assert.deepEqual(ids(), [EXPECTED]);
 } else if (SCENARIO === 'tail') {
   assert.ok(served.every(page => page.data.events.length === 0 && typeof page.data.next_cursor === 'string'));
   assert.deepEqual(ids(), []);
 } else if (SCENARIO === 'budget') {
   assert.ok(served.every(page => page.data.events.length === 0));
   const continuation = observation.cursor;
   await observation.recover();
   assert.equal(served.length, 20); assert.equal(served[10].body.params.cursor, continuation);
   assert.deepEqual(ids(), [EXPECTED]); assert.equal(observation.coverage, 'partial');
 } else assert.ok(ids().includes(EXPECTED));
 assert.equal(new Set(served.map(page => page.body.request_id)).size, served.length);
 ''', SCENARIO=scenario, SPACE=case.spaces[0], CURSOR=case.cursor, EXPECTED=expected)


# LLM: 命名测试固定默认50条扫描前置条件，复用真实矩阵入口。
# 函数用途: 单独提供过滤空页之后匹配事件的验收入口。
def test_filtered_empty_page_then_match_is_found(gateway):
    test_round_trip_matrix(gateway, 'filtered', 50)


# LLM: 不透明游标只允许原样传递及null检测，禁止解释内容或字符串推尾。
# 函数用途: 静态复核recover没有解码、游标等值推断或空事件早退。
def test_consumer_never_decodes_or_compares_cursor():
    source = APP.read_text()
    recover = source.split('async recover(expired = false)', 1)[1].split('async accept(item)', 1)[0]
    assert 'atob' not in recover and 'JSON.parse' not in recover
    assert not re.search(r'(?:this\.cursor|data\.next_cursor)\s*(?:===?|!==?)\s*(?:this\.cursor|data\.next_cursor)', recover)
    assert 'this.cursor = data.next_cursor;' in recover
    assert 'if (data.next_cursor === null) break;' in recover
    assert '!data.events.length' not in recover
    assert "if (data.reset_required) { this.cursor = null; this.coverage = 'expired'; this.seen.clear(); break; }" in recover


# LLM: 原Gateway没有runtime时确实返回unknown，消费者不能伪造complete。
# 函数用途: 验证未知覆盖原样出现在真实页，预算仍以partial披露。
def test_unknown_coverage_round_trip(gateway):
    run_node(gateway, r'''
 const pages = [], observation = new ui.BusinessObservation(new ui.BusinessClient(async (path, options) => {
   const response = await live(path, options); pages.push((await response.clone().json()).data); return response;
 }), () => {});
 await observation.recover();
 assert.equal(pages.length, 10); assert.ok(pages.every(page => page.coverage === 'unknown'));
 assert.equal(observation.coverage, 'partial'); assert.equal(observation.cursor, pages.at(-1).next_cursor);
 ''')


# LLM: 200与202直接来自真实Gateway，不手工构造成功封套。
# 函数用途: 验证成功状态匹配及身份字段只在认证头出现。
def test_reset_and_expired_paths_and_request_body_fields(gateway):
    run_node(gateway, r'''
 const bodies = [];
 const api = new ui.BusinessClient(async (path, options) => {
   bodies.push(JSON.parse(options.body)); return live(path, options);
 });
 const completed = await api.call('spaces.list', {});
 assert.equal(completed.state, 'completed'); assert.equal(completed.error, null);
 const accepted = await api.call('background_agents.messages.send',
   {agent_id: 'wire-agent', text: '原样受理', input_kind: 'message'}, {mutating: true});
 assert.equal(accepted.state, 'accepted'); assert.equal(accepted.error, null);
 for (const body of bodies) for (const field of ['user_id', 'channel', 'metadata']) assert.ok(!Object.hasOwn(body, field));
 assert.deepEqual(Object.keys(bodies[0]).sort(), ['action', 'params', 'request_id']);
 ''', identity=MAIN)
