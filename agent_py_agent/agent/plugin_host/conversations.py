# LLM: B1仅接收A0调用，显式目录限有效管理员；默认摘要不公开路径，绑定事实及回执共原线程原子提交。
# 模块用途: 管理线程生命周期、目录绑定和完整历史，不因管理员安装或目录参数改变数据owner。
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from ..conversation.context_usage import context_usage_from_thread
from ..conversation.models import ConversationThread
from ..conversation.store_threads import create_thread_atomic
from ..gateway_parts.io import locked_file_transition
from . import contracts
from .action_specs import action_spec
from .authorization import workspace_directory
from .cursors import CursorCodec
from .errors import HostAPIError
from .model_settings_v2_handlers import configured_thread, creation_schema, settings_fields
from .schemas import PAGE_MAX_BYTES, canonical_json, integer, text, thaw, validate_object

# 游标允许续读的固定窗口，单位秒；并非授权有效期，返回前仍重核A0。
CURSOR_LIFETIME_SECONDS = 900
_STATE_KEY = "plugin_host_v2"
_THREAD_ACTIONS = frozenset({"create", "get", "list", "rename", "archive", "restore"})


# LLM: DTO不自证授权；A0未安装时拒绝就绪，不能使用本模块自建发行器。
# 函数用途: 取得当前真实owner上下文，并核对来源对象的一致性。
def read_context(authorized):
    if not isinstance(authorized, contracts.AuthorizedCall):
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    try:
        context = contracts.resolve_read_context(authorized)
    except ModuleNotFoundError as error:
        if error.name != __package__ + ".authorization":
            raise
        raise HostAPIError("HOST_NOT_READY", 503) from None
    if (context.identity != authorized.effective_owner or context.source_binding is not authorized.binding
            or context.owner_home.identity != context.identity):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return context


