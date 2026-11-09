"""固定窗口的调查数量不能放大真实用量账本读取。"""
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.tests import test_v13_w12_metrics as baseline
from agent_py_agent.tests.test_scale_f01 import _fds

metrics_case = baseline.metrics_case
usage_case = baseline.usage_case
v13_host = baseline.v13_host


# LLM: 所有事件、任务、调查和查询都走真实原入口；计量包装只转发已有读口。
# 函数用途: 对 1/5/10 个调查验证结果、未知费用、重复 task 与单窗口 I/O。
def test_investigation_reads_one_usage_window(usage_case, monkeypatch):
    case = usage_case
    store = case.host.conversation_store
    tasks = [case.task_ids['agent-a']]
    for index in range(1, 10):
        task = store.session_tasks.create(SessionTaskDraft('controller', 'agent-a', '独立用量任务',
            dedupe_key=f'batch-{index}'),
            business_context_intent=BusinessContextIntent(case.owner, case.space, None, None),
            business_context_factory=lambda tid: BusinessExecutionContext(
                case.owner, case.space, None, tid, 1, None, (), (), 'business-context.v1'))
        tasks.append(task.task_id)
        baseline._append_usage(case, 'agent-a', task.task_id, f'batch-event-{index}', 10)
    original = store.model_usage.events_window_report
    values = {'bytes': 0, 'reads': 0}

    def events(thread_id, **window):
        result = original(thread_id, **window)
        values['reads'] += 1
        path = store.model_usage._path(thread_id)
        values['bytes'] += path.stat().st_size if path.exists() else 0
        return result

    monkeypatch.setattr(store.model_usage, 'events_window_report', events)
    observed = []
    for count in (1, 5, 10):
        # 每轮仅变更有限调查集；同一真实事件窗口和三份来源账本保持不变。
        rows = [baseline._investigation(case, f'run-{index:02}', tasks[index]) for index in range(count)]
        case.store.transaction('soc_investigations',
            lambda records: records.clear() or records.update({row['run_id']: row for row in rows}) or {},
            idempotency_key=f'runs-{count}', params_digest=f'runs-{count}')
        values.update(bytes=0, reads=0)
        before = _fds()
        result = baseline._data(case, 'costs.investigations_aggregate', {'space_id': case.space})
        assert result['totals']['input_tokens'] == 123 + (count - 1) * 10
        assert result['investigations'] == count and len(result['top']) == count
        assert result['totals']['cost'] is None and 'cost' in result['unknown']
        assert _fds() == before
        observed.append(dict(values))
    print('usage source bytes/reads for R=1,5,10:', observed)
    assert observed[1] == observed[0] and observed[2] == observed[0]
    assert 0 < observed[0]['reads'] <= 3
    duplicate = baseline._investigation(case, 'duplicate-task', tasks[0])
    baseline._seed(case, 'soc_investigations', [duplicate])
    result = baseline._data(case, 'costs.investigations_aggregate', {'space_id': case.space})
    assert result['totals']['input_tokens'] == 213 and 'duplicate_tasks' in result['unknown']


# LLM: 注册探测与两个真实费用请求共用原reader，计量包装只转发，不制造返回数据。
# 函数用途: 防止dashboard的Agent/space循环和模型次数聚合重复读取usage窗口。
def test_dashboard_reads_each_usage_ledger_once_per_request(usage_case, monkeypatch):
    from collections import Counter

    from agent_py_agent.agent.gateway_parts import business_api_v13_readers as readers

    case = usage_case
    store = case.host.conversation_store
    original_events = store.model_usage.events_window_report
    original_snapshot = readers._OwnerReaders.resource_snapshot
    counts, snapshots = Counter(), []

    # LLM: 精确计量原账本读口，不改变事件或错误。
    # 函数用途: 记录每个thread的事件读取次数。
    def events(thread_id, **window):
        counts[thread_id] += 1
        return original_events(thread_id, **window)

    # LLM: 只观察原快照调用，不替换计数来源。
    # 函数用途: 记录真实资源快照次数。
    def snapshot(self):
        snapshots.append(self.owner)
        return original_snapshot(self)

    monkeypatch.setattr(store.model_usage, 'events_window_report', events)
    monkeypatch.setattr(readers._OwnerReaders, 'resource_snapshot', snapshot)
    registered = readers.default_observation_readers(case.home, case.owner, store, case.services.observation.runtime)
    assert 'usage.batch' in registered
    assert counts == {} and snapshots == []
    for _index in range(2):
        counts.clear()
        snapshots.clear()
        before = _fds()
        result = baseline._data(case, 'costs.dashboard', {'space_id': case.space})
        assert result['totals']['actual']['input_tokens'] == 200
        assert [row['actual']['input_tokens'] for row in result['by_agent']] == [123, 77]
        assert result['by_model'] == [{'model': 'model-a', 'attempt_count': 4}]
        assert result['totals']['actual']['cost'] is None
        assert set(counts) == {'agent-a', 'agent-b', 'agent-other'}
        assert all(count == 1 for count in counts.values())
        assert len(snapshots) == 1
        assert _fds() == before


