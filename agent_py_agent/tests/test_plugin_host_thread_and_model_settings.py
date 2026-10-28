# LLM: W4直接合同测试只操作pytest临时owner及真实Store；不冒充HTTP授权、真实模型请求或运行接线验收。
# 模块用途: 验证线程设置CAS、模型私有目录和内置能力限制；替身仅用于模型压缩边界。
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.plugin_host import contracts
from agent_py_agent.agent.plugin_host.builtin_plugins_v2 import (
    list_builtin_plugins,
    resolve_builtin_plugin_restrictions,
    toggle_builtin_plugin,
)
from agent_py_agent.agent.plugin_host.conversations import (
    ConversationDomain,
    thread_scoped,
    thread_summary,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers
from agent_py_agent.agent.plugin_host.thread_settings_v2 import ThreadSettingsV2Handlers
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


@pytest.fixture
# LLM: 只创建pytest临时owner和原Store，不接真实授权发行器。
# 函数用途: 为W4直接领域合同提供隔离环境。
def case(tmp_path):
    home = resolve_owner_home(tmp_path, OwnerIdentity.local_main())
    home.home_dir.mkdir(parents=True)
    paths = home_paths_with_owner(home_paths(tmp_path), home)
    store = ConversationStore(home.data_dir / "conversations")
    thread = store.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir), cwd=str(home.home_dir))
    scope = {"scope": "owner_all", "created_by_installation": True}
    binding = SimpleNamespace(installation_ref="test-install", grant={"threads": scope})
    authorized = SimpleNamespace(binding=binding, resource_scope={"threads": scope})
    context = contracts.OwnerReadContext(home.identity, home, binding)
    service = ThreadSettingsV2Handlers(store, context, home_paths=paths)
    return SimpleNamespace(home=home, paths=paths, store=store, thread=thread, service=service,
                           authorized=authorized, context=context)


# LLM: 所有地址和key均为测试占位，不发请求。
# 函数用途: 构造可由原模型校验器读取的服务商表单。
def provider_payload(**changes):
    return {"provider_id": "example", "display_name": "Example", "api_base": "https://example.invalid/v1",
            "api_key": "TEST_KEY_NOT_A_CREDENTIAL", "session_header": "x-opencode-session",
            "custom_headers": {"x-project": "public-name"}, "models": [{"model_name": "agent-model",
                "model_backend": "openai_compatible", "model_context_window_tokens": 32768}], **changes}


