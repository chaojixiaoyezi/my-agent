# LLM: 主代理真实 task/run/attempt 必须在模型前完成绑定并交给宿主持久保存；
# 正常结束、异常和 Compact 续接沿用同一权威，不从会话展示编号反推执行身份。
# 快照准入失败同样必须恢复线程临时字段，不能将失败任务的参数留给同线程下一次调用。
# 模块用途: 执行主代理的模型工具循环、上下文续接与精确执行轮收尾，不替子代理调度另建身份。
from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..concurrency.interrupt import is_interrupted
from ..conversation.authority import conversation_transcript_is_authoritative
from ..conversation.task_run_closeout import settle_terminal_task_run
from ..runtime_db.repository import AGENT_RUN_TERMINAL_STATUSES
from ..runtime_db.run_takeover import TAKEOVER_RUNTIME_SOURCE
from ._finalization_service import FinalizationService
from ._runtime_params import FinalizeContext
from .cli_run_conversation import (
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


# LLM: 先固定旧临时状态，再在同一 try/finally 中安装参数与快照；包括 yield 前的过期引用错误也必须恢复。
# 函数用途: 进入一轮线程隔离的 prompt/任务/能力快照范围，正常、失败和嵌套退出均恢复原状态。
@contextmanager
def current_prompt_scope(agent, user_prompt: str, params: RunParams | None = None):
    """Expose one thread-local run/tool scope without requiring a model turn."""
    had_current_prompt = hasattr(agent, "_current_user_prompt")
    previous_current_prompt = getattr(agent, "_current_user_prompt", "")
    had_current_run_params = hasattr(agent, "_current_run_params")
    previous_current_run_params = getattr(agent, "_current_run_params", None)
    had_current_skills = hasattr(agent, "_current_skill_snapshot")
    previous_current_skills = getattr(agent, "_current_skill_snapshot", None)
    snapshot = _PromptScopeSnapshot(
        had_current_prompt,
        previous_current_prompt,
        had_current_run_params,
        previous_current_run_params,
        had_current_skills,
        previous_current_skills,
    )
    try:
        agent._current_user_prompt = user_prompt
        if params is not None:
            agent._current_run_params = params
        agent._current_skill_snapshot = _turn_skill_snapshot(agent)
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


    # LLM: run 冻结模型和身份并登记端点占用；完整工作片结束后释放，后台记忆不得趁工具间隙抢同端点。
    # 函数用途: 绑定本轮模型/权限，再进入共享运行、压缩、工具和保存主链；前台之间不串行。
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
        from ..backends.request_scope import foreground_model_scope
        from ..settings.model_scope import selected_model_scope

        attrs = params.task_attributes or {}
        with provider_runtime_scope(self, params), selected_model_scope(
            self, inherited=params.context_scope == "task_local",
            thread_id=str(attrs.get("conversation_thread_id") or ""),
        ):
            with foreground_model_scope(self.backend):
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
#   Gateway 在模型/工具之前收到真实 DB 身份；request_id 保留消息身份，run/attempt 一起回填，
#   Compact 再入仍按消息编号发布宿主凭据。同步检查跨身份续跑与工具权威门测试。
# 函数用途: 登记并传回成对的执行身份，避免追问拿新消息编号执行旧任务；凭据保存失败不进入模型。
def _bind_main_agent_authority(agent, params: RunParams) -> RunParams:
    """MANAGED 下主代理 run 登记权威链（seq 255 单一闭合：root/main AgentRun）。

    主代理与子代理共用 agent.run：子代理 run 的权威链由 create_run
    （record_run_creation）+ prepare_runner_attempt（create_attempt 轮换）
    登记，runner 上下文非空 → 此处跳过（结构化信号，不重复轮换）。
    主代理 run 无登记 → record_run_creation 建 root Task/TaskRun/AgentRun/
    首 attempt；compact/账本续跑轮（同 run_id 已登记）→ create_attempt
    轮换 current pointer（G4 语义：旧 attempt 非终态操作转 UNKNOWN）。

    返回 replace 后的 params（run_id/attempt_id 同属 DB 权威执行链，
    request_id 保留调用方消息身份，工具循环与子代理使用同源权威）；
    LOCAL_UNMANAGED（无 repo）/无 run 上下文/子代理上下文 → 原样返回，
    投影 id 行为不变。
    """
    from ..runtime_context import current_subagent_run_id

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
                "task_id": task_id, "run_id": authority_run_id,
                "invocation_run_id": str(params.request_id or run_id),
                "agent_run_id": agent_run_id, "attempt_id": attempt_id,
            }) is not True:
                raise RuntimeError("主代理执行身份无法可靠保存，尚未进入模型或工具")
        except Exception as exc:
            # 这时模型尚未进入；沿用精确 attempt 收口，不能把未开始的轮次留成永远 running。
            _settle_main_agent_run_status(
                agent, run_id=authority_run_id, attempt_id=attempt_id,
                facts=RunCloseoutFacts(
                    runtime_status="cancelled" if isinstance(exc, InterruptedError) else "failed",
                    runtime_reason="runtime_authority_publish_failed",
                ),
            )
            raise
    return replace(params, run_id=authority_run_id, attempt_id=attempt_id)


