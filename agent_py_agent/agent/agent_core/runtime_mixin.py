# LLM: 主代理真实 task/run/attempt 必须在模型前完成绑定并交给宿主持久保存；
# 正常结束、异常和 Compact 续接沿用同一权威，不从会话展示编号反推执行身份。
# 模块用途: 执行主代理的模型工具循环、上下文续接与精确执行轮收尾，不替子代理调度另建身份。
from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..conversation.authority import conversation_transcript_is_authoritative
from ..conversation.task_state import conversation_task_link_is_terminal
from ..runtime_db.repository import AGENT_RUN_TERMINAL_STATUSES
from ._finalization_service import FinalizationService
from ._runtime_params import FinalizeContext
from .cli_run_conversation import (
    _is_cli_run,
    bind_cli_run_conversation,
    persist_cli_run_assistant,
)
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
    finalization: FinalizationService




@dataclass(frozen=True)
class _PromptScopeSnapshot:
    had_prompt: bool
    previous_prompt: str
    had_params: bool
    previous_params: object
    had_skills: bool
    previous_skills: object


# LLM: Gateway conversation compact may release only this attempt's pre-provider active inputs;
# callers outside agent_core use this narrow runtime boundary instead of importing private leaves.
# 函数用途: 在 Compact 续跑前释放本轮尚未提交给模型的活动回合补充消息。
def release_active_turn_inputs_for_compact(
    agent: object,
    params: object,
) -> tuple[str, ...]:
    from .runtime.guidance import release_reserved_turn_input_after_attempt

    return release_reserved_turn_input_after_attempt(agent, params)


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
                finalization=FinalizationService(self),
            )
        return self._services


    # LLM: run 的工具权限只接受 allowed_tools；工作片冻结模型及宿主会话身份，探针/Compact/子代理不能混用头或模型。
    # 函数用途: 规范化一次用户请求，绑定本轮模型配置，再进入共享运行、压缩、工具和保存主链。
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
        from ..backends.provider_headers import provider_runtime_scope
        from ..settings.model_scope import selected_model_scope

        attrs = params.task_attributes or {}
        with provider_runtime_scope(self, params), selected_model_scope(
            self, inherited=params.context_scope == "task_local",
            thread_id=str(attrs.get("conversation_thread_id") or ""),
        ):
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
            canonical_native_messages=list(params.canonical_native_messages or []),
            on_chunk=rp.on_chunk,
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


