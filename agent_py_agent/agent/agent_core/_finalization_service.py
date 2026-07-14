

from __future__ import annotations

import json
import logging
import time as time_module
from dataclasses import dataclass, replace
from pathlib import Path

from ..backends import ModelResponse
from ..conversation.authority import conversation_transcript_is_authoritative
from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ..user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from ._runtime_params import (
    ArchiveRunParams,
    EstimateTokenParams,
    FinalizeContext,
    ToolLoopExecuteParams,
)
from .delivery_closeout.artifacts import _required_artifacts
from .delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from .delivery_closeout.delivery_assurance import apply_delivery_assurance
from .delivery_closeout.uncontracted import _current_run_task_output_artifacts
from .delivery_closeout.user_summary import model_response_user_summary
from .delivery_completion_soft_hint import target_coverage_blocks_delivery_auto_closeout
from .finalization_compact_auto import compact_auto_cycle_fields
from .model.usage import input_token_usage, output_token_usage
from .models import AgentRunResult
from .run_task_workspace_writer import (
    current_run_task_workspace_root,
    write_run_task_workspace_if_needed,
)
from .runtime.owner_roots import runtime_archive_roots


@dataclass(frozen=True)
class BuildAgentRunResultParams:
    ctx: FinalizeContext
    archive_result: object
    token_ledger: dict[str, int]
    run_request_id: str


