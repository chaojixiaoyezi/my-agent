# ruff: noqa: F811
# LLM: 只观测实际冷HTTP群规则入口的固定锁出生和原P2，受控错误类型不代物理runner证明。
# 模块用途: 防止群配置锁旁路创建或将flock等待放进Root。
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.global_write_commit import GlobalWriteCommitUnavailable
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    read_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.user_space import group_authorization_context
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_coordinator_agents_domain import cold, submit


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_group_policy_lock_birth_in_owner_gate_flock_outside(cold, monkeypatch):
    identity = OwnerIdentity.provider_group("feishu", "oc_coldagents")
    home = home_paths_with_owner(
        cold.agent.home_paths, resolve_owner_home(cold.agent.home_paths.root, identity)
    )
    policy_lock = home.owner_tool_policy_json.with_name(home.owner_tool_policy_json.name + ".lock")
    held, births, flocks, policy_descriptors = [], [], [], {}
    controller = cold.server.control_plane.controller()
    launch = controller.launch_owner_writer

    def observe_gate(owner, operation):
        def entered():
            held.append(owner)
            try:
                return operation()
            finally:
                held.pop()

        return launch(owner, entered)

    original_open, original_flock = json_io._open_private_lock_descriptor, json_io._flock_descriptor

    def opened(path):
        fd = original_open(path)
        if Path(path) == policy_lock:
            births.append(tuple(held))
            policy_descriptors[fd] = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        return fd

    def flock(fd, **kwargs):
        if policy_descriptors.get(fd) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            flocks.append(tuple(held))
        return original_flock(fd, **kwargs)

    monkeypatch.setattr(controller, "launch_owner_writer", observe_gate)
    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", opened)
    monkeypatch.setattr(json_io, "_flock_descriptor", flock)
    status, result = submit(cold, "/agents show", "policy-birth-original", as_group=True)
    assert status == 200 and result["ok"], (status, result)
    assert births and set(births) == {(owner_digest(identity),)}, births
    assert flocks and set(flocks) == {()}, flocks


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_group_birth_rejection_never_opens_or_completes(cold, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )

    sources = []
    source_check = _LocalControlWriteSource._require_http_current

    def observe_source(source):
        sources.append(source)
        return source_check(source)

    monkeypatch.setattr(_LocalControlWriteSource, "_require_http_current", observe_source)
    original = group_authorization_context.locked_json_path
    error = GlobalWriteCommitUnavailable("controlled-before-policy-lock-birth")
    attempts, opened = [], []
    original_open = json_io._open_private_lock_descriptor

    def open_fd(path):
        if Path(path).name == "tool_policy.json.lock":
            opened.append(path)
        return original_open(path)

    def policy_lock(path, **kwargs):
        assert callable(kwargs.get("birth_commit")), (
            "actual C guard must pass original birth writer"
        )

        def reject(operation):
            attempts.append(operation)
            raise error

        return original(path, **(kwargs | {"birth_commit": reject}))

    monkeypatch.setattr(group_authorization_context, "locked_json_path", policy_lock)
    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", open_fd)
    status, result = submit(cold, "/agents show", "policy-birth-rejected", as_group=True)
    assert status == 503 and result["error_code"] == error.error_code, (status, result)
    assert len(attempts) == 1 and not opened
    receipt = read_gateway_control_operation(
        cold.server.paths, sources[-1].receipt.operation_id, read_only=True
    )
    assert receipt.state == "executing" and receipt.dispatch is None
    assert submit(cold, "/agents show", "policy-birth-rejected", as_group=True)[0] == 202
    assert len(attempts) == 1
