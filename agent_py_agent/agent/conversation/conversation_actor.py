"""可重建的命令序列器：邮箱短门只改内存，业务提交仍由调用线程在 T 内完成。"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, field, replace

from ..concurrency import DurableDaemonThreadPoolExecutor
from ..concurrency.lock_order import note_handler_overrun

# 单条邮箱处理超过50毫秒只记诊断，严格测试将其视为阻塞处理器。
ACTOR_HANDLER_BUDGET_MS = 50


@dataclass(frozen=True)
class Command:
    owner_key: str
    conversation_key: str
    request_id: str = ""
    attempt_id: str = ""
    epoch: int = 0
    seq: int = 0


class Admit(Command):
    pass


class Steer(Command):
    pass


class Stop(Command):
    pass


class TurnFinished(Command):
    pass


@dataclass(frozen=True)
class Recover(Command):
    exit_proof: bool = False
    previous_attempt: tuple | None = None


@dataclass(frozen=True)
class ActorDecision:
    seq: int = 0
    action: str = ""
    expected_seq: int = 0


class ConversationSequencer:
    def __init__(self):
        self._lock = threading.Lock()
        self._conversations: dict[str, _Mailbox] = {}
        self._next_sequence = 0

    def _box(self, key):
        return self._conversations.setdefault(key, _Mailbox())

    def _stamp(self, box, msg):
        if msg.seq:
            box.seq = max(box.seq, msg.seq)
            self._next_sequence = max(self._next_sequence, msg.seq)
            return msg
        self._next_sequence += 1
        box.seq = self._next_sequence
        return replace(msg, seq=box.seq)

    def _advance_version(self, box):
        # 排队命令可能晚于新控制完成；提交版本另取新序号，不能倒退或复活旧决策。
        self._next_sequence += 1
        box.version = self._next_sequence

    def decide(self, msg) -> ActorDecision:
        if not self._lock.acquire(blocking=False):
            return ActorDecision(0, "retry", -1)
        try:
            box = self._box(msg.conversation_key)
            command = self._stamp(box, msg)
            action = self._apply(box, command)
            return ActorDecision(command.seq, action, -1 if action == "stale" else box.version)
        finally:
            self._lock.release()

    def _apply(self, box, msg):
        target = (msg.request_id, msg.attempt_id, msg.epoch)
        if isinstance(msg, (Stop, Steer)):
            if box.inflight.get(message_key(msg)) == msg.seq:
                box.inflight.pop(message_key(msg))
            if msg.seq < max(box.control_seq, box.version):
                return "stale"
            box.control_seq = msg.seq
            self._advance_version(box)
            if isinstance(msg, Stop):
                stopped = msg.request_id or (box.active[0] if box.active else "")
                if stopped:
                    box.stopped.add(stopped)
                if box.active is None:
                    return "run_leftover_cleanup"
                if not msg.request_id or box.active[0] == msg.request_id:
                    box.state = "stopping"
                return "stop"
            return "steer" if box.active is not None and box.state == "running" else "rejected"
        if isinstance(msg, TurnFinished):
            if box.active == target:
                box.active, box.state = None, "idle"
                self._advance_version(box)
                return "finished"
            return "ignored"
        if isinstance(msg, Recover):
            if (not msg.exit_proof or box.controls or box.inflight
                    or box.active is not None and box.active != msg.previous_attempt):
                return "require_exit_proof"
            if msg.request_id in box.stopped:
                return "rejected"
            box.active, box.state = target, "running"
            self._advance_version(box)
            return "admit"
        if isinstance(msg, Admit):
            if msg.request_id in box.stopped:
                return "rejected"
            provisional = box.active is not None and box.active[0] == msg.request_id and not box.active[1]
            if box.controls or box.inflight or (box.active is not None and box.active != target and not provisional):
                return "defer"
            if box.state == "stopping":
                return "rejected"
            if box.active == target:
                return "already_running"
            box.active, box.state = target, "running"
            self._advance_version(box)
            return "admit"
        raise TypeError("unknown conversation command")

    def check_still_current(self, conversation_key: str, expected_seq: int) -> bool:
        if expected_seq < 0 or not self._lock.acquire(blocking=False):
            return False
        try:
            return self._box(conversation_key).version == expected_seq
        finally:
            self._lock.release()

    def enqueue(self, msg):
        envelope, previous = self.put(msg)
        if previous is not None and not previous.result.done():
            previous.result.set_result(ActorDecision(previous.command.seq, "superseded", -1))
        return envelope.command

    def put(self, msg, handler=None):
        """消息、回调、回执同槽绑定；Future回调只能在短门外触发。"""
        with self._lock:
            box = self._box(msg.conversation_key)
            if isinstance(msg, Stop) and not msg.request_id and box.active is not None:
                msg = replace(msg, request_id=box.active[0])
            msg = self._stamp(box, msg)
            queue = box.controls if isinstance(msg, (Stop, Steer)) else box.work
            key = message_key(msg)
            previous = queue.get(key)
            if previous is not None and previous.command.seq > msg.seq:
                return previous, None
            envelope = _Envelope(msg, handler)
            queue[key] = envelope
            if isinstance(msg, (Stop, Steer)):
                box.control_seq = max(box.control_seq, msg.seq)
                box.version = max(box.version, msg.seq)
                if isinstance(msg, Stop) and msg.request_id:
                    box.stopped.add(msg.request_id)
            return envelope, previous

    def pop(self, conversation_key):
        with self._lock:
            box = self._box(conversation_key)
            queue = box.controls if box.controls else box.work
            if not queue:
                return None
            msg = queue.popitem(last=False)[1].command
            if isinstance(msg, (Stop, Steer)):
                box.inflight[message_key(msg)] = msg.seq
            return msg

    def take_decision(self, conversation_key):
        """出队及状态应用在同一个短门内，控制不留越过窗口。"""
        if not self._lock.acquire(blocking=False):
            return None, ActorDecision(0, "retry", -1)
        try:
            box = self._box(conversation_key)
            queue = box.controls if box.controls else box.work
            if not queue:
                return None, None
            envelope = queue.popitem(last=False)[1]
            action = self._apply(box, envelope.command)
            return envelope, ActorDecision(envelope.command.seq, action, -1 if action == "stale" else box.version)
        finally:
            self._lock.release()

    def has_pending(self, key):
        with self._lock:
            box = self._conversations.get(key)
            return box is not None and bool(box.controls or box.work)

    def discard(self, envelope):
        with self._lock:
            msg = envelope.command
            box = self._box(msg.conversation_key)
            queue = box.controls if isinstance(msg, (Stop, Steer)) else box.work
            if queue.get(message_key(msg)) is envelope:
                queue.pop(message_key(msg))

    def retire_terminal(self, conversation_key, request_id):
        """只消费调用方已确认的canonical终态；不以计时淘汰未结tombstone。"""
        with self._lock:
            box = self._conversations.get(conversation_key)
            if box is None:
                return
            box.stopped.discard(request_id)
            if box.active is None and not (box.controls or box.work or box.inflight or box.stopped):
                self._conversations.pop(conversation_key)

    def snapshot(self, conversation_key):
        with self._lock:
            box = self._box(conversation_key)
            return {"state": box.state, "active": box.active, "seq": box.seq,
                    "controls": len(box.controls), "queued": len(box.work)}

    def rebuild(self, durable_commands):
        """调用方只传已核durable活跃集；恢复仍须原退出证明。"""
        for command in durable_commands:
            self.decide(command)


@dataclass
class _Mailbox:
    seq: int = 0
    control_seq: int = 0
    version: int = 0
    active: tuple | None = None
    state: str = "idle"
    stopped: set[str] = field(default_factory=set)
    controls: OrderedDict = field(default_factory=OrderedDict)
    work: OrderedDict = field(default_factory=OrderedDict)
    inflight: dict = field(default_factory=dict)


def message_key(msg):
    return ((type(msg), msg.request_id) if isinstance(msg, (Stop, Steer))
            else (type(msg), msg.request_id, msg.attempt_id, msg.epoch))


@dataclass
class _Envelope:
    command: Command
    handler: object = None
    result: Future = field(default_factory=Future)


class ActorScheduler:
    """一个会话一个排空者；每次只处理一条，队尾重排给其他会话让路。"""
    def __init__(self, *, workers: int, sequencer=None):
        self.sequencer = sequencer if sequencer is not None else ConversationSequencer()
        self._executor = DurableDaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix="conversation-mailbox")
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()
        self._runners: dict[str, object] = {}
        self._closed = False
        self._stop_scheduling = False
        self._pending_results: set[Future] = set()
        self.slow_handlers = 0

    def _schedule_locked(self, key):
        token = object()
        self._scheduled.add(key)
        self._runners[key] = token
        try:
            self._executor.submit(self._drain, key, token)
        except BaseException:
            self._scheduled.discard(key)
            self._runners.pop(key, None)
            raise

    def post(self, msg, *, handler=None):
        previous = None
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("conversation scheduler is closed")
                envelope, previous = self.sequencer.put(msg, handler)
                self._pending_results.add(envelope.result)
                if msg.conversation_key not in self._scheduled:
                    try:
                        self._schedule_locked(msg.conversation_key)
                    except BaseException:
                        self.sequencer.discard(envelope)
                        self._pending_results.discard(envelope.result)
                        raise
        finally:
            if previous is not None and not previous.result.done():
                previous.result.set_result(ActorDecision(previous.command.seq, "superseded", -1))
        envelope.result.add_done_callback(self._forget_result)
        return envelope.result

    def _forget_result(self, result):
        with self._lock:
            self._pending_results.discard(result)

    def _drain(self, key, token):
        with self._lock:
            # submit可能入队后才抛错；失败代次的幽灵任务不能领取后来消息。
            if self._runners.get(key) is not token:
                return
        while True:
            envelope = None
            continue_here = False
            try:
                envelope, decision = self.sequencer.take_decision(key)
                if envelope is not None:
                    started = time.monotonic()
                    try:
                        if envelope.handler is not None and decision.action != "stale":
                            envelope.handler(envelope.command, decision)
                    finally:
                        if (time.monotonic() - started) * 1000 > ACTOR_HANDLER_BUDGET_MS:
                            with self._lock:
                                self.slow_handlers += 1
                            note_handler_overrun()
                    envelope.result.set_result(decision)
            except BaseException as error:
                if envelope is not None and not envelope.result.done():
                    envelope.result.set_exception(error)
                else:
                    raise
            finally:
                with self._lock:
                    if self._runners.get(key) is token:
                        self._runners.pop(key)
                        self._scheduled.discard(key)
                        if not self._stop_scheduling and self.sequencer.has_pending(key):
                            try:
                                self._schedule_locked(key)
                            except Exception:
                                # 当前worker仍活着，移交失败就保留其排空权；不遗失已接受消息或Future。
                                self._runners[key] = token
                                self._scheduled.add(key)
                                continue_here = True
            if not continue_here:
                return

    def close(self, *, wait=False):
        with self._lock:
            self._closed = True
            self._stop_scheduling = not wait
            pending = tuple(self._pending_results)
        if wait:
            for result in pending:
                try:
                    result.result()
                except BaseException:
                    # 原Future已向提交者报告失败；清理不得丢下其它资源，也不把失败改写为成功。
                    pass
        self._executor.shutdown(wait=wait)