class FinalizationService:

    def __init__(self, agent):
        self._agent = agent

    def finalize(self, ctx: FinalizeContext):
        assert ctx.final_response is not None
        ctx = self._with_final_delivery_closeout_if_ready(ctx)
        # 交付保障自检(A1):必须在归档/落盘之前——归档、fact source、最终结果都读
        # final_response.text,空响应/散落交付要在这里被确定性兜住,绝不空手送用户。
        ctx = apply_delivery_assurance(self._agent, ctx)
        if _delivery_complete(ctx.final_response):
            # 只有收口代码生成的结构化完成标记才关闭会话任务；不根据“做完了”
            # 之类自然语言猜测，普通聊天也不会误关历史工作。
            from ..conversation.task_promotion import complete_current_conversation_task

            complete_current_conversation_task(
                self._agent,
                ctx.task_attributes,
                source=ctx.source,
                current_task_id=ctx.task_id,
            )
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
            task_attributes=ctx.task_attributes,
        )
        archive_result = self._archive_run_if_needed(archive_params)
        self._write_runtime_fact_source_if_needed(ctx, run_request_id)
        self._update_main_context_bundle_artifacts(ctx, run_request_id)
        token_ledger = self._estimate_token_usage(_estimate_token_params(ctx, run_request_id))

        return self._build_agent_run_result(
            BuildAgentRunResultParams(ctx, archive_result, token_ledger, run_request_id)
        )

    def _with_final_delivery_closeout_if_ready(self, ctx: FinalizeContext) -> FinalizeContext:
        return _finalize_with_delivery_closeout_if_ready(self._agent, ctx)

    def _write_runtime_fact_source_if_needed(self, ctx: FinalizeContext, run_request_id: str) -> str:
        if not ctx.do_save:
            return ""
        written = ""
        for root in runtime_archive_roots(self._agent):
            written = write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=run_request_id,
                    user_prompt=ctx.user_prompt,
                    response_text=ctx.final_response.text,
                    backend=ctx.final_response.backend,
                    status="ok",
                    next_actions=ctx.recovery_next_actions or [],
                    archive_tool_calls=ctx.archive_tool_calls or [],
                    runtime_injections=tuple(str(item) for item in ctx.runtime_injections or []),
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    source=ctx.source,
                    phase="final",
                    tool_rounds=ctx.tool_rounds,
                    executed_tools=list(ctx.executed_tools or []),
                    latest_archive_refs=_latest_archive_refs(ctx.archive_tool_calls or []),
                    artifact_refs=_artifact_refs(ctx.archive_tool_calls or []),
                    delivery_contract=ctx.delivery_contract,
                )
            )
        return written

    def _update_main_context_bundle_artifacts(self, ctx: FinalizeContext, run_request_id: str) -> None:
        if not ctx.do_save or not ctx.main_context_bundle_path:
            return
        update_main_context_bundle_artifacts(
            MainContextBundleArtifactUpdateRequest(
                context_bundle_path=ctx.main_context_bundle_path,
                workspace_root=self._agent.root,
                request_id=ctx.request_id or run_request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            )
        )

    def _archive_run_if_needed(self, params: ArchiveRunParams):
        if not params.do_save:
            return None
        write_run_task_workspace_if_needed(self._agent, params)
        if not conversation_transcript_is_authoritative(params.task_attributes):
            self._agent.memory.add("user", params.user_prompt)
            self._agent.memory.add(
                "agent", params.final_response.text, tags=[params.final_response.backend]
            )
        result = None
        for root in runtime_archive_roots(self._agent):
            result = archive_run_turn(
                ArchiveRunTurnParams(
                    root=root,
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
                        preview_limits=_memory_archive_preview_limits(self._agent.config),
                        summary_chars=int(getattr(self._agent.config, "memory_archive_summary_chars", 96) or 96),
                    ),
                ),
            )
        return result

    def _estimate_token_usage(self, params: EstimateTokenParams):
        input_tokens = input_token_usage(params.final_response)
        prompt_tokens = estimate_tokens(params.final_prompt) if params.final_prompt else 0
        if input_tokens is None:
            if params.final_prompt:
                input_tokens = prompt_tokens
            else:
                input_tokens = (
                    estimate_tokens(params.user_prompt)
                    + estimate_tokens(params.runtime_injections)
                    + estimate_tokens([getattr(memory, "content", "") for memory in params.memories])
                )
        output_tokens = output_token_usage(params.final_response)
        if output_tokens is None:
            output_tokens = estimate_tokens(params.final_response.text)
        tool_tokens = estimate_tokens(params.archive_tool_calls)
        ledger = {}
        for root in runtime_archive_roots(self._agent):
            ledger = append_session_token_usage(
                root,
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
            "active": _active_context_tokens(input_tokens, output_tokens, prompt_tokens),
        }

    def _build_agent_run_result(self, params: BuildAgentRunResultParams):
        ctx = params.ctx
        routed_context = ctx.routed_context
        return AgentRunResult(
            prompt=ctx.final_prompt,
            response=ctx.final_response.text,
            backend=ctx.final_response.backend,
            used_memories=len(ctx.memories),
            tool_rounds=ctx.tool_rounds,
            executed_tools=ctx.executed_tools,
            archive_tool_calls=ctx.archive_tool_calls,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=params.archive_result.event_count if params.archive_result else 0,
            archive_token_estimate=params.archive_result.token_estimate if params.archive_result else 0,
            prompt_token_estimate=estimate_tokens(ctx.final_prompt),
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections)
            if ctx.runtime_injections
            else 0,
            **_snapshot_result_fields(),
            **_resume_context_fields(ctx),
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=params.token_ledger["turn"],
            cumulative_token_estimate=params.token_ledger["cumulative"],
            main_context_bundle_path=ctx.main_context_bundle_path,
            main_context_bundle_markdown_path=ctx.main_context_bundle_markdown_path,
            runtime_status=str(getattr(ctx.final_response, "runtime_status", "ok") or "ok"),
            runtime_reason=str(getattr(ctx.final_response, "runtime_reason", "") or ""),
            **compact_auto_cycle_fields(self._agent, ctx, params.token_ledger, request_id=params.run_request_id),
        )


def _estimate_token_params(ctx: FinalizeContext, run_request_id: str) -> EstimateTokenParams:
    turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
    return EstimateTokenParams(
        user_prompt=ctx.user_prompt,
        runtime_injections=ctx.runtime_injections,
        memories=ctx.memories,
        final_response=ctx.final_response,
        archive_tool_calls=ctx.archive_tool_calls,
        run_request_id=run_request_id,
        turn_id=turn_id,
        final_prompt=ctx.final_prompt,
    )


