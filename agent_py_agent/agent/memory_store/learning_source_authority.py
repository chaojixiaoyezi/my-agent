# LLM: 来源只读宿主消息与当前运行属性；不读正文、不接受模型自报来源，自动化和旧未知保持不可学。
# 模块用途: 为直接记忆与策展路径取得一致的宿主学习来源。
"""宿主消息及当前运行的学习资格；不读取正文、不接受模型自报来源。"""
from __future__ import annotations

from ..conversation.learning_source import (
    derive_source_projection,
    facts_for_message,
    merge_message_sources,
    merge_source_projections,
    unknown_projection,
)


# LLM: 只经当前owner的ConversationStore读权威线程/消息；单次对象内缓存精确查询，无持久第二权威库。
# 类用途: 给直接工具和Curator提供一致的来源资格读取器。
class ConversationLearningSources:
    # LLM: owner由宿主传入；模型不能选择store或owner；缓存只覆盖本次收集。
    # 函数用途: 绑定一次来源核对的宿主上下文。
    def __init__(self, store: object, owner: str) -> None:
        self.store = store
        self.owner = owner
        self._threads: dict[str, object] = {}
        self._requests: dict[tuple[str, str], dict[str, object]] = {}

    # LLM: 坏线程、读错或身份不符均返回空；不得把错误吞成可学默认值。
    # 函数用途: 读取精确线程来源默认值。
    def _thread(self, thread_id: str) -> object:
        if thread_id not in self._threads:
            try:
                thread, error = self.store.threads.load_report(thread_id)
                self._threads[thread_id] = thread if not error and thread and thread.thread_id == thread_id else None
            except Exception:
                self._threads[thread_id] = None
        return self._threads[thread_id]

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

    # LLM: 缓存只保存精确请求的原核验结果；查询拆平不改变坏来源拒绝策略。
    #   缺引用、歧义或损坏均不可学；不把同线程其他回合来源借给本请求。
    # 函数用途: 从精确请求身份核对工具事件的触发来源。
    def request(self, thread_id: str, request_id: str) -> dict[str, object]:
        key = (thread_id, request_id)
        if key not in self._requests:
            self._requests[key] = self._request_uncached(thread_id, request_id)
        return self._requests[key]

    # LLM: request必须匹配唯一宿主输入消息；role仅用于选择输入，异常/歧义仍不可学。
    # 函数用途: 查询尚未核验的请求来源，不借同线程其它回合授权。
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
        for name in ("gateway_request_id", "request_id", "conversation_request_id")
    }


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
