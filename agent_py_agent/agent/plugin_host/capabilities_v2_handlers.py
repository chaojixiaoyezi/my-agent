# LLM: WP-4沿原A0适配领域；新写和回执重放都须先核当前对象范围，复用原控制账，不创建影子状态库。
# 模块用途: 集中接通目录、分叉、数据源、提炼、录制和后台进程动作，保持组合根轻量。
from __future__ import annotations

import importlib
import time
from dataclasses import replace

from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import read_bytes_beneath
from ..gateway_parts import control_operation_service as controls
from ..gateway_parts.control_service import GatewayControlScope
from ..gateway_parts.io import write_json_file_atomic
from ..user_space.owner_resolver import OwnerIdentity
from .action_specs import action_spec, required_capabilities
from .action_specs_v2_ext import V2_ACTIONS
from .authorization import workspace_directory
from .contracts import DomainResult
from .conversations import digest, read_context, thread_visible, thread_workspace
from .errors import HostAPIError
from .observations import _has_agents, process_visible, workspace_asset_rows, workspace_snapshot
from .schemas import canonical_json, thaw


# LLM: 仅公开包卡字段，不复制安装路径/reader；旧manifest缺应用声明时保留空映射。
# 函数用途: 给W4包列表/详情委派提供含应用范围的公共投影。
def _package_dict(record):
    cap = getattr(record, "capability", None) or getattr(getattr(record, "manifest", None), "capability", None)
    return {"package_id": getattr(record, "package_id", ""), "name": getattr(record, "name", ""),
            "enabled": bool(getattr(record, "enabled", False)),
            "application_scope": dict(getattr(cap, "application_scope", None) or {})}


# LLM: 不从payload接受身份，也不以包/管理员安装身份代替有效owner；鲜活授权仍唯一归原解析器。
# 函数用途: 向W4身份接口委派原owner解析入口。
def user_space_identity_resolve(authorized):
    return read_context(authorized)

# LLM: 不resolve或清洗相对路径；任意大小写.git组件都以403拒绝，不能作为文件浏览入口。
# 函数用途: 为W5文件委派拆出安全非空相对组件。
def _parts(value):
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value or "\\" in value:
        raise HostAPIError("WORKSPACE_PATH_INVALID")
    parts = tuple(value.split("/"))
    if any(part.lower() == ".git" for part in parts):
        raise HostAPIError("PATH_OUTSIDE_WORKSPACE", 403)
    if any(part in {"", ".", ".."} for part in parts):
        raise HostAPIError("WORKSPACE_PATH_INVALID")
    return parts


# LLM: 导入地址仅来自本模块静态表；只对准确缺模块返回未就绪，不吞掉模块内部依赖错误。
# 函数用途: 装入一个已经固定在产品代码中的领域实现。
def _module(name):
    try:
        return importlib.import_module("agent_py_agent.agent." + name)
    except ImportError:
        raise HostAPIError("HOST_NOT_READY", 503) from None


# LLM: 投递/治理也逐项核真实公开方法；缺领域明确不可用，不因登记动作而宣称实现完成。
# 函数用途: 核对新增动作在本组合根是否已经接齐领域。
def capabilities_v2_ready(action, ports):
    if action not in V2_ACTIONS or not callable(getattr(ports.conversation, "store_resolver", None)):
        return False
    if action_spec(action).requires_idempotency and not callable(getattr(getattr(ports.observations, "services", None), "once", None)):
        return False
    from .governance_delivery_v2_handlers import (
        DELIVERY_GOVERNANCE_ACTIONS,
        governance_delivery_ready,
    )
    if action in DELIVERY_GOVERNANCE_ACTIONS:
        return governance_delivery_ready(action)
    dependencies = {
        "assets.workspace.promote": ("assets.workspace_assets", "promote_to_owner"),
        "conversations.threads.fork": ("conversation.thread_fork_service", "fork_conversation_thread"),
        "conversations.turns.search": ("assets.turn_extractor", "search_user_turns"),
    }
    if action.startswith("datasources."):
        dependency = ("assets.datasource_store", "DatasourceStore")
    elif action.startswith("assets.extract.from_turns."):
        dependency = ("assets.turn_extractor", {"propose": "propose_assets_from_turns", "get": "get_extraction_proposal",
                                               "commit": "commit_extracted_assets"}[action.rsplit(".", 1)[1]])
    elif action.startswith("recorder."):
        dependency = ("assets.browser_recorder", {"start": "start_recording_session", "stop": "stop_recording_session",
                     "get": "get_recording_session", "compile": "compile_recording_to_script"}[action.rsplit(".", 1)[1]])
    elif action.startswith("processes."):
        dependency = ("conversation.background_process_view", "list_background_processes")
    else:
        dependency = dependencies.get(action)
    if dependency is None:
        return True
    try:
        return callable(getattr(_module(dependency[0]), dependency[1], None))
    except HostAPIError:
        return False


