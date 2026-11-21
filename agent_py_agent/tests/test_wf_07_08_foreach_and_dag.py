"""WF-07 Public Foreach & WF-08 Parallel Dependency DAG Scheduler 完整测试套件。

涵盖：
- WF-07: validate_foreach公有Schema、concurrency别名、item/batch错误策略、item_id_key稳定提取、
         pack_items稳定身份与preview_foreach_plan纯函数无副作用预览及端到端执行；
- WF-08: should_schedule_dag判定、显式/隐式(inputs_map/condition)依赖推导、圈检测与未知依赖fail-closed拒绝、
         菱形DAG并行执行与输入输出传递、4并发扇出扇入时序证明、中途失败时波次排空与同伴回执不丢失、
         断点续跑成功前缀保留、条件分支跳过与下游级联跳过、准入观察器通知等。
"""

import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec, WorkflowStore
from agent_py_agent.agent.workflows import batching, dag_scheduler
from agent_py_agent.agent.workflows.batching import ForeachInvalid
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord
from agent_py_agent.tests.test_v13_rework_workflow_claim_lifecycle import native  # noqa: F401


def _flow(steps, **options):
    metadata = {}
    if 'execution_mode' in options:
        metadata['execution_mode'] = options['execution_mode']
    if 'max_parallel_steps' in options:
        metadata['max_parallel_steps'] = options['max_parallel_steps']

    desc = {'id': 'dag-flow', 'kind': 'workflow', 'name': 'dag-flow', 'owner_id': 'alice', 'metadata': metadata}
    desc.update(options.get('descriptor', {}))

    normalized_steps = []
    for s in steps:
        s_dict = dict(s)
        s_dict.setdefault('name', s_dict['step_id'])
        normalized_steps.append(s_dict)

    return {'descriptor': desc, 'steps': normalized_steps}


# ==========================================
# WF-07: Public Foreach Tests
# ==========================================


def test_foreach_validate_formal_schema():
    # 1. 正常基础配置
    cfg = batching.validate_foreach({'from': 'inputs.records'})
    assert cfg['from'] == 'inputs.records'
    assert cfg['batch_size'] == 50
    assert cfg['max_parallel'] == 1
    assert cfg['concurrency'] == 1
    assert cfg['on_batch_error'] == 'fail_fast'
    assert cfg['on_item_error'] == 'fail_fast'
    assert cfg['mode'] == 'map'
    assert cfg['item_id_key'] is None

    # 2. 支持上游步骤路径: <step_id>.<path>
    cfg_step = batching.validate_foreach({'from': 'fetch_data.items.rows'})
    assert cfg_step['from'] == 'fetch_data.items.rows'

    # 3. concurrency 别名归一化至 max_parallel
    cfg_conc = batching.validate_foreach({'from': 'inputs.rows', 'concurrency': 4})
    assert cfg_conc['max_parallel'] == 4
    assert cfg_conc['concurrency'] == 4

    # 4. max_parallel 与 concurrency 一致时通过
    cfg_both = batching.validate_foreach({'from': 'inputs.rows', 'max_parallel': 3, 'concurrency': 3})
    assert cfg_both['max_parallel'] == 3

    # 5. max_parallel 与 concurrency 冲突时拒绝
    with pytest.raises(ForeachInvalid, match='max_parallel与concurrency冲突'):
        batching.validate_foreach({'from': 'inputs.rows', 'max_parallel': 2, 'concurrency': 4})

    # 6. item_id_key 字段
    cfg_id = batching.validate_foreach({'from': 'inputs.rows', 'item_id_key': 'sku_id'})
    assert cfg_id['item_id_key'] == 'sku_id'

    with pytest.raises(ForeachInvalid, match='item_id_key必须是非空字符串或null'):
        batching.validate_foreach({'from': 'inputs.rows', 'item_id_key': ''})

    # 7. on_item_error: collect_errors 映射至 continue
    cfg_err = batching.validate_foreach({'from': 'inputs.rows', 'on_item_error': 'collect_errors'})
    assert cfg_err['on_batch_error'] == 'continue'
    assert cfg_err['on_item_error'] == 'collect_errors'

    cfg_err2 = batching.validate_foreach({'from': 'inputs.rows', 'on_item_error': 'continue'})
    assert cfg_err2['on_batch_error'] == 'continue'

    cfg_err3 = batching.validate_foreach({'from': 'inputs.rows', 'on_batch_error': 'collect_errors'})
    assert cfg_err3['on_batch_error'] == 'continue'
    assert cfg_err3['on_item_error'] == 'collect_errors'

    # 8. 非法属性被严格拦截
    with pytest.raises(ForeachInvalid, match='foreach必须是合法配置对象'):
        batching.validate_foreach({'from': 'inputs.rows', 'unsupported_key': True})


def test_foreach_pack_items_stable_ids():
    items = [
        {'id': 'item-alpha', 'val': 10},
        {'sku_id': 'SKU-999', 'val': 20},
        {'val': 30},
        'simple_primitive',
    ]

    # 带 item_id_key 指定
    packed = batching.pack_items(items, batch_size=2, item_id_key='sku_id')
    assert len(packed) == 2
    # 第一批: item-alpha 没有 sku_id，fallback 至 id -> item-alpha; 第二个有 sku_id -> SKU-999
    assert packed[0]['item_ids'] == ['item-alpha', 'SKU-999']
    # 第二批: 第三个无 id 无 sku_id -> item:2; 第四个是字符串 -> item:3
    assert packed[1]['item_ids'] == ['item:2', 'item:3']

    # 幂等性检验
    packed_again = batching.pack_items(items, batch_size=2, item_id_key='sku_id')
    assert packed[0]['item_ids'] == packed_again[0]['item_ids']
    assert packed[1]['item_ids'] == packed_again[1]['item_ids']


