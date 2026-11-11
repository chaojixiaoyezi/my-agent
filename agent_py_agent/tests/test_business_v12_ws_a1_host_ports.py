# LLM: WS-A1 业务宿主标准端口全链路回归测试。
# 模块用途: 验证原生空间/工作流引用解析拆分、G4动作就绪、BIZ-003包挂载与执行收据、BIZ-005编号归一化、BIZ-011代理步骤与BIZ-012回收引用覆盖。
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_host_ports import (
    build_default_business_active_references,
    build_default_business_space_reference_resolver,
    build_default_business_workflow_reference_reader,
    build_default_task_business_context,
    build_default_workflow_agent_steps,
    wire_business_host_ports,
)
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_receipts import definition


# LLM: 验证 G4 原生动作就绪：未接线时不可用，接线后 spaces.archive + interfaces.* + assets.* 完整就绪。
# 函数用途: 测试 G4 原生就绪状态与 allowed_actions / system.capabilities 的同步。
def test_g4_native_host_ports_readiness_and_archive(native):  # noqa: F811
    server, agent = native
    space = ok(native, 'spaces.create', {'name': 'g4-space'}, idempotency_key='g4-space-create-00001')
    sid = space['space_id']

    # 1. 未接线时，interfaces.list 不在 allowed_actions 中且调用 503
    supported_before = set(ok(native, 'system.capabilities')['supported_actions'])
    allowed_before = set(ok(native, 'spaces.get', {'space_id': sid})['allowed_actions'])
    assert 'interfaces.list' not in supported_before
    assert 'interfaces.list' not in allowed_before
    status, payload = call(native, 'interfaces.list', {'space_id': sid})
    assert status == 503 and payload['error']['code'] == 'HOST_NOT_READY'

    # 2. 注入 WS-A1 原生业务端口
    wire_business_host_ports(agent)
    # 重置 services 缓存让新注入的端口生效
    if hasattr(server, '_business_services'):
        server._business_services.clear()

    supported_after = set(ok(native, 'system.capabilities')['supported_actions'])
    allowed_after = set(ok(native, 'spaces.get', {'space_id': sid})['allowed_actions'])

    # G4: spaces.archive + interfaces.* (6 actions) + assets.* (7 actions) 全部就绪
    assert 'spaces.archive' in supported_after and 'spaces.archive' in allowed_after
    for iface_action in ('interfaces.list', 'interfaces.get', 'interfaces.bind', 'interfaces.unbind'):
        assert iface_action in supported_after
    for asset_action in ('assets.list', 'assets.get', 'assets.register', 'assets.unlink'):
        assert asset_action in supported_after

    # 3. interfaces.list 可正常调用
    status, payload = call(native, 'interfaces.list', {'space_id': sid})
    assert status == 200, payload
    assert 'items' in payload['data']

    # 4. spaces.archive 在无活跃引用时成功归档
    archive_res = ok(native, 'spaces.archive', {'space_id': sid}, idempotency_key='g4-space-archive-00001', expected_revision=space['revision'])
    assert archive_res['space']['state'] == 'archived'
    assert archive_res['blockers'] == []


