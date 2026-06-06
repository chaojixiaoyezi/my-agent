
from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace

from ._compression_service import CompressionService
from ._finalization_service import FinalizationService
from ._runtime_params import FinalizeContext
from ._tool_loop_service import ToolLoopService
from .compact_auto_continuation import (
    compact_auto_continuation_decision,
    mark_compact_auto_continued,
)
from .run_task_workspace_writer import attach_run_task_workspace_context
from .runtime.loop_models import RuntimeContextRequest
from .runtime.loop_support import (
    FinalizeParams,
    RunParams,
    _execute_runtime_loop,
    _finalize_params,
    _prepare_runtime_context,
    _runtime_loop_params,
)
from .runtime.run_params import (
    RunKeywordFields,
    run_params_from_keywords,
    run_params_with_materialized_delivery_contract,
    run_params_with_request_id,
)


@dataclass
class _RuntimeServices:

    tool_loop: ToolLoopService
    compression: CompressionService
    finalization: FinalizationService


@dataclass(frozen=True)
class _CompressionSnapshotRequest:
    user_prompt: object
    memories: object
    runtime_injections: object
    routed_context: object
    resume_context_section: object


@dataclass(frozen=True)
class _PromptScopeSnapshot:
    had_prompt: bool
    previous_prompt: str
    had_params: bool
    previous_params: object


@contextmanager
def _current_prompt_scope(agent, user_prompt: str, params: RunParams | None = None):
    had_current_prompt = hasattr(agent, "_current_user_prompt")
    previous_current_prompt = getattr(agent, "_current_user_prompt", "")
    had_current_run_params = hasattr(agent, "_current_run_params")
    previous_current_run_params = getattr(agent, "_current_run_params", None)
    agent._current_user_prompt = user_prompt
    if params is not None:
        agent._current_run_params = params
    snapshot = _PromptScopeSnapshot(
        had_current_prompt,
        previous_current_prompt,
        had_current_run_params,
        previous_current_run_params,
    )
    try:
        yield
    finally:
        _restore_current_prompt(agent, snapshot)


def _restore_current_prompt(agent, snapshot: _PromptScopeSnapshot) -> None:
    if snapshot.had_prompt:
        agent._current_user_prompt = snapshot.previous_prompt
    elif hasattr(agent, "_current_user_prompt"):
        delattr(agent, "_current_user_prompt")
    if snapshot.had_params:
        agent._current_run_params = snapshot.previous_params
    elif hasattr(agent, "_current_run_params"):
        delattr(agent, "_current_run_params")


class SimpleAgentRuntimeMixin:

    _services: _RuntimeServices | None = None

    def _get_services(self) -> _RuntimeServices:
        if self._services is None:
            self._services = _RuntimeServices(
                tool_loop=ToolLoopService(self),
                compression=CompressionService(self),
                finalization=FinalizationService(self),
            )
        return self._services

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        return self._get_services().compression._compress_memories(memories, keep_recent=keep_recent)

    def _build_compression_snapshot_content(
        self,
        *,
        params: _CompressionSnapshotRequest | None = None,
        user_prompt=None,
        memories=None,
        runtime_injections=None,
        routed_context=None,
        resume_context_section=None,
    ):
        from ._compression_service import CompressionSnapshotContentParams

        values = params or _CompressionSnapshotRequest(
            user_prompt, memories, runtime_injections, routed_context, resume_context_section
        )
        return self._get_services().compression._build_compression_snapshot_content(
            CompressionSnapshotContentParams(
                user_prompt=values.user_prompt,
                memories=values.memories,
                runtime_injections=values.runtime_injections,
                routed_context=values.routed_context,
                resume_context_section=values.resume_context_section,
            )
        )

    def run(
        self,
        user_prompt: str,
        *,
        params: RunParams = None,
        inject: list[str] | None = None, prompt_files: list[str] | None = None, save: bool | None = None,
        allowed_tools: list[str] | None = None, granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None, request_id: str | None = None,
        run_id: str | None = None, task_id: str | None = None, task_attributes: dict | None = None,
        delivery_contract: dict | None = None,
        system_prompt_override: str | None = None, source: str | None = None,
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None, recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
        context_scope: str | None = None,
    ):
        params = run_params_from_keywords(
            params,
            RunKeywordFields(
                inject=inject,
                prompt_files=prompt_files,
                save=save,
                allowed_tools=allowed_tools,
                granted_capabilities=granted_capabilities,
                write_boundary=write_boundary,
                request_id=request_id,
                run_id=run_id,
                task_id=task_id,
                task_attributes=task_attributes,
                delivery_contract=delivery_contract,
                system_prompt_override=system_prompt_override,
                source=source,
                resume_context=resume_context,
                recovery_task_refs=recovery_task_refs,
                recovery_content_paths=recovery_content_paths,
                recovery_next_actions=recovery_next_actions,
                on_chunk=on_chunk,
                context_scope=context_scope,
            ),
        )
        return _run_with_params(self, user_prompt, params)

    def _build_finalize_context(self, params: FinalizeParams):
        rp = params.run_params
        return FinalizeContext(
            user_prompt=params.user_prompt,
            final_prompt=params.final_prompt,
            final_response=params.final_response,
            memories=params.memories,
            executed_tools=params.executed_tools,
            archive_tool_calls=params.archive_tool_calls,
            routed_context=params.routed_context,
            resume_context_result=params.resume_context_result,
            runtime_injections=params.runtime_injections,
            compression_snapshot_id=params.compression_snapshot_id,
            compression_snapshot_path=params.compression_snapshot_path,
            compression_applied=params.compression_applied,
            request_id=rp.request_id,
            run_id=rp.run_id,
            task_id=rp.task_id,
            source=rp.source,
            do_save=self.config.auto_save_memory if rp.save is None else rp.save,
            task_attributes=rp.task_attributes,
            recovery_task_refs=rp.recovery_task_refs,
            recovery_content_paths=rp.recovery_content_paths,
            recovery_next_actions=rp.recovery_next_actions,
            tool_rounds=params.tool_rounds,
            compact_auto_continue_depth=rp.compact_auto_continue_depth,
            compact_auto_no_tool_continue_depth=rp.compact_auto_no_tool_continue_depth,
            main_context_bundle_path=params.main_context_bundle_path,
            main_context_bundle_markdown_path=params.main_context_bundle_markdown_path,
            delivery_contract=rp.delivery_contract,
        )

    def remember(self, content: str, *, kind: str = "note"):
        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)