# LLM: 坏usage用真实坏行表示；降级资源行不能再次扫描，下一请求又必须重读而非跨请求缓存。
# 函数用途: 验证资源快照的失败复用边界和未知用量语义。
def test_usage_failure_reuses_only_current_request_resource_snapshot(usage_case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api_v13_readers as readers

    case = usage_case
    path = case.host.conversation_store.model_usage._path('agent-a')
    with path.open('a', encoding='utf-8') as stream:
        stream.write('{broken\n')
    original = readers._OwnerReaders.resource_snapshot
    seen = []

    # LLM: 计量原快照，不伪造成功资源。
    # 函数用途: 记录当前请求的原快照读取。
    def snapshot(self):
        seen.append(self.owner)
        return original(self)

    monkeypatch.setattr(readers._OwnerReaders, 'resource_snapshot', snapshot)
    for expected in (1, 2):
        status, body = baseline._request(case, 'usage.get')
        assert status == 200, body
        assert body['data']['actual']['input_tokens'] is None
        assert body['data']['actual']['cost'] is None
        assert body['data']['unknown'] == ['usage_source_unavailable']
        assert len(seen) == expected


# LLM: 使用真实BusinessActor活授权，不将同一次或上一次读取数据当成访问许可。
# 函数用途: 验证内部分组逐成员拒绝以及返回前撤权末核。
def test_grouped_usage_keeps_member_and_final_live_authorization(usage_case):
    import pytest

    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError

    case = usage_case
    service = case.services.observation
    params = {'filter': {'since': baseline._at(case, -86400), 'until': baseline._at(case, 0),
        'space_id': case.space}}
    selectors = [{'task_id': case.task_ids['agent-a']}]
    state = {'allowed': True, 'checks': 0, 'revoke_at_final': False}

    # LLM: 只在准确成员事实到达时裁决，入口级owner授权不能替代该裁决。
    # 函数用途: 在第二次请求或本次末核撤销真实成员可见性。
    def authorize(_action, facts):
        if facts.get('thread_id') == 'agent-a':
            state['checks'] += 1
            return state['allowed'] and not (state['revoke_at_final'] and state['checks'] > 2)
        return True

    actor = BusinessActor(case.owner, 'grouped-reader', service, authorize)
    values, events = service.usage_groups(params, selectors, actor=actor)
    assert values[0]['actual']['input_tokens'] == 123 and events is None
    state.update(allowed=False, checks=0)
    with pytest.raises(BusinessError) as denied:
        service.usage_groups(params, selectors, actor=actor)
    assert denied.value.code == 'HOST_NOT_READY'
    state.update(allowed=True, checks=0, revoke_at_final=True)
    with pytest.raises(BusinessError) as revoked:
        service.usage_groups(params, selectors, actor=actor)
    assert revoked.value.code == 'HOST_NOT_READY'
    assert state['checks'] > 2


# LLM: 同一AST规则检测生产与最小反例，防止恢复循环内usage读取。
# 函数用途: 返回两个成本入口中循环体里的用量读取调用。
def _loop_usage_calls(source):
    import ast

    failures = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef) or function.name not in {'costs_dashboard', 'costs_investigations'}:
            continue
        for loop in ast.walk(function):
            if not isinstance(loop, (ast.For, ast.While, ast.AsyncFor)):
                continue
            for node in ast.walk(loop):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'_usage', '_usage_groups'}:
                    failures.append(node.lineno)
    return failures


# LLM: 只防实际N+1模式，不把输入计量fixture或本地分组误判成I/O。
# 函数用途: 为两个费用入口保留正反静态防线。
def test_cost_usage_ast_rejects_reads_in_loops():
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import v13_w12_metrics

    assert _loop_usage_calls(Path(v13_w12_metrics.__file__).read_text()) == []
    assert _loop_usage_calls('def costs_dashboard():\n for row in rows:\n  _usage(call, since, until)\n') == [3]
    assert _loop_usage_calls('def costs_investigations():\n for row in rows:\n  _usage_groups(call, since, until, selectors)\n') == [3]


