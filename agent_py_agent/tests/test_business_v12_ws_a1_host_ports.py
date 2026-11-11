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
    build_default_governance_ports,
    build_default_task_business_context,
    build_default_workflow_agent_steps,
    is_business_host_ports_enabled,
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

    # BIZ-004: 治理端口未配置时，policies.* 动作不公布
    assert 'policies.apply' not in supported_after
    assert 'policies.apply' not in allowed_after

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


# LLM: 验证宿主端口装配时序解耦与网关生产默认装配机制。
# 函数用途: 验证 wire_business_host_ports 在 conversation_store 注入前调用仍可动态绑定任务，
# 以及网关基于 config.business_host_ports_enabled 自动装配端口且支持 explicit False 覆盖。
def test_business_host_ports_wiring_order_and_production_default(native, monkeypatch):  # noqa: F811
    server, agent = native

    # 1. 验证 wire_business_host_ports 在 conversation_store 注入前调用：
    # 模拟在 conversation_store 挂载前即完成宿主接线
    unattached_host = SimpleNamespace(
        home_paths=agent.home_paths,
        config=agent.config,
    )
    wire_business_host_ports(unattached_host)
    assert unattached_host._business_host_ports_wired is True
    assert unattached_host.business_workflow_reference_reader is not None

    # 后续挂载 conversation_store 并注入活跃任务
    flow_id = 'dynamic-wf-' + uuid.uuid4().hex[:8]
    dyn_stask = SimpleNamespace(
        task_id='task-delayed-01',
        status='running',
        metadata={'workflow_ref': flow_id}
    )
    unattached_host.conversation_store = SimpleNamespace(
        session_tasks=SimpleNamespace(
            list_page=lambda limit=100: {'items': [dyn_stask], 'next_cursor': None}
        )
    )

    # 读者在调用时动态识别后来挂载的 session_tasks，阻断项归一化为 task
    cov = unattached_host.business_workflow_reference_reader(flow_id)
    assert cov['coverage'] == 'complete'
    assert any(b['kind'] == 'task' and b['ref'] == 'task-delayed-01' for b in cov['blockers'])

    # 2. 验证生产网关 business_api / business_actions 默认装配无需手动设置 host.business_host_ports_enabled = True
    # 宿主本身未显式设置 business_host_ports_enabled (值为 None)
    prod_host = SimpleNamespace(
        home_paths=agent.home_paths,
        config=SimpleNamespace(business_host_ports_enabled=True, business_spaces_enabled=True),
    )
    assert getattr(prod_host, 'business_host_ports_enabled', None) is None
    # 但根据 config.business_host_ports_enabled = True，判定为已启用
    assert is_business_host_ports_enabled(prod_host) is True

    # 验证显式 override: 当 host.business_host_ports_enabled = False 时，即使 config 为 True 也禁用
    disabled_host = SimpleNamespace(
        home_paths=agent.home_paths,
        config=SimpleNamespace(business_host_ports_enabled=True, business_spaces_enabled=True),
        business_host_ports_enabled=False,
    )
    assert is_business_host_ports_enabled(disabled_host) is False

    # 3. 验证通过网关 API / _local_business_services 真实消费，无需显式在 base 上打开关
    from agent_py_agent.agent.gateway_parts import business_api as api
    gateway_host = SimpleNamespace(
        home_paths=agent.home_paths,
        config=SimpleNamespace(business_host_ports_enabled=True, business_spaces_enabled=True),
        conversation_store=agent.conversation_store,
    )
    # 模拟 _local_business_services 自动触发 wire_business_host_ports
    api._local_business_services(gateway_host)
    assert gateway_host._business_host_ports_wired is True
    assert gateway_host.business_capabilities is not None
    assert gateway_host.business_workflow_reference_reader is not None