# LLM: 主代理每轮必须挂到 params.task_id 对应的权威根 run；run_id 命中旧任务时
# 必须丢弃该行并按 task 回退，绝不能仅凭线程级旧身份跨任务创建 attempt。
#   Gateway 需要在模型/工具之前收到本次实际 DB 身份，不能从后续会话展示任务反推恢复身份。
# 函数用途: 登记主代理数据库权威链并回填 attempt；同步宿主恢复凭据，落盘失败时不进入模型。
def _bind_main_agent_authority(agent, params: RunParams) -> RunParams:
    """MANAGED 下主代理 run 登记权威链（seq 255 单一闭合：root/main AgentRun）。

    主代理与子代理共用 agent.run：子代理 run 的权威链由 create_run
    （record_run_creation）+ prepare_runner_attempt（create_attempt 轮换）
    登记，runner 上下文非空 → 此处跳过（结构化信号，不重复轮换）。
    主代理 run 无登记 → record_run_creation 建 root Task/TaskRun/AgentRun/
    首 attempt；compact/账本续跑轮（同 run_id 已登记）→ create_attempt
    轮换 current pointer（G4 语义：旧 attempt 非终态操作转 UNKNOWN）。

    返回 replace 后的 params（attempt_id=DB current attempt，工具循环
    `_loop_attempt_id` 回落读 params 即拿到权威 attempt，与子代理同源）；
    LOCAL_UNMANAGED（无 repo）/无 run 上下文/子代理上下文 → 原样返回，
    投影 id 行为不变。
    """
    from .runner.context import current_subagent_run_id

    if current_subagent_run_id(agent):
        return params  # 子代理 runner：登记链已由 create_run + prepare_runner_attempt 建立
    subagents = getattr(agent, "subagents", None)
    repo = getattr(subagents, "runtime_db", None)
    run_id = str(params.run_id or "").strip()
    if repo is None or not run_id:
        return params  # LOCAL_UNMANAGED / 无 run 上下文 → 投影（显式选择）
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    home_paths = getattr(agent, "home_paths", None)
    owner_id = str(getattr(home_paths, "owner_id", "") or "local/main").strip()
    # root Task 身份对齐门比对键：conversation_task_id 取调用方 task_id
    # （run scope 的 task_id 兜底 = run_id），保证 require_authority 的
    # tasks.task_id 比对与调用者声明一致，无需再回存 runtime_authority attrs。
    task_id = str(params.task_id or run_id).strip()
    row = repo.agent_run_for_run_id(run_id)
    if row is not None and repo.task_id_for_run_id(run_id) != task_id:
        # 旧版本曾把同一 thread 的所有后台轮都登记成 bg-main-{thread_id}。
        # 新任务再次命中该 legacy run 时只能视为未命中；否则下面 create_attempt
        # 会把新任务的工具调用挂到旧任务，授权门随后必然拒绝整轮工具。
        row = None
    if row is None:
        # R1-03 补漏：主代理续跑身份（bg-main-thread-{thread}）可能与请求身份
        # （req_{id}）不同，按 run_id 查不到对方登记。回退按 task 查主链
        # role='main' root run——同一 task 已登记 → create_attempt 续挂
        # （挂载闸轮换 generation + 执行权锁，终态放行由任务级闸裁决），
        # 禁止分裂第二棵 run 树（真机实证：unfinished 任务续跑分裂出
        # taskrun/agentrun 双份，create_attempt 挂载闸被绕过）。
        row = repo.main_agent_run_for_task(task_id)
    if row is None:
        record = repo.record_run_creation(
            owner_id=owner_id,
            goal=str(getattr(params, "root_user_prompt", "") or "")[:200],
            conversation_task_id=task_id,
            thread_id=str(attrs.get("conversation_thread_id") or "").strip(),
            run_id=run_id,
            role="main",
        )
        attempt_id = str(record.get("attempt_id") or "").strip()
        agent_run_id = str(record.get("agent_run_id") or "")
        authority_run_id = run_id
    else:
        attempt = repo.create_attempt(str(row["agent_run_id"]))
        attempt_id = str(attempt["attempt_id"] or "").strip()
        agent_run_id = str(row["agent_run_id"] or "")
        authority_run_id = str(row["run_id"] or "")
    if not attempt_id:
        return params
    publish = getattr(params.conversation_task_binding_callback, "bind_runtime_authority", None)
    if callable(publish):
        try:
            if publish({
                "task_id": task_id, "run_id": authority_run_id, "invocation_run_id": run_id,
                "agent_run_id": agent_run_id, "attempt_id": attempt_id,
            }) is not True:
                raise RuntimeError("主代理执行身份无法可靠保存，尚未进入模型或工具")
        except Exception as exc:
            # 这时模型尚未进入；沿用精确 attempt 收口，不能把未开始的轮次留成永远 running。
            _settle_main_agent_run_status(
                agent, run_id=authority_run_id, attempt_id=attempt_id,
                runtime_status="cancelled" if isinstance(exc, InterruptedError) else "failed",
                runtime_reason="runtime_authority_publish_failed",
            )
            raise
    return replace(params, attempt_id=attempt_id)


# 审计账本终态别名：运行时语义 → agent_runs 终态串（只认结构化字段）。
_RUN_STATUS_ALIASES = {"ok": "done", "user_stop": "cancelled", "conversation_control": "cancelled"}


