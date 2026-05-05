"""FinalizationService: result finalization, archiving, and token estimation."""

from __future__ import annotations

import time as time_module

from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
    write_recovery_snapshot,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.snapshots import (
    RecoverySnapshotInput,
)
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ._runtime_params import (
    ArchiveRunParams,
    EstimateTokenParams,
    FinalizeContext,
    WriteRecoverySnapshotParams,
)
from .models import AgentRunResult


class FinalizationService:
    """Service for result finalization, archiving, and token estimation."""

    def __init__(self, agent):
        self._agent = agent

    def finalize(self, ctx: FinalizeContext):
        """Finalize run result: archive, snapshots, token estimation, and return value."""
        assert ctx.final_response is not None
        run_request_id = ctx.request_id or f"run-{time_module.time_ns()}"

        archive_params = ArchiveRunParams(
            do_save=ctx.do_save,
            user_prompt=ctx.user_prompt,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
        )
        archive_result = self._archive_run_if_needed(archive_params)

        recovery_params = WriteRecoverySnapshotParams(
            do_save=ctx.do_save,
            recovery_snapshot=ctx.recovery_snapshot,
            user_prompt=ctx.user_prompt,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
            recovery_task_refs=ctx.recovery_task_refs,
            recovery_content_paths=ctx.recovery_content_paths,
            recovery_next_actions=ctx.recovery_next_actions,
            routed_context=ctx.routed_context,
        )
        snapshot_result = self._write_recovery_snapshot_if_needed(recovery_params)
        turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
        token_params = EstimateTokenParams(
            user_prompt=ctx.user_prompt,
            runtime_injections=ctx.runtime_injections,
            memories=ctx.memories,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            turn_id=turn_id,
        )
        token_ledger = self._estimate_token_usage(token_params)

        return self._build_agent_run_result(ctx, archive_result, snapshot_result, token_ledger)

    def _archive_run_if_needed(self, params: ArchiveRunParams):
        """Archive run turn if do_save is enabled."""
        if not params.do_save:
            return None
        self._agent.memory.add("user", params.user_prompt)
        self._agent.memory.add(
            "agent", params.final_response.text, tags=[params.final_response.backend]
        )
        return archive_run_turn(
            ArchiveRunTurnParams(
                root=self._agent.root,
                ctx=ArchiveTurnContext(
                    session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                    request_id=params.run_request_id,
                    run_id=params.run_id,
                    task_id=params.task_id,
                    user_prompt=params.user_prompt,
                    response_text=params.final_response.text,
                    backend=params.final_response.backend,
                    tool_calls=params.archive_tool_calls or [],
                    source=params.source,
                    archive_level=int(getattr(self._agent.config, "memory_archive_level", 3)),
                ),
            )
        )

    def _write_recovery_snapshot_if_needed(self, params: WriteRecoverySnapshotParams):
        """Write recovery snapshot if enabled."""
        should_write = bool(getattr(self._agent.config, "memory_hook_enabled", True)) and (
            params.do_save if params.recovery_snapshot is None else bool(params.recovery_snapshot)
        )
        if not should_write:
            return None
        return write_recovery_snapshot(
            self._agent.root,
            params=RecoverySnapshotInput(
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                user_prompt=params.user_prompt,
                response_text=params.final_response.text,
                backend=params.final_response.backend,
                source=params.source,
                request_id=params.run_request_id,
                run_id=params.run_id,
                task_id=params.task_id,
                status="ok",
                tool_calls=params.archive_tool_calls,
                task_refs=params.recovery_task_refs or [],
                content_paths=[
                    *(params.recovery_content_paths or []),
                    *(getattr(params.routed_context, "required_read_paths", None) or []),
                    *(getattr(params.routed_context, "candidate_paths", None) or []),
                ],
                next_actions=params.recovery_next_actions or [],
                archive_level=int(getattr(self._agent.config, "memory_hook_archive_level", 3)),
            ),
        )

    def _estimate_token_usage(self, params: EstimateTokenParams):
        """Estimate and record token usage."""
        input_tokens = (
            estimate_tokens(params.user_prompt)
            + estimate_tokens(params.runtime_injections)
            + estimate_tokens([getattr(memory, "content", "") for memory in params.memories])
        )
        output_tokens = estimate_tokens(params.final_response.text)
        tool_tokens = estimate_tokens(params.archive_tool_calls)
        ledger = append_session_token_usage(
            self._agent.root,
            usage=TurnTokenUsage(
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                turn_id=params.turn_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_tokens=tool_tokens,
                created_at=str(time_module.time()),
            ),
        )
        return {
            "turn": int(ledger["turn_total"]),
            "cumulative": int(ledger["cumulative_tokens"]),
        }

    def _build_agent_run_result(
        self, ctx: FinalizeContext, archive_result, snapshot_result, token_ledger
    ):
        """Build the final AgentRunResult object."""
        routed_context = ctx.routed_context
        return AgentRunResult(
            prompt=ctx.final_prompt,
            response=ctx.final_response.text,
            backend=ctx.final_response.backend,
            used_memories=len(ctx.memories),
            tool_rounds=ctx.tool_rounds,
            executed_tools=ctx.executed_tools,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=archive_result.event_count if archive_result else 0,
            archive_token_estimate=archive_result.token_estimate if archive_result else 0,
            prompt_token_estimate=estimate_tokens(ctx.final_prompt),
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections)
            if ctx.runtime_injections
            else 0,
            recovery_snapshot_id=snapshot_result.snapshot_id if snapshot_result else "",
            recovery_snapshot_path=snapshot_result.path if snapshot_result else "",
            recovery_snapshot_error=snapshot_result.error if snapshot_result else "",
            recovery_snapshot_token_estimate=snapshot_result.token_estimate
            if snapshot_result
            else 0,
            memory_resume_context_injected=ctx.resume_context_result.injected
            if ctx.resume_context_result
            else False,
            memory_resume_context_query=ctx.resume_context_result.query
            if ctx.resume_context_result
            else "",
            memory_resume_context_matches=(
                ctx.resume_context_result.archive_match_count
                + ctx.resume_context_result.local_match_count
                + ctx.resume_context_result.task_fact_source_count
            )
            if ctx.resume_context_result
            else 0,
            memory_resume_context_token_estimate=estimate_tokens(
                ctx.resume_context_result.context_block
            )
            if ctx.resume_context_result and ctx.resume_context_result.injected
            else 0,
            memory_resume_context_error=ctx.resume_context_result.error
            if ctx.resume_context_result
            else "",
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=token_ledger["turn"],
            cumulative_token_estimate=token_ledger["cumulative"],
        )