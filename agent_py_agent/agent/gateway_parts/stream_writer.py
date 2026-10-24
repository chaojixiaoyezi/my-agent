# LLM: Gateway 流出口组合请求缓冲、公开投影、审批和等待 mixin；执行权及最终结果由调用方提交，等待关闭只拦展示。
# 事件先写原chunk再投影，raw候选只消费不留存；正文唯一来自原sealed-call commit，修改时同步审批、插话和重连回归。
# 模块用途: 将模型、工具、排队与控制过程按原顺序写给客户端，保留权限、脱敏和迟到读取边界。
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..common.agent_time import now as display_now
from ..common.json_io import append_private_text
from ..common.nofollow_fs import ensure_private_dir
from ..conversation.channels import (
    project_host_paths_for_channel,
    redact_structured_identifiers,
    supports_transcript_delivery,
)
from ..conversation.compact_progress import normalize_conversation_compact_progress
from ..conversation.tool_input_progress import public_tool_input_progress
from .approval_session import ToolApprovalSessionCache
from .stream_approval import StreamApproval, StreamApprovalRequestOptions
from .stream_events import (
    active_turn_input_event,
    provider_retry_event,
    public_context_compaction_payload,
    public_context_usage_payload,
    thinking_event,
)
from .stream_liveness import StreamLiveness

# 分块流刷新间隔 0.08 秒：输出攒够 128 字符或超时即推给对端。
_CHUNK_STREAM_FLUSH_INTERVAL_SECONDS = 0.08
# 分块流攒满 128 字符即刷新：低延迟又不至于每字一包。
_CHUNK_STREAM_FLUSH_CHARS = 128


# LLM: 仅准备原请求的 chunk 目录；事件文件由首次追加创建，结束后须保留供客户端补读。
#   目录缺失时经统一私有原语逐级按 0700 新建（pbfix 口径：中间层同样 0700）；已存在的目录一律不动。
# 函数用途: 建立流文件所在目录并返回既有路径和创建时刻。
def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    ensure_private_dir(chunk_path.parent)
    return chunk_path, time.time()


# LLM: 公开流事件先经共用凭据净化再落盘，kind/数字/操作身份保持；客户端不得再解析正文猜状态。
#   追加走私有原语：新文件出生 0600、已有宽权限文件先收紧再追加、缺失目录按 0700 建；
#   序列化格式与原来逐字节一致（ensure_ascii=False、行尾换行）。
# 函数用途: 追加 typed 流事件；可选谓词在原文件锁后复核，展示故障不改变请求结果。
def write_chunk_event(chunk_path: Path, payload: dict[str, object], *, still_current: Callable[[], bool] | None = None) -> None:
    from ..common.log_redaction import redact_sensitive_value

    try:
        line = json.dumps(redact_sensitive_value({"t": time.time(), **payload}), ensure_ascii=False)
        if still_current is None:
            append_private_text(chunk_path, line + "\n")
        else:
            append_private_text(chunk_path, line + "\n", still_current=still_current)
    except OSError:
        pass


# LLM: 排队复用原 writer 的脱敏出口与关闭谓词；落盘/投影前重核，不能复活已关闭的等待展示。
# 函数用途: 白名单化并发布仍有效的排队通知，关闭后的迟到通知不再写原流。
def _write_provider_queue_event(writer, value: object) -> None:
    from .stream_events import provider_queue_event

    # LLM: 仅展示资格，不改变队列/调用生命周期，原文件锁内还会复核同一谓词。
    # 函数用途: 在每次落盘或投影之前读取原 writer 的关闭状态。
    def current() -> bool:
        return not getattr(writer, "_liveness_closed", False)

    if payload := provider_queue_event(value):
        writer._write_event(payload, still_current=current)


