"""只核原parent/versions FD的C退出优先级；真实scope自然结束/真实close，不发网络模型。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import persona_storage_io as storage
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.tests.test_coordinator_jsonl_write_ports import actual_errors
from agent_py_agent.tests.test_persona_coordinator_io import Door


# LLM: 只观测原严格打开得到的FD，门和实际close仍原实现；不构造来源或借错误码授信。
# 函数用途: 保留两个原上下文的确切FD，供关一次和不抢primary断言。
def enter_context(root, kind, coordinator_io, monkeypatch):
    targets = []
    directory = storage.open_directory_beneath
    opened = storage._openat_file

    def directories(*args, **kwargs):
        descriptor = directory(*args, **kwargs)
        targets.append(descriptor)
        return descriptor

    def files(*args, **kwargs):
        descriptor = opened(*args, **kwargs)
        targets.append(descriptor)
        return descriptor

    monkeypatch.setattr(storage, "open_directory_beneath", directories)
    monkeypatch.setattr(storage, "_openat_file", files)
    context = (
        storage.parent_directory(root / "anchor", create=True, coordinator_io=coordinator_io)
        if kind == "parent"
        else storage.versions_lock(root / "versions.jsonl", coordinator_io=coordinator_io)
    )
    with global_write_commit_scope(Door()):
        context.__enter__()
    return context, targets


# LLM: 每个目标先真实close再报次生OSError，只观测原FD；没有retry、重开或补偿写。
# 函数用途: 证明资源确实归还，而且每个FD恰好关闭一次。
def secondary_closes(targets, monkeypatch):
    close = os.close
    counts = {}
    errors = []

    def closing(descriptor):
        close(descriptor)
        if descriptor in targets:
            counts[descriptor] = counts.get(descriptor, 0) + 1
            error = OSError("secondary original FD close")
            errors.append(error)
            raise error

    monkeypatch.setattr(os, "close", closing)
    return counts, errors


@pytest.mark.parametrize("kind", ["parent", "versions"])
@pytest.mark.parametrize("error", actual_errors(), ids=lambda error: type(error).__name__)
def test_c_close_preserves_same_native_outer_cause_context_and_closes_once(
    tmp_path, monkeypatch, kind, error
):
    context, targets = enter_context(tmp_path, kind, True, monkeypatch)
    counts, _errors = secondary_closes(targets, monkeypatch)
    cause = OSError("original physical cause")
    previous = LookupError("original context")
    try:
        try:
            raise previous
        except LookupError:
            raise error from cause
    except type(error) as active:
        before = (active, active.__cause__, active.__context__)
        assert context.__exit__(type(active), active, active.__traceback__) is False
        assert (active, active.__cause__, active.__context__) == before
    assert counts == dict.fromkeys(targets, 1)
    assert len(targets) == (1 if kind == "parent" else 2)
    for descriptor in targets:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("kind", ["parent", "versions"])
@pytest.mark.parametrize("coordinator_io", [False, True])
@pytest.mark.parametrize(
    "primary", [None, OSError("ordinary body failure"), ValueError("ordinary conflict")]
)
def test_default_and_ordinary_close_errors_keep_original_priority(
    tmp_path, monkeypatch, kind, coordinator_io, primary
):
    context, targets = enter_context(tmp_path, kind, coordinator_io, monkeypatch)
    counts, errors = secondary_closes(targets, monkeypatch)
    with pytest.raises(OSError) as caught:
        context.__exit__(type(primary) if primary else None, primary, None)
    assert caught.value is errors[-1]
    assert counts == dict.fromkeys(targets, 1)


@pytest.mark.parametrize("kind", ["parent", "versions"])
def test_default_false_does_not_swallow_native_primary_close_error(tmp_path, monkeypatch, kind):
    context, targets = enter_context(tmp_path, kind, False, monkeypatch)
    counts, errors = secondary_closes(targets, monkeypatch)
    native = GlobalWriteCommitUnavailable("controlled original class")
    with pytest.raises(OSError) as caught:
        context.__exit__(type(native), native, None)
    assert caught.value is errors[-1] and caught.value is not native
    assert counts == dict.fromkeys(targets, 1)


@pytest.mark.parametrize("operation", ["open", "transfer", "remove"])
def test_real_c_callers_transport_close_policy_after_scope_naturally_exits(
    tmp_path, monkeypatch, operation
):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    source = left / "source"
    source.write_bytes(b"original input")
    destination = right / "destination"
    targets = []
    original_open = storage.open_directory_beneath
    execute = storage.execute_global_write
    observed = []

    def directories(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        targets.append(descriptor)
        return descriptor

    def actual_guard(*args, **kwargs):
        try:
            return execute(*args, **kwargs)
        except GlobalWriteCommitUnavailable as error:
            observed.append((error, error.__cause__, error.__context__))
            raise

    monkeypatch.setattr(storage, "open_directory_beneath", directories)
    monkeypatch.setattr(storage, "execute_global_write", actual_guard)
    counts, _errors = secondary_closes(targets, monkeypatch)
    with global_write_commit_scope(Door()):
        pass
    with pytest.raises(GlobalWriteCommitUnavailable) as caught:
        if operation == "open":
            storage.open_file(
                destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, coordinator_io=True
            )
        elif operation == "transfer":
            storage.transfer(source, destination, coordinator_io=True)
        else:
            storage.remove_private(source, coordinator_io=True)
    assert (caught.value, caught.value.__cause__, caught.value.__context__) == observed[0]
    assert counts == dict.fromkeys(targets, 1)
    assert len(targets) == (2 if operation == "transfer" else 1)
    assert source.read_bytes() == b"original input" and not destination.exists()


def test_single_fd_helper_no_primary_never_swallows_ordinary_close_error(tmp_path, monkeypatch):
    path = tmp_path / "held"
    path.write_bytes(b"data")
    descriptor = os.open(Path(path), os.O_RDONLY)
    counts, errors = secondary_closes([descriptor], monkeypatch)
    with pytest.raises(OSError) as caught:
        storage._close_storage_descriptor(descriptor, None)
    assert caught.value is errors[0] and counts == {descriptor: 1}


def test_actual_storage_and_scope_origin_is_canonical_package():
    import agent_py_agent
    from agent_py_agent.agent.common import global_write_commit

    root = Path(agent_py_agent.__file__).resolve().parent
    for module, relative in (
        (storage, Path("agent/capability/persona_storage_io")),
        (global_write_commit, Path("agent/common/global_write_commit")),
    ):
        origin = Path(module.__file__).resolve()
        assert origin.is_file()
        assert origin.suffix in {".py", ".pyc"}
        assert origin.with_suffix("") == root / relative
        assert origin == Path(module.__spec__.origin).resolve()
    assert storage.GlobalWriteCommitUnavailable is global_write_commit.GlobalWriteCommitUnavailable