# LLM: 验证 BIZ-003 与 V03-06：包引用解析、挂载、以及 spaces.usage.list executed 阶段。
# 函数用途: 测试能力包挂载、预览与运行时执行收据投影。
def test_biz_003_packages_and_v03_executed_phase(native, monkeypatch):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)
    if hasattr(server, '_business_services'):
        server._business_services.clear()

    # 模拟已安装包
    from agent_py_agent.agent.capability import package_provider
    fake_pkg = SimpleNamespace(
        package_id='pkg.data.cleaner',
        package_ref='pkg.data.cleaner',
        name='data cleaner',
        enabled=True,
        digest='sha256-fake123'
    )
    monkeypatch.setattr(package_provider, 'list_installed_capability_packages', lambda home: [fake_pkg])

    space = ok(native, 'spaces.create', {'name': 'pkg-space'}, idempotency_key='pkg-space-create-00001')
    sid = space['space_id']

    # 1. 验证 spaces.packages.preview 原生支持包解析 (BIZ-003)
    preview = ok(native, 'spaces.packages.preview', {'space_id': sid, 'mounts': [{'package_ref': 'pkg.data.cleaner', 'enabled': True}]})
    assert preview['unavailable'] == []
    assert len(preview['diff']) == 1

    # 2. 验证 spaces.packages.attach
    attached = ok(native, 'spaces.packages.attach', {'space_id': sid, 'package_refs': ['pkg.data.cleaner']},
                  idempotency_key='pkg-attach-create-00001', expected_revision=space['revision'])
    assert any(m['package_ref'] == 'pkg.data.cleaner' for m in attached['mounts'])

    # 3. 验证 spaces.usage.list 包含 executed 阶段 (V03-06) 与 mounted_first 证明 (V03-04)
    # 创建模拟 runtime_db tool operation
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    db_path = runtime_db_path(Path(agent.home_paths.owner_home_dir).resolve())
    repo = RuntimeRepository(db_path)
    task_id = 'task-usage-test-' + uuid.uuid4().hex[:8]
    tr = repo.create_task_run(task_id=task_id)
    ar = repo.create_agent_run(task_run_id=tr['task_run_id'])
    att = repo.create_attempt(ar['agent_run_id'])
    repo.create_tool_operation(
        agent_run_id=ar['agent_run_id'],
        attempt_id=att['attempt_id'],
        operation_type='pkg.data.cleaner.clean',
        canonical_scope='workspace',
    )

    # 注入该 task_id 对应的 business_context
    stask = SimpleNamespace(
        task_id=task_id,
        metadata={'business_context': {'space_id': sid, 'owner_id': agent.home_paths.owner_id}}
    )
    if agent.conversation_store is not None and hasattr(agent.conversation_store, 'session_tasks'):
        monkeypatch.setattr(agent.conversation_store.session_tasks, 'load', lambda tid: stask if tid == task_id else None)

    usage = ok(native, 'spaces.usage.list', {'space_id': sid, 'task_id': task_id})
    executed_items = [it for it in usage['items'] if it['phase'] == 'executed']
    assert len(executed_items) >= 1
    assert executed_items[0]['method_ref'] == 'pkg.data.cleaner.clean'
    assert 'runtime_execution_receipt' in executed_items[0]['reason_codes']
    assert 'mounted_first' in executed_items[0]['reason_codes']


# LLM: 验证 BIZ-005：工作流 workflow:<id> 前缀在 W3 适配器边界自动归一化。
# 函数用途: 测试带规范 workflow: 前缀的查询与运行不再触发 INVALID_ARGUMENT。
def test_biz_005_workflow_ref_normalization(native):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)
    if hasattr(server, '_business_services'):
        server._business_services.clear()

    flow_id = 'norm-flow-' + uuid.uuid4().hex[:8]
    ok(native, 'workflows.create', {'definition': definition(flow_id)}, idempotency_key='flow-create-norm-00001')

    # 以裸 flow_id 获取正常
    res_bare = ok(native, 'workflows.get', {'workflow_ref': flow_id})
    assert res_bare['workflow']['workflow_ref'] == flow_id

    # BIZ-005: 以带规范 workflow: 前缀获取，W3 适配层自动截掉前缀，返回正确对象
    res_norm = ok(native, 'workflows.get', {'workflow_ref': f'workflow:{flow_id}'})
    assert res_norm['workflow']['workflow_ref'] == flow_id


