# LLM: R2使用临时原Owner/授权/控制账；签名桩只代表兄弟接口，不冒充真实模型或跨进程停止。
# 模块用途: 固定execute幂等、错误结果、兄弟接线和附件/锁前路径边界。
import json
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_host import composition, requests
from agent_py_agent.agent.plugin_host.action_specs import action_spec
from agent_py_agent.agent.plugin_host.contracts import (
    DomainResult,
    HostDomainPorts,
    decode_call,
    resolve_read_context,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests.test_plugin_host_requests_and_streams_v2 import (
    authorized_call,
    queued_case,
)


@pytest.fixture
# LLM: 使用真实安装、issuer及规范队列；不替换授权对象，不运行模型。
# 函数用途: 建立覆盖门户控制能力的独立宿主。
def r2_case(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.plugin_host.conversations import ConversationDomain
    from agent_py_agent.agent.plugin_host.gateway_composition import GatewayPortalDomain
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    caps = ["read", "write", "chat", "threads.manage", "tasks.read", "tasks.control"]
    home, install, entry, ref = active_grant(tmp_path, caps)
    api.set_host_api_base("http://127.0.0.1:8420")
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    host = composition.HostComposition(capability_policy=lambda: frozenset(caps))
    store = ConversationStore(home.data_dir / "conversations")
    paths = gateway_paths_from_root(home.data_dir / "gateway")
    ensure_gateway_folders(paths)
    conversation = ConversationDomain(lambda _: store, host.cursors, default_thread=lambda *_: False)
    resources = SimpleNamespace(store_for=lambda _: store, paths_for=lambda _: paths, server=SimpleNamespace(agent=None))
    conversation.portal_v2 = GatewayPortalDomain(resources, conversation)
    host.ports = HostDomainPorts(conversation=conversation)
    try:
        yield host, conversation, store, paths, host.issuer.ingress(token), home, install, entry
    finally:
        host.close()
        api.set_host_api_base(None)


# LLM: 只注册本测试协议桩，不伪造业务数据或修改兄弟工作树。
# 函数用途: 暂时提供惰性导入的固定模块名。
def sibling(monkeypatch, name, **attributes):
    qualified = "agent_py_agent.agent.plugin_host." + name
    module = ModuleType(qualified)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, qualified, module)
    return module


EXECUTE_CASES = [
    ("conversations.threads.compact", {"thread_id": "thread-one"}),
    ("conversations.requests.interject", {"thread_id": "thread-one", "text": "补充"}),
    ("subagents.steer", {"subagent_id": "run-one", "message": "补充"}),
    ("subagents.stop", {"subagent_id": "run-one"}),
    ("processes.background.stop", {"session_id": "process-one"}),
]


@pytest.mark.parametrize("action,payload", EXECUTE_CASES)
@pytest.mark.parametrize("key", [None, "fixture-execute-key-0001"])
# LLM: execute只允许可选合法key，不把write必填或read禁止一并放宽。
# 函数用途: 验证五个execute入口有无key均可解码。
def test_execute_optional_key(action, payload, key):
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
    if key is not None:
        row["idempotency_key"] = key
    assert decode_call(json.dumps(row).encode()).idempotency_key == key


@pytest.mark.parametrize("action,payload,key", [
    ("host.capabilities.get", {}, "fixture-execute-key-0001"),
    ("conversations.threads.create", {}, None),
    ("subagents.stop", {"subagent_id": "run-one"}, "short"),
])
# LLM: 反例保留旧读写约束及key自身格式边界。
# 函数用途: 拒绝越界外层幂等字段。
def test_idempotency_boundaries_unchanged(action, payload, key):
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
    if key is not None:
        row["idempotency_key"] = key
    with pytest.raises(HostAPIError) as failure:
        decode_call(json.dumps(row).encode())
    assert failure.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("failure_mode,expected", [("unexpected", "unknown"), ("post_revoke", "committed")])
# LLM: 真实授权先通过后才运行有状态handler；异常及撤权均不能回报not_started。
# 函数用途: 固定无key execute的副作用结果语义。
def test_execute_outcomes_after_handler_entry(r2_case, failure_mode, expected):
    host, conversation, *_ = r2_case
    entered = []

    # LLM: 受控异常仅测试组合根结果语义，不执行真实停止。
    # 函数用途: 模拟已进入的控制操作。
    def effect(authorized):
        entered.append(authorized.call.action)
        if failure_mode == "unexpected":
            raise RuntimeError("private-exception")
        host.issuer.close()
        return DomainResult(200, {"accepted": True})

    conversation.portal_v2 = SimpleNamespace(ready=lambda _: True, call=effect)
    call = authorized_call(r2_case, "subagents.stop", {"subagent_id": "run-one"})
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, host.ports)
    assert entered == ["subagents.stop"]
    assert failure.value.outcome == expected
    if failure_mode == "unexpected":
        assert failure.value.code == "OUTCOME_UNKNOWN"


