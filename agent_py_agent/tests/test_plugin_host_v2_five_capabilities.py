# LLM: WP-4仅验证公开动作合同、原授权和领域接线；替身依赖与真实存储验收必须分别记账。
# 模块用途: 守住五项核心能力、后台观察、目录边界及插件跨用户隔离。
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host import composition, contracts, gateway_observations
from agent_py_agent.agent.plugin_host.action_specs import action_specs, required_capabilities
from agent_py_agent.agent.plugin_host.contracts import decode_call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import canonical_json, thaw
from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_plugin_host_v2_conversations import Harness

# 需求矩阵刻意独立于生产目录，避免“实现漏一项、测试也跟着漏”的同源断言。
ACTION_CASES = (
    ("conversations.threads.workspace.get", "read", {"read"}, {}),
    ("conversations.threads.workspace.switch", "write", {"threads.manage"},
     {"thread_id": "thread-source", "workspace_dir": "/tmp/project"}),
    ("assets.workspace.list", "read", {"read"}, {}),
    ("assets.workspace.promote", "write", {"assets.write"},
     {"workspace_dir": "/tmp/project", "asset_kind": "script", "asset_id": "daily"}),
    ("conversations.threads.fork", "write", {"threads.manage"}, {"source_thread_id": "thread-source"}),
    ("datasources.list", "read", {"datasources.read"}, {}),
    ("datasources.save", "write", {"datasources.write"},
     {"spec": {"datasource_id": "orders", "kind": "sqlite", "title": "订单"}}),
    ("datasources.test", "read", {"datasources.read"}, {"datasource_id": "orders"}),
    ("datasources.schema.inspect", "read", {"datasources.read"}, {"datasource_id": "orders"}),
    ("datasources.migrate", "write", {"datasources.write"},
     {"source_id": "orders", "target_spec": {"datasource_id": "orders-next", "kind": "sqlite"}}),
    ("conversations.turns.search", "read", {"chat.history"}, {"query": "中文查询", "cursor": 0}),
    ("assets.extract.from_turns.propose", "write", {"chat.history", "assets.write"},
     {"turn_refs": [{"thread_id": "thread-source", "message_id": "message-source"}]}),
    ("assets.extract.from_turns.get", "read", {"read", "chat.history"}, {"proposal_id": "proposal-one"}),
    ("assets.extract.from_turns.commit", "write", {"assets.write"}, {"proposal_id": "proposal-one"}),
    ("recorder.sessions.start", "write", {"recorder.control"}, {"start_url": "https://example.com/"}),
    ("recorder.sessions.stop", "write", {"recorder.control"}, {"recording_id": "rec_one"}),
    ("recorder.sessions.get", "read", {"recorder.control"}, {}),
    ("recorder.sessions.compile", "write", {"recorder.control", "assets.write"}, {"recording_id": "rec_one"}),
    ("processes.background.list", "read", {"tasks.read"}, {}),
    ("processes.background.inspect", "read", {"tasks.read"}, {"session_id": "session-one"}),
)


# LLM: 始终走真实decode_call；幂等键仅为临时测试意图，不携带生产凭据。
# 函数用途: 构造符合v2信封的请求字节。
def call_bytes(action, payload, *, key=None):
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
    if key is not None:
        row["idempotency_key"] = key
    return canonical_json(row).encode()


# LLM: 需求矩阵独立于实现；此处只证明合同，不证明领域业务成功。
# 函数用途: 逐动作核对闭合参数和准确能力。
@pytest.mark.parametrize("action,mode,caps,payload", ACTION_CASES, ids=[row[0] for row in ACTION_CASES])
def test_twenty_actions_have_closed_schemas_and_exact_capabilities(action, mode, caps, payload):
    assert action in action_specs(), f"缺少标准动作: {action}"
    spec = action_specs()[action]
    assert spec.mode == mode
    assert required_capabilities(action, payload) == caps
    assert spec.payload_schema["additionalProperties"] is False
    parsed = decode_call(call_bytes(action, payload, key="wp4-contract-key-001" if mode == "write" else None))
    assert parsed.action == action
    with pytest.raises(HostAPIError) as error:
        decode_call(call_bytes(action, {**payload, "unknown_field": 1},
                               key="wp4-contract-key-001" if mode == "write" else None))
    assert error.value.code == "INVALID_SCHEMA"


# LLM: 必须由真实解码器拒绝，不能靠前端生成键代替服务端约束。
# 函数用途: 逐动作校验读写幂等键合同。
@pytest.mark.parametrize("action,mode,caps,payload", ACTION_CASES, ids=[row[0] for row in ACTION_CASES])
def test_writes_require_idempotency_and_reads_forbid_it(action, mode, caps, payload):
    assert action in action_specs(), f"缺少标准动作: {action}"
    with pytest.raises(HostAPIError) as error:
        decode_call(call_bytes(action, payload, key=None if mode == "write" else "wp4-contract-key-001"))
    assert error.value.code == "INVALID_SCHEMA"


# LLM: 仅检查显式能力集合，不把能力登记当执行授权。
# 函数用途: 检查v9认得四个新增能力。
def test_v9_understands_the_four_new_explicit_capabilities():
    assert {"assets.write", "datasources.read", "datasources.write", "recorder.control"} <= host_capabilities()


# LLM: 参数通过不授目录访问权，权限在后续真实目录测试校验。
# 函数用途: 守住创建和列表的可选目录参数。
@pytest.mark.parametrize("action", ["conversations.threads.create", "conversations.threads.list"])
def test_existing_thread_actions_accept_optional_workspace_directory(action):
    payload = {"workspace_dir": "/tmp/project"}
    key = "wp4-create-key-001" if action.endswith("create") else None
    try:
        decoded = decode_call(call_bytes(action, payload, key=key))
    except HostAPIError:
        pytest.fail("已有线程动作未接受新增workspace_dir参数")
    assert decoded.payload["workspace_dir"] == "/tmp/project"