# LLM: 仅测试准备，使用原私有保存函数物化真实目录。
# 函数用途: 建立一个稳定ID的聊天模型供更新和删除测试使用。
def seed_catalog(case):
    path = model_profiles_path(case.paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_model_profiles(path)
    pid = "11111111-1111-4111-8111-111111111111"
    data["providers"] = {"example": {"display_name": "Example", "api_base": "https://example.invalid/v1",
        "api_key": "TEST_KEY_NOT_A_CREDENTIAL", "session_header": "x-opencode-session", "custom_headers": {},
        "enabled": True, "capabilities": ["agentic"]}}
    data["profiles"] = {pid: {"provider_id": "example", **provider_payload()["models"][0], "capability": "agentic", "enabled": True}}
    _save_profiles(path, data)
    return pid


# LLM: 设置必须经真实Store落盘，不以mock调用次数证明选择成功。
# 函数用途: 核对双名能力包、模型名解析和显式选择版本。
def test_settings_roundtrip_aliases_and_native_model_selection(case):
    pid = seed_catalog(case)
    result = case.service.update_thread_settings({"thread_id": case.thread.thread_id, "expected_revision": 1,
        "model_profile_id": "agent-model", "reasoning_effort": "ultra", "permission_mode": "workspace-write",
        "pack_ids": ["pack-one", "pack-two"]})
    assert isinstance(result, dict)
    assert result["model_profile_id"] == pid and result["model_name"] == "agent-model"
    assert result["permission_mode"] == "auto" and result["reasoning_effort"] == "ultra"
    assert result["preferred_pack_ids"] == result["pack_ids"] == ["pack-one", "pack-two"]
    assert result["revision"] == 2
    row = case.store.threads.require(case.thread.thread_id)
    assert row.model_selection_source == "explicit" and row.model_selection_revision == 1
    assert row.model_selection_last_explicit_revision == 1
    assert case.service.get_thread_settings({"thread_id": row.thread_id}) == result


@pytest.mark.parametrize("field,value", [("reasoning_effort", "invalid"), ("reasoning_effort", True),
    ("permission_mode", "root"), ("model_profile_id", "missing"), ("preferred_pack_ids", "pack"),
    ("pack_ids", [1]), ("expected_revision", True), ("pack_ids", ["../bad"])])
# LLM: 无效请求与原文件逐字节比较，不接受部分写入。
# 函数用途: 守住设置字段类型、枚举和路径形包编号边界。
def test_invalid_settings_do_not_modify_thread(case, field, value):
    before = case.store.storage.thread_path(case.thread.thread_id).read_bytes()
    with pytest.raises(HostAPIError):
        case.service.update_thread_settings({"thread_id": case.thread.thread_id, field: value})
    assert case.store.storage.thread_path(case.thread.thread_id).read_bytes() == before


# LLM: 两个门户别名冲突时不得任选其一。
# 函数用途: 验证矛盾能力包列表明确拒绝。
def test_pack_alias_conflict_rejected(case):
    with pytest.raises(HostAPIError):
        case.service.update_thread_settings({"thread_id": case.thread.thread_id,
            "pack_ids": ["one"], "preferred_pack_ids": ["two"]})


@pytest.mark.parametrize("method", ["update_thread_settings", "compact_thread", "delete_thread"])
# LLM: 三个写动作共用canonical版本前置条件。
# 函数用途: 验证陈旧CAS的错误码及原记录保留。
def test_stale_revision_has_stable_error_and_no_write(case, method):
    with pytest.raises(HostAPIError) as error:
        getattr(case.service, method)({"thread_id": case.thread.thread_id, "expected_revision": 77})
    assert error.value.code == "REVISION_CONFLICT"
    assert case.store.threads.require(case.thread.thread_id).revision == 1


# LLM: 并发使用原文件锁，不用伪存储模拟胜负。
# 函数用途: 验证同版本竞争恰有一个成功写者。
def test_settings_concurrent_cas_one_winner(case):
    # LLM: 仅将测试异常归为可断言结果，不改变生产错误。
    # 函数用途: 竞争保存同一线程版本。
    def update(index):
        try:
            result = case.service.update_thread_settings({"thread_id": case.thread.thread_id,
                "expected_revision": 1, "pack_ids": [f"pack-{index}"]})
            return "saved" if isinstance(result, dict) else "missing"
        except HostAPIError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == ["REVISION_CONFLICT", "saved"]


@pytest.mark.parametrize("method", ["get_thread_settings", "update_thread_settings", "get_thread_usage", "compact_thread", "delete_thread"])
# LLM: 外来记录即便混入同一物理Store也不得可见。
# 函数用途: 验证各线程入口都核对owner身份及家目录。
def test_cross_owner_threads_hidden(case, method):
    foreign = case.store.threads.create(owner_id="local/bob", owner_home="/never-read-bob")
    with pytest.raises(HostAPIError) as error:
        getattr(case.service, method)({"thread_id": foreign.thread_id})
    assert error.value.code == "RESOURCE_NOT_FOUND"


# LLM: 使用生产遥测schema，未知耗时不以模型延迟冒充。
# 函数用途: 核对用量字段映射和嵌套秘密剔除。
def test_usage_from_real_persisted_schema_no_private_metadata(case):
    case.store.threads.update_atomic(case.thread.thread_id, lambda row: replace(row,
        model_context_usage={"schema": "model_visible_context_usage.v1", "compact_generation": 0,
            "current_tokens": 123, "context_window_tokens": 1000, "compact_trigger_tokens": 800, "private": "SECRET"},
        model_metrics={"schema": "model_runtime_metrics.v1", "input_tokens": 1200, "output_tokens": 300,
            "cache_read_input_tokens": 42, "cache_read_reported_calls": 1, "output_tps": 15,
            "request_main_latency_ms": 2000, "private": "SECRET"}))
    result = case.service.get_thread_usage({"thread_id": case.thread.thread_id})
    assert isinstance(result, dict)
    assert result["context_used_tokens"] == result["used_tokens"] == 123
    assert result["context_max_tokens"] == result["context_window_tokens"] == 1000
    assert result["trigger_tokens"] == 800 and result["cumulative_input_tokens"] == 1200
    assert result["cumulative_output_tokens"] == 300 and result["cached_tokens"] == 42
    assert result["last_turn_tokens_per_sec"] == 15 and result["last_turn_elapsed_seconds"] is None
    assert result["stats"]["request_main_latency_ms"] == 2000
    assert "SECRET" not in json.dumps(result)


# LLM: 没有运行时不可伪造压缩成功，拒绝不得改变线程或原消息。
# 函数用途: 验证无agent时503且保持原代次和历史。
def test_standalone_compact_boundary_persists_without_losing_messages(case):
    msg = case.store.messages.append({"thread_id": case.thread.thread_id, "role": "user", "content": "Keep me"})
    before = case.store.storage.thread_path(case.thread.thread_id).read_bytes()
    revision = case.service.get_thread_settings({"thread_id": case.thread.thread_id})["revision"]
    with pytest.raises(HostAPIError) as error:
        case.service.compact_thread({"thread_id": case.thread.thread_id, "expected_revision": revision, "instruction": "retain facts"})
    assert (error.value.code, error.value.status) == ("HOST_NOT_READY", 503)
    row = case.store.threads.require(case.thread.thread_id)
    assert row.compact_generation == 0 and "plugin_host_v2" not in row.metadata
    assert case.store.storage.thread_path(row.thread_id).read_bytes() == before
    assert case.store.messages.recent(row.thread_id, limit=0)[0].message_id == msg.message_id


# LLM: 只替换模型准备端口，选项、结果类型和存储均真实。
# 函数用途: 验证手动强制压缩与自定义指令传递。
def test_agent_compact_uses_real_options_and_result(case, monkeypatch):
    from agent_py_agent.agent.conversation import compact
    agent = object()
    captured = []
    # LLM: 不调用模型或网络，保留原选项合同及真实Store写入。
    # 函数用途: 记录参数并返回一份可核验的压缩结果。
    def prepare(actual_agent, store, thread, *, options):
        captured.append(options)
        assert actual_agent is agent and store is case.store
        updated = store.threads.update_atomic(thread.thread_id, lambda row: replace(row, compact_generation=1, summary="facts"))
        return compact.ConversationCompactResult(updated, [], 10, 80, compacted=True)
    monkeypatch.setattr(compact, "prepare_conversation_context", prepare)
    handler = ThreadSettingsV2Handlers(case.store, case.context, agent=agent, home_paths=case.paths)
    result = handler.compact_thread({"thread_id": case.thread.thread_id, "instruction": "retain facts"})
    assert isinstance(result, dict)
    assert captured[0].force and captured[0].bypass_circuit_breaker
    assert captured[0].custom_instructions == "retain facts" and result["summary_preview"] == "facts"


@pytest.mark.parametrize("busy", ["active_task", "claim"])
# LLM: 活跃索引和真实claim分别覆盖，不能静默停止它们。
# 函数用途: 验证忙碌线程不得删除。
def test_delete_rejects_active_turn(case, busy):
    tid = case.thread.thread_id
    if busy == "claim":
        assert case.store.claims.acquire({"thread_id": tid, "task_id": "turn"})
    else:
        case.store.threads.update_atomic(tid, lambda row: replace(row, active_task_ids=("turn",)))
    with pytest.raises(HostAPIError) as error:
        case.service.delete_thread({"thread_id": tid})
    assert error.value.code == "THREAD_BUSY" and case.store.threads.load(tid)


# LLM: 删除前先建立原线程缓存，删除后重新走原读取口。
# 函数用途: 验证线程文件和缓存同时失效。
def test_delete_removes_record_and_cached_projection(case):
    assert case.store.threads.load(case.thread.thread_id)
    result = case.service.delete_thread({"thread_id": case.thread.thread_id, "expected_revision": 1})
    assert result == {"thread_id": case.thread.thread_id, "deleted": True}
    assert case.store.threads.load(case.thread.thread_id) is None


@pytest.mark.parametrize("scope", ["owner", "owner_all", "all"])
# LLM: grant与本次resource_scope必须取交集，不能由请求自扩权。
# 函数用途: 验证三种owner范围名称和单层授权不足。
def test_owner_all_requires_both_scopes(case, scope):
    case.authorized.binding.grant["threads"]["scope"] = scope
    assert thread_scoped(case.thread.thread_id, {}, case.authorized)
    case.authorized.resource_scope = {"threads": {"created_by_installation": True}}
    assert not thread_scoped(case.thread.thread_id, {}, case.authorized)


# LLM: 分叉使用原服务及消息文件，不替换发布或谱系算法。
# 函数用途: 验证创建偏好、摘要和消息边界别名继承。
def test_create_summary_and_fork_copy_settings_and_alias(case):
    pid = seed_catalog(case)
    domain = ConversationDomain(None, None, default_thread=lambda *_: False)
    row = domain.new_thread({"workspace_ref": "default", "title": "created", "model_profile_id": pid,
        "reasoning_effort": "max", "permission_mode": "auto", "preferred_pack_ids": ["pack-one"]},
        case.authorized, case.context)
    summary = thread_summary(row, case.authorized)
    assert summary.get("model_profile_id") == pid and summary.get("reasoning_effort") == "max"
    assert summary.get("pack_ids") == summary.get("preferred_pack_ids") == ["pack-one"]
    assert summary.get("permission_mode") == "auto" and summary.get("compact_generation") == 0
    case.store.threads.write(row)
    message = case.store.messages.append({"thread_id": row.thread_id, "role": "user", "content": "fork here"})
    branch = domain.fork(case.store, {"source_thread_id": row.thread_id, "source_message_id": message.message_id},
                         case.authorized, case.context)
    assert isinstance(branch, dict)
    forked = case.store.threads.require(branch["thread_id"])
    assert forked.model_profile_id == pid and forked.reasoning_effort == "max"
    assert forked.metadata["plugin_host_v2"]["preferred_pack_ids"] == ["pack-one"]
    assert forked.metadata["plugin_host_v2"]["permission_mode"] == "auto"


@pytest.mark.parametrize("key", [None, "", "***"])
# LLM: 只在私有临时目录检查伪key，不触碰本人凭据。
# 函数用途: 验证三种保key更新和目录代次轮换。
def test_provider_update_preserves_secret_and_rotates_generation(case, key):
    seed_catalog(case)
    handler = ModelSettingsV2Handlers(case.paths)
    before = read_model_profiles(model_profiles_path(case.paths))
    payload = provider_payload()
    if key is None:
        payload.pop("api_key")
    else:
        payload["api_key"] = key
    result = handler.save_provider(payload)
    assert isinstance(result, dict)
    after = read_model_profiles(model_profiles_path(case.paths))
    assert after["providers"]["example"]["api_key"] == "TEST_KEY_NOT_A_CREDENTIAL"
    assert after["catalog_generation"] != before["catalog_generation"]
    assert len(after["profiles"]) == 1 and "TEST_KEY" not in json.dumps(result)


# LLM: 公共结果不得带私有key字段或伪key正文。
# 函数用途: 核对服务商卡片和八档推理强度目录。
def test_provider_list_filters_and_never_leaks_key_or_oauth(case):
    seed_catalog(case)
    handler = ModelSettingsV2Handlers(case.paths)
    result = handler.list_providers()
    assert isinstance(result, dict)
    assert result["providers"][0]["has_key"] and result["providers"][0]["api_key_masked"] == "***"
    assert result["providers"][0]["session_header"] == "x-opencode-session"
    assert set(result["reasoning_efforts"]) == {"off", "low", "medium", "high", "xhigh", "max", "ultra", "auto"}
    assert "TEST_KEY" not in json.dumps(result)
    assert all("api_key" not in row and "auth" not in row for row in result["providers"])
    assert handler.list_providers({"capability_filter": "all"})["providers"]


@pytest.mark.parametrize("changes", [{"provider_id": "../bad"}, {"api_base": "https://user:secret@example.invalid"},
    {"session_header": "bad\nheader"}, {"custom_headers": {"x-one": 2}}, {"models": [{}]}, {"models": "model"}])
# LLM: 原生表单校验失败后不能创建半份模型目录。
# 函数用途: 覆盖服务商编号、地址、请求头和模型结构错误。
def test_invalid_provider_does_not_write(case, changes):
    with pytest.raises(HostAPIError):
        ModelSettingsV2Handlers(case.paths).save_provider(provider_payload(**changes))
    assert not model_profiles_path(case.paths).exists()


@pytest.mark.parametrize("target", ["provider_id", "profile_id"])
# LLM: 删除只作用本owner真实目录，当前选择只能回默认。
# 函数用途: 核对级联删除和单profile删除的区别。
def test_delete_provider_cascade_or_selected_profile_resets_default(case, target):
    pid = seed_catalog(case)
    path = model_profiles_path(case.paths)
    data = read_model_profiles(path)
    data["selected"] = pid
    _save_profiles(path, data)
    result = ModelSettingsV2Handlers(case.paths).delete_provider_or_profile({target: "example" if target == "provider_id" else pid})
    assert isinstance(result, dict)
    after = read_model_profiles(path)
    assert after["selected"] == "default" and not after["profiles"]
    assert bool(after["providers"]) == (target == "profile_id")


CATALOG = {"persona", "agent-instructions", "time-context", "tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search",
           "tool-jobs", "tool-schedule", "local-skills", "goal-runner", "skill-recorder"}


# LLM: 开关与运行限制投影是不同观察点，不声称turn已接线。
# 函数用途: 验证12项开关持久化及线程覆盖默认值。
def test_builtin_catalog_and_thread_override_persist(case):
    initial = list_builtin_plugins(case.home.home_dir)
    assert isinstance(initial, dict)
    assert {row["plugin_id"] for row in initial["plugins"]} == CATALOG
    assert all(row["enabled"] for row in initial["plugins"])
    toggle_builtin_plugin(case.home.home_dir, plugin_id="persona", enabled=False)
    toggle_builtin_plugin(case.home.home_dir, plugin_id="persona", enabled=True, thread_id=case.thread.thread_id)
    assert resolve_builtin_plugin_restrictions(case.home.home_dir)["skip_persona"]
    assert not resolve_builtin_plugin_restrictions(case.home.home_dir, thread_id=case.thread.thread_id)["skip_persona"]
    assert (case.home.data_dir / "plugin_host" / "builtin_features.json").is_file()


@pytest.mark.parametrize("plugin,tool", [("tool-bash", "run_command"), ("tool-pwsh", "run_command"),
    ("tool-fs", "read_file"), ("tool-fs-search", "search_text"), ("tool-jobs", "process_session"),
    ("tool-schedule", "schedule"), ("goal-runner", "create_goal"), ("skill-recorder", "skill_summarize")])
# LLM: 只核实际工具名投影，不发起任何工具执行。
# 函数用途: 验证各类插件禁用后对应工具在裁剪集合内。
def test_builtin_disabled_tools_projected(case, plugin, tool):
    toggle_builtin_plugin(case.home.home_dir, plugin_id=plugin, enabled=False)
    result = resolve_builtin_plugin_restrictions(case.home.home_dir)
    assert isinstance(result, dict)
    assert plugin in result["disabled_plugin_ids"] and tool in result["disabled_tool_names"]


@pytest.mark.parametrize("plugin,enabled", [("unknown", False), ("persona", "false")])
# LLM: 布尔字符串不能被truthy转换成启用。
# 函数用途: 拒绝未知插件及非布尔开关。
def test_builtin_invalid_inputs_rejected(case, plugin, enabled):
    with pytest.raises(HostAPIError):
        toggle_builtin_plugin(case.home.home_dir, plugin_id=plugin, enabled=enabled)


@pytest.mark.parametrize("mode,expected", [("ask", "ask"), ("auto", "auto"), ("full-access", "full-access"),
    ("read-only", "ask"), ("workspace-write", "auto"), ("danger-full-access", "full-access")])
# LLM: 沿原审批设置口落盘，测试不赋予真实宿主权限。
# 函数用途: 核对三档模式及三个旧别名的owner默认继承。
def test_owner_permission_default_uses_native_policy_and_is_inherited(case, mode, expected):
    result = case.service.update_thread_settings({"permission_mode": mode})
    assert isinstance(result, dict) and result["mode"] == expected
    assert json.loads(case.paths.owner_tool_policy_json.read_text())["permission_mode"] == expected
    assert case.service.get_thread_settings({"thread_id": case.thread.thread_id})["permission_mode"] == expected


# LLM: Alice为独立普通用户，不能借插件设置取得管理员权限。
# 函数用途: 验证owner和thread两种Full Access写入均拒绝。
def test_nonadmin_full_access_is_rejected_for_owner_and_thread(case):
    home = resolve_owner_home(case.home.root, OwnerIdentity.local_user("alice"))
    paths = home_paths_with_owner(home_paths(home.root), home)
    context = contracts.OwnerReadContext(home.identity, home, case.context.source_binding)
    store = ConversationStore(home.data_dir / "conversations")
    row = store.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir))
    handler = ThreadSettingsV2Handlers(store, context, home_paths=paths)
    for params in ({}, {"thread_id": row.thread_id}):
        with pytest.raises(HostAPIError) as error:
            handler.update_thread_settings({**params, "permission_mode": "full-access"})
        assert error.value.code == "CAPABILITY_DENIED"


