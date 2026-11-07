"""W5 自定义工具：owner 元数据、原生插件生命周期与唯一插件执行门。

工具注册和权限事实仍归 PluginManagement/PluginInstallStore；这里只保存生成包的
定义与 SHA CAS，不创建工具注册表。脚本以 JSON stdin 收参、stdout 返回预览，
仅在已批准的插件子进程内执行，宿主不 import/exec 用户正文。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ..command_arguments import CommandActionSpec
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_installation import PluginInstallationError
from ..plugin_invocation import PluginInvocation, execute_plugin_invocation
from ..plugin_management import _visible_entries
from ..plugin_manifest import PluginPackageError
from ..plugin_package import inspect_plugin_package
from ..plugin_runtime import PluginMCPClient, plugin_tool_name
from ..runtime_db.operations import RuntimeConflictError
from ..runtime_db.repository import RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from ..tooling.input_schema import canonicalize_tool_input_schema, validate_tool_input
from ..tooling.mcp_registration import MCPProxyTool
from . import gap158_w5_mcp_servers as mcp
from . import v13_w11_plugins as plugins
from .v13_store import _page_rows, v13_store_for

if TYPE_CHECKING:
    from ..gateway_parts.business_api_v13 import V13Call

try:
    from .gap158_limits import CUSTOM_TOOLS_MAX_PER_OWNER, CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS
except ImportError:  # W0 尚未合入时仅使用派工明确批准的默认值。
    CUSTOM_TOOLS_MAX_PER_OWNER = 50
    CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS = 5


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _access(call, *, execute=False, admin=False):
    from ..gateway_parts.business_api_v13 import v13_require_access, v13_require_admin

    v13_require_access(call)
    if admin:
        v13_require_admin(call)
    if execute and not ready(call):
        raise HostAPIError("HOST_NOT_READY", 503, message="自定义工具尚未开放。")


def ready(call: V13Call) -> bool:
    """供 V13Action.ready 使用；没有从参数恢复开关的旁路。"""
    return getattr(call.context.host.config, "custom_tools_enabled", False) is True


def _operation(call):
    return "ct-" + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:40]


# LLM: reserve 失败仍可确定未开始；finish 位于原生副作用之后，持久化不确定时不得降为普通拒绝。
# 函数用途: 复用原 owner 事务，并把收尾存储故障保留为未知结局。
def _transaction(call, stage, mutate):
    try:
        return v13_store_for(call, "owner").transaction(
            "custom_tools", mutate, idempotency_key=_operation(call) + "-" + stage,
            params_digest=_digest([call.params, call.expected_revision]))
    except BusinessError as error:
        if stage == 'finish':
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '实体收尾提交尚未确认。') from None
        status = {"IDEMPOTENCY_CONFLICT": 409, "REVISION_CONFLICT": 409,
                  "RESOURCE_NOT_FOUND": 404, "INVALID_SCHEMA": 400}.get(error.code, 503)
        code = error.code if error.code in {"IDEMPOTENCY_CONFLICT", "REVISION_CONFLICT",
                                           "RESOURCE_NOT_FOUND", "INVALID_SCHEMA"} else "HOST_NOT_READY"
        raise HostAPIError(code, status, message="自定义工具元数据提交未完成。") from None


class _Replay(Exception):
    """事务中的点查结果仅用于原调用重放，不把结果留在无限增长的实体快照中。"""
    def __init__(self, fact):
        self.fact = fact


# LLM: 先耐久 claim 再执行原生动作；unknown 不封成失败回执，也不清除占位或自动重执行。
# 函数用途: 为自定义工具保存同键重放与未结操作的唯一身份。
def _receipted(call, work):
    """同一 owner Store 保存有界在途元数据与逐键终态事实，副作用在原事务锁外执行。"""
    _access(call)
    store = v13_store_for(call, "owner")
    identity = _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])
    fingerprint = _digest([call.action, call.params, call.expected_revision])
    claim = uuid4().hex

    def reserve(metadata, result):
        fact = metadata.fact(identity)
        if fact is not None:
            if fact.get("fingerprint") != fingerprint:
                raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)
            raise _Replay(fact)
        active = metadata.setdefault("custom_tool_operations", {})
        previous = active.get(identity)
        if previous is not None:
            if previous.get("fingerprint") != fingerprint:
                raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原自定义工具操作尚未结清，禁止重复执行。")
        if len(active) >= CUSTOM_TOOLS_MAX_PER_OWNER:
            raise HostAPIError("HOST_NOT_READY", 503, message="当前用户在途工具操作已达容量上限。")
        active[identity] = {"claim": claim, "fingerprint": fingerprint}

    try:
        # 每次探针只点查一个终态；不重写历史结果，也不扫描操作账。
        store.transaction("custom_tools", lambda records: {"claim": claim},
                          idempotency_key="claim-" + claim, params_digest=fingerprint,
                          metadata_update=reserve)
    except _Replay as replay:
        fact = replay.fact
        if fact.get("state") == "completed":
            return fact["result"]
        error = fact["error"]
        raise HostAPIError(error["code"], error["status"], error["outcome"], "原操作已结束。") from None

    def finish(value):
        def seal(metadata, result):
            active = metadata.get("custom_tool_operations", {})
            if active.get(identity) != {"claim": claim, "fingerprint": fingerprint}:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原操作占位已经变化。")
            metadata.seal_fact(identity, {"fingerprint": fingerprint, **value})
            del active[identity]
        try:
            store.transaction("custom_tools", lambda records: {"saved": True},
                              idempotency_key="seal-" + claim, params_digest=fingerprint,
                              metadata_update=seal)
        except Exception:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原操作已有结局，但回执提交尚未确认。") from None

    try:
        result = work()
    except HostAPIError as error:
        if error.outcome == 'unknown':
            raise
        finish({"state": "failed", "error": {"code": error.code, "status": error.status, "outcome": error.outcome}})
        raise
    except Exception:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原自定义工具操作结果尚未确认。") from None
    finish({"state": "completed", "result": result})
    return result


def _definition(params, previous=None):
    value = dict(previous or {})
    value.update({key: params[key] for key in ("name", "description", "language", "entry_script",
                                              "input_schema", "timeout_seconds") if key in params})
    value.setdefault("timeout_seconds", 30)
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["name"]):
            raise ValueError("name")
        if value["language"] not in {"python", "shell"}:
            raise ValueError("language")
        if not isinstance(value["entry_script"], str) or not 0 <= len(value["entry_script"]) <= 200000:
            raise ValueError("script")
        if not isinstance(value["description"], str) or len(value["description"]) > 1000:
            raise ValueError("description")
        if type(value["timeout_seconds"]) is not int or not 1 <= value["timeout_seconds"] <= 600:
            raise ValueError("timeout")
        value["input_schema"] = canonicalize_tool_input_schema(value["input_schema"])
        json.dumps(value, allow_nan=False).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise HostAPIError("INVALID_SCHEMA", message="工具定义不符合插件输入合同。") from None
    return value


def _server_source(definition):
    # 适配器只实现 stdio MCP；用户脚本为独立文件，不能污染 MCP stdout。
    header = "import json, subprocess, sys\nfrom pathlib import Path\n"
    frozen = json.dumps({key: definition[key] for key in (
        "description", "language", "input_schema", "timeout_seconds")},
        ensure_ascii=False, sort_keys=True, allow_nan=False)
    header += "definition = json.loads(" + repr(frozen) + ")\n"
    return (header + '''
root = Path(__file__).resolve().parent
for line in sys.stdin:
    request = None
    try:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "custom-tool", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": "run", "description": definition["description"] or "自定义工具",
                                 "inputSchema": definition["input_schema"]}]}
        elif method == "tools/call":
            if request["params"]["name"] != "run":
                raise ValueError("unknown tool")
            argv = ([sys.executable, "-I", str(root / "entry.py")] if definition["language"] == "python"
                    else ["/bin/sh", str(root / "entry.sh")])
            try:
                completed = subprocess.run(argv, input=json.dumps(request["params"].get("arguments", {})),
                                           text=True, capture_output=True, timeout=definition["timeout_seconds"], check=False)
                text = completed.stdout if completed.returncode == 0 else completed.stderr or completed.stdout
                result = {"content": [{"type": "text", "text": text[:4000]}], "isError": completed.returncode != 0}
            except subprocess.TimeoutExpired:
                result = {"content": [{"type": "text", "text": "自定义工具执行超时。"}], "isError": True}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "Method not found"}}), flush=True)
            continue
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    except (ValueError, TypeError, KeyError):
        response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32602, "message": "Invalid params"}}
    print(json.dumps(response, ensure_ascii=False), flush=True)
''').encode("utf-8")


def _package(plugin_id, definition):
    script = "entry.py" if definition["language"] == "python" else "entry.sh"
    contents = {"server.py": _server_source(definition), script: definition["entry_script"].encode("utf-8")}
    payload = {
        "schema_version": "plugin_package.v6", "plugin_id": plugin_id,
        "version": "1." + _digest(definition)[:24], "summary": definition["description"] or "自定义工具",
        "entry": {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"},
        "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "executable": False}
                  for name, raw in sorted(contents.items())], "platforms": ["any"],
        "actions": [asdict(CommandActionSpec("run", "运行自定义工具", kind="tool", target="run"))],
        "default_action": "run",
        "tools": [{"name": "run", "description": definition["description"] or "自定义工具",
                   "requested_effect": "dangerous", "input_schema": definition["input_schema"]}],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "panels": [], "skills": [], "host_api": [],
    }
    contents["plugin.json"] = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    raw = stream.getvalue()
    try:
        inspect_plugin_package(raw)
    except (PluginPackageError, ValueError):
        raise HostAPIError("INVALID_SCHEMA", message="工具定义不能生成有效的原生插件包。") from None
    return raw


def _reserve(call, tool_id, candidate=None, *, deleting=False):
    """在原 owner CAS 锁内查 SHA、配额和并发占位；不持锁执行插件。"""
    def mutate(records):
        previous = records.get(tool_id)
        if previous is not None and previous.get("owner_id") != call.owner_id:
            previous = None
        if candidate is not None:
            if previous is not None:
                raise HostAPIError("STATE_CONFLICT", 409, message="当前用户已有同名自定义工具。")
            if len(records) >= CUSTOM_TOOLS_MAX_PER_OWNER:
                raise HostAPIError("LIMIT_EXCEEDED", message="当前用户的自定义工具数量已达上限。")
            record = candidate
        else:
            if previous is None:
                if deleting:
                    return {"record": None}
                raise HostAPIError("RESOURCE_NOT_FOUND", 404, message="未找到当前用户的自定义工具。")
            if previous.get("_busy"):
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "该工具已有未结操作，请先核对原操作。")
            expected = call.params.get("expected_sha256")
            if expected is not None and (re.fullmatch(r"[0-9a-f]{64}", expected) is None
                                         or expected != previous["sha256"]):
                raise HostAPIError("REVISION_CONFLICT", 409, message="自定义工具内容已经变化。")
            record = previous
        records[tool_id] = {**record, "_busy": _operation(call)}
        return {"record": record}
    return _transaction(call, "reserve", mutate)["record"]


def _finish(call, tool_id, record):
    def mutate(records):
        current = records.get(tool_id)
        if current is None or current.get("_busy") != _operation(call):
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原工具占位已变化，不能覆盖。")
        if record is None:
            del records[tool_id]
        else:
            records[tool_id] = {key: value for key, value in record.items() if key != "_busy"}
        return {"saved": True}
    return _transaction(call, "finish", mutate)


def _native_entry(manager, record, *, conflict="REVISION_CONFLICT"):
    entry = next((item for item in manager.installations.snapshot()
                  if item.manifest.plugin_id == record["plugin_id"]), None)
    if entry is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, message="当前用户的插件安装已不存在。")
    if entry.package_sha256 != record["sha256"]:
        raise HostAPIError(conflict, 409, message="原生插件内容与自定义工具元数据不一致。")
    return entry


def _view(record, entry):
    state = "installed" if entry.enabled else ("pending_authorization" if record.get("pending_authorization") else "disabled")
    return {"tool_id": record["tool_id"], "name": record["definition"]["name"],
            "language": record["definition"]["language"], "plugin_id": record["plugin_id"],
            "state": state, "sha256": entry.package_sha256}


# LLM: 原安装及同次目录修订进入原管理命令；读回变更须属于该 HostCommand 提交，不能接受同包重装。
# 函数用途: 固定启停和卸载的目标，保留原审批/CAS，并把提交后读回故障留为未知。
def _lifecycle(call, record, action, suffix, *, expected):
    _access(call, execute=True)
    manager = plugins.management(call)
    try:
        entries = manager.installations.snapshot()
        current = next((item for item in entries if item.manifest.plugin_id == record['plugin_id']), None)
        if current is None or _installation_stamp(current) != _installation_stamp(expected):
            raise HostAPIError('REVISION_CONFLICT', 409, message='原生插件安装已变化。')
        selected_stamp = _installation_stamp(current)
        revision = manager._catalog(_visible_entries(manager, entries)).revision
        command = 'remove' if action == 'uninstall' else action
        request_id = _operation(call) + '-' + suffix + '-0'
        outcome = manager.command('/plugins ' + command + ' ' + shlex.quote(record['plugin_id']),
                                  revision=revision, request_id=request_id) or {}
    except PluginInstallationError as error:
        raise _installation_error(call, error) from None
    except RuntimeConflictError:
        raise HostAPIError("IDEMPOTENCY_CONFLICT", 409, message="原生插件请求编号发生冲突。") from None
    if isinstance(outcome, dict) and (outcome.get("state") == "outcome_unknown"
                                      or outcome.get("effect_outcome") == "unknown"):
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原生插件操作结果尚未确认。")
    if outcome.get('error_code') == 'PLUGIN_CATALOG_STALE':
        raise HostAPIError('REVISION_CONFLICT', 409, message='原生插件目录已经变化。')
    try:
        current = next((item for item in manager.installations.snapshot()
                        if item.manifest.plugin_id == record['plugin_id']), None)
    except Exception:
        # 已调用原执行门，读表失败不能借异常的默认 not_committed 断言副作用未发生。
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '原生提交后的安装事实尚不可读。') from None
    if outcome.get('state') == 'succeeded':
        if ((action == 'uninstall' and current is not None)
                or (action != 'uninstall' and (current is None or current.package_sha256 != record['sha256']
                    or current.enabled is not (action == 'enable')))):
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '原生提交后的安装事实已经变化。')
    if action != 'uninstall' and current is not None and _installation_stamp(current) != selected_stamp:
        # 启停合法改变引用，但即使本次只返回待审批，也不能接受另一请求重装的安装。
        thread = manager._thread(create=False)
        commit = getattr(current, 'last_commit', None)
        if (thread is None or getattr(commit, 'operation_id', None)
                != manager._identity(thread.thread_id, request_id).operation_id):
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '读回安装不属于本次原生命令提交。')
    return current, outcome or {}


# LLM: 绑定重装引用、修订及激活代次，不能只按可重复的 plugin_id 或包 SHA 认定旧请求目标。
# 函数用途: 提取原安装的不可变目标身份。
def _installation_stamp(entry):
    return (entry.installation_ref, entry.revision, entry.package_sha256, entry.enabled, entry.activation_id)


def _installation_error(call, error):
    mapped = plugins._installation_error(error)
    if call.action == "custom_tools.create" and mapped.code in {"REVISION_CONFLICT", "RESOURCE_NOT_FOUND"}:
        return HostAPIError("STATE_CONFLICT", 409, mapped.outcome, "原生插件身份已经存在或安装状态已变化。")
    if mapped.code == "STATE_CONFLICT" and call.action in {"custom_tools.update", "custom_tools.delete"}:
        return HostAPIError("HOST_NOT_READY", 503, mapped.outcome, "原生插件尚未处于可变更状态。")
    return mapped


def _pending(outcome):
    return (outcome.get("state") in {"approval_required", "confirmation_required"}
            or outcome.get("error_code") == "PLUGIN_CONFIRMATION_REQUIRED")


# LLM: 安装成功后的启用拒绝不撤销已写包；错误必须保留 committed，未知继续留原占位。
# 函数用途: 沿原安装与启用流程生成可重放回执，准确标明部分完成的副作用。
def _install_enable(call, record, raw, expected_revision, *, before=None):
    try:
        _access(call, execute=True)
        result = plugins.management(call).install_package_bytes(
            raw, operation_id=_operation(call) + "-install", expected_revision=expected_revision)
    except PluginInstallationError as error:
        if error.commit_state == "not_committed":
            _finish(call, record["tool_id"], before)
        elif error.commit_state == "committed":
            committed = {**record, "sha256": hashlib.sha256(raw).hexdigest(), "pending_authorization": False}
            _native_entry(plugins.management(call), committed)
            _finish(call, record["tool_id"], committed)
        raise _installation_error(call, error) from None
    except HostAPIError as error:
        if error.outcome == "not_started":
            _finish(call, record["tool_id"], before)
        raise
    entry = result.installation
    updated = {**record, "sha256": entry.package_sha256, "pending_authorization": False}
    try:
        current, outcome = _lifecycle(call, updated, "enable", "enable", expected=entry)
    except HostAPIError as error:
        # 包已写入时保留真实内容；未知仍留占位，不假报失败前的旧版本。
        if error.outcome != "unknown":
            _finish(call, record["tool_id"], updated)
            if error.outcome == "not_started":
                raise HostAPIError(error.code, error.status, "committed", error.message) from None
        raise
    if current is None:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "插件安装后原生记录不可见。")
    updated["pending_authorization"] = not current.enabled and _pending(outcome)
    _finish(call, record["tool_id"], updated)
    return {"tool": _view(updated, current)}


def create(call: V13Call) -> dict:
    _access(call, execute=True)
    definition = _definition(call.params)
    if not definition["entry_script"] or not definition["description"]:
        raise HostAPIError("INVALID_SCHEMA", message="创建工具时脚本与说明不能为空。")
    tool_id = "custom-" + _digest([call.owner_id, definition["name"]])[:24]
    raw = _package(tool_id, definition)
    candidate = {"tool_id": tool_id, "owner_id": call.owner_id, "plugin_id": tool_id,
                 "definition": definition, "sha256": hashlib.sha256(raw).hexdigest(),
                 "pending_authorization": False}

    def work():
        _reserve(call, tool_id, candidate)
        return _install_enable(call, candidate, raw, 0)
    return _receipted(call, work)


# LLM: 已确认停用本身就是副作用；后续安装拒绝不能把整次更新标成未开始。
# 函数用途: 按 SHA 和原生安装修订更新工具，保留停用后失败的真实结局。
def update(call: V13Call) -> dict:
    _access(call, execute=True)
    tool_id = call.params["tool_id"]

    def work():
        record = _reserve(call, tool_id)
        changed = False
        disabled = False
        try:
            definition = _definition(call.params, record["definition"])
            raw = _package(record["plugin_id"], definition)
            current = _native_entry(plugins.management(call), record)
            if current.enabled:
                current, outcome = _lifecycle(call, record, "disable", "disable", expected=current)
                disabled = current is not None and not current.enabled
                if current is None or current.enabled or outcome.get("state") != "succeeded":
                    raise HostAPIError("HOST_NOT_READY", 503, message="请先完成原生插件停用确认；尚未替换包。")
            changed = True
            return _install_enable(call, {**record, "definition": definition}, raw, current.revision, before=record)
        except HostAPIError as error:
            if not changed and error.outcome != "unknown":
                _finish(call, tool_id, record)
            if disabled and error.outcome == "not_started":
                raise HostAPIError(error.code, error.status, "committed", error.message) from None
            raise
    return _receipted(call, work)


# LLM: 停用已完成而卸载失败时仍有已提交变化；重送只重放原结局，不再次卸载。
# 函数用途: 经原插件停用和卸载删除自定义工具，并保留部分完成事实。
def delete(call: V13Call) -> dict:
    _access(call, execute=True)
    tool_id = call.params["tool_id"]

    def work():
        record = _reserve(call, tool_id, deleting=True)
        if record is None:
            return {"deleted": False}
        disabled = False
        try:
            current = _native_entry(plugins.management(call), record)
            if current.enabled:
                current, outcome = _lifecycle(call, record, "disable", "disable", expected=current)
                disabled = current is not None and not current.enabled
                if current is None or current.enabled or outcome.get("state") != "succeeded":
                    raise HostAPIError("HOST_NOT_READY", 503, message="请先完成原生插件停用确认；尚未删除。")
            current, outcome = _lifecycle(call, record, "uninstall", "remove", expected=current)
            if current is not None or outcome.get("state") != "succeeded":
                raise HostAPIError("HOST_NOT_READY", 503, message="原生插件删除尚未确认。")
        except HostAPIError as error:
            if error.outcome != "unknown":
                _finish(call, tool_id, record)
            if disabled and error.outcome == "not_started":
                raise HostAPIError(error.code, error.status, "committed", error.message) from None
            raise
        _finish(call, tool_id, None)
        return {"deleted": True}
    return _receipted(call, work)


def test(call: V13Call) -> dict:
    """执行一次已安装工具；保留原 HostCommand、审批、沙箱、资源释放和工具账。"""
    _access(call, execute=True)
    tool_id = call.params["tool_id"]

    def work():
        record = _reserve(call, tool_id)
        try:
            manager = plugins.management(call)
            installed = _native_entry(manager, record, conflict="STATE_CONFLICT")
            if not installed.enabled:
                raise HostAPIError("STATE_CONFLICT", 409, message="工具尚未通过原生插件启用流程。")
            name = plugin_tool_name(record["plugin_id"], "run")
            arguments = json.loads(json.dumps(call.params["args"], ensure_ascii=False, allow_nan=False))
            if not validate_tool_input(arguments, installed.manifest.tools[0].input_schema).ok:
                raise HostAPIError("INVALID_SCHEMA", message="工具测试参数不符合已安装 schema。")

            def select():
                _access(call, execute=True)
                live = plugins.management(call)
                entry = _native_entry(live, record, conflict="STATE_CONFLICT")
                if (not live.context.enabled or not live.context.business_allowed
                        or name in live.context.disabled_tools):
                    raise HostAPIError("BUSINESS_ACCESS_DENIED", 403, message="原生插件策略拒绝工具执行。")
                if not entry.enabled or entry.activation_id != installed.activation_id:
                    raise HostAPIError("STATE_CONFLICT", 409, message="工具激活代次已经变化。")
                if live._business_sandbox_problem(entry):
                    raise HostAPIError("HOST_NOT_READY", 503, message="原生插件运行时或沙箱尚未就绪。")
                return PluginInvocation(entry, name, arguments)

            select()  # 先核策略，拒绝不创建运行或连接。
            thread = manager._thread(create=True)
            repo = RuntimeRepository(runtime_db_path(manager.context.owner.home_dir))
            request = manager._request(thread.thread_id, _operation(call) + "-test", name, arguments)
            started = time.monotonic()
            outcome = execute_plugin_invocation(manager.context, repo, request, select)
            duration = max(0, int((time.monotonic() - started) * 1000))
            if outcome.get("state") in {"outcome_unknown", "running", "pending"}:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原插件测试尚未确认结束，不能重复执行。")
            preview = outcome.get("output", "")
            if not isinstance(preview, str):
                preview = json.dumps(preview, ensure_ascii=False, allow_nan=False)
            result = {"ok": outcome.get("state") == "succeeded" and outcome.get("ok") is True, "duration_ms": duration,
                      "result_preview": preview[:4000], "error_code": str(outcome.get("error_code") or "")[:64]}
        except HostAPIError as error:
            if error.outcome != "unknown":
                _finish(call, tool_id, record)
            raise
        except (ValueError, TypeError):
            _finish(call, tool_id, record)
            raise HostAPIError("INVALID_SCHEMA", message="工具测试参数不符合已安装 schema。") from None
        _finish(call, tool_id, record)
        return result
    return _receipted(call, work)


def _catalog(call):
    with mcp.live_registry(call) as registry:
        return _catalog_rows(call, registry)


def _catalog_rows(call, registry):
    snapshot = registry.runtime_snapshot()
    records = v13_store_for(call, "owner").list("custom_tools")
    own_plugins = {row["plugin_id"]: row for row in records if row.get("owner_id") == call.owner_id}
    installed_custom = {}
    if own_plugins:
        installed_custom = {entry.manifest.plugin_id: entry for entry in plugins.management(call).installations.snapshot()
                            if entry.manifest.plugin_id in own_plugins
                            and entry.package_sha256 == own_plugins[entry.manifest.plugin_id]["sha256"]}
    categories = registry.catalog_categories
    deferred = registry.catalog_deferred_categories
    rows = {}
    for spec in snapshot.catalog_specs:
        provider = spec.hints.provider_id
        tool = registry.tools.get(spec.name)
        if provider.startswith("plugin:"):
            source = "custom" if provider.removeprefix("plugin:") in own_plugins else "plugin"
        elif isinstance(tool, MCPProxyTool):
            source = "plugin" if isinstance(tool.client, PluginMCPClient) else "mcp"
        else:
            source = "builtin"
        hidden = spec.category in deferred or spec.hints.default_deferred
        rows[spec.name] = {"tool": spec.name, "source": source, "category": spec.category,
                           "deferred": bool(hidden), "visible": bool(spec.name in snapshot.available_tool_names
                               and (not categories or spec.category in categories) and not hidden)}
    # 尚未获准的自定义工具没有可调用代理；静态安装事实可列出，但不能宣称可见或可执行。
    for record in own_plugins.values():
        name = plugin_tool_name(record["plugin_id"], "run")
        if name not in rows and record["plugin_id"] in installed_custom:
            rows[name] = {"tool": name, "source": "custom", "category": "plugins",
                          "deferred": "plugins" in deferred, "visible": False}
    return sorted(rows.values(), key=lambda row: row["tool"])


def list_tools(call: V13Call) -> dict:
    _access(call)
    rows = _catalog(call)
    source = call.params.get("source")
    if source is not None:
        rows = [row for row in rows if row["source"] == source]
    result = _page_rows(rows, [call.owner_id, call.subject_id, "custom_tools", source, rows],
                        cursor=call.params.get("cursor"), limit=call.params.get("limit", 50))
    return {**result, "next_cursor": result["next_cursor"] or ""}


def _category_change(call, resetting=False):
    from ..settings.config import AgentConfig
    from ..settings.parameter_changes import ChangeOrigin, WritePaths, set_parameters

    _access(call, admin=True)

    def apply(registry):
        _access(call, admin=True)
        selected = next((row for row in _catalog_rows(call, registry) if row["tool"] == call.params["tool"]), None)
        if selected is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404, message="当前 owner 的真实目录没有该工具。")
        category = selected["category"]
        requested = call.params.get("category")
        if requested is not None and requested not in {"", category}:
            raise HostAPIError("INVALID_SCHEMA", message="分类必须与工具真实分类一致，不能重命名单个工具。")
        path, revision, current = mcp.config_snapshot(call)
        expected = call.params.get("expected_config_revision")
        if expected is not None and expected != revision:
            raise HostAPIError("REVISION_CONFLICT", 409, message="配置版本已经变化。")
        values = {"tool_catalog_categories": list(current.tool_catalog_categories),
                  "tool_catalog_deferred_categories": list(current.tool_catalog_deferred_categories)}
        if resetting:
            defaults = AgentConfig()
            for key in values:
                values[key] = [item for item in values[key] if item != category]
                if category in getattr(defaults, key):
                    values[key].append(category)
        else:
            # 分类列表为空沿原义表示全部；空 category 仅撤销本类过滤，不产生单工具覆盖表。
            if requested is not None:
                values["tool_catalog_categories"] = [item for item in values["tool_catalog_categories"] if item != category]
                if requested:
                    values["tool_catalog_categories"].append(category)
            if "deferred" in call.params:
                values["tool_catalog_deferred_categories"] = [
                    item for item in values["tool_catalog_deferred_categories"] if item != category]
                if call.params["deferred"]:
                    values["tool_catalog_deferred_categories"].append(category)
        _access(call, admin=True)
        report = set_parameters(values, paths=WritePaths(path, expected_revision=revision),
                                origin=ChangeOrigin("client_business", call.action))
        saved_revision = mcp.config_result(report)
        for key, value in values.items():
            setattr(call.context.host.config, key, list(value))
        registry.catalog_categories = list(values["tool_catalog_categories"])
        registry.catalog_deferred_categories = list(values["tool_catalog_deferred_categories"])
        selected = next(row for row in _catalog_rows(call, registry) if row["tool"] == call.params["tool"])
        return {"row": selected, "config_revision": saved_revision}

    def work():
        with mcp.live_registry(call) as registry:
            # 与原 registry 准备入口共用锁，避免两个配置回执乱序应用到同一活宿主。
            with registry._mcp_prepare_lock:
                return apply(registry)
    return _receipted(call, work)


def configure(call: V13Call) -> dict:
    return _category_change(call)


def reset(call: V13Call) -> dict:
    return _category_change(call, resetting=True)


def python_detect(call: V13Call) -> dict:
    _access(call)
    candidates = [sys.executable]
    for name in ("python3", "python3.11", "python3.11t", "python3.12", "python3.12t",
                 "python3.13", "python3.13t", "python3.14", "python3.14t"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    probe = ("import json,platform,sysconfig; print(json.dumps({"
             "'version':platform.python_version(),'implementation':platform.python_implementation(),"
             "'free_threaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))")
    items, seen = [], set()
    for candidate in candidates:
        resolved = str(Path(candidate).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            completed = subprocess.run([resolved, "-I", "-c", probe], capture_output=True, text=True,
                                       timeout=CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS, check=False)
            if completed.returncode or len(completed.stdout) > 4096:
                continue
            result = json.loads(completed.stdout)
            if (not isinstance(result.get("version"), str) or not 1 <= len(result["version"]) <= 32
                    or not isinstance(result.get("implementation"), str) or len(result["implementation"]) > 32
                    or type(result.get("free_threaded")) is not bool):
                continue
        except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
            continue
        items.append({"path": resolved, "version": result["version"],
                      "implementation": result["implementation"], "free_threaded": result["free_threaded"]})
    return {"interpreters": items}


HANDLERS = {
    "custom_tools.list": list_tools,
    "custom_tools.configure": configure,
    "custom_tools.reset": reset,
    "custom_tools.create": create,
    "custom_tools.update": update,
    "custom_tools.delete": delete,
    "custom_tools.test": test,
    "custom_tools.python_detect": python_detect,
}
