# LLM: 夹具包使用实际 wheel、venv、原管理/工具执行器与 MCP；脚本只实现测试用文件读取，不证明产品 TUI 或模型效果。
# 模块用途: 在临时 owner 中贯通真实启用组件，所有输入和进程仅属于各自测试。

import io
import json
import zipfile
from dataclasses import replace

from agent_py_agent.agent.runtime_db.host_command_execution import execute_host_command
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, tool_arguments_hash
from agent_py_agent.tests.plugin_wheel_fixtures import make_wheel
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_package import _manifest

_SERVER = '''import json, os, sys
from pathlib import Path
tools = __TOOLS__
settings = json.loads(os.environ.get("MY_AGENT_PLUGIN_SETTINGS", "{}"))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        value = Path(request["params"]["arguments"]["path"]).read_text()
        result = {"content": [{"type": "text", "text": value}],
                  "structuredContent": {"isolated": sys.prefix != sys.base_prefix,
                                        "configured": bool(settings.get("note"))}}
    else:
        raise RuntimeError("unsupported fixture method")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


# LLM: 包只写测试临时目录；tools_override 可提供整个坏目录，不通过产品专项分支影响行为。
# 函数用途: 生成可实际启用的最小 Python 包，经原管理命令完成安装和可选配置。
def installed_runtime_plugin(tmp_path, *, tools_override=None, module_source=None, configure=False):
    service, source = manager(tmp_path)
    manifest = _manifest(b"")
    declared = [{"name": item["name"], "description": item["description"], "inputSchema": item["input_schema"]}
                for item in manifest["tools"]]
    code = _SERVER.replace("__TOOLS__", repr(declared if tools_override is None else tools_override))
    wheel = make_wheel(files={"peek/__init__.py": b"", "peek/__main__.py": (module_source or code).encode()})
    manifest = _manifest(wheel[1])
    if configure:
        manifest["settings_schema"] = {"type": "object", "properties": {"note": {"type": "string"}},
                                       "required": ["note"], "additionalProperties": False}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr(wheel[0], wheel[1])
    source.write_bytes(data.getvalue())
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result
    if configure:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"note": "private-settings-value"}))
        result = service.command(f'/plugins configure sample-peek --file "{settings}"',
                                 revision=service.catalog().revision, request_id="configure")
        assert result["state"] == "succeeded", result
    return service


# LLM: 只用现有 RegistryParams 注入明确临时 owner，不创建模型/Gateway；构造后应仍无插件进程。
# 函数用途: 为插件新运行组合提供真实核心工具与原执行器依赖。
def plugin_registry(service, **changes):
    params = dict(workspace_root=service.context.workspace, max_chars=10000, max_entries=100,
                  max_matches=100, web_max_chars=10000, http_timeout=3, catalog_limit=100,
                  retrieval_limit=10, vector_search_enabled=False, plugin_owner=service.context.owner)
    return ToolRegistry(ToolRegistryParams(**(params | changes)))


# LLM: 业务调用仍经原 RuntimeDB/HostCommand/ToolExecutor，测试宿主明确使用自动批准模式；不直接调用 proxy 掩盖权限链。
# 函数用途: 在独立真实运行中调用固定 registry 快照，并保留原工具账供验收。
def invoke_registered_tool(service, registry, name, arguments, *, request_id="call", snapshot=None):
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    request = HostCommandRequest(service.context.owner.owner_id, "tester", "test", "calls", request_id,
                                 name, tool_arguments_hash(arguments).removeprefix("sha256:"))

    # LLM: 每次调用冻结原工具或明确旧快照；审批模式仅来自测试宿主，不修改插件自述 effect。
    # 函数用途: 将当前业务请求适配到唯一执行器。
    def prepare(binding):
        selected = (replace(snapshot, run_id=binding.run_id, snapshot_hash="")
                    if snapshot else registry.runtime_snapshot(run_id=binding.run_id))
        tool = next(runtime for runtime in selected.runtimes if runtime.model_spec.name == name)
        call = ToolCall(request_id, name, arguments, "native", tool.model_spec.schema_hash,
                        binding.run_id, request_id, binding.attempt_id, operation_id=request.operation_id)
        return ToolExecutorRequest(call, selected, service.context.workspace, approval_mode="auto",
                                   operation_owner_id=service.context.owner.owner_id)

    result = execute_host_command(repo, request, prepare)
    binding = repo.find_host_command(request)
    record = ManagedOperationStore(repo).get_tool_operation(
        owner_id=request.owner_id, run_id=binding.run_id, task_id=binding.task_id,
        attempt_id=binding.attempt_id, operation_id=request.operation_id,
        tool_name=name, args_hash="sha256:" + request.input_digest,
    )
    return result | {"stored_output": record.result.get("output", "") if record else ""}