# LLM: 既有损坏状态必须失败关闭，不能误认为首次使用。
# 函数用途: 验证空对象文件不被全开默认覆盖。
def test_builtin_empty_existing_state_is_corruption_not_default(case):
    path = case.home.data_dir / "plugin_host" / "builtin_features.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    with pytest.raises(HostAPIError) as error:
        resolve_builtin_plugin_restrictions(case.home.home_dir)
    assert error.value.code == "CORRUPT_PLUGIN_STATE"
    assert path.read_text() == "{}"


# LLM: 宿主路径与owner上下文也必须一致，不仅检查线程字段。
# 函数用途: 拒绝混用另一个owner的模型/策略路径。
def test_home_paths_cannot_target_another_owner(case):
    other = resolve_owner_home(case.home.root, OwnerIdentity.local_user("alice"))
    with pytest.raises(HostAPIError):
        ThreadSettingsV2Handlers(case.store, case.context,
            home_paths=home_paths_with_owner(home_paths(other.root), other))


# LLM: 只验证领域schema，A0动作目录和HTTP接线不在本测试范围。
# 函数用途: 验证创建的可选偏好不会被领域闭合校验剔除。
def test_create_payload_accepts_settings_before_w5_route_wiring(case):
    from agent_py_agent.agent.plugin_host.conversations import domain_payload
    payload = domain_payload(contracts.Call("plugin_host_call.v2", "conversations.threads.create",
        {"title": "with prefs", "reasoning_effort": "high", "pack_ids": ["pack-one"]}))
    assert payload["reasoning_effort"] == "high" and payload["pack_ids"] == ["pack-one"]


