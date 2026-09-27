# LLM: 验证原包检索/读取的地址与错误恢复；仅当前授权同代的未声明路径给入口建议，真实读取失败和取消不得降级，组件不联网。
# 模块用途: 守住包成员的地址空间、同代导航和原任务引用，建议须再次显式读取，不替真实任务交付。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_discovery import (
    discovery_fixture,
    package_fixture,
)
from agent_py_agent.tests.test_capability_package_native_pipeline import _PackagePipelineBackend
from agent_py_agent.tests.test_capability_package_runtime_binding import _execute, _unpromoted_turn
from agent_py_agent.tests.test_capability_package_selector_recovery import _observe_pins
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 只检查宿主返回的结构化声明与可重用参数，不把正文中的相对路径推断为包成员。
# 函数用途: 统一核对 get/search 都携带同代、限页的资源定位信息。
def _assert_navigation(payload, package):
    assert payload["resource_namespace"] == {
        "kind": "capability_package", "path_base": "package_root", "declared_members_only": True,
        "filesystem_path": False, "reader_tool": "skill_search", "path_parameter": "resource_path",
    }
    assert payload["next_search"] == {
        "action": "search", "package_id": package.package_id, "offset": 0, "limit": 5,
        "expected_package_sha256": package.package_sha256, "expected_activation_id": package.activation_id,
    }
    assert "已声明" in payload["resource_access_hint"] and "业务" in payload["resource_access_hint"]
    assert "write_file.source_ref" in payload["resource_copy_hint"]


@pytest.mark.parametrize("action", ["get", "search"])
def test_scoped_navigation_is_reusable_without_reading_or_reclassifying_business_paths(
    tmp_path, skill_catalog_factory, monkeypatch, action,
):
    reads = []
    body = "读取 methods/review.md；业务输入在 inputs/story.json，交付在 output/result.json。"
    files = {"CAPABILITY.md": body.encode(), **{f"methods/{i:02d}.md": b"private" for i in range(9)}}
    package = package_fixture(files=files, reads=reads)
    _, _, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    agent.config = replace(agent.config, tool_read_max_chars=5000)
    pins = _observe_pins(monkeypatch)
    result = tool.execute({"action": action, "package_id": package.package_id, "limit": 1})
    assert result.ok
    payload = json.loads(result.output)
    _assert_navigation(payload, package)
    if action == "get":
        assert payload["body"] == body
        assert set(payload["source_ref"]) == {
            "kind", "stable_id", "name", "source", "content_sha256", "package_id", "activation_id",
            "resource_path", "resource_sha256",
        }
        assert "methods/09.md" not in result.output
    assert reads == (["CAPABILITY.md"] if action == "get" else [])
    before = (list(reads), list(pins))
    arguments = deepcopy(payload["next_search"])
    listed = tool.execute(arguments)
    assert listed.ok and arguments == payload["next_search"]
    page = json.loads(listed.output)
    assert len(page["matches"]) == 5 and page["has_more"] is True
    assert page["continuation"]["offset"] == 5 and page["continuation"]["limit"] == 5
    second = json.loads(tool.execute(page["continuation"]).output)
    assert second["offset"] == 5 and second["has_more"] is False
    assert {x["resource_path"] for x in page["matches"]}.isdisjoint(x["resource_path"] for x in second["matches"])
    assert (reads, pins) == before