# LLM: 多个事件引用相同task/thread，原列表和点读包装只计量真实I/O入口，不替换事实。
# 函数用途: 验证一次usage请求内distinct身份复用，下一请求仍重新取规范事实。
def test_usage_identity_reads_are_request_local_and_distinct(usage_case, monkeypatch):
    from collections import Counter

    case = usage_case
    store = case.host.conversation_store
    for index in range(4):
        baseline._append_usage(case, 'agent-a', case.task_ids['agent-a'], f'identity-event-{index}', 10)
    points, lists = Counter(), Counter()
    original_task_load = store.session_tasks.load
    original_thread_require = store.threads.require
    original_task_list = store.session_tasks.list_report
    original_thread_list = store.threads.list_report

    # LLM: 缺失/失败保持原返回或异常，不改变读语义。
    # 函数用途: 计量准确task点读。
    def task_load(task_id):
        points[('task', task_id)] += 1
        return original_task_load(task_id)

    # LLM: 原线程归属校验仍由生产reader执行。
    # 函数用途: 计量准确thread点读。
    def thread_require(thread_id):
        points[('thread', thread_id)] += 1
        return original_thread_require(thread_id)

    # LLM: 不用索引或替身替换原历史任务来源。
    # 函数用途: 计量真实task列表读取。
    def task_list(*, limit=100):
        lists['tasks'] += 1
        return original_task_list(limit=limit)

    # LLM: 所有reader对同一次列表的数据复用不能扩张授权。
    # 函数用途: 计量真实thread列表读取。
    def thread_list(*, limit=100, workspace_dir=None):
        lists['threads'] += 1
        return original_thread_list(limit=limit, workspace_dir=workspace_dir)

    monkeypatch.setattr(store.session_tasks, 'load', task_load)
    monkeypatch.setattr(store.threads, 'require', thread_require)
    monkeypatch.setattr(store.session_tasks, 'list_report', task_list)
    monkeypatch.setattr(store.threads, 'list_report', thread_list)
    filters = {'since': baseline._at(case, -86400), 'until': baseline._at(case, 0), 'space_id': case.space}
    for _request_index in range(2):
        points.clear()
        lists.clear()
        status, body = baseline._request(case, 'usage.get', {'filter': filters})
        assert status == 200, body
        assert body['data']['actual']['input_tokens'] == 240
        assert lists == {'tasks': 1} and lists['threads'] == 0
        assert points and all(count == 1 for count in points.values())


# LLM: 公共聚合DTO无法反推成员归属，内部端口缺失应只禁用成本分组，不修改usage.get合同。
# 函数用途: 验证显式宿主缺分组能力时readiness诚实且公共原读口仍正常。
def test_missing_grouped_source_does_not_claim_costs_ready(usage_case):
    case = usage_case
    case.services.observation.readers.pop('usage.batch')
    assert case.services.ready('usage.get')
    assert not case.services.ready('costs.dashboard')
    assert not case.services.ready('costs.investigations_aggregate')
    filters = {'space_id': case.space, 'since': baseline._at(case, -86400), 'until': baseline._at(case, 0)}
    status, body = baseline._request(case, 'usage.get', {'filter': filters})
    assert status == 200, body
    assert body['data']['actual']['input_tokens'] == 200


# LLM: 使用原线程原子创建及准确业务身份，不替换目录、账本或授权结果。
# 函数用途: 创建没有窗口内事件的合法Agent，可选保留窗口外原生事件。
def _idle_agent(case, *, owner=None, old_event=False):
    from agent_py_agent.agent.conversation.background_team_lifecycle import BACKGROUND_IDENTITY_KEY
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic

    store = case.host.conversation_store
    identifier = 'idle-agent'
    identity = {'agent_id': identifier, 'managed_by_thread_id': 'controller', 'space_id': case.space,
        'project_ref': None, 'identity_state': 'available', 'after_task': 'reuse', 'identity_revision': 1}
    create_thread_atomic(store.threads, ConversationThread(thread_id=identifier, owner_id=owner or case.owner,
        canonical_user_id=owner or case.owner, title=identifier,
        metadata={BACKGROUND_IDENTITY_KEY: identity}, model_profile_id='echo-profile'))
    if old_event:
        task = store.session_tasks.create(SessionTaskDraft('controller', identifier, '窗口外用量', dedupe_key=identifier),
            business_context_intent=BusinessContextIntent(case.owner, case.space, None, None),
            business_context_factory=lambda tid: BusinessExecutionContext(
                case.owner, case.space, None, tid, 1, None, (), (), 'business-context.v1'))
        baseline._append_usage(case, identifier, task.task_id, 'idle-old-event', 300, -100000)
    return identifier


# LLM: 新Agent没有任务或事件；合法空组不能使已有活跃组和原模型次数不可读。
# 函数用途: 验证空闲Agent保留null计量且不破坏成本仪表盘。
def test_dashboard_keeps_active_usage_with_idle_agent(usage_case):
    case = usage_case
    identifier = _idle_agent(case)
    data = baseline._data(case, 'costs.dashboard', {'space_id': case.space})
    assert data['totals']['actual']['input_tokens'] == 200
    assert data['by_model'] == [{'model': 'model-a', 'attempt_count': 4}]
    idle = next(row for row in data['by_agent'] if row['agent_id'] == identifier)
    assert idle['actual']['input_tokens'] is None and idle['actual']['cost'] is None
    assert idle['estimated']['input_tokens'] is None and idle['actual']['coverage'] == 'partial'
    assert data['coverage'] == 'partial'