# 审计账本终态别名：运行时语义 → agent_runs 终态串（只认结构化字段）。
_RUN_STATUS_ALIASES = {"ok": "done", "user_stop": "cancelled", "conversation_control": "cancelled"}


# LLM: 收口判定与 payload 证据共用的结构化事实包；把一组同源参数收成小数据类，避免逐参透传。
# 类用途: 承载一次执行轮结束时的运行事实（状态/原因/来源/工具轮数），只读结构化字段。
@dataclass(frozen=True)
class RunCloseoutFacts:
    runtime_status: str
    runtime_reason: str = ""
    runtime_source: str = ""
    tool_rounds: int = 0


# 等待用户族（3a 裁定，rco）：这类 runtime_status 是任务级可恢复语义，保留 run 非终态
# 等用户动作驱动续跑；只认结构化状态串，不解析文案。
_WAITING_USER_RUNTIME_STATUSES = frozenset({"needs_user_input", "approval_required"})


# LLM: 非终态收口的唯一分族点（3a 裁定：统一收口、不为单条路径打补丁）。等用户族保留；
#   其余非终态用共享技术续跑 gate（should_continue_task）分族——可续跑族保留非终态等续跑，
#   不可续跑族（blocked/协议违规/UNKNOWN 等）收口 failed，杜绝 attempt 已结束而 run 停在 created。
#   分族主键是 runtime_reason（TOOL_CALL_UNCLOSED / 截断 / 超窗三项再按 runtime_source/runtime_status
#   精确核对）；runtime_status 字面不单独决定分族——status=blocked 但 reason=TOOL_ROUND_LIMIT_REACHED
#   的轮次按 reason 保留非终态是正确的（初审 rco-f3）。
#   判据不可用时（导入或调用异常）不静默悬挂：记结构化 error 日志并按不可续跑收口 failed——
#   收口可逆（create_attempt 对终态放行、会重开），悬挂没有任何机制能救（初审 rco-f4）。
# 函数用途: 把非终态 runtime_status 映射成 run 收口终态；返回空串表示保留非终态。
def _nonterminal_run_closeout_status(runtime_status: str, runtime_reason: str, runtime_source: str) -> str:
    if runtime_status in _WAITING_USER_RUNTIME_STATUSES:
        return ""
    try:
        from ..turn_end import should_continue_task

        should, _ = should_continue_task(
            SimpleNamespace(
                runtime_status=runtime_status,
                runtime_reason=runtime_reason,
                runtime_source=runtime_source,
            )
        )
    except Exception:  # noqa: BLE001 分族判据故障必须可见且不悬挂（初审 rco-f4）
        logging.getLogger(__name__).error(
            "run 收口分族判据不可用，按不可续跑收口 failed：runtime_status=%s runtime_reason=%s runtime_source=%s",
            runtime_status, runtime_reason, runtime_source, exc_info=True,
        )
        return "failed"
    return "" if should else "failed"