def _settle_main_agent_run_status(
    agent,
    *,
    run_id: str,
    attempt_id: str,
    runtime_status: str,
    runtime_reason: str = "",
    runtime_source: str = "",
    tool_rounds: int = 0,
    cli_one_shot: bool = False,
    continuation_seq: int = 0,
) -> None:
    """把主代理 run 的真实终态落进权威审计账本（fail-silent）。

    修复②（run 级审计终态）：/ask 路径此前 agent_runs.status 恒 'created'、
    attempt 恒 'running'、无 run 级完成事件。这里在 run 真实结束时按
    结构化字段收口：runtime_status=='ok' → 'done'；cancelled 族
    （user_stop/conversation_control）→ 'cancelled'；failed 等直接落账。

    R1-03（收口闸）：unfinished/blocked/needs_user_input/approval_required
    等是任务级可恢复 runtime_status（返工门/等待用户输入后还会续跑），
    不是 agent_runs.status 合法终态；它们只关闭本次 AgentAttempt、释放工具
    权限，run 保持 created，发现层下轮显式 create_attempt 后再继续。
    LOCAL_UNMANAGED（无 repo）/查无 run → noop。
    审计是附加保证，任何失败绝不反噬执行路径。

    CLI 一次性 run 例外（问题C同族, 2026-08-14 真机）：source=cli_run 的
    one-shot run 结束后**没有** gateway 发现层/wake 循环再驱动它。此前
    blocked/unfinished 等非终态不 settle → attempt 永卡 running/ended_at=0
    （testbox local/main 遗留 20+ 条 created/running，aiohttp 首轮 break 后
    无 ended_at）。CLI 一次性 run 真实结束即兜底落 failed 终态，原始
    runtime_status/runtime_reason 保留在 payload 证据，绝不写 DONE 撒谎；
    gateway 等可续跑路径行为完全不变。

    2026-08-14 根因3 设计 v2（审查意见3）轮/任务终态分层：**续跑轮**
    （continuation_seq>0）的 unfinished/blocked 是任务级可恢复语义——由
    resume_loop 决定是否继续，此处不落 failed（保留任务级非终态）；仅首轮
    （seq=0）保持 one-shot 兜底。首轮失败账完整保留，续跑轮不覆盖。
    """
    terminal = _RUN_STATUS_ALIASES.get(runtime_status, runtime_status or "")
    if not terminal or terminal in ("", "created", "running"):
        return
    if terminal not in AGENT_RUN_TERMINAL_STATUSES:
        # 缺口E(双席复核 seq1835): 首轮可续跑族 unfinished 也保留任务级非终态
        # (resume_loop 会续跑), 不落 failed——仅不可续跑族(blocked/协议违规/
        # UNKNOWN, 共享 gate 判定 False)才 one-shot 兜底 failed(问题C同族
        # 兜底保留)。续跑轮非终态一律不落账。
        if cli_one_shot and runtime_status and int(continuation_seq or 0) <= 0:
            from ..conversation.runtime import should_continue_task

            # 共享 gate: 可续跑族(True) → 不落 failed(保留任务级非终态);
            # 不可续跑族(False) → 兜底 failed。
            should, _ = should_continue_task(
                SimpleNamespace(
                    runtime_status=runtime_status,
                    runtime_reason=runtime_reason,
                    runtime_source=runtime_source,
                )
            )
            if not should:
                terminal = "failed"
            else:
                terminal = ""  # 可续跑族只关闭 attempt，run 留给 resume_loop
        else:
            terminal = ""  # 非终态只关闭 attempt，避免 status_conflict 噪音
    if not run_id or not attempt_id:
        return
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return  # LOCAL_UNMANAGED：显式选择本地投影库，无权威账本
    try:
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            return
        payload = {
            "status": terminal or "attempt_done",
            "runtime_status": runtime_status,
            "runtime_reason": runtime_reason,
            "runtime_source": runtime_source,
            "tool_rounds": int(tool_rounds or 0),
        }
        if terminal:
            repo.settle_agent_run(
                agent_run_id=str(row["agent_run_id"]),
                status=terminal,
                attempt_id=attempt_id,
                payload=payload,
            )
        else:
            repo.settle_agent_attempt(
                agent_run_id=str(row["agent_run_id"]),
                attempt_id=attempt_id,
                payload=payload,
            )
    except Exception:  # noqa: BLE001 审计收口失败不回吐执行
        pass