def test_preview_foreach_plan_pure_metadata():
    inputs = {'records': [{'id': f'rec-{i}', 'content': f'text-{i}'} for i in range(15)]}
    spec = {
        'from': 'inputs.records',
        'batch_size': 5,
        'concurrency': 3,
        'on_item_error': 'collect_errors',
        'item_id_key': 'id',
    }

    preview = batching.preview_foreach_plan(spec, inputs)
    assert preview['item_count'] == 15
    assert preview['batch_count'] == 3
    assert preview['max_parallel'] == 3
    assert preview['concurrency'] == 3
    assert preview['on_batch_error'] == 'continue'
    assert preview['on_item_error'] == 'collect_errors'
    assert len(preview['batches']) == 3
    assert preview['batches'][0]['item_ids'] == ['rec-0', 'rec-1', 'rec-2', 'rec-3', 'rec-4']
    assert preview['batches'][0]['start'] == 0
    assert preview['batches'][0]['end'] == 5


def test_foreach_execution_with_concurrency_alias_and_collect_errors(tmp_path):
    executed_batches = []

    def analyze(spec, context, skills):
        rows = context.step_inputs['batch']
        executed_batches.append(rows)
        # 故意让第一批中的某批报错
        if rows[0] == 2:
            raise ValueError('simulated item failure in batch 2')
        return {'processed': rows}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow([
        {
            'step_id': 'batch_process',
            'name': 'batch_process',
            'mode': 'llm_skill_analysis',
            'foreach': {
                'from': 'inputs.items',
                'batch_size': 2,
                'concurrency': 2,
                'on_item_error': 'collect_errors',
                'mode': 'aggregate',
                'as': 'batch',
            },
        }
    ])

    run = engine.run_workflow(flow, {'items': [0, 1, 2, 3, 4, 5]})
    # continue / collect_errors 模式下整步成功，跳过失败批
    assert run.status == 'succeeded'
    assert len(executed_batches) == 3
    assert run.final_output == {'processed': [0, 1, 4, 5]}
    assert run.steps[0].batches_summary['completed'] == 2
    assert run.steps[0].batches_summary['failed'] == 1


# ==========================================
# WF-08: Parallel Dependency DAG Scheduler Tests
# ==========================================


def test_dag_should_schedule_dag():
    # 1. 显式 execution_mode == 'dag'
    assert dag_scheduler.should_schedule_dag({'execution_mode': 'dag', 'steps': [{'step_id': 's1'}]})

    # 2. 包含 depends_on
    assert dag_scheduler.should_schedule_dag({'steps': [{'step_id': 's1'}, {'step_id': 's2', 'depends_on': ['s1']}]})

    # 3. 传统顺序流程无 depends_on
    assert not dag_scheduler.should_schedule_dag({'steps': [{'step_id': 's1'}, {'step_id': 's2'}]})
    assert not dag_scheduler.should_schedule_dag({})


def test_dag_validation_cycles_and_unknown_steps():
    # 自环检测
    with pytest.raises(ValueError, match='Self-dependency cycle detected'):
        dag_scheduler.build_dag([{'step_id': 'a', 'depends_on': ['a']}])

    # 两节点环
    with pytest.raises(ValueError, match='Cycle detected'):
        dag_scheduler.build_dag([
            {'step_id': 'a', 'depends_on': ['b']},
            {'step_id': 'b', 'depends_on': ['a']},
        ])

    # 三节点环
    with pytest.raises(ValueError, match='Cycle detected'):
        dag_scheduler.build_dag([
            {'step_id': 'a', 'depends_on': ['c']},
            {'step_id': 'b', 'depends_on': ['a']},
            {'step_id': 'c', 'depends_on': ['b']},
        ])

    # 未知前置依赖
    with pytest.raises(ValueError, match="Unknown upstream dependency 'nonexistent'"):
        dag_scheduler.build_dag([
            {'step_id': 'a', 'depends_on': ['nonexistent']},
        ])

    # 重复 step_id
    with pytest.raises(ValueError, match="Duplicate step_id"):
        dag_scheduler.build_dag([
            {'step_id': 'a'},
            {'step_id': 'a'},
        ])


def test_dag_implicit_dependencies_inputs_map_and_conditions():
    steps = [
        {'step_id': 'root'},
        {
            'step_id': 'child_inputs_map',
            'inputs_map': {
                'data': {'step_id': 'root', 'key': 'payload'},
                'static_input': {'step_id': '$inputs', 'key': 'global_param'},
            },
        },
        {
            'step_id': 'child_condition',
            'condition': {'op': 'eq', 'ref': {'step_id': 'child_inputs_map', 'key': 'status'}, 'value': 'ok'},
        },
    ]

    deps, reverse_deps, topo_order = dag_scheduler.build_dag(steps)
    assert deps['root'] == set()
    assert deps['child_inputs_map'] == {'root'}
    assert deps['child_condition'] == {'child_inputs_map'}
    assert topo_order == ['root', 'child_inputs_map', 'child_condition']


