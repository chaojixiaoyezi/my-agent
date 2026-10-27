# LLM: 请求域只投影原输入、claim和规范终态；不另建执行账本，不把此reader当授权入口。
# 模块用途: 提供B2请求状态事实读取，供真实授权/游标适配器组合。
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from ..common.nofollow_fs import read_bytes_beneath
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts import input_delivery_service as inputs
from ..gateway_parts.io import validated_gateway_request_fingerprint
from ..gateway_parts.request_owner_binding import frozen_request_owner
from ..runtime_errors import DataCorruptionError
from .contracts import project_request_state as project_request_state
from .errors import HostAPIError

# 单条插件会话请求或回执 JSON 记录的最大读取字节数（4 MiB）。
_MAX_RECORD_BYTES = 4 * 1024 * 1024


# LLM: 同一次只读快照不建锁/索引、不跟随链接；上限不截断后冒充完整JSON。
# 函数用途: 严格读取一份宿主原账，明确区分缺失和损坏。
def _read_record(paths, parts):
    try:
        raw = read_bytes_beneath(paths.root, parts, max_bytes=_MAX_RECORD_BYTES,
                                 require_dir_fd=True, snapshot_retries=2)
        if raw is None:
            return None
        row = json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
        if type(row) is not dict or not row:
            raise ValueError("invalid record")
        return row
    except (OSError, ValueError, UnicodeError) as error:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from error


# LLM: 存储JSON中的重复键也不能静默覆盖原身份。
# 函数用途: 拒绝歧义字段。
def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


# LLM: 非有限常量不属于可信JSON账本。
# 函数用途: 拒绝NaN/Infinity。
def _invalid_constant(value):
    raise ValueError("nonfinite JSON value")


# LLM: 原完整prepared digest必须通过，owner和安装检查后才读执行内容；元数据本身不授调用权限。
# 函数用途: 读取并核对指定scope的原输入回执。
def _read_receipt(paths, request_id, expected_owner, installation_ref):
    raw = _read_record(paths, ("input_receipts", request_id + ".json"))
    if raw is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    receipt = inputs.GatewayInputReceipt.from_dict(raw)
    inputs._validate_prepared_request(receipt)
    if receipt.request_id != request_id:
        raise DataCorruptionError("input receipt id mismatch")
    prepared = receipt.prepared_request
    provenance = prepared.get("metadata", {}).get("plugin_provenance")
    if (frozen_request_owner(prepared) != expected_owner or type(provenance) is not dict
            or provenance.get("installation_ref") != installation_ref):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    validate_opaque_id(provenance.get("thread_id"), kind="thread")
    if provenance.get("kind") not in {"chat", "tool", "script", "workflow"}:
        raise DataCorruptionError("request kind invalid")
    return receipt, provenance


# LLM: 只接受与原input完整摘要一致的请求，不从responses/done/failed推断执行结局。
# 函数用途: 读取同一请求的当前claim或规范终态。
def _read_execution(paths, receipt):
    name = receipt.request_id + ".json"
    for folder in ("terminal", "processing", "pending"):
        row = _read_record(paths, ("requests", folder, name))
        if row is None:
            continue
        if row.get("id") != receipt.request_id or not inputs._prepared_input_matches(receipt, row):
            raise DataCorruptionError("request identity changed")
        validated_gateway_request_fingerprint(row, receipt.request_id)
        if folder == "terminal" and row.get("schema_version") != "gateway_terminal_request.v1":
            raise DataCorruptionError("terminal schema invalid")
        return row, folder
    return {}, ""


# LLM: 原canonical完整response决定终态，用量只取同请求已记录数值；未知/资源停止不补零或猜成功。
# 函数用途: 将原终态及真实计量映射成公开白名单字段。
def _terminal_view(row):
    response = row.get("terminal_response")
    if type(response) is not dict or response.get("id") != row["id"]:
        raise DataCorruptionError("terminal response missing")
    status = {"done": "succeeded", "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled"}.get(row.get("status"))
    stamp = row.get("completed_at")
    if (status is None or type(stamp) not in (float, int) or not math.isfinite(stamp)
            or status == "succeeded" and response.get("ok") is not True):
        raise DataCorruptionError("terminal status invalid")
    code = row.get("error_code")
    # 原canonical writer用空字符串表示无错误；不能把其他falsey类型当成空值。
    if type(code) is str and code == "":
        code = None
    if code is not None and (type(code) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)):
        raise DataCorruptionError("terminal error code invalid")
    return {"terminal_id": row["id"], "status": status,
            "completed_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z"),
            "result_message_ids": [], "error_code": code,
            "usage": request_usage(row), "resources_stopped": None}


