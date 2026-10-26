# LLM: 保留原self-watch死锁回归，新增终态根多次后台唤醒、Compact绑定携带及候选探针测试；仅临时目录。
# 模块用途: 验证工作工具绑定和候选热读不会被终态身份或重复全文件摘要卡住。
"""2026-08-09 真机 CONVERSATION_TASK_BINDING_FAILED 死锁复现 + 修复验证。

真机链条（requests 复刻任务）：
1. 父代理 wait 无 run_id → watch_run_id fallback 成自身 task_id（self-watch）
2. claim 驱动轮 conversation_task_id=已死子代理 ID（无 active link）
3. promote 走 sticky 回落 bind 父任务 → 父任务 link active + self-watch policy
   enabled → 被 conversation_task_execution_blocker 判 running → bind None → BINDING_FAILED
4. 父代理全部工作工具被拦，连 cancel policy 都救不了自己 → 永久死锁

修复：
- 读侧：subagent_progress_watch policy 用 watch_run_id 判 running，self-watch 不算
- 写侧：wait 无 run_id 时 watch_run_id 置空（不再 fallback 成自身）
- 消费侧：watch 目标已终态的陈旧 policy 到期即禁用
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_support import RunParams
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    _promote_conversation_task_for_work_tool,
)
from agent_py_agent.agent.conversation.task_promotion import (
    conversation_task_execution_state,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call


def _agent_with_self_watch(tmp_path: Path, *, policy_enabled: bool = True):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayConversationLoadRequest,
        gateway_conversation_context,
    )

    store = agent.conversation_store
    conversation = gateway_conversation_context(
        GatewayConversationLoadRequest(
            agent,
            {
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "session-repro",
                    "channel_user_id": "local-agent",
                }
            },
            "gw-repro",
            "复刻 requests/urllib3",
        )
    )
    thread_id = conversation.thread_id
    task_id = "req_1786250623278_558710_1"
    task_path = Path(agent.home_paths.owner_home_dir) / "tasks" / "existing-task"
    (task_path / "output").mkdir(parents=True)
    (task_path / "work").mkdir()
    # 真机实况 1：父任务 active link
    store.tasks.bind(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "goal": "requests/urllib3 复刻",
            "status": "active",
            "task_path": str(task_path),
            "work_kind": "",
            "work_name": "",
            "cancellation_scope": "foreground",
        }
    )
    # 真机实况 1.5：thread sticky workspace_task_id=父任务（上轮晋升写入）
    store.tasks.select_workspace_task({"thread_id": thread_id, "task_id": task_id})
    # 真机实况 2：父任务自身的 wait policy（wait 无 run_id 的旧 fallback 产物）
    if policy_enabled:
        store.progress.create(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": 600,
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": "sleep",
                    "scope": "own_task_tree",
                    "reason": "",
                    "watch_run_id": task_id,
                },
            }
        )
    return agent, thread_id, task_id


def _promote(agent, params: RunParams, tool_name: str = "list_files"):
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    params.tool_runtime_snapshot = snapshot
    call = canonical_test_call(snapshot, tool_name, {})
    return _promote_conversation_task_for_work_tool(
        ToolCallRuntimeRequest(
            agent=agent,
            request=SimpleNamespace(params=params),
            call=call,
        )
    )


def _params(thread_id, attrs_task_id, task_id):
    return RunParams(
        request_id="bg-main",
        run_id="bg-main-thread-3d9f1fa52eae489c",
        task_id=task_id,
        root_user_prompt="继续 requests/urllib3 复刻",
        task_attributes={
            "conversation_thread_id": thread_id,
            "conversation_task_id": attrs_task_id,
        },
    )


def test_self_watch_policy_does_not_mark_parent_running(tmp_path):
    """修复验证：self-watch policy 不再把父任务判成 running（读侧）。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    state = conversation_task_execution_state(agent.conversation_store, thread_id, task_id)
    assert state["running"] is False


def test_self_watch_policy_no_longer_blocks_sticky_rebind(tmp_path):
    """修复验证：claim 驱动轮（conversation_task_id=子代理 ID）sticky 回落
    bind 父任务不再被 self-watch 拦——BINDING_FAILED 死锁解除。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    params = _params(thread_id, "subagent-1786211892-55685e17", "subagent-1786211892-55685e17")
    agent._current_run_params = params
    outcome = _promote(agent, params)
    assert outcome is None, f"promote 应放行，实际={getattr(outcome, 'error_code', outcome)}"


def test_realistic_watch_of_other_run_still_marks_that_run_running(tmp_path):
    """语义保留：wait 显式 watch 子代理时，子代理仍算 running（H2-2 不重复催促）。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786245734-a462d5b5"
    agent.conversation_store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": child_id,
            },
        }
    )
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    # 父任务 blocker 不因 watch 子代理而拦
    assert _promote(agent, params) is None
    # 但子代理被判 running
    from agent_py_agent.agent.conversation.task_promotion import (
        conversation_task_execution_state,
    )

    state = conversation_task_execution_state(
        agent.conversation_store, thread_id, child_id
    )
    assert state["running"] is True
    assert "progress_policy" in state["sources"]


def _retire_with_child(agent, store, thread_id, child_id, *, task_id):
    store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": child_id,
            },
        }
    )
    # 子代理 BLOCKED 终态 link
    store.tasks.bind(
        {
            "thread_id": thread_id,
            "task_id": child_id,
            "goal": "fixer-2",
            "status": "blocked",
            "cancellation_scope": "foreground",
        }
    )
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        FakeDeliveryService,
    )

    enabled, _ = store.progress.list_report(enabled_only=True)
    stale_policies = [p for p in enabled if str((p.metadata or {}).get("watch_run_id") or "") == child_id]
    assert stale_policies, "watch 子代理的 policy 应存在且 enabled"
    # tick 落在「已 due 但未 stale」窗口(next_due+100s,catchup 是 2h):
    # 只验证「watch 目标终态 → 退休」路径,不靠 stale 假绿。
    due_at = max(float(p.next_due_at or 0) for p in stale_policies)
    stale_ids = {str(p.policy_id) for p in stale_policies}
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent, store=store, channels=FakeDeliveryService()
            ),
            "store": store,
            "claim_ttl_seconds": 30,
        }
    )
    scheduler.tick(now=due_at + 100)
    after_enabled, _ = store.progress.list_report(enabled_only=True)
    after = {str(p.policy_id) for p in after_enabled}
    assert not (stale_ids & after), "watch 目标已终态的陈旧 policy 应被禁用"