# LLM: 两次合法外部调用走真实原控制账，桩只标记执行次数。
# 函数用途: 验证带key的execute一次执行、同key异文冲突。
def test_execute_key_uses_canonical_control_receipt(r2_case, monkeypatch):
    calls = []

    # LLM: 同Owner函数桩，不运行真实进程。
    # 函数用途: 观察控制回执是否阻止二次执行。
    def stop(owner_home, **payload):
        calls.append(owner_home.owner_id)
        return {"accepted": True}

    sibling(monkeypatch, "subagents_v2_handlers", stop_subagent=stop)
    host, _, _, _, _, home, *_ = r2_case
    call = authorized_call(r2_case, "subagents.stop", {"subagent_id": "run-one"}, "fixture-execute-key-0001")
    for _ in range(2):
        assert composition.dispatch_call(call, host.ports).data["accepted"] is True
    assert calls == [home.owner_id]
    changed = authorized_call(r2_case, "subagents.stop", {"subagent_id": "run-two"}, "fixture-execute-key-0001")
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(changed, host.ports)
    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


# LLM: 使用原pending请求、guidance及原关联记录；同key重试不能消耗第二个配额。
# 函数用途: 验证插话正文和原请求关联均真正去重。
def test_interject_same_key_records_once(r2_case):
    host, _, store, paths, *_ = r2_case
    thread, request_id = queued_case(r2_case)
    call = authorized_call(r2_case, "conversations.requests.interject", {
        "thread_id": thread, "request_id": request_id, "mode": "queue", "text": "只接受一次"}, "fixture-interject-key-0001")
    first = composition.dispatch_call(call, host.ports)
    second = composition.dispatch_call(call, host.ports)
    assert first.data["guidance_id"] == second.data["guidance_id"]
    entries = store.guidance.pending(target_type="thread", target_id=thread)
    assert len(entries) == 1
    assert len(json.loads((paths.inbox / (request_id + ".json")).read_bytes())["interjections"]) == 1


# LLM: 不用非法ID真正访问文件；探针只观察锁入口是否被调用。
# 函数用途: 确认路径样式request_id在原锁创建之前拒绝。
def test_interject_invalid_id_never_enters_transition(r2_case, monkeypatch):
    host, *_ = r2_case
    thread, _ = queued_case(r2_case)
    entered = []

    # LLM: 锁探针不落盘，真实请求校验仍执行。
    # 函数用途: 捕获任何不合法的锁前进入行为。
    def transition(paths, request_id):
        entered.append(request_id)
        return nullcontext()

    monkeypatch.setattr(requests.inputs, "gateway_input_transition", transition)
    call = authorized_call(r2_case, "conversations.requests.interject", {
        "thread_id": thread, "request_id": "../../invalid", "text": "不得建锁"})
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, host.ports)
    assert failure.value.code == "INVALID_SCHEMA"
    assert entered == []