# LLM: 同请求计量优先usage→result→terminal_response；布尔、负数、非有限值不冒充测量，不改原记录。
# 函数用途: 投影已经记录的token和耗时，未记录时保留旧版两字段null合同。
def request_usage(row):
    aliases = {"input_tokens": ("input_tokens", "prompt_tokens", "model_accounted_input_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens", "model_output_tokens"),
        "cached_tokens": ("cached_tokens", "cache_read_tokens", "cache_read_input_tokens", "model_cached_input_tokens"),
        "elapsed_seconds": ("elapsed_seconds", "last_turn_elapsed_seconds"),
        "tokens_per_second": ("tokens_per_second", "last_turn_tokens_per_sec", "output_tps"),
        "cache_read_tokens": ("cache_read_tokens",), "cache_write_tokens": ("cache_write_tokens",),
        "total_tokens": ("total_tokens", "model_total_tokens")}
    sources = [row.get("usage"), row.get("result"), row.get("terminal_response")]
    candidates = []
    for source in sources:
        if type(source) is dict:
            candidates.extend([source.get("usage"), source, source.get("model_metrics"), source.get("model_context_usage")])
    values = {}
    for source in candidates:
        if type(source) is not dict:
            continue
        for key, names in aliases.items():
            for name in names:
                value = source.get(name)
                kinds = (int, float) if key in {"elapsed_seconds", "tokens_per_second"} else (int,)
                if key not in values and type(value) in kinds and math.isfinite(value) and 0 <= value <= 2**63 - 1:
                    values[key] = value
                    break
    if not values:
        return {"input_tokens": None, "output_tokens": None}
    return {**dict.fromkeys(tuple(aliases)[:5]), **values}


# LLM: 原unknown观察必须匹配同一claim，不让旧attempt的运输未知覆盖新代次。
# 函数用途: 读取与当前执行代次一致的未知结果标记。
def _execution_unknown(row):
    observation = row.get("execution_outcome")
    return (type(observation) is dict and observation.get("schema_version") == "gateway_execution_outcome.v1"
            and observation.get("status") == "unknown"
            and all(observation.get(key) == row.get(key) for key in
                    ("execution_attempt_id", "lease_epoch", "lease_owner"))
            and observation.get("request_id") == row.get("id"))


# LLM: 这里只映射原claim存在与显式阶段；空文件/丢失队列仍unknown，不制造waiting进度。
# 函数用途: 把原输入和当前执行观察组装为最小公开状态。
def _request_facts(receipt, provenance, row, folder):
    terminal = _terminal_view(row) if folder == "terminal" else None
    claim_state = "running" if folder == "processing" and row.get("execution_attempt_id") and row.get("lease_epoch") else ""
    receipt_state = "queued" if folder == "pending" and receipt.state == "queued" else ""
    if receipt_state and row.get("admission_wait_reason"):
        receipt_state = "admission_wait"
    cancel = row.get("cancel_requested") is True
    state = project_request_state(receipt_state=receipt_state, claim_state=claim_state,
        cancellation_requested=cancel, canonical_terminal=terminal,
        execution_unknown=receipt.state == "terminal_unknown" or _execution_unknown(row))
    return {"request_id": receipt.request_id, "thread_id": provenance["thread_id"], "kind": provenance["kind"],
            "state": state, "cancel_requested": cancel, "terminal": terminal}