# LLM: 使用真实ConversationStore和原控制幂等账；只替换A0对象认证边界，不冒充真实HTTP/登录验收。
# 类用途: 在隔离临时目录里验证跨owner、原服务接线和副作用重放。
class V2Harness:
    # LLM: fake来源只认可已登记调用；生产组合仍使用原IngressIssuer，任何测试文件都不会装进产品。
    # 函数用途: 建立三个owner和真实会话域、控制回执端口。
    def __init__(self, tmp_path, monkeypatch):
        self.base = Harness(tmp_path / "host", monkeypatch)
        home = resolve_owner_home(tmp_path / "host", OwnerIdentity.local_main())
        self.base.homes["main"] = home
        self.base.stores["main"] = ConversationStore(home.data_dir / "conversations")
        self.domain = self.base.domain
        monkeypatch.setattr(composition, "revalidate_call", self.base.resolve)
        monkeypatch.setattr(gateway_observations, "revalidate_call", self.base.resolve)
        monkeypatch.setattr(gateway_observations, "resolve_read_context", self.base.resolve)
        resources = SimpleNamespace(paths_for=lambda context: gateway_paths_from_root(context.owner_home.home_dir / "test-gateway"))
        self.services = gateway_observations.GatewayObservationServices(resources, self.domain, self.domain.cursor_codec)
        self.ports = contracts.HostDomainPorts(conversation=self.domain, observations=SimpleNamespace(services=self.services))

    # LLM: 真实解码器仍执行闭合schema；thread_scope模拟新调用的最新授权，不改旧调用或生产认证。
    # 函数用途: 构造一个带当前线程范围、在临时宿主登记过的调用。
    def request(self, action, payload, *, owner="alice", key="wp4-effect-key-001", caps=None,
                installation="install-one", thread_scope=None):
        spec = action_specs()[action]
        parsed = decode_call(call_bytes(action, payload, key=key if spec.requires_idempotency else None))
        original = self.base.request("threads.list", {}, owner=owner, installation=installation,
                                     caps=host_capabilities() if caps is None else caps)
        if thread_scope is not None:
            original = replace(original, binding=replace(original.binding,
                grant={**thaw(original.binding.grant), "threads": thread_scope}), resource_scope={"threads": thread_scope})
        authorized = replace(original, call=parsed)
        self.base.authorized[id(authorized)] = authorized
        return authorized

    # LLM: 新动作走真实组合根，原create/list仍进真实ConversationDomain，结果只读为普通字典。
    # 函数用途: 通过产品分发入口调用一个动作。
    def call(self, action, payload=None, **kwargs):
        authorized = self.request(action, payload or {}, **kwargs)
        return thaw(composition.dispatch_call(authorized, self.ports).data)

    # LLM: 创建沿现有canonical线程文件和创建幂等门，不直接给伪造线程设置安装来源。
    # 函数用途: 为目录、检索和后台测试建立可见线程。
    def create(self, *, owner="alice", workspace=None, key="wp4-create-key-001", installation="install-one"):
        payload = {"title": "项目会话"}
        if workspace is not None:
            payload["workspace_dir"] = str(workspace)
        authorized = self.request("conversations.threads.create", payload, owner=owner, key=key, installation=installation)
        return thaw(self.domain.call(authorized).data)


# LLM: 只使用测试目录和作用域替身，不接生产home。
# 函数用途: 提供领域接线用隔离宿主。
@pytest.fixture
def domain_case(tmp_path, monkeypatch):
    return V2Harness(tmp_path, monkeypatch)


# LLM: 原端口保持，新增固定路由不能被动态动作名改变。
# 函数用途: 核对全部新增动作分派领域。
@pytest.mark.parametrize("action,mode,caps,payload", ACTION_CASES, ids=[row[0] for row in ACTION_CASES])
def test_new_actions_use_the_fixed_v2_domain(action, mode, caps, payload):
    assert composition.domain_name(action) == "capabilities_v2"


# LLM: 有效管理员身份来自测试登记；不设置full-access放宽产品校验。
# 函数用途: 验证管理员正常模式绑定外部目录。
def test_admin_create_binds_external_workspace_without_full_access(domain_case, tmp_path):
    directory = tmp_path / "external-project"
    directory.mkdir()
    thread = domain_case.create(owner="main", workspace=directory)
    stored = domain_case.base.stores["main"].threads.load(thread["thread_id"])
    assert stored.cwd == str(directory.resolve())
    assert str(directory.resolve()) in stored.runtime_workspace_roots


# LLM: 拒绝前不创建线程，两个普通owner分别验证。
# 函数用途: 检查非管理员外部目录403。
@pytest.mark.parametrize("owner", ["alice", "bob"])
def test_non_admin_create_rejects_external_workspace(domain_case, tmp_path, owner):
    directory = tmp_path / "external-project"
    directory.mkdir()
    with pytest.raises(HostAPIError) as error:
        domain_case.create(owner=owner, workspace=directory)
    assert (error.value.status, error.value.code) == (403, "WORKSPACE_DIR_FORBIDDEN_FOR_NON_ADMIN")
    assert not domain_case.base.stores[owner].threads.list()


# LLM: 使用原存储真实线程，不从模拟返回值构造过滤成功。
# 函数用途: 核对目录过滤和谱系字段。
def test_thread_list_filters_directory_and_returns_lineage(domain_case):
    home = domain_case.base.homes["main"].home_dir
    first, second = home / "first", home / "second"
    first.mkdir()
    second.mkdir()
    one = domain_case.create(owner="main", workspace=first)
    domain_case.create(owner="main", workspace=second, key="wp4-create-key-002")
    rows = domain_case.call("conversations.threads.list", {"workspace_dir": str(first)}, owner="main")["items"]
    assert [row["thread_id"] for row in rows] == [one["thread_id"]]
    assert rows[0]["workspace_root"] == str(first)
    assert rows[0]["cwd"] == str(first)
    assert {"forked_from_thread_id", "forked_from_message_id", "fork_lineage_depth"} <= rows[0].keys()
    assert rows == [one]


# LLM: 前后目录集合实测相同，不用只读命名代替无副作用证明。
# 函数用途: 检查默认目录查询不创建文件。
def test_workspace_default_query_is_cold_and_owner_scoped(domain_case):
    home = domain_case.base.homes["alice"].home_dir
    before = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    data = domain_case.call("conversations.threads.workspace.get")
    assert data["owner_home"] == str(home)
    assert data["is_admin"] is False
    assert {"skills", "scripts", "workflows", "monitors", "has_agents_md"} == data["overlay_counts"].keys()
    assert sorted(str(path.relative_to(home)) for path in home.rglob("*")) == before


# LLM: 原线程修订、回执和可见性共同验证，不允许同键重复写。
# 函数用途: 核对目录切换CAS、重放及跨用户拒绝。
def test_workspace_switch_cas_replay_and_cross_owner_denial(domain_case):
    home = domain_case.base.homes["main"].home_dir
    directory = home / "project"
    directory.mkdir()
    thread = domain_case.create(owner="main")
    payload = {"thread_id": thread["thread_id"], "workspace_dir": str(directory), "expected_revision": thread["revision"]}
    switched = domain_case.call("conversations.threads.workspace.switch", payload, owner="main")
    assert switched["cwd"] == str(directory)
    assert domain_case.call("conversations.threads.workspace.switch", payload, owner="main") == switched
    assert domain_case.call("conversations.threads.list", owner="main")["items"] == [switched]
    with pytest.raises(HostAPIError) as stale:
        domain_case.call("conversations.threads.workspace.switch", payload, key="wp4-effect-key-002", owner="main")
    assert stale.value.code == "REVISION_CONFLICT"
    with pytest.raises(HostAPIError) as foreign:
        domain_case.call("conversations.threads.workspace.get", {"thread_id": thread["thread_id"]}, owner="bob")
    assert foreign.value.status == 404


# LLM: 真实链接指向外部测试目录，身份检查必须按解析后的路径生效。
# 函数用途: 拒绝通过家目录内软链接越界。
def test_external_workspace_symlink_is_not_a_non_admin_escape(domain_case, tmp_path):
    home = domain_case.base.homes["alice"].home_dir
    external = tmp_path / "external"
    external.mkdir()
    (home / "shortcut").symlink_to(external, target_is_directory=True)
    with pytest.raises(HostAPIError) as error:
        domain_case.create(workspace=home / "shortcut")
    assert error.value.code == "WORKSPACE_DIR_FORBIDDEN_FOR_NON_ADMIN"