@pytest.mark.parametrize("case", ["root_link", "ancestor_link", "dot"])
# LLM: 链接目标为同临时Owner普通样本，不读用户资料；点路径必须在nofollow读取前明确拒绝。
# 函数用途: 防止从链接工作根或空relative_parts读取附件。
def test_context_root_ancestors_and_dot_are_rejected(r2_case, case):
    home = r2_case[5].home_dir
    base = home / "r2-attachments"
    real = base / "real"
    (real / "project").mkdir(parents=True)
    (real / "project" / "fixture.txt").write_text("fixture")
    root, name = real / "project", "fixture.txt"
    if case == "root_link":
        root = base / "link"
        root.symlink_to(real / "project", target_is_directory=True)
    elif case == "ancestor_link":
        (base / "link").symlink_to(real, target_is_directory=True)
        root = base / "link" / "project"
    else:
        name = "."
    context = resolve_read_context(authorized_call(r2_case, "host.capabilities.get", {}))
    thread = SimpleNamespace(workspace_root=str(root), cwd=str(root), metadata={})
    with pytest.raises(HostAPIError) as failure:
        requests._context_inputs(context, thread, [name])
    assert failure.value.code == "WORKSPACE_PATH_OUTSIDE_ROOT"


@pytest.mark.parametrize("action,fields,method", [
    ("conversations.threads.settings.get", {}, "get_thread_settings"),
    ("conversations.threads.packs.get", {}, "get_thread_settings"),
    ("conversations.threads.settings.update", {"reasoning_effort": "high"}, "update_thread_settings"),
    ("conversations.threads.packs.set", {"pack_ids": []}, "update_thread_settings"),
    ("conversations.threads.model.set", {"model_profile_id": "default"}, "update_thread_settings"),
    ("conversations.threads.permission_mode.set", {"permission_mode": "ask"}, "update_thread_settings"),
    ("conversations.threads.usage.get", {}, "get_thread_usage"),
    ("conversations.threads.compact", {}, "compact_thread"),
    ("conversations.threads.delete", {}, "delete_thread"),
])
# LLM: 类构造器与方法精确对应已读W4合同；仅断言接缝，不声称桩完成真实compact。
# 函数用途: 核验九个线程别名都能调用类式后端且传入当前授权。
def test_thread_class_handler_signature(r2_case, monkeypatch, action, fields, method):
    host, conversation, store, _, _, home, *_ = r2_case
    thread, _ = queued_case(r2_case)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home.home_dir, owner_id=home.owner_id))
    conversation.portal_v2.resources.server.agent = agent
    observed = []

    class ThreadHandler:
        # LLM: 精确模拟W4构造参数，不执行业务。
        # 函数用途: 捕获同Owner依赖及授权。
        def __init__(self, actual_store, owner_context, *, agent=None, home_paths=None, authorized=None):
            observed.append((actual_store, owner_context, agent, authorized))

        # LLM: 保持W4实例方法的单个params协议。
        # 函数用途: 返回被测路由的已解析载荷。
        def handle(self, params):
            return {"thread_id": params["thread_id"]}

    setattr(ThreadHandler, method, ThreadHandler.handle)
    sibling(monkeypatch, "thread_settings_v2", ThreadSettingsV2Handlers=ThreadHandler)
    assert conversation.portal_v2.ready(action)
    mode = action_spec(action).mode
    key = "fixture-thread-key-0001" if mode == "write" else None
    call = authorized_call(r2_case, action, {"thread_id": thread, **fields}, key)
    assert composition.dispatch_call(call, host.ports).data["thread_id"] == thread
    assert observed[0][0] is store and observed[0][2] is agent and observed[0][3] is call
    assert observed[0][1].owner_home == home


