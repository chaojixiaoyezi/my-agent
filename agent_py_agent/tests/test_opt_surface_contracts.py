# LLM: 优化只收敛模型输入，不扩大快照权限；本文件用真实注册、SQLite及原校验器验证，无外部网络或模型调用。
# 模块用途: 钉住28项首轮工具、单一派工形状、六模式互斥及版本化旧输入边界。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
    build_task_progress_model_spec,
)
from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.settings import AgentConfig


# LLM: 每例使用临时owner与echo后端，不借真实home或启动模型。
# 函数用途: 构造可检查完整工具表的真实代理。
@pytest.fixture
def surface_agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)


# LLM: 数量与成员同时钉住，不能通过隐藏其它主链工具凑28。
# 函数用途: 验证skill反馈首轮稳定而三个低频会话工具可搜索加载。
def test_initial_surface_is_28_and_skill_feedback_never_needs_loading(surface_agent):
    registry = surface_agent.tools
    names = {spec.name for spec in registry.model_visible_specs()}
    assert len(names) == 28, sorted(names)
    assert "skill_feedback" in names
    assert "list_tools" not in registry.tools
    assert {"create_session_task", "get_session_task", "list_owner_sessions"}.isdisjoint(names)
    for name in ("create_session_task", "get_session_task", "list_owner_sessions"):
        result = registry.tools["tool_search"].execute({"action": "search", "names": [name]})
        assert result.ok
        assert result.result_envelope["tool_search"]["loaded_tool_names"] == [name]


# LLM: catalog只读同一快照，不产生加载回执或发现副作用。
# 函数用途: 验证合并后目录保留完整事实和有界现场摘要。
def test_catalog_preserves_manifest_without_loading(surface_agent):
    result = surface_agent.tools.tools["tool_search"].execute({"action": "catalog"})
    assert result.ok, result.output
    payload = json.loads(result.output)
    assert {"self_context", "permission_mode", "tool_failure_taxonomy", "tool_retrieval"} <= payload.keys()
    assert "tool_search" not in result.result_envelope
    live = result.result_envelope["tool_output_policy"]["live_prompt_output"]
    assert len(live.encode()) <= 12_000


# LLM: catalog不接受搜索参数，错误组合不得悄悄加载工具。
# 函数用途: 钉住目录与搜索操作互斥。
@pytest.mark.parametrize("extra", [{"query": "read_file"}, {"names": ["read_file"]}, {"limit": 1}, {"category": "system"}])
def test_catalog_rejects_search_fields(surface_agent, extra):
    result = surface_agent.tools.tools["tool_search"].execute({"action": "catalog", **extra})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"


# LLM: 单项仍走items，保留全部原item字段及媒体开关，不广播父级交付物。
# 函数用途: 验证派工与小型别名只在新模型面收敛。
def test_canonical_small_schemas_have_no_duplicate_aliases():
    schema = build_create_subagents_model_spec().input_schema
    assert set(schema["properties"]) == {"items", "batch_summary"}
    assert schema["required"] == ["items"]
    assert schema["properties"]["items"]["minItems"] == 1
    assert schema["properties"]["items"]["items"]["properties"]["goal"]["minLength"] == 1
    assert build_task_progress_model_spec().input_schema["properties"]["action"]["enum"] == ["read", "update"]
    assert set(SkillFeedbackTool.model_spec.input_schema["properties"]) == {"action", "skill_id"}


# LLM: 规范六模式使用平铺object和宿主互斥表，不能依赖供应商支持根oneOf。
# 函数用途: 验证跨provider可运输的会话检索声明。
def test_session_search_schema_is_flat_and_modes_are_complete():
    schema = SessionSearchTool.model_spec.input_schema
    assert schema["type"] == "object" and "oneOf" not in schema
    assert schema["required"] == ["mode"]
    assert set(schema["properties"]["mode"]["enum"]) == {"search", "recent", "around", "message", "thread_search", "thread_browse"}
    assert "current_thread" not in schema["properties"]
    assert schema["x-exclusive-argument-groups"]