# LLM: 以下替身只验证跨工作包的公开签名接线，不证明SQL迁移、模型提炼、真实浏览器或分叉安全已经验收。
# 函数用途: 固定领域边界回执，同时记录传入的原owner和目录。
@pytest.fixture
def backend_case(domain_case, monkeypatch):
    case = domain_case
    thread = case.create()
    case.thread_id = thread["thread_id"]
    case.backend_calls = []
    home = case.base.homes["alice"].home_dir
    for collection, identifier, key in (("asset_extractions", "proposal-one", "proposal_id"),
                                         ("browser_recordings", "rec_one", "recording_id")):
        folder = home / "data" / collection
        folder.mkdir(parents=True)
        (folder / (identifier + ".json")).write_text(json.dumps({key: identifier, "revision": 1}))

    # LLM: 来源仅为本测试中的静态模块名；不改磁盘或生产模块，不执行参数中的任意代码。
    # 函数用途: 为缺少的并行领域安装纯内存接口替身。
    def module(name, values):
        value = ModuleType("agent_py_agent.agent." + name)
        value.__dict__.update(values)
        monkeypatch.setitem(sys.modules, value.__name__, value)

    # LLM: 含owner的标记只存在测试结果，用于断言适配器未落到管理员home。
    # 函数用途: 返回具有真实来源引用形状的领域结果。
    def result(home, **extra):
        case.backend_calls.append((str(home), extra))
        return {"marker": "unit-backend", "proposal_id": "proposal-one", "recording_id": "rec_one",
                "source_turn_refs": [{"thread_id": case.thread_id, "message_id": "message-source"}],
                "scope": "owner", "workspace_root": "", **extra}

    # LLM: 仅提供WP-2约定的五个公开方法；实际数据库、保险箱和迁移另走真实集成测试。
    # 类用途: 校验Host将已核owner和参数交给数据源领域。
    class Datasources:
        # LLM: 不初始化文件，只保存宿主传入的家目录。
        # 函数用途: 模拟数据源Store构造。
        def __init__(self, home):
            self.home = home

        # LLM: 明确只读，不提供任意反射方法。
        # 函数用途: 返回测试数据源摘要。
        def list_datasources(self, **options):
            return [result(self.home)]

        # LLM: 验证secret和scope走明确kwargs，不作为owner构造参数。
        # 函数用途: 返回保存接线回执。
        def save_datasource(self, spec, **options):
            return result(self.home, received_spec=spec)

        # LLM: 不发网络请求。
        # 函数用途: 返回连通性测试接线回执。
        def test_datasource(self, datasource_id, **options):
            return result(self.home, datasource_id=datasource_id)

        # LLM: 不连接真实库。
        # 函数用途: 返回表结构接线回执。
        def inspect_schema(self, datasource_id, **options):
            return result(self.home, datasource_id=datasource_id)

        # LLM: 不执行迁移或切换。
        # 函数用途: 返回迁移接线回执。
        def migrate_datasource(self, source_id, target_spec, **options):
            return result(self.home, target_spec=target_spec)

    # LLM: 使用真实临时线程保存假后端的fork结果，只验证Host归属重新绑定，不验证消息克隆算法。
    # 函数用途: 提供WP-1的公开分叉函数形状。
    def fork(store, *, source_thread_id, **options):
        from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
        source = store.threads.load(source_thread_id)
        cloned = replace(source, thread_id="thread-fork-unit", title="分叉unit-backend", metadata=dict(source.metadata))
        create_thread_atomic(store.threads, cloned)
        return result(Path(source.owner_home), thread_id=cloned.thread_id, title=cloned.title,
                      forked_from_thread_id=source_thread_id, forked_from_message_id="message-source",
                      fork_lineage_depth=1, message_count=1, boundary_adjusted=False)

    module("assets.datasource_store", {"DatasourceStore": Datasources})
    module("conversation.thread_fork_service", {"fork_conversation_thread": fork})
    module("assets.workspace_assets", {"promote_to_owner": lambda *args, **kw: result(kw["owner_home"])})
    module("assets.turn_extractor", {
        "search_user_turns": lambda store, **kw: {**result(case.base.homes["alice"].home_dir), "items": [], "total": 0, "next_cursor": None},
        "propose_assets_from_turns": lambda store, **kw: result(kw["owner_home"]),
        "get_extraction_proposal": lambda home, proposal_id: result(home),
        "commit_extracted_assets": lambda home, **kw: result(home),
    })
    module("assets.browser_recorder", {
        "start_recording_session": lambda home, **kw: result(home),
        "stop_recording_session": lambda home, recording_id, **kw: result(home),
        "get_recording_session": lambda home, recording_id: result(home),
        "list_recording_sessions": lambda home: [result(home)],
        "compile_recording_to_script": lambda home, **kw: result(home),
    })
    return case


BACKEND_CASES = [row for row in ACTION_CASES if row[0] not in {
    "conversations.threads.workspace.get", "conversations.threads.workspace.switch", "assets.workspace.list",
    "processes.background.list", "processes.background.inspect"}]


# LLM: 此处只认领域适配证据，不能外推为真实数据库或浏览器验证。
# 函数用途: 逐动作检查公开服务签名与owner参数。
@pytest.mark.parametrize("action,mode,caps,payload", BACKEND_CASES, ids=[row[0] for row in BACKEND_CASES])
def test_domain_adapters_call_the_owner_scoped_public_services(backend_case, action, mode, caps, payload):
    payload = json.loads(canonical_json(payload))
    owner = "main" if "workspace_dir" in payload else "alice"
    if "workspace_dir" in payload:
        payload["workspace_dir"] = str(backend_case.base.homes[owner].home_dir)
    if "source_thread_id" in payload:
        payload["source_thread_id"] = backend_case.thread_id
    if "turn_refs" in payload:
        payload["turn_refs"][0]["thread_id"] = backend_case.thread_id
    try:
        data = backend_case.call(action, payload, owner=owner)
    except HostAPIError as error:
        pytest.fail(f"新动作没有接通公开领域服务: {error.code}")
    assert "unit-backend" in canonical_json(data)
    assert backend_case.backend_calls
    assert all(home == str(backend_case.base.homes[owner].home_dir) for home, _ in backend_case.backend_calls)


# LLM: 原日志与会话JSON按字节复核，不消费通知或控制进程。
# 函数用途: 检查后台只读尾部和跨用户隔离。
def test_background_inspect_is_read_only_and_respects_owner_and_thread(domain_case):
    thread = domain_case.create()
    home = domain_case.base.homes["alice"].home_dir
    root = home / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True)
    sid = "bg-1234567890-aabbccddeeff0011"
    log = root / (sid + ".log")
    log.write_text("first\nsecond\nlast\n")
    record = root / (sid + ".json")
    record.write_text(json.dumps({"session_id": sid, "status": "exited", "exit_code": 0, "pid": 0,
        "command": "synthetic command", "cwd": str(home), "output_path": str(log),
        "metadata": {"thread_id": thread["thread_id"], "owner_home": str(home)}}))
    before = {path: path.read_bytes() for path in root.iterdir()}
    try:
        listing = domain_case.call("processes.background.list")
        inspected = domain_case.call("processes.background.inspect", {"session_id": sid, "tail_lines": 2})
    except HostAPIError as error:
        pytest.fail(f"后台只读接口未接通: {error.code}")
    assert [row["session_id"] for row in listing["items"]] == [sid]
    assert inspected["log_tail"] == ["second", "last"]
    assert {path: path.read_bytes() for path in root.iterdir()} == before
    with pytest.raises(HostAPIError) as foreign:
        domain_case.call("processes.background.inspect", {"session_id": sid}, owner="bob")
    assert foreign.value.status == 404


