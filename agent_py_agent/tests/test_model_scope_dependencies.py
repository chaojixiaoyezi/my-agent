"""原模型作用域的显式候选依赖：同 Agent、无热修改、嵌套恢复与并发隔离。"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from agent_py_agent.agent.settings.model_profiles import selected_model_config
from agent_py_agent.agent.settings.model_scope import (
    active_thread_model_name,
    model_dependencies_scope,
    prepare_model_dependencies,
    selected_model_scope,
)
from agent_py_agent.tests.test_model_profiles import Host, add


def test_explicit_candidate_dependencies_share_original_scope_and_restore(tmp_path):
    host = Host(tmp_path)
    candidate, _ = add(host, model_name="candidate")
    defaults = host.config, host.backend, host.prompts
    with selected_model_scope(host, inherited=True, thread_id="child"):
        inherited = host.config, host.backend, host.prompts
        dependencies = prepare_model_dependencies(host, selected_model_config(host, profile_id=candidate), inherited=True)
        assert (host.config, host.backend, host.prompts) == inherited
        with model_dependencies_scope(host, dependencies, thread_id="child", active=True):
            assert host.config is dependencies.config
            assert host.backend.model_name == host.config.model_name == "candidate"
            assert host.prompts.config is host.config
            assert active_thread_model_name(host, "child") == "candidate"
            candidate_objects = host.config, host.backend, host.prompts
            # 原隐式 scope 仍不重选；模型线程复制上下文后读到精确候选对象。
            with selected_model_scope(host):
                assert (host.config, host.backend, host.prompts) == candidate_objects
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(copy_context().run, lambda: host.backend).result() is candidate_objects[1]
        assert (host.config, host.backend, host.prompts) == inherited
        assert active_thread_model_name(host, "child") == inherited[0].model_name
    assert (host.config, host.backend, host.prompts) == defaults
    assert active_thread_model_name(host, "child") == ""


def test_candidate_scope_exception_does_not_publish_or_hot_mutate(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    original = dict(host.__dict__)
    dependencies = prepare_model_dependencies(host, selected_model_config(host, profile_id=key))
    with pytest.raises(InterruptedError), model_dependencies_scope(host, dependencies, thread_id="child"):
        assert active_thread_model_name(host, "child") == ""
        raise InterruptedError("cancel")
    for field in ("config", "backend", "prompts"):
        assert host.__dict__[field] is original[field]


def test_candidate_dependencies_reject_another_agent(tmp_path):
    host = Host(tmp_path / "a")
    other = Host(tmp_path / "b")
    dependencies = prepare_model_dependencies(host, host.config)
    with pytest.raises(ValueError), model_dependencies_scope(other, dependencies):
        pytest.fail("different identity must not bind")


def test_candidate_dependencies_keep_original_backend_cache(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    first = prepare_model_dependencies(host, selected_model_config(host, profile_id=key))
    second = prepare_model_dependencies(host, selected_model_config(host, profile_id=key))
    assert dict(first.values)["backend"] is dict(second.values)["backend"]


def test_activated_dependencies_live_through_finalization_and_restore_on_exception(tmp_path):
    from agent_py_agent.agent.backends.request_scope import foreground_model_active
    from agent_py_agent.agent.settings.model_scope import (
        activate_model_dependencies,
        model_dependency_lifetime,
    )

    host = Host(tmp_path)
    key, _ = add(host, model_name="candidate")
    defaults = host.config, host.backend, host.prompts
    with selected_model_scope(host, inherited=True):
        with pytest.raises(InterruptedError), model_dependency_lifetime(host):
            dependencies = prepare_model_dependencies(host, selected_model_config(host, profile_id=key))
            activate_model_dependencies(host, dependencies, thread_id="child")
            assert host.config.model_name == "candidate"
            assert foreground_model_active(host.backend)
            selected_backend = host.backend
            # 同片的后续模型轮/工具/收口都在相同生命周期内读取这批依赖。
            assert host.prompts.config is dependencies.config
            raise InterruptedError("cancel during finalization")
        assert not foreground_model_active(selected_backend)
    assert (host.config, host.backend, host.prompts) == defaults


def test_io_thread_cannot_own_parent_lifetime_tokens(tmp_path):
    from agent_py_agent.agent.settings.model_scope import (
        activate_model_dependencies,
        model_dependency_lifetime,
    )

    host = Host(tmp_path)
    dependencies = prepare_model_dependencies(host, host.config)
    with model_dependency_lifetime(host), ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError):
            pool.submit(copy_context().run, activate_model_dependencies, host, dependencies).result()