# LLM: 收口按权威 attempt 定位 AgentRun，技术续跑共用 turn_end；临时 run_id 不得导致漏记或误关别人的轮次。
# 函数用途: 结束精确执行代次并释放执行权；旧 attempt 的 CAS 不能覆盖新 attempt。
def _settle_main_agent_run_status(
    agent,
    *,
    run_id: str,
    attempt_id: str,
    facts: RunCloseoutFacts,
) -> None:
    """把主代理 run 的真实终态落进权威审计账本（fail-silent）。

    修复②（run 级审计终态）：/ask 路径此前 agent_runs.status 恒 'created'、
    attempt 恒 'running'、无 run 级完成事件。这里在 run 真实结束时按
    结构化字段收口：runtime_status=='ok' → 'done'；cancelled 族
    （user_stop/conversation_control）→ 'cancelled'；failed 等直接落账。

    R1-03（收口闸，2026-10-04 rco 修订）：unfinished/blocked/needs_user_input/
    approval_required 等是任务级可恢复 runtime_status，不是 agent_runs.status
    合法终态；它们只关闭本次 AgentAttempt、释放工具权限。修订点（3a 裁定）：
    非终态统一分族（_nonterminal_run_closeout_status）——不可续跑族（blocked/
    协议违规/UNKNOWN 等，共享 gate should_continue_task 判定 False）的尝试
    结束时 run 收口 failed（把 CLI one-shot 兜底口径推广到所有路径，杜绝
    attempt 已结束而 run 永远停在 created）；可续跑族保留 run 非终态等续跑
    ——技术续跑族由 resume/Goal 驱动，等用户族（needs_user_input/
    approval_required）由用户动作驱动。原始 runtime_status/runtime_reason
    保留在 payload 证据，绝不写 DONE 撒谎。
    LOCAL_UNMANAGED（无 repo）/查无 run → noop。
    审计是附加保证，任何失败绝不反噬执行路径。
    """
    runtime_status = facts.runtime_status
    terminal = _RUN_STATUS_ALIASES.get(runtime_status, runtime_status or "")
    if not terminal or terminal in ("", "created", "running"):
        return
    if terminal not in AGENT_RUN_TERMINAL_STATUSES:
        # 统一分族（3a 裁定，rco）：不可续跑族收口 failed（CLI one-shot 兜底口径
        # 推广到所有路径）；可续跑族只关闭 attempt，run 留给续跑。
        terminal = _nonterminal_run_closeout_status(
            runtime_status, facts.runtime_reason, facts.runtime_source
        )
    if not run_id or not attempt_id:
        return
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return  # LOCAL_UNMANAGED：显式选择本地投影库，无权威账本
    try:
        attempt = repo.get_attempt(attempt_id)
        if attempt is None:
            return
        agent_run_id = str(attempt["agent_run_id"])
        payload = {
            "status": terminal or "attempt_done",
            "runtime_status": runtime_status,
            "runtime_reason": facts.runtime_reason,
            "runtime_source": facts.runtime_source,
            "tool_rounds": int(facts.tool_rounds or 0),
        }
        if terminal:
            repo.settle_agent_run(
                agent_run_id=agent_run_id,
                status=terminal,
                attempt_id=attempt_id,
                payload=payload,
            )
        else:
            repo.settle_agent_attempt(
                agent_run_id=agent_run_id,
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

    非终态统一分族收口（不可续跑族落 failed、可续跑族保留），杜绝 attempt
    悬挂——见 _settle_main_agent_run_status 注释。
    """
    if _task_local_continuation_keeps_attempt_open(agent, params, result):
        return
    runtime_status = str(getattr(result, "runtime_status", "") or "").strip()
    _settle_main_agent_run_status(
        agent,
        run_id=str(params.run_id or "").strip(),
        attempt_id=str(params.attempt_id or "").strip(),
        facts=RunCloseoutFacts(
            runtime_status=runtime_status,
            runtime_reason=str(getattr(result, "runtime_reason", "") or "").strip(),
            runtime_source=str(getattr(result, "runtime_source", "") or "").strip(),
            tool_rounds=int(getattr(result, "tool_rounds", 0) or 0),
        ),
    )
    _settle_terminal_conversation_task_run(agent, params)


# LLM: A TaskRun spans every foreground/background slice and descendant of one conversation task. This runs on root and
# child terminal edges so either side of their race can complete the same CAS. The decision itself (root terminal, link
# terminal or truly absent, tree terminal/quiescent) lives in conversation/task_run_closeout.settle_terminal_task_run, which
# the gateway /recover child branch also calls (gateway_parts may not import agent_core); keep both callers on that function.
# 函数用途: 在任一代理执行收口后核对持久会话任务状态，并幂等关闭整棵任务执行总账；没有会话任务的请求在代理树结束后同样关闭。
def _settle_terminal_conversation_task_run(agent: object, params: object) -> None:
    settle_terminal_task_run(
        getattr(getattr(agent, "subagents", None), "runtime_db", None),
        getattr(agent, "conversation_store", None),
        run_id=str(getattr(params, "run_id", "") or ""),
        task_id=str(getattr(params, "task_id", "") or ""),
    )


# LLM: 执行异常的运行账收口，只决定 cancelled/failed 与结构化原因；原因来源见 _exception_closeout_reason。
#   调用方在 except 里随后重新抛出原异常，所以这里必须 fail-silent，不能盖住原异常。改动同步 test_run_audit_terminal.py。
# 函数用途: 执行中途抛错或被中断时，把本轮写成 cancelled（中断）或 failed（其它异常），并顺带核对整棵任务能否关账。
def _settle_main_agent_run_exception(agent, params: RunParams, exc: BaseException) -> None:
    """异常路径收口：InterruptedError → 'cancelled'，其余 → 'failed'。"""
    status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
    reason, source = _exception_closeout_reason(agent, params, exc)
    _settle_main_agent_run_status(
        agent,
        run_id=str(params.run_id or "").strip(),
        attempt_id=str(params.attempt_id or "").strip(),
        facts=RunCloseoutFacts(
            runtime_status=status, runtime_reason=reason, runtime_source=source
        ),
    )
    _settle_terminal_conversation_task_run(agent, params)


# LLM: 子代理被 canonical 接替（TAKEN_OVER）后由 runner 心跳自停，中断收口要与静止来源的接替补账
#   （runtime_db/run_takeover）同一口径 taken_over / subagent_takeover；是否被接替只认 subagents.taken_over_successor
#   这一个判据（与 record_takeover 收口、网关 /recover 共用）。主代理、记录读不到、非中断异常都保留异常类名。
#   只读，不改 runtime_status、不改状态机；读失败回落类名，不能盖住原异常。改动同步 test_subagent_takeover_runtime_closeout.py。
# 函数用途: 给异常收口挑运行账结束事件的原因和来源，被接替而自停的子代理不再记成 InterruptedError、来源为空。
def _exception_closeout_reason(agent, params: RunParams, exc: BaseException) -> tuple[str, str]:
    successor_of = getattr(getattr(agent, "subagents", None), "taken_over_successor", None)
    if not isinstance(exc, InterruptedError) or not callable(successor_of):
        return type(exc).__name__, ""
    try:
        taken_over = bool(successor_of(str(params.run_id or "").strip()))
    except Exception:  # noqa: BLE001 审计标签读失败只回落异常类名，绝不盖住原异常
        taken_over = False
    return ("taken_over", TAKEOVER_RUNTIME_SOURCE) if taken_over else (type(exc).__name__, "")


# LLM: 先绑定 canonical run/新 attempt 再写归档，request 仍是消息身份；返回参数贯穿 Compact 与收尾。
# 与终态扫描共用换代锁，入锁复查本片中断和任务终态；恢复不能复活已收到中断的旧片。
# 归档失败只结算本次新 attempt；修改时核对复用 run、停止竞争和归档准备失败测试。
# 函数用途: 在同一锁内确定执行身份并准备运行目录，防止续做归档与收尾使用不同 run；失败不遗留新执行权。
def _bind_main_agent_turn_params(
    agent,
    user_prompt: str,
    params: RunParams,
) -> RunParams:
    store = getattr(agent, "conversation_store", None)
    guard = getattr(getattr(store, 'tasks', None), 'transition_guard', None)
    task_id = str(params.task_id or params.run_id or "")
    with guard(task_id) if task_id and callable(guard) else nullcontext():
        if is_interrupted():
            raise InterruptedError("当前执行片已被中断，未创建新执行轮")
        loader = getattr(getattr(store, "tasks", None), "load", None)
        link = loader(task_id) if task_id and callable(loader) else None
        if link is not None and getattr(link, "status", "") in {"interrupted", "cancelled"}:
            raise InterruptedError("当前任务已停止，未创建新执行轮")
        bound = _bind_main_agent_authority(agent, params)
        try:
            return attach_run_task_workspace_context(agent, bound, user_prompt)
        except Exception as exc:
            if bound.attempt_id and bound.attempt_id != params.attempt_id:
                _settle_main_agent_run_status(
                    agent, run_id=bound.run_id, attempt_id=bound.attempt_id,
                    facts=RunCloseoutFacts(
                        runtime_status="cancelled" if isinstance(exc, InterruptedError) else "failed",
                        runtime_reason="workspace_preparation_failed",
                    ),
                )
            raise


# LLM: 顶层运行和所有自动 Compact 续接共用这一返回缝隙；只有最终不再续接时才能投影 standalone 终态。
# 宿主实验授权钩子只在首个执行片绑定 run/attempt 之后、首次模型调用之前触发一次；Compact 续接不再授权。
# 函数用途: 执行一次完整请求，必要时续接 Compact，并在真正结束时收尾任务工作区。
def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
    if not current_params.root_user_prompt:
        current_params = replace(current_params, root_user_prompt=user_prompt)
    # Public ``my-agent run`` is single-shot, but its exact user input must already exist in
    # ConversationStore before remember or Curator can claim message evidence.
    current_params = bind_cli_run_conversation(agent, current_params, user_prompt)
    current_params = _bind_main_agent_turn_params(agent, user_prompt, current_params)
    _grant_host_decision_experiment(agent, current_params)
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


# LLM: 只调用宿主绑定回调自带的 grant_decision_experiment（当前仅 Gateway 请求写入器提供）；参数必须已是精确 run/attempt。
# 授权失败或回调异常都不能阻断业务回合；中断等 BaseException 照常传播。
# 函数用途: 在首次模型调用前让宿主按用户显式命令建立有界决策实验授权。
def _grant_host_decision_experiment(agent, params: RunParams) -> None:
    grant = getattr(params.conversation_task_binding_callback, "grant_decision_experiment", None)
    if not callable(grant):
        return
    try:
        grant(agent, params)
    except Exception:  # noqa: BLE001 实验授权只提示用户，不影响主任务执行
        logging.getLogger(__name__).warning("决策实验授权回调失败；本轮业务继续，不会发送实验请求。")


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


# LLM: 原执行片为已验证候选保留依赖到 finalization；所有异常仍结算真实 attempt/用量，清理不得热改 Agent 或猜补消耗。
# 函数用途: 执行模型工具循环及原收尾，typed overflow向宿主交回同次冻结IR，其它结束仍按实际消耗和终态处理。
def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    _run_stage_started = time.monotonic()
    _log_run_stage("run_started", params)
    # ``_run_with_params`` has already replaced any Gateway/CLI transport attempt
    # with the exact RuntimeDB attempt.  Keep this function execution-only so the
    # caller retains the same params object for final settlement.
    root_user_prompt = params.root_user_prompt or user_prompt
    from ..settings.model_scope import model_dependency_lifetime
    from .subagent.model_selection import canonical_subagent_model_scope

    try:
        with canonical_subagent_model_scope(agent, params.run_id, task_local=params.context_scope == "task_local"), model_dependency_lifetime(agent), current_prompt_scope(agent, user_prompt, params):
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
            result.native_compact_carry = loop_result.native_compact_carry
            _log_run_stage("finalize_done", params, started_mono=_run_stage_started)
            return result
    except BaseException as exc:
        try:
            FinalizationService(agent).settle_model_usage(params)
        except Exception:
            logging.getLogger(__name__).error("模型用量收口失败，保留原任务错误；请核对用量账", exc_info=False)
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


# LLM: 原工具账保留未知旧记录，但只有新出现的完整调用身份能证明本轮新增执行；累计计数不作为证明。
# 函数用途: 判断Compact续跑是否确有新工具事实，不把重复携带旧材料算进展。
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
        from ..conversation.compact_tool_identity import compact_tool_ref_key

        carried_keys = {compact_tool_ref_key(record) for record in list(params.carried_archive_tool_calls or [])}
        return any(
            compact_tool_ref_key(record) is not None
            and compact_tool_ref_key(record) not in carried_keys
            and _archive_record_is_tool_progress(record)
            for record in incoming_archive_calls
        )
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


# LLM: 精确工具身份包含run/attempt/模型turn/call；未知旧记录不按裸call_id去重，防止续跑先于覆盖解析丢来源。
# 函数用途: 合并完整活动工具账；保留不同执行回合的同名调用及身份未知的原记录。
def _merged_archive_tool_calls(
    existing: list[dict[str, object]] | None,
    incoming: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    from ..conversation.compact_tool_identity import compact_tool_ref_key

    merged: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for record in [*list(existing or []), *list(incoming or [])]:
        if not isinstance(record, dict):
            continue
        identity = compact_tool_ref_key(record)
        if identity is None:
            merged.append(record)
            continue
        key = (
            identity,
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
