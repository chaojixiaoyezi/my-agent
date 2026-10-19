
# LLM: CLI正文候选保持私有；只有同次已验response提交的说明可打印，最终回复仍走原结果出口，联测脱敏/终答去重。
# 模块用途: 显示一次性CLI工具进度、模型过程说明和最终结果，不决定工具或续跑。
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from ..agent.backends import (
    ProviderRecoverableError,
    provider_recoverable_report,
)
from ..agent.gateway_parts.response_renderer import current_context_token_estimate
from .thinking_spinner import ThinkingSpinner


# LLM: 原回调只缓存候选；commit携真实调用身份并走共用公开净化，正文相同的不同调用不去重。
# 类用途: 在工具边界显示模型确认的过程说明，工具状态与最终正文各走原出口。
@dataclass
class _LocalRunChunkWriter:
    """候选保持私有；完整响应确认过程说明，终答仍由原结果出口打印。"""

    spinner: ThinkingSpinner
    stream_state: dict[str, object]
    _committed_calls: set[str] = field(default_factory=set)
    _commentary: list[str] = field(default_factory=list)

    # LLM: str回调只作候选输入，不允许据正文判断完成或直接对外打印。
    # 函数用途: 将普通模型增量交给同一候选缓冲。
    def __call__(self, chunk: str) -> None:
        self.write_model(chunk)

    # LLM: 未通过完整response裁决前不发布；流式和非流式最后均由typed commit确认。
    # 函数用途: 暂存正文增量并停止等待动画，不输出原始候选。
    def write_model(self, chunk: str) -> None:
        if not chunk:
            return
        self.stream_state["model_text"] = (
            str(self.stream_state.get("model_text", "")) + chunk
        )
        self.spinner.stop()

    # LLM: 已验消息沿共用净化再stdout；final不在此打印，避免与原print_run_result重复，工具账不受影响。
    # 函数用途: 按真实调用幂等显示过程说明，非流式也能使用同一入口。
    def commit_model_response(self, payload: dict) -> None:
        from ..agent.gateway_parts.stream_events import committed_model_events

        events = committed_model_events(payload)
        call_id = (payload.get("source") or {}).get("call_id")
        if not events or call_id in self._committed_calls:
            return
        self._committed_calls.add(call_id)
        self.stream_state["model_text"] = ""
        for event in events:
            if event["kind"] == "assistant_commentary":
                self.spinner.stop()
                self._commentary.append(event["text"])
                sys.stdout.write(event["text"] + "\n")
                sys.stdout.flush()

    # LLM: 只返回typed提交的公开正文，不将候选/工具状态当成会话消息。
    # 函数用途: 为原最终持久化提供已经确认的过程说明。
    def assistant_commentary_messages(self) -> tuple[str, ...]:
        return tuple(self._commentary)

    # LLM: 工具状态打印不确认候选正文；说明提交来自更早的同次已验response边界。
    # 函数用途: 沿原CLI路径显示结构化工具状态及其兼容文本。
    def write_progress(self, _event: dict[str, object], legacy_text: str) -> None:
        if not legacy_text:
            return
        self.stream_state["seen"] = True
        self.stream_state["text"] = (
            str(self.stream_state.get("text", "")) + legacy_text
        )
        self.spinner.stop()
        sys.stdout.write(legacy_text)
        sys.stdout.flush()


# LLM: 工厂只构造原本地writer，不开进程/线程或模型请求；调用方持有同一stream_state。
# 函数用途: 创建一次性CLI的候选缓冲与已验说明显示出口。
def make_run_chunk_writer(spinner: ThinkingSpinner, stream_state: dict[str, object]):
    return _LocalRunChunkWriter(spinner, stream_state)


# LLM: Print model-authored final content separately from program-owned runtime diagnostics;
# typed transcript degradation is observable but must never rewrite or replace the model body.
# 函数用途: 输出 CLI 最终回复、运行摘要及结构化降级提示，避免把系统诊断混入模型正文。
def print_run_result(result, *, show_prompt: bool, streamed_text: str = "") -> None:
    if show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    pending_response = _unstreamed_final_response(result, streamed_text)
    if pending_response:
        if streamed_text and not streamed_text.endswith("\n"):
            print()
        print(pending_response)
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}; routed_rules={result.memory_route_matches}; "
        f"ctx_tokens≈{current_context_token_estimate(result)}; "
        f"prompt_tokens≈{result.prompt_token_estimate}; inject_tokens≈{result.runtime_injection_token_estimate}; "
        f"archive_events={result.archive_events}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}; "
        f"resume_tokens≈{result.memory_resume_context_token_estimate}]"
    )
    if getattr(result, "conversation_persist_degraded", False) is True:
        detail = str(getattr(result, "conversation_persist_error", "") or "").strip()
        print(
            "[conversation_persist_degraded] 最终回复已返回，但 assistant 会话记录未可靠落账。"
            + (f" detail={detail}" if detail else "")
        )
    _print_compact_suggestion(result)


def run_exit_code(result) -> int:
    raw_status = getattr(result, "runtime_status", "ok")
    status = raw_status.strip() if isinstance(raw_status, str) else "ok"
    return 0 if status in {"", "ok"} else 2


def provider_recoverable_cli_report(agent, exc: ProviderRecoverableError) -> str:
    return provider_recoverable_report(
        exc,
        timeout_seconds=getattr(getattr(agent, "config", None), "request_timeout", ""),
    )


# LLM: 内部response可含原协议和秘密标签；完整公开投影先于streamed比较，model_response只用于安全同文去重，不能复活拒绝正文。
# 函数用途: 取得尚未上屏的安全终答，保原最终正文与宿主附加字段的结构边界。
def _unstreamed_final_response(result: object, streamed_text: str) -> str:
    """Return only the committed final text not already delivered by streaming.

    The model-authored body and program-owned additions are separate structured
    fields on ``AgentRunResult``.  A streamed model body may be suppressed only
    in the safe direction: it is the exact tail of delivered model output and
    the exact prefix of the committed response.  This preserves the rendered
    final-item state and 长期助手' prefix-only stream matching without inspecting
    what the prose says.
    """
    from ..agent.gateway_parts.stream_events import public_model_text

    response = public_model_text(getattr(result, "response", ""), max_chars=0, delivery_channel="", identifiers=())
    if not response:
        return ""
    model_response = public_model_text(getattr(result, "model_response", ""), max_chars=0, delivery_channel="", identifiers=()).rstrip()
    delivered = public_model_text(streamed_text, max_chars=0, delivery_channel="", identifiers=()).rstrip()
    if (
        model_response
        and delivered.endswith(model_response)
        and response.startswith(model_response)
    ):
        return response[len(model_response) :].lstrip("\n")
    return "" if response in delivered else response


def _print_compact_suggestion(result) -> None:
    if not result.memory_compact_suggested:
        return
    print(f"[compact_suggestion={result.memory_compact_status}; {result.memory_compact_message}]")
    print(
        "[compact_auto="
        f"{result.memory_compact_auto_status}; next={result.memory_compact_auto_next_action}; "
        f"tools={result.memory_compact_auto_tool_execution}; "
        f"continue_ready={result.memory_compact_auto_continue_ready}; "
        f"apply_id={result.memory_compact_auto_apply_id or '-'}]"
    )
    for command in result.memory_compact_commands or []:
        print(f"- {command}")