# LLM: 回执必须与最后写入的新线程一致，不能回旧发布快照。
# 函数用途: 验证分叉返回值包含已绑定的能力包偏好。
def test_fork_returns_current_thread_settings_not_pre_bind_snapshot(case):
    case.service.update_thread_settings({"thread_id": case.thread.thread_id, "pack_ids": ["pack-one"]})
    result = case.service.fork_thread({"source_thread_id": case.thread.thread_id})
    assert result["thread"]["metadata"]["plugin_host_v2"]["preferred_pack_ids"] == ["pack-one"]


# LLM: 读取不建目录/锁，写入竞争使用原路径锁。
# 函数用途: 同时验证纯读取与两个开关更新不互相丢失。
def test_builtin_helper_is_readonly_and_parallel_writers_keep_both_updates(case):
    home = case.home.home_dir / "new-owner"
    assert not home.exists()
    assert not resolve_builtin_plugin_restrictions(home)["disabled_plugin_ids"]
    assert not home.exists()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda key: toggle_builtin_plugin(home, plugin_id=key, enabled=False), ["persona", "local-skills"]))
    assert set(resolve_builtin_plugin_restrictions(home)["disabled_plugin_ids"]) == {"persona", "local-skills"}


# LLM: 稳定ID不能成为搬运另一连接模型的入口。
# 函数用途: 验证跨服务商ID覆盖被拒绝且目录字节不变。
def test_profile_upsert_cannot_move_another_provider_model(case):
    pid = seed_catalog(case)
    payload = provider_payload(provider_id="other")
    payload["models"][0]["profile_id"] = pid
    before = model_profiles_path(case.paths).read_bytes()
    with pytest.raises(HostAPIError):
        ModelSettingsV2Handlers(case.paths).save_provider(payload)
    assert model_profiles_path(case.paths).read_bytes() == before


