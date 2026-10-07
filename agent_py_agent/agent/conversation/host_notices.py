# LLM: 宿主提示（host notice）的唯一存取点。待送达的提示只存在会话线程记录的 pending_host_notices 上（通用字段，
#   不为某个来源专设），由同一会话下一次前台回复提交规范最终消息时按编号取走，放进最终消息元数据 host_notices 与
#   Gateway 结果 channel_delivery.host_notices；提交即算已读（集成者 2026-09-27 定的 B 口径）。模型上下文不含它
#   （models.MODEL_HIDDEN_THREAD_FIELDS）。正文由宿主生成，source / code 是结构化事实，同一来源的新提示替换旧的。
#   本轮工具用 queue_turn_host_notice 排的提示在 details 里带本轮 Gateway 请求编号（turn_request_id），由 request_execution
#   收尾时随当轮回复一起发布（turn_host_notices）；别的来源在回合中途到的提示仍留给下一轮。
#   改动须同步 test_host_notices.py、gateway_parts/request_history、stream_writer、adapter/manager 与各显示出口。
# 模块用途: 保存、读取、取走与清除会话的宿主提示，并把提示渲染成 IM 正文前的提示行。
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from uuid import uuid4

from .authority import current_conversation_task_attributes

# 每个会话最多暂存的提示条数（同一来源只留最新一条，超出时丢最旧的）；正文与名字的长度上限。
HOST_NOTICE_LIMIT_COUNT = 5
# 宿主提示正文最多 500 字符：超长截断。
_HOST_NOTICE_TEXT_LIMIT_CHARS = 500
# 宿主提示来源名最多 64 字符：超长截断。
_NAME_LIMIT_CHARS = 64
# 宿主提示详情最多 200 字符：超长截断，保持提示紧凑。
_DETAIL_LIMIT_CHARS = 200
_LINE_PREFIX = "【提示】"
# 本轮工具排的提示在 details 里记本轮 Gateway 请求编号的键名；收尾按它挑出要随当轮回复发布的提示。
TURN_REQUEST_DETAIL = "turn_request_id"


# LLM: 四个字段都是宿主写入的纯文本；notice_id 是取走与去重的唯一依据，source / code 供测试和同来源替换，不参与模型输入。
#   details 是可选的结构化事实（键、值都是短字符串，例如唤醒毒丸提示里的 message_dedupe_key），给程序读，文案只作展示；
#   为空时 to_dict 不写这个键，旧提示的字典形状不变。
# 类用途: 一条要在下一条回复顶部告诉用户的宿主提示。
@dataclass(frozen=True)
class HostNotice:
    notice_id: str
    source: str
    code: str
    text: str
    details: tuple[tuple[str, str], ...] = ()

    # 函数用途: 转成可持久化、可放进结果载荷的字典。
    def to_dict(self) -> dict[str, object]:
        row: dict[str, object] = {"notice_id": self.notice_id, "source": self.source, "code": self.code, "text": self.text}
        if self.details:
            row["details"] = dict(self.details)
        return row


# LLM: 正文去掉控制字符、合并空白并限长；来源与代码只保留有限长度；details 的键与值同样清洗限长，空键丢弃。生成新的随机编号。
# 函数用途: 由宿主代码创建一条新提示。
def host_notice(source: str, code: str, text: str, *, details: Mapping[str, object] | None = None) -> HostNotice:
    return HostNotice(uuid4().hex[:12], _clean(source, _NAME_LIMIT_CHARS), _clean(code, _NAME_LIMIT_CHARS), _clean(text, _HOST_NOTICE_TEXT_LIMIT_CHARS),
                      _details(details))


# 函数用途: 把结构化事实清洗成排好序的 (键, 值) 元组；不是映射时为空。
def _details(value: object) -> tuple[tuple[str, str], ...]:
    rows = value.items() if isinstance(value, Mapping) else ()
    cleaned = ((_clean(key, _NAME_LIMIT_CHARS), _clean(item, _DETAIL_LIMIT_CHARS)) for key, item in rows)
    return tuple(sorted((key, item) for key, item in cleaned if key))


