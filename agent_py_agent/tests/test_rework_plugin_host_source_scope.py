"""R5-P1-01: 插件资产必须继承可信 Gateway 的宿主来源保护。

复用生产安装/激活夹具、真实解码/令牌授权/组合根/队列/资产栅栏和 Python 子进程。
只把 socket 运输换成进程内公开 dispatch；guard 车道仅选择已有外层 Seatbelt 分支，
不是原生沙箱通过证据。另有不启动进程的原生启动边界检查和按真实能力跳过的原生执行检查。
所有来源与业务输入均为临时合成资料。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_host_api
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
from agent_py_agent.agent.path_access_policy import host_sandbox_paths
from agent_py_agent.agent.plugin_host import gateway_assets, gateway_runtime
from agent_py_agent.agent.plugin_host.action_specs import action_spec
from agent_py_agent.agent.plugin_host.authorization import authorize_call
from agent_py_agent.agent.plugin_host.composition import dispatch_call
from agent_py_agent.agent.plugin_host.contracts import decode_call
from agent_py_agent.agent.plugin_host.schemas import canonical_json, thaw
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.secret_ref import managed_secret_environment_names
from agent_py_agent.agent.tooling import sandbox as tooling
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as _production_host,
)

_HOST_ENV = "MY_HOST_APP_VALUE"
_BUSINESS_ENV = "BUSINESS_REGION"
_EXPECTED = {"env_visible": False, "file_visible": False, "business_env": "north",
             "business_file": "ordinary business input"}
_ACTIONS = ("execute.script", "execute.workflow", "execute.workflow_resume")
_PROBE = '''import json, os, sys
from pathlib import Path
inputs = json.load(sys.stdin)
result = {"env_visible": "MY_HOST_APP_VALUE" in os.environ,
          "business_env": os.environ.get("BUSINESS_REGION"),
          "business_file": Path(inputs["business_file"]).read_text()}
try:
    result["file_visible"] = bool(Path(inputs["host_file"]).read_bytes())
except (PermissionError, FileNotFoundError):
    result["file_visible"] = False
Path(inputs["report"]).write_text(json.dumps(result))
if inputs.get("pause_marker"):
    marker = Path(inputs["pause_marker"])
    if not marker.exists():
        marker.write_text("paused once")
        result["_pause_for_resume"] = True
print(json.dumps(result))
'''


# LLM: 只替换 socket 出生；原安装签发器仍验证真实激活，地址不被访问，不伪造 AuthorizedCall。
# 函数用途: 为进程内公开分派复用生产组合夹具。
def _in_process_start(_server):
    plugin_host_api.set_host_api_base("http://127.0.0.1:1")


# LLM: 请求仍经过真实 decoder、issuer、授权与公开 dispatch，省略的仅为 socket 和传输限流。
# 函数用途: 发送一个合成资产宿主请求并检查业务 HTTP 状态。
def _call(case, action, payload=None, *, user="alice", status=200):
    case.sequence += 1
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if action_spec(action).requires_idempotency or action in _ACTIONS:
        row["idempotency_key"] = f"source_scope_call_{case.sequence:04d}"
    if user is not None:
        row["as_user"] = {"user_id": user}
    composition = case.host[0].plugin_host.composition
    decoded = decode_call(json.dumps(row).encode())
    authorized = authorize_call(composition.issuer.ingress(case.host[1]), decoded)
    result = dispatch_call(authorized, composition.ports)
    assert result.http_status == status, result
    return thaw(result.data)


# LLM: 两个工作流只在是否创建真实暂停断点上不同；所有步骤仍为获批注册脚本，不调用模型。
# 函数用途: 构造普通执行和断点续跑需要的资产定义。
def _flow(workflow_id, inputs):
    return WorkflowDefinition(
        AssetDescriptor(workflow_id, "workflow", workflow_id, metadata={"revision": 1}),
        (WorkflowStepSpec("probe", "probe", "deterministic_script", script_id="source-probe",
                          inputs_map=inputs, on_error="fail"),),
    )


# LLM: 仅本模块使用；先解析来源再避开 Linux 被 scratch 重映射的 /tmp，依赖 teardown 保证 Host 先关闭。
# 函数用途: 保留安全的 pytest 根，必要时创建并自动清理非重映射的私有资产根。
@pytest.fixture
def asset_root(tmp_path):
    root = tmp_path.resolve()
    if not root.is_relative_to("/tmp"):
        yield tmp_path
        return
    with tempfile.TemporaryDirectory(prefix="my-agent-source-scope-", dir="/var/tmp") as directory:
        root = Path(directory).resolve()
        assert not root.is_relative_to("/tmp"), "asset root must be outside the remapped /tmp subtree"
        yield root


# LLM: 来源全是临时合成值；宿主 file: 来源刻意落在 alice 获批业务根中，不靠目录外拒绝掩盖缺口；先关闭 Host 再清理 asset_root。
# 函数用途: 提供完整生产 Host API 组合、可信配置与注册脚本/工作流。
@pytest.fixture
def asset_case(asset_root, monkeypatch):
    tree = Path(__file__).resolve().parents[1]
    for module in (gateway_runtime, gateway_assets, script_runner, attempt, tooling):
        assert Path(module.__file__).resolve().is_relative_to(tree)
    monkeypatch.setenv(_HOST_ENV, "synthetic-host-only-value")
    monkeypatch.setenv(_BUSINESS_ENV, "north")
    monkeypatch.setattr(GatewayHTTPServer, "start", _in_process_start)
    home = asset_root / "owners/local/alice"
    workspace = home / "workspace"
    source, sibling = workspace / "host-source.data", workspace / "host-source.data-business"
    report = workspace / "report.json"
    # 业务输入中的同名字段不能替代可信宿主保护。
    inputs = {"host_file": str(source), "business_file": str(sibling), "report": str(report),
              "host_credential_env_names": [], "host_path_protection": {"hidden": []}}
    normal = _flow("source-flow", inputs)
    resumed = _flow("source-resume", {**inputs, "pause_marker": str(workspace / "pause.marker")})
    grants = [{"kind": "script", "id": "source-probe", "revision": 1,
               "sha256": hashlib.sha256(_PROBE.encode()).hexdigest()}]
    for flow in (normal, resumed):
        grants.append({"kind": "workflow", "id": flow.descriptor.id, "revision": 1,
                       "sha256": hashlib.sha256(canonical_json(flow.to_dict()["steps"]).encode()).hexdigest()})
    fixture = _production_host.__wrapped__(asset_root, SimpleNamespace(param={"asset_refs": grants}))
    host = next(fixture)
    try:
        case = SimpleNamespace(host=host, home=home, workspace=workspace, source=source, sibling=sibling,
                               report=report, inputs=inputs, sequence=0)
        _call(case, "users.local.create", {"user_id": "alice"}, user=None, status=201)
        workspace.mkdir(parents=True, exist_ok=True)
        source.write_text("synthetic-host-file-value")
        sibling.write_text("ordinary business input")
        config_file = asset_root / "deployment.yaml"
        config_file.write_text(f"my_agent_home: {json.dumps(str(asset_root))}\n"
                               f"feishu_app_secret: env:{_HOST_ENV}\n"
                               f"feishu_encrypt_key: {json.dumps('file:' + str(source))}\n")
        config = load_config(config_file)
        assert _HOST_ENV in managed_secret_environment_names(config)
        assert source in host_sandbox_paths(asset_root, home, config=config).hidden
        case.config, case.config_file = config, config_file
        case.thread = _call(case, "conversations.threads.create", {}, status=201)["thread_id"]
        ScriptStore(home, extra_roots=()).save_script(
            AssetDescriptor("source-probe", "script", "source-probe", metadata={"revision": 1}), _PROBE)
        for flow in (normal, resumed):
            WorkflowStore(home, extra_roots=()).save_workflow(flow)
        yield case
    finally:
        next(fixture, None)


# LLM: 只在真实 Gateway 对象上切换可信配置端口；agent 存在时 control_plane 故意提供空配置来检查优先级。
# 函数用途: 选择任务要求覆盖的两种生产配置来源。
def _configure(case, origin):
    server = case.host[0]
    server.agent = SimpleNamespace(config=case.config) if origin == "agent" else None
    server.control_plane = SimpleNamespace(config=None if origin == "agent" else case.config)


# LLM: 终态和断点均从原队列/运行账本读取；resume 真正重跑暂停步骤，不伪造 run record。
# 函数用途: 执行指定动作并返回请求回执及必要的原工作流记录。
def _execute_asset(case, action):
    payload = {"thread_id": case.thread, "expected_revision": 1}
    if action == "execute.script":
        payload.update(script_id="source-probe", inputs=case.inputs)
    elif action == "execute.workflow":
        payload["workflow_id"] = "source-flow"
    else:
        paused = _call(case, "execute.workflow", {**payload, "workflow_id": "source-resume"}, status=202)
        poll = _call(case, "conversations.requests.poll", {"request_id": paused["request_id"]})
        assert poll["request"]["state"] == "failed", poll
        (record,) = WorkflowRunStore(case.home).list_runs(workflow_id="source-resume")
        assert record.status == "paused_for_resume", record
        payload.update(workflow_run_id=record.workflow_run_id, from_step_id="probe")
    accepted = _call(case, action, payload, status=202)
    assert accepted["accepted"]
    resources = case.host[0].plugin_host.composition.ports.observations.services.resources
    paths = resources.paths_for(SimpleNamespace(owner_home=resources.users.owner_home("alice")))
    terminal = json.loads((paths.terminal / (accepted["request_id"] + ".json")).read_text())
    assert terminal["schema_version"] == "gateway_terminal_request.v1"
    assert terminal["id"] == terminal["terminal_response"]["id"] == accepted["request_id"]
    assert terminal["terminal_response"]["ok"] is True and terminal["status"] == "done", terminal
    assert terminal["metadata"]["plugin_provenance"]["kind"] == action.split(".", 1)[1]
    if action != "execute.workflow_resume":
        polled = _call(case, "conversations.requests.poll", {"request_id": accepted["request_id"]})
        assert polled["request"]["state"] == "succeeded", polled
    # 基线 requests._read_receipt 不接受 workflow_resume kind（独立既有缺陷，超出白名单）；
    # 续跑用原 canonical 终态 + WorkflowRunStore + 子进程报告验事实，不冒称其公开 poll 成功。
    if action != "execute.script":
        workflow_id = "source-resume" if action.endswith("resume") else "source-flow"
        (record,) = WorkflowRunStore(case.home).list_runs(workflow_id=workflow_id)
        assert record.status == "succeeded" and record.steps[0].status == "succeeded", record
        assert record.steps[0].output_snapshot == _EXPECTED, record.steps[0]
    return accepted


# LLM: 只固定已有非strict外层Seatbelt选择器；执行、环境擦洗、路径spec和Python守卫保持生产实现。
# 函数用途: 验证三种插件资产动作在两个可信配置入口都不泄露来源且正常业务仍可用。
@pytest.mark.parametrize("origin", ["agent", "control_plane"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_real_asset_child_hides_configured_sources(asset_case, monkeypatch, origin, action):
    case = asset_case
    _configure(case, origin)
    monkeypatch.setattr(script_runner, "_outer_seatbelt_active", lambda: True)
    _execute_asset(case, action)
    observed = json.loads(case.report.read_text())
    assert observed == _EXPECTED, observed
    assert case.source.read_text() == "synthetic-host-file-value"
    assert case.sibling.read_text() == "ordinary business input"


# LLM: 只替换平台/就绪事实和最终进程启动点；真实 runner 生成 env、spec、Python guard 与原生 argv，不执行替代命令。
# 函数用途: 即使本机无法启动原生沙箱，也检查三种资产动作交到原生启动边界的完整保护。
@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("origin", ["agent", "control_plane"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_native_launch_boundary_keeps_environment_and_path_projection(asset_case, monkeypatch, system, origin, action):
    case = asset_case
    _configure(case, origin)
    monkeypatch.setattr(attempt.platform, "system", lambda: system)
    monkeypatch.setattr(script_runner, "_outer_seatbelt_active", lambda: False)
    monkeypatch.setattr(tooling, "find_bwrap", lambda: "/fixture/bwrap")
    monkeypatch.setattr(tooling, "_PROC_MOUNT_KIND", "dir")
    # 只对编译边界提供合成就绪值，不把它计为真实内核执行。
    monkeypatch.setattr(attempt.AttemptExecutionSandbox, "require_ready",
                        lambda _self: tooling.SandboxReadiness(True, "FIXTURE_ONLY", "compile boundary"))
    if system == "Darwin":
        original_which = attempt.shutil.which
        monkeypatch.setattr(attempt.shutil, "which", lambda name, *args, **kwargs:
                            "/fixture/sandbox-exec" if name == "sandbox-exec" else original_which(name, *args, **kwargs))
    specs, launches = [], []
    can_apply = script_runner._can_apply_sandbox

    def observe_spec(sandbox, *, strict=False):
        specs.append(sandbox.spec)
        return can_apply(sandbox, strict=strict)

    def observe_launch(command, folder, environment, input_json, timeout, capture):
        launches.append((command, environment, json.loads(environment["MY_AGENT_SCRIPT_GUARD_JSON"])))
        output = dict(_EXPECTED)
        if "pause_marker" in json.loads(input_json) and len(launches) == 1:
            output["_pause_for_resume"] = True
        return 0, json.dumps(output), "", "", {}

    monkeypatch.setattr(script_runner, "_can_apply_sandbox", observe_spec)
    monkeypatch.setattr(script_runner, "_execute", observe_launch)
    _execute_asset(case, action)
    assert len(specs) == len(launches) == (2 if action.endswith("resume") else 1)
    host = host_sandbox_paths(case.host[2].root, case.home, config=case.config)
    for spec, (command, environment, guard) in zip(specs, launches):
        assert _HOST_ENV not in environment and environment[_BUSINESS_ENV] == "north"
        assert set(spec.hidden_paths) == set(host.hidden)
        assert set(spec.protected_write_paths) == set(host.readonly)
        assert spec.protected_write_patterns == host.patterns
        assert spec.protected_directory_write_patterns == host.directory_write_patterns
        assert spec.protected_directory_unlink_patterns == host.directory_unlink_patterns
        assert not spec.network_access and case.workspace in spec.public_read_roots
        assert case.workspace in spec.extra_write_roots
        assert set(guard["host_exclusive_paths"]) == set(map(str, host.hidden))
        assert not guard["network_access"] and guard["block_subprocess"] is False
        if system == "Linux":
            # 只读插件形态按原SandboxSpec合同不额外套资源限制；bwrap本身仍是不可去掉的安全外层。
            assert command[0] == "/fixture/bwrap"
            assert {"--die-with-parent", "--new-session", "--unshare-net"}.issubset(command)
            masks = [command[index + 2] for index in range(len(command) - 2)
                     if command[index:index + 2] == ["--ro-bind", tooling.HIDDEN_FILE_MASK_SOURCE]]
            assert str(case.source) in masks and str(case.sibling) not in masks
        else:
            assert command[:2] == ["/fixture/sandbox-exec", "-p"]
            assert str(case.source) in command[2] and "deny file-read" in command[2]


# LLM: 不替换原生沙箱、不伪造就绪，也不以无隔离进程替代；平台/就绪缺失明确 skip，启动后的失败保持失败。
# 函数用途: 在支持的平台真实执行插件脚本、工作流与续跑，并检查最终子进程观察结果。
@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_native_asset_execution_hides_configured_sources(asset_case, system, action):
    if (system == "Linux" and not sys.platform.startswith("linux")) or (system == "Darwin" and sys.platform != "darwin"):
        pytest.skip(f"需要真实 {system} 宿主")
    if script_runner._outer_seatbelt_active():
        pytest.skip("真实探针确认外层 Seatbelt 禁止嵌套 sandbox_apply；未验证内层原生执行")
    case = asset_case
    _configure(case, "agent")
    sandbox = attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
        attempt_view=case.workspace, staging_root=case.workspace, shared_workspace=case.workspace,
        owner_home=case.home, network_access=False))
    try:
        sandbox.require_ready()
    except attempt.SandboxUnavailableError as exc:
        pytest.skip(f"真实 {system} 沙箱未就绪：{exc}")
    _execute_asset(case, action)
    assert json.loads(case.report.read_text()) == _EXPECTED


# LLM: 显式必传参数是内部边界，不能让未来调用者静默创建无保护的 runner。
# 函数用途: 锁定资产执行器的两项 keyword-only 必传合同。
def test_asset_executor_requires_explicit_host_protection():
    parameters = inspect.signature(gateway_assets.execute_registered_asset).parameters
    for name in ("host_credential_env_names", "host_path_protection"):
        assert name in parameters
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty


# LLM: 最小宿主没有配置时仍投影规范布局；payload 中的伪造空保护完全不参与权威计算。
# 函数用途: 覆盖缺少 agent/control_plane/config 属性的独立宿主兼容行为。
@pytest.mark.parametrize("server", [SimpleNamespace(), SimpleNamespace(agent=SimpleNamespace()),
                                    SimpleNamespace(agent=None, control_plane=SimpleNamespace())])
def test_minimal_host_projects_canonical_layout(tmp_path, monkeypatch, server):
    home = tmp_path / "owners/local/alice"
    context = SimpleNamespace(owner_home=SimpleNamespace(home_dir=home))
    plan = SimpleNamespace(action="execute.script")
    observed = []

    def execute(actual_context, actual_plan, actual_fence, **protection):
        observed.append((actual_context, actual_plan, actual_fence, protection))
        return True, ""

    monkeypatch.setattr(gateway_assets, "execute_registered_asset", execute)
    requests = gateway_runtime.GatewayPluginRequests(SimpleNamespace(server=server, root=tmp_path))
    fence = object()
    assert requests._run_execution_plan(context, plan, {"config": None, "host_path_protection": {}}, fence) == (True, "")
    assert observed == [(context, plan, fence, {"host_credential_env_names": frozenset(),
                                               "host_path_protection": host_sandbox_paths(tmp_path, home, config=None)})]


# LLM: 配置投影出错必须在资产调用前传播，绝不降为空保护；只替换最后资产端口以证明未调用。
# 函数用途: 回归真实配置中非法来源路径的失败即关闭行为。
@pytest.mark.parametrize("origin", ["agent", "control_plane"])
def test_configured_projection_error_never_launches_asset(asset_case, monkeypatch, origin):
    case = asset_case
    case.config.host_exclusive_path_roots = ["relative-source"]
    _configure(case, origin)
    resources = SimpleNamespace(server=case.host[0], root=case.host[2].root)
    context = SimpleNamespace(owner_home=SimpleNamespace(home_dir=case.home))
    called = []

    def execute(*args, **kwargs):
        called.append((args, kwargs))
        return True, ""

    monkeypatch.setattr(gateway_assets, "execute_registered_asset", execute)
    with pytest.raises(ValueError, match="HOST_EXCLUSIVE_SOURCE_INVALID"):
        gateway_runtime.GatewayPluginRequests(resources)._run_execution_plan(
            context, SimpleNamespace(action="execute.script"), {}, object())
    assert called == []