def test_dag_diamond_execution_order_and_inputs_map(tmp_path):
    timeline = []

    def analyze(spec, context, skills):
        sid = spec['step_id']
        timeline.append(f'start:{sid}')
        time.sleep(0.05)
        timeline.append(f'end:{sid}')
        if sid == 'step_a':
            return {'alpha': 100}
        if sid == 'step_b':
            a_val = context.step_inputs.get('from_a', 0)
            return {'beta': a_val + 1}
        if sid == 'step_c':
            a_val = context.step_inputs.get('from_a', 0)
            return {'gamma': a_val + 2}
        if sid == 'step_d':
            b_val = context.step_inputs.get('from_b', 0)
            c_val = context.step_inputs.get('from_c', 0)
            return {'sum': b_val + c_val}
        return {}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'step_a', 'mode': 'llm_skill_analysis'},
            {
                'step_id': 'step_b',
                'mode': 'llm_skill_analysis',
                'depends_on': ['step_a'],
                'inputs_map': {'from_a': {'step_id': 'step_a', 'key': 'alpha'}},
            },
            {
                'step_id': 'step_c',
                'mode': 'llm_skill_analysis',
                'depends_on': ['step_a'],
                'inputs_map': {'from_a': {'step_id': 'step_a', 'key': 'alpha'}},
            },
            {
                'step_id': 'step_d',
                'mode': 'llm_skill_analysis',
                'depends_on': ['step_b', 'step_c'],
                'inputs_map': {
                    'from_b': {'step_id': 'step_b', 'key': 'beta'},
                    'from_c': {'step_id': 'step_c', 'key': 'gamma'},
                },
            },
        ],
        max_parallel_steps=2,
    )

    run = engine.run_workflow(flow)
    assert run.status == 'succeeded'
    assert run.final_output == {'sum': 203}

    # 验证执行顺序拓扑约束：
    # 1. step_a 必须在 step_b 和 step_c 启动前完成
    idx_end_a = timeline.index('end:step_a')
    idx_start_b = timeline.index('start:step_b')
    idx_start_c = timeline.index('start:step_c')
    assert idx_end_a < idx_start_b
    assert idx_end_a < idx_start_c

    # 2. step_d 必须在 step_b 和 step_c 都完成后启动
    idx_end_b = timeline.index('end:step_b')
    idx_end_c = timeline.index('end:step_c')
    idx_start_d = timeline.index('start:step_d')
    assert idx_end_b < idx_start_d
    assert idx_end_c < idx_start_d


def test_dag_parallel_timing_proof(tmp_path):
    import threading

    barrier = threading.Barrier(2, timeout=5.0)

    def analyze(spec, context, skills):
        sid = spec['step_id']
        if sid in ('fetch_1', 'fetch_2'):
            barrier.wait()
        return {'ok': True}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    # 根节点启动 -> 2个并行检索节点 -> 聚合汇聚节点
    flow = _flow(
        [
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {'step_id': 'fetch_1', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'fetch_2', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'join', 'mode': 'llm_skill_analysis', 'depends_on': ['fetch_1', 'fetch_2']},
        ],
        max_parallel_steps=2,
    )

    t0 = time.monotonic()
    run = engine.run_workflow(flow)
    elapsed = time.monotonic() - t0

    assert run.status == 'succeeded'
    # 使用 threading.Barrier(2) 确定性证明两个并行分支同时执行（若顺序执行则超时抛出 BrokenBarrierError）
    assert elapsed < 5.0, f"Parallel timing proof failed: elapsed={elapsed:.3f}s"


def test_dag_mid_wave_failure_draining_no_lost_receipts(tmp_path):
    executed = []

    def analyze(spec, context, skills):
        sid = spec['step_id']
        executed.append(sid)
        if sid == 'failing_node':
            raise ValueError('simulated node explosion')
        if sid == 'sibling_node':
            time.sleep(0.08)
            return {'sibling': 'done'}
        return {'root': 'ok'}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {'step_id': 'failing_node', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'sibling_node', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'downstream', 'mode': 'llm_skill_analysis', 'depends_on': ['failing_node', 'sibling_node']},
        ],
        max_parallel_steps=2,
    )

    run = engine.run_workflow(flow)
    assert run.status == 'failed'
    assert run.failed_step_id == 'failing_node'

    step_map = {s.step_id: s for s in run.steps}
    # root 成功
    assert step_map['root'].status == 'succeeded'
    # failing_node 失败
    assert step_map['failing_node'].status == 'failed'
    # sibling_node 被完整排空执行完毕，未丢失回执且 status == 'succeeded'
    assert step_map['sibling_node'].status == 'succeeded'
    assert step_map['sibling_node'].output_snapshot == {'sibling': 'done'}
    # downstream 未启动
    assert step_map['downstream'].status == 'pending'


def test_dag_resume_preserves_successful_prefix(tmp_path):
    should_fail = [True]
    calls = []

    def analyze(spec, context, skills):
        sid = spec['step_id']
        calls.append(sid)
        if sid == 'step_a':
            return {'data_a': 42}
        if sid == 'step_b':
            if should_fail[0]:
                raise ValueError('temporary failure on B')
            return {'data_b': 84}
        if sid == 'step_c':
            a_val = context.step_inputs.get('a', 0)
            b_val = context.step_inputs.get('b', 0)
            return {'result': a_val + b_val}
        return {}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'step_a', 'mode': 'llm_skill_analysis'},
            {'step_id': 'step_b', 'mode': 'llm_skill_analysis', 'depends_on': ['step_a']},
            {
                'step_id': 'step_c',
                'mode': 'llm_skill_analysis',
                'depends_on': ['step_a', 'step_b'],
                'inputs_map': {
                    'a': {'step_id': 'step_a', 'key': 'data_a'},
                    'b': {'step_id': 'step_b', 'key': 'data_b'},
                },
            },
        ],
        max_parallel_steps=2,
    )

    saved = WorkflowStore(tmp_path).save_workflow(flow)
    run = engine.run_workflow(saved.descriptor.id)
    assert run.status == 'failed'
    assert run.failed_step_id == 'step_b'
    assert calls == ['step_a', 'step_b']

    # 修复故障并恢复运行
    should_fail[0] = False
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded'
    assert resumed.final_output == {'result': 126}
    # 验证 step_a 成功前缀被完整保留，续跑时未重新调用 step_a
    assert calls == ['step_a', 'step_b', 'step_b', 'step_c']