# LLM: 无效mode组合必须在触碰存储前拒绝；bool不可伪装int，未知字段不能静默丢弃。
# 函数用途: 验证handler直调也有严格模式校验而非仅依赖schema。
@pytest.mark.parametrize("params", [
    {"mode": "search"}, {"mode": "search", "query": " "},
    {"mode": "recent", "query": "secret"}, {"mode": "around", "around_id": "r", "limit": 1},
    {"mode": "message", "message_id": "m", "source_type": "memory"},
    {"mode": "thread_search", "query": "a", "thread_id": "foreign"},
    {"mode": "thread_browse", "current_thread": True}, {"mode": "recent", "limit": True},
    {"mode": "recent", "limit": 21}, {"mode": "unknown"}, {},
])
def test_session_search_invalid_mode_combinations_fail_before_storage(params):
    result = SessionSearchTool(SimpleNamespace()).execute(params)
    assert not result.ok
    assert result.error_code == "TOOL_SCHEMA_INVALID_MODE_COMBINATION", result.output


# LLM: 真实FTS和最近列表沿原来源及task_ref投影，不生成第二套历史库。
# 函数用途: 验证规范mode实际路由原数据源且保留返回协议。
def test_canonical_search_recent_and_around_use_existing_store(tmp_path):
    store = LocalStore(tmp_path / "history.db")
    store.upsert_record(source_type="memory", source_id="a", title="导航", content="确定的中文历史")
    tool = SessionSearchTool(SimpleNamespace(local_store=store))
    result = tool.execute({"mode": "search", "query": "中文历史"})
    assert result.ok, result.output
    hit = json.loads(result.output)["results"][0]
    recent = tool.execute({"mode": "recent", "limit": 1})
    assert recent.ok and json.loads(recent.output)["count"] == 1
    around = tool.execute({"mode": "around", "around_id": hit["around_id"]})
    assert around.ok and json.loads(around.output)["messages"][0]["anchor"]


# LLM: 旧输入必须显式指定宿主版本；转换不变更原字典或选择权，混合别名冲突不得进入effect计算。
# 函数用途: 核对统一输入解码器的精确旧优先级和非广播语义。
@pytest.mark.parametrize("name,params,expected", [
    ("list_tools", {}, ("tool_search", {"action": "catalog"})),
    ("skill_feedback", {"operation": "adopted", "skill_id": "owner:x"},
        ("skill_feedback", {"action": "adopted", "skill_id": "owner:x"})),
    ("task_progress", {"action": "create"}, ("task_progress", {"action": "update"})),
    ("session_search", {"query": "a", "around_id": "r", "current_thread": True},
        ("session_search", {"mode": "around", "around_id": "r"})),
    ("session_search", {"message_id": "m", "around_id": "r", "query": "a"},
        ("session_search", {"mode": "message", "message_id": "m"})),
    ("session_search", {"current_thread": True, "query": "a"},
        ("session_search", {"mode": "thread_search", "query": "a"})),
    ("session_search", {}, ("session_search", {"mode": "recent"})),
    ("create_subagents", {"goal": "单项", "output_files": ["a"], "items": []},
        ("create_subagents", {"items": [{"goal": "单项", "output_files": ["a"]}]})),
    ("create_subagents", {"goal": "整批", "output_files": ["parent"], "model": "m", "items": [{"goal": "a", "allowed_tools": []}]},
        ("create_subagents", {"batch_summary": "整批", "items": [{"goal": "a", "model": "m", "allowed_tools": []}]})),
])
def test_v1_decode_is_explicit_and_preserves_effective_semantics(name, params, expected):
    from copy import deepcopy

    from agent_py_agent.agent.tooling.versioned_input_decoder import decode_tool_input
    prior = deepcopy(params)
    assert decode_tool_input(name, params, version=1) == expected
    assert params == prior
    assert decode_tool_input(name, params, version=2) == (name, params)