# LLM: 直接调用也复核真实来源、AND能力和闭合参数；固定预检在原写锁内检查新写/重放对象，不缓存权限。
# 函数用途: 在原owner和线程域上执行一个新增动作。
def call_capabilities_v2(authorized, ports):
    action = authorized.call.action
    if action not in V2_ACTIONS:
        raise HostAPIError("UNSUPPORTED_ACTION")
    context = user_space_identity_resolve(authorized)
    if not required_capabilities(action, authorized.call.payload) <= authorized.binding.capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    payload = action_spec(action).validate_payload(thaw(authorized.call.payload))
    if "workspace_dir" in payload:
        workspace_directory(context, payload["workspace_dir"])
    conversation = ports.conversation
    store = conversation.store_resolver(context)
    # LLM: 闭包只引用本次已核对象，不缓存来源或把payload转成可执行回调。
    # 函数用途: 在原幂等门内执行本次固定动作。
    def effect():
        return _effect(action, payload, authorized, context, ports, store)
    # LLM: 只读原对象，不执行effect；saved来自原控制账，检查分叉目标时不得把旧结果当授权。
    # 函数用途: 在本次写入或回执重放前核对当前资源范围。
    def preflight(saved):
        _preflight_resource_scope(action, payload, authorized, context, conversation, store, saved)
    if action_spec(action).requires_idempotency:
        return _once(authorized, context, ports.observations.services, effect, preflight)
    return effect()


# LLM: 原锁内先保留冲突/未完成unknown，再为新写和完成重放核当前对象范围；正文只保存摘要，不缓存权限。
# 函数用途: 串行执行写入并安全重放，不让持久回执变成永久资源访问票据。
def _once(authorized, context, services, effect, preflight):
    message_id = digest([authorized.binding.installation_ref, context.owner_home.owner_id,
                         authorized.call.action, authorized.call.idempotency_key])
    scope = GatewayControlScope(user_id=context.owner_home.owner_id, channel="plugin",
        conversation_id=authorized.binding.installation_ref, resolved_owner=context.identity,
        metadata={"message_id": message_id})
    safe_text = canonical_json({"action": authorized.call.action, "payload_sha256": digest(authorized.call.payload)})
    operation_id, fingerprint = controls.gateway_control_operation_identity(scope, safe_text)
    paths = services.resources.paths_for(context)
    with controls.gateway_control_operation_transition(paths, operation_id):
        read_context(authorized)
        prior = controls.read_gateway_control_operation(paths, operation_id)
        if prior and prior.input_digest != fingerprint:
            raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
        if prior and prior.state != "completed":
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        cached = prior.result["details"] if prior else None
        preflight(cached["data"] if cached is not None else None)
        if prior:
            return DomainResult(cached["status"], cached["data"], warnings=tuple(cached.get("warnings", ())))
        operation = controls._new_control_operation_receipt(scope, "plugin_domain", safe_text, operation_id, fingerprint)
        path = controls.gateway_control_operation_path(paths, operation_id)
        write_json_file_atomic(path, replace(operation, state="executing", updated_at=time.time()).to_dict())
        try:
            result = effect()
        except HostAPIError as error:
            if error.outcome == "not_started":
                path.unlink(missing_ok=True)
            raise
        data = redact_sensitive_value(thaw(result.data))
        result = DomainResult(result.http_status, data, warnings=tuple(redact_sensitive_value(list(result.warnings))))
        write_json_file_atomic(path, replace(operation, state="completed", updated_at=time.time(), result={
            "kind": "plugin_domain", "ok": True, "message": "",
            "details": {"status": result.http_status, "data": data, "warnings": list(result.warnings)}}).to_dict())
        return result