# LLM: 值为合成测试内容，原幂等账不得保存其正文，后端只调用一次。
# 函数用途: 检查写入重放的秘密隔离。
def test_write_idempotency_receipt_never_stores_raw_secret(backend_case):
    secret = "wp4-fixture-secret-not-a-real-key"
    payload = {"spec": {"datasource_id": "orders", "kind": "sqlite", "title": "订单"},
               "secret": {"password": secret}}
    try:
        first = backend_case.call("datasources.save", payload)
        assert backend_case.call("datasources.save", payload) == first
    except HostAPIError as error:
        pytest.fail(f"数据源写入和重放未接通: {error.code}")
    home = backend_case.base.homes["alice"].home_dir
    assert all(secret.encode() not in path.read_bytes() for path in home.rglob("*.json"))
    assert len(backend_case.backend_calls) == 1


# LLM: 此夹具走真实安装、v9令牌、IngressIssuer及Gateway组合，不替换认证函数，不启动Gateway或执行插件进程。
# 函数用途: 验证新增动作确实受原授权交集约束。
@pytest.fixture
def real_host(tmp_path):
    from agent_py_agent.agent import plugin_host_api
    from agent_py_agent.agent.plugin_host.gateway_composition import build_gateway_host_composition
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant
    home, _, entry, ref = active_grant(tmp_path, sorted(host_capabilities()), scope="private")
    plugin_host_api.set_host_api_base("http://127.0.0.1:18429")
    token = plugin_host_api.issue_host_api_env(ref, entry.manifest.plugin_id)[plugin_host_api.HOST_API_TOKEN_ENV]
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(my_agent_home=str(tmp_path))))
    composed = build_gateway_host_composition(server, None)
    source = composed.issuer.ingress(token)
    policy = set(host_capabilities())
    composed.issuer._policy = lambda: frozenset(policy)
    yield composed, source, policy, home
    composed.close()
    plugin_host_api.set_host_api_base(None)


# LLM: 真安装和发行器，不替换认证函数；逐个移除AND能力再试。
# 函数用途: 证明全部新增动作受原能力交集约束。
@pytest.mark.parametrize("action,mode,caps,payload", ACTION_CASES, ids=[row[0] for row in ACTION_CASES])
def test_real_ingress_checks_every_required_capability(real_host, action, mode, caps, payload):
    composed, source, policy, home = real_host
    payload = json.loads(canonical_json(payload))
    if "workspace_dir" in payload:
        payload["workspace_dir"] = str(home.home_dir)
    call = decode_call(call_bytes(action, payload, key="wp4-real-key-001" if mode == "write" else None))
    assert composed.issuer.authorize(source, call).effective_owner == home.identity
    for capability in caps:
        policy.remove(capability)
        with pytest.raises(HostAPIError) as error:
            composed.issuer.authorize(source, call)
        assert (error.value.status, error.value.code) == (403, "CAPABILITY_DENIED")
        policy.add(capability)


# LLM: 新动作独立集合不加入旧Broker宽松路由。
# 函数用途: 检查Broker登记20个v2动作。
def test_broker_registers_all_twenty_actions_without_legacy_grant_downgrade():
    from agent_py_agent.agent.gateway_parts import plugin_broker_api
    assert hasattr(plugin_broker_api, "HOST_V2_ACTIONS"), "Broker尚未登记v2动作"
    assert {row[0] for row in ACTION_CASES} <= plugin_broker_api.HOST_V2_ACTIONS


# LLM: 真实来源走原authorize/dispatch；payload身份字段不能变成宿主参数。
# 函数用途: 验证Broker桥接及公开只读主题。
def test_broker_uses_real_v2_ingress_and_topics(real_host):
    from agent_py_agent.agent.gateway_parts.plugin_broker_api import PluginBrokerService
    from agent_py_agent.agent.plugin_host_broker_topics import query_broker_topics
    composed, source, _, home = real_host
    assert hasattr(PluginBrokerService, "from_host_v2"), "Broker尚未接入真实v2来源"
    broker = PluginBrokerService.from_host_v2(source, composed.ports)
    result = broker.dispatch("conversations.threads.workspace.get")
    assert result["ok"] is True and result["data"]["owner_home"] == str(home.home_dir)
    topics = query_broker_topics(home.home_dir, home.owner_id, {"topics": ["workspace_context", "workspace_assets"]},
                                 host_v2_service=broker)
    assert topics["workspace_context"]["ok"] is True
    assert topics["workspace_assets"]["ok"] is True
    denied = broker.dispatch("conversations.threads.workspace.get", {"owner_home": "/untrusted"})
    assert denied["ok"] is False and denied["error_code"] == "INVALID_SCHEMA"


# LLM: 原Store的新建CAS为0，不能被外围schema错误拒绝。
# 函数用途: 保持数据源新建修订合同。
def test_datasource_create_cas_accepts_zero_revision():
    payload = {"spec": {"datasource_id": "orders", "kind": "sqlite", "title": "订单"}, "expected_revision": 0}
    try:
        call = decode_call(call_bytes("datasources.save", payload, key="wp4-create-cas-001"))
    except HostAPIError:
        pytest.fail("新建数据源CAS必须可以声明expected_revision=0")
    assert call.payload["expected_revision"] == 0


# LLM: 原Overlay被观察而非替换，其冷读仍真实执行；只在临时项目构造原ScriptStore资产。
# 函数用途: 证明接的是C4覆盖服务且列表不公开代码。
def test_workspace_overlay_uses_original_service(domain_case, monkeypatch):
    from agent_py_agent.agent.assets import workspace_assets
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    home = domain_case.base.homes["main"].home_dir
    directory = home / "project"
    directory.mkdir()
    ScriptStore(directory / ".agent").save_script(AssetDescriptor(id="daily", kind="script", name="每日"), "print('private-code')")
    original, called = workspace_assets.WorkspaceAssetOverlay, []

    # LLM: 子类沿原实现执行，不伪造列表。
    # 类用途: 记录公开Overlay的实际调用。
    class ObservedOverlay(original):
        # LLM: 记录后立即委托原服务，仍然只读。
        # 函数用途: 确认列表来源。
        def list_assets(self, *args, **kwargs):
            called.append(True)
            return super().list_assets(*args, **kwargs)

    monkeypatch.setattr(workspace_assets, "WorkspaceAssetOverlay", ObservedOverlay)
    result = domain_case.call("assets.workspace.list", {"workspace_dir": str(directory), "kind": "script"}, owner="main")
    assert called, "必须优先使用原WorkspaceAssetOverlay"
    row = next(row for row in result["items"] if row["id"] == "daily")
    assert row["source_scope"] == "workspace" and "private-code" not in json.dumps(result)
    promoted = domain_case.call("assets.workspace.promote", {"workspace_dir": str(directory), "asset_kind": "script", "asset_id": "daily"}, owner="main")
    assert promoted["items"][0]["id"] == "daily"
    assert ScriptStore(home).get_script("daily") is not None


# LLM: 真实C4分叉和原消息文件，源字节、插件范围与幂等同时验证；不执行历史工具。
# 函数用途: 验证Host返回完整分叉合同并且源记录零修改。
def test_real_fork_returns_contract_and_replays_once(domain_case):
    thread = domain_case.create()
    store = domain_case.base.stores["alice"]
    message = store.messages.append({"thread_id": thread["thread_id"], "role": "user", "content": "独立分叉"})
    path = store.storage.message_path(thread["thread_id"])
    before = path.read_bytes()
    payload = {"source_thread_id": thread["thread_id"], "up_to_message_id": message.message_id, "title": "分支"}
    branch = domain_case.call("conversations.threads.fork", payload)
    assert {"thread_id", "title", "forked_from_thread_id", "forked_from_message_id", "fork_lineage_depth",
            "message_count", "boundary_adjusted", "workspace_root"} <= branch.keys()
    assert branch["message_count"] == 1 and branch["title"] == "分支"
    assert domain_case.call("conversations.threads.fork", payload) == branch
    assert path.read_bytes() == before and len(store.threads.list()) == 2
    assert domain_case.call("conversations.threads.workspace.get", {"thread_id": branch["thread_id"]})["thread_id"] == branch["thread_id"]