@pytest.mark.parametrize("foreign", [False, True])
# LLM: 不创建另一Owner的Agent，也不借main运行时扩权。
# 函数用途: 只向兄弟处理器交付准确Owner的现存Agent。
def test_owner_agent_is_not_borrowed(r2_case, foreign):
    _, conversation, _, _, _, home, *_ = r2_case
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home.home_dir / "foreign" if foreign else home.home_dir))
    conversation.portal_v2.resources.server.agent = agent
    context = resolve_read_context(authorized_call(r2_case, "host.capabilities.get", {}))
    assert conversation.portal_v2._owner_agent(context) is (None if foreign else agent)


@pytest.mark.parametrize("action,payload", [
    ("models.providers.list", {}),
    ("models.providers.save", {"provider_id": "fixture-provider", "api_base": "http://127.0.0.1:1", "models": []}),
    ("models.providers.delete", {"provider_id": "fixture-provider"}),
    ("models.profiles.delete", {"profile_id": "fixture-profile"}),
])
# LLM: 真实W4类式签名；只用假模型配置，不联网或保存用户密钥。
# 函数用途: 核验模型入口使用同Owner HomePaths和params对象。
def test_model_class_handler_signature(r2_case, monkeypatch, action, payload):
    host, conversation, _, _, _, home, *_ = r2_case
    seen = []

    class ModelHandler:
        # LLM: 构造只记录由宿主传入的准确路径。
        # 函数用途: 核验没有把OwnerHome当HomePaths。
        def __init__(self, home_paths):
            seen.append(home_paths.owner_home_dir)

        # LLM: 模拟W4单params实例方法。
        # 函数用途: 回传稳定公开结果。
        def handle(self, params):
            assert isinstance(params, dict)
            return {"accepted": True}

        list_providers = save_provider = delete_provider_or_profile = handle

    sibling(monkeypatch, "model_settings_v2_handlers", ModelSettingsV2Handlers=ModelHandler)
    assert conversation.portal_v2.ready(action)
    key = None if action.endswith(".list") else "fixture-model-key-0001"
    result = composition.dispatch_call(authorized_call(r2_case, action, payload, key), host.ports)
    assert result.data["accepted"] is True and seen == [home.home_dir]


# LLM: 使用W4严格关键字签名；store只交给声明接受它的列表口。
# 函数用途: 过滤toggle未支持的expected_revision并传递列表所需store。
def test_builtin_signature_filtering(r2_case, monkeypatch):
    host, _, store, _, _, home, *_ = r2_case
    thread, _ = queued_case(r2_case)
    seen = []

    # LLM: 不运行内置插件，只观察精确文件根和参数。
    # 函数用途: 模拟可接受store的新列表签名。
    def listing(owner_home, *, thread_id=None, store=None):
        seen.append((owner_home, thread_id, store))
        return {"items": []}

    # LLM: 明确不接expected_revision或store，TypeError不能靠业务重试掩盖。
    # 函数用途: 模拟真实toggle签名。
    def toggle(owner_home, *, plugin_id, enabled, thread_id=None):
        seen.append((owner_home, plugin_id, enabled))
        return {"enabled": enabled}

    sibling(monkeypatch, "builtin_plugins_v2", list_builtin_plugins=listing, toggle_builtin_plugin=toggle)
    composition.dispatch_call(authorized_call(r2_case, "plugins.builtin.list", {"thread_id": thread}), host.ports)
    result = composition.dispatch_call(authorized_call(r2_case, "plugins.builtin.toggle", {
        "plugin_id": "tool-fs", "enabled": False, "expected_revision": 1}, "fixture-builtin-key-0001"), host.ports)
    assert result.data["enabled"] is False
    assert seen == [(home.home_dir, thread, store), (home.home_dir, "tool-fs", False)]