@pytest.mark.parametrize("field", ["expected_package_sha256", "expected_activation_id"])
def test_navigation_rejects_changed_generation_without_read_pin_or_disclosure(
    tmp_path, skill_catalog_factory, monkeypatch, field,
):
    reads = []
    package = package_fixture(reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    navigation = json.loads(tool.execute({"action": "search", "package_id": package.package_id}).output)["next_search"]
    changed = replace(package, **{"package_sha256" if field == "expected_package_sha256" else "activation_id": "0" * 64})
    agent.current_skill_snapshot = lambda: replace(snapshot, packages=(changed,))
    outcome = tool.execute(navigation)
    assert not outcome.ok and outcome.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    payload = json.loads(outcome.output)
    assert "next_search" not in payload and "resource_namespace" not in payload
    assert "matches" not in payload and "body" not in payload and reads == pins == []


@pytest.mark.parametrize("action", ["get", "search"])
def test_ungranted_or_unknown_package_cannot_obtain_navigation(
    tmp_path, skill_catalog_factory, monkeypatch, action,
):
    reads = []
    visible, hidden = package_fixture("visible", reads=reads), package_fixture("hidden", reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [visible, hidden])
    agent.current_skill_snapshot = lambda: snapshot.restricted([visible.stable_id], expected_refs=[visible.to_ref()])
    pins = _observe_pins(monkeypatch)
    for package_id in ("hidden", "missing"):
        arguments = {"action": action, "package_id": package_id}
        if action == "get":
            arguments["resource_path"] = "SKILL.md"
        original = deepcopy(arguments)
        outcome = tool.execute(arguments)
        assert not outcome.ok
        assert json.loads(outcome.output) == {"error": "CAPABILITY_PACKAGE_NOT_AVAILABLE"}
        assert outcome.error_code == "SKILL_SNAPSHOT_UNAVAILABLE" and arguments == original
    assert reads == pins == []


@pytest.mark.parametrize("resource_path,entry_document,explicit_generation", [
    ("SKILL.md", "CAPABILITY.md", False),
    ("methods/missing.md", "docs/entry.txt", True),
    ("inputs/story.json", "CAPABILITY.md", True),
])
def test_undeclared_member_suggests_only_same_generation_entry_without_read_or_pin(
    tmp_path, skill_catalog_factory, monkeypatch, resource_path, entry_document, explicit_generation,
):
    reads = []
    files = {"CAPABILITY.md": b"default entry", "docs/entry.txt": b"declared alternative entry",
             "methods/private.md": b"private body"}
    package = replace(package_fixture(files=files, reads=reads), entry_document=entry_document)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    agent.config = replace(agent.config, tool_read_max_chars=5000)
    agent.current_skill_snapshot = lambda: snapshot.restricted(
        [package.stable_id], expected_refs=[package.to_ref()],
    )
    pins = _observe_pins(monkeypatch)
    expected = {"action": "get", "package_id": package.package_id,
                "expected_package_sha256": package.package_sha256,
                "expected_activation_id": package.activation_id}
    arguments = {"action": "get", "package_id": package.package_id, "resource_path": resource_path}
    if explicit_generation:
        arguments.update(expected)
    original = deepcopy(arguments)
    failed = tool.execute(arguments)
    assert not failed.ok and failed.error_code == "TOOL_INVALID_ARGUMENTS"
    payload = json.loads(failed.output)
    assert payload["error"] == "CAPABILITY_RESOURCE_NOT_AVAILABLE"
    assert payload["next_read"] == expected and "resource_path" not in payload["next_read"]
    assert "body" not in payload and "source_ref" not in payload and "matches" not in payload
    assert arguments == original and reads == pins == []
    retry = deepcopy(payload["next_read"])
    read = tool.execute(retry)
    assert read.ok, read.output
    assert retry == expected and json.loads(read.output)["body"] == files[entry_document].decode()
    assert reads == [entry_document] and pins == [package.to_ref()]


def test_undeclared_member_executor_recovery_repairs_arguments_then_pins_original_task(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path)
    thread, params = _unpromoted_turn(agent)
    package = agent.current_skill_snapshot().resolve_package("story-a")
    reference = package.to_ref()
    pins = _observe_pins(monkeypatch)
    arguments = {"action": "get", "package_id": "story-a", "resource_path": "SKILL.md"}
    original = deepcopy(arguments)
    failed = _execute(agent, params, arguments, "wrong-package-member")
    assert not failed.ok and failed.error_code == "TOOL_INVALID_ARGUMENTS"
    assert failed.recommended_action == "repair_tool_arguments"
    payload = json.loads(failed.output)
    assert payload["error"] == "CAPABILITY_RESOURCE_NOT_AVAILABLE"
    assert arguments == original and pins == []
    # 失败 get 可以沿原策略晋升任务；本次只要求它不固定任何包引用。
    assert all(not task.skill_snapshot_refs for task in agent.conversation_store.tasks.list(thread.thread_id))
    retry = deepcopy(payload["next_read"])
    expected = {"action": "get", "package_id": package.package_id,
                "expected_package_sha256": package.package_sha256,
                "expected_activation_id": package.activation_id}
    assert retry == expected
    read = _execute(agent, params, retry, "read-suggested-entry")
    assert read.ok, read.output
    result = json.loads(read.output)
    assert result["body"] == "# story-a" and result["source_ref"]["resource_path"] == package.entry_document
    assert retry == expected and pins == [reference]
    task = agent.conversation_store.tasks.load(params.task_attributes["conversation_task_id"])
    assert task.thread_id == thread.thread_id and task.skill_snapshot_refs == (reference,)


@pytest.mark.parametrize("field", ["package_sha256", "activation_id"])
def test_undeclared_member_with_old_generation_cannot_suggest_same_named_new_entry(
    tmp_path, skill_catalog_factory, monkeypatch, field,
):
    reads = []
    old = package_fixture(reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [old])
    agent.current_skill_snapshot = lambda: replace(snapshot, packages=(replace(old, **{field: "0" * 64}),))
    pins = _observe_pins(monkeypatch)
    arguments = {"action": "get", "package_id": old.package_id, "resource_path": "SKILL.md",
                 "expected_package_sha256": old.package_sha256, "expected_activation_id": old.activation_id}
    original = deepcopy(arguments)
    failed = tool.execute(arguments)
    assert not failed.ok and failed.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert json.loads(failed.output) == {"error": "SKILL_SNAPSHOT_STALE"}
    assert arguments == original and reads == pins == []


@pytest.mark.parametrize("failure", ["missing_bytes", "wrong_digest"])
def test_declared_member_reader_failure_is_not_wrong_path_recovery(
    tmp_path, skill_catalog_factory, monkeypatch, failure,
):
    reader = (
        Mock(side_effect=FileNotFoundError("declared member bytes missing"))
        if failure == "missing_bytes" else Mock(return_value=b"changed")
    )
    package = replace(package_fixture(), reader=reader)
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    arguments = {"action": "get", "package_id": package.package_id, "resource_path": package.entry_document}
    original = deepcopy(arguments)
    failed = tool.execute(arguments)
    assert not failed.ok and failed.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert json.loads(failed.output) == {"error": f"CAPABILITY_RESOURCE_UNAVAILABLE package={package.package_id}"}
    reader.assert_called_once_with(package.entry_document)
    assert arguments == original and pins == []


def test_package_reader_interruption_propagates_without_argument_recovery(
    tmp_path, skill_catalog_factory, monkeypatch,
):
    interruption = InterruptedError("reader interrupted")
    reader = Mock(side_effect=interruption)
    package = replace(package_fixture(), reader=reader)
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    arguments = {"action": "get", "package_id": package.package_id}
    original = deepcopy(arguments)
    with pytest.raises(InterruptedError) as caught:
        tool.execute(arguments)
    assert caught.value is interruption and arguments == original and pins == []
    reader.assert_called_once_with(package.entry_document)


def test_cancelled_undeclared_member_preserves_original_cancellation_without_read_or_pin(
    tmp_path, skill_catalog_factory, monkeypatch,
):
    reads = []
    package = package_fixture(reads=reads)
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    token = CancellationToken()
    token.cancel("cancelled-before-package-get")
    arguments = {"action": "get", "package_id": package.package_id, "resource_path": "SKILL.md"}
    original = deepcopy(arguments)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled, match="cancelled-before-package-get"):
        tool.execute(arguments)
    assert arguments == original and reads == pins == []


