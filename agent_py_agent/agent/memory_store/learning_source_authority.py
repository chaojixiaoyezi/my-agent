# LLM: 来源只读宿主消息与当前运行属性；不读正文、不接受模型自报来源，自动化和旧未知保持不可学。
# 模块用途: 为直接记忆与策展路径取得一致的宿主学习来源。
"""宿主消息及当前运行的学习资格；不读取正文、不接受模型自报来源。"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from ..conversation.learning_source import (
    derive_source_projection,
    facts_for_message,
    merge_message_sources,
    merge_source_projections,
    unknown_projection,
)
from ..conversation.models import MessageLogEntry
from ..io.cursor_cache import CursorCache, file_stamp

_REQUEST_ALIASES = ("gateway_request_id", "request_id", "conversation_request_id")
# 每轮只缓存64个线程快照的来源投影，不保存消息正文；淘汰仅恢复只读扫描。
_SOURCE_SNAPSHOT_CACHE_COUNT = 64


# LLM: 只经当前owner的ConversationStore读权威线程/消息；单次对象内缓存精确查询，无持久第二权威库。
# 类用途: 给直接工具和Curator提供一致的来源资格读取器。
class ConversationLearningSources:
    # LLM: owner由宿主传入；两份有界缓存均以原线程/消息五字段stat绑定，不形成第二权威库。
    # 函数用途: 绑定一次来源核对，只保存线程资格和请求投影，不驻留历史消息正文。
    def __init__(self, store: object, owner: str) -> None:
        self.store = store
        self.owner = owner
        self._threads = CursorCache(_SOURCE_SNAPSHOT_CACHE_COUNT)
        self._requests = CursorCache(_SOURCE_SNAPSHOT_CACHE_COUNT)

    # LLM: 线程资格随线程与消息文件共同失效；坏线程、身份错配或变更中的快照不得被旧缓存授权。
    # 函数用途: 读取精确线程来源默认值，稳定快照只读一次。
    def _thread(self, thread_id: str) -> object:
        key = self._snapshot_key(thread_id)
        cached = self._threads.get(key) if _stable_snapshot(key) else None
        if cached is not None:
            return cached[0]
        try:
            thread, error = self.store.threads.load_report(thread_id)
            thread = thread if not error and thread and thread.thread_id == thread_id else None
            if thread is not None:
                thread = SimpleNamespace(thread_id=thread.thread_id, learning_source=getattr(thread, "learning_source", None))
        except Exception:
            thread = None
        if key is not None and key != self._snapshot_key(thread_id):
            return None
        if _stable_snapshot(key):
            self._threads.put(key, (thread,))
        return thread

    # LLM: 已定位消息仍须核对真实thread；来源只读metadata及线程默认，不凭role/text授权。
    # 函数用途: 把一条真实消息转换为来源投影。
    def _message_source(self, message: object, thread_id: str) -> dict[str, object]:
        thread = self._thread(thread_id)
        if not self.owner or thread is None or getattr(message, "thread_id", "") != thread_id:
            return unknown_projection()
        facts = facts_for_message(getattr(message, "metadata", {}), owner=self.owner, thread_id=thread_id,
                                  thread_source=getattr(thread, "learning_source", None))
        return merge_message_sources([facts])

    # LLM: 消息引用必须精确匹配当前owner存储，不接受另一条可学消息代替缺失引用。
    # 函数用途: 按thread/message身份核对一条消息来源。
    def message(self, thread_id: str, message_id: str) -> dict[str, object]:
        if not thread_id or not message_id:
            return unknown_projection()
        try:
            message, errors = self.store.messages.by_id_report(thread_id, message_id)
        except Exception:
            return unknown_projection()
        if errors or message is None or getattr(message, "message_id", "") != message_id:
            return unknown_projection()
        return self._message_source(message, thread_id)

    # LLM: 一个固定EOF快照完整扫描后才发布全部request投影；重复、坏行、身份错配一律不可学。
    # 函数用途: 一次索引同线程所有请求，保留三种原别名并按文件变更失效。
    def request(self, thread_id: str, request_id: str) -> dict[str, object]:
        if not thread_id or not request_id:
            return unknown_projection()
        key = self._snapshot_key(thread_id)
        if key is None:
            return self._request_uncached(thread_id, request_id)
        index = self._requests.get(key) if _stable_snapshot(key) else None
        if index is None:
            index = self._stream_requests(thread_id, key)
            if _stable_snapshot(key) and key == self._snapshot_key(thread_id):
                self._requests.put(key, index)
        return dict(index.get(request_id) or unknown_projection())

    # LLM: 路径只取宿主Store的canonical API，五字段包括inode/ctime；缺失或替身无真实路径时不复用。
    # 函数用途: 同时绑定线程资格和消息快照，避免旧线程来源或旧请求匹配持续生效。
    def _snapshot_key(self, thread_id: str):
        try:
            storage = self.store.storage
            thread_path, message_path = storage.thread_path(thread_id), storage.message_path(thread_id)
            return (str(thread_path.absolute()), file_stamp(thread_path.stat()),
                    str(message_path.absolute()), file_stamp(message_path.stat()))
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    # LLM: 只读打开时固定EOF并核fstat，逐行解析但不保留正文；到EOF后仍检查线程/消息stamp，任何损坏丢弃全索引。
    # 函数用途: 一遍流式建立请求来源索引，较晚的重复或坏行也会使匹配保守关闭。
    def _stream_requests(self, thread_id: str, key) -> dict[str, dict]:
        thread = self._thread(thread_id)
        if not self.owner or thread is None:
            return {}
        index: dict[str, dict] = {}
        try:
            path = self.store.storage.message_path(thread_id)
            with path.open("rb") as handle:
                if file_stamp(os.fstat(handle.fileno())) != key[3]:
                    return {}
                for raw in _snapshot_lines(handle, key[3].size):
                    if not raw.strip():
                        continue
                    row = json.loads(raw.decode("utf-8"))
                    if not isinstance(row, dict):
                        return {}
                    message = MessageLogEntry.from_dict(row)
                    if (not message.message_id or not message.role or message.thread_id != thread_id
                            or (message.metadata.get("owner_id") not in (None, "", self.owner))):
                        return {}
                    if message.role != "user":
                        continue
                    facts = facts_for_message(message.metadata, owner=self.owner, thread_id=thread_id,
                                              thread_source=getattr(thread, "learning_source", None))
                    projection = merge_message_sources([facts])
                    for request_id in {str(message.metadata.get(name) or "") for name in _REQUEST_ALIASES} - {""}:
                        index[request_id] = unknown_projection() if request_id in index else projection
            return index if key == self._snapshot_key(thread_id) else {}
        except (OSError, UnicodeError, ValueError, TypeError):
            return {}

    # LLM: 仅无真实路径的替身沿原权威接口读取；真实文件走完整快照单遍，不反复物化历史消息。
    # 函数用途: 保留无文件Store的来源核验契约，异常和歧义仍关闭。
    def _request_uncached(self, thread_id: str, request_id: str) -> dict[str, object]:
        if not thread_id or not request_id:
            return unknown_projection()
        try:
            messages, errors = self.store.messages.recent_report(thread_id, limit=0)
            matches = [message for message in messages if _matches_request(message, request_id)]
            if not errors and len(matches) == 1:
                return self._message_source(matches[0], thread_id)
        except Exception:
            pass
        return unknown_projection()


# LLM: 原三种request字段保持精确字符串匹配，非法metadata交由外层拒绝，不猜正文。
# 函数用途: 判断一条消息是否是目标请求的宿主输入。
def _matches_request(message: object, request_id: str) -> bool:
    return getattr(message, "role", "") == "user" and request_id in {
        str((getattr(message, "metadata", {}) or {}).get(name) or "")
        for name in _REQUEST_ALIASES
    }


# LLM: 单次来源对象内以两个文件完整五字段stat绑定，首次固定EOF扫描且读后复核；新鲜时间不引发同快照重扫。
# 函数用途: 判断是否拥有完整的线程与消息快照键，缺任一文件不能缓存。
def _stable_snapshot(key) -> bool:
    return key is not None


# LLM: 固定原字节EOF，之后追加留待下一次；保CR/CRLF/LF边界而不拆开UTF-8文本里的NEL。
# 函数用途: 逐物理行读取已冻结的消息快照，内存不随历史行数增长。
def _snapshot_lines(handle, through: int):
    while handle.tell() < through:
        raw = handle.readline(through - handle.tell())
        if not raw:
            break
        yield from raw.splitlines(keepends=True)


# LLM: 当前运行属性只能约束当前run内的工具；异run证据未知关闭；消息与运行两侧任何禁止事实均保留。
#   没有ConversationStore的直接CLI仅能使用宿主显式运行投影，缺失仍关闭，不从文本补造。
# 函数用途: 为remember附加完整宿主来源资格，避免空投影绕过晋升来源闸。
def remember_source_projection(agent: object, message_refs: tuple, tool_refs: tuple) -> dict[str, object]:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    run_id = str(getattr(current, "run_id", "") or "")
    if not owner or any(ref.get("run_id") != run_id or ref.get("owner_id") != owner for ref in tool_refs):
        return unknown_projection()
    sources = ConversationLearningSources(getattr(agent, "conversation_store", None), owner)
    projections = [sources.message(str(ref.get("thread_id") or ""), str(ref.get("message_id") or "")) for ref in message_refs]
    thread_id = str(attrs.get("conversation_thread_id") or "")
    if not projections and thread_id:
        projections.append(sources.request(thread_id, str(getattr(current, "request_id", "") or "")))
    runtime_source = derive_source_projection(attrs)
    if runtime_source is not None:
        projections.append(runtime_source)
    elif "learning_source" in attrs:
        projections.append(unknown_projection())
    return merge_source_projections(projections) if projections else unknown_projection()
