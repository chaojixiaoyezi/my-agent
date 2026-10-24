# LLM: 沿真实Agent目录入口计数；禁止历史枚举及进程刷新，不以耗时阈值代替行为证明。
# 模块用途: 验证总览轻量、完整work只读和旧注册兼容。
# ruff: noqa: F811
import pytest

from agent_py_agent.tests.test_self_map_runtime import _bytes, _read, make_agent  # noqa: F401


# LLM: 列表失败不能被聚合器吞掉后伪装成功，计数与完整性一并断言。
# 函数用途: 证明总览不枚举历史会话、定时任务或进程。
def test_overview_never_scans_history_or_jobs(make_agent, monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent.conversation_store.threads, 'list_report', lambda **kw: pytest.fail('history scan'))
    monkeypatch.setattr(agent.scheduler_repository, 'list_jobs', lambda: pytest.fail('schedule scan'))
    value = _read(agent)
    work = next(row for row in value['sections'] if row['section'] == 'work')
    assert work['count'] is None and work['completeness'] == 'partial'
    assert work['next_read']['arguments'] == {'section': 'work'}


# LLM: 已存在的进程存储也不能让目录调用有写盘能力的生命周期读口。
# 函数用途: 完整work不刷新进程，且受控文件字节和mtime均不变。
def test_work_never_refreshes_process_registry(make_agent, monkeypatch, tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.tooling.process_registry import process_registry
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )
    from agent_py_agent.tests.test_process_session_store import _running
    agent = make_agent()
    thread = agent.conversation_store.threads.get_or_create({'channel': 'local', 'channel_conversation_id': 'process-test'})
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    owner = str(agent.home_paths.owner_home_dir)
    root = process_session_store_root(agent.tools.workspace_root, owner)
    record = _running(access_scope={'owner_id': agent.home_paths.owner_id, 'conversation_id': thread.thread_id, 'owner_home': owner},
                      execution_scope={'owner_home': owner, 'thread_id': thread.thread_id, 'root_task_id': '', 'run_id': '', 'attempt_id': ''})
    with ProcessSessionStore(root).transaction() as transaction:
        transaction.write(record)
    refresh = process_registry.list_report
    monkeypatch.setattr('agent_py_agent.agent.tooling.process_registry._process_instance_terminated', lambda *a: True)
    monkeypatch.setattr(process_registry, 'list_report', lambda *a, **kw: pytest.fail('refresh writes'))
    before = _bytes(tmp_path)
    process_before = _bytes(root)
    value = _read(agent, 'work', allowed=['self_map', 'process_session'])
    process = next(row for row in value['items'] if row['id'] == 'process:current')
    assert process['state'] == 'not_observed' and process['count'] is None
    assert _bytes(tmp_path) == before
    assert _bytes(root) == process_before
    rows, errors = refresh(store_root=root)
    assert not errors and rows[0]['status'] == 'unknown'
    assert _bytes(root) != process_before
