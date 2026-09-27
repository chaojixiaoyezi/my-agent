"""进入活动轮时快照失败也必须恢复原线程上下文；不启动模型、后台任务或真实会话。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_py_agent.agent.agent_core.runtime_mixin import current_prompt_scope
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
from agent_py_agent.agent.runtime_context import ThreadLocalAgentAttribute

_FIELDS = ("_current_user_prompt", "_current_run_params", "_current_skill_snapshot")


# LLM: 使用生产的线程属性描述符，只替换快照构造回调；测试不能靠共享 dict 模拟线程隔离。
# 类用途: 让快照准入故障、嵌套和并发恢复无需构造有落盘副作用的完整 Agent。
class ScopeAgent:
    _current_user_prompt = ThreadLocalAgentAttribute("_current_user_prompt")
    _current_run_params = ThreadLocalAgentAttribute("_current_run_params")
    _current_skill_snapshot = ThreadLocalAgentAttribute("_current_skill_snapshot")
    effective_workspace_root = "."

    # LLM: provider 仅由测试固定，不接受模型参数，也不涉及持久任务记录。
    # 函数用途: 绑定一个可返回或抛错的快照准入函数。
    def __init__(self, provider):
        self.provider = provider

    # LLM: 调用真实 current_prompt_scope 后读取它刚安装的线程参数，重现生产快照解析时机。
    # 函数用途: 将当前任务参数交给测试的只读快照构造器。
    def skill_snapshot_for_run_scope(self, _workspace):
        return self.provider(self._current_run_params)


@pytest.mark.parametrize("existing", [False, True])
def test_snapshot_admission_failure_restores_existing_or_removes_new_context(existing):
    def reject(_params):
        raise SkillSnapshotError("SKILL_SNAPSHOT_STALE")

    agent = ScopeAgent(reject)
    previous = ("old prompt", object(), object())
    if existing:
        for name, value in zip(_FIELDS, previous, strict=True):
            setattr(agent, name, value)
    with pytest.raises(SkillSnapshotError, match="STALE"):
        with current_prompt_scope(agent, "new prompt", object()):
            pytest.fail("过期快照不应进入正文")
    if existing:
        assert tuple(getattr(agent, name) for name in _FIELDS) == previous
    else:
        assert all(not hasattr(agent, name) for name in _FIELDS)


def test_nested_entry_failure_keeps_outer_snapshot_and_final_exit_cleans_up():
    outer_params, outer_snapshot = object(), object()

    def resolve(params):
        if params is outer_params:
            return outer_snapshot
        raise SkillSnapshotError("SKILL_TASK_REFERENCE_UNAVAILABLE")

    agent = ScopeAgent(resolve)
    with current_prompt_scope(agent, "outer", outer_params):
        with pytest.raises(SkillSnapshotError):
            with current_prompt_scope(agent, "inner", object()):
                pytest.fail("故障内层不应执行")
        assert (agent._current_user_prompt, agent._current_run_params, agent._current_skill_snapshot) == (
            "outer", outer_params, outer_snapshot)
    assert all(not hasattr(agent, name) for name in _FIELDS)


def test_parallel_admission_failure_cannot_change_another_threads_context():
    entered, exited = Barrier(2), Barrier(2)

    def resolve(params):
        entered.wait(timeout=5)
        if params == "stale":
            raise SkillSnapshotError("SKILL_SNAPSHOT_STALE")
        return "fresh snapshot"

    agent = ScopeAgent(resolve)

    def run_stale():
        with pytest.raises(SkillSnapshotError):
            with current_prompt_scope(agent, "stale prompt", "stale"):
                pytest.fail("故障线程不应执行")
        assert all(not hasattr(agent, name) for name in _FIELDS)
        exited.wait(timeout=5)

    def run_fresh():
        with current_prompt_scope(agent, "fresh prompt", "fresh"):
            exited.wait(timeout=5)
            assert (agent._current_user_prompt, agent._current_run_params, agent._current_skill_snapshot) == (
                "fresh prompt", "fresh", "fresh snapshot")
        assert all(not hasattr(agent, name) for name in _FIELDS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_stale), pool.submit(run_fresh)]
        for future in futures:
            future.result(timeout=10)