# LLM: 只能由授权领域适配器提供owner/安装与原路径；这是事实reader，不是完整poll/公开读取入口。
# 函数用途: 只读原input、队列、canonical并返回最小状态事实，不创建任何目录或锁文件。
def read_request_facts(paths, request_id, *, expected_owner, installation_ref):
    try:
        validate_opaque_id(request_id, kind="plugin_request")
    except ValueError as error:
        raise HostAPIError("INVALID_SCHEMA") from error
    try:
        receipt, provenance = _read_receipt(paths, request_id, expected_owner, installation_ref)
        row, folder = _read_execution(paths, receipt)
        return _request_facts(receipt, provenance, row, folder)
    except HostAPIError:
        raise
    except (DataCorruptionError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from error


# LLM: 宿主端口只解析已验证上下文；原input/canonical继续是唯一请求权威。
# 类用途: 把B2公开调用接到B1线程范围及原网关记录。
class RequestDomain:
    # LLM: 不创建目录或执行主体，不复制main agent。
    # 函数用途: 绑定现有会话领域与同一MAC服务。
    def __init__(self, conversation):
        self.conversation = conversation
        self.ports = conversation.request_ports
        self.codec = conversation.cursor_codec

    # LLM: 插话同样消费真实发行对象且先核线程/请求归属；端口缺失拒绝，所有结果由组合根末核。
    # 函数用途: 分发send、poll、cancel和interject动作。
    def call(self, authorized, context):
        from ..gateway_parts.plugin_request_source import require_plugin_source
        from .contracts import DomainResult

        require_plugin_source(authorized)
        if self.ports is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        paths = self.ports.paths_for(context)
        store = self.conversation.store_resolver(context)
        if authorized.call.action == "conversations.messages.send":
            return self.send(authorized, context, paths, store)
        if authorized.call.action == "conversations.requests.interject":
            return interject_request(self, authorized, context, paths, store)
        facts = self.facts(authorized, context, paths, store)
        if authorized.call.action == "conversations.requests.poll":
            return DomainResult(200, self.poll(authorized, paths, store, facts))
        if authorized.call.action == "conversations.requests.cancel":
            from ..gateway_parts.plugin_request_submission import cancel_message
            return cancel_message(self, authorized, context, paths, store)
        raise HostAPIError("UNSUPPORTED_ACTION")

    # LLM: 请求归属先核原回执owner/install，再核当前thread范围和kind权限；chat不包含execute。
    # 函数用途: 获取当前调用可见的准确请求事实。
    def facts(self, authorized, context, paths, store):
        facts = read_request_facts(paths, authorized.call.payload["request_id"],
            expected_owner=context.identity, installation_ref=authorized.binding.installation_ref)
        required = "chat" if facts["kind"] == "chat" else "execute"
        if required not in authorized.binding.capabilities:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        self.conversation.require_thread(store, facts["thread_id"], authorized, context)
        receipt, _ = _read_receipt(paths, facts["request_id"], context.identity, authorized.binding.installation_ref)
        row, _ = _read_execution(paths, receipt)
        facts["revision"] = request_revision(row)
        return facts

    # LLM: 附件在原首次prepare窗口核Owner与nofollow后冻结，正文进入原goal及指纹；bounded仍须原C接缝。
    # 函数用途: 验证线程并经唯一原输入回执提交带附件消息。
    def send(self, authorized, context, paths, store):
        from .contracts import DomainResult, resolve_execution_context

        thread = self.conversation.require_thread(store, authorized.call.payload["thread_id"], authorized, context)
        if thread.status != "active":
            raise HostAPIError("THREAD_ARCHIVED", 409)
        execution = resolve_execution_context(authorized) if authorized.call.payload["tool_policy"] == "bounded" else None
        if self.ports.prepare_request is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        from ..gateway_parts.plugin_request_submission import submit_message

        # LLM: 只在原input首次准备时读附件，不在重放时重读已变文件；后端仍落实原执行策略。
        # 函数用途: 在宿主原prepare接缝内冻结工作区文件和媒体引用。
        def prepare(source, owner, policy, payload):
            payload = attach_message_inputs(payload, source, owner, thread)
            return self.ports.prepare_request(source, owner, policy, payload)

        request_id = submit_message(authorized, context, execution, paths, prepare)
        facts = read_request_facts(paths, request_id, expected_owner=context.identity,
                                   installation_ref=authorized.binding.installation_ref)
        return DomainResult(202, {**facts, "event_cursor": None})

    # LLM: 分页引用绑定真实发行对象及request/channel，不跨请求、安装或动作复用。
    # 函数用途: 解码一条内部分页位置。
    def position(self, authorized, channel):
        from .schemas import thaw

        token = authorized.call.payload.get(channel + "_cursor")
        if token is None:
            return {}
        saved = self.codec.decode(authorized, token, {"channel": channel})
        if saved.snapshot_id != authorized.call.payload["request_id"]:
            raise HostAPIError("INVALID_SCHEMA")
        return thaw(saved.position)

    # LLM: 位置只携内部计数和锚点，不放正文或工具秘密。
    # 函数用途: 签发当前请求的有界MAC游标。
    def cursor(self, authorized, channel, position):
        return self.codec.encode(authorized, authorized.call.payload["request_id"], position, {"channel": channel})

    # LLM: 公开状态来自本次原账观察，不伪造历史状态转移或把空回复当成功。
    # 函数用途: 返回请求状态及可续读的双通道页面。
    def poll(self, authorized, paths, store, facts):
        event_position = self.position(authorized, "event")
        reply_position = self.position(authorized, "reply")
        receipt, _ = _read_receipt(paths, facts["request_id"], authorized.effective_owner, authorized.binding.installation_ref)
        rows, reply_position, more = request_replies(store, facts, reply_position)
        signature = facts["state"]
        events = []
        if event_position.get("state") != signature:
            sequence = event_position.get("sequence", 0) + 1
            terminal = facts["terminal"]
            events.append({"event_id": facts["request_id"] + ":" + signature,
                "sequence": sequence, "created_at": terminal["completed_at"] if terminal else datetime.fromtimestamp(receipt.updated_at, timezone.utc).isoformat().replace("+00:00", "Z"),
                "kind": "terminal" if terminal else "status", "data": terminal or {"state": signature}})
            event_position = {"state": signature, "sequence": sequence}
        return {"request": facts, "events": events, "replies": rows,
                "event_cursor": self.cursor(authorized, "event", event_position),
                "reply_cursor": self.cursor(authorized, "reply", reply_position),
                "has_more_events": False, "has_more_replies": more}


# LLM: 旧锚点必须同时匹配实际打开的句柄及返回前的canonical路径；只读本request最终正文，不拼接替换文件。
# 函数用途: 返回一个有界最终回复片段和可重建的内部位置。
def request_replies(store, facts, position):
    from .history import (
        HISTORY_RECORD_MAX_BYTES,
        _entry,
        _open,
        _scan,
        _verify_current,
        _visible_content,
    )

    tid = facts["thread_id"]
    try:
        if position.get("anchor"):
            _verify_current(store, tid, position["anchor"])
        with _open(store, tid) as handle:
            if position.get("anchor"):
                _verify_reply_handle(handle, position["anchor"])
            handle.seek(0)
            through = handle.seek(0, 2)
            _, _, anchor, bad = _scan(handle, through, tid, 0, {"limit": 1, "max_content_bytes": 1})
            if bad:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            offset, content_offset = position.get("offset", 0), position.get("content_offset", 0)
            if not 0 <= offset <= through:
                raise HostAPIError("CURSOR_EXPIRED", 410)
            handle.seek(offset)
            rows = []
            while handle.tell() < through:
                start = handle.tell()
                raw = handle.readline(HISTORY_RECORD_MAX_BYTES + 1)
                if len(raw) > HISTORY_RECORD_MAX_BYTES:
                    raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
                if not raw.endswith(b"\n"):
                    raise HostAPIError("STORE_UNAVAILABLE", 503)
                entry = _entry(raw, tid)
                visible = _visible_content(entry)
                linked = any(entry.metadata.get(key) == facts["request_id"] for key in
                             ("turn_id", "gateway_request_id", "background_transcript_request_id"))
                offset = handle.tell()
                if not linked or visible is None or visible[:2] != ("assistant", "final"):
                    content_offset = 0
                    continue
                text = visible[2].encode("utf-8")
                if not 0 <= content_offset <= len(text):
                    raise HostAPIError("CURSOR_EXPIRED", 410)
                piece = text[content_offset:content_offset + 8192].decode("utf-8", errors="ignore")
                end = content_offset + len(piece.encode("utf-8"))
                rows.append({"request_id": facts["request_id"], "message_id": entry.message_id,
                    "content": piece, "offset_bytes": content_offset, "complete": end == len(text)})
                offset, content_offset = (offset, 0) if end == len(text) else (start, end)
                break
        if position.get("anchor"):
            _verify_current(store, tid, position["anchor"])
        _verify_current(store, tid, anchor)
        return rows, {"anchor": anchor, "offset": offset, "content_offset": content_offset}, offset < through
    except FileNotFoundError:
        if position:
            raise HostAPIError("CURSOR_EXPIRED", 410) from None
        return [], {}, False
    except HostAPIError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 验证的是本次已打开句柄，不再开路径；分块核完整旧前缀，追加合法，换inode/截短/改写均409。
# 函数用途: 拒绝旧锚点预检与实际打开之间发生的回复文件替换。
def _verify_reply_handle(handle, anchor):
    import hashlib
    import os

    info = os.fstat(handle.fileno())
    size = anchor.get("byte_size")
    if (type(size) is not int or not 0 <= size <= info.st_size or info.st_nlink != 1
            or [info.st_dev, info.st_ino] != anchor.get("identity")):
        raise HostAPIError("CURSOR_EXPIRED", 409)
    handle.seek(0)
    remaining, digest = size, hashlib.sha256()
    while remaining:
        raw = handle.read(min(remaining, 256 * 1024))
        if not raw:
            raise HostAPIError("CURSOR_EXPIRED", 409)
        digest.update(raw)
        remaining -= len(raw)
    if digest.hexdigest() != anchor.get("sha256") or os.fstat(handle.fileno()).st_nlink != 1:
        raise HostAPIError("CURSOR_EXPIRED", 409)
    handle.seek(0)


# LLM: revision来自原请求当前代次及cancel/终态事实，不保存第二套可变版本。
# 函数用途: 为精确请求控制生成有界CAS比较值。
def request_revision(row):
    from hashlib import sha256

    from .schemas import canonical_json

    fields = {key: row.get(key) for key in ("id", "execution_attempt_id", "lease_epoch", "lease_owner",
              "cancel_requested", "schema_version", "status")}
    return int(sha256(canonical_json(fields).encode()).hexdigest()[:15], 16) + 1


# LLM: 线程根及其Owner内祖先拒链接；从Owner/文件系统根逐段nofollow读取，空相对组件在I/O前拒绝，首次prepare冻结正文。
# 函数用途: 把工作区文本附件转成不可变用户上下文，保留准确路径引用而不在worker重读路径。
def _context_inputs(context, thread, names):
    from pathlib import Path
    from types import SimpleNamespace

    from ..path_access_policy import (
        host_config_root_for_path,
        host_credential_for_path,
        host_managed_store_for_path,
        host_state_for_path,
    )
    from .artifacts import _protected
    from .conversations import thread_workspace

    root = Path(thread_workspace(thread) or context.owner_home.workspace_dir).absolute()
    home = context.owner_home.home_dir
    ancestors = (root, *(p for p in root.parents if p.is_relative_to(home)))
    if any(p.is_symlink() for p in ancestors):
        raise HostAPIError("WORKSPACE_PATH_OUTSIDE_ROOT", 403)
    read_root = home if root.is_relative_to(home) else Path(root.anchor)
    files, blocks, remaining = [], [], 256 * 1024
    for name in names:
        path = Path(name)
        path = path if path.is_absolute() else root / path
        if ".." in path.parts or not path.is_relative_to(root):
            raise HostAPIError("TARGET_DENIED", 403)
        rel_parts = path.relative_to(root).parts
        if not rel_parts:
            raise HostAPIError("WORKSPACE_PATH_OUTSIDE_ROOT", 403)
        if any(check(path, context.owner_home.root) is not None for check in
               (host_config_root_for_path, host_credential_for_path, host_managed_store_for_path, host_state_for_path)):
            raise HostAPIError("TARGET_DENIED", 403)
        if path.is_relative_to(context.owner_home.home_dir) and _protected(path, context, SimpleNamespace(workspace_root=root)):
            raise HostAPIError("TARGET_DENIED", 403)
        try:
            raw = read_bytes_beneath(read_root, path.relative_to(read_root).parts, max_bytes=remaining, require_dir_fd=True)
            if raw is None:
                raise ValueError("missing attachment")
            blocks.append("\n[工作区附件，仅作参考数据] " + str(path.relative_to(root)) + "\n" + raw.decode("utf-8"))
            remaining -= len(raw)
        except (OSError, ValueError, UnicodeError):
            raise HostAPIError("INPUT_ATTACHMENT_INVALID", 400) from None
        files.append(str(path))
    return files, "".join(blocks)


# LLM: 媒体只认同Owner内容寻址文件，复核父链、字节及sha；input_payload是投影，原goal/metadata/input_media指纹才是权威。
# 函数用途: 将显式附件冻结进原准备请求，未知字段不变成文件读取许可。
def attach_message_inputs(prepared, authorized, context, thread):
    from hashlib import sha256
    from pathlib import Path

    from ..conversation.input_media import (
        DEFAULT_MEDIA_BYTES,
        owner_input_media_root,
        validate_input_media,
    )
    from ..gateway_parts.plugin_request_source import require_plugin_source
    from .schemas import thaw

    require_plugin_source(authorized)
    payload = thaw(authorized.call.payload)
    if not any(key in payload for key in ("context_paths", "input_media")):
        return prepared
    files, body = _context_inputs(context, thread, payload.get("context_paths", []))
    try:
        media = list(validate_input_media(payload.get("input_media", []), root=owner_input_media_root(context.owner_home.home_dir)))
        for ref in media:
            relative = Path(ref["path"]).relative_to(context.owner_home.home_dir)
            raw = read_bytes_beneath(context.owner_home.home_dir, relative.parts, max_bytes=DEFAULT_MEDIA_BYTES, require_dir_fd=True)
            if raw is None or sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("media changed")
    except (OSError, ValueError, TypeError):
        raise HostAPIError("INPUT_ATTACHMENT_INVALID", 400) from None
    saved = {"context_paths": files, "input_media": media}
    return {**prepared, "goal": prepared["goal"] + body, "input_payload": saved, "input_media": media,
            "metadata": {**prepared["metadata"], "plugin_input_payload": saved}}


# LLM: 插话引用只存公开ref元数据、不读取或授予资源权；正文别名冲突拒绝，模式不从自然语言推断。
# 函数用途: 规范插话文字和可选引用，拒绝空白或不支持的投递模式。
def _interjection_input(payload):
    from .action_specs import id_schema
    from .schemas import closed, text, validate_object

    mode = payload.get("mode", "steer")
    message = payload.get("text", payload.get("message", ""))
    if (mode not in {"steer", "queue", "interrupt"} or not isinstance(message, str) or not message.strip()
            or "text" in payload and "message" in payload and payload["text"] != payload["message"]):
        raise HostAPIError("INVALID_SCHEMA")
    refs = [validate_object(ref, closed({"artifact_ref": id_schema()}, {
        "task_id": id_schema(), "sha256": text(64, 64, r"^[a-f0-9]{64}$")}))
        for ref in payload.get("input_artifact_refs", ())]
    return mode, message, refs


# LLM: 同key沿原guidance回执比完整输入，重放不耗第二个配额；无key保持原非幂等追加，不另建账。
# 函数用途: 在原guidance幂等门追加或重放一条插话。
def _interjection_guidance(store, request, idempotency_key, count):
    from uuid import uuid4

    from ..conversation.store_guidance import guidance_receipt_input_matches

    key = "plugin-interject:" + (idempotency_key or uuid4().hex)
    prior = store.guidance.receipt(key) if idempotency_key is not None else None
    if prior is not None and not guidance_receipt_input_matches(prior, request):
        raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
    if count >= 64 and prior is None:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return store.guidance.append_once(request, dedupe_key=key)


# LLM: 必须在原请求T内调用；原guidance先提交，原claim只记一次引用，不更新指纹内原输入或复制回执。
# 函数用途: 追加一条准确目标的插话，并在运行请求中保留已受理记录。
def _append_interjection(domain, authorized, context, paths, store, payload, *, idempotency_key=None):
    from ..gateway_parts.io import update_json_file_atomic
    from ..gateway_parts.plugin_request_source import require_plugin_source

    mode, message, refs = _interjection_input(payload)
    request_id, row, folder = payload.get("request_id"), {}, ""
    if request_id:
        facts = domain.facts(authorized, context, paths, store)
        if facts["thread_id"] != payload["thread_id"] or facts["kind"] != "chat":
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        receipt, _ = _read_receipt(paths, request_id, context.identity, authorized.binding.installation_ref)
        row, folder = _read_execution(paths, receipt)
        if facts["terminal"] is not None or folder not in {"pending", "processing"} or mode != "queue" and folder != "processing":
            raise HostAPIError("REQUEST_NOT_RUNNING", 409)
    require_plugin_source(authorized)
    entry = _interjection_guidance(store, {"target_type": "thread", "target_id": payload["thread_id"], "message": message,
        "delivery": "next_turn" if mode == "queue" else "interrupt", "sender": authorized.binding.installation_ref,
        "metadata": {"expected_turn_id": request_id if mode != "queue" and request_id else "",
                     "plugin_installation_ref": authorized.binding.installation_ref, "input_artifact_refs": refs,
                     "plugin_request_id": request_id, "mode": mode}}, idempotency_key, len(row.get("interjections", [])))
    if folder in {"pending", "processing"}:
        try:
            require_plugin_source(authorized)
        except HostAPIError as error:
            raise HostAPIError(error.code, error.status, "unknown") from None

        # LLM: 同一请求T内保留原path锁；已有guidance重放不计数，部分提交恢复遇满额保unknown，绝不补成65项。
        # 函数用途: 为排队和运行请求共同记录插话计数，保留其它并发字段。
        def record(current):
            if current.get("id") != request_id or current.get("request_fingerprint") != row.get("request_fingerprint"):
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            if any(item.get("guidance_id") == entry.guidance_id for item in current.get("interjections", [])):
                return current
            if len(current.get("interjections", [])) >= 64:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            return {**current, "interjections": [*current.get("interjections", []), {"guidance_id": entry.guidance_id, "mode": mode}]}

        path = (paths.processing if folder == "processing" else paths.inbox) / (request_id + ".json")
        try:
            update_json_file_atomic(path, record, require_existing=True)
        except (OSError, ValueError):
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    return {"accepted": True, "thread_id": payload["thread_id"], "request_id": request_id,
            "mode": mode, "guidance_id": entry.guidance_id}


# LLM: 真实来源/线程先核，opaque请求ID在构造原T锁前验证；可选key只交原guidance幂等门，不授执行能力。
# 函数用途: 通过原guidance原账受理线程或活动回合插话。
def interject_request(domain, authorized, context, paths, store):
    from contextlib import nullcontext

    from ..gateway_parts.plugin_request_source import require_plugin_source
    from .contracts import DomainResult
    from .schemas import thaw

    require_plugin_source(authorized)
    payload = thaw(authorized.call.payload)
    thread = domain.conversation.require_thread(store, payload["thread_id"], authorized, context)
    if thread.status != "active":
        raise HostAPIError("THREAD_ARCHIVED", 409)
    _interjection_input(payload)
    if payload.get("request_id") is not None:
        try:
            payload["request_id"] = validate_opaque_id(payload["request_id"], kind="plugin_request")
        except ValueError:
            raise HostAPIError("INVALID_SCHEMA") from None
    scope = inputs.gateway_input_transition(paths, payload["request_id"]) if payload.get("request_id") else nullcontext()
    with scope:
        result = _append_interjection(domain, authorized, context, paths, store, payload,
                                      idempotency_key=authorized.call.idempotency_key)
    return DomainResult(202, result)