# 函数用途: 去掉控制字符、合并空白并截到上限。
def _clean(value: object, limit: int) -> str:
    text = "".join(char if char.isprintable() else " " for char in str(value or ""))
    return " ".join(text.split())[:limit]


# LLM: 读旧记录或外部载荷时宽容解析：缺编号、来源或正文的条目直接丢弃，不补造。只读。
#   传进来的常常是持久化字典，但 take_host_notices 返回的本来就是 HostNotice（真实回复出口就是这样取的），
#   所以两种都要认：只认字典会让取走的提示在渲染时被整条丢掉（文案写不出去却也不报错）。
# 函数用途: 把一组提示（字典或 HostNotice）解析成提示，坏条目跳过。
def host_notices_from(values: object) -> tuple[HostNotice, ...]:
    rows = values if isinstance(values, (list, tuple)) else ()
    notices = (row if isinstance(row, HostNotice) else _notice_from(row) for row in rows if isinstance(row, (Mapping, HostNotice)))
    return tuple(notice for notice in notices if notice is not None)


# 函数用途: 解析一条提示字典，缺关键字段时返回 None。
def _notice_from(row: Mapping) -> HostNotice | None:
    notice = HostNotice(*(_clean(row.get(key), _HOST_NOTICE_TEXT_LIMIT_CHARS if key == "text" else _NAME_LIMIT_CHARS)
                          for key in ("notice_id", "source", "code", "text")), _details(row.get("details")))
    return notice if notice.notice_id and notice.source and notice.text else None


# LLM: 线程锁内读改写；默认同一来源的旧提示被替换，replace_same_code=True 时只替换同来源且同 code 的旧提示
#   （同一来源会发几类提示、每类只留最新一条时用，例如唤醒毒丸按原因码合并），超过上限丢最旧的。线程不存在或不可写时
#   返回 False，不抛异常（调用方多在后台线程里，提示是附带信息，不能拖垮主流程）。副作用：改写会话线程记录。
# 函数用途: 给一个会话追加一条待送达的宿主提示。
def queue_host_notice(store: object, thread_id: str, notice: HostNotice, *, replace_same_code: bool = False) -> bool:
    # 函数用途: 判断一条旧提示是否被这条新提示替换。
    def replaced(row: HostNotice) -> bool:
        return row.source == notice.source and (not replace_same_code or row.code == notice.code)

    # LLM: 在线程锁内执行；只动 pending_host_notices。
    # 函数用途: 替换同来源（或同来源同 code）旧提示并追加新提示。
    def update(thread):
        kept = [row for row in host_notices_from(thread.pending_host_notices) if not replaced(row)]
        rows = (*kept, notice)[-HOST_NOTICE_LIMIT_COUNT:]
        return replace(thread, pending_host_notices=tuple(row.to_dict() for row in rows))

    return _update(store, thread_id, update)


# LLM: 给工具用：会话编号只读本轮 task_attributes 的结构化 conversation_thread_id，请求编号只读本轮运行参数 request_id（Gateway
#   前台回合就是 Gateway 请求编号），不从参数或正文接收。有请求编号时记进 details.turn_request_id，收尾随当轮回复发布；
#   没有（比如后台续跑）就和普通提示一样留到下一条前台回复。没有会话或写不进去时返回 None，不抛异常。副作用：改写会话线程记录。
# 函数用途: 让工具把"用户必须知道的事"排成本轮的宿主提示，随这一轮回复直接显示给用户，不经模型转告。
def queue_turn_host_notice(agent: object, source: str, code: str, text: str, *,
                           details: Mapping[str, object] | None = None) -> HostNotice | None:
    thread_id = str(current_conversation_task_attributes(agent).get("conversation_thread_id") or "").strip()
    request_id = str(getattr(getattr(agent, "_current_run_params", None), "request_id", "") or "").strip()
    if not thread_id:
        return None
    facts = {**(details or {}), **({TURN_REQUEST_DETAIL: request_id} if request_id else {})}
    notice = host_notice(source, code, text, details=facts)
    return notice if queue_host_notice(getattr(agent, "conversation_store", None), thread_id, notice) else None