def test_dag_conditional_branch_and_cascading_skip(tmp_path):
    executed = []

    def analyze(spec, context, skills):
        sid = spec['step_id']
        executed.append(sid)
        if sid == 'produce':
            return {'score': 20}
        if sid == 'independent':
            return {'indep': 'ok'}
        return {'val': 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow([
        {'step_id': 'produce', 'mode': 'llm_skill_analysis'},
        {
            'step_id': 'high_score_only',
            'mode': 'llm_skill_analysis',
            'depends_on': ['produce'],
            'condition': {'op': 'gt', 'ref': {'step_id': 'produce', 'key': 'score'}, 'value': 80},
        },
        {
            'step_id': 'cascaded_dependent',
            'mode': 'llm_skill_analysis',
            'depends_on': ['high_score_only'],
        },
        {
            'step_id': 'independent',
            'mode': 'llm_skill_analysis',
        },
    ], max_parallel_steps=2)

    run = engine.run_workflow(flow)
    assert run.status == 'succeeded'

    step_map = {s.step_id: s for s in run.steps}
    assert step_map['produce'].status == 'succeeded'
    assert step_map['independent'].status == 'succeeded'
    # 条件不满足 -> skipped
    assert step_map['high_score_only'].status == 'skipped'
    # 依赖的前置步骤 skipped 且无 fallback -> 级联 skipped
    assert step_map['cascaded_dependent'].status == 'skipped'
    assert set(executed) == {'produce', 'independent'}


def test_dag_admission_observer_callback(tmp_path):
    events = []

    def observer(event):
        events.append(event)

    engine = WorkflowEngine(tmp_path, analysis_handler=lambda s, c, sk: {'ok': True})
    engine.concurrency_admission_observer = observer

    flow = _flow([
        {'step_id': 'step_1', 'mode': 'llm_skill_analysis'},
        {'step_id': 'step_2', 'mode': 'llm_skill_analysis', 'depends_on': ['step_1']},
    ])

    run = engine.run_workflow(flow)
    assert run.status == 'succeeded'
    admitted_steps = [e['step_id'] for e in events if e.get('event') == 'step_admitted']
    assert 'step_1' in admitted_steps
    assert 'step_2' in admitted_steps


def test_dag_cancellation_mid_wave(tmp_path):
    from agent_py_agent.agent.common.cancellation import ToolCancelled

    executed = []

    def analyze(spec, context, skills):
        sid = spec['step_id']
        executed.append(sid)
        if sid == 'cancel_node':
            raise ToolCancelled('simulated user cancel')
        if sid == 'sibling_node':
            time.sleep(0.08)
            return {'sibling': 'ok'}
        return {'root': 'ok'}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {'step_id': 'cancel_node', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'sibling_node', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'downstream', 'mode': 'llm_skill_analysis', 'depends_on': ['cancel_node', 'sibling_node']},
        ],
        max_parallel_steps=2,
    )

    with pytest.raises(ToolCancelled):
        engine.run_workflow(flow)


def test_dag_parallel_workers_lock_isolation_and_atomic_persistence(tmp_path):
    """验证4个并行同伴步骤同时触发重试与水位时，工作线程锁状态严格隔离且 run.json 保持原子持久化零丢失。"""
    import threading

    from agent_py_agent.agent.workflows.run_store import _HELD_LOCKS

    step_attempts = {f"step_{i}": 0 for i in range(1, 5)}
    worker_held_locks = []
    lock = threading.Lock()

    from agent_py_agent.agent.workflows.governance import WorkflowEffectPhase

    def authorize(_run, phase):
        if not isinstance(phase, WorkflowEffectPhase) or phase.step_id in ("root", "join"):
            return
        sid = phase.step_id
        held = getattr(_HELD_LOCKS, "paths", set())
        with lock:
            worker_held_locks.append((sid, set(held)))
            step_attempts[sid] += 1
            first = step_attempts[sid] == 1
        if first:
            exc = RuntimeError("simulated pre-admission I/O error")
            exc.error_code = "TOOL_TIMEOUT"
            exc.effect_outcome = "not_started"
            raise exc

    def analyze(spec, context, skills):
        sid = spec["step_id"]
        if sid in ("root", "join"):
            return {"status": "ok"}
        # 第二次执行：成功并提交单步验证水位
        return {
            "result": f"result_{sid}",
            "watermark": {"cursor": 100 + int(sid.split("_")[1]), "shard": sid},
        }

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze, run_authorizer=authorize)

    retry_policy = {
        "max_attempts": 2,
        "delay_seconds": 0.02,
        "retryable_codes": ["TOOL_TIMEOUT"],
    }

    steps = [{"step_id": "root", "mode": "llm_skill_analysis"}]
    for i in range(1, 5):
        steps.append(
            {
                "step_id": f"step_{i}",
                "mode": "llm_skill_analysis",
                "depends_on": ["root"],
                "retry_policy": retry_policy,
            }
        )
    steps.append(
        {
            "step_id": "join",
            "mode": "llm_skill_analysis",
            "depends_on": [f"step_{i}" for i in range(1, 5)],
        }
    )

    flow = _flow(steps, max_parallel_steps=4)

    run = engine.run_workflow(flow)
    assert run.status == "succeeded"

    # 验证全部工作线程执行期间 _HELD_LOCKS.paths 均为空
    assert len(worker_held_locks) >= 8  # 4 steps × 2 attempts
    for sid, held_set in worker_held_locks:
        assert held_set == set(), f"Worker thread for {sid} leaked held locks: {held_set}"

    # 从磁盘直接重新加载 run.json，验证4个步骤的结果、重试历史与水位全部完整保留且零覆盖丢失
    persisted_run = engine.store.load_run(run.workflow_run_id)
    assert persisted_run.status == "succeeded"

    step_map = {s.step_id: s for s in persisted_run.steps}
    for i in range(1, 5):
        sid = f"step_{i}"
        assert sid in step_map
        st = step_map[sid]
        assert st.status == "succeeded"
        assert st.attempt_count == 2
        history = st.escalation_result.get("retry_history", [])
        assert len(history) == 1
        assert history[0]["attempt"] == 1
        assert history[0]["error_code"] == "TOOL_TIMEOUT"
        assert st.watermark == {"cursor": 100 + i, "shard": sid}


