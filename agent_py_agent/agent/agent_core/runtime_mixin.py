from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace

from ._compression_service import CompressionService
from ._finalization_service import FinalizationService
from ._runtime_params import FinalizeContext
from .compact_auto_continuation import (
    compact_auto_continuation_decision,
    mark_compact_auto_continued,
)
from .run_task_workspace_writer import (
    attach_run_task_workspace_context,
    finish_run_task_workspace_if_needed,
)
from .runtime.live_archive import update_runtime_fact_terminal_if_enabled
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
    run_params_with_request_id,
)


@dataclass
class _RuntimeServices:
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
    had_skills: bool
    previous_skills: object


@contextmanager
def current_prompt_scope(agent, user_prompt: str, params: RunParams | None = None):
    """Expose one thread-local run/tool scope without requiring a model turn."""
    had_current_prompt = hasattr(agent, "_current_user_prompt")
    previous_current_prompt = getattr(agent, "_current_user_prompt", "")
    had_current_run_params = hasattr(agent, "_current_run_params")
    previous_current_run_params = getattr(agent, "_current_run_params", None)
    had_current_skills = hasattr(agent, "_current_skill_snapshot")
    previous_current_skills = getattr(agent, "_current_skill_snapshot", None)
    agent._current_user_prompt = user_prompt
    if params is not None:
        agent._current_run_params = params
    agent._current_skill_snapshot = _turn_skill_snapshot(agent)
    snapshot = _PromptScopeSnapshot(
        had_current_prompt,
        previous_current_prompt,
        had_current_run_params,
        previous_current_run_params,
        had_current_skills,
        previous_current_skills,
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
    if snapshot.had_skills:
        agent._current_skill_snapshot = snapshot.previous_skills
    elif hasattr(agent, "_current_skill_snapshot"):
        delattr(agent, "_current_skill_snapshot")


def _turn_skill_snapshot(agent):
    provider = getattr(agent, "skill_snapshot_for_run_scope", None)
    if not callable(provider):
        return None
    workspace = (
        getattr(agent, "_current_run_task_workspace", "")
        or getattr(agent, "effective_workspace_root", "")
        or getattr(agent, "root", ".")
    )
    return provider(workspace)


class SimpleAgentRuntimeMixin:
    _services: _RuntimeServices | None = None

    def _get_services(self) -> _RuntimeServices:
        if self._services is None:
            self._services = _RuntimeServices(
                compression=CompressionService(self),
                finalization=FinalizationService(self),
            )
        return self._services

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        return self._get_services().compression._compress_memories(
            memories, keep_recent=keep_recent
        )

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

    # LLM: run 的工具权限只接受 allowed_tools；已删除从未参与判定的 granted_capabilities 假接口。
    # 函数用途: 规范化一次用户请求并进入共享运行、压缩、工具和保存主链。
    def run(
        self,
        user_prompt: str,
        *,
        params: RunParams = None,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
        allowed_tools: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        task_attributes: dict | None = None,
        delivery_contract: dict | None = None,
        system_prompt_override: str | None = None,
        source: str | None = None,
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None,
        recovery_content_paths: list[str] | None = None,
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
            context_scope=str(getattr(rp, "context_scope", "default") or "default"),
            active_turn_user_inputs=list(params.active_turn_user_inputs or []),
            tool_runtime_evidence=dict(params.tool_runtime_evidence or {}),
        )

    # LLM: Programmatic/admin remember must traverse the same Candidate and Promotion services as
    # the model tool; direct JsonlMemory.add here would restore a hidden long-term write bypass.
    # 函数用途: 将本地用户明确确认的具体事实、事件或项目知识审核并晋升，返回正式记录。
    def remember(self, content: str, *, kind: str = "fact"):
        from ..memory_store.candidate_models import CandidateObservation, MemoryScope

        normalized_kind = str(kind or "fact").strip().lower()
        type_map = {
            "fact": "long_term_fact",
            "event": "event",
            "project": "project",
        }
        if normalized_kind not in type_map:
            raise ValueError(
                "remember kind 只允许 fact/event/project；人格偏好请使用 update_persona"
            )
        text = str(content or "").strip()
        if not text:
            raise ValueError("remember content 不能为空")
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]
        candidate = self.memory_candidates.observe(
            CandidateObservation(
                candidate_type=type_map[normalized_kind],
                content=text,
                subject_key=f"manual.{normalized_kind}.{digest}",
                scope=MemoryScope("personal", "personal", "本地用户明确保存", ""),
                origin="reviewed",
                confidence=1.0,
                proposed_action="add",
                promotion_target="long_term",
                observation_id=f"manual-remember:{digest}",
            )
        )
        if candidate.status == "pending_review":
            candidate = self.memory_promotion.review(
                candidate.candidate_id,
                approved=True,
                reviewer="local-user-explicit",
                note="本地 remember 命令显式确认。",
            )
        result = self.memory_promotion.promote(
            candidate.candidate_id,
            reviewer="local-user-explicit",
            confirmed=True,
        )
        if not result.promoted:
            raise RuntimeError(f"remember promotion failed: {result.reason_code}")
        entry_id = result.promotion_ref.rsplit("#", 1)[-1]
        for record in self.memory.all():
            if record.entry_id == entry_id:
                return record
        raise RuntimeError("remember promotion succeeded without a formal memory record")

    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)