# LLM: 可加的模型时长不等于包含工具/并发工作的墙钟。
# 函数用途: 验证没有回合耗时事实时明确返回未知。
def test_request_model_latency_is_not_turn_wall_clock(case):
    case.store.threads.update_atomic(case.thread.thread_id, lambda row: replace(row,
        model_metrics={"schema": "model_runtime_metrics.v1", "request_main_latency_ms": 2000}))
    result = case.service.get_thread_usage({"thread_id": case.thread.thread_id})
    assert result["stats"]["request_main_latency_ms"] == 2000
    assert result["last_turn_elapsed_seconds"] is None


# LLM: 只控制压缩端口交错，竞争状态写入真实Store。
# 函数用途: 防止迟到Compact边界污染新代次并核实committed结局。
def test_agent_compact_race_cannot_relabel_another_generation(case, monkeypatch):
    from agent_py_agent.agent.conversation import compact
    # LLM: 模拟两次已提交压缩，不把替身结果当真实模型压缩证据。
    # 函数用途: 在A返回前提交B的代次，建立可复现竞争窗口。
    def prepare(_agent, store, thread, *, options):
        ours = store.threads.update_atomic(thread.thread_id, lambda row: replace(row, compact_generation=1, summary="ours"))
        store.threads.update_atomic(thread.thread_id, lambda row: replace(row, compact_generation=2, revision=2,
            summary="theirs", metadata={"plugin_host_v2": {"revision": 1,
                "compact_boundary": {"generation": 2, "instruction": "theirs"}}}))
        return compact.ConversationCompactResult(ours, [], 10, 80, compacted=True)
    monkeypatch.setattr(compact, "prepare_conversation_context", prepare)
    handler = ThreadSettingsV2Handlers(case.store, case.context, agent=object(), home_paths=case.paths)
    with pytest.raises(HostAPIError) as error:
        handler.compact_thread({"thread_id": case.thread.thread_id, "instruction": "ours"})
    assert error.value.code == "REVISION_CONFLICT" and error.value.outcome == "committed"
    assert case.store.threads.require(case.thread.thread_id).metadata["plugin_host_v2"]["compact_boundary"]["instruction"] == "theirs"


# LLM: 陈旧版本不得留下半个新分支。
# 函数用途: 验证源线程CAS失败时没有新记录。
def test_fork_checks_optional_source_revision_before_creating(case):
    before = case.store.threads.list(limit=0)
    with pytest.raises(HostAPIError) as error:
        case.service.fork_thread({"source_thread_id": case.thread.thread_id, "expected_revision": 99})
    assert error.value.code == "REVISION_CONFLICT"
    assert case.store.threads.list(limit=0) == before


# LLM: Coding表单可同时提供provider和profile，不等于整连接删除。
# 函数用途: 验证组合选择器只删除指定模型。
def test_delete_profile_with_provider_selector_keeps_provider(case):
    pid = seed_catalog(case)
    ModelSettingsV2Handlers(case.paths).delete_provider_or_profile({"provider_id": "example", "profile_id": pid})
    data = read_model_profiles(model_profiles_path(case.paths))
    assert not data["profiles"] and "example" in data["providers"]


# LLM: 精确ID是稳定身份，显示名重名不能使其失效。
# 函数用途: 核对ID优先于另一个模型的同名字符串。
def test_explicit_profile_id_wins_over_another_models_name(case):
    pid = seed_catalog(case)
    path = model_profiles_path(case.paths)
    data = read_model_profiles(path)
    data["profiles"]["22222222-2222-4222-8222-222222222222"] = {**data["profiles"][pid], "model_name": pid}
    _save_profiles(path, data)
    result = case.service.update_thread_settings({"thread_id": case.thread.thread_id, "model_profile_id": pid})
    assert result["model_profile_id"] == pid


# LLM: 当前实现的Windows和POSIX共用shell工具，不存在独立run_powershell。
# 函数用途: 验证PowerShell开关裁剪真实入口而不是虚构名称。
def test_pwsh_restriction_covers_actual_shared_shell_entrypoints(case):
    toggle_builtin_plugin(case.home.home_dir, plugin_id="tool-pwsh", enabled=False)
    restrictions = resolve_builtin_plugin_restrictions(case.home.home_dir)
    assert {"run_command", "terminal_session"} <= set(restrictions["disabled_tool_names"])


# LLM: 名称只取显式部署配置，不从profile名称猜测。
# 函数用途: 验证有agent时可返回真实默认模型显示名。
def test_default_model_name_from_available_agent_configuration(case):
    case.service.update_thread_settings({"thread_id": case.thread.thread_id, "model_profile_id": "default"})
    handler = ThreadSettingsV2Handlers(case.store, case.context, home_paths=case.paths,
        agent=SimpleNamespace(config=SimpleNamespace(model_name="deployment-model")))
    assert handler.get_thread_settings({"thread_id": case.thread.thread_id})["model_name"] == "deployment-model"


# LLM: 伪凭据只在临时私有目录；使用原OAuth校验读取，不把任意mock字典当目录事实。
# 函数用途: 防止静态普通头重用key/token后绕过公开列表的脱敏。
@pytest.mark.parametrize("auth_kind", ["api_key", "oauth"])
def test_provider_secret_cannot_reappear_under_public_header_name(case, auth_kind):
    seed_catalog(case)
    path = model_profiles_path(case.paths)
    data = read_model_profiles(path)
    provider = data["providers"]["example"]
    marker = "TEST_PRIVATE_VALUE_NOT_A_REAL_SECRET"
    provider["custom_headers"] = {"x-project": marker}
    if auth_kind == "api_key":
        provider["api_key"] = marker
    else:
        provider["api_key"] = ""
        provider["auth"] = {"mode": "oauth_device", "client_id": "test-client", "generation": "test-generation",
            "access_token": marker, "device_url": "https://example.invalid/device", "token_url": "https://example.invalid/token"}
    _save_profiles(path, data)
    public = ModelSettingsV2Handlers(case.paths).list_providers()
    assert marker not in json.dumps(public)
    assert public["providers"][0]["custom_headers"]["x-project"] == "***"


