"""只读发现与Count0短路；不启动宿主或创建authority。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_executor_delegation as executor
from agent_py_agent.agent.gateway_parts import multiprocess_native_host as native


@pytest.mark.parametrize(
    "module,helper,entry",
    [
        (executor, "bootstrap_argv", "multiprocess_executor_bootstrap"),
        (native, "_native_host_bootstrap_argv", "multiprocess_native_host_bootstrap"),
    ],
)
@pytest.mark.parametrize("missing", [None, SimpleNamespace(origin=None)])
def test_missing_fixed_spec_never_returns_source_fallback(
    monkeypatch, module, helper, entry, missing
):
    calls = []

    def find(name):
        calls.append(name)
        return missing

    monkeypatch.setattr(module.importlib.util, "find_spec", find)
    with pytest.raises((AttributeError, TypeError)):
        getattr(module, helper)(41)
    assert calls == ["agent_py_agent.agent.gateway_parts." + entry]


def test_count_zero_does_not_even_discover_entry(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Count0 must return before discovery or pipe")

    monkeypatch.setattr(native.importlib.util, "find_spec", reject)
    monkeypatch.setattr(native.os, "pipe", reject)
    assert (
        native.start_native_host(
            None, SimpleNamespace(partition=SimpleNamespace(count=0)), None, None
        )
        is None
    )


@pytest.mark.parametrize(
    "module,helper,entry",
    [
        (executor, "bootstrap_argv", "multiprocess_executor_bootstrap"),
        (native, "_native_host_bootstrap_argv", "multiprocess_native_host_bootstrap"),
    ],
)
def test_working_directory_cannot_redirect_fixed_entry(
    tmp_path, monkeypatch, module, helper, entry
):
    before = getattr(module, helper)(41)
    fake = tmp_path / (entry + ".py")
    fake.write_text('raise AssertionError("must not load cwd")')
    monkeypatch.chdir(tmp_path)
    after = getattr(module, helper)(41)
    assert after == before and Path(after[2]).parent == Path(module.__file__).resolve().parent