@pytest.mark.parametrize("arguments", [
    {"action": "search", "package_id": "visible", "resource_path": "方法/私有独门/SKILL.md"},
    {"action": "search", "package_id": "visible", "resource_path": "missing.md"},
    {"action": "search", "package_id": "visible", "resource_path": ""},
    {"package_id": "visible", "resource_path": "方法/私有独门/SKILL.md"},
    {"package_id": "visible", "resource_path": ""},
    {"action": "search", "package_id": "hidden", "resource_path": "方法/私有独门/SKILL.md"},
    {"action": "search", "package_id": "missing", "resource_path": "missing.md"},
    {"action": "search", "query": "普通用户方法", "resource_path": ""},
])
def test_search_rejects_resource_path_before_snapshot_read_or_pin(
    tmp_path, skill_catalog_factory, monkeypatch, arguments,
):
    reads = []
    visible, hidden = package_fixture("visible", reads=reads), package_fixture("hidden", reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [visible, hidden])
    selected = snapshot.restricted([visible.stable_id], expected_refs=[visible.to_ref()])
    current_snapshot = Mock(return_value=selected)
    agent.current_skill_snapshot = current_snapshot
    pins = _observe_pins(monkeypatch)
    original = deepcopy(arguments)
    outcome = tool.execute(arguments)
    assert not outcome.ok and outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    payload = json.loads(outcome.output)
    assert set(payload) == {"error", "hint"}
    assert "resource_path" in payload["error"] and "action=get" in payload["error"]
    assert arguments == original and reads == pins == []
    current_snapshot.assert_not_called()