def test_dag_multi_step_with_deterministic_script_id(tmp_path):
    # SA-R2-F1: Verify real ScriptStore deterministic_script (script_id) in a multi-step depends_on DAG workflow
    # preserves script_id through dag_scheduler._pending_step into StepExecutionRecord and persists into store.
    scripts = ScriptStore(tmp_path)
    scripts.save_script(
        AssetDescriptor('script_a', 'script', 'Script A', owner_id='alice'),
        'import json\nprint(json.dumps({"a_val": 42}))\n',
    )
    scripts.save_script(
        AssetDescriptor('script_b', 'script', 'Script B', owner_id='alice'),
        'import json\nprint(json.dumps({"b_val": 84}))\n',
    )

    steps = [
        {
            'step_id': 'step_a',
            'mode': 'deterministic_script',
            'script_id': 'script_a',
        },
        {
            'step_id': 'step_b',
            'mode': 'deterministic_script',
            'script_id': 'script_b',
            'depends_on': ['step_a'],
        },
    ]

    flow = _flow(steps, descriptor={'id': 'wf_dag_scripts'})
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow(flow)
    assert run.status == 'succeeded'
    assert len(run.steps) == 2

    # Verify runtime record preserved script_id
    step_a_rec = next(s for s in run.steps if s.step_id == 'step_a')
    step_b_rec = next(s for s in run.steps if s.step_id == 'step_b')
    assert step_a_rec.script_id == 'script_a'
    assert step_b_rec.script_id == 'script_b'
    assert step_a_rec.output_snapshot == {'a_val': 42}
    assert step_b_rec.output_snapshot == {'b_val': 84}

    # Verify persisted run in store also preserves script_id
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.status == 'succeeded'
    p_step_a = next(s for s in persisted.steps if s.step_id == 'step_a')
    p_step_b = next(s for s in persisted.steps if s.step_id == 'step_b')
    assert p_step_a.script_id == 'script_a'
    assert p_step_b.script_id == 'script_b'


def test_dag_foreach_step_and_debug_step_side_effects(tmp_path):
    # SA-R2-F2: Run a depends_on DAG workflow containing a foreach step (max_parallel=2)
    # AND call engine.debug_step(..., allow_side_effects=True) on that foreach step,
    # asserting completion within < 5s with accurate batches_summary persisted in engine.store.load_run(...).
    scripts = ScriptStore(tmp_path)
    scripts.save_script(
        AssetDescriptor('produce_items', 'script', 'Producer', owner_id='alice'),
        'import json\nprint(json.dumps({"items": ["alpha", "beta", "gamma", "delta"]}))\n',
    )
    scripts.save_script(
        AssetDescriptor('process_item', 'script', 'Processor', owner_id='alice'),
        'import json\nprint(json.dumps({"processed": True}))\n',
    )

    steps = [
        {
            'step_id': 'producer',
            'mode': 'deterministic_script',
            'script_id': 'produce_items',
        },
        {
            'step_id': 'batch_proc',
            'mode': 'deterministic_script',
            'script_id': 'process_item',
            'depends_on': ['producer'],
            'foreach': {
                'from': 'producer.items',
                'batch_size': 1,
                'max_parallel': 2,
            },
        },
    ]

    flow = _flow(steps, descriptor={'id': 'wf_foreach_dag'}, max_parallel_steps=2)
    WorkflowStore(tmp_path).save_workflow(flow)
    engine = WorkflowEngine(tmp_path)

    t0 = time.monotonic()
    run = engine.run_workflow('wf_foreach_dag')
    duration = time.monotonic() - t0
    assert duration < 5.0, f"Workflow execution took {duration:.2f}s, expected < 5s"
    assert run.status == 'succeeded'

    # Verify persisted batches_summary in engine.store.load_run
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.status == 'succeeded'
    fe_step = next(s for s in persisted.steps if s.step_id == 'batch_proc')
    assert fe_step.status == 'succeeded'
    assert fe_step.batches_summary is not None
    assert fe_step.batches_summary['total'] == 4
    assert fe_step.batches_summary['completed'] == 4
    assert fe_step.batches_summary['failed'] == 0
    assert len(fe_step.batches) == 4

    # Now call engine.debug_step(..., allow_side_effects=True) on the foreach step
    t_dbg = time.monotonic()
    dbg_res = engine.debug_step(
        'wf_foreach_dag',
        'batch_proc',
        mock_inputs={'producer': {'items': ['item1', 'item2']}},
        allow_side_effects=True,
    )
    dbg_duration = time.monotonic() - t_dbg
    assert dbg_duration < 5.0, f"Debug step took {dbg_duration:.2f}s, expected < 5s"
    assert dbg_res['status'] == 'succeeded'
    assert dbg_res['side_effects_blocked'] is False
    assert dbg_res['dry_run'] is False


