from __future__ import annotations

"""LLM: Own the two-level closeout of a steer that reached a turn without an ingress receipt.

模块用途: 回合收口时，给没有入口请求号的插话（/steer 控制命令、IM 才有的情况）定结局：能拿到可重放的结构化输入
就经同一个入口回执排成“备用下一轮”，拿不到就明确告诉用户“刚才补充的话没有被处理，请重新发送”，两条出口
（TUI、IM）共用这一套，不再各写一份。只读回执与 scope 里的结构化字段，不读正文。
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..conversation.host_notices import host_notice, queue_host_notice
from ..conversation.store_guidance_recovery import (
    CLOSEOUT_REPLAY_QUEUED,
    CLOSEOUT_REPLAY_UNAVAILABLE,
)
from ..conversation.turn_resume_notice import STEER_CLOSEOUT_UNAVAILABLE_NOTICE

# 宿主提示来源与原因码：同一条插话的“没被处理”提示在同一会话只保留最新一条。
STEER_CLOSEOUT_NOTICE_SOURCE = "gateway_steer_closeout"
STEER_CLOSEOUT_NOTICE_CODE = "steer_closeout_unavailable"
# 回执上没有入口请求号时用它，保证同一插话重放时回执幂等（同一条插话不会排两轮）。
_STEER_REPLAY_KIND = "steer_closeout_replay"


# LLM: 收口只能从回执的结构化字段做判断：entry.metadata 里的会话（thread_id）、渠道、会话 ID、用户身份、渠道消息号，
#   以及回执自己的去重键。全部齐了才够重放出一条独立请求；缺一项都不能编，只能走提示那条路。
# 类用途: 一条可重放插话的固定输入事实。
@dataclass(frozen=True)
class SteerReplayInput:
    thread_id: str
    channel: str
    conversation_id: str
    user_id: str
    client_message_id: str
    dedupe_key: str

    # LLM: 重放只复用结构化身份，不改写原插话的正文和投递目标；同一条插话重放两次得到同一个请求号。
    # 函数用途: 由这条插话的身份推出固定的重放请求号与客户端输入指纹。
    def identity(self) -> tuple[str, str]:
        raw = "|".join((_STEER_REPLAY_KIND, self.thread_id, self.dedupe_key))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"steer-replay-{digest[:24]}", digest


# LLM: 入口队列函数由调用方（input_delivery_service）注入：本模块只描述“重放一条插话”这件事，
#   不反向 import 入口服务，避免两个模块互相 import 成环；注入的函数签名保持和入口服务一致。
# 类用途: 排一条重放请求需要的最小入口能力。
@dataclass(frozen=True)
class SteerReplayQueue:
    prepare: Callable[..., object]
    enqueue: Callable[..., object]
    transition: Callable[..., object]


# LLM: 只认结构化字段：回执级 closeout_replay=pending（被收口拒收、内容还没着落）已经由会话层判定，这里再要求
#   上下文齐全到足以排出一条独立请求。缺 thread_id 就没有会话历史可挂，缺 dedupe_key 就无法保证幂等，两者都视为重放不了。
# 函数用途: 从被收口拒收的插话回执里取可重放的结构化输入。
def steer_replay_input(receipt: object) -> SteerReplayInput | None:
    entry = getattr(receipt, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    values = SteerReplayInput(
        thread_id=str(metadata.get("thread_id") or "").strip(),
        channel=str(metadata.get("channel") or "").strip(),
        conversation_id=str(metadata.get("conversation_id") or "").strip(),
        user_id=str(getattr(entry, "sender", "") or "").strip(),
        client_message_id=str(metadata.get("channel_message_id") or "").strip(),
        dedupe_key=str(getattr(receipt, "dedupe_key", "") or "").strip(),
    )
    return values if values.thread_id and values.dedupe_key else None


# LLM: 可重放时经同一条结构化入口排队——先在同一入口锁内用固定请求号写回执和完整排队请求（prepare），
#   再走 enqueue 物化 inbox。判断依据必须是**回执终态**（state=="queued"），不能是 enqueue 的 created：
#   created 只说明"这次有没有新建入口项"，重跑命中已存在的请求时它是 False，但内容其实已经在队列里。
#   用 created 会把已排队的插话误判成"排不了"，进而发"请重新发送"——用户一重发就是双份
#   （be 2026-10-03 复审在"enqueue 成功、mark_closeout_replay 还没落盘就重启重跑"的窗口上实测到）。
#   state=="consumed" 时 queue_gateway_input_locked 不排队、直接原样返回，正好被 state=="queued" 排除在外。
# 函数用途: 把一条可重放的插话排成“备用下一轮”，返回内容现在是否已经在队列里。
def queue_steer_replay(paths: object, prepared_request: dict, replay: SteerReplayInput, queue: SteerReplayQueue) -> bool:
    request_id, digest = replay.identity()
    with queue.transition(paths, request_id):
        receipt, _created = queue.prepare(
            paths,
            request_id=request_id,
            client_input_digest=digest,
            client_message_id=replay.client_message_id,
            guidance_dedupe_key=replay.dedupe_key,
            prepared_request=dict(prepared_request),
        )
        queued_receipt, _created_now = queue.enqueue(paths, receipt)
    return str(getattr(queued_receipt, "state", "") or "") == "queued"


# LLM: 重放不了（回执里没有够用的结构化输入，或排队本身失败）时，内容已经没有任何去向，唯一负责的做法是叫用户重发。
#   提示用宿主提示表（TUI 灰行与 IM【提示】共用一条），带结构化原因码；同一会话同一条插话的旧提示被替换，不刷屏。
# 函数用途: 给重放不了的插话所在会话留一条“请重新发送”的宿主提示。
def notice_steer_closeout(store: object, thread_id: str) -> bool:
    if not str(thread_id or "").strip():
        return False
    notice = host_notice(
        STEER_CLOSEOUT_NOTICE_SOURCE,
        STEER_CLOSEOUT_NOTICE_CODE,
        STEER_CLOSEOUT_UNAVAILABLE_NOTICE,
        details={"reason": STEER_CLOSEOUT_NOTICE_CODE, "at": int(time.time())},
    )
    return bool(queue_host_notice(store, thread_id, notice, replace_same_code=True))


__all__ = [
    "STEER_CLOSEOUT_NOTICE_CODE",
    "STEER_CLOSEOUT_NOTICE_SOURCE",
    "SteerReplayQueue",
    "SteerReplayInput",
    "notice_steer_closeout",
    "queue_steer_replay",
    "steer_replay_input",
]