# LLM: 验证真实能力包 dict 记录、ConversationStore 代理身份与 WorkflowStore 定义引用解析。
# 函数用途: 验证 space_reference_resolver 对 package dict、agent 线程与 workflow 定义的标准合约支持。
def test_space_reference_resolver_canonical_store_contracts(native, monkeypatch):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)
    resolver = build_default_business_space_reference_resolver(agent)

    # 1. package: 支持真实 list_installed_capability_packages 返回的 dict 项与 capability: 前缀
    from agent_py_agent.agent.capability import package_provider

    dict_pkg = {
        'package_id': 'pkg.finance.ledger',
        'name': 'pkg.finance.ledger',
        'package_ref': 'pkg.finance.ledger',
        'version': '1.0.0',
        'digest': 'sha256-digest-abc12345',
        'enabled': True,
    }
    disabled_pkg = {
        'package_id': 'pkg.legacy.tool',
        'name': 'pkg.legacy.tool',
        'package_ref': 'pkg.legacy.tool',
        'digest': 'sha256-legacy-999',
        'enabled': False,
    }
    monkeypatch.setattr(package_provider, 'list_installed_capability_packages', lambda home: [dict_pkg, disabled_pkg])

    # 通过 package_id 匹配
    res1 = resolver('package', 'pkg.finance.ledger', None)
    assert res1 is not None
    assert res1['state'] == 'available'
    assert res1['version_ref'] == 'sha256-digest-abc12345'
    assert res1['package_ref'] == 'pkg.finance.ledger'

    # 通过 capability: 前缀匹配 (emitted by spaces.packages.catalog)
    res2 = resolver('package', 'capability:pkg.finance.ledger', None)
    assert res2 is not None
    assert res2['state'] == 'available'
    assert res2['version_ref'] == 'sha256-digest-abc12345'

    # 禁用包状态投影为 disabled
    res_dis = resolver('package', 'pkg.legacy.tool', None)
    assert res_dis is not None
    assert res_dis['state'] == 'disabled'

    # 2. agent: 真实 store.threads.load 与 list_threads/list_report 查找
    th = agent.conversation_store.threads.create(
        title='finance-agent-alpha',
        owner_id=agent.home_paths.owner_id,
    )
    # 通过精确 thread_id 加载
    res_th = resolver('agent', th.thread_id, None)
    assert res_th is not None
    assert res_th['state'] == 'available'
    assert res_th['kind'] == 'agent'

    # 通过 title / background agent name 查找
    res_title = resolver('agent', 'finance-agent-alpha', None)
    assert res_title is not None
    assert res_title['state'] == 'available'

    # 退役后台代理返回 retired 状态
    from dataclasses import replace

    th_retired = agent.conversation_store.threads.create(
        title='retired-agent-beta',
        owner_id=agent.home_paths.owner_id,
    )
    th_retired_obj = agent.conversation_store.threads.load(th_retired.thread_id)
    th_retired_meta = dict(getattr(th_retired_obj, 'metadata', None) or {})
    th_retired_meta['background_agent'] = {
        'name': 'retired-agent-beta',
        'identity_state': 'retired',
    }
    agent.conversation_store.threads.update_atomic(th_retired.thread_id, lambda t: replace(t, metadata=th_retired_meta))
    res_ret = resolver('agent', th_retired.thread_id, None)
    assert res_ret is not None
    assert res_ret['state'] == 'retired'

    # 3. workflow: 真实 WorkflowStore.get_workflow 支持
    wf_id = 'wf-contract-test-' + uuid.uuid4().hex[:8]
    ok(native, 'workflows.create', {'definition': definition(wf_id)}, idempotency_key='wf-create-contract-001')

    # 裸 workflow id
    res_wf = resolver('workflow', wf_id, None)
    assert res_wf is not None
    assert res_wf['state'] == 'available'
    assert res_wf['kind'] == 'workflow'

    # 带 workflow: 规范前缀
    res_wf_prefix = resolver('workflow', f'workflow:{wf_id}', None)
    assert res_wf_prefix is not None
    assert res_wf_prefix['state'] == 'available'