# LLM: 真实C2搜索/提案/提交，不调用模型；目录使用C4正式字段，不依赖旧metadata旁路。
# 函数用途: 验证中文多回合检索可按新目录绑定过滤并完成技能发布。
def test_real_turn_extraction_respects_workspace_field(domain_case):
    home = domain_case.base.homes["main"].home_dir
    directory = home / "turn-project"
    directory.mkdir()
    thread = domain_case.create(owner="main", workspace=directory)
    store = domain_case.base.stores["main"]
    message = store.messages.append({"thread_id": thread["thread_id"], "role": "user", "content": "中文流程：先核对输入再生成清单"})
    store.messages.append({"thread_id": thread["thread_id"], "role": "assistant", "content": "检查输入，生成清单并人工核对。"})
    result = domain_case.call("conversations.turns.search", {"query": "中文流程", "workspace_dir": str(directory)}, owner="main")
    assert result["total"] == 1, "目录过滤必须认识C4正式workspace_root字段"
    assert result["items"][0]["workspace_root"] == str(directory)
    proposal = domain_case.call("assets.extract.from_turns.propose", {
        "turn_refs": [{"thread_id": thread["thread_id"], "message_id": message.message_id}], "target_types": ["skill"]}, owner="main")
    read = domain_case.call("assets.extract.from_turns.get", {"proposal_id": proposal["proposal_id"]}, owner="main")
    import signal
    # LLM: 有效集成测试保留；到期以失败中止自锁，不跳过或放宽业务断言，恢复原计时器与处理器。
    # 函数用途: 防止共享底座死锁拖住整套回归。
    def deadline(*_):
        raise TimeoutError("原提炼提交超过10秒；检查资产根锁重入")
    previous = signal.signal(signal.SIGALRM, deadline)
    timer = signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        committed = domain_case.call("assets.extract.from_turns.commit", {"proposal_id": proposal["proposal_id"], "expected_revision": read["revision"]}, owner="main")
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)
    assert committed["status"] == "COMMITTED"


# LLM: 此自包含样本只规定门户合同，不是生产实现；真正工作台源码仅沿显式环境变量追加验收。
# 模块用途: 让干净克隆无需本机目录、浏览器或生产账号也能核对核心断言。
WORKBENCH_CONTRACT_SOURCE = '''
# LLM: 需求矩阵提供最小模板，复制输入避免测试互相改值。
# 函数用途: 提供新增动作的便携模板合同。
def _sample_payload_for_action(action):
    return json.loads(json.dumps(payloads.get(action, {})))

# LLM: 只信服务端私有租约，客户端身份不能进入分派；这里不是生产认证验收。
# 函数用途: 为门户请求提供最小的身份隔离合同。
def execute_core_lab_call(self, request):
    with self._lock:
        lease = self._core_lab_sessions.get(request.get("lab_session_token"))
        if not lease or lease.get("expires_at", 0) <= time.time():
            return {"ok": False, "http_status": 401}
        clean = {key: request[key] for key in ("action", "payload", "idempotency_key") if key in request}
        if lease.get("is_admin"):
            clean["caller_owner_id"] = "local/main"
        else:
            clean["user_session_token"] = lease["user_session_token"]
    return self.execute_host_call(clean, trusted_core_lab=True)

# LLM: 只删除指定私有租约，不读取生产用户。
# 函数用途: 提供注销撤销合同。
def revoke_core_lab_session(self, request):
    with self._lock:
        self._core_lab_sessions.pop(request.get("lab_session_token"), None)
    return {"ok": True}

# LLM: 仅验证已有启用安装的选择规则，不安装、改grant或模拟批准。
# 函数用途: 选择不破坏旧安装的完整能力公共插件。
def install_and_activate_v9_plugin(root):
    home = ensure_owner_home(root, OwnerIdentity.local_main())
    entries = PluginInstallStore(home).snapshot()
    required = set(host_capabilities())
    old = next((row for row in entries if row.manifest.plugin_id == "host-workbench"), None)
    selected = "host-workbench-core" if old and not required <= set(old.manifest.host_api) else "host-workbench"
    entry = next(row for row in entries if row.manifest.plugin_id == selected and row.enabled)
    return home, None, entry, None
'''


# LLM: 默认编译内联纯合同；配置外部目录时只提取指定AST定义，缺文件直接失败，不静默降级。
# 函数用途: 隔离检查模板/门户桥接，不导入工作台或初始化真实home。
def workbench_definition(name):
    import __future__

    import ast
    directory = os.environ.get("MY_AGENT_WORKBENCH_DIR")
    path = Path(directory) / "workbench_server.py" if directory else None
    source = path.read_text() if path is not None else WORKBENCH_CONTRACT_SOURCE
    node = next((row for row in ast.walk(ast.parse(source)) if isinstance(row, ast.FunctionDef) and row.name == name), None)
    assert node is not None, f"缺工作台接口{name}"
    namespace = {"json": json, "time": time, "payloads": {row[0]: row[3] for row in ACTION_CASES}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path) if path is not None else "<workbench-contract>",
                 "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace[name]


# LLM: 模板经真实v2解码，不因前端存在按钮就认为动作可用。
# 函数用途: 确认Action Explorer覆盖全部新增合同。
def test_workbench_templates_cover_all_twenty_actions():
    sample = workbench_definition("_sample_payload_for_action")
    for action, mode, _, required in ACTION_CASES:
        payload = sample(action)
        assert required.keys() <= payload.keys(), f"{action}缺默认模板"
        decode_call(call_bytes(action, payload, key="wp4-ui-template-key-001" if mode == "write" else None))


# LLM: 未认证lab请求必须在调用旧Host工作台入口之前被拒绝，body不能伪造管理员。
# 函数用途: 守住新门户动作的服务端登录门。
def test_core_lab_rejects_forged_owner_before_dispatch():
    import threading
    method = workbench_definition("execute_core_lab_call")
    runtime = SimpleNamespace(_lock=threading.RLock(), _core_lab_sessions={})
    result = method(runtime, {"action": "datasources.list", "caller_owner_id": "local/main", "is_admin": True})
    assert result["ok"] is False and result["http_status"] == 401


# LLM: 真实C1 SQLite规格/秘密/测试/迁移在临时Owner内执行，无外部网络或生产库。
# 函数用途: 验证五个数据源动作的完整本地服务接线和跨用户隔离。
def test_real_sqlite_datasource_lifecycle(domain_case):
    secret = "wp4-only-synthetic-value"
    payload = {"spec": {"datasource_id": "orders", "kind": "sqlite", "title": "订单"},
               "secret": {"password": secret}, "expected_revision": 0}
    saved = domain_case.call("datasources.save", payload)
    assert domain_case.call("datasources.save", payload) == saved
    assert saved["revision"] == 1 and secret not in canonical_json(saved)
    assert domain_case.call("datasources.test", {"datasource_id": "orders"})["ok"] is True
    assert isinstance(domain_case.call("datasources.schema.inspect", {"datasource_id": "orders"}), dict)
    assert domain_case.call("datasources.list", owner="bob")["items"] == []
    target = {"datasource_id": "orders_next", "kind": "sqlite", "title": "迁移目标"}
    prepared = domain_case.call("datasources.migrate", {"source_id": "orders", "target_spec": target})
    assert prepared["state"] == "READY_TO_SWITCH"
    switched = domain_case.call("datasources.migrate", {"source_id": "orders", "target_spec": {"migration_id": prepared["migration_id"]}, "mode": "switch"}, key="wp4-migration-switch-001")
    assert switched["state"] == "SWITCHED"