# LLM: Child Compact and active-Goal boundaries stay in the exact outer runner; only that lifecycle may finally settle them.
# 函数用途: 子代理压缩或 Goal 下一轮不能提前注销执行凭证；错误、停止和已完成目标仍正常收口。
def _task_local_continuation_keeps_attempt_open(agent, params: RunParams, result: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if str(getattr(params, "context_scope", "") or "") != "task_local" or not conversation_transcript_is_authoritative(attrs):
        return False
    if str(getattr(result, "runtime_status", "") or "") == "context_overflow":
        return True
    from types import SimpleNamespace

    from ..conversation.goal_delegation import active_delegated_goal_after_turn

    task = SimpleNamespace(id=params.run_id, agent_thread_id=str((attrs or {}).get("agent_thread_id") or ""))
    return bool(task.agent_thread_id and active_delegated_goal_after_turn(agent, task, result) is not None)


# LLM: This closeout owns standalone/main terminalization, but a delegated authoritative
# ConversationThread Compact/active Goal boundary remains inside the same authorized runner.
# 函数用途: 真正结束时收口权威 run/attempt；子代理压缩和 Goal 下一轮不提前收口。
def _settle_main_agent_run(agent, params: RunParams, result) -> None:
    """正常返回路径收口：runtime_status=='ok' → 'done'，否则透传终态。

    CLI 一次性 run（source=cli_run）结束后非终态（blocked/unfinished 等）兜底
    落 failed，杜绝 attempt 悬挂——见 _settle_main_agent_run_status 注释。
    """
    if _task_local_continuation_keeps_attempt_open(agent, params, result):
        return
    runtime_status = str(getattr(result, "runtime_status", "") or "").strip()
    _settle_main_agent_run_status(
        agent,
        run_id=str(params.run_id or "").strip(),
        attempt_id=str(params.attempt_id or "").strip(),
        runtime_status=runtime_status,
        runtime_reason=str(getattr(result, "runtime_reason", "") or "").strip(),
        runtime_source=str(getattr(result, "runtime_source", "") or "").strip(),
        tool_rounds=int(getattr(result, "tool_rounds", 0) or 0),
        cli_one_shot=_is_cli_run(params),
        continuation_seq=int(getattr(params, "continuation_seq", 0) or 0),
    )
    _settle_terminal_conversation_task_run(agent, params)


# LLM: A TaskRun spans every foreground/background slice and descendant of one
# conversation task. The durable link—not a turn-local flag or model prose—authorizes
# closeout, while RuntimeDB proves the exact agent tree terminal. This function runs on
# root and child terminal edges so either side of their race can complete the same CAS.
# 函数用途: 在任一代理执行收口后核对持久会话任务状态，并幂等关闭整棵任务执行总账。
def _settle_terminal_conversation_task_run(agent: object, params: object) -> None:
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    store = getattr(agent, "conversation_store", None)
    if repo is None or store is None:
        return
    run_id = str(getattr(params, "run_id", "") or "").strip()
    task_id = str(getattr(params, "task_id", "") or "").strip()
    try:
        row = repo.agent_run_for_run_id(run_id) if run_id else None
        if row is None and task_id:
            row = repo.main_agent_run_for_task(task_id)
        if row is None or str(row["status"] or "") not in AGENT_RUN_TERMINAL_STATUSES:
            return
        task_run_id = str(row["task_run_id"] or "").strip()
        task_run = repo.get_task_run(task_run_id)
        if task_run is None:
            return
        canonical_task_id = str(task_run["task_id"] or "").strip()
        link = store.load_task_link(canonical_task_id)
        link_status = str(getattr(link, "status", "") or "").strip().lower()
        if link is None or not conversation_task_link_is_terminal(link_status):
            return
        repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=task_run_id,
            task_id=canonical_task_id,
            operator="conversation-runtime",
            reason=f"conversation_task_{link_status}",
        )
    except Exception:  # noqa: BLE001 审计投影失败不反噬已完成的用户任务
        return


def _settle_main_agent_run_exception(agent, params: RunParams, exc: BaseException) -> None:
    """异常路径收口：InterruptedError → 'cancelled'，其余 → 'failed'。"""
    status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
    _settle_main_agent_run_status(
        agent,
        run_id=str(params.run_id or "").strip(),
        attempt_id=str(params.attempt_id or "").strip(),
        runtime_status=status,
        runtime_reason=type(exc).__name__,
    )
    _settle_terminal_conversation_task_run(agent, params)


# LLM: Authority binding replaces the transport attempt id with the RuntimeDB attempt id;
# every caller must retain the returned params through execution, exception closeout,
# Compact continuation, and final closeout.  Dropping the replacement recreates a stale-attempt
# closeout and leaves the root active forever.
# 函数用途: 为一次主代理模型回合选定任务工作区并绑定数据库权威 attempt，返回值必须贯穿整轮。
def _bind_main_agent_turn_params(
    agent,
    user_prompt: str,
    params: RunParams,
) -> RunParams:
    attached = attach_run_task_workspace_context(agent, params, user_prompt)
    return _bind_main_agent_authority(agent, attached)