def test_stale_watch_policy_retired_when_watch_target_terminal(tmp_path):
    """修复验证：watch 目标已终态（BLOCKED/completed）的陈旧 policy 消费即禁用。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786245734-a462d5b5"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=task_id)


def test_run_observation_does_not_add_a_workspace_file_lock(tmp_path):
    """问题6契约:执行锁豁免从 ToolRuntimePolicy.mutates_workspace 声明推导,
    不再手写工具名名单。executor running 时:写 workspace 工具被拦,读/编排
    工具(wait/cancel 等)必须始终可用——2026-08-09 真机死锁里连 cancel 都
    救不了自己,根源就是豁免名单与模型行为脱节。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    # blocker 只拦「当前 conversation_task_id 自身 running」:用第三者任务 watch
    # 当前任务,让 task_id 在 execution_state 里 running(而非 child running)。
    agent.conversation_store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": "other-task-9",
            "interval_seconds": 300,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "sleep",
                "scope": "own_task_tree",
                "reason": "",
                "watch_run_id": task_id,
            },
        }
    )
    params = _params(thread_id, task_id, task_id)
    agent._current_run_params = params
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    params.tool_runtime_snapshot = snapshot

    # run_command 在无 bwrap 测试环境被 availability 隐藏,不在快照中;契约
    # 测试验证声明驱动机制本身,写类集合取快照内必在的文件系统写工具。
    writing = {"write_file", "edit_file", "apply_patch"}
    exempt = {"read_file", "list_files", "cancel_subagents", "send_guidance"}
    # 先写工具后豁免工具:豁免工具 promote 成功后同轮 TURN_ACTIVE 置位,后续
    # 写工具放行——「首轮绑定后同轮放行」是既有语义,与声明无关,避免串扰。
    for tool_name in sorted(writing) + sorted(exempt):
        runtime = snapshot.runtime(tool_name)
        assert runtime is not None, f"{tool_name} 应在 run snapshot 中"
        call = canonical_test_call(snapshot, tool_name, {})
        outcome = _promote_conversation_task_for_work_tool(
            ToolCallRuntimeRequest(
                agent=agent,
                request=SimpleNamespace(params=params),
                call=call,
            )
        )
        assert outcome is None, f"{tool_name} 不应被其他运行的观察状态当成目录锁"


def test_stale_self_task_watch_retired_when_target_terminal(tmp_path):
    """真机实况补测：陈旧 policy 的 task_id 直接就是被 watch 的子代理 ID
    （watch 目标=task_id 自身）时,目标终态同样必须退休——修复 3 的
    watched != task_id_field 条件把这类政策永远排除,300s 一轮空转。"""
    agent, thread_id, task_id = _agent_with_self_watch(tmp_path)
    child_id = "subagent-1786211892-55685e17"
    _retire_with_child(agent, agent.conversation_store, thread_id, child_id, task_id=child_id)


# LLM: 三次真实工具准入均携带原根ID；按WP15新契约根持久为superseded并指向第一代，不得被复活。
# 函数用途: 复现并验证二次、三次后台唤醒能够递增后继代次且保持同一目录。
def test_three_background_wakes_keep_terminal_root_and_advance_successors(tmp_path):
    import hashlib

    from agent_py_agent.agent.conversation.task_promotion import complete_current_conversation_task

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    derived = root_id + '-continue-' + hashlib.sha256(root_id.encode()).hexdigest()[:8]
    expected = [derived, derived + '-visit-2', derived + '-visit-3']
    published = []
    for index, successor_id in enumerate(expected, 1):
        params = _params(thread_id, root_id, root_id)
        params.source = 'background_main_agent'
        params.task_attributes['background_wake_signal_id'] = f'wake-{index}'
        params.conversation_task_binding_callback = lambda link: published.append(link.task_id) or True
        agent._current_run_params = params
        outcome = _promote(agent, params)
        assert outcome is None, f'第{index}次后台唤醒被拒绝：{getattr(outcome, "error_code", "")}'
        assert params.task_attributes['conversation_task_id'] == successor_id
        assert params.task_attributes['conversation_continued_from_task_id'] == root_id
        assert store.tasks.load(successor_id).task_path == root.task_path
        before_repeat = store.tasks.load(successor_id)
        assert before_repeat.status == 'active'
        assert _promote(agent, params) is None
        after_repeat = store.tasks.load(successor_id)
        assert after_repeat.goal == before_repeat.goal
        assert after_repeat.capability_selection == before_repeat.capability_selection
        assert params.task_attributes['conversation_task_id'] == successor_id
        assert published[-1] == successor_id
        assert complete_current_conversation_task(agent, params.task_attributes,
            source='background_main_agent', current_task_id=successor_id)
        assert store.tasks.load(successor_id).status == 'completed'
        assert store.tasks.load(root_id).status == 'superseded'
        assert store.tasks.load(root_id).superseded_by_task_id == derived
    assert store.threads.require(thread_id).workspace_task_id == expected[-1]


# LLM: 仅后台空提示可继承目标；成功续作按WP15持久化superseded和继任边，拒绝的空前台保持旧终态。
# 函数用途: 覆盖提示来源优先级及后台空字段回退。
@pytest.mark.parametrize(('source', 'prompts', 'expected'), [
    ('background_main_agent', ('', '', ''), 'requests/urllib3 复刻'),
    ('background_main_agent', ('新根提示', '次级提示', '普通提示'), '新根提示'),
    ('background_main_agent', ('', '次级提示', '普通提示'), '次级提示'),
    ('background_main_agent', ('', '', '普通提示'), '普通提示'),
    ('background_main_agent', ('   ', '本次非空提示', ''), '本次非空提示'),
    ('gateway', ('   ', '本次非空提示', '普通提示'), '本次非空提示'),
    ('gateway', ('', '', ''), None),
])
def test_terminal_continuation_prompt_priority_and_background_fallback(tmp_path, source, prompts, expected):
    from agent_py_agent.agent.conversation.task_promotion import promote_current_conversation_task

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    params = _params(thread_id, root_id, root_id)
    params.source = source
    params.root_user_prompt, params.user_prompt, params.prompt = prompts
    agent._current_run_params = params
    selected = promote_current_conversation_task(agent)
    if expected is None:
        assert selected is None
    else:
        assert selected is not None, '后台空提示不应阻断精确旧任务的续接'
        assert selected.goal == expected
    retired = store.tasks.load(root_id)
    assert retired.status == ('completed' if expected is None else 'superseded')
    if selected is not None:
        assert retired.superseded_by_task_id == selected.task_id