# LLM: 真实C2动作导入/冻结/编译及原ScriptStore，不启动浏览器，不把编译通过当真实回放。
# 函数用途: 验证录制四动作与秘密参数化保存接线。
def test_real_recorder_import_stop_compile(domain_case):
    record = domain_case.call("recorder.sessions.start", {"start_url": "https://example.com/", "initial_actions": [
        {"action_type": "fill", "selector": "input[name=password]", "value": "wp4-synthetic-password"}]})
    identifier = record["recording_id"]
    stopped = domain_case.call("recorder.sessions.stop", {"recording_id": identifier})
    assert stopped["status"] == "STOPPED"
    assert domain_case.call("recorder.sessions.get", {"recording_id": identifier})["status"] == "STOPPED"
    compiled = domain_case.call("recorder.sessions.compile", {"recording_id": identifier, "save_to_store": True})
    assert "script_spec" in compiled and "wp4-synthetic-password" not in canonical_json(compiled)
    assert domain_case.call("recorder.sessions.get", owner="bob")["items"] == []


# LLM: 默认执行内联门户协议断言，不依赖浏览器；明确配置外部目录才追加Chromium/真实HTML路由夹具检查。
# 函数用途: 核对便携核心合同，并按明确输入追加外部浏览器验收。
def test_core_lab_browser_contract():
    import ast

    sample = workbench_definition("_sample_payload_for_action")
    catalog = [{"action": name, "sample_payload": sample(name), "requires_idempotency": mode == "write"} for name, mode, _, _ in ACTION_CASES]
    if not os.environ.get("MY_AGENT_WORKBENCH_DIR"):
        assert_portable_lab_contract(catalog)
        return
    from playwright.sync_api import sync_playwright
    source = Path(os.environ["MY_AGENT_WORKBENCH_DIR"]) / "workbench_ui.py"
    html = ast.literal_eval(next(node.value for node in ast.parse(source.read_text()).body if isinstance(node, ast.AnnAssign)))
    calls = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        # LLM: 路由不调用真实Host；只记录浏览器实际发出的字节，未知写回执用于重试防重验证。
        # 函数用途: 提供隔离UI的固定HTTP观察点。
        def route(request_route):
            path = request_route.request.url
            data = {"ok": True}
            if path.endswith("/api/state"):
                data = {"ok": True, "actions_catalog": catalog}
            elif path.endswith("/api/portal/core-lab/call"):
                calls.append(request_route.request.post_data_json)
                data = {"ok": False, "http_status": 503, "error_code": "OUTCOME_UNKNOWN", "response_wire": {"error": {"outcome": "unknown"}}}
            request_route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
        page.route("http://127.0.0.1:18429/**", route)
        page.goto("http://127.0.0.1:18429/")
        page.set_content(html)
        page.evaluate("document.getElementById('loginScreen').classList.add('hidden'); switchModule('mod-core-lab'); sessionStorage.setItem('ma_core_lab_session','lab-first')")
        page.evaluate("coreLabLoadCatalog()")
        assert page.locator("#coreLabAction option").count() == 20
        page.on("dialog", lambda dialog: dialog.accept())
        page.select_option("#coreLabAction", "datasources.save")
        page.evaluate("coreLabTemplate(); coreLabExplore()")
        page.evaluate("coreLabRetry()")
        assert len(calls) == 2 and calls[0]["idempotency_key"] == calls[1]["idempotency_key"]
        page.evaluate("sessionStorage.setItem('ma_core_lab_session','lab-second'); coreLabRetry()")
        assert len(calls) == 2, "切换登录者后不能重发上一用户的私密请求"
        browser.close()


# LLM: 使用内联服务端桥和真实decode，不把此协议参考样本称为生产UI验证；敏感输入仅留在本栈。
# 函数用途: 无浏览器时保留模板、身份剥离、原键重放和跨登录阻断的核心断言。
def assert_portable_lab_contract(catalog):
    import threading
    assert len(catalog) == 20
    method = workbench_definition("execute_core_lab_call")
    observed = []
    runtime = SimpleNamespace(_lock=threading.RLock(), _core_lab_sessions={
        "lab-first": {"expires_at": time.time() + 60, "user_session_token": "first-user"},
        "lab-second": {"expires_at": time.time() + 60, "user_session_token": "second-user"}},
        execute_host_call=lambda request, **_: observed.append(request))
    for row in catalog:
        decode_call(call_bytes(row["action"], row["sample_payload"], key="portable-key-001" if row["requires_idempotency"] else None))
    sample = next(row["sample_payload"] for row in catalog if row["action"] == "datasources.save")
    pending = {"lab_session_token": "lab-first", "action": "datasources.save", "payload": sample,
               "idempotency_key": "portable-key-001", "caller_owner_id": "local/main", "as_user": {"user_id": "bob"}}

    # LLM: 便携客户端合同把待发请求绑定创建时登录态；旧租约仍有效时也不允许跨登录重试。
    # 函数用途: 为无浏览器合同保留原键重试和跨登录阻断断言。
    def retry(current_lease):
        if current_lease != pending["lab_session_token"]:
            return {"ok": False, "error": "CROSS_LOGIN_PENDING"}
        return method(runtime, pending)

    method(runtime, pending)
    retry("lab-first")
    assert len(observed) == 2 and observed[0] == observed[1]
    assert observed[0]["user_session_token"] == "first-user"
    assert "caller_owner_id" not in observed[0] and "as_user" not in observed[0]
    assert retry("lab-second")["error"] == "CROSS_LOGIN_PENDING" and len(observed) == 2
    workbench_definition("revoke_core_lab_session")(runtime, {"lab_session_token": "lab-first"})
    assert method(runtime, pending)["http_status"] == 401 and len(observed) == 2


# LLM: 原DomainResult警告必须随同一个回执重放，不因第二次调用消失。
# 函数用途: 防止成功幂等重放丢失领域警告。
def test_write_replay_preserves_warnings(domain_case, monkeypatch):
    from agent_py_agent.agent.plugin_host import capabilities_v2_handlers as handlers
    called = []
    def effect(*_):
        called.append(True)
        return contracts.DomainResult(200, {"ok": True}, warnings=("仅保存草稿，未执行",))
    monkeypatch.setattr(handlers, "_effect", effect)
    payload = {"spec": {"datasource_id": "orders", "kind": "sqlite", "title": "订单"}}
    first = composition.dispatch_call(domain_case.request("datasources.save", payload), domain_case.ports)
    second = composition.dispatch_call(domain_case.request("datasources.save", payload), domain_case.ports)
    assert first.warnings == second.warnings == ("仅保存草稿，未执行",)
    assert len(called) == 1


# LLM: 登出须撤销服务器私有租约，而不只是清理浏览器存储。
# 函数用途: 检查lab短期令牌的显式撤销入口。
def test_core_lab_logout_revokes_lease():
    import threading
    method = workbench_definition("revoke_core_lab_session")
    runtime = SimpleNamespace(_lock=threading.RLock(), _core_lab_sessions={"synthetic-lease": {}})
    result = method(runtime, {"lab_session_token": "synthetic-lease"})
    assert result["ok"] is True and runtime._core_lab_sessions == {}


