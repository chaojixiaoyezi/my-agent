# LLM: Gateway 流出口只拥有请求级缓冲、公开投影和审批交互；canonical 历史、执行权及最终结果由调用方提交。
# 事件先写原 chunk 再投影，模型候选与确认消息必须区分；修改时同步 streaming、审批、插话和重连回归。
# 模块用途: 将模型、工具和控制过程按原顺序写给客户端，保留权限、脱敏和迟到客户端读取边界。
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..conversation.channels import (
    project_host_paths_for_channel,
    redact_structured_identifiers,
)
from ..conversation.compact_progress import normalize_conversation_compact_progress
from ..conversation.tool_input_progress import public_tool_input_progress
from .approval_session import ToolApprovalSessionCache
from .stream_approval import StreamApproval
from .stream_events import (
    active_turn_input_event,
    provider_retry_event,
    public_context_compaction_payload,
    public_context_usage_payload,
    public_model_text,
    thinking_event,
)

_CHUNK_STREAM_FLUSH_INTERVAL_SECONDS = 0.08
_CHUNK_STREAM_FLUSH_CHARS = 128


# LLM: 仅准备原请求的 chunk 目录；事件文件由首次追加创建，结束后须保留供客户端补读。
# 函数用途: 建立流文件所在目录并返回既有路径和创建时刻。
def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