# LLM: 验证 active_references 阻断 spaces.archive：活跃后台代理、非终态工作流与 catalog.json 订阅。
# 函数用途: 验证 spaces.archive 针对 3 类异构活跃引用的阻断行为。
def test_active_references_blocks_spaces_archive(native):  # noqa: F811
    server, agent = native
    wire_business_host_ports(agent)
    if hasattr(server, '_business_services'):
        server._business_services.clear()

    space = ok(native, 'spaces.create', {'name': 'archive-blockers-space'}, idempotency_key='sp-create-blk-0001')
    sid = space['space_id']

    # 1. 活跃 background_agent 阻断
    from dataclasses import replace

    th_bg = agent.conversation_store.threads.create(
        title='active-bg-worker',
        owner_id=agent.home_paths.owner_id,
    )
    agent.conversation_store.threads.update_atomic(
        th_bg.thread_id,
        lambda t: replace(t, metadata={'background_agent': {'space_id': sid, 'name': 'active-bg-worker', 'identity_state': 'active'}})
    )

    ar_fn = build_default_business_active_references(agent)
    blockers = ar_fn(sid, None)
    assert any(b['kind'] == 'agent' and b['reason_code'] == 'ACTIVE_AGENT' and b['ref'] == th_bg.thread_id for b in blockers)

    # 验证 spaces.archive 被阻断，space 未被归档
    res_arc = ok(native, 'spaces.archive', {'space_id': sid}, idempotency_key='sp-arc-blk-0000001', expected_revision=space['revision'])
    assert res_arc['space']['state'] == 'active'
    assert any(b['kind'] == 'agent' for b in res_arc['blockers'])

    # 将代理标记为 retired 后解除代理阻断
    agent.conversation_store.threads.update_atomic(
        th_bg.thread_id,
        lambda t: replace(t, metadata={'background_agent': {'space_id': sid, 'name': 'active-bg-worker', 'identity_state': 'retired'}})
    )

    # 2. 处于非终态 (如 waiting_for_agent / running) 的 WorkflowRun 阻断
    from agent_py_agent.agent.workflows.run_store import WorkflowRunRecord, WorkflowRunStore
    rstore = WorkflowRunStore(Path(agent.home_paths.owner_home_dir).resolve())
    run_rec = WorkflowRunRecord(
        workflow_run_id='wf-run-wait-agent-01',
        workflow_id='wf-sample',
        owner_id=agent.home_paths.owner_id,
        trigger_kind='manual',
        status='waiting_for_agent',
        started_at=100.0,
        ended_at=0.0,
        duration_ms=0.0,
        business_context={'space_id': sid},
    )
    rstore.save_run(run_rec)

    blockers_wf = ar_fn(sid, None)
    assert any(b['kind'] == 'workflow_run' and b['reason_code'] == 'ACTIVE_WORKFLOW_RUN' and b['ref'] == 'wf-run-wait-agent-01' for b in blockers_wf)

    # 将运行标记为 completed 终态
    run_rec.status = 'completed'
    rstore.save_run(run_rec)

    # 3. catalog.json 活跃订阅阻断
    owner_home = Path(agent.home_paths.owner_home_dir).resolve()
    cat_dir = owner_home / 'data' / 'business'
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat_file = cat_dir / 'catalog.json'
    cat_data = json.loads(cat_file.read_text(encoding='utf-8')) if cat_file.is_file() else {}
    cat_data.setdefault('subscriptions', {})['sub-catalog-001'] = {
        'subscription_id': 'sub-catalog-001',
        'space_id': sid,
        'state': 'active',
        'revision': 1,
    }
    cat_file.write_text(json.dumps(cat_data), encoding='utf-8')

    blockers_sub = ar_fn(sid, None)
    assert any(b['kind'] == 'subscription' and b['reason_code'] == 'ACTIVE_SUBSCRIPTION' and b['ref'] == 'sub-catalog-001' for b in blockers_sub)

    # 移除/暂停活跃订阅后成功归档
    cat_data['subscriptions']['sub-catalog-001']['state'] = 'paused'
    cat_file.write_text(json.dumps(cat_data), encoding='utf-8')

    archive_final = ok(native, 'spaces.archive', {'space_id': sid}, idempotency_key='sp-arc-blk-0000002', expected_revision=space['revision'])
    assert archive_final['space']['state'] == 'archived'
    assert archive_final['blockers'] == []


