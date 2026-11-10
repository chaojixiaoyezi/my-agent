# LLM: 原认证 scope 决定唯一 owner Store；列表只读绑定快照和有界候选/消息窗口，定点历史仍走原绑定权限与分页叶子。
# 模块用途: 给现有 history 路由提供 owner 会话列表及列表项打开能力，不创建冷目录或业务 Agent。
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import replace

from ..plugin_host.business_action_specs import BUSINESS_LIST_DEFAULT_ROWS, BUSINESS_LIST_MAX_ROWS

_COUNT_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int], int]] = OrderedDict()
_COUNT_LOCK = threading.Lock()


# LLM: owner 仅由原 scope resolver 解析；C 首末验证原 transport，Count0 沿原被动 Store wrapper，不回退其他 owner。
# 函数用途: 返回当前 owner 的只读会话库、公开 owner ID 和读取后的来源复核函数。
def _owner_store(server, scope):
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
    from .control_plane import GatewayControlPlane
    from .control_service import _scope_request_payload, resolve_gateway_scope_owner
    from .multiprocess_http_transport import require_http_transport
    from .owner_conversation_store import (
        owner_conversation_store,
        owner_conversation_store_from_config,
    )
    from .request_worker import _resolve_request_owner_from_config

    if getattr(server, "control_plane", None) is not None or getattr(server, "drain_runtime", None) is not None:
        control, runtime, paths = server.control_plane, server.drain_runtime, server.paths
        if type(control) is not GatewayControlPlane:
            raise ValueError("http_original_control_required")
        require_http_transport(runtime, control=control, paths=paths)
        owner = _resolve_request_owner_from_config(control.config, _scope_request_payload(scope), control.home_root)
        base_home = home_paths(control.home_root)
        scoped_home = home_paths_with_owner(base_home, resolve_owner_home(control.home_root, owner))
        config = control.config
        if owner != owner_identity_from_config(config):
            fields = resolve_runtime_paths_for_agent(config, base_home.owner_home_dir, base_home).paths
            config = replace(config, **{name: "" for name in fields if hasattr(config, name)})
        store = owner_conversation_store_from_config(config, scoped_home, initialize=False)

        # LLM: 复核捕获的同一来源，不允许读到一半切换、关闭或替换 C 后继续返回正文。
        # 函数用途: 在只读结果返回前检查原控制面、runtime 和 paths。
        def recheck():
            if (server.control_plane is not control or server.drain_runtime is not runtime or server.paths is not paths):
                raise ValueError("http_original_control_required")
            require_http_transport(runtime, control=control, paths=paths)

        return store, scoped_home.owner_id, recheck
    base = server.agent
    owner = resolve_gateway_scope_owner(base, scope)
    result = resolve_owner_home(base.home_paths.root, owner)
    home = home_paths_with_owner(base.home_paths, result)
    store = owner_conversation_store(base, home, initialize=False)

    # LLM: 原单进程没有 coordinator transport；读取仍由已解析 owner Store 负责。
    # 函数用途: 保持两种执行模式一致的返回形状。
    def recheck():
        return None

    return store, home.owner_id, recheck


# LLM: 游标绑定当前渠道、用户与访问级别；摘要不是授权，owner 仍须由原请求身份解析。
# 函数用途: 为列表游标生成稳定的 viewer 标记。
def _viewer_digest(channel, user_id, all_user_access):
    value = channel + "\x1f" + user_id + "\x1f" + ("1" if all_user_access else "0")
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# LLM: 排序键只含目录快照时间与绑定身份，游标不携带正文、路径或 owner 选择权。
# 函数用途: 把分页位置编码为不带填充的 base64url JSON。
def _encode_cursor(owner_id, viewer_digest, key):
    raw = json.dumps(["s1", owner_id, viewer_digest, *key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# LLM: 任何格式、类型、版本、owner 或 viewer 不符均拒绝，不将坏游标降级成第一页。
# 函数用途: 解码并验证同一 owner/viewer 的列表位置。
def _decode_cursor(cursor, owner_id, viewer_digest):
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, list) or len(value) != 7 or value[0] != "s1"
                or not isinstance(value[1], str) or value[1] != owner_id
                or not isinstance(value[2], str) or value[2] != viewer_digest
                or type(value[3]) is not int or not all(isinstance(part, str) for part in value[4:])):
            raise ValueError
        return tuple(value[3:])
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise ValueError("invalid session cursor") from exc