# LLM: 宿主旧版本不能成为宽松解析旁路；错误在任何存储/claim前拒绝。
# 函数用途: 钉住别名冲突、旧类型错误和已完成历史不可解码。
@pytest.mark.parametrize("name,params", [
    ("skill_feedback", {"operation": "adopted", "action": "status"}),
    ("session_search", {"current_thread": "true"}),
    ("session_search", {"limit": True}),
    ("session_search", {"mode": "recent", "current_thread": True}),
    ("create_subagents", {"goal": "a", "items": [{"goal": " "}]}),
])
def test_v1_decoder_rejects_conflicts_and_bad_types(name, params):
    from agent_py_agent.agent.tooling.versioned_input_decoder import decode_tool_input
    with pytest.raises(ValueError):
        decode_tool_input(name, params, version=1)


# LLM: 单项数组不能丢掉原逻辑锁；不同输入参数不得落成同一个大JSON锁。
# 函数用途: 验证接替和输入引用在claim前保持精确资源身份。
def test_nested_create_items_keep_original_resource_locks(surface_agent, tmp_path):
    tool = surface_agent.tools.tools["create_subagents"]
    scopes = tool.effective_resource_scopes({"items": [
        {"goal": "a", "input_refs": ["a"], "replacement_for_run_ids": ["r1"]},
        {"goal": "b", "input_refs": ["b"], "replacement_for_run_ids": ["r1"]},
    ]}, None, tmp_path)
    assert set(scopes) == {"logical:input_refs:a", "logical:input_refs:b", "logical:replacement_for_run_ids:r1"}


# LLM: 只走真实执行器，宿主v1版本在schema/effect/claim前归一；模型字段不能自报版本。
# 函数用途: 验证旧目录名在唯一执行链迁移且保留tool-use配对身份。
def test_executor_wires_host_v1_decode_and_preserves_call_identity(surface_agent, tmp_path):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    registry = surface_agent.tools
    snapshot = registry.runtime_snapshot()
    call = ToolCall(call_id="legacy-1", tool_name="list_tools", arguments={}, source_protocol="native",
        schema_hash="sha256:legacy-v1", run_id="r", turn_id="t", attempt_id="a")
    execution = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
        workspace_root=tmp_path, operation_store_required=False, approval_mode="ask",
        trusted_run_context={"tool_input_version": 1}))
    assert execution.result.ok, execution.result.error_code
    assert execution.result.call_id == call.call_id
    migration = execution.result.metadata["input_migration"]
    assert migration["requested_tool_name"] == "list_tools"
    assert migration["canonical_tool_name"] == "tool_search"
    assert migration["action"] == "catalog"
    assert "tool_search" not in execution.result.metadata.get("tool_result_envelope", {})


# LLM: 实际handler前的归档回调必须已拿到原名/原指纹；事后装饰结果不能充当持久审计。
# 函数用途: 验证可信迁移事实在执行前已进入canonical调用。
def test_migration_is_available_to_original_archiver_before_execution_returns(surface_agent, tmp_path):
    from agent_py_agent.agent.tooling.executor import (
        ToolExecutor,
        ToolExecutorRequest,
        ToolOutputProjection,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    seen = []
    # LLM: 仅捕获原归档缝隙的输入，不替换业务处理器或重执行工具。
    # 函数用途: 在正式投影之前记录可信迁移信息。
    def archive(call, outcome):
        seen.append(getattr(call, "input_migration", {}))
        return ToolOutputProjection(())
    call = ToolCall(call_id="archived-v1", tool_name="list_tools", arguments={}, source_protocol="native",
        schema_hash="sha256:legacy-v1", run_id="r", turn_id="t", attempt_id="a")
    result = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=surface_agent.tools.runtime_snapshot(),
        workspace_root=tmp_path, operation_store_required=False, output_archiver=archive,
        trusted_run_context={"tool_input_version": 1})).result
    assert result.ok
    assert seen and seen[0]["requested_tool_name"] == "list_tools"
    assert result.metadata["input_migration"] == seen[0]