# LLM: 空间通过真实业务写入口创建，不能用请求中的space_id自证归属。
# 函数用途: 验证合法空空间不会拖垮其它空间的已知用量。
def test_dashboard_keeps_active_usage_with_empty_space(usage_case):
    case = usage_case
    status, created = baseline._request(case, 'spaces.create', {'name': '没有用量的空间'}, write=True)
    assert status == 200, created
    data = baseline._data(case, 'costs.dashboard')
    assert data['totals']['actual']['input_tokens'] == 1100
    empty = next(row for row in data['by_space'] if row['space_id'] == created['data']['space_id'])
    assert empty['actual']['input_tokens'] is None and empty['actual']['cost'] is None
    assert empty['estimated']['input_tokens'] is None and data['coverage'] == 'partial'


# LLM: 原账本真实存在但事件不在窗口内；不得把旧事件混入或制造零token。
# 函数用途: 验证只有窗口外事件的Agent作为合法空组返回。
def test_dashboard_keeps_unknown_for_agent_events_outside_window(usage_case):
    case = usage_case
    identifier = _idle_agent(case, old_event=True)
    data = baseline._data(case, 'costs.dashboard', {'space_id': case.space})
    assert data['totals']['actual']['input_tokens'] == 200
    idle = next(row for row in data['by_agent'] if row['agent_id'] == identifier)
    assert idle['actual']['input_tokens'] is None and idle['actual']['cost'] is None
    assert data['by_model'] == [{'model': 'model-a', 'attempt_count': 4}]


# LLM: 同一空组先成功再撤权，并在独立请求中拒绝；规范事实不是授权缓存。
# 函数用途: 验证空组逐次活授权与返回前末核。
def test_empty_usage_group_keeps_final_live_authorization(usage_case):
    import pytest

    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError

    case = usage_case
    identifier = _idle_agent(case)
    service = case.services.observation
    state = {'checks': 0, 'revoke': False}

    # LLM: 只有规范thread事实抵达时计数，不拿入口filter许可替代成员许可。
    # 函数用途: 在本次返回前撤销准确空Agent范围。
    def authorize(_action, facts):
        if facts.get('thread_id') == identifier:
            state['checks'] += 1
            return not state['revoke'] or state['checks'] < 2
        return True

    actor = BusinessActor(case.owner, 'empty-group-reader', service, authorize)
    params = {'filter': {'since': baseline._at(case, -86400), 'until': baseline._at(case, 0),
        'space_id': case.space}}
    selectors = [{'agent_id': identifier}]
    values, _events = service.usage_groups(params, selectors, actor=actor)
    assert values[0]['actual']['input_tokens'] is None and state['checks'] == 2
    state.update(checks=0, revoke=True)
    with pytest.raises(BusinessError) as denied:
        service.usage_groups(params, selectors, actor=actor)
    assert denied.value.code == 'HOST_NOT_READY' and state['checks'] == 2


# LLM: 缺失或跨owner的规范身份不得借filter成为合法空组，也不能被投影为零费用。
# 函数用途: 为新增空组范围证明保留失败关闭反例。
def test_empty_usage_group_rejects_unwitnessed_and_foreign_scopes(usage_case):
    import pytest

    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError

    case = usage_case
    foreign = _idle_agent(case, owner='local/another')
    service = case.services.observation
    actor = BusinessActor(case.owner, 'empty-group-negative', service, lambda _action, _facts: True)
    params = {'filter': {'since': baseline._at(case, -86400), 'until': baseline._at(case, 0)}}
    for selector in ({'agent_id': 'missing-agent'}, {'agent_id': foreign}, {'space_id': 'missing-space'}):
        with pytest.raises(BusinessError) as denied:
            service.usage_groups(params, [selector], actor=actor)
        assert denied.value.code == 'HOST_NOT_READY'


def test_batch_rechecks_source_revision_after_last_single_pass(usage_case, monkeypatch):
    from agent_py_agent.agent.conversation.usage_sources import UsageSourcePlan

    case = usage_case
    original, appended = UsageSourcePlan.rows, []
    def changed_after_sources(plan):
        yield from original(plan)
        if not appended:
            # 真实末源消费后发表新事件；不能把新revision贴到已经读取的旧窗口上。
            baseline._append_usage(case, 'agent-a', case.task_ids['agent-a'], 'late-batch-revision', 10)
            appended.append(True)
    monkeypatch.setattr(UsageSourcePlan, 'rows', changed_after_sources)
    status, body = baseline._request(case, 'costs.dashboard', {'space_id': case.space})
    assert appended == [True]
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