# LLM: 只测旧安装选择逻辑，不伪造真实安装/认证证据；不得靠改写旧授权或绕过释放协议升级。
# 函数用途: 确认管理员默认改用完整能力的兼容公共包，而不是无条件复用旧清单。
def test_workbench_does_not_reuse_legacy_public_capabilities():
    method = workbench_definition("install_and_activate_v9_plugin")
    needed = {"assets.write", "datasources.read", "datasources.write", "recorder.control"}
    old = SimpleNamespace(manifest=SimpleNamespace(plugin_id="host-workbench", host_api=tuple(host_capabilities() - needed)), enabled=True, activation_id="old")
    new = SimpleNamespace(manifest=SimpleNamespace(plugin_id="host-workbench-core", host_api=tuple(host_capabilities())), enabled=True, activation_id="new")
    home = SimpleNamespace(owner_id="local/main")
    namespace = method.__globals__
    namespace.update(OwnerIdentity=OwnerIdentity, ensure_owner_home=lambda *_: home,
        LocalUserStore=lambda _: SimpleNamespace(owner_home=lambda _: home),
        PluginInstallStore=lambda _: SimpleNamespace(snapshot=lambda: [old, new]), host_capabilities=host_capabilities,
        DEFAULT_ZIP_PATH=Path("not-written.zip"), PRIVATE_ZIP_PATH=Path("not-written-private.zip"),
        build_workbench_plugin_zip=lambda _, **kw: (None, SimpleNamespace(manifest=SimpleNamespace(plugin_id=kw["plugin_id"]))),
        PluginActivationRef=SimpleNamespace(from_owner=lambda *_: None),
        host_api_mod=SimpleNamespace(set_host_api_base=lambda _: None, HOST_API_TOKEN_ENV="test", issue_host_api_env=lambda *_: {"test": "synthetic"}))
    _, _, entry, _ = method(Path("not-used-home"))
    assert needed <= set(entry.manifest.host_api)


# LLM: 领域明确标unknown时，宿主不得保存成completed或让门户显示确定成功。
# 函数用途: 验证迁移不确定提交沿原未知结果返回。
def test_migration_unknown_is_not_cached_as_success(backend_case, monkeypatch):
    backend = sys.modules["agent_py_agent.agent.assets.datasource_store"]
    monkeypatch.setattr(backend.DatasourceStore, "migrate_datasource", lambda *_args, **_kw:
        {"effect_outcome": "unknown", "error_code": "DATASOURCE_MIGRATION_COMMIT_UNCERTAIN"})
    with pytest.raises(HostAPIError) as error:
        backend_case.call("datasources.migrate", {"source_id": "orders", "target_spec": {}})
    assert error.value.code == "OUTCOME_UNKNOWN" and error.value.outcome == "unknown"


# LLM: 家目录本身及内部子目录都不授予普通用户显式目录权；拒绝不得改线程或业务回执。
# 函数用途: 验证所有目录参数入口只对管理员开放。
@pytest.mark.parametrize("action", ["conversations.threads.create", "conversations.threads.list",
    "conversations.threads.workspace.switch", "assets.workspace.list", "assets.workspace.promote",
    "datasources.list", "datasources.save", "conversations.turns.search", "conversations.threads.fork",
    "assets.extract.from_turns.propose", "recorder.sessions.start", "recorder.sessions.compile"])
@pytest.mark.parametrize("location", ["home", "child"])
def test_non_admin_explicit_directory_is_always_forbidden(domain_case, action, location):
    home = domain_case.base.homes["alice"].home_dir
    directory = home if location == "home" else home / "project"
    directory.mkdir(exist_ok=True)
    thread = domain_case.create()
    required = next((row[3] for row in ACTION_CASES if row[0] == action), {})
    payload = {**required, "workspace_dir": str(directory)}
    if "thread_id" in payload:
        payload["thread_id"] = thread["thread_id"]
    if "source_thread_id" in payload:
        payload["source_thread_id"] = thread["thread_id"]
    before = {str(path): path.read_bytes() for path in home.rglob("*.json")}
    with pytest.raises(HostAPIError) as error:
        domain_case.call(action, payload)
    assert (error.value.status, error.value.code) == (403, "WORKSPACE_DIR_FORBIDDEN_FOR_NON_ADMIN")
    assert {str(path): path.read_bytes() for path in home.rglob("*.json")} == before


# LLM: 源由别的安装创建，只靠explicit可见；新调用模拟范围收紧，仍运行真实分叉和控制账。
# 函数用途: 防止源线程撤权后凭同键取回旧分叉回执。
def test_fork_replay_rechecks_removed_explicit_source(domain_case):
    source = domain_case.create(installation="source-installation")
    store = domain_case.base.stores["alice"]
    store.messages.append({"thread_id": source["thread_id"], "role": "user", "content": "显式授权的源"})
    payload = {"source_thread_id": source["thread_id"], "title": "不可泄露的回执"}
    scope = {"created_by_installation": True, "explicit_ids": [source["thread_id"]]}
    first = domain_case.call("conversations.threads.fork", payload, thread_scope=scope)
    assert first["thread_id"] != source["thread_id"]
    home = domain_case.base.homes["alice"].home_dir
    before = {str(path): path.read_bytes() for path in home.rglob("*.json")}
    with pytest.raises(HostAPIError) as error:
        domain_case.call("conversations.threads.fork", payload,
            thread_scope={"created_by_installation": True, "explicit_ids": []})
    assert (error.value.status, error.value.code) == (404, "RESOURCE_NOT_FOUND")
    assert {str(path): path.read_bytes() for path in home.rglob("*.json")} == before


# LLM: 源仍显式可见但目标不再由created范围覆盖；回执必须核目标当前可见性。
# 函数用途: 防止只核源却泄露已撤权的分叉目标。
def test_fork_replay_rechecks_hidden_target(domain_case):
    source = domain_case.create()
    domain_case.base.stores["alice"].messages.append({"thread_id": source["thread_id"], "role": "user", "content": "源"})
    payload = {"source_thread_id": source["thread_id"]}
    assert domain_case.call("conversations.threads.fork", payload)["thread_id"]
    with pytest.raises(HostAPIError) as error:
        domain_case.call("conversations.threads.fork", payload,
            thread_scope={"created_by_installation": False, "explicit_ids": [source["thread_id"]]})
    assert (error.value.status, error.value.code) == (404, "RESOURCE_NOT_FOUND")


# LLM: 记录读取用接线替身，原线程和控制账真实；同键的新授权必须重新核提案/录制来源线程。
# 函数用途: 防止其它写动作从旧回执旁路对象范围门。
@pytest.mark.parametrize("action", ["assets.extract.from_turns.propose", "assets.extract.from_turns.commit",
    "recorder.sessions.start", "recorder.sessions.stop", "recorder.sessions.compile"])