# LLM: 显式目录一律核管理员；隐式普通线程只接受精确owner home，旧自定义cwd也不能绕过目录门。
# 函数用途: 得到本次动作明确选择的工作目录，未指定时返回None以保留原领域默认。
def _workspace(payload, authorized, context, conversation, store):
    thread = (conversation.require_thread(store, payload["thread_id"], authorized, context)
              if payload.get("thread_id") else None)
    raw = payload.get("workspace_dir") or (thread_workspace(thread) if thread else None)
    if raw and "workspace_dir" not in payload and context.identity != OwnerIdentity.local_main():
        if raw != str(context.owner_home.home_dir):
            raise HostAPIError("WORKSPACE_DIR_FORBIDDEN_FOR_NON_ADMIN", 403)
        return context.owner_home.home_dir
    return workspace_directory(context, raw) if raw else None


# LLM: 控制锁内重核当前治理目标和提审归属；原线程/目录交集预检保持，禁止业务写入。
# 函数用途: 为新写与幂等重放统一执行无副作用的资源范围预检。
def _preflight_resource_scope(action, payload, authorized, context, conversation, store, saved=None):
    from .governance_delivery_v2_handlers import (
        DELIVERY_GOVERNANCE_ACTIONS,
        preflight_governance_delivery,
    )
    if action in DELIVERY_GOVERNANCE_ACTIONS:
        preflight_governance_delivery(action, payload, authorized, context)
        return
    _workspace(payload, authorized, context, conversation, store)
    if action == "conversations.threads.fork":
        conversation.require_thread(store, payload["source_thread_id"], authorized, context)
        _workspace({"thread_id": payload["source_thread_id"]}, authorized, context, conversation, store)
        if saved is not None and saved.get("thread_id"):
            conversation.require_thread(store, saved["thread_id"], authorized, context)
        return
    if action == "assets.extract.from_turns.propose":
        for ref in payload["turn_refs"]:
            conversation.require_thread(store, ref["thread_id"], authorized, context)
        return
    if action == "assets.extract.from_turns.commit":
        proposal = _proposal(_module("assets.turn_extractor"), payload["proposal_id"], authorized, context, conversation, store)
        if (payload.get("scope") or proposal.get("scope", "owner")) == "workspace":
            raw = payload.get("workspace_dir") or proposal.get("workspace_root")
            if not raw:
                raise HostAPIError("WORKSPACE_DIR_REQUIRED")
            workspace_directory(context, raw)
        return
    if action in {"recorder.sessions.stop", "recorder.sessions.compile"}:
        record = _recording(_module("assets.browser_recorder"), payload["recording_id"], authorized, context, conversation, store)
        if action.endswith(".compile") and payload.get("scope") == "workspace":
            raw = payload.get("workspace_dir") or record.get("workspace_root")
            if not raw:
                raise HostAPIError("WORKSPACE_DIR_REQUIRED")
            workspace_directory(context, raw)


# LLM: 只过滤原store中已证明owner及安装范围的线程；显式不可见ID返回404，禁止搜索后再过滤导致内容泄露。
# 函数用途: 为搜索和资产提炼取得准确的可见线程ID。
def _visible_thread_ids(payload, authorized, context, conversation, store):
    if "thread_ids" in payload:
        return [conversation.require_thread(store, tid, authorized, context).thread_id for tid in payload["thread_ids"]]
    return [row.thread_id for row in conversation.all_threads(store) if thread_visible(row, authorized, context)]


# LLM: 普通用户只冷读固定home，不把默认位置误作显式目录绑定；沿原资产/规则只读口，不修改白名单外投影模块。
# 函数用途: 保留默认工作区查询，同时让管理员的项目查询继续走原快照。
def _workspace_context(payload, authorized, context, conversation, store):
    if context.identity == OwnerIdentity.local_main():
        return workspace_snapshot(authorized, context, conversation, store, payload)
    directory = _workspace(payload, authorized, context, conversation, store) or context.owner_home.home_dir
    thread = (conversation.require_thread(store, payload["thread_id"], authorized, context)
              if payload.get("thread_id") else None)
    rows = workspace_asset_rows(context, directory)
    counts = {kind + "s": sum(row["kind"] == kind for row in rows) for kind in ("skill", "script", "workflow", "monitor")}
    counts["has_agents_md"] = any(_has_agents(directory, parts) for parts in (("AGENTS.md",), (".agent", "AGENTS.md")))
    return {"thread_id": thread.thread_id if thread else None, "cwd": str(directory), "workspace_root": str(directory),
            "runtime_workspace_roots": list(thread.runtime_workspace_roots) if thread else [str(directory)],
            "owner_home": str(context.owner_home.home_dir), "is_admin": False, "overlay_counts": counts}


