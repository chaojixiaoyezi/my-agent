# LLM: 规范输入显式mode，提示示例须原样执行；旧优先级仅在v1夹具验证，保留owner索引、task_ref和游标语义。
# 模块用途: 回归历史工具的字段声明、检索、锚点和最近列表及不可用失败。
"""session_search 工具钉子(输出仍沿discover/scroll/browse旧协议)。

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
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.local_storage.models import LocalSearchResult
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.models import KeywordToolSearchProvider


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


def _search_result(
    *,
    source_type: str,
    source_id: str,
    title: str,
    content: str,
    score: float = 0.0,
    metadata: dict | None = None,
) -> LocalSearchResult:
    return LocalSearchResult(
        id=f"record-{source_id}",
        source_type=source_type,
        source_id=source_id,
        title=title,
        content=content,
        metadata=metadata or {},
        visibility="private",
        created_at=1.0,
        updated_at=2.0,
        score=score,
    )


# LLM: 只验证现行原生schema和检索提示，不执行历史查询或改变权限。
# 类用途: 核对历史工具模型声明和推荐可达性。
class TestSpec:
    # LLM: 参数边界和必填mode必须出现在原生声明，不能依赖正文猜测。
    # 函数用途: 检查现行字段类型及模式必填约束。
    def test_spec_has_precise_input_schema(self):
        spec = build_session_search_model_spec()
        assert spec.name == "session_search"
        # 精确类型(对齐原生 tool_use):整型参数带 integer + 上下界。
        properties = spec.input_schema["properties"]
        assert properties["query"]["type"] == "string"
        assert properties["window"]["type"] == "integer"
        assert properties["limit"]["type"] == "integer"
        assert properties["around_id"]["type"] == "string"
        assert spec.input_schema.get("required", []) == ["mode"]

    def test_recent_named_project_wording_recommends_history_search(self):
        """真实中文续作措辞必须命中历史工具，而不是让模型先遍历整个 owner 目录。"""
        spec = build_session_search_model_spec()

        hits = KeywordToolSearchProvider().search(
            "回到刚才那个星河日志分析器，在原项目里增加 CSV 输出",
            [spec],
            1,
        )

        assert [hit.name for hit in hits] == ["session_search"]
        assert any("刚才" in reason or "回到" in reason for reason in hits[0].reasons)
        assert "source_type\":\"gateway_request" in spec.examples[1]

    def test_recent_named_project_ranks_session_search_first_in_real_registry(self, tmp_path):
        """完整默认工具集里，真实续作措辞也必须把 session_search 排到首位。"""
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
            tmp_path,
        )

        section = agent.tools.render_recommended_tools_section(
            "回到刚才那个星河日志分析器，在原项目里增加 CSV 输出"
        )
        recommendations = [line for line in section.splitlines() if line.startswith("- ")]

        assert recommendations[0].startswith("- session_search：")


# LLM: 用临时SQLite或固定命中验证规范search，保留来源过滤和可信任务引用。
# 类用途: 检查全文检索输出与历史任务续作线索。
class TestDiscoveryMode:
    # LLM: 中文查询沿原FTS数据源，不能用模式收敛丢掉摘要和定位键。
    # 函数用途: 检查中文命中、摘要和锚点引用。
    def test_discovery_returns_chinese_hits_with_snippet(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"mode": "search", "query": "推送模式"}))
        assert p["mode"] == "discover"
        assert p["count"] >= 2
        titles = {r["title"] for r in p["results"]}
        assert "记忆推送模式" in titles
        assert "推送触发条件" in titles
        for r in p["results"]:
            assert r["snippet"]
            assert r["around_id"]  # 可继续 scroll 的定位 id

    # LLM: source_type保持开放字符串，仍过滤原事实源而非改查另一库。
    # 函数用途: 验证检索来源条件不泄入其它类型。
    def test_discovery_source_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(source_type="task", source_id="t1", title="任务记录", content="推送模式相关任务")
        tool = _tool(store)
        p = _payload(tool.execute({"mode": "search", "query": "推送模式", "source_type": "task"}))
        assert all(r["source_type"] == "task" for r in p["results"])

    # LLM: 空结果是本次检索事实，不冒充来源全无数据。
    # 函数用途: 检查无命中仍返回结构化提示。
    def test_discovery_no_hit_is_graceful(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"mode": "search", "query": "完全不存在zzz"}))
        assert p["count"] == 0
        assert "hint" in p

    # LLM: 新合同越界须拒绝，不能用旧handler夹取范围掩盖坏参数。
    # 函数用途: 检查检索上限越界在读库前失败。
    def test_discovery_limit_clamped(self, tmp_path):
        tool = _tool(_store(tmp_path))
        result = tool.execute({"mode": "search", "query": "推送模式", "limit": 999})
        assert not result.ok and result.error_code == "TOOL_SCHEMA_INVALID_MODE_COMBINATION"

    # LLM: 命中范围只取结构化metadata，不能用正文补全个人或会话权限。
    # 函数用途: 验证记忆命中保留召回范围字段。
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

        p = _payload(_tool(store).execute({"mode": "search", "query": "苍穹折页"}))

        assert p["count"] == 1
        assert p["results"][0]["scope"] == {
            "role": "user",
            "scope_type": "session",
            "scope_key": "session:thread-example",
        }

    # LLM: 可续作目录只取已完成Gateway的宿主task_ref，不从标题猜路径。
    # 函数用途: 检查历史任务命中保留准确续作引用。
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

        p = _payload(_tool(store).execute({"mode": "search", "query": "日志分析工具"}))

        hit = next(item for item in p["results"] if item["source_id"] == "gw-log-tool")
        assert hit["task_ref"] == {
            "task_id": "task-log-tool",
            "task_path": "/owner/tasks/log-tool",
            "request_id": "gw-log-tool",
            "thread_id": "thread-one",
            "status": "done",
        }

    # LLM: 普通记忆metadata即使同形也不能冒充宿主历史任务引用。
    # 函数用途: 拒绝非Gateway来源伪造续作目录。
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

        p = _payload(_tool(store).execute({"mode": "search", "query": "伪造任务引用校验词"}))

        assert p["count"] == 1
        assert "task_ref" not in p["results"][0]

    # LLM: 同源宽检索可优先呈现已结束任务引用，不能被聊天排序挤掉。
    # 函数用途: 检查准确续作线索在普通文本命中前保留。
    def test_completed_task_ref_survives_prose_top_five(self):
        """宽检索须从本地候选中提起已结束任务引用，不能被五条聊天文本挤掉。"""
        rows = [
            _search_result(
                source_type="conversation_message",
                source_id=f"chat-{index}",
                title=f"聊天 {index}",
                content="北辰账目汇总器的普通对话",
                score=float(index),
            )
            for index in range(5)
        ]
        rows.append(
            _search_result(
                source_type="gateway_request",
                source_id="gw-north-star",
                title="Gateway ask done gw-north-star",
                content="北辰账目汇总器已经完成",
                score=99.0,
                metadata={
                    "status": "done",
                    "conversation_runtime": {
                        "task_id": "task-north-star",
                        "task_path": "/owner/tasks/north-star",
                    },
                },
            )
        )
        search_limits: list[int] = []
        store = SimpleNamespace(
            search=lambda _query, *, limit, source_type: search_limits.append(limit) or rows[:limit],
            list_recent=lambda **_kwargs: [],
            records_around=lambda *_args, **_kwargs: ([], 0, 0),
        )

        p = _payload(_tool(store).execute({"mode": "search", "query": "北辰账目汇总器", "limit": 5}))

        assert search_limits == [20]
        assert p["count"] == 5
        assert p["results"][0]["source_id"] == "gw-north-star"
        assert p["results"][0]["task_ref"]["task_path"] == "/owner/tasks/north-star"
        assert [item["source_id"] for item in p["results"][1:]] == [
            "chat-0",
            "chat-1",
            "chat-2",
            "chat-3",
        ]

    # LLM: 活跃占位目录不属于已结束历史，不能凭高分获得task_ref权威。
    # 函数用途: 核对未结束任务不会伪装成历史续作目标。
    @pytest.mark.parametrize("status", ["queued", "processing", "running", ""])
    def test_live_gateway_placeholder_cannot_pose_as_historical_task(self, status):
        """当前请求的占位目录即使命中更高，也不能取得历史续作 task_ref。"""
        metadata = {
            "status": status,
            "conversation_runtime": {
                "task_id": "task-current-placeholder",
                "task_path": "/owner/tasks/current-placeholder",
            },
        }
        rows = [
            _search_result(
                source_type="gateway_request",
                source_id="gw-current",
                title="当前请求",
                content="北辰账目汇总器续作",
                metadata=metadata,
            ),
            _search_result(
                source_type="gateway_request",
                source_id="gw-history",
                title="历史请求",
                content="北辰账目汇总器完成",
                metadata={
                    "status": "done",
                    "conversation_runtime": {
                        "task_id": "task-history",
                        "task_path": "/owner/tasks/history",
                    },
                },
            ),
        ]
        store = SimpleNamespace(
            search=lambda *_args, **_kwargs: rows,
            list_recent=lambda **_kwargs: [],
            records_around=lambda *_args, **_kwargs: ([], 0, 0),
        )

        p = _payload(_tool(store).execute({"mode": "search", "query": "北辰账目汇总器"}))

        assert p["results"][0]["source_id"] == "gw-history"
        current = next(item for item in p["results"] if item["source_id"] == "gw-current")
        assert "task_ref" not in current


# LLM: around沿原锚点和邻域读取，输入mode不能改变旧输出或task_ref。
# 类用途: 检查锚点窗口及严格window参数。
class TestScrollMode:
    # LLM: 锚点定位和前后窗口由原SQLite提供，不另造聊天顺序。
    # 函数用途: 验证around读取真实锚点和邻域。
    def test_scroll_returns_window_around_anchor(self, tmp_path):
        store = _store(tmp_path)
        anchor = store.search("Gateway", limit=1)[0]
        tool = _tool(store)
        p = _payload(tool.execute({"mode": "around", "around_id": anchor.id, "window": 2}))
        assert p["mode"] == "scroll"
        assert p["around_id"] == anchor.id
        # 锚被标记
        flagged = [m for m in p["messages"] if m.get("anchor")]
        assert len(flagged) == 1
        assert flagged[0]["id"] == anchor.id
        assert "messages_before" in p
        assert "messages_after" in p

    # LLM: 此例显式v1旧输入，优先级仅由版本化边界决定，不能污染规范handler。
    # 函数用途: 验证旧锚点优先规则在迁移后仍等价。
    def test_scroll_wins_over_query(self, tmp_path):
        # 明确v1夹具：旧优先级只由宿主输入解码保留；规范handler不再推断。
        from agent_py_agent.agent.tooling.versioned_input_decoder import decode_tool_input

        store = _store(tmp_path)
        anchor = store.search("会话", limit=1)[0]
        tool = _tool(store)
        _, params = decode_tool_input("session_search", {"query": "推送模式", "around_id": anchor.id}, version=1)
        p = _payload(tool.execute(params))
        assert p["mode"] == "scroll"

    # LLM: 未找到的锚点只返回当前读取空结果，不扩大范围寻找他人记录。
    # 函数用途: 检查坏锚点的结构化提示。
    def test_scroll_bad_anchor_is_graceful(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"mode": "around", "around_id": "rec-doesnotexist"}))
        assert p["mode"] == "scroll"
        assert p["count"] == 0
        assert "hint" in p

    # LLM: 规范window越界必须前置拒绝，不沿旧夹取逻辑执行。
    # 函数用途: 检查窗口上限的严格校验。
    def test_scroll_window_clamped(self, tmp_path):
        store = _store(tmp_path)
        anchor = store.search("Gateway", limit=1)[0]
        tool = _tool(store)
        result = tool.execute({"mode": "around", "around_id": anchor.id, "window": 999})
        assert not result.ok and result.error_code == "TOOL_SCHEMA_INVALID_MODE_COMBINATION"

    # LLM: 导航输出与检索输出共用可信task_ref投影，不能翻页后丢失续作身份。
    # 函数用途: 验证around仍携带历史任务引用。
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

        p = _payload(_tool(store).execute({"mode": "around", "around_id": anchor.id, "window": 1}))

        anchored = next(item for item in p["messages"] if item.get("anchor"))
        assert anchored["task_ref"]["task_path"] == "/owner/tasks/scroll"


# LLM: recent复用原倒序列表，不用空query隐式选择模式。
# 类用途: 检查最近记录、来源过滤与可信任务引用。
class TestBrowseMode:
    # LLM: 输入recent仍保持原browse输出，已有消费方无需同步改名。
    # 函数用途: 检查最近记录条目和预览。
    def test_browse_lists_recent(self, tmp_path):
        tool = _tool(_store(tmp_path))
        p = _payload(tool.execute({"mode": "recent"}))
        assert p["mode"] == "browse"
        assert p["count"] == 4
        for r in p["results"]:
            assert "preview" in r
            assert "when" in r

    # LLM: 临时空库返回本次观察事实，不伪造记录或失败。
    # 函数用途: 检查recent在空库的正常输出。
    def test_browse_empty_store_is_graceful(self, tmp_path):
        store = LocalStore(tmp_path / "empty.db")
        tool = _tool(store)
        p = _payload(tool.execute({"mode": "recent"}))
        assert p["mode"] == "browse"
        assert p["count"] == 0

    # LLM: 最近列表必须执行同一来源过滤，不能按mode省略条件。
    # 函数用途: 验证recent仅返回指定来源记录。
    def test_browse_source_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_record(source_type="archive", source_id="a1", title="归档", content="归档内容")
        tool = _tool(store)
        p = _payload(tool.execute({"mode": "recent", "source_type": "archive"}))
        assert all(r["source_type"] == "archive" for r in p["results"])

    # LLM: 列表与检索共用宿主历史任务引用，不从预览正文造路径。
    # 函数用途: 检查recent保留准确任务续作线索。
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

        p = _payload(_tool(store).execute({"mode": "recent", "source_type": "gateway_request"}))

        assert p["results"][0]["task_ref"]["task_path"] == "/owner/tasks/recent"


# LLM: 索引不可用必须明确报错，不将缺数据源当成查询无命中。
# 类用途: 检查历史存储缺失和不完整的失败合同。
class TestUnavailableStore:
    # LLM: search需要索引，缺失返回原TOOL_UNAVAILABLE而非空成功。
    # 函数用途: 检查无历史库时的准确失败码。
    def test_no_store_returns_unavailable_code(self):
        tool = SessionSearchTool(SimpleNamespace(local_store=None))
        result = tool.execute({"mode": "search", "query": "x"})
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"

    # LLM: 不完整对象不能冒充原LocalStore，不能吞成无命中。
    # 函数用途: 验证缺少索引读口时明确拒绝。
    def test_store_missing_methods_treated_unavailable(self):
        # 鸭子类型校验:缺 records_around 的对象不被当 store。
        bogus = SimpleNamespace(search=lambda *a, **k: [], list_recent=lambda *a, **k: [])
        tool = SessionSearchTool(SimpleNamespace(local_store=bogus))
        result = tool.execute({"mode": "search", "query": "x"})
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"


# LLM: 真实canonical会话和索引，原样提取回执JSON执行，不替提示补mode；消息继续读取必须确实返回对应原文。
# 函数用途: 覆盖message丢失、长消息续读及thread_browse首末页的提示下一跳。
@pytest.mark.parametrize('case', ['missing', 'continuation', 'browse_first', 'browse_last', 'explicit_thread'])
def test_canonical_history_response_hints_are_executable(tmp_path, case):
    import re

    from agent_py_agent.tests.test_session_history_read import _owner

    owner = _owner(tmp_path)
    params = {'mode': 'message', 'message_id': 'missing'}
    if case == 'continuation':
        params.update(message_id=owner.rows[0].message_id, max_chars=200)
    elif case == 'explicit_thread':
        # 分页必须继续明确指定的其它本人会话，不能静默回到当前会话。
        message = owner.store.messages.append({'thread_id': owner.other.thread_id, 'role': 'user', 'content': '其它会话原文' * 100})
        params.update(message_id=message.message_id, thread_id=owner.other.thread_id, max_chars=200)
    elif case.startswith('browse'):
        params = {'mode': 'thread_browse', 'limit': 1 if case == 'browse_first' else 20}
    tool = SessionSearchTool(owner.agent)
    page = _payload(tool.execute(params))
    calls = [json.loads(match.group()) for match in re.finditer(r'\{[^{}]+\}', page['hint'])]
    assert calls, page['hint']
    for call in calls:
        assert 'mode' in call
        follow = _payload(tool.execute(call))
        if call['mode'] == 'message':
            assert follow['found'] is True
            row, errors = owner.store.messages.by_id_report(follow['thread_id'], call['message_id'])
            assert not errors and row is not None
            assert follow['content'] == row.content[follow['offset']:follow['offset'] + len(follow['content'])]
        if case in {'continuation', 'explicit_thread'}:
            assert follow['offset'] == page['next_offset']
            assert follow['thread_id'] == page['thread_id']
            assert follow['content']
        if call['mode'] == 'thread_browse' and 'cursor' in call:
            assert call['cursor'] == page['older_cursor']


# LLM: 压缩召回说明给出合法v2 JSON模板；仅把示例中的定位符换成本临时库真实ID，不增加mode或字段。
# 函数用途: 证明压缩地标的message、around和thread_browse示例照做可达。
def test_compact_landmark_recall_examples_execute(tmp_path):
    import re

    from agent_py_agent.agent.conversation.compact_landmarks import _RECALL_LINE
    from agent_py_agent.tests.test_session_history_read import _owner

    owner = _owner(tmp_path)
    text = _RECALL_LINE.replace('<message_id>', owner.rows[0].message_id).replace('<around_id>', owner.agent.local_store.list_recent(limit=1)[0].id)
    calls = [json.loads(match.group()) for match in re.finditer(r'\{[^{}]+\}', text)]
    assert {call.get('mode') for call in calls} == {'message', 'around', 'thread_browse'}
    for call in calls:
        follow = _payload(SessionSearchTool(owner.agent).execute(call))
        if call['mode'] == 'message':
            assert follow['found'] and follow['content']
        elif call['mode'] == 'around':
            assert follow['count'] > 0
        else:
            assert follow['messages']