def _active_context_tokens(input_tokens: int, output_tokens: int, prompt_tokens: int) -> int:
    return max(int(input_tokens), int(prompt_tokens)) + int(output_tokens)


def _snapshot_result_fields() -> dict:
    return {
        "recovery_snapshot_id": "",
        "recovery_snapshot_path": "",
        "recovery_snapshot_error": "",
        "recovery_snapshot_token_estimate": 0,
    }


def _latest_archive_refs(records: list[object]) -> list[str]:
    return [
        str(record.get("raw_archive_path") or "")
        for record in records[-20:]
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


def _artifact_refs(records: list[object]) -> list[str]:
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records[-20:]:
        if not isinstance(record, dict):
            continue
        refs.extend(str(record.get(key) or "") for key in keys if str(record.get(key) or ""))
    return refs


def _resume_context_fields(ctx: FinalizeContext) -> dict:
    resume = ctx.resume_context_result
    return {
        "memory_resume_context_injected": resume.injected if resume else False,
        "memory_resume_context_query": resume.query if resume else "",
        "memory_resume_context_matches": (
            resume.archive_match_count + resume.local_match_count + resume.task_fact_source_count
        )
        if resume
        else 0,
        "memory_resume_context_token_estimate": estimate_tokens(resume.context_block)
        if resume and resume.injected
        else 0,
        "memory_resume_context_error": resume.error if resume else "",
    }


def _memory_archive_preview_limits(config) -> dict[int, int]:
    return {
        0: int(getattr(config, "memory_archive_preview_level_0_chars", 2048) or 0),
        1: int(getattr(config, "memory_archive_preview_level_1_chars", 1024) or 0),
        2: int(getattr(config, "memory_archive_preview_level_2_chars", 512) or 0),
        3: int(getattr(config, "memory_archive_preview_level_3_chars", 160) or 0),
    }


def _tool_loop_params_from_finalize_context(ctx: FinalizeContext) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=ctx.user_prompt,
        memories=ctx.memories,
        runtime_injections=ctx.runtime_injections,
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=ctx.task_attributes,
        request_id=ctx.request_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        one_shot_tool_calls=set(),
        executed_tools=list(ctx.executed_tools or []),
        archive_tool_calls=list(ctx.archive_tool_calls or []),
        tool_rounds=ctx.tool_rounds,
        save=ctx.do_save,
        # 必须透传真实 scope(曾硬编码 "default"):子代理 task_local 收尾轮按主代理
        # 规则认领根任务 id,聚合门把【兄弟】当成自己的孩子 → SUBAGENTS_UNFINISHED
        # 打回(编队回归实锤:6/6 子代理收尾崩 structured_output_parse_error)。
        context_scope=ctx.context_scope,
        delivery_contract=ctx.delivery_contract,
        source=ctx.source,
    )


# LLM: 出口/收尾是否存在"可走 closeout 的产物候选"的唯一判定。contract 分支的
#   写入记录判定(_has_successful_delivery_record)只看内存 archive——R7b 实锤:
#   长任务 compact 后记录丢失、run_command 生成文件无路径 ref,真实交付对此判定
#   不可见;故记录为空时回落与 uncontracted 分支同源的交付区产物事实
#   (_current_run_task_output_artifacts 已带 task_output 目录扫描兜底)。
# LLM: P2 非阻塞出口门在【收尾层】的复用口(与 tool_loop.final_exit_contract 同源判据)。
#   曾漏:final_exit 已把"派完之后的查进度/聊天轮"原文放行,但收尾层
#   _with_final_delivery_closeout_if_ready 会重跑一遍 closeout,再次撞上 open 子代理→
#   SUBAGENTS_UNFINISHED 返工。两层必须用同一判据。open_summary 现算(收尾层没有现成的)。
# 函数用途: 收尾层判断"这轮是不是该为 open 子代理背锅返工的用户交互轮"——不是则原文放行。
def _open_children_user_interaction_passthrough(params: ToolLoopExecuteParams, agent: object) -> bool:
    from .delivery_closeout.subagent_aggregation import open_task_state_summary
    from .tool_loop.background_liveness import user_interaction_open_children_passthrough

    open_summary = open_task_state_summary(current_run_task_workspace_root(agent, params))
    return user_interaction_open_children_passthrough(params, open_summary)