# LLM: 只读；挑出 details.turn_request_id 等于给定请求编号的待送达提示（本轮工具排的），请求编号为空时返回空。
# 函数用途: 读出本轮工具排的、要随当轮回复一起发布的提示。
def turn_host_notices(store: object, thread_id: str, request_id: str) -> tuple[HostNotice, ...]:
    if not request_id:
        return ()
    return tuple(row for row in pending_host_notices(store, thread_id) if dict(row.details).get(TURN_REQUEST_DETAIL) == request_id)


# 函数用途: 读取一个会话的待送达提示；读不到时返回空。只读。
def pending_host_notices(store: object, thread_id: str) -> tuple[HostNotice, ...]:
    try:
        thread = store.threads.load(thread_id)
    except Exception:  # noqa: BLE001 - 读不到提示不能影响回合。
        return ()
    return host_notices_from(getattr(thread, "pending_host_notices", ()))


# LLM: 只取走给定编号的提示（就是本轮已经在流里发布过的那些），本轮期间新到的提示留给下一轮；返回真正取走的。
#   这一步就是“已读”。副作用：改写会话线程记录。
# 函数用途: 从待送达列表里取走本轮要随最终回复送出的提示。
def take_host_notices(store: object, thread_id: str, notice_ids: Iterable[str]) -> tuple[HostNotice, ...]:
    wanted = {str(item) for item in notice_ids if str(item or "")}
    taken: list[HostNotice] = []

    # LLM: 在线程锁内执行；记下取走的条目供外层返回，只动 pending_host_notices。
    # 函数用途: 移除给定编号的提示。
    def update(thread):
        rows = host_notices_from(thread.pending_host_notices)
        taken[:] = [row for row in rows if row.notice_id in wanted]
        return replace(thread, pending_host_notices=tuple(row.to_dict() for row in rows if row.notice_id not in wanted))

    return tuple(taken) if wanted and _update(store, thread_id, update) else ()


# LLM: 用户已经通过别的入口看到了这个来源的结论（如 /effort 查看），就清掉同来源的待送达提示，避免重复。副作用：改写会话线程记录。
# 函数用途: 清除一个会话里某个来源的全部待送达提示。
def clear_host_notices(store: object, thread_id: str, source: str) -> bool:
    # LLM: 在线程锁内执行；只动 pending_host_notices。
    # 函数用途: 移除这个来源的全部提示。
    def update(thread):
        rows = host_notices_from(thread.pending_host_notices)
        return replace(thread, pending_host_notices=tuple(row.to_dict() for row in rows if row.source != source))

    return _update(store, thread_id, update)


# 函数用途: 在线程锁内应用一次更新；线程不存在或存储出错时返回 False。
def _update(store: object, thread_id: str, update) -> bool:
    if store is None or not thread_id:
        return False
    try:
        store.threads.update_atomic(thread_id, update)
    except Exception:  # noqa: BLE001 - 提示写不进去只是少一行提示，不能影响调用方。
        return False
    return True


# LLM: IM 没有“系统行”的样式，用固定前缀把宿主提示和模型正文分开；提示在前、正文在后，中间空一行。只读。
# 函数用途: 把宿主提示渲染到 IM 回复正文前面。
def with_host_notice_lines(content: str, notices: object) -> str:
    lines = [f"{_LINE_PREFIX}{notice.text}" for notice in host_notices_from(notices)]
    return "\n".join(lines) + ("\n\n" + content if content else "") if lines else content


__all__ = [
    "HOST_NOTICE_LIMIT_COUNT",
    "TURN_REQUEST_DETAIL",
    "HostNotice",
    "clear_host_notices",
    "host_notice",
    "host_notices_from",
    "pending_host_notices",
    "queue_host_notice",
    "queue_turn_host_notice",
    "take_host_notices",
    "turn_host_notices",
    "with_host_notice_lines",
]