@pytest.mark.parametrize("migrated", [False, True])
# LLM: 插件原计数仍逐次+1；R3消息字节增量参与公共修订，不能再用裸计数代替公共CAS。
# 函数用途: 验证首次设置的修订与后续消息更新统一。
def test_r2_settings_and_summary_share_revision_after_every_write(case, migrated):
    tid = case.thread.thread_id
    if migrated:
        case.store.threads.update_atomic(tid, lambda row: replace(row, metadata={"plugin_host_v2": {"revision": 1}}))
    updated = case.service.update_thread_settings({"thread_id": tid, "expected_revision": 1, "pack_ids": ["one"]})
    row = case.store.threads.require(tid)
    assert updated["revision"] == thread_summary(row, case.authorized)["revision"] == 2
    case.store.messages.append({"thread_id": tid, "role": "user", "content": "native append"})
    row = case.store.threads.require(tid)
    state = row.metadata["plugin_host_v2"]
    assert state["revision"] == 3
    expected = 3 + case.store.storage.message_path(tid).stat().st_size - state["msg_bytes"]
    assert case.service.get_thread_settings({"thread_id": tid})["revision"] == thread_summary(row, case.authorized)["revision"] == expected
    with pytest.raises(HostAPIError) as error:
        case.service.update_thread_settings({"thread_id": tid, "expected_revision": 2, "pack_ids": []})
    assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)


@pytest.mark.parametrize("migrated", [False, True])
@pytest.mark.parametrize("method", ["update_thread_settings", "compact_thread", "delete_thread", "fork_thread"])
# LLM: 真实原生选择入口不联网，所有设置写动作都须拒绝其之前的版本。
# 函数用途: 验证原生显式切模会使已读CAS失效。
def test_r2_native_model_selection_invalidates_all_settings_cas(case, monkeypatch, migrated, method):
    from agent_py_agent.agent.settings import thread_model_selection as native
    tid = case.thread.thread_id
    if migrated:
        case.store.threads.update_atomic(tid, lambda row: replace(row, metadata={"plugin_host_v2": {"revision": 1}}))
    old = case.service.get_thread_settings({"thread_id": tid})["revision"]
    monkeypatch.setattr(native, "selected_model_config", lambda *_args, **_kwargs: None)
    native.thread_model_profile_id(SimpleNamespace(conversation_store=case.store, home_paths=case.paths), tid, select="default")
    params = {"source_thread_id" if method == "fork_thread" else "thread_id": tid, "expected_revision": old, "pack_ids": []}
    with pytest.raises(HostAPIError) as error:
        getattr(case.service, method)(params)
    assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)


# LLM: 旧摘要的hash修订仍是合法迁移前置条件，不得强迫客户端丢弃CAS。
# 函数用途: 验证未迁移线程接收threads.get读出的版本。
def test_r2_unmigrated_summary_revision_can_initialize_settings(case):
    version = thread_summary(case.thread, case.authorized)["revision"]
    result = case.service.update_thread_settings({"thread_id": case.thread.thread_id, "expected_revision": version, "pack_ids": []})
    assert result["revision"] == 2


# LLM: 删除仅移除目标线程的原文件和索引，其他线程必须逐值保留。
# 函数用途: 验证消息JSONL、通道绑定及最近线程索引均清理。
def test_r2_delete_cleans_messages_bindings_and_latest_without_touching_others(case):
    from agent_py_agent.agent.conversation.message_selection import message_position_index
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    tid, storage = case.thread.thread_id, case.store.storage
    other = case.store.threads.create(owner_id=case.home.owner_id, owner_home=str(case.home.home_dir))
    for row in (case.thread, other):
        case.store.messages.append({"thread_id": row.thread_id, "role": "user", "content": "keep only other"})
        path = storage.message_path(row.thread_id)
        assert message_position_index(path, path.stat().st_size).by_id
    for path in (storage.bindings_path, storage.user_latest_path):
        write_json_file_atomic(path, {"target": tid, "unrelated": other.thread_id})
    case.service.delete_thread({"thread_id": tid})
    assert not storage.thread_path(tid).exists() and not storage.message_path(tid).exists()
    assert case.store.messages.recent(tid, limit=0) == []
    assert case.store.messages.recent(other.thread_id, limit=0)[0].content == "keep only other"
    for path in (storage.bindings_path, storage.user_latest_path):
        assert json.loads(path.read_text()) == {"unrelated": other.thread_id}