# LLM: 使用原 no-follow 普通文件 opener；读取量固定为尾窗，不跟随链接、不阻塞 FIFO，也不调用历史分页。
# 函数用途: 从最近用户或助手消息生成空白折叠后的短预览。
def _preview(path, tail_bytes, chars):
    from .client_inflight_projection import _open_chunk_file

    try:
        with _open_chunk_file(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - max(0, tail_bytes))
            handle.seek(start)
            raw = handle.read(max(0, tail_bytes))
        if start:
            raw = raw.partition(b"\n")[2]
        for line in reversed(raw.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            if not isinstance(role, str) or role not in {"user", "assistant"}:
                continue
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                return " ".join(content.split())[:max(0, chars)]
    except OSError:
        return ""
    return ""


# LLM: 延迟导入原 canonical marker，不另造计数定义；保留单一可观测调用点供预算回归统计真实扫描。
# 函数用途: 沿用冷段计数加热文件行数的唯一消息计数口径。
def canonical_message_tail_marker(path, thread_id):
    from ..conversation.store_messages import canonical_message_tail_marker as marker

    try:
        return marker(path, thread_id)
    finally:
        if isinstance(path, _CountPath):
            path.close()


# LLM: 只包装当前计数调用的热FD；所有read/readline共享页预算，包括前缀校验、尾界扫描和布局重读。
# 类用途: 限制原canonical marker实际消费的热消息字节，不修改其解析或计数口径。
class _CountReader:
    # LLM: 预算属于调用栈，不保存在Store或全局缓存；FD仍由原reader上下文关闭。
    # 函数用途: 绑定一个只读热文件句柄与共享余额。
    def __init__(self, handle, remaining):
        self.handle, self.remaining = handle, remaining

    # LLM: 只读marker的定位、身份和清理操作仍委托原句柄；正文读取由显式方法限额。
    # 函数用途: 保留fileno、seek、tell、close等原文件接口。
    def __getattr__(self, name):
        return getattr(self.handle, name)

    # LLM: 不重开或复制FD。
    # 函数用途: 进入当前只读句柄上下文。
    def __enter__(self):
        return self

    # LLM: 无论预算不足或解析失败都关闭原FD。
    # 函数用途: 结束当前只读句柄上下文。
    def __exit__(self, exc_type, exc_value, traceback):
        self.handle.close()

    # LLM: 余额耗尽不能假装EOF；真实EOF允许零字节结束，避免完整扫描被误报为坏账。
    # 函数用途: 为一次热文件读取计算允许消费的字节数。
    def _size(self, size):
        if size == 0:
            return 0
        if self.remaining[0] <= 0 and self.handle.tell() < os.fstat(self.handle.fileno()).st_size:
            raise OSError("message_count_budget")
        return self.remaining[0] if size < 0 else min(size, self.remaining[0])

    # LLM: 原尾界/前缀的块读也逐次扣账；不足产生短读，由canonical reader保守拒绝。
    # 函数用途: 在剩余预算内读取一个字节块。
    def read(self, size=-1):
        raw = self.handle.read(self._size(size))
        self.remaining[0] -= len(raw)
        return raw

    # LLM: 即使单行或文件在stat后增长，也不允许越过共享字节预算。
    # 函数用途: 在预算内读取一行，保留原canonical行校验。
    def readline(self, size=-1):
        raw = self.handle.readline(self._size(size))
        self.remaining[0] -= len(raw)
        return raw

    # LLM: for循环不能绕过readline预算。
    # 函数用途: 返回受限行迭代器。
    def __iter__(self):
        return self

    # LLM: 仅真实空读是EOF，预算不足仍抛错而不是接受截断消息计数。
    # 函数用途: 读取下一条原始消息行。
    def __next__(self):
        raw = self.readline()
        if not raw:
            raise StopIteration
        return raw


# LLM: 原marker仅通过path.open读取热文件；其它规范路径、manifest和stat操作保留原Path，不改全局reader。
# 类用途: 为单次计数的热文件打开操作附加预算，不创建文件或改变Store。
class _CountPath:
    # LLM: 每个实例只活在一次marker调用中。
    # 函数用途: 保存原规范路径与当前页预算。
    def __init__(self, path, remaining):
        self.path, self.remaining = path, remaining
        self.readers = []

    # LLM: manifest定位和身份检查仍使用原规范Path。
    # 函数用途: 委托stem、parent、with_suffix、stat等路径操作。
    def __getattr__(self, name):
        return getattr(self.path, name)

    # 路径元数据核验可取原规范名字，但正文打开仍必须留在本包装器的共享预算内。
    def __fspath__(self):
        return os.fspath(self.path)

    # LLM: marker只需要rb；复用no-follow普通文件opener，拒绝竞态链接或FIFO。
    # 函数用途: 返回受当前页字节预算保护的热文件句柄。
    def open(self, mode):
        from .client_inflight_projection import _open_chunk_file

        if mode != "rb":
            raise ValueError("message_count_read_only")
        reader = _CountReader(_open_chunk_file(self.path), self.remaining)
        self.readers.append(reader)
        return reader

    # LLM: TranscriptReader初始化途中预算不足也须释放已开FD，不依赖构造完成或垃圾回收。
    # 函数用途: 关闭本次marker调用打开过的全部热文件句柄。
    def close(self):
        for reader in self.readers:
            reader.close()


# LLM: 缓存只投影同一路径的文件身份和版本，不是权威；预算不够或 marker 失败明确返回未知，不改文件或建索引。
# 函数用途: 在每次调用的共享字节预算内统计消息数，并维护有界缓存。
def _message_count(path, thread_id, remaining, max_cached):
    try:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return canonical_message_tail_marker(_CountPath(path, remaining), thread_id)["message_count"]
        if not stat.S_ISREG(info.st_mode):
            return None
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        key = str(path)
        with _COUNT_LOCK:
            cached = _COUNT_CACHE.get(key)
            if cached is not None and cached[0] == stamp:
                _COUNT_CACHE.move_to_end(key)
                return cached[1]
        if info.st_size > remaining[0]:
            return None
        count = canonical_message_tail_marker(_CountPath(path, remaining), thread_id)["message_count"]
        with _COUNT_LOCK:
            _COUNT_CACHE[key] = stamp, count
            _COUNT_CACHE.move_to_end(key)
            while len(_COUNT_CACHE) > max(0, max_cached):
                _COUNT_CACHE.popitem(last=False)
        return count
    except Exception:  # noqa: BLE001 消息计数是可选投影，失败必须保留未知而不破坏列表。
        return None


# LLM: 目录或原 C 来源不可用时丢弃整页，不泄露路径或异常原文，也不借其他 owner 的库补结果。
# 函数用途: 返回固定的列表不可用回执。
def _catalog_failure(owner_id, error):
    from .client_service import _safe_load_error

    return {"ok": False, "operation": "list", "owner_id": owner_id, "items": [], "next_cursor": None,
            "coverage": "unknown", "inflight_coverage": "skipped", "load_errors": [
                {"code": "CLIENT_SESSION_CATALOG_UNAVAILABLE", "error": _safe_load_error(error)}]}


# LLM: 一次读取绑定快照，每个可见绑定仅 lstat；按裁决每页最多 load limit 个候选并校验线程内绑定，不重读目录。
# 函数用途: 列出已选 owner 的一页会话，合并有界预览、计数、claim 与一次 Gateway 在途投影。
def list_owner_sessions(
    store, *, owner_id: str, viewer_user_id: str, viewer_channel: str, all_user_access: bool,
    limit: int = BUSINESS_LIST_DEFAULT_ROWS, cursor: str | None = None, inflight=None,
    now: float | None = None, preview_tail_bytes: int = 65_536, preview_chars: int = 160,
    max_count_scan_bytes: int = 8_388_608, max_cached_counts: int = 1024,
) -> dict:
    from .client_service import _safe_load_error

    viewer = _viewer_digest(viewer_channel, viewer_user_id, all_user_access)
    after = _decode_cursor(cursor, owner_id, viewer) if cursor is not None else None
    bindings, error = store.threads._read_bindings_report()
    if error is not None:
        return _catalog_failure(owner_id, error)
    candidates = []
    for key, thread_id in bindings.items():
        parts = key.split("\x1f")
        if len(parts) != 3:
            continue
        channel, conversation_id, user = parts
        if not all_user_access and (channel != viewer_channel or user != viewer_user_id):
            continue
        try:
            info = os.lstat(store.storage.thread_path(thread_id))
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode):
            candidates.append((info.st_mtime_ns, conversation_id, channel, user, thread_id))
    candidates.sort(reverse=True)
    if after is not None:
        candidates = [row for row in candidates if row[:4] < after]
    items, errors, page_entries, claims = [], [], {}, {}
    remaining = [max(0, max_count_scan_bytes)]
    window = candidates[:limit]
    for _, conversation_id, channel, user, thread_id in window:
        thread, error = store.threads.load_report(thread_id)
        if error is not None:
            errors.append({"code": "CLIENT_SESSION_LOAD_FAILED", "conversation_id": conversation_id,
                           "error": _safe_load_error(error)})
            continue
        if thread is None or thread.status == "archived" or thread.thread_id != thread_id:
            continue
        if not any(binding.channel == channel and binding.channel_conversation_id == conversation_id
                   and binding.channel_user_id == user for binding in thread.channel_bindings):
            continue
        key = "\x1f".join((channel, conversation_id, user))
        path = store.storage.message_path(thread.thread_id)
        claim, claim_error = store.claims.load_report(thread.thread_id)
        if claim_error is not None:
            errors.append({"code": "CLIENT_SESSION_LOAD_FAILED", "conversation_id": conversation_id,
                           "error": _safe_load_error(claim_error)})
            claim = {}
        claims[key] = claim if isinstance(claim, dict) else {}
        page_entries[key] = channel, conversation_id, user, thread.thread_id
        items.append({"conversation_id": conversation_id, "thread_id": thread.thread_id, "owner_id": owner_id,
                      "channel": channel, "channel_user_id": user, "title": thread.title,
                      "preview": _preview(path, preview_tail_bytes, preview_chars),
                      "created_at": thread.created_at, "updated_at": thread.updated_at,
                      "message_count": _message_count(path, thread.thread_id, remaining, max_cached_counts)})
    found, inflight_coverage = ({}, "skipped") if inflight is None else inflight(page_entries)
    stamp = time.time() if now is None else now
    for item in items:
        key = "\x1f".join((item["channel"], item["conversation_id"], item["channel_user_id"]))
        claim, gateway = claims[key], found.get(key, {})
        try:
            claim_running = claim.get("status") == "running" and float(claim.get("expires_at") or 0) > stamp
        except (ValueError, TypeError, OverflowError):
            claim_running = False
        active = {}
        if gateway.get("status") == "running":
            status, active = "running", gateway
        elif claim_running:
            status = "running"
        elif gateway.get("status") == "queued":
            status, active = "queued", gateway
        else:
            status = "failed" if claim.get("status") == "failed" else "idle"
        item.update(status=status, in_flight=status in {"running", "queued"},
                    active_request_id=active.get("active_request_id") or None,
                    active_run_id=active.get("active_run_id") or None)
    # 跳过坏候选也前移页位置：遵守裁决的每页线程读取上限，避免空页反复返回同一候选。
    next_cursor = (_encode_cursor(owner_id, viewer, window[-1][:4])
                   if window and len(candidates) > len(window) else None)
    return {"ok": True, "operation": "list", "owner_id": owner_id, "items": items, "next_cursor": next_cursor,
            "coverage": "partial" if errors else "complete", "inflight_coverage": inflight_coverage,
            "load_errors": errors}