def test_no_package_keeps_ordinary_skill_get_bytes_and_public_search_contract(tmp_path, skill_catalog_factory):
    _, snapshot, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [])
    entry = snapshot.entries[0]
    expected = {"skill_id": entry.stable_id, "name": entry.name, "source": entry.source,
                "path": entry.path, "content_sha256": entry.content_sha256, "body": snapshot.read_body(entry.stable_id)}
    result = tool.execute({"skill_id": entry.stable_id})
    assert result.output == json.dumps(expected, ensure_ascii=False, indent=2)
    search = tool.execute({"query": "普通用户方法"})
    assert search.ok and "resource_namespace" not in search.output and "next_search" not in search.output
    assert json.loads(search.output)["matches"][0]["next_read"] == {"action": "get", "skill_id": entry.stable_id}


@pytest.mark.parametrize("preview_chars", [0, 1000])
def test_large_search_keeps_whole_preview_cards_and_original_page_cursor(
    tmp_path, skill_catalog_factory, preview_chars,
):
    files = {"CAPABILITY.md": b"entry", **{f"methods/{i:02d}-" + "x" * 100 + ".md": b"private" for i in range(22)}}
    package = package_fixture(files=files)
    _, _, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    agent.config = replace(agent.config, tool_output_preview_chars=preview_chars)
    outcome = tool.execute({"action": "search", "package_id": package.package_id, "limit": 20})
    original, live = json.loads(outcome.output), json.loads(outcome.model_visible_output())
    _assert_navigation(live, package)
    assert len(original["matches"]) == 20 and original["continuation"]["offset"] == 20
    assert live["continuation"] == original["continuation"] and live["has_more"] is True
    assert live["matches_preview_complete"] is False and "matches" not in live
    assert live["matches_preview"] == original["matches"][:len(live["matches_preview"])]
    assert len(json.dumps(live["matches_preview"], ensure_ascii=False)) <= preview_chars + 2
    assert len(outcome.model_visible_output()) < 3500
    assert outcome.result_envelope["tool_output_policy"]["requires_recovery_artifact"] is True
    if preview_chars == 0:
        assert live["matches_preview"] == []
    assert "read_artifact" in live["body_read_hint"]


# LLM: 在原三轮读取/复制替身上只观察工具结果；write_file 仍直接消费真实 source_ref，零预览也必须复制原字节。
# 类用途: 证明地址说明不会破坏原生大正文和原样资源物化的主链。
class _ScopedCopyBackend(_PackagePipelineBackend):
    # LLM: 先沿父替身发起原工具调用，再检查已收到的实际 native result，不替换或补写返回值。
    # 函数用途: 核对小/大结果的包导航在模型下一轮确实可见。
    def generate(self, prompt, on_chunk=None, **kwargs):
        response = super().generate(prompt, on_chunk=on_chunk, **kwargs)
        if len(self.requests) == 2:
            block = next(b for row in kwargs["messages"] for b in row["content"]
                         if b["type"] == "tool_result" and b["tool_use_id"] == "read-package")
            payload, _ = json.JSONDecoder().raw_decode(block["content"][block["content"].index("{"):])
            assert payload["resource_namespace"]["declared_members_only"] is True
            assert payload["next_search"]["expected_activation_id"] == self.source_ref["activation_id"]
            assert "write_file.source_ref" in payload["resource_copy_hint"]
        return response


@pytest.mark.parametrize("preview_chars,large", [(0, True), (200, True), (40_000, False)])
def test_native_read_retains_navigation_and_original_copy_reference(tmp_path, preview_chars, large):
    raw = b"# source\r\n" + b"preserve = True\r\n" * (800 if large else 1)
    agent, _, _ = _agent(tmp_path, method_body=raw)
    workspace = resolve_owner_home(agent.home_paths.root).workspace_dir / "scoped-copy"
    workspace.mkdir(parents=True)
    agent = SimpleAgent(replace(agent.config, tool_output_preview_chars=preview_chars,
                                tool_output_externalize_min_chars=100, tool_read_max_chars=5000), workspace)
    target = workspace / "original.py"
    agent.backend = _ScopedCopyBackend(target)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "scope-copy",
    })
    result = agent.run("把已有包资源原样整理到工作区。", save=False, allowed_tools=["skill_search", "write_file"],
                       task_attributes={"conversation_thread_id": thread.thread_id}, context_scope="conversation")
    assert result.response == "已按原资源生成文件。" and target.read_bytes() == raw