@pytest.mark.parametrize("busy", ["claim", "active_task", "goal"])
# LLM: 忙碌检查必须发生在慢模型端口之前，使用真实claim/goal存储。
# 函数用途: 验证忙碌压缩不调用prepare且不写线程。
def test_r2_compact_rejects_busy_before_prepare(case, monkeypatch, busy):
    from agent_py_agent.agent.conversation import compact
    tid = case.thread.thread_id
    if busy == "claim":
        assert case.store.claims.acquire({"thread_id": tid, "task_id": "active"})
    elif busy == "active_task":
        case.store.threads.update_atomic(tid, lambda row: replace(row, active_task_ids=("active",)))
    else:
        case.store.goals.create({"thread_id": tid, "objective": "active goal"})
    prepare = []
    # LLM: 若误调用端口，返回合法但未压缩的结果，让断言精确失败而非因替身类型出错。
    # 函数用途: 记录是否越过忙碌保护。
    def prepare_port(*_args, **_kwargs):
        prepare.append(True)
        return compact.ConversationCompactResult(case.store.threads.require(tid), [], 10, 80, compacted=False)
    monkeypatch.setattr(compact, "prepare_conversation_context", prepare_port)
    before = case.store.storage.thread_path(tid).read_bytes()
    handler = ThreadSettingsV2Handlers(case.store, case.context, agent=object(), home_paths=case.paths)
    with pytest.raises(HostAPIError) as error:
        handler.compact_thread({"thread_id": tid})
    assert (error.value.code, error.value.status) == ("THREAD_BUSY", 409)
    assert not prepare and case.store.storage.thread_path(tid).read_bytes() == before


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("advice_key", ["host_subagent_model_advice.v1", "subagent_model_advice"])
# LLM: 校准及建议来自真实持久线程，同值重选仍应终结pending但保留校准。
# 函数用途: 验证Web选模与原生入口的模型变更副作用一致。
def test_r2_web_selection_preserves_native_side_effects(case, changed, advice_key):
    pid = seed_catalog(case)
    tid = case.thread.thread_id
    case.service.update_thread_settings({"thread_id": tid, "model_profile_id": pid})
    before = case.store.threads.update_atomic(tid, lambda row: replace(row,
        provider_context_observation={"test": "observation"}, model_context_usage={"test": "usage"},
        metadata={**row.metadata, advice_key: {"status": "pending", "operation_id": "keep"}}))
    case.service.update_thread_settings({"thread_id": tid, "model_profile_id": "default" if changed else pid})
    after = case.store.threads.require(tid)
    assert after.provider_context_observation == ({} if changed else before.provider_context_observation)
    assert after.model_context_usage == ({} if changed else before.model_context_usage)
    assert after.metadata[advice_key] == {"status": "retained", "operation_id": "keep", "reason": "explicit_model_selection"}


# LLM: 所有自定义头值都按潜在秘密处理，脱敏读回保存不能覆盖私有原值。
# 函数用途: 验证非典型敏感头、双has_key别名及完整往返保留。
def test_r2_all_custom_headers_redacted_and_roundtrip_preserved(case):
    handler = ModelSettingsV2Handlers(case.paths)
    headers = {"X-Password": "TEST_PASSWORD", "x-private-session": "TEST_SESSION", "x-project": "public-name", "x-empty": ""}
    public = handler.save_provider(provider_payload(custom_headers=headers))["providers"][0]
    assert public["custom_headers"] == {key: "***" if value else "" for key, value in headers.items()}
    assert public["custom_header_names"] == sorted(headers)
    assert public["has_api_key"] is public["has_key"] is True
    handler.save_provider(provider_payload(api_key="***", custom_headers=public["custom_headers"]))
    assert read_model_profiles(model_profiles_path(case.paths))["providers"]["example"]["custom_headers"] == headers


# LLM: 摘要不缓存owner默认，权限及数值用量都来自原读取函数。
# 函数用途: 验证无线程覆盖时摘要与设置动态继承默认模式。
def test_r2_summary_inherits_owner_permission_and_public_context_usage(case):
    from agent_py_agent.agent.conversation.context_usage import context_usage_from_thread
    tid = case.thread.thread_id
    case.service.update_thread_settings({"permission_mode": "auto"})
    row = case.store.threads.update_atomic(tid, lambda row: replace(row,
        model_context_usage={"schema": "model_visible_context_usage.v1", "compact_generation": 0, "current_tokens": 123,
            "context_window_tokens": 1000, "compact_trigger_tokens": 800, "private": "SECRET"}))
    summary = thread_summary(row, case.authorized)
    assert summary["permission_mode"] == case.service.get_thread_settings({"thread_id": tid})["permission_mode"] == "auto"
    assert summary["context_usage"] == context_usage_from_thread(row)
    assert summary["context_usage"]["current_tokens"] == 123 and "SECRET" not in json.dumps(summary)


# LLM: 新增的plugin_host父级不能成为跟随链接写出owner的路径。
# 函数用途: 验证开关目录是链接时读写均拒绝且不修改目标。
def test_r2_builtin_rejects_symlinked_plugin_host_parent(case, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (case.home.data_dir / "plugin_host").symlink_to(target, target_is_directory=True)
    for operation in (lambda: list_builtin_plugins(case.home.home_dir),
                      lambda: toggle_builtin_plugin(case.home.home_dir, plugin_id="persona", enabled=False)):
        with pytest.raises(HostAPIError) as error:
            operation()
        assert error.value.code == "INVALID_SCHEMA"
    assert not list(target.iterdir())


@pytest.mark.parametrize("changes", [{"compact_generation": 1}, {"summary": "native summary"}, {"revision": 2}])
# LLM: 旧线程的原生压缩/摘要提交未必推进目录修订，不能继续接收已读版本1。
# 函数用途: 验证迁移前的原生更新也会使陈旧设置CAS失效。
def test_r2_unmigrated_native_compact_summary_and_workspace_invalidate_cas(case, changes):
    tid = case.thread.thread_id
    case.store.threads.update_atomic(tid, lambda row: replace(row, **changes))
    with pytest.raises(HostAPIError) as error:
        case.service.update_thread_settings({"thread_id": tid, "expected_revision": 1, "pack_ids": []})
    assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)


@pytest.mark.parametrize("bound_default", [False, True])
@pytest.mark.parametrize("layout", ["data", "legacy"])
@pytest.mark.parametrize("method", ["update_thread_settings", "delete_thread", "fork_thread", "compact_thread"])
# LLM: 原生首次消息前绝不调用设置写；固定时间戳让版本变化只能来自消息账本，四入口均须无写拒绝。
# 函数用途: 覆盖空模型和默认模型初始绑定、两种Store目录及连续追加的旧CAS拒绝。
def test_r3_unmigrated_appends_invalidate_all_cas(case, bound_default, layout, method):
    root = (case.home.data_dir if layout == "data" else case.home.home_dir) / "conversations"
    store = ConversationStore(root, model_default=(lambda: "default") if bound_default else None)
    row = store.threads.create(owner_id=case.home.owner_id, owner_home=str(case.home.home_dir))
    handler = ThreadSettingsV2Handlers(store, case.context, home_paths=case.paths)
    tid = row.thread_id
    assert "plugin_host_v2" not in row.metadata
    assert row.model_profile_id == ("default" if bound_default else "")
    old = handler.get_thread_settings({"thread_id": tid})["revision"]
    assert old == thread_summary(row, case.authorized)["revision"] == 1
    for content in ("first", "second"):
        store.messages.append({"thread_id": tid, "role": "user", "content": content, "now": row.updated_at})
        latest = store.threads.require(tid)
        current = handler.get_thread_settings({"thread_id": tid})["revision"]
        assert current == thread_summary(latest, case.authorized)["revision"] > 1
        assert current != old and latest.updated_at == row.updated_at
        before = store.storage.thread_path(tid).read_bytes()
        params = {"source_thread_id" if method == "fork_thread" else "thread_id": tid, "expected_revision": old}
        with pytest.raises(HostAPIError) as error:
            getattr(handler, method)(params)
        assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)
        assert store.storage.thread_path(tid).read_bytes() == before
        old = current
    assert len(store.messages.recent(tid, limit=0)) == 2