# LLM: BufferedChunkStreamWriter 是 Gateway run 的公开事件出口；审批与富 transcript 必须分别来自显式客户端能力，普通客户端不得收到思考或大段工具展示数据。
# 公开事件先沿原 chunk 落盘，再投影 main 数字/阶段与公开过程；审批模式提供者由宿主绑定，显示故障不能中止请求。
# 类用途: 缓冲宿主/工具事件，提交完整模型公开正文并组合IM等待接口；不发布未提交raw候选。
@dataclass
class BufferedChunkStreamWriter(StreamLiveness):
    """Persist committed model replies and typed host/tool events."""

    chunk_path: Path
    flush_interval_seconds: float = _CHUNK_STREAM_FLUSH_INTERVAL_SECONDS
    flush_chars: int = _CHUNK_STREAM_FLUSH_CHARS
    _buffer: list[str] = field(default_factory=list)
    _buffer_chars: int = 0
    _last_flush_at: float = field(default_factory=time.monotonic)
    _verbose_level: str = "off"
    _final_only: bool = field(default=False, init=False)
    _im_preview_max_chars: int = 100
    _display_timezone: str = ""
    im_approval_agent: object | None = field(default=None, repr=False)
    _committed_model_calls: set[str] = field(default_factory=set)
    _commit_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
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

    # LLM: 真实等待前按原次序刷新宿主进度；模型说明已由原工具准入前sealed-call hook提交，不从审批边界确认raw。
    # 函数用途: 在审批请求发布之前排空宿主缓冲，使客户端保持原事件顺序。
    def _prepare_permission_request(self) -> None:
        self.flush()

    # LLM: Gateway writer 的 callable 接口沿 write 的原缓冲，不建立另一条发送路径。
    # 函数用途: 让既有文本回调调用者继续使用同一流出口。
    def __call__(self, text: str) -> None:
        self.write(text)

    # LLM: 同一共用净化结果送 chunk 与实时 sink，annotation 不能旁路；谓词在每个出口前复核，已进入的回调不可撤销。
    # 函数用途: 在落盘和实时展示之前清洗整个 typed 事件，关闭等待后停止后续投影，不改变工具输入。
    def _write_event(self, event: dict[str, object], *, still_current: Callable[[], bool] | None = None) -> None:
        _publish_writer_event(self, event, still_current=still_current)

    # LLM: 保留原sink方法消费未提交raw，防止ToolBoundaryChunkFilter退回write后公开正文；不保存或发布，数字计量仍走原write_model_metrics。
    # 函数用途: 接收并丢弃供应商候选，公开正文只由同sealed-call commit_model_response提交。
    def write_model(self, text: str) -> None:
        return None

    # LLM: 仅原已采纳响应提交此入口；终态全文替换本次候选，幂等键来自真实call而非正文，IM档位和final-only仍由原出口守门。
    # 函数用途: 确认流式或非流式模型说明并复用原事件，丢弃未确认增量而不重放工具。
    def commit_model_response(self, payload: dict) -> None:
        _commit_model_response(self, payload)

    # LLM: 详细级别及字符上限消费本请求有效配置，只投影展示，不裁剪内部候选、工具正文或最终结果。
    # 函数用途: 固定本请求的过程档位、短窗口上限和用户时区，随事件传给 IM 投递端。
    def set_verbose_level(self, level: str, *, preview_max_chars: int = 100, timezone: str = "") -> None:
        normalized = str(level or "off").strip().lower()
        self._verbose_level = normalized if normalized in {"off", "on", "full", "stream"} else "off"
        self._im_preview_max_chars = max(1, min(8000, int(preview_max_chars)))
        self._display_timezone = timezone

    # LLM: 只由原宿主派工确认后设置；仅普通IM候选/进度静默，不改变rich记录、模型或最终response事实。
    # 函数用途: 让短受理轮只由原最终回复watcher外送一次。
    def set_final_only(self) -> None:
        self._final_only = True

    # LLM: Binding happens only after canonical owner/thread/cwd resolution and before the model
    # can call tools. Request payload prose or tool arguments must never choose this scope.
    #   grant_recorder 由宿主（request_execution）注入：用户选 approved_owner 时把 binding.grant_key 写进 owner 策略文件；
    #   None 表示本请求不支持长期授权，面板照常只提供一次/本会话/拒绝。
    # 函数用途: 把当前请求接到真实会话级审批缓存和长期授权记录器，供后续同类调用复用授权。
    def configure_approval_session(
        self,
        cache: ToolApprovalSessionCache,
        scope_provider: Callable[[], str],
        grant_recorder: Callable[[str], None] | None = None,
    ) -> None:
        self._approval.configure(cache, scope_provider, grant_recorder)


    # LLM: steering重开普通客户端首段额度，显示sink在同一typed边界清当前公开段；writer不再保未确认raw。
    # 函数用途: 补充输入进入活动回合后建立新分段，不让旧半句混入后续回答。
    def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
        """Allow one new model-authored commentary after live user steering.

        Ordinary tool rounds remain suppressed after the first commentary.  A
        real user message arriving during the active turn opens exactly one new
        segment so the same run can answer without waiting for final closeout.
        """
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

    # LLM: 仅原宿主缓冲落chunk，模型正文另由同call commit提交，任何flush都不授raw公开资格。
    # 函数用途: 排空累计运行过程，保工具/审批/Compact边界原顺序。
    def flush(self) -> None:
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
    # 函数用途: 在工具边界收起参数进度并发布原结构化状态，模型说明沿此前的sealed-call提交。
    def write_progress(self, event: dict[str, object], legacy_text: str) -> None:
        try:
            observed_round = int(event.get("round") or 0)
        except (TypeError, ValueError):
            observed_round = 0
        self._observed_tool_rounds = max(self._observed_tool_rounds, observed_round)
        self.flush()
        if event.get("phase") == "started":
            self._reset_tool_input_progress()
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

    # LLM: thinking_delta 保原独立思考展示合同，只做路径/标识脱敏，
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
    #   params 是可选收口参数包（{"final": True, "error_code": ...}），原样交给 provider_retry_event 投影。
    # 函数用途: 在传输层或模型回合退避期间立即显示重连次数和等待秒数；到上限时显示收口提示。
    def write_provider_retry(
        self,
        *,
        scope: str,
        attempt: int,
        total: int,
        delay_seconds: float,
        error_type: str,
        params: dict | None = None,
    ) -> bool:
        if not self.rich_transcript:
            return False
        payload = provider_retry_event(scope=scope, attempt=attempt, total=total,
                                       delay_seconds=delay_seconds, error_type=error_type,
                                       params=params)
        self.flush()
        self._reset_tool_input_progress()
        self._write_event(payload)
        return True

    # LLM: 排队事件只沿原 _write_event 的脱敏与生命周期发布，不写执行状态或模型正文。
    # 函数用途: 将本请求的排队和解除进度交给 TUI 与通道状态消费者。
    write_provider_queue = _write_provider_queue_event

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

    # LLM: 固定审批选项来自owner控制面；插件强制确认先守门，普通请求可随自主切换续跑，不接受模型参数。
    # 函数用途: 向客户端发布审批并等待决定或自主模式切换；取消保持优先。
    def request_permission(
        self,
        request_value: dict[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        return request_stream_permission(self, request_value, cancellation_token)

    # LLM: compact boundary只由canonical generation前进触发；刷新宿主事件但不确认raw，不复制摘要或建立第二会话状态。
    # 函数用途: 在同一 Gateway chunk 流中公布一次上下文压缩代际边界。
    def write_compact_boundary(self, generation: int) -> None:
        normalized_generation = max(0, int(generation or 0))
        if normalized_generation <= 0:
            return
        self.flush()
        self._write_event(
            {
                "kind": "conversation_compacted",
                "compact_generation": normalized_generation,
            },
        )

    # LLM: 续跑边界只由宿主在同一请求重新执行、写任何本代事件之前调用一次；只带 recovery 标记里的结构化 cause，
    #   不写正文，也不代表恢复核对已经通过。客户端据此收口上一执行代次的显示，未知 kind 的旧客户端会忽略它。
    # 函数用途: 在原 chunk 流里标出“这一轮被打断后重新开始执行”，让 TUI 关掉旧确认框、旧动画和运行中的旧工具卡。
    def write_turn_resumed(self, cause: str) -> None:
        self.flush()
        self._write_event({"kind": "turn_resumed", "cause": str(cause or "").strip()})

    # LLM: 工具轮数来自结构化 progress，不能从输出文本或动画推断。
    # 函数用途: 返回当前请求已经观察到的最大工具轮号。
    @property
    def observed_tool_rounds(self) -> int:
        """Return exact structured progress observed during this request."""
        return self._observed_tool_rounds

    # LLM: 先拒绝等待新发布，再刷新非等待宿主事件；close不释放raw，在途外部回调仍不可撤销。
    # 函数用途: 收口显示并拒绝迟到等待事件，不把流关闭当作工具或任务成功。
    def close(self) -> None:
        self._liveness_closed = True
        self.flush()
        for sink in (self.main_activity_sink, self.transcript_sink):
            closer = getattr(sink, "close", None)
            if callable(closer):
                try:
                    closer()
                except (OSError, RuntimeError, TypeError, ValueError):
                    pass

    # LLM: 在final持久化前刷新宿主事件并准备已确认显示快照，不因收口flush确认raw；失败不改变模型结果。
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




# LLM: 短接单轮仍不向普通IM发过程；正文已由原commit门提交，退休raw kind无producer，其余事件净化落原chunk/sink并逐出口核still_current。
# 函数用途: 发布 writer 的安全事件与展示时刻，sink 故障不改变任务结果，不创建第二发送路径。
def _publish_writer_event(writer: BufferedChunkStreamWriter, event: dict[str, object], *, still_current: Callable[[], bool] | None = None) -> None:
    from ..common.log_redaction import redact_sensitive_value

    if still_current is not None and not still_current():
        return
    if writer._final_only and not writer.rich_transcript and event.get("kind") in {
        "runtime_progress", "tool_progress", "assistant_commentary", "model_liveness", "tool_liveness",
    }:
        return
    event = redact_sensitive_value(event)
    if writer._verbose_level == "stream" and not writer.rich_transcript:
        event = {**event, "preview_max_chars": writer._im_preview_max_chars}
    if not writer.rich_transcript and event.get("kind") in {"tool_progress", "assistant_commentary", "model_liveness"}:
        event = {**event, "updated_at": display_now(writer._display_timezone).isoformat(timespec="seconds")}
    if still_current is None:
        write_chunk_event(writer.chunk_path, event)
    else:
        write_chunk_event(writer.chunk_path, event, still_current=still_current)
    for sink in (writer.main_activity_sink, writer.transcript_sink):
        if still_current is not None and not still_current():
            return
        if not callable(sink):
            continue
        try:
            sink(event)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass


# LLM: 模型增量与运行进度使用同一刷新公式，各自传入独立计数/时钟；不读或改变任务状态。
# 函数用途: 判断字符量、换行或单调时钟是否达到原发送阈值，避免两种缓冲各维护一套规则。
def _stream_flush_due(latest_text: str, chars: int, last_flush_at: float, flush_chars: int, interval: float) -> bool:
    if chars >= max(1, int(flush_chars)) or latest_text.endswith("\n"):
        return True
    return time.monotonic() - last_flush_at >= max(0.0, float(interval))


# LLM: 宿主提示只由 Gateway 在用户消息落账后、模型执行前调用一次（request_execution._publish_gateway_host_notices）；事件只带
#   HostNotice 的公开字段。IM /progress 不转发这种 kind（飞书从最终回复正文前拿），TUI 画成灰色系统行，同会话窗口经
#   transcript sink 同步并进入最终快照。写在类外是尺寸治理（BufferedChunkStreamWriter 已接近上限）：只调用同模块的事件出口，
#   不另建发送路径。副作用：追加 chunk 流事件。
# 函数用途: 在前台流里发布本轮要告诉用户的宿主提示。
def write_host_notice_events(writer: BufferedChunkStreamWriter, notices: Iterable[object]) -> None:
    writer.flush()
    for notice in notices:
        writer._write_event({"kind": "host_notice", "notice": notice.to_dict()})






# LLM: 私人卡只用于宿主准确 feishu 渠道；恢复标记不是渠道授权，TUI 是否交互仍认原 client capability，不从展示支持猜 IM。
# 函数用途: 把飞书确认交原私人卡桥，其它客户端沿原 stream 审批，安全恢复不丢失原交互能力。
def request_stream_permission(writer,request,cancellation_token):
    if writer.im_approval_agent is not None and not writer.rich_transcript and writer.delivery_channel == "feishu":
        from ..conversation.im_tool_approval import request_foreground_im_permission
        return request_foreground_im_permission(writer.im_approval_agent,request,cancellation_token=cancellation_token)
    return writer._approval.request(request,options=StreamApprovalRequestOptions(
        writer.interactive_approvals,writer.approval_mode_decision_provider,cancellation_token))


# LLM: 只由原sink完整响应提交调用，保原call幂等和显示档位；并发worker同时started时用短锁消除重复提交竞态。
# 函数用途: 将已净化的完整响应提交到原显示段，丢弃旧候选并保留原final-only/通道策略。
def _commit_model_response(writer: BufferedChunkStreamWriter, payload: dict) -> None:
    from .stream_events import committed_model_events

    events = committed_model_events(payload, delivery_channel=writer.delivery_channel, identifiers=writer._identifier_redactions)
    call_id = (payload.get("source") or {}).get("call_id")
    if not events:
        return
    with writer._commit_lock:
        if call_id in writer._committed_model_calls or getattr(writer, "_liveness_closed", False):
            return
        writer._committed_model_calls.add(call_id)
        for event in events:
            if event["kind"] == "assistant_commentary":
                writer._committed_commentary.append(event["text"])
                if writer._commentary_emitted and not writer.rich_transcript and writer._verbose_level != "stream":
                    continue
                writer._commentary_emitted = True
                writer._write_event(event)
            elif writer.rich_transcript:
                writer._write_event(event)