# LLM: wrapper 共用唯一 owner Store 与 scope；在途入口只消费当页绑定，读完仍须验证 C 原来源。
# 函数用途: 为 HTTP 与后续业务调用返回相同的 owner 会话列表。
def gateway_client_session_list(
    server, scope, *, limit: int = BUSINESS_LIST_DEFAULT_ROWS, cursor: str | None = None,
    include_inflight: bool = True,
) -> dict:
    owner_id = ""
    try:
        store, owner_id, recheck = _owner_store(server, scope)
        inflight = None
        if include_inflight:
            from .client_inflight_projection import client_inflight_index

            # LLM: 保留原 metadata/resolved_owner，只替换当前页真实绑定身份；不从外部正文选择 owner。
            # 函数用途: 把一页会话绑定转换为原 Gateway scope 后批量读取在途状态。
            def inflight(entries):
                return client_inflight_index(server, scope, {
                    key: (replace(scope, channel=c, conversation_id=conv, user_id=u,
                                  resolved_owner=scope.resolved_owner), tid)
                    for key, (c, conv, u, tid) in entries.items()})

        body = list_owner_sessions(store, owner_id=owner_id, viewer_user_id=scope.user_id,
                                   viewer_channel=scope.channel, all_user_access=bool(scope.all_user_access),
                                   limit=limit, cursor=cursor, inflight=inflight)
        recheck()
        return body
    except ValueError as exc:
        if str(exc) == "invalid session cursor":
            raise
        return _catalog_failure(owner_id, exc)
    except Exception as exc:  # noqa: BLE001 公开固定错误投影，不泄露内部来源或路径。
        return _catalog_failure(owner_id, exc)