@pytest.mark.parametrize("title", ["", "旧线程"])
# LLM: 自定义标题的旧线程也必须从同一个读口取得CAS，不接受旧设置基数作为第二版本。
# 函数用途: 验证摘要与设置在迁移前一致，标题变化后拒绝已读修订。
def test_r3_legacy_titles_share_one_revision(case, title):
    row = case.store.threads.create(owner_id=case.home.owner_id, owner_home=str(case.home.home_dir), title=title)
    old = case.service.get_thread_settings({"thread_id": row.thread_id})["revision"]
    assert old == thread_summary(row, case.authorized)["revision"]
    changed = case.store.threads.update_atomic(row.thread_id, lambda current: replace(current, title="changed"))
    assert case.service.get_thread_settings({"thread_id": row.thread_id})["revision"] == thread_summary(changed, case.authorized)["revision"] != old
    for version in {old, 1}:
        with pytest.raises(HostAPIError) as error:
            case.service.update_thread_settings({"thread_id": row.thread_id, "expected_revision": version})
        assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)


# LLM: 同时间戳使原Store的插件计数不变，消息账增长仍必须逐次改版本；设置再写不能重置字节锚。
# 函数用途: 验证已迁移线程的连续同tick追加、拒旧CAS和新CAS可提交。
def test_r3_migrated_same_tick_appends_keep_advancing_and_settings_anchor(case):
    tid = case.thread.thread_id
    old = case.service.update_thread_settings({"thread_id": tid, "pack_ids": ["one"]})["revision"]
    row = case.store.threads.require(tid)
    native = row.metadata["plugin_host_v2"]["revision"]
    for content in ("first", "second"):
        case.store.messages.append({"thread_id": tid, "role": "user", "content": content, "now": row.updated_at})
        latest = case.store.threads.require(tid)
        assert latest.metadata["plugin_host_v2"]["revision"] == native
        current = case.service.get_thread_settings({"thread_id": tid})["revision"]
        assert current == thread_summary(latest, case.authorized)["revision"] > old
        with pytest.raises(HostAPIError) as error:
            case.service.update_thread_settings({"thread_id": tid, "expected_revision": old, "pack_ids": []})
        assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)
        old = current
    updated = case.service.update_thread_settings({"thread_id": tid, "expected_revision": old, "pack_ids": ["two"]})
    assert updated["revision"] == old + 1
    with pytest.raises(HostAPIError) as error:
        case.service.update_thread_settings({"thread_id": tid, "expected_revision": old, "pack_ids": []})
    assert error.value.code == "REVISION_CONFLICT"


# LLM: 仅替换授权重核端口，不冒充HTTP认证；原领域CAS、原子存储和幂等回执均真实执行。
# 函数用途: 验证消息附加修订参与改名后，回执版本仍与真实落盘一致。
def test_r3_message_revision_rename_receipt_matches_persisted_state(case, monkeypatch):
    from agent_py_agent.agent.plugin_host import conversations
    tid = case.thread.thread_id
    case.service.update_thread_settings({"thread_id": tid, "pack_ids": []})
    row = case.store.threads.require(tid)
    case.store.messages.append({"thread_id": tid, "role": "user", "content": "same tick", "now": row.updated_at})
    old = case.service.get_thread_settings({"thread_id": tid})["revision"]
    authorized = case.authorized
    authorized.call = SimpleNamespace(idempotency_key="r3_rename_receipt_001")
    authorized.binding.activation_id = "r3-test"
    monkeypatch.setattr(conversations, "read_context", lambda *_: case.context)
    domain = ConversationDomain(None, None, default_thread=lambda *_: False)
    payload = {"thread_id": tid, "title": "renamed", "expected_revision": old}
    result = domain.mutate(case.store, "threads.rename", payload, authorized, case.context)
    assert result.data["revision"] == case.service.get_thread_settings({"thread_id": tid})["revision"] == old + 1
    assert result.data["revision"] == thread_summary(case.store.threads.require(tid), authorized)["revision"]
    assert domain.mutate(case.store, "threads.rename", payload, authorized, case.context) == result


@pytest.mark.parametrize("layout", ["data", "legacy"])
# LLM: R3原文以msg_fp==(0,0)区分未出现账本；已存在的空文件也纳入指纹，读取绝不落盘迁移。
# 函数用途: 核对两布局的零字节账本采用明示公式，摘要和设置读取不建文件或改线程。
def test_r3_empty_ledger_uses_explicit_fingerprint_contract_without_read_writes(case, layout):
    from agent_py_agent.agent.plugin_host.conversations import digest
    tid = case.thread.thread_id
    assert case.service.get_thread_settings({"thread_id": tid})["revision"] == 1
    root = case.home.data_dir if layout == "data" else case.home.home_dir
    path = root / "conversations" / "messages" / (tid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stamp = path.stat()
    assert stamp.st_size == 0 and stamp.st_mtime_ns > 0
    row = case.store.threads.require(tid)
    expected = int(digest({**row.to_dict(), "_msg_fp": [0, stamp.st_mtime_ns]})[:15], 16) + 2
    before = {str(p): p.stat().st_mtime_ns for p in case.home.home_dir.rglob("*")}
    assert case.service.get_thread_settings({"thread_id": tid})["revision"] == thread_summary(row, case.authorized)["revision"] == expected
    assert {str(p): p.stat().st_mtime_ns for p in case.home.home_dir.rglob("*")} == before
    assert case.store.threads.require(tid).to_dict() == row.to_dict()