# LLM: 单次提交固定检索参数后只读下一轮真实模型输入，不调用恢复/成员读取，不产生业务文件或包 pin。
# 类用途: 检查 search 的原生成功和错误投影，零预览也能找到原结果与同代包。
class _ScopedSearchBackend(_TestNativeBackend):
    # LLM: 模型替身复制本测试参数，不改变调用方字典或共享代理状态；省略参数保留原目录检索。
    # 函数用途: 初始化一轮搜索的计数、结果和参数观测容器。
    def __init__(self, parameters=None):
        self.calls = 0
        self.content = ""
        self.result = {}
        self.parameters = deepcopy(parameters) if parameters is not None else {"action": "search", "package_id": "story-a"}

    # LLM: 只输出一次结构化 search；后续检查 host 已投影的结果，不自行重构能力返回值。
    # 函数用途: 沿真实原生循环观察导航、预览完整性和原归档锚点。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text="", backend="scope-fixture", tool_use_blocks=[{
                "id": "search-members", "name": "skill_search", "input": deepcopy(self.parameters),
            }])
        assert self.calls == 2
        block = next(b for row in kwargs["messages"] for b in row["content"]
                     if b["type"] == "tool_result" and b["tool_use_id"] == "search-members")
        self.result = block
        self.content = block["content"]
        return ModelResponse(text="目录已收到。", backend="scope-fixture")


@pytest.mark.parametrize("preview_chars", [0, 4000])
def test_native_search_archive_preserves_namespace_and_recovery_anchor(tmp_path, preview_chars):
    agent, _, _ = _agent(tmp_path)
    agent.config.tool_output_preview_chars = preview_chars
    agent.config.tool_output_externalize_min_chars = 1
    backend = _ScopedSearchBackend()
    agent.backend = backend
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "scope-search",
    })
    result = agent.run("查看现有包的资源目录。", save=False, allowed_tools=["skill_search"],
                       task_attributes={"conversation_thread_id": thread.thread_id}, context_scope="conversation")
    assert result.response == "目录已收到。"
    payload, _ = json.JSONDecoder().raw_decode(backend.content[backend.content.index("{"):])
    assert payload["resource_namespace"]["filesystem_path"] is False
    assert payload["next_search"]["limit"] == 5 and "write_file.source_ref" in payload["resource_copy_hint"]
    assert "read_artifact_hint" in backend.content and "output_scoped_call_id" in backend.content
    if preview_chars == 0:
        assert payload["matches_preview"] == [] and payload["matches_preview_complete"] is False
    else:
        assert len(payload.get("matches", payload.get("matches_preview"))) == 2
    assert agent.conversation_store.tasks.list(thread.thread_id) == []


@pytest.mark.parametrize("parameters", [
    {"action": "search", "package_id": "story-a", "resource_path": "methods/SKILL.md"},
    {"package_id": "story-a", "resource_path": ""},
])
def test_native_search_rejects_resource_path_without_promotion_or_pin(tmp_path, monkeypatch, parameters):
    agent, _, _ = _agent(tmp_path)
    backend = _ScopedSearchBackend(parameters)
    agent.backend = backend
    pins = _observe_pins(monkeypatch)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "scope-search-invalid",
    })
    agent.run("查询当前可用方法。", save=False, allowed_tools=["skill_search"],
              task_attributes={"conversation_thread_id": thread.thread_id}, context_scope="conversation")
    assert backend.calls == 2 and backend.result["is_error"] is True
    assert "TOOL_INVALID_ARGUMENTS" in backend.content
    payload, _ = json.JSONDecoder().raw_decode(backend.content[backend.content.index("{"):])
    assert set(payload) == {"error", "hint"}
    assert backend.parameters == parameters and pins == []
    assert agent.conversation_store.tasks.list(thread.thread_id) == []
