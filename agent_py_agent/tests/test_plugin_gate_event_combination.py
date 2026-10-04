# LLM: B5拒绝必须在B4 handler观察之前生效；用真实样例和执行入口，传输替身不冒充宿主启用验收。
# 模块用途: 固定17j执行/观察/归档/决定写账的组合链，真实读取B6展示，不手插决定。
"""B5+B4+B8 组合：真实 rm-guard 协议/注册表/执行器；只替换安装激活与通道传输。

临时 owner 只安装 v8、不启用；不启动 Gateway 或沙箱。原归档与持久化入口写决定，
实际 /plugins info 消费这次裁决；绝不手插决定行伪造接通。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.plugin_events.points import EventPointContext
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call
from agent_py_agent.tests.test_plugin_event_display import decisions, info_message
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_tool_gate_execution import _Harness
from scripts.build_plugin_files_package import build_files_package


# LLM: 仅临时安装v8样例，不启用、不解除B7关闭门；协议函数从样例源码原样加载。
# 函数用途: 给组合用例准备真实样例声明与服务，所有写入只落pytest临时目录。
def _installed_sample(tmp_path):
    (tmp_path / "owner").mkdir()
    service, _ = manager(tmp_path / "owner")
    project = Path(__file__).resolve().parents[2] / "plugins" / "rm-guard"
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, project, tmp_path / "rm-guard.zip")
    result = service.command(f'/plugins install "{package}"', revision=service.catalog().revision,
                             request_id="install-rm-guard")
    assert result["state"] == "succeeded", result
    manifest = PluginInstallStore(service.context.owner).snapshot()[0].manifest
    spec = importlib.util.spec_from_file_location("combination_rm_guard", project / "src" / "server.py")
    sample = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample)
    return service, manifest, sample


# LLM: 只经产品仓储建立临时运行身份，不写runtime_events；决定必须来自原归档/持久化链。
# 函数用途: 为组合调用准备真实owner库与可归档的宿主投影，写入均在测试目录。
def _ledger_host(service, root):
    owner_home = service.context.owner.home_dir
    repo = RuntimeRepository(runtime_db_path(owner_home))
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    repo.create_agent_run(task_run_id=task_run["task_run_id"], run_id="run-combination")
    return SimpleNamespace(root=root, config=AgentConfig(),
        home_paths=SimpleNamespace(owner_home_dir=owner_home), subagents=SimpleNamespace(runtime_db=repo))


# LLM: 复用生产的archive_tool_call_record→persist_tool_runtime_ledger两段，不构造决定字段或直接append_event。
# 函数用途: 将真实执行结果沿原归档入口保存，供B6查询核对接缝。
def _persist_execution(agent, execution):
    params = SimpleNamespace(request_id="combination", task_id="", run_id=execution.call.run_id,
        attempt_id=execution.call.attempt_id, run_scope=None, context_scope="default",
        task_attributes={}, live_archive_state={})
    record = ToolCallRecordParams(params, 1, 1, execution.call, execution.result, execution.states)
    archive = archive_tool_call_record(agent, record)
    persist_tool_runtime_ledger(agent, archive)
    return archive


# LLM: 返回安装样例的隔离通道投影，真实安装仍停用；协议裁决来自样例，不构造决定账本。
# 函数用途: 单独准备真实样例协议的假传输，避免组合fixture混入协议装配细节。
def _sample_gate_harness(service, manifest, sample):
    harness = _Harness()
    harness.owner = service.context.owner
    # 激活是测试投影，真实安装仍停用，B7 关闭门不解除。
    harness.rows = [SimpleNamespace(manifest=manifest, enabled=True,
                                    activation=SimpleNamespace(activation_id="act-combination"))]
    harness.capabilities = sample.handle({"jsonrpc": "2.0", "id": 1,
                                          "method": "initialize"})["result"]["capabilities"]
    harness.responses = []

    # LLM: 决定由真实样例协议产出，测试只收集回复，不手填宿主决定或账本。
    # 函数用途: 转发一次隔离门征询并保存样例回执。
    def answer(_row, params, _timeout):
        result = sample.handle({"jsonrpc": "2.0", "id": 2,
            "method": "my-agent/tool-gate.review", "params": params})["result"]
        harness.responses.append(result)
        return result

    harness.answer = answer
    return harness


# LLM: 共用执行链和B4观察保持真实；只替换激活表/通道，不手插决定账本或越过原handler。
# 函数用途: 执行一次临时补丁并记录handler和事件，结束后关闭本用例的假通道池。
@pytest.fixture
def delete_review(tmp_path, monkeypatch, request):
    service, manifest, sample = _installed_sample(tmp_path)
    harness = _sample_gate_harness(service, manifest, sample)
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "obsolete.txt"
    target.write_text("必须保留\n", encoding="utf-8")
    registry = ToolRegistry(ToolRegistryParams(root, 100, 10, 10, 100, 1, 10, 10, False,
        operation_store_required=False, approval_mode_reader=lambda: "auto",
        plugin_gate_reviewer=harness.reviewer.review))
    snapshot = registry.runtime_snapshot(run_id="run-combination")
    patch_tool = snapshot.runtime("apply_patch").handler
    executed, events = [], []
    original = patch_tool.execute

    # LLM: 记录真正进入handler的参数后调用原实现，拒绝分支不应触达这里。
    # 函数用途: 给组合用例留下实际handler执行证据。
    def execute(arguments):
        executed.append(dict(arguments))
        return original(arguments)

    monkeypatch.setattr(patch_tool, "execute", execute)
    patch = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch\n"
    if getattr(request, "param", "delete") == "update":
        patch = "*** Begin Patch\n*** Update File: obsolete.txt\n-必须保留\n+通过更新\n*** End Patch\n"
    call = canonical_test_call(snapshot, "apply_patch", {"patch": patch})
    context = EventPointContext(SimpleNamespace(plugin_events_enabled=True), events.append,
                                "local", "isolated-thread", "main")
    execution = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot,
        trusted_run_context={"plugin_event_context": context, "interactive": False})
    archive = _persist_execution(_ledger_host(service, root), execution)
    try:
        yield SimpleNamespace(service=service, harness=harness, call=call, execution=execution,
                              executed=executed, events=events, target=target, archive=archive)
    finally:
        harness.pool.close()


# LLM: 结构化拒绝、handler执行与事件共同反证，不能只断言错误码或靠关闭观察开关过关。
# 函数用途: 核对样例已经拒绝且文件与真实工具观察都没有被触碰。
def _assert_denied_before_handler(review):
    execution = review.execution
    assert execution.decision.status == "deny"
    assert execution.result.error_code == "PLUGIN_GATE_DENIED"
    assert execution.result.handler_executed is False
    assert execution.result.effect_outcome == "not_started"
    assert review.executed == []
    assert review.events == []  # 无 started，也无 finished；观察开关已开，不能靠禁用伪造零事件。
    assert review.target.read_text(encoding="utf-8") == "必须保留\n"
    assert len(review.harness.sent) == 1
    sent = review.harness.sent[0][2]
    assert sent["gate_id"] == "guard-delete"
    assert sent["call"]["tool"] == "apply_patch"
    # plugin_requirements 是待用户确认的门集合，不含直接 deny；原协议回执才是这里的裁决观察点。
    assert review.harness.responses == [{"verdict": "deny", "reason_code": "DELETE_FILE_BLOCKED",
                                         "message": "补丁要删除文件，拒绝"}]


def test_rm_guard_delete_rejected_without_handler_or_tool_events(delete_review):
    _assert_denied_before_handler(delete_review)


# LLM: 允许正对照必须真实写文件并发成对B4事件，再核原链写出的allow决定，不能靠观察关闭形成假绿。
# 函数用途: 核对门放行后的真实handler、工具事件与决定写账。
@pytest.mark.parametrize("delete_review", ["update"], indirect=True)
def test_allowed_update_runs_real_handler_and_emits_both_tool_events(delete_review):
    assert delete_review.execution.result.ok
    assert delete_review.execution.result.handler_executed is True
    assert len(delete_review.executed) == 1
    assert [event.type for event in delete_review.events] == ["tool_call_started", "tool_call_finished"]
    assert delete_review.events[0].facts["call_id"] == delete_review.call.call_id
    assert delete_review.events[1].facts["handler_executed"] is True
    assert delete_review.target.read_text(encoding="utf-8") == "通过更新\n"
    rows = decisions(delete_review.service, plugin_id="rm-guard")
    assert len(rows) == 1 and rows[0]["verdict"] == "allow_as_is"
    assert rows[0]["call_id"] == delete_review.call.call_id


def test_rm_guard_delete_decision_visible_in_real_plugins_info(delete_review):
    _assert_denied_before_handler(delete_review)
    rows = decisions(delete_review.service, plugin_id="rm-guard")
    assert len(rows) == 1
    assert rows[0]["call_id"] == delete_review.call.call_id
    assert rows[0]["tool"] == "apply_patch"
    assert rows[0]["final_status"] == "deny"
    assert rows[0]["reason_code"] == "DELETE_FILE_BLOCKED"
    assert "原因码 DELETE_FILE_BLOCKED" in info_message(delete_review.service, plugin_id="rm-guard")