# LLM: 顶层运行和所有自动 Compact 续接共用这一返回缝隙；只有最终不再续接时才能投影 standalone 终态。
# 函数用途: 执行一次完整请求，必要时续接 Compact，并在真正结束时收尾任务工作区。
def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
    if not current_params.root_user_prompt:
        current_params = replace(current_params, root_user_prompt=user_prompt)
    # Public ``my-agent run`` is single-shot, but its exact user input must already exist in
    # ConversationStore before remember or Curator can claim message evidence.
    current_params = bind_cli_run_conversation(agent, current_params, user_prompt)
    current_params = _bind_main_agent_turn_params(agent, user_prompt, current_params)
    result = _run_once_with_params(agent, user_prompt, current_params)
    while True:
        decision = compact_auto_continuation_decision(
            result,
            depth=current_params.compact_auto_continue_depth,
        )
        if decision.should_continue:
            released_input_ids = release_active_turn_inputs_for_compact(
                agent,
                current_params,
            )
            next_params = _compact_auto_continue_params(
                current_params,
                decision.injection,
                result,
                released_active_turn_input_ids=released_input_ids,
            )
            next_params = _bind_main_agent_turn_params(
                agent,
                decision.user_prompt,
                next_params,
            )
            continued = _run_once_with_params(agent, decision.user_prompt, next_params)
            result = mark_compact_auto_continued(
                continued, result, depth=next_params.compact_auto_continue_depth
            )
            current_params = next_params
            continue
        # 会话运行时 turn boundary: task_progress is advisory model state, not a
        # host-owned continuation trigger. A plain model final closes this turn;
        # only Compact continuation or a typed lifecycle event opens another slice.
        finish_run_task_workspace_if_needed(agent, current_params, result)
        _settle_main_agent_run(agent, current_params, result)
        persist_cli_run_assistant(agent, current_params, result)
        return result


# LLM: 阶段诊断日志只在 MY_AGENT_STAGE_DEBUG=1 时输出，用于定位 run 内部耗时构成；
# 默认零开销，不参与任何业务判定。
# 函数用途: 输出一次 agent.run 的细粒度阶段时间戳（按 request/run id 关联）。
def _log_run_stage(
    stage: str,
    params: object,
    *,
    started_mono: float | None = None,
) -> None:
    # 诊断行走 stderr（gateway 的 nohup 2>&1 已并入日志文件）；logging 在 gateway
    # 进程无 handler 时 INFO 会被 lastResort 吞掉，不能用 logger。
    if os.environ.get("MY_AGENT_STAGE_DEBUG") != "1":
        return
    elapsed = ""
    if started_mono is not None:
        elapsed = f" elapsed_ms={round((time.monotonic() - started_mono) * 1000, 1)}"
    print(
        "gateway run stage request_id=%s run_id=%s stage=%s%s"
        % (
            getattr(params, "request_id", "") or "",
            getattr(params, "run_id", "") or "",
            stage,
            elapsed,
        ),
        file=sys.stderr,
        flush=True,
    )


def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    _run_stage_started = time.monotonic()
    _log_run_stage("run_started", params)
    # ``_run_with_params`` has already replaced any Gateway/CLI transport attempt
    # with the exact RuntimeDB attempt.  Keep this function execution-only so the
    # caller retains the same params object for final settlement.
    root_user_prompt = params.root_user_prompt or user_prompt
    try:
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
            _log_run_stage("context_ready", params, started_mono=_run_stage_started)
            loop_result = _execute_runtime_loop(
                agent,
                _runtime_loop_params(user_prompt, prepared, params),
            )
            _log_run_stage("loop_done", params, started_mono=_run_stage_started)
            ctx = agent._build_finalize_context(
                _finalize_params(root_user_prompt, prepared, loop_result, params)
            )
            result = agent._get_services().finalization.finalize(ctx)
            _log_run_stage("finalize_done", params, started_mono=_run_stage_started)
            return result
    except BaseException as exc:
        # 会话运行时 在 task spawn 边界统一调用 on_task_finished；这里等价地覆盖
        # 已绑定 attempt 后的所有入口，包括 skill/tool snapshot、provider
        # capability probe、上下文准备、模型循环和 finalization。任何一步抛错
        # 都不能让 TUI 已空闲而权威账本仍保留 running。
        update_runtime_fact_terminal_if_enabled(agent, params, exc)
        _settle_main_agent_run_exception(agent, params, exc)
        raise


def _compact_auto_continue_params(
    params: RunParams,
    injection: str,
    source_result,
    *,
    released_active_turn_input_ids: tuple[str, ...] = (),
) -> RunParams:
    from ..conversation.active_turn_input import (
        exclude_active_turn_user_input_ids,
        merge_active_turn_user_inputs,
    )

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
        carried_active_turn_user_inputs=exclude_active_turn_user_input_ids(
            merge_active_turn_user_inputs(
                params.carried_active_turn_user_inputs,
                getattr(source_result, "active_turn_user_inputs", None),
            ),
            released_active_turn_input_ids,
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
