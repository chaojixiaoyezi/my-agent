# LLM: 共享 reader 只在原授权快照和 task pin 上工作；组件测试不启动模型、Gateway 或包内脚本。
# 模块用途: 守住工具原回执、宿主有界正文、撤销和停止后的零读取或零 pin 边界。
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import task_references
from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
from agent_py_agent.agent.capability.package_read import PackagePageChecks
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotError
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_capability_activation import content_activation_fixture
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_task_refs import _agent, _bind_main_task


# LLM: 只构造调用方已裁剪的快照，不提供 owner 参数或全局服务回退；reads 由包夹具记录。
# 函数用途: 为 reader 与原工具提供同一宿主配置及当前不可变包范围。
def _reader_agent(*packages, max_chars=4, preview_chars=20):
    snapshot = SkillSnapshot((), (), "reader-test", "local/main", "", tuple(packages))
    return SimpleNamespace(current_skill_snapshot=lambda: snapshot,
                           config=AgentConfig(tool_read_max_chars=max_chars,
                                              tool_output_preview_chars=preview_chars)), snapshot


# LLM: 仅在测试外层观察 pin 并继续原实现；缺失或取消检查必须真实抛出，不伪造持久成功。
# 函数用途: 证明正文错误和迟到取消不会固定新引用。
def _pins(monkeypatch):
    seen = []
    original = task_references.pin_package_reference

    def observe(agent, reference, **kwargs):
        seen.append(dict(reference))
        return original(agent, reference, **kwargs)

    monkeypatch.setattr(task_references, "pin_package_reference", observe)
    return seen