@pytest.mark.parametrize("name,action,payload,method", [
    ("subagents_v2_handlers", "subagents.get", {"subagent_id": "run-one"}, "get_subagent"),
    ("subagents_v2_handlers", "subagents.steer", {"subagent_id": "run-one", "message": "补充"}, "steer_subagent"),
    ("subagents_v2_handlers", "subagents.stop", {"subagent_id": "run-one"}, "stop_subagent"),
    ("workspace_v2_handlers", "workspace.files.read", {"workspace_dir": "fixture", "path": "one"}, "read_workspace_file"),
    ("workspace_v2_handlers", "workspace.files.upload", {"workspace_dir": "fixture", "path": "one", "data_base64": ""}, "upload_workspace_file"),
    ("workspace_v2_handlers", "workspace.files.download", {"workspace_dir": "fixture", "path": "one"}, "download_workspace_file"),
    ("workspace_v2_handlers", "workspace.git.diff", {"workspace_dir": "fixture"}, "inspect_workspace_git"),
    ("workspace_v2_handlers", "workspace.git.inspect", {"workspace_dir": "fixture"}, "inspect_workspace_git"),
])
# LLM: 使用W5(context,payload,*,agent,store)及(context,payload)两类精确协议，禁止通过TypeError后二次执行猜签名。
# 函数用途: 验证子代理与工作区入口收到OwnerReadContext和单个params。
def test_w5_context_payload_signatures(r2_case, monkeypatch, name, action, payload, method):
    host, _, store, *_ = r2_case
    seen = []
    payload = dict(payload)
    if "workspace_dir" in payload:
        root = r2_case[5].workspace_dir
        root.mkdir(parents=True, exist_ok=True)
        payload["workspace_dir"] = str(root)

    # LLM: 仅记录同Owner依赖，不执行停止、指导或文件操作。
    # 函数用途: 模拟W5子代理入口。
    def subagent(context, params, *, agent=None, store=None):
        seen.append((context, params, store))
        return {"accepted": True}

    # LLM: 无额外keyword的W5工作区签名。
    # 函数用途: 模拟只接两个位置参数的真实领域入口。
    def workspace(context, payload):
        seen.append((context, payload, store))
        return {"accepted": True}

    sibling(monkeypatch, name, **{method: subagent if name.startswith("subagents") else workspace})
    key = "fixture-workspace-key-0001" if action.endswith(".upload") else None
    call = authorized_call(r2_case, action, payload, key)
    assert composition.dispatch_call(call, host.ports).data["accepted"] is True
    assert seen[0][0].owner_home == r2_case[5] and seen[0][2] is store
    assert isinstance(seen[0][1], dict)


# LLM: W1新导出尚未合入时由受控读口桩代表；投影可来自W5公共_package_dict，不开放原安装对象。
# 函数用途: 验证capability_packages.list不是必须依赖同名handler导出。
def test_capability_package_list_fallback(r2_case, monkeypatch):
    from agent_py_agent.agent.capability import package_provider
    from agent_py_agent.agent.plugin_host import capabilities_v2_handlers

    host, conversation, _, _, _, home, *_ = r2_case
    seen = []

    # LLM: 假包卡无私有路径，不访问真实已安装包。
    # 函数用途: 观察读取准确Owner home。
    def installed(owner_home):
        seen.append(owner_home)
        return [SimpleNamespace(package_id="fixture-package")]

    monkeypatch.setattr(package_provider, "list_installed_capability_packages", installed, raising=False)
    monkeypatch.setattr(capabilities_v2_handlers, "_package_dict", lambda row: {"package_id": row.package_id}, raising=False)
    assert conversation.portal_v2.ready("capability_packages.list")
    result = composition.dispatch_call(authorized_call(r2_case, "capability_packages.list", {}), host.ports)
    assert result.data["items"][0]["package_id"] == "fixture-package" and seen == [home.home_dir]