# LLM: 投递/治理固定适配复用同一幂等账；非HostAPIError仍沿组合根unknown，不公开原异常正文。
# 函数用途: 将新增动作交给已有领域或目录只读投影。
def _effect(action, payload, authorized, context, ports, store):
    from .governance_delivery_v2_handlers import (
        DELIVERY_GOVERNANCE_ACTIONS,
        call_governance_delivery,
    )
    if action in DELIVERY_GOVERNANCE_ACTIONS:
        return call_governance_delivery(action, payload, authorized, context)
    conversation = ports.conversation
    if action == "conversations.threads.workspace.get":
        return DomainResult(200, _workspace_context(payload, authorized, context, conversation, store))
    if action == "conversations.threads.workspace.switch":
        return conversation.mutate(store, "threads.workspace.switch", payload, authorized, context)
    if action == "assets.workspace.list":
        directory = _workspace(payload, authorized, context, conversation, store) or context.owner_home.home_dir
        return DomainResult(200, {"items": workspace_asset_rows(context, directory, payload["kind"]), "workspace_root": str(directory)})
    if action == "assets.workspace.promote":
        directory = _workspace(payload, authorized, context, conversation, store)
        result = _module("assets.workspace_assets").promote_to_owner(payload["asset_kind"], payload["asset_id"],
            workspace_root=directory, owner_home=context.owner_home.home_dir, expected_revision=payload.get("expected_revision"))
        return DomainResult(200, result)
    if action == "conversations.threads.fork":
        return _fork(payload, authorized, context, conversation, store)
    if action.startswith("datasources."):
        return _datasource(action, payload, authorized, context, conversation, store)
    if action == "conversations.turns.search" or action.startswith("assets.extract.from_turns."):
        return _extraction(action, payload, authorized, context, conversation, store)
    if action.startswith("recorder."):
        return _recorder(action, payload, authorized, context, conversation, store)
    if action.startswith("processes.background."):
        return _background(action, payload, authorized, context, ports.observations.services, store)
    raise HostAPIError("UNSUPPORTED_ACTION")


# LLM: 先检查源线程及允许创建范围；克隆后只为新线程固定本安装来源，绝不改源线程或沿用其它安装回执。
# 函数用途: 调用WP-1主会话分叉并将新线程接回原插件范围。
def _fork(payload, authorized, context, conversation, store):
    conversation.require_thread(store, payload["source_thread_id"], authorized, context)
    for scope in (authorized.binding.grant["threads"], authorized.resource_scope["threads"]):
        if scope.get("created_by_installation") is not True:
            raise HostAPIError("CAPABILITY_DENIED", 403)
    directory = _workspace(payload, authorized, context, conversation, store)
    result = _module("conversation.thread_fork_service").fork_conversation_thread(store,
        source_thread_id=payload["source_thread_id"], up_to_message_id=payload.get("up_to_message_id"),
        title=payload.get("title"), workspace_dir=str(directory) if directory else None,
        owner_home=context.owner_home.home_dir, is_admin=context.identity == OwnerIdentity.local_main())

    # LLM: 新线程尚未具备安装来源，先核canonical owner再附加来源；撤权或失败不伪造完整结果。
    # 函数用途: 给已经创建的新线程固定独立修订和安装标记。
    def bind(row):
        read_context(authorized)
        if row.owner_id != context.owner_home.owner_id or row.owner_home != str(context.owner_home.home_dir):
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        state = {"installation_ref": authorized.binding.installation_ref, "revision": 1, "workspace_ref": "default"}
        return replace(row, metadata={**row.metadata, "plugin_host_v2": state})

    branch = store.threads.update_atomic(result["thread_id"], bind)
    return DomainResult(201, {
        "thread_id": branch.thread_id, "title": branch.title,
        "forked_from_thread_id": getattr(branch, "forked_from_thread_id", payload["source_thread_id"]),
        "forked_from_message_id": getattr(branch, "forked_from_message_id", result.get("forked_from_message_id", "")),
        "fork_lineage_depth": getattr(branch, "fork_lineage_depth", 1),
        "message_count": result.get("forked_message_count", result.get("message_count", 0)),
        "boundary_adjusted": result.get("boundary_adjusted", False), "workspace_root": thread_workspace(branch)
    })