# LLM: 顶层运行和所有自动 Compact 续接共用这一返回缝隙；只有最终不再续接时才能投影 standalone 终态。
# 函数用途: 执行一次完整请求，必要时续接 Compact，并在真正结束时收尾任务工作区。
def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
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
            finish_run_task_workspace_if_needed(agent, current_params, result)
            return result
        next_params = _compact_auto_continue_params(current_params, decision.injection, result)
        continued = _run_once_with_params(agent, decision.user_prompt, next_params)
        result = mark_compact_auto_continued(
            continued, result, depth=next_params.compact_auto_continue_depth
        )
        current_params = next_params


def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    params = attach_run_task_workspace_context(agent, params, user_prompt)
    root_user_prompt = params.root_user_prompt or user_prompt
    with current_prompt_scope(agent, user_prompt, params):
        if agent.config.enable_tools:
            agent.tools.prepare_for_run()
        prepared = _prepare_runtime_context(
            agent,
            RuntimeContextRequest(
                user_prompt,
                params.inject,
                params.resume_context,
                params.context_scope,
                allowed_tools=params.allowed_tools,
                write_boundary=params.write_boundary,
                request_id=params.request_id,
                run_id=params.run_id,
                task_id=params.task_id,
                source=params.source,
                save=params.save,
                task_attributes=params.task_attributes,
            ),
        )
        try:
            loop_result = _execute_runtime_loop(
                agent,
                _runtime_loop_params(user_prompt, prepared, params),
            )
            ctx = agent._build_finalize_context(
                _finalize_params(root_user_prompt, prepared, loop_result, params)
            )
            return agent._get_services().finalization.finalize(ctx)
        except BaseException as exc:
            # 会话运行时 and 长期助手 both close every run through one terminal
            # lifecycle path.  Keep the durable runtime fact aligned with the
            # background claim even when the model/provider raises.
            update_runtime_fact_terminal_if_enabled(agent, params, exc)
            raise


def _compact_auto_continue_params(params: RunParams, injection: str, source_result) -> RunParams:
    from ..conversation.active_turn_input import merge_active_turn_user_inputs

    incoming_archive_calls = _merged_archive_tool_calls(
        getattr(source_result, "archive_tool_calls", None),
        _pending_deferred_tool_calls_from_result(source_result),
    )
    made_tool_progress = _result_added_tool_progress(
        params,
        source_result,
        incoming_archive_calls,
    )
    no_tool_depth = 0 if made_tool_progress else params.compact_auto_no_tool_continue_depth + 1
    return replace(
        params,
        inject=[*_non_compact_auto_injections(params.inject), injection],
        compact_auto_continue_depth=params.compact_auto_continue_depth + 1,
        compact_auto_no_tool_continue_depth=no_tool_depth,
        carried_archive_tool_calls=_merged_archive_tool_calls(
            params.carried_archive_tool_calls,
            incoming_archive_calls,
        ),
        carried_active_turn_user_inputs=merge_active_turn_user_inputs(
            params.carried_active_turn_user_inputs,
            getattr(source_result, "active_turn_user_inputs", None),
        ),
    )


def _result_added_tool_progress(
    params: RunParams,
    result: object,
    incoming_archive_calls: list[dict[str, object]],
) -> bool:
    """Count only tool records added by this continuation.

    ``tool_rounds`` and ``executed_tools`` are cumulative after a compact
    continuation.  Reusing those counters made an idle continuation look busy
    forever.  The durable archive is the structured source of truth: compare
    it with the records already carried into this run and ignore the synthetic
    record that merely says a tool was deferred for compact.
    """

    if hasattr(result, "archive_tool_calls"):
        carried = _merged_archive_tool_calls(params.carried_archive_tool_calls, None)
        merged = _merged_archive_tool_calls(carried, incoming_archive_calls)
        new_records = merged[len(carried) :]
        return any(_archive_record_is_tool_progress(record) for record in new_records)
    # Compatibility for focused callers that predate the typed archive field.
    return not _result_has_no_tool_progress(result)


def _archive_record_is_tool_progress(record: dict[str, object]) -> bool:
    tool_name = str(record.get("tool") or "").strip()
    error_code = str(record.get("error_code") or "").strip().upper()
    if error_code == "CONTEXT_COMPACT_DEFERRED":
        return False
    return bool(tool_name)


def _result_has_no_tool_progress(result) -> bool:
    return int(getattr(result, "tool_rounds", 0) or 0) <= 0 and not list(
        getattr(result, "executed_tools", None) or []
    )


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
