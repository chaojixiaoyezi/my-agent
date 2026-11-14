"""冻结业务上下文只读本项目事实，通用偏好与非业务兼容召回保持原规则。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.memory_search_tool import _scope_predicate
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.memory_store.recall import runtime_long_term_scope


def _context(project):
    return BusinessExecutionContext(
        "owner", "space-A", project, "task-A", 1, None, (), (), "business-context.v1"
    )


def _record(scope, key):
    return SimpleNamespace(attributes={"scope_type": scope, "scope_key": key}, kind="fact")


@pytest.mark.parametrize("project", ["project-A", None])
def test_frozen_business_scope_is_exact_and_shared_by_search(project):
    records = [
        _record("project", key)
        for key in ("project:project-A", "project:project-B", "project:task-A")
    ]
    memory = SimpleNamespace(all=lambda: records)
    attrs = {
        "project_id": "project-B",
        "memory_scopes": [{"scope_type": "project", "scope_key": "project-B"}],
    }
    scope = runtime_long_term_scope(
        memory, task_id="task-A", task_attributes=attrs, business_context=_context(project)
    )
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_id="task-A", task_attributes=attrs, business_context=_context(project)
        )
    )
    predicate = _scope_predicate(agent, memory, "")
    assert not scope.allows("project", "project:project-B")
    assert not predicate(records[1])
    assert scope.allows("project", "project:project-A") is (project is not None)
    assert predicate(records[0]) is (project is not None)
    assert predicate(records[2])
    for kind in ("global", "personal"):
        assert scope.allows(kind, kind)
        assert predicate(_record(kind, kind))


def test_nonbusiness_scope_keeps_existing_project_recall():
    record = _record("project", "project:project-B")
    memory = SimpleNamespace(
        all=lambda: [record],
        scope_pairs=lambda scope_type: (("project", "project:project-B"),)
        if scope_type == "project"
        else (),
    )
    scope = runtime_long_term_scope(memory, task_id="task-A")
    assert scope.allows("project", "project:project-B")
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(task_id="task-A", task_attributes={})
    )
    assert _scope_predicate(agent, memory, "")(_record("project", "project:project-B"))


def test_automatic_recall_receives_frozen_business_scope():
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.tests.test_memory_first_loop import _runtime_context_capture_agent

    agent = _runtime_context_capture_agent({})
    agent._current_run_params = SimpleNamespace(business_context=_context("project-A"))
    checked = []

    def search(_query, _limit, predicate):
        checked.append(True)
        assert predicate(_record("project", "project:project-A"))
        assert not predicate(_record("project", "project:project-B"))
        assert predicate(_record("personal", "personal"))
        return []

    def forbidden_all():
        raise AssertionError("冻结业务回合不应全扫其它项目范围")

    agent.memory.all = forbidden_all
    agent.memory.search_scoped = search
    loop_support._prepare_runtime_context(
        agent,
        loop_support.RuntimeContextRequest("不含项目名称的普通问题", [], False, task_id="task-A"),
    )
    assert checked == [True]