# LLM: Store只用已核owner构造，workspace_root由统一目录门提供；不把spec/secret中的字段当宿主身份。
# 函数用途: 显式适配WP-2数据源五个公开接口。
def _datasource(action, payload, authorized, context, conversation, store):
    directory = _workspace(payload, authorized, context, conversation, store)
    backend = _module("assets.datasource_store").DatasourceStore(context.owner_home.home_dir)
    if payload.get("scope") == "workspace" and directory is None:
        raise HostAPIError("WORKSPACE_DIR_REQUIRED")
    if action == "datasources.list":
        rows = backend.list_datasources(workspace_root=directory)
        return DomainResult(200, {"items": rows, "total": len(rows)})
    if action == "datasources.save":
        result = backend.save_datasource(payload["spec"], secret_payload=payload.get("secret"),
            scope=payload.get("scope", "owner"), workspace_root=directory, expected_revision=payload.get("expected_revision"))
    elif action == "datasources.test":
        result = backend.test_datasource(payload["datasource_id"], workspace_root=directory)
    elif action == "datasources.schema.inspect":
        result = backend.inspect_schema(payload["datasource_id"], workspace_root=directory)
    else:
        result = backend.migrate_datasource(payload["source_id"], payload["target_spec"],
            mode=payload["mode"], workspace_root=directory)
    if isinstance(result, dict) and result.get("effect_outcome") == "unknown":
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
    return DomainResult(200, result)


# LLM: 只在原owner规范记录集合确认存在；ID沿原validator，错误/链接不洗成空记录，亦不读取其它owner目录。
# 函数用途: 在提案或录制详情读取前提供准确404和安全ID校验。
def _record_exists(context, collection, identifier):
    from ..assets import safe_asset_id
    try:
        selected = safe_asset_id(identifier)
    except ValueError:
        raise HostAPIError("INVALID_SCHEMA") from None
    if read_bytes_beneath(context.owner_home.home_dir, ("data", collection, selected + ".json"), max_bytes=4 * 1024 * 1024) is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)


# LLM: 提案属于owner仍不等于在本安装线程范围内；预览和提交均核来源线程，不能靠猜proposal_id绕过chat范围。
# 函数用途: 读取当前owner提案并校验全部原始回合引用。
def _proposal(backend, identifier, authorized, context, conversation, store):
    _record_exists(context, "asset_extractions", identifier)
    proposal = backend.get_extraction_proposal(context.owner_home.home_dir, identifier)
    refs = proposal.get("source_turn_refs")
    if not isinstance(refs, (list, tuple)) or not refs:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("thread_id"):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        conversation.require_thread(store, ref["thread_id"], authorized, context)
    return proposal


# LLM: 搜索在调用底座前收紧thread_ids；空授权集合不回退全库，提案与正式发布复用原领域及其CAS。
# 函数用途: 适配WP-3回合检索、草稿生成、读取和选择性提交。
def _extraction(action, payload, authorized, context, conversation, store):
    backend = _module("assets.turn_extractor")
    directory = _workspace(payload, authorized, context, conversation, store)
    home = context.owner_home.home_dir
    if action == "conversations.turns.search":
        thread_ids = _visible_thread_ids(payload, authorized, context, conversation, store)
        if directory:
            thread_ids = [tid for tid in thread_ids if thread_workspace(store.threads.require(tid)) == str(directory)]
        if not thread_ids:
            return DomainResult(200, {"items": [], "total": 0, "next_cursor": None})
        filters = {key: value for key, value in payload.items() if key not in {"workspace_dir", "thread_ids"}}
        result = backend.search_user_turns(store, thread_ids=thread_ids, **filters)
        return DomainResult(200, {**result, "items": [{**row,
            "workspace_root": thread_workspace(store.threads.require(row["thread_id"]))} for row in result["items"]]})
    if action.endswith(".propose"):
        for ref in payload["turn_refs"]:
            conversation.require_thread(store, ref["thread_id"], authorized, context)
        if payload.get("scope") == "workspace" and directory is None:
            raise HostAPIError("WORKSPACE_DIR_REQUIRED")
        result = backend.propose_assets_from_turns(store, owner_home=home, turn_refs=payload["turn_refs"],
            target_types=payload.get("target_types", ("all",)), scope=payload.get("scope", "owner"),
            workspace_root=directory, title_hint=payload["title_hint"])
        return DomainResult(201, result)
    proposal = _proposal(backend, payload["proposal_id"], authorized, context, conversation, store)
    if action.endswith(".get"):
        return DomainResult(200, proposal)
    scope = payload.get("scope") or proposal.get("scope", "owner")
    if scope == "workspace":
        raw = payload.get("workspace_dir") or proposal.get("workspace_root")
        if not raw:
            raise HostAPIError("WORKSPACE_DIR_REQUIRED")
        directory = workspace_directory(context, raw)
    result = backend.commit_extracted_assets(home, proposal_id=payload["proposal_id"],
        selected_assets=payload.get("selected_assets"), scope=scope, workspace_root=directory,
        expected_revision=payload.get("expected_revision"))
    return DomainResult(200, result)


