"""session_search 工具钉子(对标 长期助手 三模式:discovery/scroll/browse)。

封装现成 LocalStore 检索,三种调用形态由参数推断:
① query=discovery 全文检索;② around_id=scroll 时间窗翻看;③ 无参=browse 最近列表。
钉住:三模式各自工作、参数 schema 精确、空库/无命中/坏锚/store 不可用都优雅降级。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.session_search_tool import (
    SessionSearchTool,
    build_session_search_model_spec,
)
from agent_py_agent.agent.local_storage.store import LocalStore


def _store(tmp_path: Path) -> LocalStore:
    store = LocalStore(tmp_path / "sess.db")
    seeds = [
        ("记忆推送模式", "记忆推送模式已落地 failure 和 planner 注入点 修复中文短 goal 检索缺陷"),
        ("QQ网关稳定", "QQ Gateway 稳定性 Round 5 完成 scoped locks supervisor"),
        ("推送触发条件", "推送模式触发条件结构化待做"),
        ("会话架构", "session 跨通道接续 通知路由 并发控制 审计"),
    ]
    for i, (title, content) in enumerate(seeds):
        store.upsert_record(source_type="memory", source_id=f"m{i}", title=title, content=content)
    return store


def _tool(store) -> SessionSearchTool:
    return SessionSearchTool(SimpleNamespace(local_store=store))


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


class TestSpec:
    def test_spec_has_precise_input_schema(self):
        spec = build_session_search_model_spec()
        assert spec.name == "session_search"
        # 精确类型(对齐原生 tool_use):整型参数带 integer + 上下界。
        properties = spec.input_schema["properties"]
        assert properties["query"]["type"] == "string"
        assert properties["window"]["type"] == "integer"
        assert properties["limit"]["type"] == "integer"
        assert properties["around_id"]["type"] == "string"
        assert spec.input_schema.get("required", []) == []


class TestDiscoveryMode:
    def test_discovery_returns_chinese_hits_with_snippet(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"query": "推送模式"}))
        assert p["mode"] == "discover"
        assert p["count"] >= 2
        titles = {r["title"] for r in p["results"]}
        assert "记忆推送模式" in titles
        assert "推送触发条件" in titles
        for r in p["results"]:
            assert r["snippet"]
            assert r["around_id"]  # 可继续 scroll 的定位 id

    def test_discovery_source_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(source_type="task", source_id="t1", title="任务记录", content="推送模式相关任务")
        tool = _tool(store)
        p = _payload(tool.execute({"query": "推送模式", "source_type": "task"}))
        assert all(r["source_type"] == "task" for r in p["results"])

    def test_discovery_no_hit_is_graceful(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"query": "完全不存在zzz"}))
        assert p["count"] == 0
        assert "hint" in p

    def test_discovery_limit_clamped(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"query": "推送模式", "limit": 999}))
        assert p["count"] <= 20

    def test_memory_hit_exposes_typed_recall_scope(self, tmp_path):
        """记忆索引的嵌套 attributes 必须投影到检索结果，供模型区分会话与个人记忆。"""
        store = _store(tmp_path)
        store.upsert_record(
            source_type="memory",
            source_id="scoped-memory",
            title="会话校验词",
            content="苍穹折页-420871",
            metadata={
                "role": "user",
                "attributes": {
                    "scope_type": "session",
                    "scope_key": "session:thread-example",
                },
            },
        )

        p = _payload(_tool(store).execute({"query": "苍穹折页"}))

        assert p["count"] == 1
        assert p["results"][0]["scope"] == {
            "role": "user",
            "scope_type": "session",
            "scope_key": "session:thread-example",
        }

    def test_gateway_hit_exposes_exact_task_ref_for_continuation(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(
            source_type="gateway_request",
            source_id="gw-log-tool",
            title="Gateway ask done gw-log-tool",
            content="用户之前完成了日志分析工具",
            metadata={
                "status": "done",
                "conversation_runtime": {
                    "request_id": "gw-log-tool",
                    "thread_id": "thread-one",
                    "task_id": "task-log-tool",
                    "task_path": "/owner/tasks/log-tool",
                },
            },
        )

        p = _payload(_tool(store).execute({"query": "日志分析工具"}))

        hit = next(item for item in p["results"] if item["source_id"] == "gw-log-tool")
        assert hit["task_ref"] == {
            "task_id": "task-log-tool",
            "task_path": "/owner/tasks/log-tool",
            "request_id": "gw-log-tool",
            "thread_id": "thread-one",
            "status": "done",
        }

    def test_non_gateway_metadata_cannot_pose_as_task_ref(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(
            source_type="memory",
            source_id="memory-fake-task",
            title="伪造任务引用",
            content="伪造任务引用校验词",
            metadata={
                "conversation_runtime": {
                    "task_id": "fake",
                    "task_path": "/another-owner/task",
                }
            },
        )

        p = _payload(_tool(store).execute({"query": "伪造任务引用校验词"}))

        assert p["count"] == 1
        assert "task_ref" not in p["results"][0]


class TestScrollMode:
    def test_scroll_returns_window_around_anchor(self, tmp_path):
        store = _store(tmp_path)
        anchor = store.search("Gateway", limit=1)[0]
        tool = _tool(store)
        p = _payload(tool.execute({"around_id": anchor.id, "window": 2}))
        assert p["mode"] == "scroll"
        assert p["around_id"] == anchor.id
        # 锚被标记
        flagged = [m for m in p["messages"] if m.get("anchor")]
        assert len(flagged) == 1
        assert flagged[0]["id"] == anchor.id
        assert "messages_before" in p
        assert "messages_after" in p

    def test_scroll_wins_over_query(self, tmp_path):
        # 同时给 query 与 around_id 时,scroll 优先(显式锚点压过检索)。
        store = _store(tmp_path)
        anchor = store.search("会话", limit=1)[0]
        tool = _tool(store)
        p = _payload(tool.execute({"query": "推送模式", "around_id": anchor.id}))
        assert p["mode"] == "scroll"

    def test_scroll_bad_anchor_is_graceful(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"around_id": "rec-doesnotexist"}))
        assert p["mode"] == "scroll"
        assert p["count"] == 0
        assert "hint" in p

    def test_scroll_window_clamped(self, tmp_path):
        store = _store(tmp_path)
        anchor = store.search("Gateway", limit=1)[0]
        tool = _tool(store)
        p = _payload(tool.execute({"around_id": anchor.id, "window": 999}))
        # 窗口被夹到 [1,20];库里 4 条,total <= 4。
        assert p["count"] <= 4

    def test_scroll_keeps_gateway_task_ref(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(
            source_type="gateway_request",
            source_id="gw-scroll",
            title="历史任务滚动锚点",
            content="历史任务滚动正文",
            metadata={
                "status": "done",
                "conversation_runtime": {
                    "task_id": "task-scroll",
                    "task_path": "/owner/tasks/scroll",
                },
            },
        )
        anchor = store.search("历史任务滚动正文", limit=1)[0]

        p = _payload(_tool(store).execute({"around_id": anchor.id, "window": 1}))

        anchored = next(item for item in p["messages"] if item.get("anchor"))
        assert anchored["task_ref"]["task_path"] == "/owner/tasks/scroll"


class TestBrowseMode:
    def test_browse_lists_recent(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({}))
        assert p["mode"] == "browse"
        assert p["count"] == 4
        for r in p["results"]:
            assert "preview" in r
            assert "when" in r

    def test_browse_empty_store_is_graceful(self, tmp_path):
        store = LocalStore(tmp_path / "empty.db")
        tool = _tool(store)
        p = _payload(tool.execute({}))
        assert p["mode"] == "browse"
        assert p["count"] == 0

    def test_browse_source_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(source_type="archive", source_id="a1", title="归档", content="归档内容")
        tool = _tool(store)
        p = _payload(tool.execute({"source_type": "archive"}))
        assert all(r["source_type"] == "archive" for r in p["results"])

    def test_browse_keeps_gateway_task_ref(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(
            source_type="gateway_request",
            source_id="gw-recent",
            title="最近任务",
            content="最近任务正文",
            metadata={
                "status": "done",
                "conversation_runtime": {
                    "task_id": "task-recent",
                    "task_path": "/owner/tasks/recent",
                },
            },
        )

        p = _payload(_tool(store).execute({"source_type": "gateway_request"}))

        assert p["results"][0]["task_ref"]["task_path"] == "/owner/tasks/recent"


class TestUnavailableStore:
    def test_no_store_returns_unavailable_code(self):
        tool = SessionSearchTool(SimpleNamespace(local_store=None))
        result = tool.execute({"query": "x"})
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"

    def test_store_missing_methods_treated_unavailable(self):
        # 鸭子类型校验:缺 records_around 的对象不被当 store。
        bogus = SimpleNamespace(search=lambda *a, **k: [], list_recent=lambda *a, **k: [])
        tool = SessionSearchTool(SimpleNamespace(local_store=bogus))
        result = tool.execute({"query": "x"})
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"