# LLM: 旧目录白名单只投影catalog动作，不能靠canonical名搜索加载；禁用和撤权仍有效。
# 函数用途: 通过真实快照与执行器核对兼容授权不扩张。
def test_legacy_catalog_grant_is_action_restricted_and_current_disable_wins(surface_agent, tmp_path):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    registry = surface_agent.tools
    allowed = ["list_tools"]
    snapshot = registry.runtime_snapshot(allowed_tools=allowed)
    assert allowed == ["list_tools"]
    runtime = snapshot.runtime("tool_search")
    assert runtime is not None
    assert runtime.model_spec.input_schema["properties"]["action"]["enum"] == ["catalog"]
    # LLM: 每次构造独立调用ID，不复用一次执行结果绕过授权。
    # 函数用途: 对同一冻结快照执行兼容目录或规范搜索。
    def execute(name, args, version):
        call = ToolCall(call_id=name + str(version), tool_name=name, arguments=args, source_protocol="native",
            schema_hash=runtime.model_spec.schema_hash, run_id="r", turn_id="t", attempt_id="a")
        return ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot, workspace_root=tmp_path,
            operation_store_required=False, trusted_run_context={"tool_input_version": version})).result
    assert execute("list_tools", {}, 1).ok
    assert not execute("list_tools", {"query": "read_file"}, 1).ok
    assert not execute("tool_search", {"action": "search", "names": ["read_file"]}, 2).ok
    direct = registry.tools["tool_search"].execute_scoped({"action": "search", "names": ["read_file"]},
        __import__("agent_py_agent.agent.tooling.models", fromlist=["ToolInvocationContext"]).ToolInvocationContext(snapshot))
    assert not direct.ok
    registry.disabled_tool_names.add("tool_search")
    assert registry.runtime_snapshot(allowed_tools=allowed).runtime("tool_search") is None


# LLM: 规范创建服务不推断v1形状，不接受顶层默认值广播；兼容只能由显式v1边界完成。
# 函数用途: 拦住仍留在业务层的旧单派与批次默认值分支。
@pytest.mark.parametrize("params", [
    {"goal": "old"}, {"goal": "old", "items": []},
    {"items": [{"goal": "new"}], "role": "writer"},
])
def test_canonical_create_parser_rejects_legacy_shapes(params):
    from agent_py_agent.agent.agent_core.orchestration.create_payload import (
        create_items_from_params,
    )

    assert isinstance(create_items_from_params(params), str)


# LLM: 规范数组派工经原manager和原操作账去重，局部启动回执不等于真实后台模型运行。
# 函数用途: 验证一项或多项创建在LocalStore及权威Runtime账本都保留操作幂等身份。
@pytest.mark.parametrize("managed", [False, True])
def test_canonical_create_operation_replay_preserves_child_identity(tmp_path, monkeypatch, managed):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _patch_auto_start_receipt,
        _workspace_agent,
    )

    monkeypatch.chdir(tmp_path)
    _patch_auto_start_receipt(monkeypatch)
    agent = _workspace_agent(tmp_path)
    attempt_id = "test-attempt"
    if managed:
        repo = RuntimeRepository(tmp_path / "runtime.db")
        run = repo.record_run_creation(owner_id="test-owner", goal="派工验证", run_id="test-run",
            conversation_task_id="test-run", thread_id="test-thread", role="assistant")
        attempt_id, store = run["attempt_id"], ManagedOperationStore(repo)
    else:
        store = LocalStore(tmp_path / "local.db", enable_fts=False)
    results = []
    for call_id in ("dispatch-1", "dispatch-1", "dispatch-2"):
        execution = execute_canonical_test_call(tmp_path, tools={"create_subagents": CreateSubagentsTool(agent)},
            tool_name="create_subagents", arguments={"items": [{"goal": "检查正常行为"}, {"goal": "检查错误行为"}]},
            call_id=call_id, attempt_id=attempt_id, operation_id=f"dispatch:{call_id}", idempotency_key=f"dispatch:{call_id}",
            operation_store=store, operation_store_required=True)
        assert execution.result.ok, execution.result.output
        results.append(execution.result)
    first, replay, following = results
    ids = json.loads(first.output)["created_run_ids"]
    assert len(ids) == 2
    assert json.loads(replay.output)["created_run_ids"] == ids
    assert replay.operation.replayed and not replay.handler_executed
    assert not set(json.loads(following.output)["created_run_ids"]) & set(ids)
    assert len(agent.subagents.list_runs()) == 4