def test_dag_failed_step_persists_effect_started_and_triggers_reconciliation_fence(tmp_path):
    # Verify failed DAG step persists effect_started=True and needs_repair=True,
    # satisfying the business-scope resume fence.
    def analyze(spec, context, skills):
        sid = spec['step_id']
        if sid == 'second_wave_fail':
            raise RuntimeError('simulated deterministic execution error')
        return {'ok': True}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {
                'step_id': 'second_wave_fail',
                'mode': 'llm_skill_analysis',
                'depends_on': ['root'],
            },
        ],
        max_parallel_steps=2,
    )

    run = engine.run_workflow(flow)
    assert run.status == 'failed'
    assert run.failed_step_id == 'second_wave_fail'

    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.status == 'failed'
    failed_step = next(s for s in persisted.steps if s.step_id == 'second_wave_fail')
    assert failed_step.status == 'failed'
    assert failed_step.escalation_result.get('effect_started') is True
    assert failed_step.needs_repair is True

    # Evaluate business-scope resume condition (workflows/business_scope.py L447-451) inline
    is_resume_control = True
    requires_native_agent = (
        persisted.status == 'waiting_for_agent'
        or any(
            step.mode in ('agent_conversation', 'human_escalation')
            and not step.escalation_result.get('completion_ref')
            for step in persisted.steps
        )
    )
    requires_terminal_pub = (
        persisted.status == 'paused_for_resume'
        and persisted.phase == 'terminal'
    )
    has_unreconciled_effect = any(
        step.escalation_result.get('effect_started')
        and (step.needs_repair or step.status == 'running')
        for step in persisted.steps
    )
    reconciliation_required = (
        is_resume_control
        and not requires_native_agent
        and not requires_terminal_pub
        and (persisted.effect_outcome == 'unknown' or has_unreconciled_effect)
    )
    assert has_unreconciled_effect is True
    assert reconciliation_required is True


def test_dag_success_run_persists_effect_started_and_excludes_skipped_steps(tmp_path):
    # Verify that in a successful DAG run, every executed step has effect_started=True
    # while skipped branches have no effect_started.
    def analyze(spec, context, skills):
        sid = spec['step_id']
        if sid == 'root':
            return {'score': 20}
        if sid == 'independent':
            return {'indep': 'ok'}
        return {'val': 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)

    flow = _flow(
        [
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {
                'step_id': 'high_score_only',
                'mode': 'llm_skill_analysis',
                'depends_on': ['root'],
                'condition': {
                    'op': 'gt',
                    'ref': {'step_id': 'root', 'key': 'score'},
                    'value': 80,
                },
            },
            {
                'step_id': 'cascaded_dependent',
                'mode': 'llm_skill_analysis',
                'depends_on': ['high_score_only'],
            },
            {
                'step_id': 'independent',
                'mode': 'llm_skill_analysis',
                'depends_on': ['root'],
            },
        ],
        max_parallel_steps=2,
    )

    run = engine.run_workflow(flow)
    assert run.status == 'succeeded'

    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.status == 'succeeded'

    step_map = {s.step_id: s for s in persisted.steps}
    assert step_map['root'].status == 'succeeded'
    assert step_map['independent'].status == 'succeeded'
    assert step_map['high_score_only'].status == 'skipped'
    assert step_map['cascaded_dependent'].status == 'skipped'

    # Every executed step has effect_started is True
    assert step_map['root'].escalation_result.get('effect_started') is True
    assert step_map['independent'].escalation_result.get('effect_started') is True

    # Every skipped step does NOT have effect_started
    assert not step_map['high_score_only'].escalation_result.get('effect_started')
    assert not step_map['cascaded_dependent'].escalation_result.get('effect_started')

    # Comprehensive check across all steps in persisted run
    for step in persisted.steps:
        if step.status == 'succeeded':
            assert step.escalation_result.get('effect_started') is True
        elif step.status == 'skipped':
            assert not step.escalation_result.get('effect_started')


@pytest.mark.parametrize("configured", [8, 12])
def test_nested_maps_share_host_capacity_and_allow_independent_progress(tmp_path, monkeypatch, configured):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from types import SimpleNamespace

    monkeypatch.setattr(batching, "_configured_max_parallel", batching.resolved_max_parallel())
    monkeypatch.setattr(batching, "_configured_host_parallel", batching._configured_host_parallel)
    batching.configure_from_config(SimpleNamespace(
        workflow_foreach_max_parallel=configured, gateway_http_max_request_workers=2,
    ))
    two_started, small_queued, release_one, release_all = Event(), Event(), Event(), Event()
    from agent_py_agent.agent.workflows import dag_scheduler

    submit = dag_scheduler.submit_workflow_step

    def observe_submission(engine, *args, **kwargs):
        result = submit(engine, *args, **kwargs)
        if engine.owner_home == tmp_path / "small":
            small_queued.set()
        return result

    monkeypatch.setattr(dag_scheduler, "submit_workflow_step", observe_submission)
    lock = Lock()
    active = peak = started = 0

    def large(spec, context, skills):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            number = started
            peak = max(peak, active)
            if started == 2:
                two_started.set()
        try:
            assert (release_one if number == 1 else release_all).wait(10)
            return {"batch": context.step_inputs["batch"]}
        finally:
            with lock:
                active -= 1

    def small(*_):
        with lock:
            assert active < 2
        return {"value": "small"}

    wide = _flow([{"step_id": f"map-{index}", "mode": "llm_skill_analysis",
                   "foreach": {"from": "inputs.items", "batch_size": 1, "max_parallel": configured, "as": "batch"}}
                  for index in range(2)], execution_mode="dag", max_parallel_steps=configured)
    independent = _flow([{"step_id": "small", "mode": "llm_skill_analysis"}], execution_mode="dag")
    with ThreadPoolExecutor(max_workers=2) as callers:
        big = callers.submit(WorkflowEngine(tmp_path / "large", analysis_handler=large).run_workflow,
                             wide, {"items": list(range(configured))})
        try:
            assert two_started.wait(10)
            ready = callers.submit(WorkflowEngine(tmp_path / "small", analysis_handler=small).run_workflow, independent)
            assert small_queued.wait(5)
            release_one.set()
            assert ready.result(timeout=5).status == "succeeded"
            assert not big.done()
        finally:
            release_one.set()
            release_all.set()
        result = big.result(timeout=10)
    assert result.status == "succeeded"
    assert peak == 2
    for step in result.steps:
        assert step.output_snapshot == [{"batch": [value]} for value in range(configured)]