# LLM: 验证 BIZ-011 / G5b：原生 SessionTask 代理步骤集成。
# 函数用途: 测试 WorkflowAgentSteps 的 submit、completion 与 cancel 闭环。
def test_biz_011_workflow_agent_steps(native, monkeypatch):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)

    steps = build_default_workflow_agent_steps(agent)
    assert steps is not None

    run = SimpleNamespace(
        owner_id=agent.home_paths.owner_id,
        workflow_run_id='wf-run-agent-001',
        business_context={'space_id': 'sp-agent-01', 'owner_id': agent.home_paths.owner_id}
    )
    step = SimpleNamespace(
        step_id='step-a1',
        escalation_result={'attempt_id': 'att-1'}
    )
    spec = {
        'config': {
            'instruction': 'execute step instruction'
        }
    }

    # 模拟 background_service.submit
    monkeypatch.setattr(steps.background_service, 'submit', lambda req, actor, idempotency_key: {
        'task_id': 'stask-0099', 'state': 'accepted', 'operation_id': 'op-99'
    })

    steps.submit(run, spec, step)
    assert step.escalation_result.get('task_id') == 'stask-0099'
    assert step.escalation_result.get('submission_state') == 'accepted'

    # 模拟任务运行中 completion 为 None
    stask_running = SimpleNamespace(task_id='stask-0099', status='running', result=None)
    monkeypatch.setattr(agent.conversation_store.session_tasks, 'load', lambda tid: stask_running)
    assert steps.completion(run, step) is None

    # 模拟任务完成 completion 结构化回执
    stask_done = SimpleNamespace(task_id='stask-0099', status='done', result={'key': 'value'}, completion_ref='task:stask-0099:done')
    monkeypatch.setattr(agent.conversation_store.session_tasks, 'load', lambda tid: stask_done)
    comp = steps.completion(run, step)
    assert comp is not None
    assert comp['status'] == 'completed'
    assert comp['output'] == {'key': 'value'}

    # 模拟任务取消
    cancelled = steps.cancel(run, step)
    assert cancelled is True
    assert step.escalation_result.get('cancellation_receipt', {}).get('state') == 'completed'


# LLM: 验证 BIZ-012：回收引用读者与 owner_maintenance 完整覆盖与阻断。
# 函数用途: 测试活跃任务/订阅阻断工作流回收，以及无引用时正常回收。
def test_biz_012_workflow_reference_reader_and_recycle(native, monkeypatch):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)
    if hasattr(server, '_business_services'):
        server._business_services.clear()

    flow_id = 'recycle-flow-' + uuid.uuid4().hex[:8]
    ok(native, 'workflows.create', {'definition': definition(flow_id)}, idempotency_key='recycle-create-flow-0001')

    # 1. 当存在活跃 SessionTask 引用此 workflow 时，reader 必须返回 blocker 且 recycle 拒绝
    stask_active = SimpleNamespace(
        task_id='task-blocker-01',
        status='running',
        metadata={'workflow_ref': flow_id}
    )
    monkeypatch.setattr(agent.conversation_store.session_tasks, 'list_page', lambda limit=100: {'items': [stask_active], 'next_cursor': None})

    reader = build_default_business_workflow_reference_reader(agent)
    cov = reader(flow_id)
    assert cov['coverage'] == 'complete'
    assert any(b['kind'] == 'task' and b['ref'] == 'task-blocker-01' for b in cov['blockers'])

    # 2. 当无活跃引用时，reader 返回 blockers 为空，且 workflows.recycle 成功
    monkeypatch.setattr(agent.conversation_store.session_tasks, 'list_page', lambda limit=100: {'items': [], 'next_cursor': None})
    cov_empty = reader(flow_id)
    assert cov_empty['coverage'] == 'complete'
    assert cov_empty['blockers'] == []

    definition_rev = ok(native, 'workflows.get', {'workflow_ref': flow_id})['workflow']['definition_revision']
    recycle_res = ok(native, 'workflows.recycle', {'workflow_ref': flow_id},
                     idempotency_key='recycle-op-execute-0001', expected_revision=definition_rev)
    assert recycle_res['lifecycle'] == 'recycled'

    # 3. 验证 owner_maintenance 能使用读者正常处理
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_workflow_maintenance
    m_res = run_owner_workflow_maintenance(
        agent.home_paths,
        previous={},
        now=datetime.now(timezone.utc),
        safe_to_delete=False
    )
    # 验证 safe_to_delete 门禁正常工作
    assert m_res['executed'] is False and m_res['reason'] == 'retention_not_safe'