# LLM: list 有独立有界游标，不受旧历史游标影响；权限仍由原 middleware 和 _gateway_control_scope 决定。
# 函数用途: 验证列表请求参数，并沿现有 HTTP 路由返回列表。
def handle_client_session_list(handler, server, body: dict, *, user_id: str, channel: str) -> None:
    from .http_handlers import _gateway_control_scope

    limit = body.get("limit", BUSINESS_LIST_DEFAULT_ROWS)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        handler._send_json(400, {"error": "invalid session list limit"})
        return
    cursor = body.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
        handler._send_json(400, {"error": "invalid session cursor"})
        return
    include_inflight = body.get("include_inflight", True)
    if not isinstance(include_inflight, bool):
        handler._send_json(400, {"error": "invalid include_inflight"})
        return
    scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
    try:
        result = gateway_client_session_list(server, scope, limit=min(BUSINESS_LIST_MAX_ROWS, limit),
                                             cursor=cursor, include_inflight=include_inflight)
    except ValueError as exc:
        if str(exc) != "invalid session cursor":
            raise
        handler._send_json(400, {"error": "invalid session cursor"})
        return
    handler._send_json(200, result)


# LLM: target 只在已解析 owner Store 内查找；绑定必须同 conversation，普通用户还须匹配原渠道和 actor。
# 函数用途: 校验列表中的线程与当前绑定后，使用原历史分页叶子读取正文。
def _history_from_target(store, server, scope, target_thread_id, *, max_turns, before):
    from .client_inflight_projection import client_inflight_projection
    from .client_service import (
        GatewayClientHistoryResult,
        _safe_load_error,
        read_gateway_client_history_from_store,
    )

    try:
        thread, error = store.threads.load_report(target_thread_id)
    except ValueError:
        return GatewayClientHistoryResult(True)
    if error is not None:
        return GatewayClientHistoryResult(False, load_errors=(_safe_load_error(error),))
    if thread is None:
        return GatewayClientHistoryResult(True)
    candidates = [binding for binding in thread.channel_bindings
                  if binding.channel_conversation_id == scope.conversation_id
                  and (scope.all_user_access or (binding.channel == scope.channel
                       and binding.channel_user_id == scope.user_id))]
    if not candidates:
        return GatewayClientHistoryResult(True)
    exact = next((binding for binding in candidates if binding.channel == scope.channel
                  and binding.channel_user_id == scope.user_id), None)
    binding = exact if exact is not None else min(candidates, key=lambda item: (item.channel, item.channel_user_id))
    resolved, error = store.threads.resolve_report(channel=binding.channel,
        channel_conversation_id=binding.channel_conversation_id, channel_user_id=binding.channel_user_id)
    if error is not None:
        return GatewayClientHistoryResult(False, load_errors=(_safe_load_error(error),))
    if resolved is None or resolved.thread_id != target_thread_id:
        return GatewayClientHistoryResult(True)
    bound = replace(scope, channel=binding.channel, user_id=binding.channel_user_id)
    result = read_gateway_client_history_from_store(store, scope=bound, max_turns=max_turns,
                                                    before_message_cursor=before)
    if result.ok and result.thread_id:
        result = replace(result, **client_inflight_projection(server, bound, result.thread_id))
    return result


