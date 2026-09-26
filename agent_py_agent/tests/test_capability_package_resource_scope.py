# LLM: 只验证原包检索/读取的地址投影和显式恢复；组件与原生替身不联网，不改样包、不替真实任务交付。
# 模块用途: 守住包成员与业务文件的地址空间、同代导航和大结果的完整引用。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_discovery import (
    discovery_fixture,
    package_fixture,
)
from agent_py_agent.tests.test_capability_package_native_pipeline import _PackagePipelineBackend
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
        outcome = tool.execute({"action": action, "package_id": package_id})
        assert not outcome.ok
        assert json.loads(outcome.output) == {"error": "CAPABILITY_PACKAGE_NOT_AVAILABLE"}
    assert reads == pins == []


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


# LLM: 单次显式检索后只读下一轮真实模型输入，不调用恢复/成员读取，不产生业务文件或包 pin。
# 类用途: 检查 search 的原生归档投影，零预览也能找到原结果与同代包。
class _ScopedSearchBackend(_TestNativeBackend):
    # LLM: 模型替身只持有本测试的计数与输入，不共享代理运行状态。
    # 函数用途: 初始化一轮搜索的观测容器。
    def __init__(self):
        self.calls = 0
        self.content = ""

    # LLM: 只输出一次结构化 search；后续检查 host 已投影的结果，不自行重构能力返回值。
    # 函数用途: 沿真实原生循环观察导航、预览完整性和原归档锚点。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text="", backend="scope-fixture", tool_use_blocks=[{
                "id": "search-members", "name": "skill_search", "input": {"action": "search", "package_id": "story-a"},
            }])
        assert self.calls == 2
        block = next(b for row in kwargs["messages"] for b in row["content"]
                     if b["type"] == "tool_result" and b["tool_use_id"] == "search-members")
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