def test_resource_write_replay_rechecks_current_threads(backend_case, monkeypatch, action):
    case = backend_case
    payload = json.loads(canonical_json(next(row[3] for row in ACTION_CASES if row[0] == action)))
    if "turn_refs" in payload:
        payload["turn_refs"][0]["thread_id"] = case.thread_id
    if action.endswith(".start"):
        payload["thread_id"] = case.thread_id
    backend = sys.modules["agent_py_agent.agent.assets.browser_recorder"]
    monkeypatch.setattr(backend, "get_recording_session", lambda home, identifier:
        {"recording_id": identifier, "thread_id": case.thread_id})
    assert case.call(action, payload)
    with pytest.raises(HostAPIError) as error:
        case.call(action, payload, thread_scope={"created_by_installation": False, "explicit_ids": []})
    assert (error.value.status, error.value.code) == (404, "RESOURCE_NOT_FOUND")


# LLM: 不提供外部目录且禁止读取外部工作台文件/导入浏览器依赖，核心合同仍须执行而非skip。
# 函数用途: 验证干净克隆可以独立运行模板、门户身份和幂等合同。
def test_workbench_contract_does_not_require_external_checkout(monkeypatch):
    monkeypatch.delenv("MY_AGENT_WORKBENCH_DIR", raising=False)
    original = Path.read_text

    # LLM: 只阻止工作台外部文件，pytest源码和原业务文件仍沿真实读取。
    # 函数用途: 模拟CI没有外部工作台目录。
    def no_external(path, *args, **kwargs):
        if path.name in {"workbench_server.py", "workbench_ui.py"}:
            raise FileNotFoundError("external workbench is absent")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_external)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    for check in (test_workbench_templates_cover_all_twenty_actions,
                  test_core_lab_rejects_forged_owner_before_dispatch, test_core_lab_logout_revokes_lease,
                  test_workbench_does_not_reuse_legacy_public_capabilities, test_core_lab_browser_contract):
        try:
            check()
        except (ImportError, FileNotFoundError) as error:
            pytest.fail(f"未配置外部目录时核心合同仍依赖本机资源: {type(error).__name__}")


# LLM: 原执行中回执的副作用仍未知；线程撤权可阻止读取结果，但不能把原unknown重标成not_started。
# 函数用途: 验证未完成写入重试仍报unknown且不重新执行业务。
def test_unknown_receipt_stays_unknown_after_thread_scope_shrinks(backend_case, monkeypatch):
    from agent_py_agent.agent.plugin_host import capabilities_v2_handlers as handlers
    calls = []

    # LLM: 只模拟业务结果未知，原控制账仍真实写executing状态，不碰生产服务。
    # 函数用途: 为重试保留一个真实的未完成回执。
    def uncertain(*_args):
        calls.append(True)
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")

    monkeypatch.setattr(handlers, "_effect", uncertain)
    payload = {**next(row[3] for row in ACTION_CASES if row[0] == "recorder.sessions.start"),
               "thread_id": backend_case.thread_id}
    with pytest.raises(HostAPIError) as first:
        backend_case.call("recorder.sessions.start", payload)
    assert first.value.code == "OUTCOME_UNKNOWN"
    with pytest.raises(HostAPIError) as replay:
        backend_case.call("recorder.sessions.start", payload,
            thread_scope={"created_by_installation": False, "explicit_ids": []})
    assert (replay.value.code, replay.value.status, replay.value.outcome) == ("OUTCOME_UNKNOWN", 503, "unknown")
    assert len(calls) == 1


# LLM: 创建仍走原ConversationDomain而非新增_once；缓存目标撤权后由原线程回执门拒绝。
# 函数用途: 验证旧创建动作重放没有漏掉当前目标可见性。
def test_original_create_replay_rechecks_hidden_target(domain_case):
    thread = domain_case.create()
    assert thread["thread_id"]
    request = domain_case.request("conversations.threads.create", {"title": "项目会话"},
        key="wp4-create-key-001", thread_scope={"created_by_installation": False, "explicit_ids": []})
    with pytest.raises(HostAPIError) as error:
        composition.dispatch_call(request, domain_case.ports)
    assert (error.value.code, error.value.status) == ("RESOURCE_NOT_FOUND", 404)


# LLM: 安装者虽是管理员，私有安装也不得继承治理或跨Owner委托；测试不更改安装记录。
# 函数用途: 用真实令牌发行器核对私有管理员插件的有效能力和目标边界。
def test_admin_private_plugin_drops_delegation_and_denies_foreign_target(real_host):
    composed, source, _, home = real_host
    authorized = composed.issuer.authorize(source, decode_call(call_bytes("host.capabilities.get", {})))
    assert authorized.effective_owner == home.identity
    assert not {"users.manage", "act_as_user"} & authorized.binding.capabilities
    row = {"schema": "plugin_host_call.v2", "action": "conversations.threads.list", "payload": {},
           "as_user": {"user_id": "alice"}}
    with pytest.raises(HostAPIError) as error:
        composed.issuer.authorize(source, decode_call(canonical_json(row).encode()))
    assert error.value.status == 403 and error.value.code in {"TARGET_DENIED", "CAPABILITY_DENIED"}


# LLM: 未保存installation_scope的旧安装按private处理，不能因字段缺失扩大权限。
# 函数用途: 验证纯能力计算对旧私有记录同样移除治理和委托能力。
def test_legacy_private_plugin_scope_cannot_keep_admin_capabilities():
    from agent_py_agent.agent.plugin_host.authorization import _caller_capabilities

    caps = frozenset({"read", "users.manage", "act_as_user"})
    assert _caller_capabilities(OwnerIdentity.local_main(), SimpleNamespace(), None, caps) == {"read"}


# LLM: 真实授权门在读取草稿之前拒绝缺chat.history；通用read仍存在，不能被误当成充分授权。
# 函数用途: 验证提炼草稿的历史权限负向边界。
def test_extraction_get_requires_history_even_with_generic_read(real_host):
    composed, source, policy, _ = real_host
    policy.remove("chat.history")
    assert "read" in policy
    with pytest.raises(HostAPIError) as error:
        composed.issuer.authorize(source, decode_call(call_bytes("assets.extract.from_turns.get", {"proposal_id": "proposal-one"})))
    assert (error.value.status, error.value.code) == (403, "CAPABILITY_DENIED")


# LLM: 通过真实install请求显式创建public安装，不替换授权函数或把私有记录临时改名。
# 函数用途: 验证公共管理员插件仍保留原有跨用户授权路径。
def test_public_admin_plugin_keeps_explicit_cross_owner_delegation(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_host_api
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    original = PluginInstallStore.install

    # LLM: 仅改变本次新安装请求的范围，原Store仍验证管理员资格和持久化摘要。
    # 函数用途: 在临时Owner安装真正的public插件。
    def install_public(store, request):
        return original(store, replace(request, installation_scope="public"))

    monkeypatch.setattr(PluginInstallStore, "install", install_public)
    _, _, entry, ref = active_grant(tmp_path, ["read", "act_as_user"])
    assert entry.installation_scope == "public"
    plugin_host_api.set_host_api_base("http://127.0.0.1:18429")
    issuer = IngressIssuer()
    try:
        token = plugin_host_api.issue_host_api_env(ref, entry.manifest.plugin_id)[plugin_host_api.HOST_API_TOKEN_ENV]
        row = {"schema": "plugin_host_call.v2", "action": "conversations.threads.list", "payload": {},
               "as_user": {"user_id": "alice"}}
        with pytest.raises(HostAPIError) as pending:
            issuer.authorize(issuer.ingress(token), decode_call(canonical_json(row).encode()))
        # 能力和目标门已通过，因本夹具故意未接resolver才到503；不宣称实际用户数据读取成功。
        assert (pending.value.status, pending.value.code) == (503, "HOST_NOT_READY")
    finally:
        issuer.close()
        plugin_host_api.set_host_api_base(None)
