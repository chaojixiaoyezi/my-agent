
from __future__ import annotations

"""implements status, timeline, run, memory, local-search, local-doctor, and local-rebuild CLI commands.

给人看的解释：
这个文件只放和'本地状态/记忆/LocalStore'相关的命令。
它会调用 local_doctor 里的诊断规则，也会调用 gateway/subagent 的公开 API 取状态。
"""

import json
import time

from ..agent.agent_core.cli_run_conversation import (
    CliRunConversationPersistenceError,
)
from ..agent.agent_core.native_tool_protocol import ToolProtocolSelectionError
from ..agent.backends import ProviderRecoverableError
from ..agent.gateway_parts import (
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    recover_gateway_processing_requests,
)
from ..agent.gateway_parts.io import read_json_file, read_json_file_report
from ..agent.subagents.models import SubAgentBoardOptions
from .common import format_local_time, make_agent, resume_context_override
from .delivery_contracts import delivery_contract_from_file
from .local_doctor import build_status_suggestions
from .local_repair_commands import (
    build_local_doctor_report,
    cmd_local_doctor,
    cmd_local_rebuild,
    rebuild_local_store,
)
from .local_status_payload import (
    GatewayStatusRequest,
    StatusPayloadContext,
    build_status_payload,
    resolve_gateway_status,
)
from .local_status_view import StatusPrintContext, print_status_human
from .models import LocalSearchOptions, TimelineOptions
from .run_output import (
    make_run_chunk_writer,
    print_run_result,
    provider_recoverable_cli_report,
    run_exit_code,
)
from .thinking_spinner import ThinkingSpinner