# LLM: 录制只从当前owner原记录读取；有线程绑定时另核安装范围，未绑定录制沿recorder.control的owner范围。
# 函数用途: 在查看或修改录制前检查原记录和关联线程。
def _recording(backend, identifier, authorized, context, conversation, store):
    _record_exists(context, "browser_recordings", identifier)
    record = backend.get_recording_session(context.owner_home.home_dir, identifier)
    if record.get("thread_id"):
        conversation.require_thread(store, record["thread_id"], authorized, context)
    return record


# LLM: 不自行启动浏览器或解释录制代码；只传固定公开参数，发布目录再核当前有效owner。
# 函数用途: 适配WP-3录制的创建、停止、查询和参数化编译。
def _recorder(action, payload, authorized, context, conversation, store):
    backend = _module("assets.browser_recorder")
    home = context.owner_home.home_dir
    directory = _workspace(payload, authorized, context, conversation, store)
    if action.endswith(".start"):
        result = backend.start_recording_session(home, start_url=payload["start_url"], title=payload["title"],
            thread_id=payload.get("thread_id", ""), workspace_root=directory,
            headless=payload["headless"], initial_actions=payload.get("initial_actions"))
        return DomainResult(201, result)
    if action.endswith(".get") and not payload.get("recording_id"):
        rows = []
        for row in backend.list_recording_sessions(home):
            try:
                if row.get("thread_id"):
                    conversation.require_thread(store, row["thread_id"], authorized, context)
                rows.append(row)
            except HostAPIError as error:
                if error.status != 404:
                    raise
        return DomainResult(200, {"items": rows, "total": len(rows)})
    record = _recording(backend, payload["recording_id"], authorized, context, conversation, store)
    if action.endswith(".get"):
        return DomainResult(200, record)
    if action.endswith(".stop"):
        return DomainResult(200, backend.stop_recording_session(home, payload["recording_id"], final_actions=payload.get("final_actions")))
    scope = payload.get("scope", "owner")
    if scope == "workspace":
        raw = payload.get("workspace_dir") or record.get("workspace_root")
        if not raw:
            raise HostAPIError("WORKSPACE_DIR_REQUIRED")
        directory = workspace_directory(context, raw)
    result = backend.compile_recording_to_script(home, recording_id=payload["recording_id"],
        script_id=payload.get("script_id", ""), title=payload.get("title", ""),
        parameter_overrides=payload.get("parameter_overrides"), save_to_store=payload["save_to_store"],
        scope=scope, workspace_root=directory, generate_workflow=payload["generate_workflow"], generate_skill=payload["generate_skill"])
    return DomainResult(200, result)


# LLM: 先核元数据的线程及task范围再读日志；原backend不消费完成通知、不恢复redo或发控制信号。
# 函数用途: 返回授权范围内的后台列表及有界日志详情，隐藏宿主日志路径。
def _background(action, payload, authorized, context, services, store):
    backend = _module("conversation.background_process_view")
    conversation = services.conversation
    tid = payload.get("thread_id", "")
    if tid:
        conversation.require_thread(store, tid, authorized, context)
    snapshot = backend.list_background_processes(context.owner_home.home_dir, thread_id=tid, active_only=False)
    rows = [row for row in snapshot["items"] if process_visible(row, authorized, context, conversation, store, services)]
    if action.endswith(".inspect"):
        if not any(row["session_id"] == payload["session_id"] for row in rows):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        detail = backend.inspect_background_process(context.owner_home.home_dir, payload["session_id"], tail_lines=payload["tail_lines"])
        if detail.get("status") == "not_found":
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        if not process_visible(detail, authorized, context, conversation, store, services):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        return DomainResult(200, redact_sensitive_value({key: value for key, value in detail.items() if key != "log_path"}))
    active = [row for row in rows if row.get("alive") is True or row.get("wake_pending") is True]
    selected = active if payload["active_only"] else rows
    return DomainResult(200, {"items": redact_sensitive_value([{key: value for key, value in row.items() if key != "log_path"}
                             for row in selected]), "total": len(rows), "active_count": len(active),
                             "projection_ok": snapshot.get("projection_ok", True)})