# LLM: 替身只替代模型/Compact边界；原后台循环须跨三次全新prepare仅携带三个绑定字符串，不携带权限/attempt。
# 函数用途: 复现压缩重试丢失已晋升任务属性，且证明属性不跨后台片保留。
def test_compact_retries_retain_only_promoted_task_binding_in_same_slice(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import background_execution as execution

    thread = SimpleNamespace(thread_id='thread', compact_generation=0)
    request = SimpleNamespace(conversation_turn_id='bg-turn')
    history = SimpleNamespace(seed=None, compact_context=None, compact_source=SimpleNamespace(messages=['history']))
    promoted = {'conversation_task_id': 'root-continue-digest', 'conversation_task_path': '/owner/tasks/original',
                'conversation_continued_from_task_id': 'root'}
    seen, prepared, completed = [], [], []

    # LLM: 每次返回全新参数且故意重复已终态前代，模拟真实prepare_run；轮内其他字段必须保持独立。
    # 函数用途: 构造不带上次晋升属性的新尝试。
    def prepare(*args, **kwargs):
        params = RunParams(request_id='request', task_id='root', run_id='fresh-run',
            task_attributes={'conversation_task_id': 'root', 'fresh_attempt': len(prepared)})
        prepared.append(params)
        return params

    # LLM: 第一模型边界模拟工具已晋升，后二次故意缺少部分属性；拒绝把其他临时字段复制到后续尝试。
    # 函数用途: 生成两次overflow及最后成功结果。
    def attempt(deps, prompt, params, *args, **kwargs):
        seen.append(dict(params.task_attributes))
        position = len(seen)
        if position == 1:
            params.task_attributes.update(promoted, should_not_carry='attempt-only')
        elif position == 2:
            params.task_attributes['conversation_continued_from_task_id'] = ''
        status = 'context_overflow' if position < 3 else 'completed'
        result = SimpleNamespace(runtime_status=status, runtime_source='provider')
        recovery = SimpleNamespace(committed=True, committed_thread=thread) if position > 1 else None
        return result, replace(params, run_id=f'actual-{position}', attempt_id=f'attempt-{position}'), recovery

    monkeypatch.setattr(execution, 'prepare_background_history_or_raise', lambda *a, **k: history)
    monkeypatch.setattr(execution, '_prepared_slice_context', lambda *a, **k: None)
    monkeypatch.setattr(execution, 'render_background_context', lambda *a, **k: '')
    monkeypatch.setattr(execution, '_run_background_recovery_attempt', attempt)
    monkeypatch.setattr(execution, '_refresh_background_compact_source', lambda *a, **k: (thread, history))
    monkeypatch.setattr(execution, '_next_overflow_carry', lambda *a, **k: (None, [], []))
    monkeypatch.setattr(execution, '_persist_background_native_turn', lambda *a, **k: None)
    deps = execution.BackgroundExecutionDependencies(SimpleNamespace(), SimpleNamespace(), prepare)
    sink = SimpleNamespace(begin_model_attempt=lambda *a: None)
    execution.run_background_turn_with_compact(deps, thread, request, user_prompt='继续',
        continuation_injection=[], proactive_delivery_available=False, activity_sink=sink,
        completed_run_callback=completed.append)
    assert len(seen) == 3
    for position, attrs in enumerate(seen[1:], 1):
        assert {key: attrs.get(key) for key in promoted} == promoted
        assert attrs['fresh_attempt'] == position and 'should_not_carry' not in attrs
        assert prepared[position].run_id == 'fresh-run'
    assert completed[0].attempt_id == 'attempt-3'
    execution.run_background_turn_with_compact(deps, thread, request, user_prompt='下一片',
        continuation_injection=[], proactive_delivery_available=False, activity_sink=sink)
    assert seen[-1]['conversation_task_id'] == 'root'
    assert 'conversation_task_path' not in seen[-1]


# LLM: spy只统计当前临时路径的read_bytes，不将调用次数当成真实Gateway CPU或外部环境性能结论。
# 函数用途: 记录摘要探针实际读取整文件的次数。
def _count_probe_reads(monkeypatch, path):
    original, reads = Path.read_bytes, []

    # LLM: 其它读取原样执行，只有指定文件计数，避免影响候选真实JSONL解析。
    # 函数用途: 包装原始文件读取并保留其结果与异常。
    def read_bytes(current):
        if current == path:
            reads.append(True)
        return original(current)

    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    return reads


# LLM: 大小边界使用真实文件；小文件每次重算，达到1MiB的稳定五元stamp才允许复用摘要。
# 函数用途: 验证探针阈值及热读避免重复整文件读取。
@pytest.mark.parametrize(('size', 'expected_reads'), [(1024 * 1024 - 1, 2), (1024 * 1024, 1), (1024 * 1024 + 1, 1)])
def test_candidate_probe_reuses_digest_only_at_large_file_threshold(tmp_path, monkeypatch, size, expected_reads):
    from agent_py_agent.agent.memory_store import candidates

    path = tmp_path / 'probe.jsonl'
    path.write_bytes(b'x' * size)
    reads = _count_probe_reads(monkeypatch, path)
    first = candidates._candidate_load_probe(path)
    assert candidates._candidate_load_probe(path) == first
    assert first is not None and len(first[0]) == 5
    assert len(reads) == expected_reads


# LLM: 冻结小文件全部stat字段模拟同纳秒原位改写，摘要依然必须检测到内容变化。
# 函数用途: 守住小账本不依赖stat猜测内容的旧安全合同。
def test_candidate_small_probe_rehashes_even_if_all_stamps_match(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    path = tmp_path / 'small.jsonl'
    path.write_bytes(b'old')
    stamp = candidates._candidate_load_stamp(path)
    monkeypatch.setattr(candidates, '_candidate_load_stamp', lambda _path: stamp)
    reads = _count_probe_reads(monkeypatch, path)
    old = candidates._candidate_load_probe(path)
    path.write_bytes(b'new')
    current = candidates._candidate_load_probe(path)
    assert current[0] == old[0] and current[1] != old[1]
    assert len(reads) == 2


# LLM: list/get/locked_snapshot使用同一真实CandidateService；观察写入和快照锁不被替身代替。
# 函数用途: 验证多MiB候选热查询不再反复读取全文件且正常写入会失效摘要。
def test_large_candidate_list_get_snapshot_share_probe_and_invalidate_on_write(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    path = tmp_path / 'candidates.jsonl'
    service = CandidateService(path)
    records = service.observe_many([CandidateObservation(candidate_type='project', subject_key=f'large.probe.{index}',
        content=f'大候选{index} ' + 'x' * 1900, scope=MemoryScope('personal', 'personal'), origin='reviewed')
        for index in range(512)])
    expected = records[0]
    assert path.stat().st_size >= 1024 * 1024
    reads = _count_probe_reads(monkeypatch, path)
    assert {row.candidate_id for row in service.list()} == {row.candidate_id for row in records}
    assert service.get(expected.candidate_id) == expected
    with service.locked_snapshot(expected) as actual:
        assert actual == expected and actual is not expected
    assert service.list()[0] is not expected
    assert len(reads) == 0
    updated = service.transition(expected.candidate_id, 'rejected', reviewer='test-reviewer')
    after_write = len(reads)
    assert service.get(expected.candidate_id) == updated
    assert len(reads) == after_write
    assert updated in service.list() and len(reads) == after_write


# LLM: 五元stamp中任一字段变化都拒绝复用；直接替换stat投影以确定性覆盖每个维度，不等待系统时钟。
# 函数用途: 检验mtime/size/inode/device/ctime都是大文件摘要的有效版本键。
@pytest.mark.parametrize('field', range(5))
def test_candidate_large_probe_rehashes_on_each_stamp_change(tmp_path, monkeypatch, field):
    from agent_py_agent.agent.memory_store import candidates

    path = tmp_path / 'large.jsonl'
    data = b'x' * (1024 * 1024)
    path.write_bytes(data)
    stamp = list(candidates._candidate_load_stamp(path))
    monkeypatch.setattr(candidates, '_candidate_load_stamp', lambda _path: tuple(stamp))
    reads = _count_probe_reads(monkeypatch, path)
    assert candidates._candidate_load_probe(path) is not None
    if field == 1:
        path.write_bytes(data + b'y')
    stamp[field] += 1
    current = candidates._candidate_load_probe(path)
    assert current is not None and current[0] == tuple(stamp)
    assert candidates._candidate_load_probe(path) == current
    assert len(reads) == 2


# LLM: 解析缓存驱逐同时清理stat摘要，不能留下独立且永远命中的第二份缓存。
# 函数用途: 验证显式失效会强制下一次大文件探针重新读取。
def test_drop_candidate_cache_also_drops_stamp_digest(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    path = tmp_path / 'large.jsonl'
    path.write_bytes(b'x' * (1024 * 1024))
    reads = _count_probe_reads(monkeypatch, path)
    candidates._candidate_load_probe(path)
    candidates._drop_candidate_load_cache(path)
    assert str(path) not in getattr(candidates, '_CANDIDATE_STAMP_DIGEST_CACHE', {})
    candidates._candidate_load_probe(path)
    candidates._candidate_load_probe(path)
    assert len(reads) == 2


# LLM: 摘要只占常数空间且按原路径数上界LRU驱逐，不增加永久状态或放大原128MiB内容预算。
# 函数用途: 验证摘要缓存路径数有界且热命中刷新顺序。
def test_candidate_stamp_digest_cache_is_bounded_lru(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    monkeypatch.setattr(candidates, '_CANDIDATE_LOAD_CACHE_MAX_PATHS', 2)
    paths = [tmp_path / f'{index}.jsonl' for index in range(3)]
    for path in paths:
        path.write_bytes(b'x' * (1024 * 1024))
    for path in paths[:2]:
        candidates._candidate_load_probe(path)
    candidates._candidate_load_probe(paths[0])
    candidates._candidate_load_probe(paths[2])
    cache = getattr(candidates, '_CANDIDATE_STAMP_DIGEST_CACHE', {})
    assert list(cache) == [str(paths[0]), str(paths[2])]


# LLM: 文件在读取窗口变化或不可读取时不发布摘要；超原字节预算也不能触发read_bytes。
# 函数用途: 验证读取竞态与过大文件仍沿原禁用缓存合同。
def test_candidate_probe_rejects_race_and_oversize_without_cache_publication(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    path = tmp_path / 'race.jsonl'
    path.write_bytes(b'x' * (1024 * 1024))
    original = candidates._candidate_load_stamp(path)
    changed = (*original[:4], original[4] + 1)
    stamps = iter([original, changed])
    monkeypatch.setattr(candidates, '_candidate_load_stamp', lambda _path: next(stamps))
    assert candidates._candidate_load_probe(path) is None
    assert str(path) not in getattr(candidates, '_CANDIDATE_STAMP_DIGEST_CACHE', {})
    monkeypatch.setattr(candidates, '_candidate_load_stamp', lambda _path: (1, candidates._CANDIDATE_LOAD_CACHE_MAX_BYTES + 1, 1, 1, 1))
    monkeypatch.setattr(Path, 'read_bytes', lambda _path: pytest.fail('超预算文件不得读取'))
    assert candidates._candidate_load_probe(path) is None


# LLM: 全部候选经真实observe/transition生成并预热缓存，不把测试手工元组当作已验证账本。
# 函数用途: 准备含多条淘汰、两条待审和一条批准记录的候选快照。
@pytest.fixture
def readonly_candidate_snapshot(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    service = CandidateService(tmp_path / 'readonly-candidates.jsonl')
    rows = service.observe_many([CandidateObservation(
        candidate_type='long_term_fact' if index % 2 == 0 else 'project',
        content=f'候选只读筛选测试记录{index}', subject_key=f'readonly.filter.{index}',
        scope=MemoryScope('personal', 'personal'), origin='reviewed',
    ) for index in range(6)])
    for row in rows[:3]:
        service.transition(row.candidate_id, 'rejected', reviewer='test-reviewer')
    service.transition(rows[-1].candidate_id, 'approved', reviewer='test-reviewer')
    return service, service.list()


# LLM: 只包装候选模块的深拷贝入口，内部递归仍由真实copy实现；记录拷贝容器与候选ID而非依赖耗时阈值。
# 函数用途: 精确观察列表和统计热读是否复制了无关候选。
def _count_candidate_copies(monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    original, copies = candidates.deepcopy, []

    # LLM: 保留真实深复制结果与异常，不把spy变成绕过隔离的浅拷贝。
    # 函数用途: 记录本次被深拷贝的候选容器。
    def copy_rows(value):
        copies.append((type(value).__name__, tuple(row.candidate_id for row in value)))
        return original(value)

    monkeypatch.setattr(candidates, 'deepcopy', copy_rows)
    return copies


# LLM: 先过滤再按created_at/id排序取末尾limit，空集合不等于无过滤；命中缓存时只复制最终结果一次。
# 函数用途: 复现并守住热列表不再深复制全表的边界。
@pytest.mark.parametrize(('statuses', 'limit'), [
    ({'pending_review'}, 0), ({'pending_review'}, 1), ({'rejected'}, 1),
    (None, 2), (set(), 1), ({'unknown_status'}, 0), (None, 0), ({'pending_review'}, -1),
])
def test_readonly_candidate_list_copies_only_filtered_sorted_slice(
    readonly_candidate_snapshot, monkeypatch, statuses, limit,
):
    service, snapshot = readonly_candidate_snapshot
    expected = [row for row in snapshot if statuses is None or row.status in statuses]
    expected.sort(key=lambda row: (row.created_at, row.candidate_id))
    if limit > 0:
        expected = expected[-limit:]
    copies = _count_candidate_copies(monkeypatch)
    actual = service.list(statuses=statuses, limit=limit)
    assert actual == expected
    assert copies == [('list', tuple(row.candidate_id for row in expected))]


# LLM: 返回的候选本身及嵌套scope/history都是外部可变对象，任何改写不得污染进程内只读投影或磁盘。
# 函数用途: 验证筛选后深拷贝依旧提供完整隔离。
def test_readonly_candidate_list_returned_objects_do_not_mutate_cache(readonly_candidate_snapshot):
    service, snapshot = readonly_candidate_snapshot
    selected = service.list(statuses={'pending_review'}, limit=1)
    before = next(row for row in snapshot if row.candidate_id == selected[0].candidate_id)
    selected[0].content = '只改返回值'
    selected[0].scope['scope_key'] = 'forged-scope'
    selected[0].status_history[0]['status'] = 'forged-status'
    selected[0].learning_source_coverage['forged'] = ['mutable']
    selected.clear()
    assert service.get(before.candidate_id) == before
    assert service.list() == snapshot


# LLM: 热统计只读取已验证快照中的计数字段，不能为计数复制任何候选；输出不含正文或缓存引用。
# 函数用途: 复现统计全表深拷贝并核对状态/类型聚合完全一致。
def test_readonly_candidate_stats_borrows_snapshot_without_deepcopy(readonly_candidate_snapshot, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import CANDIDATE_SCHEMA_VERSION

    service, _snapshot = readonly_candidate_snapshot
    copies = _count_candidate_copies(monkeypatch)
    actual = service.stats()
    assert copies == []
    assert actual == {'schema_version': CANDIDATE_SCHEMA_VERSION, 'total': 6,
                      'by_status': {'approved': 1, 'pending_review': 2, 'rejected': 3},
                      'by_type': {'long_term_fact': 3, 'project': 3}}


# LLM: 实例与子类覆盖_load_unlocked时不能借原缓存绕过其语义，即使原路径已经有热快照。
# 函数用途: 保留候选加载替身/扩展的既有读取入口。
@pytest.mark.parametrize('override_kind', ['instance', 'subclass'])
def test_readonly_candidate_snapshot_respects_load_override(readonly_candidate_snapshot, monkeypatch, override_kind):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.candidates import CandidateService

    service, snapshot = readonly_candidate_snapshot
    expected = replace(snapshot[0], candidate_id='override-only', status='pending_review')
    calls = []

    # LLM: 只读测试覆盖返回独立新列表，不写正式账本；调用次数证明缓存没有抢占此入口。
    # 函数用途: 提供覆盖后的唯一候选。
    def load():
        calls.append(True)
        return [expected]

    # LLM: 用真正子类覆盖方法验证绑定方法判定，不修改基类或其它service。
    # 类用途: 模拟扩展候选服务的自定义加载路径。
    class CustomCandidateService(CandidateService):
        # LLM: 返回调用方定义的快照，不进入父类解析缓存。
        # 函数用途: 保留子类的加载语义。
        def _load_unlocked(self):
            return load()

    if override_kind == 'instance':
        monkeypatch.setattr(service, '_load_unlocked', load)
    else:
        service = CustomCandidateService(service.path)
    assert service.list(statuses={'pending_review'}) == [expected]
    assert service.stats()['total'] == 1
    assert service.stats()['by_status'] == {'pending_review': 1}
    assert len(calls) == 3


# LLM: 覆盖list是既有兼容入口，stats必须消费它而不是自行读取缓存；Iterable不要求支持len。
# 函数用途: 验证统计保留实例/子类自定义列表及生成器语义。
@pytest.mark.parametrize('override_kind', ['instance', 'subclass'])
@pytest.mark.parametrize('as_iterator', [False, True])
def test_readonly_candidate_stats_respects_list_override(
    readonly_candidate_snapshot, monkeypatch, override_kind, as_iterator,
):
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    service, snapshot = readonly_candidate_snapshot
    calls = []

    # LLM: 覆盖入口只返回已知一条记录，不访问磁盘或原列表实现。
    # 函数用途: 构造可计数列表或一次性迭代器。
    def listed():
        calls.append(True)
        return iter(snapshot[:1]) if as_iterator else snapshot[:1]

    # LLM: 真正覆盖list，验证stats不会跨越已有扩展入口。
    # 类用途: 模拟定制列表的候选服务。
    class CustomCandidateService(CandidateService):
        # LLM: 原样返回定制集合，统计端负责按Iterable消费。
        # 函数用途: 暴露覆盖后的单条候选。
        def list(self, **kwargs):
            return listed()

    if override_kind == 'instance':
        monkeypatch.setattr(service, 'list', listed)
    else:
        service = CustomCandidateService(service.path)
    result = service.stats()
    assert calls == [True]
    assert result['total'] == 1
    assert result['by_status'] == {snapshot[0].status: 1}
    assert result['by_type'] == {snapshot[0].candidate_type: 1}


# LLM: 冷读或变更后的账本必须先验证全表，不能借状态过滤或统计把坏行隐藏成健康结果。
# 函数用途: 验证只读优化不绕过原损坏检测。
@pytest.mark.parametrize('operation', ['list', 'stats'])
def test_readonly_candidate_snapshot_does_not_hide_corrupt_ledger(readonly_candidate_snapshot, operation):
    from agent_py_agent.agent.memory_store.candidates import CandidateStoreCorruptError

    service, _snapshot = readonly_candidate_snapshot
    service.path.write_text('not valid json\n', encoding='utf-8')
    with pytest.raises(CandidateStoreCorruptError):
        if operation == 'list':
            service.list(statuses=set())
        else:
            service.stats()


# LLM: 写入路径继续从_load_unlocked取得整表独立副本，不能把热只读投影直接交给写方修改。
# 函数用途: 防止只读优化误改写侧加载合同。
def test_readonly_optimization_keeps_writer_load_snapshot_isolated(readonly_candidate_snapshot):
    service, snapshot = readonly_candidate_snapshot
    with service._locked_candidates():
        rows = service._load_unlocked()
    rows[0].content = '只改写入前快照'
    rows[0].scope['scope_key'] = 'forged-scope'
    assert service.list() == snapshot


# LLM: 每轮重新构造RunParams且携带原根ID，晋升/回绑、TaskStore及持久文件均使用真实入口。
# 函数用途: 验证原根到两代继任的持久关系与同目录绑定。
@pytest.mark.parametrize('entry', ['promote', 'bind'])
def test_wp15_background_wakes_follow_persisted_root_successor_chain(tmp_path, entry):
    import hashlib

    from agent_py_agent.agent.conversation.task_promotion import (
        bind_current_conversation_workspace,
        promote_current_conversation_task,
    )

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    first_id = root_id + '-continue-' + hashlib.sha256(root_id.encode()).hexdigest()[:8]
    for expected_id in (first_id, first_id + '-visit-2'):
        params = _params(thread_id, root_id, root_id)
        params.source, params.root_user_prompt = 'background_main_agent', ''
        agent._current_run_params = params
        selected = (promote_current_conversation_task(agent) if entry == 'promote'
                    else bind_current_conversation_workspace(agent, root_id))
        assert selected is not None, '原根ID必须解析到当前合法继任代'
        assert selected.task_id == expected_id and selected.task_path == root.task_path
        assert params.task_attributes['conversation_task_id'] == expected_id
        assert agent._current_run_task_workspace == root.task_path
        prior_id = root_id if expected_id == first_id else first_id
        prior = store.tasks.load(prior_id)
        assert prior.status == 'superseded'
        assert getattr(prior, 'superseded_by_task_id', '') == expected_id
        store.tasks.update_status({'task_id': expected_id, 'status': 'completed'})
    assert store.tasks.load(root_id).superseded_by_task_id == first_id


# LLM: 只有历史继任指针用canonical JSON构造，其他记录通过TaskStore建立；不替换待测选择器。
# 函数用途: 准备跨重启链读取、损坏边及跳数边界的临时案例。
def wp15_chain_case(tmp_path, *, hops=1, fault=''):
    import json

    from agent_py_agent.agent.common.json_io import write_private_text_file_atomic

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.load(root_id)
    ids = [root_id, *[f'chain-hop-{index}' for index in range(1, hops + 1)]]
    for index, task_id in enumerate(ids[1:], 1):
        store.tasks.bind({'thread_id': thread_id, 'task_id': task_id, 'goal': root.goal,
            'task_path': root.task_path, 'status': 'active' if index == hops else 'superseded'})
    for index, task_id in enumerate(ids[:-1]):
        path = store.storage.task_path(task_id)
        data = json.loads(path.read_text())
        data.update(status='superseded', superseded_by_task_id=ids[index + 1])
        write_private_text_file_atomic(path, json.dumps(data))
    if fault:
        path = store.storage.task_path(ids[-1] if fault in {'failed', 'cancelled', 'unknown', 'path', 'thread'} else root_id)
        data = json.loads(path.read_text())
        if fault in {'failed', 'cancelled', 'unknown'}:
            data['status'] = fault
        elif fault == 'path':
            other = Path(root.task_path).parent / 'different'
            other.mkdir()
            data['task_path'] = str(other)
        elif fault == 'thread':
            data['thread_id'] = 'another-thread'
        elif fault in {'cycle', 'empty', 'missing'}:
            data['superseded_by_task_id'] = {'cycle': root_id, 'empty': '', 'missing': 'missing-link'}[fault]
        write_private_text_file_atomic(path, json.dumps(data))
    params = _params(thread_id, root_id, root_id)
    params.source = 'background_main_agent'
    agent._current_run_params = params
    return agent, root, ids


# LLM: 16跳允许，17跳拒绝；通过真实回绑而非仅调用辅助函数验证有界解析。
# 函数用途: 验证已有继任链可以沿原根ID绑定且不能无限遍历。
@pytest.mark.parametrize('hops', [1, 16, 17])
def test_wp15_existing_chain_resolves_with_sixteen_hop_limit(tmp_path, hops):
    from agent_py_agent.agent.conversation.task_promotion import bind_current_conversation_workspace

    agent, root, ids = wp15_chain_case(tmp_path, hops=hops)
    selected = bind_current_conversation_workspace(agent, root.task_id)
    if hops <= 16:
        assert selected is not None and selected.task_id == ids[-1]
        assert selected.task_path == root.task_path
    else:
        assert selected is None


# LLM: 保留非空提示，坏链不得回落到新任务；失败前后逐字节比较全部canonical任务JSON。
# 函数用途: 验证环路、缺边、跨路径/线程及不安全继任状态失败关闭。
@pytest.mark.parametrize('fault', ['cycle', 'empty', 'missing', 'path', 'thread', 'failed', 'cancelled', 'unknown'])
@pytest.mark.parametrize('entry', ['promote', 'bind'])
def test_wp15_invalid_successor_chain_fails_closed(tmp_path, fault, entry):
    from agent_py_agent.agent.conversation.task_promotion import (
        bind_current_conversation_workspace,
        promote_current_conversation_task,
    )

    agent, root, _ids = wp15_chain_case(tmp_path, fault=fault)
    storage = agent.conversation_store.storage
    before = {path: path.read_bytes() for path in storage.tasks_dir.glob('*.json')}
    selected = (promote_current_conversation_task(agent) if entry == 'promote'
                else bind_current_conversation_workspace(agent, root.task_id))
    assert selected is None
    assert {path: path.read_bytes() for path in storage.tasks_dir.glob('*.json')} == before


# LLM: 已解析的活动继任代才是执行观察键，不能按原根ID漏查进度策略。
# 函数用途: 防止正在运行的继任代被第二次绑定为新执行者。
def test_wp15_successor_running_policy_still_blocks_root_rebind(tmp_path):
    from agent_py_agent.agent.conversation.task_promotion import bind_current_conversation_workspace

    agent, root, ids = wp15_chain_case(tmp_path)
    agent.conversation_store.progress.create({'thread_id': root.thread_id, 'task_id': 'observer',
        'interval_seconds': 300, 'metadata': {'kind': 'subagent_progress_watch', 'watch_run_id': ids[-1]}})
    assert bind_current_conversation_workspace(agent, root.task_id) is None


# LLM: 通过实际模型JSON往返检查字段；旧缺失仍为空，不从任务名推导继任。
# 函数用途: 验证持久继任指针能序列化和恢复。
def test_wp15_task_link_successor_round_trip():
    from agent_py_agent.agent.conversation.models import ThreadTaskLink

    old = {'thread_id': 'thread', 'task_id': 'root', 'goal': 'goal'}
    assert getattr(ThreadTaskLink.from_dict(old), 'superseded_by_task_id', '') == ''
    linked = ThreadTaskLink.from_dict({**old, 'status': 'superseded', 'superseded_by_task_id': 'successor'})
    assert linked.to_dict().get('superseded_by_task_id') == 'successor'


# LLM: 真实TaskStore绑定/索引和前代文件全部保留，只在指针发布边界注入失败；不替换产品回滚。
# 函数用途: 验证继任指针返回失败、直接异常或底层IO失败后清理活动孤儿并允许重试。
@pytest.mark.parametrize('fault', ['false', 'raise', 'io'])
def test_wp19_successor_pointer_failure_rolls_back_new_task_and_retries(tmp_path, monkeypatch, fault):
    import hashlib

    from agent_py_agent.agent.conversation import task_promotion as promotion
    from agent_py_agent.agent.gateway_parts import io

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    params = _params(thread_id, root_id, root_id)
    agent._current_run_params = params
    before = store.threads.require(thread_id)
    derived = root_id + '-continue-' + hashlib.sha256(root_id.encode()).hexdigest()[:8]
    publish, update_json = promotion._persist_superseded_successor, io.update_json_file_atomic

    # LLM: 单次可确定故障，异常不能绕过清理，返回False也不能报告成功。
    # 函数用途: 模拟前代指针发布未发生。
    def fail_publish(*_args, **_kwargs):
        if fault == 'raise':
            raise OSError('pointer publication fault')
        return False

    # LLM: 仅前代canonical更新失败，继任创建与回滚仍执行真实持久化。
    # 函数用途: 注入真实发布器内部的IO错误。
    def fail_pointer_io(path, *args, **kwargs):
        if path == store.storage.task_path(root_id):
            raise OSError('pointer IO fault')
        return update_json(path, *args, **kwargs)

    if fault == 'io':
        monkeypatch.setattr(io, 'update_json_file_atomic', fail_pointer_io)
    else:
        monkeypatch.setattr(promotion, '_persist_superseded_successor', fail_publish)
    assert promotion.bind_current_conversation_workspace(agent, root_id) is None
    assert store.tasks.load(derived) is None
    after = store.threads.require(thread_id)
    assert (after.task_ids, after.active_task_ids, after.workspace_task_id) == (
        before.task_ids, before.active_task_ids, before.workspace_task_id)
    assert store.tasks.load(root_id) == root
    monkeypatch.setattr(promotion, '_persist_superseded_successor', publish)
    monkeypatch.setattr(io, 'update_json_file_atomic', update_json)
    selected = promotion.bind_current_conversation_workspace(agent, root_id)
    assert selected is not None and selected.task_id == derived
    assert store.tasks.load(root_id).superseded_by_task_id == derived
    thread = store.threads.require(thread_id)
    assert thread.active_task_ids == (derived,) and thread.workspace_task_id == derived


# LLM: 真实指针已成功提交后丢失回执，不允许误删已挂接继任；使用正式发布器而非手造成功对象。
# 函数用途: 验证发布返回False或抛错但同一有效指针已落盘时按幂等成功收口。
@pytest.mark.parametrize('raise_after_commit', [False, True])
def test_wp19_committed_successor_pointer_is_idempotent_success(tmp_path, monkeypatch, raise_after_commit):
    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    agent._current_run_params = _params(thread_id, root_id, root_id)
    publish = promotion._persist_superseded_successor

    # LLM: 原状态、指针和索引确已提交，异常只代表回执丢失，不表示事务没发生。
    # 函数用途: 注入发布后故障。
    def lose_receipt(*args, **kwargs):
        assert publish(*args, **kwargs)
        if raise_after_commit:
            raise OSError('lost publication receipt')
        return False

    monkeypatch.setattr(promotion, '_persist_superseded_successor', lose_receipt)
    selected = promotion.bind_current_conversation_workspace(agent, root_id)
    assert selected is not None
    assert store.tasks.load(root_id).superseded_by_task_id == selected.task_id
    assert store.tasks.load(selected.task_id).status == 'active'
    assert store.threads.require(thread_id).active_task_ids == (selected.task_id,)


# LLM: 未由本次创建、或新建后已被推进的任务不归本次回滚所有；真实状态更新不被替身吞掉。
# 函数用途: 防止指针冲突的清理误删既有继任或已变化的新任务。
@pytest.mark.parametrize('changed', ['existing', 'completed', 'goal'])
def test_wp19_pointer_failure_preserves_existing_or_changed_successor(tmp_path, monkeypatch, changed):
    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    params = _params(thread_id, root_id, root_id)
    agent._current_run_params = params
    successor_id = promotion._terminal_successor_task_id(store, thread_id, base_id=root_id,
        source_id=root_id, task_path=root.task_path)
    if changed == 'existing':
        store.tasks.bind({'thread_id': thread_id, 'task_id': successor_id, 'goal': '已有继任',
            'status': 'active', 'task_path': root.task_path})

    # LLM: 先让独立更新生效，再报告发布失败；清理方须比较最新任务而非只看旧active快照。
    # 函数用途: 模拟其他执行者已接手或改变新建任务。
    def conflict(_store, _previous, successor):
        if changed == 'completed':
            store.tasks.update_status({'task_id': successor.task_id, 'status': 'completed'})
        elif changed == 'goal':
            store.tasks.update_goal({'task_id': successor.task_id, 'goal': '其他执行者已更新'})
        return False

    monkeypatch.setattr(promotion, '_persist_superseded_successor', conflict)
    assert promotion.bind_current_conversation_workspace(agent, root_id) is None
    remaining = store.tasks.load(successor_id)
    assert remaining is not None
    assert remaining.status == ('completed' if changed == 'completed' else 'active')
    assert remaining.goal == {'existing': '已有继任', 'goal': '其他执行者已更新'}.get(changed, params.root_user_prompt)
    assert successor_id in store.threads.require(thread_id).task_ids


# LLM: 发布器仍执行真实指针写入，只让前代索引同步首轮失败；恢复须补索引而不是删已提交的继任。
# 函数用途: 验证指针提交与索引修复之间的部分成功可幂等恢复。
def test_wp19_pointer_committed_before_index_failure_repairs_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    agent._current_run_params = _params(thread_id, root_id, root_id)
    update, attempts = store.tasks.update_status, []

    # LLM: 不阻断前代指针的canonical写入；仅模拟首轮原索引修复返回失败。
    # 函数用途: 让回执恢复分支必须重做TaskStore的同状态同步。
    def fail_first_index(request):
        if request['task_id'] == root_id and request['status'] == 'superseded':
            attempts.append(True)
            if len(attempts) == 1:
                return None
        return update(request)

    monkeypatch.setattr(store.tasks, 'update_status', fail_first_index)
    selected = promotion.bind_current_conversation_workspace(agent, root_id)
    assert selected is not None and len(attempts) == 2
    assert store.tasks.load(root_id).superseded_by_task_id == selected.task_id
    assert store.threads.require(thread_id).active_task_ids == (selected.task_id,)


# LLM: 真实发布另一合法继任后报告本次冲突；回滚不得覆盖获胜指针、其它任务索引、并发标题或新的工作区选择。
# 函数用途: 验证冲突回滚只回收本次新建者，且待删投影才恢复原workspace。
@pytest.mark.parametrize('workspace_choice', ['ours', 'winner'])
def test_wp19_pointer_conflict_preserves_winner_and_concurrent_thread_fields(tmp_path, monkeypatch, workspace_choice):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    agent._current_run_params = _params(thread_id, root_id, root_id)
    publish, candidates = promotion._persist_superseded_successor, []

    # LLM: 所有状态及索引变更使用真实Store；只在发布边界模拟另一竞争者已获胜。
    # 函数用途: 建立冲突目标及并发显示变更。
    def competing_publish(_store, previous, successor):
        candidates.append(successor.task_id)
        winner = store.tasks.bind({'thread_id': thread_id, 'task_id': 'concurrent-winner',
            'goal': '获胜的执行者', 'status': 'active', 'task_path': root.task_path})
        assert publish(store, previous, winner)
        store.tasks.select_workspace_task({'thread_id': thread_id,
            'task_id': successor.task_id if workspace_choice == 'ours' else winner.task_id})
        store.threads.update_atomic(thread_id, lambda thread: replace(thread, title='并发新标题'))
        return False

    monkeypatch.setattr(promotion, '_persist_superseded_successor', competing_publish)
    assert promotion.bind_current_conversation_workspace(agent, root_id) is None
    assert len(candidates) == 1 and store.tasks.load(candidates[0]) is None
    assert store.tasks.load(root_id).superseded_by_task_id == 'concurrent-winner'
    thread = store.threads.require(thread_id)
    assert thread.active_task_ids == ('concurrent-winner',)
    assert thread.task_ids == (root_id, 'concurrent-winner') and thread.title == '并发新标题'
    assert thread.workspace_task_id == (root_id if workspace_choice == 'ours' else 'concurrent-winner')


# LLM: 故障均发生在真实持久化边界，不能靠改返回值掩盖活动孤儿；只允许未发布的新建任务被回收。
# 函数用途: 覆盖bind部分提交、同ID并发创建及回滚删除失败三个独立异常出口。
@pytest.mark.parametrize('fault', ['bind_after_write', 'bind_index', 'concurrent_create', 'rollback_unlink'])
def test_wp19_review_successor_creation_and_rollback_faults(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    root = store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    agent._current_run_params = _params(thread_id, root_id, root_id)
    successor_id = promotion._terminal_successor_task_id(store, thread_id,
        base_id=root_id, source_id=root_id, task_path=root.task_path)
    bind, load = store.tasks.bind, store.tasks.load
    update_index, unlink = store.tasks.update_thread_index, nofollow_fs.unlink_file_beneath
    publish = promotion._persist_superseded_successor
    foreign = []

    # LLM: 部分提交后异常，任务和索引均实际写过，不能只观察返回值。
    # 函数用途: 确定性复现bind异常收尾窗口。
    def bind_fault(request, **kwargs):
        result = bind(request, **kwargs)
        if fault == 'bind_after_write':
            raise OSError('bind post-write fault')
        return result

    # LLM: 存在性快照刚读到None后插入真正并发创建，保留旧快照；不伪造任务对象或写入器。
    # 函数用途: 确定性覆盖load与首次原子创建之间的归属竞争。
    def load_with_competing_create(task_id):
        result = load(task_id)
        if fault == 'concurrent_create' and task_id == successor_id and result is None and not foreign:
            foreign.append(bind({'thread_id': thread_id, 'task_id': successor_id, 'goal': '并发创建者的任务',
                'status': 'active', 'task_path': root.task_path}))
        return result

    # LLM: 任务JSON已写好但索引尚未发表时失败，测试必须检查磁盘而不只查active索引。
    # 函数用途: 注入bind内部线程索引写入失败。
    def index_fault(tid, task_id, status, current):
        if task_id == successor_id and status == 'active':
            raise OSError('bind index fault')
        return update_index(tid, task_id, status, current)

    # LLM: 只阻止新继任记录的删除，保留任务读取和退役写入能力以验证补偿路径。
    # 函数用途: 注入回滚最后一步IO失败。
    def unlink_fault(base, parts, *args, **kwargs):
        if tuple(parts) == store.storage.task_path(successor_id).relative_to(store.storage.root).parts:
            raise OSError('rollback unlink fault')
        return unlink(base, parts, *args, **kwargs)

    monkeypatch.setattr(store.tasks, 'bind', bind_fault)
    monkeypatch.setattr(store.tasks, 'load', load_with_competing_create)
    if fault == 'bind_index':
        monkeypatch.setattr(store.tasks, 'update_thread_index', index_fault)
    if fault == 'rollback_unlink':
        monkeypatch.setattr(nofollow_fs, 'unlink_file_beneath', unlink_fault)
    if fault in {'concurrent_create', 'rollback_unlink'}:
        monkeypatch.setattr(promotion, '_persist_superseded_successor', lambda *_a, **_k: False)
    assert promotion.bind_current_conversation_workspace(agent, root_id) is None
    remaining = store.tasks.load(successor_id)
    thread = store.threads.require(thread_id)
    if fault == 'concurrent_create':
        assert len(foreign) == 1 and remaining == foreign[0]
        assert successor_id in thread.active_task_ids
    elif fault == 'rollback_unlink':
        assert remaining is not None and remaining.status == 'superseded'
        assert successor_id in thread.task_ids and successor_id not in thread.active_task_ids
    else:
        assert remaining is None and successor_id not in thread.task_ids and successor_id not in thread.active_task_ids
    assert thread.workspace_task_id == root_id and store.tasks.load(root_id) == root
    monkeypatch.setattr(store.tasks, 'bind', bind)
    monkeypatch.setattr(store.tasks, 'load', load)
    monkeypatch.setattr(store.tasks, 'update_thread_index', update_index)
    monkeypatch.setattr(nofollow_fs, 'unlink_file_beneath', unlink)
    monkeypatch.setattr(promotion, '_persist_superseded_successor', publish)
    selected = promotion.bind_current_conversation_workspace(agent, root_id)
    assert selected is not None and store.tasks.load(root_id).superseded_by_task_id == selected.task_id


# LLM: 原发布器确已保存有效指针，仅让原TaskStore索引收尾持续不可用，不把辅助投影故障当作未发布。
# 函数用途: 验证同一有效继任指针已提交时，无论修索引False还是异常都返回幂等成功。
@pytest.mark.parametrize('index_error', [False, True])
def test_wp19_review_valid_pointer_remains_success_when_index_repair_fails(tmp_path, monkeypatch, index_error):
    from agent_py_agent.agent.conversation import task_promotion as promotion

    agent, thread_id, root_id = _agent_with_self_watch(tmp_path, policy_enabled=False)
    store = agent.conversation_store
    store.tasks.update_status({'task_id': root_id, 'status': 'completed'})
    agent._current_run_params = _params(thread_id, root_id, root_id)
    update = store.tasks.update_status

    # LLM: 新继任仍正常建立活动索引，故障只影响前代状态的重复收尾。
    # 函数用途: 模拟提交后索引收尾持续失败。
    def unavailable(request):
        if request['task_id'] == root_id and request['status'] == 'superseded':
            if index_error:
                raise OSError('index repair unavailable')
            return None
        return update(request)

    monkeypatch.setattr(store.tasks, 'update_status', unavailable)
    selected = promotion.bind_current_conversation_workspace(agent, root_id)
    assert selected is not None
    assert store.tasks.load(root_id).superseded_by_task_id == selected.task_id
    assert store.tasks.load(selected.task_id).status == 'active'