# 函数用途: 回答"这轮有没有值得走验收门的交付迹象",记录丢了就直接看交付区。
def _has_final_closeout_candidate(params: ToolLoopExecuteParams, agent: object) -> bool:
    workspace_root = Path(getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", "."))
    workspace_root = workspace_root.expanduser().resolve(strict=False)
    # 产出意图兜底(codetask 实锤):主代理把交付物误写进 .agent_delivery/(系统账本
    # 目录),交付区因此为空、closeout 静默不触发、主代理自认完成退出。误写产物也算
    # closeout candidate——让 closeout 触发并提示落点,而非放任空交付静默成功。
    # §7-2 真机同类扩展:成品经 run_command/相对路径写到任务区外(owner home 根)时,
    # 交付区空+无误写 → candidate=False 同样静默完结(千行成品用户收不到);本 run 立过
    # task_progress 账(结构化信号)也算 candidate,让空交付门接管打回。
    from .delivery_closeout.task_progress_gate import task_progress_ledger_present

    has_output = bool(_current_run_task_output_artifacts(params, workspace_root=workspace_root))
    candidate = (
        has_output
        or _misplaced_products_in_closeout_dir(workspace_root)
        or task_progress_ledger_present(agent, params)
    )
    if _delivery_contract_present(params):
        if target_coverage_blocks_delivery_auto_closeout(agent, params):
            return False
        if not _required_delivery_artifacts_present(params, agent):
            return False
        if _has_successful_delivery_record(params):
            return True
        return candidate
    return candidate


_CLOSEOUT_SYSTEM_SUFFIXES = (".json", ".jsonl")


def _is_misplaced_product_file(item: Path) -> bool:
    return item.is_file() and not item.name.endswith(_CLOSEOUT_SYSTEM_SUFFIXES)


def _misplaced_products_in_closeout_dir(workspace_root: Path) -> bool:
    """.agent_delivery/ 里若有非系统账本文件(系统账本都是 .json/.jsonl),即主代理
    误写的交付物——视为 closeout candidate,触发交付验收以提示落点纠偏。"""
    closeout_dir = workspace_root / ".agent_delivery"
    if not closeout_dir.is_dir():
        return False
    try:
        return any(_is_misplaced_product_file(item) for item in closeout_dir.rglob("*"))
    except OSError:
        return False


def _failed_final_closeout_response(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    backend: str,
) -> ModelResponse | None:
    report = _latest_closeout_report(agent, params)
    load_error = report.get("_load_error") if isinstance(report, dict) else None
    if load_error:
        # closeout 状态未知(文件损坏/不可读):绝不当"无需收口"放行完成。
        # 退一步出"返工/无法确认收口"的非终态响应,把读取错误透传给模型与用户。
        return _closeout_status_unknown_response(load_error, backend=backend)
    if not report or report.get("ok") is not False:
        return None
    payload = {
        "ok": False,
        "report_ref": str(report.get("report_ref") or ".agent_delivery/closeout.json"),
        "delivery_mode": str(report.get("delivery_mode") or ""),
        "failed_artifacts": _failed_final_artifacts(report),
        "failed_gates": {
            key: value
            for key, value in {
                "target_coverage_projection_gate": report.get("target_coverage_projection_gate"),
                "task_progress_closeout_gate": report.get("task_progress_closeout_gate"),
                "subagent_aggregation_gate": report.get("subagent_aggregation_gate"),
            }.items()
            if isinstance(value, dict) and value.get("allowed") is False
        },
        "contract_recovery": report.get("contract_recovery", {}),
        # 余留合同(任务完成力底座 P1-2):非终态退出必带结构化恢复入口,
        # 用户和下一轮 agent 都能据此接力,不靠口头描述。
        "resume": _unfinished_run_resume_block(agent, params, report),
    }
    return ModelResponse(
        text=(
            "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n[/MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n"
            "交付验收未通过。本轮不能声明任务完成；请按 failed_artifacts、failed_gates 或 contract_recovery 修复后重新提交验收。"
            "任务处于可恢复状态：resume 字段给出任务根、进度账本与恢复方式。"
        ),
        backend=backend,
    )


# 函数用途: closeout.json 损坏/不可读(状态未知)时,产出非终态"无法确认收口"响应,
#   绝不让损坏的收口账本被误判成"无需收口=已完成"。
def _closeout_status_unknown_response(
    load_error: object,
    *,
    backend: str,
) -> ModelResponse:
    payload = {
        "ok": False,
        "report_ref": ".agent_delivery/closeout.json",
        "closeout_status": "unknown",
        "load_error": load_error,
    }
    return ModelResponse(
        text=(
            "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n[/MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n"
            "无法读取交付收口账本(.agent_delivery/closeout.json 损坏或不可读)，收口状态未知。"
            "本轮不能声明任务完成；请修复/重建该账本后重新提交验收。"
        ),
        backend=backend,
    )


# LLM: P1-2 余留合同的 resume 块:全部取自结构化事实(closeout 报告/任务根),
#   不生成自然语言计划。how_to_continue 是固定的结构化入口说明(同一进程再次
#   run 会经 startup_recovery/任务账本接力;dispatch 命令可直接续派子代理)。
# 函数用途: 任务没做完时,把"从哪继续"打包成机器可读的恢复入口。
def _unfinished_run_resume_block(agent: object, params: ToolLoopExecuteParams, report: dict) -> dict:
    root = current_run_task_workspace_root(agent, params)
    gate = report.get("task_progress_closeout_gate")
    evidence = gate.get("evidence") if isinstance(gate, dict) else None
    progress_ref = ""
    open_count = -1
    if isinstance(evidence, dict):
        progress_ref = str(evidence.get("progress_ref") or "")
        try:
            open_count = int(evidence.get("open_count"))
        except (TypeError, ValueError):
            open_count = -1
    return {
        "task_root": str(root or ""),
        "progress_ref": progress_ref,
        "open_count": open_count,
        "how_to_continue": [
            "再次对同一任务发起 run（启动检测会带出未完成任务）",
            "my-agent subagents-dispatch --apply --start-runners 续派未完成子代理",
            "my-agent task-list / task-show <id> 查看任务状态",
        ],
    }


def _latest_closeout_report(agent: object, params: ToolLoopExecuteParams) -> dict[str, object]:
    root = current_run_task_workspace_root(agent, params) or _candidate_workspace_root(agent)
    path = root / ".agent_delivery" / "closeout.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # closeout.json 不存在 = 合法的"无 closeout",静默返回 {}。
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # 损坏/不可读 ≠ 无 closeout:状态未知,绝不能让调用方把它当"无需收口"
        # 而误判完成。返回带结构化错误的标记,调用方据 _load_error 走"状态未知"分支。
        from ..runtime_errors import runtime_error_report

        logging.getLogger(__name__).warning(
            "closeout.json load failed (status unknown): path=%s", path, exc_info=True
        )
        return {
            "_load_error": runtime_error_report(
                exc, context="finalization._latest_closeout_report"
            )
        }
    if not isinstance(payload, dict):
        return {
            "_load_error": {
                "category": "data_corruption",
                "context": "finalization._latest_closeout_report",
                "message": f"closeout.json 顶层非 JSON 对象 (type={type(payload).__name__})",
            }
        }
    return payload


def _failed_final_artifacts(report: dict[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return result
    for item in artifacts:
        if not isinstance(item, dict) or item.get("ok") is True:
            continue
        result.append(
            {
                "artifact_id": str(item.get("artifact_id") or ""),
                "path": str(item.get("path") or ""),
                "findings": item.get("acceptance_report", {}).get("findings", [])
                if isinstance(item.get("acceptance_report"), dict)
                else [],
            }
        )
    return result


def _delivery_contract_present(params: ToolLoopExecuteParams) -> bool:
    if isinstance(params.delivery_contract, dict) and params.delivery_contract:
        return True
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    return isinstance(attrs.get("delivery_contract"), dict) and bool(attrs.get("delivery_contract"))


def _required_delivery_artifacts_present(params: ToolLoopExecuteParams, agent: object) -> bool:
    artifacts = _required_artifacts(_delivery_contract(params))
    if not artifacts:
        return True
    paths = [_contract_artifact_path(item, _candidate_workspace_root(agent)) for item in artifacts]
    return bool(paths) and all(path is not None and path.exists() for path in paths)


def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, object]:
    if isinstance(params.delivery_contract, dict):
        return dict(params.delivery_contract)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


def _candidate_workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve(strict=False)


def _contract_artifact_path(item: dict[str, object], workspace_root: Path) -> Path | None:
    raw_path = str(item.get("preferred_path") or item.get("path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    return path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False)


def _has_successful_delivery_record(params: ToolLoopExecuteParams) -> bool:
    for record in list(params.archive_tool_calls or []):
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        if str(record.get("tool") or "").strip() in {"write_file", "apply_patch", "run_command", "controlled_exec"}:
            return True
    return False


def _delivery_complete(final_response: object) -> bool:
    return "[MAIN_AGENT_DELIVERY_COMPLETE]" in str(getattr(final_response, "text", "") or "")


def _finalize_with_delivery_closeout_if_ready(agent, ctx: FinalizeContext) -> FinalizeContext:
    if _delivery_complete(ctx.final_response):
        return ctx
    # 子代理 runner(task_local)已交出结果块时,收尾协议归 runner 自己的
    # finalize 链(_finalize_subagent_run:解析→修复→产出兜底)。这里再跑一遍
    # closeout 会用 rework/失败响应【整体替换】final_response,把模型真实输出
    # (含 findings/evidence)清掉,制造 structured_output_parse_error 假崩
    # (编队回归实锤:runner_response.md 只剩 [MAIN_AGENT_DELIVERY_REWORK_REQUIRED])。
    if _subagent_result_block_present(ctx):
        return ctx
    params = _tool_loop_params_from_finalize_context(ctx)
    if not _has_final_closeout_candidate(params, agent):
        return ctx
    # P2 非阻塞:用户交互轮(gateway/chat 的聊天/查进度)+ 上一轮派的 open 子代理还在 → 收尾层
    #   同样不为"子代理未收口"返工(与 tool loop 的 final_exit 出口同源判据)。否则 final_exit
    #   已放行的查进度/聊天轮会在这里被重跑 closeout 打回 SUBAGENTS_UNFINISHED。叫回轮/派活轮
    #   /非 wake 来源不命中,照常走门交付子代理成果。
    if _open_children_user_interaction_passthrough(params, agent):
        return ctx
    backend = str(getattr(ctx.final_response, "backend", "") or "")
    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(
            agent=agent,
            params=params,
            backend=backend,
            user_summary=model_response_user_summary(ctx.final_response),
        )
    )
    if response is None:
        if failed_response := _failed_final_closeout_response(agent, params, backend=backend):
            return replace(ctx, final_response=failed_response)
        return ctx
    return replace(ctx, final_response=response)


# 函数用途: 判断"这是子代理 runner 轮且模型已交出 [SUBAGENT_RESULT] 结果块"——
#   是则收尾协议归 runner finalize 链,本层不得重跑 closeout 替换响应。
#   found 即认(缺结束标记/JSON 坏也算):坏块该进 runner 的修复链拿模型真实输出
#   尾部去修,而不是被 rework 响应清掉后拿噪声去修。
def _subagent_result_block_present(ctx: FinalizeContext) -> bool:
    if str(ctx.context_scope or "").strip().lower() != "task_local":
        return False
    from ..subagents.parsing import parse_subagent_runner_output

    return parse_subagent_runner_output(str(getattr(ctx.final_response, "text", "") or "")).found