# LLM: 与旧 coordinator 历史共用来源首末门和安全错误投影；空/拒绝结果也复核，不初始化 foreign Agent。
# 函数用途: 为同 owner 的指定线程读取历史并附加原在途字段。
def read_client_history_by_thread(server, scope, target_thread_id: str, *, max_turns: int, before: int | None):
    from .client_service import GatewayClientHistoryResult, _safe_load_error

    try:
        store, _owner_id, recheck = _owner_store(server, scope)
        result = _history_from_target(store, server, scope, target_thread_id, max_turns=max_turns, before=before)
        recheck()
        return result
    except Exception as exc:  # noqa: BLE001 历史读取只公开固定错误类别，不泄露路径或异常原文。
        return GatewayClientHistoryResult(False, load_errors=(_safe_load_error(exc),))


# LLM: 只有 target_thread_id 是新增 selector；旧 thread_id/session_id 别名和历史游标验证保持原样。
# 函数用途: 验证指定线程 ID，并返回原 history 合同形状。
def handle_client_history_by_thread(handler, server, body: dict, scope, *, before: int | None) -> None:
    from .http_handlers import _request_limit

    target = body.get("target_thread_id")
    if not isinstance(target, str) or not target.strip() or len(target) > 256:
        handler._send_json(400, {"error": "invalid target_thread_id"})
        return
    result = read_client_history_by_thread(server, scope, target.strip(),
                                           max_turns=_request_limit(body, default=20), before=before)
    handler._send_json(200, result.to_dict())