# LLM: 六模式复用canonical消息和宿主会话，原文分页不依赖索引，不能借显式路径跨owner读取。
# 函数用途: 验证message、thread_search、thread_browse实际读取及身份隔离。
def test_canonical_thread_modes_preserve_original_readers_and_owner_boundary(tmp_path):
    from agent_py_agent.tests.test_session_history_read import _LONG, _owner

    owner = _owner(tmp_path / "owner-a")
    tool = SessionSearchTool(owner.agent)
    result = tool.execute({"mode": "thread_search", "query": "金额保留两位小数"})
    assert result.ok, result.output
    assert {hit["scope"]["thread_id"] for hit in json.loads(result.output)["results"]} == {owner.current.thread_id}
    chunks, offset = [], 0
    owner.agent.local_store = None
    while offset is not None:
        result = tool.execute({"mode": "message", "message_id": owner.rows[0].message_id, "offset": offset, "max_chars": 6000})
        assert result.ok, result.output
        page = json.loads(result.output)
        chunks.append(page["content"])
        offset = page["next_offset"]
    assert "".join(chunks) == _LONG
    result = tool.execute({"mode": "thread_browse"})
    assert result.ok, result.output
    assert [item["message_id"] for item in json.loads(result.output)["messages"]] == [owner.rows[i].message_id for i in (0, 1, 3)]
    foreign = _owner(tmp_path / "owner-b")
    for thread_id in (foreign.current.thread_id, str(foreign.store.storage.message_path(foreign.current.thread_id))):
        result = tool.execute({"mode": "message", "message_id": foreign.rows[0].message_id, "thread_id": thread_id})
        assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"


# LLM: v1批次继承的allowed_tools不等于item显式请求，不能在归一后误判递归派工意图。
# 函数用途: 经过真实执行器核对原显式字段事实在handler前保留。
def test_v1_create_preserves_item_explicitness_as_host_fact(surface_agent, tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.create_payload import (
        create_items_from_params,
    )
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    # LLM: 夹具只读原参数解析，不创建孩子或启动模型；继承原schema及effect门。
    # 类用途: 观察旧字段来源是否送达规范业务解析器。
    class CaptureCreate(CreateSubagentsTool):
        # LLM: 读取正式handler缝隙的宿主字段，不代写幂等或授权状态。
        # 函数用途: 回显原解析器计算的item显式请求标记。
        def execute(self, params):
            items = create_items_from_params(params)
            return ToolHandlerOutcome("create_subagents", True,
                json.dumps([item.params["_item_allowed_tools_explicit"] for item in items]))
    result = execute_canonical_test_call(tmp_path, tools={"create_subagents": CaptureCreate(surface_agent)},
        tool_name="create_subagents", arguments={"allowed_tools": ["create_subagents"], "items": [{"goal": "a"}]},
        trusted_run_context={"tool_input_version": 1}, operation_store=LocalStore(tmp_path / "ops.db"),
        operation_store_required=True).result
    assert result.ok, result.output
    assert json.loads(result.output) == [False]


# LLM: 新旧输入共用同一创建事务；原v1单派回执不能因转一项数组被投影成批次。
# 函数用途: 用真实manager验证旧单派兼容仍保持单项回执。
def test_v1_single_create_preserves_original_receipt_projection(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _patch_auto_start_receipt,
        _workspace_agent,
    )

    monkeypatch.chdir(tmp_path)
    _patch_auto_start_receipt(monkeypatch)
    agent = _workspace_agent(tmp_path)
    result = execute_canonical_test_call(tmp_path, tools={"create_subagents": CreateSubagentsTool(agent)},
        tool_name="create_subagents", arguments={"goal": "原单派任务"},
        trusted_run_context={"tool_input_version": 1}, operation_store=LocalStore(tmp_path / "ops.db"),
        operation_store_required=True).result
    assert result.ok, result.output
    payload = json.loads(result.output)
    assert payload["created"] == 1
    assert "batch_mode" not in payload