# LLM: 摘要是比较工具，不是授权；输入必须是可序列化的宿主公共结构。
# 函数用途: 计算分页过滤和幂等载荷的稳定摘要。
def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# LLM: 内部时间戳只投影为UTC，不公开任何宿主路径或运行元数据。
# 函数用途: 格式化线程与消息公共时间。
def public_time(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


# LLM: 冻结游标绑定安装、激活、授权修订、owner、动作和过滤；宿主codec必须提供MAC，不能用普通JSON代替。
# 类用途: 共用线程和消息页的有时效游标封套。
class BoundCursor:
    # LLM: 只保存宿主端口，不产生游标或持久化。
    # 函数用途: 绑定本次调用和不含cursor的有效过滤参数。
    def __init__(self, codec, authorized, context, filters):
        self.codec = codec
        self.authorized = authorized
        binding = authorized.binding
        self.binding = digest([context.owner_home.owner_id, binding.installation_ref, binding.activation_id,
                               binding.package_sha256, binding.grant_revision, context.user_revision,
                               binding.policy_digest, authorized.resource_scope, authorized.call.action, filters])

    # LLM: codec拒绝和绑定/时效变化统一过期，原授权撤销仍由A0优先处理。
    # 函数用途: 校验游标并返回固定快照及位置。
    def decode(self, token):
        if token is None:
            return None
        try:
            row = (thaw(self.codec.decode(self.authorized, token, {"binding": self.binding}).position)
                   if isinstance(self.codec, CursorCodec) else self.codec.decode(token))
            if (not isinstance(row, dict) or row.get("binding") != self.binding or row.get("v") != 1
                    or type(row.get("expires")) not in (int, float) or row["expires"] <= time.time()
                    or type(row.get("offset")) is not int or row["offset"] < 0 or not isinstance(row.get("anchor"), dict)):
                raise ValueError
            return row
        except (HostAPIError, ValueError, TypeError, KeyError):
            raise HostAPIError("CURSOR_EXPIRED", 409) from None

    # LLM: 续页不滑动延长期限；返回长度须满足A0的opaque游标上限。
    # 函数用途: 生成绑定当前快照的下一页游标。
    def encode(self, anchor, offset, expires):
        row = {"v": 1, "binding": self.binding, "anchor": anchor, "offset": offset, "expires": expires}
        token = (self.codec.encode(self.authorized, digest(anchor), row, {"binding": self.binding})
                 if isinstance(self.codec, CursorCodec) else self.codec.encode(row))
        if not isinstance(token, str) or not 0 < len(token) <= 2048:
            raise HostAPIError("HOST_NOT_READY", 503)
        return token


# LLM: 元数据仍在唯一canonical记录内；未登记旧线程保持空，已登记却缺revision视为损坏，不另猜版本。
# 函数用途: 读取本领域线程状态，旧线程没有该字段时保持空。
def thread_state(thread):
    if _STATE_KEY not in thread.metadata:
        return {}
    state = thread.metadata[_STATE_KEY]
    if not isinstance(state, dict) or type(state.get("revision")) is not int or state["revision"] < 1:
        raise HostAPIError("CORRUPT_THREAD", 500)
    return state


# LLM: 旧线程读取不能落盘造版本；用其canonical值生成有界比较值，首个插件写入后转入原Store递增修订。
# 函数用途: 使未迁移线程同样能拒绝宿主更新后的陈旧CAS。
def thread_revision(thread):
    state = thread_state(thread)
    return state["revision"] if "revision" in state else int(digest(thread.to_dict())[:15], 16) + 1


# LLM: 不先验证不可见对象的损坏字段；无法读取创建来源时只认明确ID授权，仍取两层范围的交集。
# 函数用途: 判断某个线程ID是否落在当前安装范围内。
def thread_scoped(tid, state, authorized):
    state = state if isinstance(state, Mapping) else {}
    for scope in (authorized.binding.grant.get("threads", {}), authorized.resource_scope.get("threads", {})):
        if not isinstance(scope, Mapping):
            return False
        if scope.get("scope") in ("owner", "owner_all", "all"):
            continue
        if tid not in scope.get("explicit_ids", ()) and not (
            scope.get("created_by_installation") is True
            and state.get("installation_ref") == authorized.binding.installation_ref
        ):
            return False
    return True


# LLM: owner核对先于范围，范围先于内部状态诊断；名称或路径不授予权限。
# 函数用途: 判断当前安装是否有权读取或修改某个线程。
def thread_visible(thread, authorized, context):
    return (thread.owner_id == context.owner_home.owner_id and thread.owner_home == str(context.owner_home.home_dir)
            and thread_scoped(thread.thread_id, thread.metadata.get(_STATE_KEY), authorized))


# LLM: 默认沿持久绑定/谱系隐藏路径；设置继承真实owner策略，用量只投影原公开数值。
# 函数用途: 为创建、查询、列表和回执生成一致的线程摘要。
def thread_summary(thread, authorized, *, include_workspace=None):
    state = thread_state(thread)
    if state.get("workspace_ref") not in (None, "default"):
        raise HostAPIError("CORRUPT_THREAD", 500)
    result = {"thread_id": thread.thread_id, "title": thread.title, "status": thread.status,
            "revision": thread_revision(thread), "workspace_ref": state.get("workspace_ref"),
            "created_by_installation": state.get("installation_ref") == authorized.binding.installation_ref,
            "created_at": public_time(thread.created_at), "updated_at": public_time(thread.updated_at)}
    result.update(settings_fields(thread))
    result["context_usage"] = context_usage_from_thread(thread)
    if include_workspace is None:
        include_workspace = state.get("has_workspace_binding") is True or bool(getattr(thread, "forked_from_thread_id", ""))
    if include_workspace:
        result.update(workspace_root=thread_workspace(thread), cwd=thread.cwd,
                      forked_from_thread_id=getattr(thread, "forked_from_thread_id", ""),
                      forked_from_message_id=getattr(thread, "forked_from_message_id", ""),
                      fork_lineage_depth=getattr(thread, "fork_lineage_depth", 0))
    return result


# LLM: 新模型字段是权威，旧记录只沿原metadata/cwd兼容，不创建第二份工作区账。
# 函数用途: 取得线程已经持久化的工作目录。
def thread_workspace(thread):
    return getattr(thread, "workspace_root", "") or thread.metadata.get("workspace_root") or thread.cwd


# LLM: 字段存在时更新原模型；旧模型只在原metadata保存兼容信息，不修改owner_home或runtime身份。
# 函数用途: 构造目录绑定所需的线程替换字段。
def workspace_changes(thread, directory):
    changes = {"cwd": str(directory), "runtime_workspace_roots": (str(directory),)}
    if "workspace_root" in thread.__dataclass_fields__:
        changes["workspace_root"] = str(directory)
    else:
        changes["metadata"] = {**thread.metadata, "workspace_root": str(directory)}
    return changes


# LLM: B1细化字段仍位于A0调用之后，不代替或放宽入站授权；A0须同步workspace_ref/max_content_bytes才能HTTP使用。
# 函数用途: 在静态动作目录基础上校验本领域有效参数。
def domain_payload(call):
    schema = action_spec(call.action).payload_schema
    if call.action == "conversations.threads.create":
        schema = creation_schema(schema)
        schema["properties"]["workspace_ref"] = {**text(128, 1, r"^[A-Za-z0-9_-]+$"), "default": "default"}
    if call.action == "conversations.messages.list":
        schema["properties"]["max_content_bytes"] = {**integer(65536, 1), "default": 16384}
    return validate_object(thaw(call.payload), schema)


# LLM: 宿主组合根注入原owner Store解析器、MAC和默认会话事实；不初始化Agent，不另建会话库。
# 类用途: 接收已授权调用的会话领域适配器。
class ConversationDomain:
    actions = frozenset({*("conversations.threads." + name for name in _THREAD_ACTIONS),
                         "conversations.messages.list", "conversations.messages.send",
                         "conversations.requests.poll", "conversations.requests.cancel", "conversations.requests.stream"})

    # LLM: 构造不读写业务状态；store_resolver应沿owner_conversation_store_from_config原规范路径打开。
    # 函数用途: 绑定原存储、游标与默认线程观察入口。
    def __init__(self, store_resolver, cursor_codec, *, default_thread, request_ports=None):
        self.store_resolver, self.cursor_codec, self.default_thread = store_resolver, cursor_codec, default_thread
        self.request_ports = request_ports

    # LLM: 读写前及返回前重核A0；分发只接受本领域固定七个动作，禁止动态执行方法。
    # 函数用途: 执行线程生命周期或完整历史查询。
    def call(self, authorized):
        context = read_context(authorized)
        action = authorized.call.action.removeprefix("conversations.")
        if action in {"messages.send", "requests.poll", "requests.cancel"}:
            from .requests import RequestDomain
            return RequestDomain(self).call(authorized, context)
        caps = authorized.binding.capabilities
        if action == "messages.list":
            required = {"chat.history"}
        elif action in {"threads.get", "threads.list"}:
            required = {"read", "chat", "chat.history"}
        elif action == "threads.create":
            required = {"chat"}
        elif action.removeprefix("threads.") in _THREAD_ACTIONS and action.startswith("threads."):
            required = {"threads.manage"}
        else:
            raise HostAPIError("UNSUPPORTED_ACTION")
        if not caps.intersection(required):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        payload = domain_payload(authorized.call)
        store = self.store_resolver(context)
        if action == "messages.list":
            from .history import history_page
            thread = self.require_thread(store, payload["thread_id"], authorized, context)
            result = contracts.DomainResult(200, history_page(store, thread, payload, authorized, context, self.cursor_codec))
        elif action == "threads.get":
            result = contracts.DomainResult(200, thread_summary(self.require_thread(store, payload["thread_id"], authorized, context), authorized))
        elif action == "threads.list":
            result = contracts.DomainResult(200, self.list_page(store, payload, authorized, context))
        else:
            result = self.mutate(store, action, payload, authorized, context)
        read_context(authorized)
        return result

    # LLM: A流泵消费RequestEvent而不是二次编码bytes；缺宿主端口时不创建后台执行。
    # 函数用途: 将请求流交给与poll共用的B2读取适配器。
    def stream(self, authorized):
        from .streams import request_events
        return request_events(self, authorized)

    # LLM: 不可信ID只经原路径验证；未能证明范围的坏记录也统一404，显式有权者可见损坏但不能覆盖。
    # 函数用途: 取得同owner且授权范围内的真实线程。
    def require_thread(self, store, tid, authorized, context):
        try:
            thread, error = store.threads.load_report(tid)
        except ValueError:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404) from None
        if error:
            if thread_scoped(tid, {}, authorized):
                raise HostAPIError("CORRUPT_THREAD", 500)
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        if thread is None or not thread_visible(thread, authorized, context):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        thread_state(thread)
        return thread

    # LLM: 只读列举使用原Store；任何坏记录阻止幂等全扫描，避免遗漏旧提交再写。
    # 函数用途: 返回线程集合或明确损坏，不忽略错误。
    def all_threads(self, store):
        threads, errors = store.threads.list_report(limit=0)
        if errors:
            raise HostAPIError("CORRUPT_THREAD", 500)
        return threads

    # LLM: 显式过滤限管理员且展开目录；无过滤沿绑定/谱系选择摘要，所有公开字段共绑原MAC快照。
    # 函数用途: 按owner和安装范围列线程，保留旧默认摘要及完整项目摘要。
    def list_page(self, store, payload, authorized, context):
        filters = {key: value for key, value in payload.items() if key != "cursor"}
        cursor = BoundCursor(self.cursor_codec, authorized, context, filters)
        saved = cursor.decode(payload.get("cursor"))
        threads = sorted(self.all_threads(store), key=lambda row: (row.created_at, row.thread_id))
        directory = workspace_directory(context, payload["workspace_dir"]) if "workspace_dir" in payload else None
        rows = [thread_summary(row, authorized, include_workspace=True if directory is not None else None) for row in threads
                if thread_visible(row, authorized, context)
                and (payload["status"] == "all" or row.status == payload["status"])
                and (directory is None or thread_workspace(row) == str(directory))]
        anchor = {"sha256": digest(rows), "record_count": len(rows)}
        if saved and (saved["anchor"] != anchor or saved["offset"] > len(rows)):
            raise HostAPIError("CURSOR_EXPIRED", 409)
        offset = saved["offset"] if saved else 0
        selected = rows[offset:offset + payload["limit"]]
        end = offset + len(selected)
        token = cursor.encode(anchor, end, saved["expires"] if saved else time.time() + CURSOR_LIFETIME_SECONDS) if end < len(rows) else None
        page = {"items": selected, "next_cursor": token, "snapshot_id": digest(anchor), "complete": token is None, "load_errors": []}
        if len(canonical_json(page).encode("utf-8")) > PAGE_MAX_BYTES:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        return page

    # LLM: receipt、目录和生命周期共原子JSON；同owner/install/action/key串行，目录写在短提交中重新核验。
    # 函数用途: 在原线程锁中完成幂等、CAS、生命周期修改或目录切换。
    def mutate(self, store, action, payload, authorized, context):
        key = authorized.call.idempotency_key
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) is None:
            raise HostAPIError("INVALID_SCHEMA")
        if action != "threads.create":
            self.require_thread(store, payload["thread_id"], authorized, context)
        receipt_key = digest([context.owner_home.owner_id, authorized.binding.installation_ref, action, key])
        fingerprint = digest(payload)
        with locked_file_transition(store.storage.threads_dir / (".plugin-" + receipt_key)), ExitStack() as activity_locks:
            read_context(authorized)
            for row in self.all_threads(store):
                receipt = thread_state(row).get("receipts", {}).get(receipt_key)
                if receipt is not None:
                    if not thread_visible(row, authorized, context):
                        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                    if receipt["payload_hash"] != fingerprint:
                        raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
                    return contracts.DomainResult(receipt["http_status"], receipt["data"])
            if action == "threads.create":
                row = self.new_thread(payload, authorized, context)
                result, row = self.receipted(row, authorized, receipt_key, fingerprint, 201)
                read_context(authorized)
                create_thread_atomic(store.threads, row)
                return result

            # LLM: CAS、显式绑定标记及receipt共原线程锁；切换仍核管理员且递增版本，归档沿原goal→claim锁序。
            # 函数用途: 在线程原子提交内复验权限并应用目录或生命周期变更。
            def update(row):
                read_context(authorized)
                if not thread_visible(row, authorized, context):
                    raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                if action == "threads.archive":
                    self.check_archive(store, row, context, activity_locks)
                state = thread_state(row)
                revision = thread_revision(row)
                if revision != payload.get("expected_revision", revision):
                    raise HostAPIError("REVISION_CONFLICT", 409)
                state = {**state, "revision": revision + 1}
                if action == "threads.workspace.switch":
                    changes = workspace_changes(row, workspace_directory(context, payload["workspace_dir"]))
                    state["has_workspace_binding"] = True
                else:
                    changes = {"title": payload["title"]} if action == "threads.rename" else {"status": "archived" if action == "threads.archive" else "active"}
                metadata = {**changes.pop("metadata", row.metadata), _STATE_KEY: state}
                updated = replace(row, **changes, metadata=metadata, updated_at=time.time())
                return self.receipted(updated, authorized, receipt_key, fingerprint, 200)[1]

            updated = store.threads.update_atomic(payload["thread_id"], update)
            receipt = thread_state(updated)["receipts"][receipt_key]
            return contracts.DomainResult(200, receipt["data"])

    # LLM: 创建仍须本安装有权；显式管理员目录与绑定标记同次提交，默认不借daemon cwd，owner home不变。
    # 函数用途: 构造绑定默认家目录或已核项目目录的未绑定通道线程。
    def new_thread(self, payload, authorized, context):
        if payload["workspace_ref"] != "default":
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        if any(scope.get("created_by_installation") is not True for scope in (
            authorized.binding.grant.get("threads", {}), authorized.resource_scope.get("threads", {})
        )):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        current = time.time()
        state = {"installation_ref": authorized.binding.installation_ref, "revision": 1, "workspace_ref": "default"}
        if "workspace_dir" in payload:
            state["has_workspace_binding"] = True
        directory = (workspace_directory(context, payload["workspace_dir"]) if "workspace_dir" in payload
                     else context.owner_home.home_dir)
        row = ConversationThread(thread_id="thread-" + uuid4().hex, canonical_user_id=context.owner_home.owner_id,
                                  owner_id=context.owner_home.owner_id, owner_home=str(context.owner_home.home_dir),
                                  title=payload["title"], cwd=str(directory),
                                  created_at=current, updated_at=current, metadata={_STATE_KEY: state})
        from ..user_space.home_layout import home_paths
        from ..user_space.owner_resolver import home_paths_with_owner
        return configured_thread(replace(row, **workspace_changes(row, directory)), payload,
            home_paths_with_owner(home_paths(context.owner_home.root), context.owner_home))

    # LLM: 直接领域调用供W5/W6接线；两层创建范围须许可，不沿用源安装权限或回执。
    # 函数用途: 使用统一分叉处理器保留偏好并兼容消息边界别名。
    def fork(self, store, payload, authorized, context):
        from .thread_settings_v2 import ThreadSettingsV2Handlers
        self.require_thread(store, payload["source_thread_id"], authorized, context)
        if any(scope.get("created_by_installation") is not True for scope in (
                authorized.binding.grant.get("threads", {}), authorized.resource_scope.get("threads", {}))):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        return ThreadSettingsV2Handlers(store, context, authorized=authorized).fork_thread(payload)

    # LLM: 回执与绑定事实同次提交且共用查询摘要；默认仍隐藏路径，绑定/分叉回执保留完整目录与谱系。
    # 函数用途: 保存生命周期或目录操作的准确原始结果。
    def receipted(self, row, authorized, key, fingerprint, status):
        data = thread_summary(row, authorized)
        state = thread_state(row)
        receipt = {"payload_hash": fingerprint, "activation_id": authorized.binding.activation_id,
                   "http_status": status, "data": data}
        state = {**state, "receipts": {**state.get("receipts", {}), key: receipt}}
        return contracts.DomainResult(status, data), replace(row, metadata={**row.metadata, _STATE_KEY: state})

    # LLM: 在线程锁内冻结task_ids，锁原goal及默认/任务独立claim至线程提交；已结案TaskLink不等于claim已结算。
    # 函数用途: 阻止归档默认线程、活跃回合、独立执行车道或任何未终结目标。
    def check_archive(self, store, row, context, activity_locks):
        from ..conversation.run_claim import detached_task_claim_scope_id

        scopes = [row.thread_id, *(detached_task_claim_scope_id(row.thread_id, task_id) for task_id in row.task_ids)]
        activity_locks.enter_context(locked_file_transition(store.storage.goal_path(row.thread_id)))
        for scope in scopes:
            activity_locks.enter_context(locked_file_transition(store.storage.background_claim_path(scope)))
        goals, goal_error = store.goals.list_report(row.thread_id)
        if goal_error:
            raise HostAPIError("CORRUPT_THREAD", 500)
        for scope in scopes:
            claim, claim_error = store.claims.load_report(row.thread_id, claim_scope_id=scope)
            if claim_error:
                raise HostAPIError("CORRUPT_THREAD", 500)
            if claim.get("status") in {"running", "claimed"}:
                raise HostAPIError("THREAD_BUSY", 409)
        if (self.default_thread(context, row.thread_id) or row.active_task_ids
                or any(goal.status not in {"complete", "cleared", "cancelled"} for goal in goals)):
            raise HostAPIError("THREAD_BUSY", 409)