def cmd_status(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_status_limit")
    paths = gateway_paths(agent)
    local_stats = agent.local_store.stats()
    board = agent.subagents.board.build_board(
        options=SubAgentBoardOptions(
            recent_limit=limit,
            include_child_status_counts=False,
        )
    )
    timeline = agent.local_store.timeline(limit=limit)
    pid, alive = gateway_running(paths)
    gateway_status, heartbeat_age, state_load_error, heartbeat_load_error = _gateway_status_from_files(agent, paths, alive)

    active_work_summary = _detect_active_work_summary(agent)
    request_counts = gateway_request_counts(paths, include_archives=False)
    archive_request_counts = gateway_request_counts(paths, include_archives=True)
    payload_ctx = StatusPayloadContext(
        agent,
        paths,
        local_stats,
        board,
        timeline,
        pid,
        alive,
        gateway_status,
        heartbeat_age,
        active_work_summary,
        request_counts,
        archive_request_counts,
        state_load_error,
        heartbeat_load_error,
    )
    payload = build_status_payload(payload_ctx)
    payload["suggestions"] = build_status_suggestions(agent, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print_ctx = StatusPrintContext(
        agent, paths, local_stats, board, timeline, gateway_status, pid, alive, heartbeat_age, active_work_summary,
        request_counts, archive_request_counts, payload["suggestions"], state_load_error, heartbeat_load_error,
    )
    print_status_human(print_ctx)
    return 0


def _gateway_status_from_files(agent, paths, alive: bool) -> tuple[str, float, dict | None, dict | None]:
    gateway_state_report = read_json_file_report(paths.state, context="cli.status.gateway_state.read")
    heartbeat_report = read_json_file_report(paths.heartbeat, context="cli.status.gateway_heartbeat.read")
    gateway_status, heartbeat_age = resolve_gateway_status(
        GatewayStatusRequest(
            alive=alive,
            gateway_state=gateway_state_report.payload,
            heartbeat=heartbeat_report.payload,
            stale_seconds=agent.config.gateway_stale_seconds,
            now=time.time(),
        )
    )
    return gateway_status, heartbeat_age, gateway_state_report.load_error, heartbeat_report.load_error


def _detect_active_work_summary(agent):
    if not agent.config.auto_detect_work_on_startup:
        return None
    from ..agent.startup_recovery import detect_active_work

    return detect_active_work(agent)


def cmd_timeline(args) -> int:

    agent = make_agent(args)
    _apply_default_arg_limit(args, agent, "cli_timeline_limit")
    options = _timeline_options(args)
    items = agent.local_store.timeline(
        limit=options.limit,
        source_type=options.source_type,
        event_type=options.event_type,
    )
    if options.json:
        print(json.dumps([item.__dict__ for item in items], ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT TIMELINE")
    if options.source_type:
        print(f"source_type={options.source_type}")
    if options.event_type:
        print(f"event_type={options.event_type}")
    if not items:
        print("暂无事件。")
        return 0
    for item in items:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        title = item.title or "-"
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {title}")
        if options.details:
            print("  payload=" + json.dumps(item.payload, ensure_ascii=False, sort_keys=True))
    return 0


# LLM: Public one-shot runs must render typed provider and transcript failures without traceback;
# the command may not continue after an authoritative user-message persistence failure.
# 函数用途: 执行一次 CLI Agent 请求，显示最终结果，并把模型或会话落账故障转成稳定中文退出信息。
def cmd_run(args) -> int:

    agent = make_agent(args)
    spinner = ThinkingSpinner()
    spinner.start()
    stream_state = {"seen": False, "text": ""}
    on_chunk = make_run_chunk_writer(spinner, stream_state)

    # 2026-08-14 根因3（CLI 自动续跑）: 首轮收口后可续跑族(共享 gate
    # should_continue_task)在进程内自动续跑——同一 task/run/thread 链路,
    # 直到完成/预算耗尽/不可续跑族。base_params 提供 request/run/task 根
    # ID(由 run_params_with_request_id 生成), resume_loop 在其中贯穿契约。
    from ..agent.agent_core.runtime.loop_models import RunParams
    from ..agent.agent_core.runtime.run_params import run_params_with_request_id
    from .resume_loop import run_with_resume

    # 首先生成 request/run/task 根 ID(bind 前必须有非空 request_id),
    # resume_loop 在其中贯穿续跑契约。
    base_params = run_params_with_request_id(
        RunParams(
            inject=args.inject or [],
            prompt_files=args.prompt_file or [],
            source="cli_run",
            task_attributes={},
            recovery_next_actions=_default_run_recovery_next_actions(),
            resume_context=resume_context_override(args),
        )
    )
    try:
        outcome = run_with_resume(
            agent,
            initial_prompt=args.prompt,
            base_params=base_params,
            save=bool(args.save),
            delivery_contract=delivery_contract_from_file(
                getattr(args, "delivery_contract_file", "")
            ),
            on_chunk=on_chunk,
        )
        result = outcome.final_result
    except ProviderRecoverableError as exc:
        print(provider_recoverable_cli_report(agent, exc))
        return 2
    except ToolProtocolSelectionError as exc:
        # 2026-08-17 MiniMax 端点实锤: probe 重试后仍失败=端点持续不可用/
        # 模型确实不支持 native——诚实报告退出（不无限退避、不假装成功）。
        print(f"工具协议能力不可用（native probe 重试后仍失败）：{exc}")
        return 2
    except CliRunConversationPersistenceError as exc:
        print(
            "[cli_run_conversation_persistence]\n"
            f"{exc}\n"
            "本次模型和工具均未执行；请修复 ConversationStore 后使用同一请求重新运行。"
        )
        return 2
    finally:
        spinner.stop()
    print_run_result(result, show_prompt=args.show_prompt, streamed_text=str(stream_state["text"]))
    return run_exit_code(result)


def _default_run_recovery_next_actions() -> list[str]:
    return [
        "继续当前用户请求的未完成部分；优先推进下一步工作，只有缺事实、引用损坏或需要核验时才读取恢复记录。",
    ]


def cmd_remember(args) -> int:

    agent = make_agent(args)
    rec = agent.remember(args.content, kind=args.kind)
    print(json.dumps(rec.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_list(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_memory_list_limit")
    records = agent.memory.all()[-limit:]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_memory_search(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_memory_search_limit")
    for rec in agent.recall(args.query, limit):
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_local_store_status(args) -> int:

    agent = make_agent(args)
    print(json.dumps(agent.local_store.stats(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_local_search(args) -> int:

    agent = make_agent(args)
    _apply_default_arg_limit(args, agent, "cli_local_search_limit")
    if getattr(args, "preview_chars", None) is None:
        args.preview_chars = int(getattr(agent.config, "cli_local_search_preview_chars", 500) or 0)
    options = _local_search_options(args)
    hits = agent.local_store.search(
        options.query,
        limit=options.limit,
        source_type=options.source_type,
        visibility=options.visibility,
    )
    for hit in hits:
        payload = hit.__dict__.copy()
        if options.preview_chars >= 0:
            payload["content"] = payload["content"][: options.preview_chars]
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_local_index_memory(args) -> int:

    agent = make_agent(args)
    count = agent.memory.index_all()
    print(
        json.dumps(
            {
                "indexed": count,
                "stats": agent.local_store.stats(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _timeline_options(args) -> TimelineOptions:
    return TimelineOptions(
        limit=int(args.limit or 0),
        source_type=args.source_type,
        event_type=args.event_type,
        json=bool(args.json),
        details=bool(args.details),
    )


def _config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)


def _apply_default_arg_limit(args, agent, config_name: str) -> None:
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, config_name, 0) or 0)


def _local_search_options(args) -> LocalSearchOptions:
    return LocalSearchOptions(
        query=args.query,
        limit=int(args.limit or 0),
        source_type=args.source_type,
        visibility=args.visibility,
        preview_chars=int(args.preview_chars),
    )