# LLM: typed progress 与模型 delta 共用归档文件但保留 kind，客户端不得再解析“[工具]”正文猜状态。
# 函数用途: 追加一个带类型的 Gateway 流事件；展示写入失败不改变请求结果。
def write_chunk_event(chunk_path: Path, payload: dict[str, object]) -> None:
    try:
        line = json.dumps({"t": time.time(), **payload}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# LLM: BufferedChunkStreamWriter 是 Gateway run 的公开事件出口；审批与富 transcript 必须分别来自显式客户端能力，普通客户端不得收到思考或大段工具展示数据。
# 公开事件先沿原 chunk 落盘，再投影 main 数字/阶段与公开过程；审批模式提供者由宿主绑定，显示故障不能中止请求。
# 类用途: 缓冲模型/工具事件，并为支持的 TUI 投递逐轮说明、折叠思考、结构化结果和审批等待。
@dataclass
class BufferedChunkStreamWriter:
    """Buffer model deltas and persist typed user-visible stream events."""

    chunk_path: Path
    flush_interval_seconds: float = _CHUNK_STREAM_FLUSH_INTERVAL_SECONDS
    flush_chars: int = _CHUNK_STREAM_FLUSH_CHARS
    _buffer: list[str] = field(default_factory=list)
    _buffer_chars: int = 0
    _last_flush_at: float = field(default_factory=time.monotonic)
    _verbose_level: str = "off"
    _model_segment: list[str] = field(default_factory=list)
    _model_delta_buffer: list[str] = field(default_factory=list)
    _model_delta_chars: int = 0
    _last_model_delta_flush_at: float = field(default_factory=time.monotonic)
    _commentary_emitted: bool = False
    _committed_commentary: list[str] = field(default_factory=list)
    _tool_input_active: bool = False
    _identifier_redactions: tuple[tuple[object, str], ...] = ()
    _observed_tool_rounds: int = 0
    # LLM: 出口正文里的宿主绝对路径是否保留，只由这条请求的通道事实决定（本机私有通道保留，
    #   外部/未知通道收敛成 basename）。不得从正文内容判断，也不得整体取消脱敏。
    delivery_channel: str = ""
    interactive_approvals: bool = False
    rich_transcript: bool = False
    main_activity_sink: object | None = field(default=None, repr=False)
    transcript_sink: object | None = field(default=None, repr=False)
    _approval: StreamApproval = field(init=False, repr=False)
    approval_mode_decision_provider: Callable | None = field(default=None, repr=False)

    # LLM: 审批组件只持有同请求的路径和同步输出回调；不会建立新线程、缓存或持久账本。
    # 函数用途: 连接审批交互与本请求的流式发布和刷新边界。
    def __post_init__(self) -> None:
        self._approval = StreamApproval(self.chunk_path, self._write_event, self._prepare_permission_request)

    # LLM: 真实等待前按原次序排空增量、确认工具前说明；命中缓存或不支持交互时不调用。
    # 函数用途: 在审批请求发布之前收起模型候选，使客户端保持一致的事件顺序。
    def _prepare_permission_request(self) -> None:
        self.flush()
        self._write_model_commentary_at_boundary()

    # LLM: Gateway writer 的 callable 接口沿 write 的原缓冲，不建立另一条发送路径。
    # 函数用途: 让既有文本回调调用者继续使用同一流出口。
    def __call__(self, text: str) -> None:
        self.write(text)

    # LLM: 原 chunk 是公开事件出口；两种投影不改变 payload/顺序，异常只丢展示，不能反噬模型回合。
    # 函数用途: 发布已清洗事件，分别同步同会话的状态条和正文，不扩大审批入口。
    def _write_event(self, event: dict[str, object]) -> None:
        write_chunk_event(self.chunk_path, event)
        for sink in (self.main_activity_sink, self.transcript_sink):
            if not callable(sink):
                continue
            try:
                sink(event)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass

    # LLM: rich transcript 逐模型轮暂存 commentary，普通客户端仍只保留首段；任何暂存段都只能在真实工具边界公开。
    # 函数用途: 接收供应商可见文本增量，等待工具边界确认它是过程说明。
    def write_model(self, text: str) -> None:
        """Keep one tentative model segment and stream rich-client deltas live.

        A provider delta is not yet an accepted user reply: the same generation
        may go on to call a tool, be invalidated by steering, or be replaced by
        the final structured response.  Only the sanitized commentary emitted
        at a real tool boundary and the terminal response file are public.

        Rich clients (TUI) additionally receive ``model_delta`` events in real
        time so the candidate reply appears while it is being generated.  The
        per-batch redaction is a display-level projection: text split across
        batches may show one raw prefix briefly, and the sanitized commentary /
        canonical terminal response always settles the final transcript.
        """
        if not text:
            return
        if self.rich_transcript or not self._commentary_emitted:
            self._model_segment.append(text)
        if self.rich_transcript:
            self._model_delta_buffer.append(text)
            self._model_delta_chars += len(text)
            if _stream_flush_due(text, self._model_delta_chars, self._last_model_delta_flush_at, self.flush_chars, self.flush_interval_seconds):
                self._flush_model_deltas()

    # LLM: 详细级别只影响展示字段，不扩大工具权限或改变执行参数。
    # 函数用途: 规范化本请求的过程显示级别。
    def set_verbose_level(self, level: str) -> None:
        normalized = str(level or "off").strip().lower()
        self._verbose_level = normalized if normalized in {"off", "on", "full"} else "off"

    # LLM: Binding happens only after canonical owner/thread/cwd resolution and before the model
    # can call tools. Request payload prose or tool arguments must never choose this scope.
    # 函数用途: 把当前请求接到真实会话级审批缓存，供后续完全相同的调用复用一次授权。
    def configure_approval_session(
        self,
        cache: ToolApprovalSessionCache,
        scope_provider: Callable[[], str],
    ) -> None:
        self._approval.configure(cache, scope_provider)


    # LLM: steering 只清空未确认段并重开普通客户端首段额度；同会话公开投影在同一 typed 边界丢弃候选。
    # 函数用途: 补充输入进入活动回合后建立新分段，不让旧半句混入后续回答。
    def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
        """Allow one new model-authored commentary after live user steering.

        Ordinary tool rounds remain suppressed after the first commentary.  A
        real user message arriving during the active turn opens exactly one new
        segment so the same run can answer without waiting for final closeout.
        """
        self._model_segment.clear()
        self._model_delta_buffer.clear()
        self._model_delta_chars = 0
        self._commentary_emitted = False
        begin = getattr(self.transcript_sink, "begin_active_turn_input", None)
        if callable(begin):
            try:
                begin(client_message_ids)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass

    # LLM: 与 consumed 严格区分:本事件只证明"这批补充输入已经进入这一次提供方调用的 prompt"
    # (ConversationStore 已 committed submitted、后端调用即将发出),不证明模型处理过、不结算回复
    # 欠账、也不清任何 pending 回执。发布它是为了让客户端在慢流/失败时也能立刻按真实位置看到
    # 用户消息,而不是等整次响应返回后才由 consumed 补上。
    # 函数用途: 在提供方调用发出前，向富客户端发布可重放的"已提交"用户消息事件。
    def submit_active_turn_input(
        self,
        client_message_ids: tuple[str, ...],
        *,
        provider_call_id: str = "",
        client_messages: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if not self.rich_transcript:
            return
        payload = active_turn_input_event("active_turn_input_submitted", client_message_ids,
                                          client_messages=client_messages, provider_call_id=provider_call_id)
        if payload:
            self.flush()
            self._write_event(payload)

    # LLM: This event is emitted only after ConversationStore committed consumed at the
    # provider-accepted prompt boundary. Text lets reconnecting clients replay the committed
    # user row, while ids remain the sole pending-receipt correlation authority.
    # 函数用途: 在模型确认收到补充消息后，向富客户端发布可重放的已消费用户消息事件。
    def complete_active_turn_input(
        self,
        client_message_ids: tuple[str, ...],
        *,
        client_messages: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if not self.rich_transcript:
            return
        payload = active_turn_input_event("active_turn_input_consumed", client_message_ids,
                                          client_messages=client_messages)
        if payload:
            self.flush()
            self._write_event(payload)

    # LLM: 脱敏编号由宿主身份绑定；修改时同步路径保留和外部通道隐藏回归。
    # 函数用途: 在发布正文前安装可信标识替换表。
    def set_identifier_redactions(
        self,
        identifiers: tuple[tuple[object, str], ...],
    ) -> None:
        """Install exact trusted identifiers before any model commentary is published."""
        self._identifier_redactions = tuple(identifiers)

    # LLM: 运行进度与模型候选分开缓冲，达到原阈值才向 chunk 写事件。
    # 函数用途: 缓存运行过程文本，必要时刷新到客户端。
    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        self._buffer_chars += len(text)
        if _stream_flush_due(text, self._buffer_chars, self._last_flush_at, self.flush_chars, self.flush_interval_seconds):
            self.flush()

    # LLM: 增量在冻结边界前落到原 chunk；共用 main 投影只观察相同已清洗事件，不改变分段顺序。
    # 函数用途: 先发尚未送出的模型增量，再发累计的运行过程，避免工具边界前漏字。
    def flush(self) -> None:
        # Live model deltas must reach the chunk file before any boundary event
        # that freezes the segment (commentary/progress/permission/compact).
        self._flush_model_deltas()
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_chars = 0
        self._last_flush_at = time.monotonic()
        self._write_event(
            {
                "kind": "runtime_progress",
                "text": text,
                "verbose_level": self._verbose_level,
            },
        )

    # LLM: rich transcript 可保留已脱敏的 output/display；真实 tool start 先
    # 清 provider 参数临时行。普通客户端继续按 verbose 合同裁剪，display 一律不外发。
    # 函数用途: 在工具边界收起参数进度、冻结模型说明，并写入结构化工具事件。
    def write_progress(self, event: dict[str, object], legacy_text: str) -> None:
        try:
            observed_round = int(event.get("round") or 0)
        except (TypeError, ValueError):
            observed_round = 0
        self._observed_tool_rounds = max(self._observed_tool_rounds, observed_round)
        self.flush()
        if event.get("phase") == "started":
            self._reset_tool_input_progress()
            self._write_model_commentary_at_boundary()
        progress = dict(event)
        if not self.rich_transcript:
            progress.pop("display", None)
        if not self.rich_transcript and self._verbose_level != "full":
            progress.pop("output", None)
        self._write_event(
            {
                "kind": "tool_progress",
                "text": legacy_text,
                "verbose_level": self._verbose_level,
                "progress": progress,
            },
        )

    # LLM: Sanitize the full thinking before preview clipping and archive through the bound
    # owner/thread sink. Events carry only preview/ref; display failure never changes model state.
    # 函数用途: 保留完整公开思考原文，再发送有界预览和引用；保持思考先于正文，不丢失长思考中间部分。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
        if not self.rich_transcript:
            return
        payload = thinking_event(text, duration_seconds=duration_seconds, delivery_channel=self.delivery_channel,
                                 identifiers=self._identifier_redactions, transcript_sink=self.transcript_sink)
        if payload:
            self._write_event(payload)

    # LLM: thinking_delta 是流式思考的展示级增量，只做路径/标识脱敏（同 model_delta，
    # 不做 project_user_reply——那会把未完成思考当终稿投影）；canonical assistant_thinking
    # 始终全量脱敏并覆盖流式内容。
    # 函数用途: 实时转发 provider 流式思考增量（rich 客户端，思考期间即可见）。
    def write_thinking_delta(self, text: str) -> None:
        if not self.rich_transcript:
            return
        content = redact_structured_identifiers(
            project_host_paths_for_channel(str(text or ""), self.delivery_channel),
            self._identifier_redactions,
            channel=self.delivery_channel,
        )
        if not content:
            return
        self._write_event(
            {
                "kind": "thinking_delta",
                "text": content,
            },
        )

    # LLM: 该出口只接受共享白名单后的累计字符计数；raw partial JSON、路径、
    # 命令和凭据不得写入 chunk 文件，普通非 rich 客户端保持完全不可见。
    # 函数用途: 向真实 TUI 流式发送“模型正在准备工具参数”的临时进度。
    def write_tool_input_progress(self, value: object) -> bool:
        if not self.rich_transcript:
            return False
        public = public_tool_input_progress(value)
        if not public:
            return False
        self._write_event(
            {
                "kind": "tool_input_progress",
                "progress": public,
            },
        )
        self._tool_input_active = str(public.get("phase") or "") != "ready"
        return True

    # LLM: reset 只能在当前 writer 确实公开过未闭合参数块时发送，避免普通
    # retry/tool 事件多出无意义行；该布尔仍只是易失显示状态。
    # 函数用途: 收起当前 TUI 的工具参数临时行，并清除 writer 本地标记。
    def _reset_tool_input_progress(self) -> bool:
        if not self.rich_transcript or not self._tool_input_active:
            return False
        self._tool_input_active = False
        self._write_event({"kind": "tool_input_reset"})
        return True

    # LLM: provider retry 只向显式 rich 客户端公开有界结构化进度；原始异常和 endpoint 不得进入公开 chunk。
    # 函数用途: 在传输层或模型回合退避期间立即显示重连次数和等待秒数。
    def write_provider_retry(
        self,
        *,
        scope: str,
        attempt: int,
        total: int,
        delay_seconds: float,
        error_type: str,
    ) -> bool:
        if not self.rich_transcript:
            return False
        payload = provider_retry_event(scope=scope, attempt=attempt, total=total,
                                       delay_seconds=delay_seconds, error_type=error_type)
        self.flush()
        self._reset_tool_input_progress()
        self._write_event(payload)
        return True

    # LLM: 数字/阶段遥测统一经过显式客户端能力和独立白名单投影，再按原 flush -> event 顺序发布。
    # 函数用途: 共用富客户端遥测的清洗与发布边界，空投影不刷新，不将正文写入统计事件。
    def _write_rich_projection(self, kind: str, field_name: str, value: object, project: Callable) -> bool:
        if not self.rich_transcript:
            return False
        public = project(value)
        if not public:
            return False
        self.flush()
        self._write_event({"kind": kind, field_name: public})
        return True

    # LLM: 统计事件只输出白名单数字，进入已有有序 chunk 流，不额外扫描账本或写模型消息。
    # 函数用途: 将模型开始、结算、工具解析后的统计快照传给前台 TUI。
    def write_model_metrics(self, metrics: dict[str, object]) -> bool:
        from ..conversation.model_metrics import public_model_metrics

        return self._write_rich_projection("model_metrics_updated", "model_metrics", metrics, public_model_metrics)

    # LLM: Context usage is a rich-client-only numeric projection. The writer must whitelist
    # fields and must never serialize prompt, message, guidance, or tool-schema content.
    # 函数用途: 把每次模型调用前的上下文总量和分类估算实时写入 TUI 事件流。
    def write_context_usage(self, usage: dict[str, object]) -> bool:
        return self._write_rich_projection("context_usage_updated", "context_usage", usage, public_context_usage_payload)

    # LLM: Native IR compaction is distinct from durable conversation compact. Only its bounded
    # counters may enter rich chunks; summary text, archived calls, and prompts remain private.
    # 函数用途: 公布活动回合内一次真实工具历史裁剪，供 TUI 留下可审计提示。
    def write_context_compaction(self, value: dict[str, object]) -> bool:
        return self._write_rich_projection("context_window_compacted", "context_compaction", value, public_context_compaction_payload)

    # LLM: durable conversation compact 进度只允许冻结 schema 中的阶段/计数进入 rich chunk，摘要与原始消息永不外发。
    # 函数用途: 把持久会话 Compact 的真实处理阶段流式发给 TUI。
    def write_conversation_compact_progress(self, value: dict[str, object]) -> bool:
        return self._write_rich_projection("conversation_compaction_progress", "compact_progress", value, normalize_conversation_compact_progress)

    # LLM: 精确请求先发布后等待；用户从菜单切自主可原地续跑，模式提供者由 owner 控制面绑定，不接受模型参数。
    # 函数用途: 向客户端发布审批并等待决定或自主模式切换；取消保持优先。
    def request_permission(
        self,
        request_value: dict[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        return self._approval.request(
            request_value, interactive=self.interactive_approvals,
            mode_decision_provider=self.approval_mode_decision_provider,
            cancellation_token=cancellation_token,
        )

    # LLM: compact boundary 只能由 canonical conversation generation 前进触发；它是显示事件，不复制摘要正文或建立第二会话状态。
    # 函数用途: 在同一 Gateway chunk 流中公布一次上下文压缩代际边界。
    def write_compact_boundary(self, generation: int) -> None:
        normalized_generation = max(0, int(generation or 0))
        if normalized_generation <= 0:
            return
        self.flush()
        self._write_model_commentary_at_boundary()
        self._write_event(
            {
                "kind": "conversation_compacted",
                "compact_generation": normalized_generation,
            },
        )

    # LLM: 工具轮数来自结构化 progress，不能从输出文本或动画推断。
    # 函数用途: 返回当前请求已经观察到的最大工具轮号。
    @property
    def observed_tool_rounds(self) -> int:
        """Return exact structured progress observed during this request."""
        return self._observed_tool_rounds

    # LLM: 先刷新原事件再关闭展示投影；chunk 文件保留给迟到读者，流关闭不提交任务终态或消费唤醒。
    # 函数用途: 收口前台输出和遗留动画，不把流关闭当作工具/任务成功。
    def close(self) -> None:
        self.flush()
        for sink in (self.main_activity_sink, self.transcript_sink):
            closer = getattr(sink, "close", None)
            if callable(closer):
                try:
                    closer()
                except (OSError, RuntimeError, TypeError, ValueError):
                    pass

    # LLM: 在 final 持久化前排空增量并冻结同片显示；失败不改变模型结果，也不新增第二份持久正文。
    # 函数用途: 给正常提交和延迟补交提供完整过程快照，候选块由最终消息按编号接替。
    def prepare_display_history(self) -> dict:
        self.flush()
        prepare = getattr(self.transcript_sink, "prepare_final", None)
        if not callable(prepare):
            return {}
        try:
            return prepare()
        except (OSError, RuntimeError, TypeError, ValueError):
            return {}

    # LLM: The returned copy contains only tool-boundary-confirmed assistant messages. It is
    # the typed handoff into ConversationStore and never includes an uncommitted final candidate.
    # 函数用途: 返回本轮已经确认的过程回复，供最终收口按原顺序持久化到长期会话。
    def assistant_commentary_messages(self) -> tuple[str, ...]:
        return tuple(self._committed_commentary)

    # LLM: rich transcript 每个真实工具边界都可发布一段；普通客户端仍严格限制为首段，
    # 但已确认正文必须完整保留并进入 canonical history，末轮正文留给 final response。
    # 函数用途: 脱敏、记录并发布当前已经由工具边界确认的模型过程说明。
    def _write_model_commentary_at_boundary(self) -> None:
        if (self._commentary_emitted and not self.rich_transcript) or not self._model_segment:
            return
        raw = "".join(self._model_segment)
        self._model_segment.clear()
        content = public_model_text(raw, max_chars=0, delivery_channel=self.delivery_channel, identifiers=self._identifier_redactions)
        if not content:
            return
        self._commentary_emitted = True
        self._committed_commentary.append(content)
        self._write_event(
            {
                "kind": "assistant_commentary",
                "text": content,
            },
        )

    # LLM: 增量只做展示级脱敏（路径与结构化标识），不做 project_user_reply——
    # 那会把未完成文本当成终稿投影；canonical 终稿始终全量脱敏并覆盖流式正文。
    # 函数用途: 把当前候选消息增量批脱敏后写入 typed model_delta 事件。
    def _flush_model_deltas(self) -> None:
        if not self._model_delta_buffer:
            return
        raw = "".join(self._model_delta_buffer)
        self._model_delta_buffer.clear()
        self._model_delta_chars = 0
        self._last_model_delta_flush_at = time.monotonic()
        content = redact_structured_identifiers(
            project_host_paths_for_channel(raw, self.delivery_channel),
            self._identifier_redactions,
            channel=self.delivery_channel,
        )
        if not content:
            return
        self._write_event(
            {
                "kind": "model_delta",
                "text": content,
            },
        )


# LLM: 模型增量与运行进度使用同一刷新公式，各自传入独立计数/时钟；不读或改变任务状态。
# 函数用途: 判断字符量、换行或单调时钟是否达到原发送阈值，避免两种缓冲各维护一套规则。
def _stream_flush_due(latest_text: str, chars: int, last_flush_at: float, flush_chars: int, interval: float) -> bool:
    if chars >= max(1, int(flush_chars)) or latest_text.endswith("\n"):
        return True
    return time.monotonic() - last_flush_at >= max(0.0, float(interval))