def test_cancelled_queued_dag_leaf_does_not_need_a_host_grant(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    from agent_py_agent.agent.workflows import dag_scheduler

    monkeypatch.setattr(batching, "_configured_host_parallel", 1)
    holder_started, release_holder, queued = Event(), Event(), Event()
    def hold(*_):
        holder_started.set()
        assert release_holder.wait(10)
        return {"held": True}

    token = CancellationToken()
    effects = []
    small = WorkflowEngine(tmp_path / "small", analysis_handler=lambda *_: effects.append("effect") or {"ok": True})
    flow = _flow([{"step_id": "step", "mode": "llm_skill_analysis"}], execution_mode="dag")
    submit = dag_scheduler.submit_workflow_step

    def observe(engine, *args, **kwargs):
        result = submit(engine, *args, **kwargs)
        if engine.owner_home == small.owner_home:
            queued.set()
        return result

    monkeypatch.setattr(dag_scheduler, "submit_workflow_step", observe)

    def invoke():
        with bind_cancellation_token(token):
            return small.run_workflow(flow)

    with ThreadPoolExecutor(max_workers=2) as callers:
        active = callers.submit(WorkflowEngine(tmp_path / "holder", analysis_handler=hold).run_workflow, flow)
        try:
            assert holder_started.wait(5)
            waiting = callers.submit(invoke)
            assert queued.wait(5)
            token.cancel()
            with pytest.raises(ToolCancelled) as caught:
                waiting.result(timeout=5)
            assert not effects and not active.done()
            workflow_run_id = caught.value.record.workflow_run_id
            paused = small.store.load_run(workflow_run_id)
            assert paused.status == "paused_for_resume" and paused.effect_outcome == "not_started"
        finally:
            release_holder.set()
        assert active.result(timeout=5).status == "succeeded"
    assert small.resume_workflow_run(workflow_run_id).status == "succeeded"
    assert effects == ["effect"]


def test_sequential_subworkflow_leaf_uses_the_host_grant(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.tests.test_conveyor_m57_subworkflow_obligation import _save_workflow

    monkeypatch.setattr(batching, "_configured_host_parallel", 1)
    original_submit = batching._WORKFLOW_DISPATCH.submit
    occupied, release, child_waiting, child_entered = Event(), Event(), Event(), Event()

    def submit(key, fn, *args):
        future = original_submit(key, fn, *args)
        if key[0] == str(tmp_path / "nested"):
            child_waiting.set()
        return future

    monkeypatch.setattr(batching._WORKFLOW_DISPATCH, "submit", submit)

    def independent(*_):
        occupied.set()
        assert release.wait(10)
        return {"value": "independent"}

    def analyze(*_):
        child_entered.set()
        child_waiting.set()
        return {"value": "child"}

    engine = WorkflowEngine(tmp_path / "nested", analysis_handler=analyze)
    _save_workflow(engine, "child", WorkflowStepSpec("work", "work", "llm_skill_analysis"))
    parent = _flow([{"step_id": "child", "mode": "subworkflow", "subworkflow_ref": "workflow:child"}],
                   execution_mode="dag")
    busy = _flow([{"step_id": "busy", "mode": "llm_skill_analysis"}], execution_mode="dag")
    with ThreadPoolExecutor(2) as callers:
        active = callers.submit(WorkflowEngine(tmp_path / "independent", analysis_handler=independent).run_workflow, busy)
        try:
            assert occupied.wait(10)
            nested = callers.submit(engine.run_workflow, parent)
            assert child_waiting.wait(10)
            assert not child_entered.is_set()
        finally:
            release.set()
        assert active.result(timeout=10).status == "succeeded"
        result = nested.result(timeout=10)
    assert result.status == "succeeded"
    assert child_entered.is_set()


@pytest.mark.parametrize("dag", [False, True], ids=["sequential-retry", "dag"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_verified_agent_settlement_releases_original_claim(native, dag, outcome):  # noqa: F811
    from agent_py_agent.tests.test_v13_rework_workflow_claim_lifecycle import _definition, _start

    definition = _definition()
    definition["steps"][-1]["retry_policy"] = {"max_attempts": 2}
    if dag:
        definition["steps"][-1]["depends_on"] = ["prefix"]
    run = native.engine.run_workflow(definition, owner_id="alice", business_context=native.context,
                                     workflow_run_id="original", concurrency_policy="skip_if_running")
    assert run.status == "waiting_for_agent"
    assert _start(native, "contender", delegated=False)["status"] == "skipped"
    task_id = run.steps[-1].escalation_result["task_id"]
    original_claim = native.engine.store._read(run.workflow_run_id, "run.json")["concurrency_claim"]["record"]

    if outcome == "cancelled":
        assert native.engine.cancel_workflow_run(run.workflow_run_id, expected_revision=run.revision,
                                                 operation_key="cancel") == []
        expected_status = "cancelled"
    else:
        native.agents.completion_reader = lambda task_id, **identity: {
            **identity, "task_id": task_id, "status": outcome,
            "completion_ref": "canonical-completion", "output": {"answer": 1},
        }
        expected_status = "succeeded" if outcome == "completed" else "failed"
        resumed = native.engine.resume_workflow_run(run.workflow_run_id)
        assert resumed.status == expected_status

    settled = native.engine.store.load_run(run.workflow_run_id)
    assert settled.status == expected_status
    assert settled.steps[-1].escalation_result["effect_outcome"] == ("confirmed" if outcome == "completed" else "failed")
    claim = native.engine.store._read(run.workflow_run_id, "run.json")["concurrency_claim"]
    assert claim["phase"] == "released"
    assert claim["record"] == original_claim
    assert _start(native, "successor", delegated=False).status == "succeeded"
    tasks, errors = native.store.session_tasks.list_report(limit=0)
    assert not errors and [task.task_id for task in tasks] == [task_id]
    assert native.calls == ["prefix", "prefix"]


@pytest.mark.parametrize("lose_control_ack", [False, True])
def test_terminal_agent_receipt_is_rechecked_without_replaying_suffix(native, monkeypatch, lose_control_ack):  # noqa: F811
    from agent_py_agent.agent.workflows.governance import WorkflowOperationRecovery
    from agent_py_agent.tests.test_v13_rework_workflow_claim_lifecycle import _definition, _start

    definition = _definition()
    definition["steps"][-1]["retry_policy"] = {"max_attempts": 2}
    run = native.engine.run_workflow(definition, owner_id="alice", business_context=native.context,
                                     workflow_run_id="legacy", concurrency_policy="skip_if_running")
    old = native.engine.store.load_run(run.workflow_run_id)
    old.status = "succeeded"
    old.effect_outcome = "confirmed"
    step = old.steps[-1]
    step.status = "succeeded"
    step.output_snapshot = {"preserved": True}
    step.escalation_result.update(completion_ref="canonical-completion", effect_state="unknown", effect_outcome="unknown")
    native.engine.store.save_run(old)
    reader_calls = []

    def complete(task_id, **identity):
        reader_calls.append((task_id, identity["attempt_id"]))
        return {**identity, "task_id": task_id, "status": "completed",
                "completion_ref": "canonical-completion", "output": {"answer": 1}}

    native.agents.completion_reader = complete
    native.engine.run_authorizer = WorkflowOperationRecovery(None).require_effect
    if lose_control_ack:
        def fail_ack(*args, **kwargs):
            raise OSError("simulated control acknowledgement error")

        with monkeypatch.context() as fault:
            fault.setattr(native.engine.store, "complete_control", fail_ack)
            with pytest.raises(OSError, match="acknowledgement"):
                native.engine.resume_workflow_run(old.workflow_run_id, operation_key="proof-only",
                                                  expected_revision=old.revision)
    repaired = native.engine.resume_workflow_run(old.workflow_run_id, operation_key="proof-only",
                                                  expected_revision=old.revision)

    assert repaired.status == "succeeded" and repaired.steps[-1].output_snapshot == {"preserved": True}
    assert repaired.steps[-1].escalation_result["effect_outcome"] == "confirmed"
    assert reader_calls == [(step.escalation_result["task_id"], step.escalation_result["attempt_id"])]
    assert _start(native, "successor", delegated=False).status == "succeeded"
    assert native.calls == ["prefix", "prefix"]


@pytest.mark.parametrize("evidence", ["pending_cancel", "outstanding_refs", "foreign_attempt"])
def test_agent_claim_stays_held_without_complete_original_evidence(native, evidence):  # noqa: F811
    from agent_py_agent.tests.test_v13_rework_workflow_claim_lifecycle import _definition, _start

    definition = _definition()
    definition["steps"][-1]["retry_policy"] = {"max_attempts": 2}
    run = native.engine.run_workflow(definition, owner_id="alice", business_context=native.context,
                                     workflow_run_id="original", concurrency_policy="skip_if_running")
    task_id = run.steps[-1].escalation_result["task_id"]
    if evidence == "pending_cancel":
        native.agents.cancel_handler = lambda *args, **kwargs: {"state": "pending", "outstanding_refs": [task_id]}
        assert native.engine.cancel_workflow_run(run.workflow_run_id, expected_revision=run.revision,
                                                 operation_key="cancel") == [task_id]
        expected_status = "cancelling"
    else:
        def incomplete(task_id, **identity):
            if evidence == "foreign_attempt":
                identity["attempt_id"] = "foreign-attempt"
            return {**identity, "task_id": task_id, "status": "completed", "completion_ref": "completion",
                    "output": {}, "outstanding_refs": [task_id] if evidence == "outstanding_refs" else []}

        native.agents.completion_reader = incomplete
        with pytest.raises(ValueError, match="WORKFLOW_AGENT_COMPLETION_"):
            native.engine.resume_workflow_run(run.workflow_run_id)
        expected_status = "waiting_for_agent"

    persisted = native.engine.store.load_run(run.workflow_run_id)
    assert persisted.status == expected_status
    assert persisted.steps[-1].escalation_result["effect_outcome"] == "unknown"
    assert _start(native, "contender", delegated=False)["status"] == "skipped"
    assert native.calls == ["prefix"]