# LLM: 验证 _start_gateway_http 自动装配业务宿主端口与 G4 工具，且 BIZ-004 治理端口未显式配置时不广播。
# 函数用途: 测试网关进程启动时自动调用 wire_business_host_ports 与 register_business_tools，及治理端口防虚假公布。
def test_gateway_startup_wiring_and_biz_004_governance_guard(native, monkeypatch):  # noqa: F811
    server, agent = native

    # 1. 验证 BIZ-004：wire_business_host_ports 不再自动注入 business_governance，policies.* 动作不公布
    assert getattr(agent, 'business_governance', None) is None
    wire_business_host_ports(agent)
    assert getattr(agent, 'business_governance', None) is None

    if hasattr(server, '_business_services'):
        server._business_services.clear()
    caps = ok(native, 'system.capabilities')
    supported = set(caps['supported_actions'])
    assert 'policies.apply' not in supported
    assert 'policies.get' not in supported
    assert 'policies.preview' not in supported

    # 显式构造默认治理桥接器时，验证 authorize_policy_change 和返回键对齐
    gov = build_default_governance_ports(agent)

    # authorize_policy_change: 无 authorization_ref 返回 False
    assert gov.authorize_policy_change('sp-1', {'workflow_ref': 'wf-1'}, None) is False
    assert gov.authorize_policy_change('sp-1', {'workflow_ref': 'wf-1'}, '') is False
    # 无 business_content_authorizer 返回 False
    assert gov.authorize_policy_change('sp-1', {'workflow_ref': 'wf-1'}, 'auth-ref-1') is False
    # 有 business_content_authorizer 且审批通过返回 True
    agent.business_content_authorizer = lambda facts: True
    assert gov.authorize_policy_change('sp-1', {'workflow_ref': 'wf-1'}, 'auth-ref-1') is True
    agent.business_content_authorizer = lambda facts: False
    assert gov.authorize_policy_change('sp-1', {'workflow_ref': 'wf-1'}, 'auth-ref-1') is False
    agent.business_content_authorizer = None

    # 对齐 get_policy / preview_policy / apply_policy 返回键
    p_get = gov.get_policy('sp-1')
    assert {'policy_ref', 'readable_rules', 'revision'} <= set(p_get.keys())
    p_prev = gov.preview_policy('sp-1', {'workflow_ref': 'wf-1'})
    assert {'diff', 'approval_required', 'affected_runs'} <= set(p_prev.keys())
    p_app = gov.apply_policy('sp-1', {'workflow_ref': 'wf-1'}, authorization_ref='auth-1')
    assert {'policy_ref', 'revision', 'effective_for'} <= set(p_app.keys())

    # 2. 验证 _start_gateway_http 在 business_spaces_enabled=True 时自动接线并注册工具
    from agent_py_agent.cli import gateway_process

    registered_tools = []
    fake_tools = SimpleNamespace(
        register=lambda tool: registered_tools.append(tool),
        get=lambda name: None,
        __contains__=lambda self, name: any(getattr(t, 'model_spec', None) and t.model_spec.name == name for t in registered_tools),
    )
    fake_agent = SimpleNamespace(
        config=SimpleNamespace(
            business_spaces_enabled=True,
            business_host_ports_enabled=True,
            gateway_bind_host='127.0.0.1',
        ),
        home_paths=agent.home_paths,
        conversation_store=agent.conversation_store,
        tools=fake_tools,
    )

    captured_params = []
    monkeypatch.setattr(gateway_process, 'start_http_server', lambda port, paths, params=None: captured_params.append(params) or 'started')

    res = gateway_process._start_gateway_http(fake_agent, SimpleNamespace(state=Path('/tmp/state.json')), 8888)
    assert res == 'started'
    assert fake_agent._business_host_ports_wired is True
    assert fake_agent.business_space_reference_resolver is not None
    # 验证 G4 工具已注册 (spaces_archive, interfaces_*, assets_*)
    tool_names = [getattr(getattr(t, 'model_spec', None), 'name', '') for t in registered_tools]
    assert 'business_spaces_archive' in tool_names
    assert any(name.startswith('business_interfaces_') for name in tool_names)
    assert any(name.startswith('business_assets_') for name in tool_names)

    # 验证 business_spaces_enabled=False 时不装配
    fake_agent_disabled = SimpleNamespace(
        config=SimpleNamespace(
            business_spaces_enabled=False,
            business_host_ports_enabled=True,
            gateway_bind_host='127.0.0.1',
        ),
        home_paths=agent.home_paths,
        conversation_store=agent.conversation_store,
        tools=fake_tools,
    )
    gateway_process._start_gateway_http(fake_agent_disabled, SimpleNamespace(state=Path('/tmp/state.json')), 8888)
    assert getattr(fake_agent_disabled, '_business_host_ports_wired', False) is False