@pytest.mark.parametrize("key", [None, "fixture-revoke-key-0001"])
# LLM: 进入真实Gateway适配器内部的末核，而非替换整个portal；不运行真实停止。
# 函数用途: 防止适配器内部撤权把已执行的execute重新标成not_started。
def test_portal_internal_post_revalidation_is_committed(r2_case, monkeypatch, key):
    host, *_ = r2_case
    entered = []

    # LLM: 回调先产生可观察副作用，再使真实issuer失效。
    # 函数用途: 注入准确的handler后撤权窗口。
    def stop(owner_home, **payload):
        entered.append(True)
        host.issuer.close()
        return {"accepted": True}

    sibling(monkeypatch, "subagents_v2_handlers", stop_subagent=stop)
    call = authorized_call(r2_case, "subagents.stop", {"subagent_id": "run-one"}, key)
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, host.ports)
    assert entered == [True] and failure.value.outcome == "committed"


# LLM: handler内部TypeError表示执行结果未知，不能拿它当签名探测重跑。
# 函数用途: 确认协议适配器不会双重调用已经进入的副作用。
def test_handler_internal_typeerror_never_retries(r2_case, monkeypatch):
    entered = []

    # LLM: 受控执行后异常，不运行真实子代理。
    # 函数用途: 验证最多一次调用。
    def stop(context, payload, *, agent=None, store=None):
        entered.append(context.owner_home.owner_id)
        raise TypeError("private handler error")

    sibling(monkeypatch, "subagents_v2_handlers", stop_subagent=stop)
    call = authorized_call(r2_case, "subagents.stop", {"subagent_id": "run-one"})
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, r2_case[0].ports)
    assert failure.value.code == "OUTCOME_UNKNOWN" and len(entered) == 1


# LLM: 重放不占第二份请求配额；同key异文必须在guidance原账写前拒绝。
# 函数用途: 验证64项边界重试及插话输入冲突。
def test_interject_retry_at_limit_and_changed_input(r2_case):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    host, _, store, paths, *_ = r2_case
    thread, request_id = queued_case(r2_case)
    payload = {"thread_id": thread, "request_id": request_id, "mode": "queue", "text": "一次"}
    key = "fixture-quota-key-0001"
    first = composition.dispatch_call(authorized_call(r2_case, "conversations.requests.interject", payload, key), host.ports)
    path = paths.inbox / (request_id + ".json")
    update_json_file_atomic(path, lambda row: {**row, "interjections": [*row["interjections"],
        *[{"guidance_id": "fixture-" + str(i), "mode": "queue"} for i in range(63)]]})
    again = composition.dispatch_call(authorized_call(r2_case, "conversations.requests.interject", payload, key), host.ports)
    assert again.data["guidance_id"] == first.data["guidance_id"]
    assert len(json.loads(path.read_bytes())["interjections"]) == 64
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(authorized_call(r2_case, "conversations.requests.interject", {**payload, "text": "另一条"}, key), host.ports)
    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    assert len(store.guidance.pending(target_type="thread", target_id=thread)) == 1


@pytest.mark.parametrize("protocol", ["legacy", "three_positional"])
# LLM: 独立复核指出线程模块级函数不能跳过签名绑定；三个参数与旧Owner协议均只执行一次。
# 函数用途: 验证W4模块函数的两种兼容入口。
def test_thread_module_function_protocols(r2_case, monkeypatch, protocol):
    host, _, store, _, _, home, *_ = r2_case
    thread, _ = queued_case(r2_case)
    seen = []

    # LLM: 旧桩只接Owner，不代表真实线程业务。
    # 函数用途: 核验旧模块函数的Owner参数。
    def legacy(owner_home, **payload):
        seen.append(owner_home.owner_id)
        return {"thread_id": payload["thread_id"]}

    # LLM: W6原有三参数函数协议继续保留。
    # 函数用途: 核验没有为支持旧桩破坏原位置参数。
    def three(actual_store, context, payload):
        assert actual_store is store
        seen.append(context.owner_home.owner_id)
        return {"thread_id": payload["thread_id"]}

    sibling(monkeypatch, "thread_settings_v2", get_thread_settings=legacy if protocol == "legacy" else three)
    result = composition.dispatch_call(authorized_call(r2_case, "conversations.threads.settings.get", {"thread_id": thread}), host.ports)
    assert result.data["thread_id"] == thread and seen == [home.owner_id]