# 基线为 f88e709f 的真实 get/search 回执与归档 envelope，避免抽取时悄悄改字段或分页协议。
@pytest.mark.parametrize("preview,action,output_sha,envelope_sha", [
    (0, "get", "1fb0c10880a64a6b0c546c382b2fd9cce8088a3eb928fe45152e5a39c333af95",
     "b59b747b8d785cfc17bd0b5f144b25874a20a87756944d6c41324cf9e4b5e526"),
    (20, "get", "1fb0c10880a64a6b0c546c382b2fd9cce8088a3eb928fe45152e5a39c333af95",
     "7459d9e45ee059d030f41eb73905bcf724c68c1bd98f8e1191af9638ee9867a3"),
    (100000, "get", "1fb0c10880a64a6b0c546c382b2fd9cce8088a3eb928fe45152e5a39c333af95",
     "557b67c00f847c7bba73495a9ae38c7d9a41e7b53133cf94b797a0729fead3e5"),
    (0, "search", "900c5658dd9438b046b278fce0278f9e1b307e8926a51f96887ee4cbf435c46b",
     "6f32b85dba249c1e8f97bcc7d0d7df59ec9cf15f9940a3eaf134d5d3c308337c"),
    (20, "search", "900c5658dd9438b046b278fce0278f9e1b307e8926a51f96887ee4cbf435c46b",
     "6f32b85dba249c1e8f97bcc7d0d7df59ec9cf15f9940a3eaf134d5d3c308337c"),
    (100000, "search", "900c5658dd9438b046b278fce0278f9e1b307e8926a51f96887ee4cbf435c46b",
     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
])
def test_original_tool_receipt_schema_and_archive_envelope_bytes_unchanged(
    preview, action, output_sha, envelope_sha,
):
    package = package_fixture(files={"CAPABILITY.md": "甲🙂\r\n乙中尾\n".encode(),
                                     "methods/detail.md": b"PRIVATE"})
    agent, _ = _reader_agent(package, preview_chars=preview)
    result = SkillSearchTool(agent).execute({"action": action, "package_id": package.package_id, "limit": 1})
    assert result.ok
    assert hashlib.sha256(result.output.encode()).hexdigest() == output_sha
    envelope = json.dumps(result.result_envelope, ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(envelope).hexdigest() == envelope_sha
    assert SkillSearchTool.model_spec.schema_hash == "sha256:0a7473248b33a02630164137b698e091d10232d89020e28f5a5171d2a5febd99"


def test_shared_entry_is_paginated_exact_text_without_following_links_or_rewriting_bytes(monkeypatch):
    from agent_py_agent.agent.capability.package_read import read_package_page

    body = "甲🙂\r\n乙中尾\n另见 methods/detail.md，不自动打开。"
    reads = []
    package = package_fixture(files={"CAPABILITY.md": body.encode(), "methods/detail.md": b"PRIVATE"}, reads=reads)
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    params = {"checks": PackagePageChecks(expected_package_sha256=package.package_sha256,
              expected_activation_id=package.activation_id), "max_chars": 100}
    page = read_package_page(agent, snapshot, package.package_id, **params)
    assert page["body"] == "甲🙂\r\n" and page["total_chars"] == len(body) and page["has_more"]
    assert page["source_ref"] == {**package.to_ref(), "resource_path": "CAPABILITY.md",
                                  "resource_sha256": hashlib.sha256(body.encode()).hexdigest()}
    assert len(page["source_ref"]) == 9 and page["continuation"]["max_chars"] == 4
    chunks = [page["body"]]
    while page["has_more"]:
        continuation = dict(page["continuation"])
        assert continuation.pop("action") == "get"
        continuation["checks"] = PackagePageChecks(
            expected_package_sha256=continuation.pop("expected_package_sha256"),
            expected_activation_id=continuation.pop("expected_activation_id"),
        )
        page = read_package_page(agent, snapshot, **continuation)
        chunks.append(page["body"])
    assert "".join(chunks).encode() == body.encode()
    assert set(reads) == {"CAPABILITY.md"} and len(pins) == len(reads)


@pytest.mark.parametrize("field", ["expected_package_sha256", "expected_activation_id"])
def test_old_generation_is_rejected_without_any_member_read_or_pin(monkeypatch, field):
    from agent_py_agent.agent.capability.package_read import read_package_page

    reads = []
    package = package_fixture(reads=reads)
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    with pytest.raises(SkillSnapshotError, match="SKILL_SNAPSHOT_STALE"):
        read_package_page(agent, snapshot, package.package_id, checks=PackagePageChecks(**{field: "f" * 64}))
    assert reads == pins == []


def test_host_cannot_expand_scoped_snapshot_or_fall_back_to_agent_catalog(monkeypatch):
    from agent_py_agent.agent.capability.package_read import read_package_page

    reads = []
    package = package_fixture(reads=reads)
    agent, broad = _reader_agent(package)
    scoped = replace(broad, packages=())
    pins = _pins(monkeypatch)
    with pytest.raises(SkillSnapshotError, match="CAPABILITY_PACKAGE_NOT_AVAILABLE"):
        read_package_page(agent, scoped, package.package_id)
    assert reads == pins == []


@pytest.mark.parametrize("when", ["before", "after_read", "before_pin"])
def test_execution_authority_is_checked_before_read_after_read_and_before_pin(monkeypatch, when):
    from agent_py_agent.agent.capability.package_read import read_package_page

    reads, checks = [], []
    package = package_fixture(reads=reads)
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    failure_check = {"before": 1, "after_read": 2, "before_pin": 3}[when]

    def check():
        checks.append(len(reads))
        if len(checks) == failure_check:
            raise PermissionError("execution authority revoked")

    with pytest.raises(PermissionError, match="authority revoked"):
        read_package_page(agent, snapshot, package.package_id, checks=PackagePageChecks(check))
    assert reads == ([] if when == "before" else ["CAPABILITY.md"])
    assert pins == []


@pytest.mark.parametrize("cancel_before", [True, False])
def test_original_bound_cancellation_stops_read_or_late_pin(monkeypatch, cancel_before):
    from agent_py_agent.agent.capability.package_read import read_package_page

    token = CancellationToken()
    reads = []
    package = package_fixture(reads=reads)
    reader = package.reader

    def cancelling_reader(path):
        content = reader(path)
        token.cancel("stop")
        return content

    package = replace(package, reader=cancelling_reader)
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    if cancel_before:
        token.cancel("stop")
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        read_package_page(agent, snapshot, package.package_id)
    assert reads == ([] if cancel_before else ["CAPABILITY.md"]) and pins == []


@pytest.mark.parametrize("case", ["corrupt", "binary", "undeclared"])
def test_member_validation_fails_without_pin_or_private_body_delivery(monkeypatch, case):
    from agent_py_agent.agent.capability.package_read import read_package_page

    package = package_fixture(files={"CAPABILITY.md": b"\xff"} if case == "binary" else None)
    if case == "corrupt":
        package = replace(package, reader=lambda _: b"corrupt")
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    with pytest.raises(SkillSnapshotError):
        read_package_page(agent, snapshot, package.package_id,
                          resource_path="../CAPABILITY.md" if case == "undeclared" else "")
    assert pins == []


def test_real_installation_revocation_invalidates_original_host_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.package_read import read_package_page

    owner, store, _, enable = content_activation_fixture(tmp_path)
    active = store.change_activation(enable).installation
    package = enabled_capability_packages(owner)[0]
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    store.change_activation(PluginActivationRequest("revoke", active.revision,
                                                    replace(active.activation, phase="revoked")))
    with pytest.raises(SkillSnapshotError):
        read_package_page(agent, snapshot, package.package_id)
    assert pins == []


@pytest.mark.parametrize("arguments", [
    {"offset": True}, {"offset": -1}, {"offset": "0"}, {"offset": 99999},
    {"max_chars": True}, {"max_chars": 0}, {"max_chars": -1}, {"max_chars": "4"},
])
def test_host_page_arguments_fail_without_pinning(monkeypatch, arguments):
    from agent_py_agent.agent.capability.package_read import read_package_page

    package = package_fixture()
    agent, snapshot = _reader_agent(package)
    pins = _pins(monkeypatch)
    with pytest.raises(ValueError):
        read_package_page(agent, snapshot, package.package_id, **arguments)
    assert pins == []


def test_host_read_rechecks_inside_original_task_lock_and_pins_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.package_read import read_package_page

    agent, _, _ = _agent(tmp_path)
    _bind_main_task(agent)
    snapshot = agent.current_skill_snapshot()
    package = snapshot.resolve_package("story-a")
    tasks = agent.conversation_store.tasks
    original_guard = tasks.transition_guard
    inside, checks = [], []

    @contextmanager
    def observe_guard(task_id):
        with original_guard(task_id):
            inside.append(task_id)
            try:
                yield
            finally:
                inside.pop()

    monkeypatch.setattr(tasks, "transition_guard", observe_guard)
    before = tasks.load("main-task")
    page = read_package_page(agent, snapshot, package.package_id, max_chars=3,
                             checks=PackagePageChecks(lambda: checks.append(bool(inside))))
    after = tasks.load("main-task")
    assert checks[-1] is True and any(not value for value in checks)
    assert list(after.skill_snapshot_refs) == [package.to_ref()]
    assert replace(after, skill_snapshot_refs=before.skill_snapshot_refs) == before
    assert page["body"] == "# s" and page["has_more"]
    read_package_page(agent, snapshot, package.package_id,
                      checks=PackagePageChecks(lambda: checks.append(bool(inside))))
    assert tasks.load("main-task") == after


def test_cancel_while_entering_original_pin_lock_cannot_commit_reference(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.package_read import read_package_page

    agent, _, _ = _agent(tmp_path)
    _bind_main_task(agent)
    snapshot = agent.current_skill_snapshot()
    tasks = agent.conversation_store.tasks
    original_guard = tasks.transition_guard
    token = CancellationToken()

    @contextmanager
    def stopped_guard(task_id):
        with original_guard(task_id):
            token.cancel("late stop before task updater")
            yield

    monkeypatch.setattr(tasks, "transition_guard", stopped_guard)
    before = tasks.load("main-task")
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        read_package_page(agent, snapshot, "story-a", checks=PackagePageChecks(lambda: None))
    assert tasks.load("main-task") == before and not before.skill_snapshot_refs


def test_original_tool_executor_denial_keeps_handler_and_shared_reader_unentered(tmp_path, monkeypatch):
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    reads = []
    package = package_fixture(reads=reads)
    agent, _ = _reader_agent(package)
    pins = _pins(monkeypatch)
    execution = execute_canonical_test_call(
        tmp_path, tools={"skill_search": SkillSearchTool(agent)}, tool_name="skill_search",
        arguments={"action": "get", "package_id": package.package_id}, allowed_tools=[],
    )
    assert not execution.result.ok and not execution.result.handler_executed
    assert execution.result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
    assert reads == pins == []
