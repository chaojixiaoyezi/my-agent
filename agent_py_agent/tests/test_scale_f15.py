"""真实业务 HTTP 的注册/点查询不应读取历史任务或全 owner 工具集合。"""
import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
from agent_py_agent.agent.gateway_parts import business_api_v13_readers as readers
from agent_py_agent.tests import test_v13_w0_observation_readers as observation
from agent_py_agent.tests.test_scale_f01 import _fds

background_http = observation.background_http


# LLM: 复制真实 writer 创建的任务协议作为历史输入，修正每条 task 的规范身份。
# 函数用途: 在固定活跃实体数量下增长 1k/10k 条已结束任务。
def _history(store, task, count):
    for index in range(count):
        identifier = f'history-{index:05}'
        context = dict(task.metadata['business_context'], task_id=identifier)
        row = replace(task, task_id=identifier, status='done',
                      metadata={**task.metadata, 'business_context': context})
        store.session_tasks._path(identifier).write_text(json.dumps(row.to_dict()))


# LLM: 计量只包原实现；观察入口仍是实际 HTTP Handler，客户端仍解析真实信封。
# 函数用途: 证明发现与单工具详情不触碰历史任务，关功能也不产生资源快照。
@pytest.mark.parametrize('count', [1000, 10000])
def test_capabilities_and_point_detail_do_no_resource_scan(background_http, monkeypatch, count):
    services, task, runtime, run, operations, digest = observation.seeded(background_http)
    store = services.host.conversation_store
    _history(store, task, count)
    counts = {'tasks': 0, 'snapshots': 0, 'full_join': 0}
    original_list = SessionTaskStore.list_report
    original_snapshot = readers._OwnerReaders.resource_snapshot
    original_join = readers._OwnerReaders.joined

    def listed(self, *args, **kwargs):
        counts['tasks'] += 1
        return original_list(self, *args, **kwargs)

    def snapshot(self):
        counts['snapshots'] += 1
        return original_snapshot(self)

    def joined(self, table, *args, **kwargs):
        # 点查询须把 ID 下推到实际 SQL，而不能先读整个 owner 再筛选。
        if table == 'tool_operations' and not args and not kwargs:
            counts['full_join'] += 1
        return original_join(self, table, *args, **kwargs)

    monkeypatch.setattr(SessionTaskStore, 'list_report', listed)
    monkeypatch.setattr(readers._OwnerReaders, 'resource_snapshot', snapshot)
    monkeypatch.setattr(readers._OwnerReaders, 'joined', joined)
    before = _fds()
    for _index in range(2):
        status, result = observation.background_call(background_http, 'system.capabilities')
        assert status == 200, result
        status, result = observation.background_call(background_http, 'tool_calls.get', {'operation_id': operations[0]})
        assert status == 200 and result['data']['tool_call']['operation_id'] == operations[0], result
    assert counts == {'tasks': 0, 'snapshots': 0, 'full_join': 0}
    assert _fds() == before
    background_http[1].config.business_spaces_enabled = False
    counts.update(tasks=0, snapshots=0, full_join=0)
    status, result = observation.background_call(background_http, 'system.capabilities')
    assert status == 200, result
    assert counts == {'tasks': 0, 'snapshots': 0, 'full_join': 0}


# LLM: AST 不依赖行号；入口不能直接执行历史扫描，负例必须被同一规则识别。
# 函数用途: 防止未来把昂贵快照重新放回 reader 注册期。
def _eager_calls(source):
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'default_observation_readers')
    return [node.lineno for node in ast.walk(function) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == 'resource_snapshot']


def test_registration_ast_rejects_eager_snapshot():
    assert _eager_calls(Path(readers.__file__).read_text()) == []
    assert _eager_calls('def default_observation_readers():\n    source.resource_snapshot()\n') == [2]


# LLM: unrelated坏行必须留在它自己的查询范围内；点详情不得先解析全owner操作。
# 函数用途: 用真实HTTP和损坏的其它operation验证SQL点读隔离。
def test_point_detail_does_not_read_unrelated_outcome(background_http):
    _services, _task, runtime, _run, operations, _digest = observation.seeded(background_http)
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operations[1]))
        conn.commit()
    status, result = observation.background_call(background_http, 'tool_calls.get', {'operation_id': operations[0]})
    assert status == 200, result
    assert result['data']['tool_call']['operation_id'] == operations[0]


# LLM: 廉价注册不能调用正文reader，关功能不能连注册/审批探测都先执行。
# 函数用途: 为真实capabilities入口补充来源正文和关闭状态的调用计量。
def test_registration_reads_no_artifact_body_and_disabled_registers_nothing(background_http, monkeypatch):
    observation.seeded(background_http)
    counts = {'artifacts': 0, 'factory': 0, 'approvals': 0}
    original_artifacts = readers._OwnerReaders.artifacts
    original_factory = readers.default_observation_readers
    original_approvals = readers.native_approval_readers

    # LLM: 包装原产物读口，只计量而不替换原数据。
    # 函数用途: 记录来源正文访问。
    def artifacts(self, params):
        counts['artifacts'] += 1
        return original_artifacts(self, params)

    # LLM: 保留冻结工厂的完整参数，不引入替代观察实现。
    # 函数用途: 记录真实默认注册调用。
    def factory(owner_home, owner_id, store, runtime, *, resource_provider=None):
        counts['factory'] += 1
        return original_factory(owner_home, owner_id, store, runtime, resource_provider=resource_provider)

    # LLM: 只计量原审批来源的廉价探测。
    # 函数用途: 记录审批注册调用。
    def approvals(agent, paths, owner_id, store):
        counts['approvals'] += 1
        return original_approvals(agent, paths, owner_id, store)

    monkeypatch.setattr(readers._OwnerReaders, 'artifacts', artifacts)
    monkeypatch.setattr(readers, 'default_observation_readers', factory)
    monkeypatch.setattr(readers, 'native_approval_readers', approvals)
    status, result = observation.background_call(background_http, 'system.capabilities')
    assert status == 200, result
    assert counts['factory'] > 0 and counts['artifacts'] == 0
    background_http[1].config.business_spaces_enabled = False
    counts.update(artifacts=0, factory=0, approvals=0)
    status, result = observation.background_call(background_http, 'system.capabilities')
    assert status == 200, result
    assert counts == {'artifacts': 0, 'factory': 0, 'approvals': 0}