# LLM: 新签名若要求store，缺省thread_id不能使已知安全依赖丢失；旧版本仍由签名过滤。
# 函数用途: 覆盖无thread_id的builtin list必需store协议。
def test_builtin_list_store_without_explicit_thread(r2_case, monkeypatch):
    seen = []

    # LLM: 只提供协议，不读取真实插件配置。
    # 函数用途: 检查准确的原ConversationStore。
    def listing(owner_home, *, store, thread_id=""):
        seen.append(store)
        return {"items": []}

    sibling(monkeypatch, "builtin_plugins_v2", list_builtin_plugins=listing)
    composition.dispatch_call(authorized_call(r2_case, "plugins.builtin.list", {}), r2_case[0].ports)
    assert seen == [r2_case[2]]


# LLM: 模拟guidance已提交但原关联未保存、随后其它关联占满的恢复窗口；不能把列表补成65项。
# 函数用途: 未完成插话的重试在满额时显式unknown而非伪成功。
def test_interject_partial_association_retry_preserves_limit(r2_case):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    host, _, store, paths, *_ = r2_case
    thread, request_id = queued_case(r2_case)
    payload = {"thread_id": thread, "request_id": request_id, "mode": "queue", "text": "恢复"}
    call = authorized_call(r2_case, "conversations.requests.interject", payload, "fixture-partial-key-0001")
    composition.dispatch_call(call, host.ports)
    path = paths.inbox / (request_id + ".json")
    update_json_file_atomic(path, lambda row: {**row, "interjections": [
        {"guidance_id": "other-" + str(i), "mode": "queue"} for i in range(64)]})
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, host.ports)
    assert failure.value.outcome == "unknown"
    assert len(json.loads(path.read_bytes())["interjections"]) == 64
    assert len(store.guidance.pending(target_type="thread", target_id=thread)) == 1


# LLM: 覆盖gateway_parts/request_execution.py写入terminal_response的model_*计量字段、64行忽略日志预算以及W1字典记录返回。
# 函数用途: 核验R2复核补齐的三处收尾边界。
def test_usage_model_aliases_and_stream_budget_and_dict_packages(r2_case, monkeypatch):
    from agent_py_agent.agent.capability import package_provider
    from agent_py_agent.agent.plugin_host.requests import request_usage
    from agent_py_agent.agent.plugin_host.streams import request_progress

    usage = request_usage({
        "terminal_response": {
            "model_accounted_input_tokens": 120,
            "model_output_tokens": 45,
            "model_cached_input_tokens": 30,
            "model_total_tokens": 165,
            "last_turn_elapsed_seconds": 1.5,
            "last_turn_tokens_per_sec": 30.0,
        }
    })
    assert usage["input_tokens"] == 120 and usage["output_tokens"] == 45
    assert usage["cached_tokens"] == 30 and usage["total_tokens"] == 165

    host, _, _, paths, _, _, *_ = r2_case
    chunks = paths.done / "req-budget.chunks.jsonl"
    chunks.write_bytes(
        b'{"kind":"heartbeat"}\n' * 65
        + b'{"kind":"tool_progress","progress":{"call_id":"call-1","tool_name":"bash","phase":"started","summary":"run"}}\n'
    )
    facts = {"request_id": "req-budget", "terminal": {"terminal_id": "req-budget", "status": "completed"}}
    event, pos1, more1 = request_progress(paths, facts, {})
    assert event is None and more1 is True and pos1["offset"] > 0
    event2, _, more2 = request_progress(paths, facts, pos1)
    assert event2 is not None and event2[0] == "tool_progress" and more2 is False

    monkeypatch.setattr(package_provider, "list_installed_capability_packages", lambda owner_home: [{"package_id": "pkg-dict"}], raising=False)
    result = composition.dispatch_call(authorized_call(r2_case, "capability_packages.list", {}), host.ports)
    assert result.data["items"][0]["package_id"] == "pkg-dict"