def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
    current_params = run_params_with_materialized_delivery_contract(agent, user_prompt, current_params)
    if not current_params.root_user_prompt:
        current_params = replace(current_params, root_user_prompt=user_prompt)
    current_params = attach_run_task_workspace_context(agent, current_params, user_prompt)
    result = _run_once_with_params(agent, user_prompt, current_params)
    while True:
        decision = compact_auto_continuation_decision(
            result,
            depth=current_params.compact_auto_continue_depth,
        )
        if not decision.should_continue:
            return result
        next_params = _compact_auto_continue_params(current_params, decision.injection, result)
        continued = _run_once_with_params(agent, decision.user_prompt, next_params)
        result = mark_compact_auto_continued(continued, result, depth=next_params.compact_auto_continue_depth)
        current_params = next_params


def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    params = attach_run_task_workspace_context(agent, params, user_prompt)
    root_user_prompt = params.root_user_prompt or user_prompt
    with _current_prompt_scope(agent, user_prompt, params):
        prepared = _prepare_runtime_context(
            agent,
            RuntimeContextRequest(
                user_prompt,
                params.inject,
                params.resume_context,
                params.context_scope,
                allowed_tools=params.allowed_tools,
                granted_capabilities=params.granted_capabilities,
                write_boundary=params.write_boundary,
                request_id=params.request_id,
                run_id=params.run_id,
                task_id=params.task_id,
                source=params.source,
                save=params.save,
                task_attributes=params.task_attributes,
            ),
        )
        loop_result = _execute_runtime_loop(
            agent,
            _runtime_loop_params(user_prompt, prepared, params),
        )
        ctx = agent._build_finalize_context(_finalize_params(root_user_prompt, prepared, loop_result, params))
        return agent._get_services().finalization.finalize(ctx)


def _compact_auto_continue_params(params: RunParams, injection: str, source_result) -> RunParams:
    no_tool_depth = params.compact_auto_no_tool_continue_depth + 1 if _result_has_no_tool_progress(source_result) else 0
    incoming_archive_calls = _merged_archive_tool_calls(
        getattr(source_result, "archive_tool_calls", None),
        _pending_deferred_tool_calls_from_result(source_result),
    )
    return replace(
        params,
        inject=[*_non_compact_auto_injections(params.inject), injection],
        compact_auto_continue_depth=params.compact_auto_continue_depth + 1,
        compact_auto_no_tool_continue_depth=no_tool_depth,
        carried_archive_tool_calls=_merged_archive_tool_calls(
            params.carried_archive_tool_calls,
            incoming_archive_calls,
        ),
    )


def _result_has_no_tool_progress(result) -> bool:
    return int(getattr(result, "tool_rounds", 0) or 0) <= 0 and not list(getattr(result, "executed_tools", None) or [])


def _non_compact_auto_injections(injections: list[str] | None) -> list[str]:
    return [
        item
        for item in list(injections or [])
        if not str(item).lstrip().startswith("# Compact Auto Continuation")
    ]


def _merged_archive_tool_calls(
    existing: list[dict[str, object]] | None,
    incoming: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for record in [*list(existing or []), *list(incoming or [])]:
        if not isinstance(record, dict):
            continue
        key = (
            str(record.get("run_id") or ""),
            str(record.get("tool") or ""),
            str(record.get("source_input") or _record_parameter_path(record)),
            _record_parameters_key(record),
            str(record.get("output_hash") or record.get("sha256") or ""),
            str(record.get("scoped_call_id") or record.get("call_id") or record.get("id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    return merged


def _record_parameter_path(record: dict[str, object]) -> str:
    params = record.get("parameters")
    if not isinstance(params, dict):
        return ""
    for key in ("path", "file_path", "target_path", "output_path", "artifact_ref", "source_ref"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    return ""


def _record_parameters_key(record: dict[str, object]) -> str:
    params = record.get("parameters")
    if not isinstance(params, dict):
        return ""
    try:
        return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return ""


def _pending_deferred_tool_calls_from_result(source_result) -> list[dict[str, object]]:
    packet = getattr(source_result, "memory_compact_auto_continue_packet", None)
    if not isinstance(packet, dict):
        return []
    rows = packet.get("pending_deferred_tool_calls")
    if not isinstance(rows, list):
        work_state = packet.get("work_state_snapshot")
        rows = work_state.get("pending_deferred_tool_calls") if isinstance(work_state, dict) else []
    result: list[dict[str, object]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            result.append(dict(row))
    return result
