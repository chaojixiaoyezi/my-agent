from __future__ import annotations

"""统一 Memory v2 管理命令的 Service 委托层。"""

# LLM: 本模块不得直接解析或写 candidates/daily/state/retention 文件；所有动作委托正式 Service。
# 模块用途: 把 argparse 参数变成 typed Service 调用，并输出不含秘密的结构化报告。

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agent.memory_api import (
    DailyMemoryStore,
    LessonRepository,
    MemoryMigrationService,
    MemoryRetentionService,
)
from .common import make_agent
from .memory_commands.memory_doctor_cmd import (
    _build_archive_doctor,
    _build_routing_doctor,
    _config_warnings,
    _memory_config_payload,
    _resolve_index_path,
)

_BLOCKED_STATUSES = frozenset({"blocked_missing_evidence", "blocked_conflict"})


# LLM: CLI handler 只选择查询参数，账本严格读取和过滤仍由 CandidateService 完成。
# 函数用途: 列出 owner 级唯一候选账本。
def cmd_memory_candidates_list(args: Any) -> int:
    return _run_admin_command(args, _candidate_list_payload)


# LLM: 审核必须走 CandidateService/PromotionService 的唯一状态图，不能写 decision 旁路文件。
# 函数用途: 执行批准、拒绝、重开、过期或替代决定。
def cmd_memory_candidates_review(args: Any) -> int:
    return _run_admin_command(args, _candidate_review_payload)


# LLM: 正式写入只能由 MemoryPromotionService 完成，CLI 不直接碰 long_term 或 Persona 文件。
# 函数用途: 晋升一条已经满足审核和证据要求的候选。
def cmd_memory_candidates_promote(args: Any) -> int:
    return _run_admin_command(args, _candidate_promote_payload)


# LLM: 状态读取来自 Curator 的 durable state，不使用 Gateway 进程内缓存冒充权威。
# 函数用途: 展示后台策展配置、游标、lease 和累计数量。
def cmd_memory_curator_status(args: Any) -> int:
    return _run_admin_command(args, _curator_status_payload)


# LLM: admin 触发先持久化 reason 再运行；崩溃后同一 state 可由维护主链恢复。
# 函数用途: 显式请求并尝试运行一个 Curator 批次。
def cmd_memory_curator_run(args: Any) -> int:
    return _run_admin_command(args, _curator_run_payload)


# LLM: 恢复只读校验+原子落回, 不登记 trigger; 失败返回非零码(ok=false)。
# 函数用途: 显式执行一次人工 Curator state 恢复。
def cmd_memory_curator_recover(args: Any) -> int:
    return _run_admin_command(args, _curator_recover_payload)


# LLM: retention plan 是严格只读调用，不能因为 CLI 展示而创建 trash 或 audit。
# 函数用途: 生成当前 owner 的 v2 保留期计划。
def cmd_memory_retention_plan(args: Any) -> int:
    return _run_admin_command(args, _retention_plan_payload)


# LLM: apply 仍由同一 Service 在锁内重新计划和重验证，CLI 不能执行计划里的裸路径删除。
# 函数用途: 显式应用当前 owner 的 v2 保留期策略。
def cmd_memory_retention_apply(args: Any) -> int:
    return _run_admin_command(args, _retention_apply_payload)


# LLM: doctor 只聚合各正式 Service 的只读健康报告，不自动修复或写 marker。
# 函数用途: 诊断 Memory v2 权威文件、后台状态、迁移和保留期。
def cmd_memory_v2_doctor(args: Any) -> int:
    return _run_admin_command(args, _doctor_payload)


# LLM: 迁移默认 plan；apply 标志只切换同一 MigrationService 的显式执行方法。
# 函数用途: 检测或应用一次性 Memory v2 安全迁移。
def cmd_memory_migrate(args: Any) -> int:
    return _run_admin_command(args, _migration_payload)


# LLM: 统一包装只处理 CLI 错误边界，不吞掉 Service 的结构化失败报告。
# 函数用途: 创建当前 owner Agent、执行命令并以稳定退出码输出结果。
def _run_admin_command(
    args: Any,
    operation: Callable[[Any, Any], dict[str, object]],
) -> int:
    try:
        agent = make_agent(args)
        payload = operation(agent, args)
    except Exception as exc:  # noqa: BLE001 - CLI must return a bounded diagnostic.
        payload = {
            "ok": False,
            "command": str(getattr(args, "memory_area", "memory")),
            "error_code": "MEMORY_ADMIN_" + type(exc).__name__.upper(),
            "message": str(exc),
        }
    _print_admin_payload(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if bool(payload.get("ok")) else 1


# LLM: Candidate 内容只在管理员显式 list 中返回；排序和 schema 校验由 repository 保证。
# 函数用途: 构造候选列表报告。
def _candidate_list_payload(agent: Any, args: Any) -> dict[str, object]:
    statuses = set(args.status) if getattr(args, "status", None) else None
    limit = max(0, int(getattr(args, "limit", 50)))
    records = agent.memory_candidates.list(statuses=statuses, limit=limit)
    return {
        "ok": True,
        "command": "memory candidates list",
        "owner_id": str(getattr(agent.home_paths, "owner_id", "") or "local/main"),
        "candidate_source": str(agent.memory_candidates.path),
        "statuses": sorted(statuses) if statuses else [],
        "count": len(records),
        "candidates": [item.to_record() for item in records],
    }


# LLM: blocked 候选只能先回到 pending_review；两步中断会留下安全待审态，不会误写正式 Memory。
# 函数用途: 按管理员决定推进唯一候选状态机。
def _candidate_review_payload(agent: Any, args: Any) -> dict[str, object]:
    service = agent.memory_candidates
    promotion = agent.memory_promotion
    candidate_id = str(args.candidate_id)
    reviewer = str(args.reviewer)
    note = str(args.note or "")
    decision = str(args.decision)
    current = service.get(candidate_id)
    metadata = _review_metadata(args)
    if decision == "approve":
        if current.status in _BLOCKED_STATUSES:
            current = service.transition(
                candidate_id,
                "pending_review",
                reviewer=reviewer,
                review_note=note or "管理员已处理阻塞项，退回待审核。",
                **metadata,
            )
        candidate = promotion.review(
            current.candidate_id,
            approved=True,
            reviewer=reviewer,
            note=note,
            proposed_action=metadata.get("proposed_action"),
            target_entry_id=metadata.get("target_entry_id"),
            promotion_target=metadata.get("promotion_target"),
        )
    elif decision == "reject":
        candidate = promotion.review(
            candidate_id,
            approved=False,
            reviewer=reviewer,
            note=note,
            **_promotion_review_metadata(metadata),
        )
    else:
        target = {"reopen": "pending_review", "expire": "expired", "supersede": "superseded"}[decision]
        candidate = service.transition(
            candidate_id,
            target,
            reviewer=reviewer,
            review_note=note,
            **metadata,
        )
    return {
        "ok": True,
        "command": "memory candidates review",
        "decision": decision,
        "candidate": candidate.to_record(),
    }


# LLM: optional 审核字段只在用户显式传入时传给 Service，避免空值意外覆盖既有决策。
# 函数用途: 收集 CandidateService.transition 可接受的元数据。
def _review_metadata(args: Any) -> dict[str, object]:
    values = {
        "proposed_action": getattr(args, "proposed_action", None),
        "target_entry_id": getattr(args, "target_entry_id", None),
        "promotion_target": getattr(args, "promotion_target", None),
    }
    return {key: value for key, value in values.items() if value is not None}


# LLM: PromotionService.review 不接收 conflicts_with；CLI 也不凭自然语言重写冲突引用。
# 函数用途: 把审核元数据限制为 PromotionService 的公开参数。
def _promotion_review_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in metadata.items()
        if key in {"proposed_action", "target_entry_id", "promotion_target"}
    }


# LLM: promote 返回的 reason_code 是机器判断依据，CLI 不从说明文字猜成功。
# 函数用途: 构造一次正式晋升报告。
def _candidate_promote_payload(agent: Any, args: Any) -> dict[str, object]:
    result = agent.memory_promotion.promote(
        str(args.candidate_id),
        reviewer=str(args.reviewer),
        automatic=bool(args.automatic),
        confirmed=bool(args.confirmed),
    )
    return {
        "ok": bool(result.promoted),
        "command": "memory candidates promote",
        "promotion": result.to_dict(),
        "candidate": agent.memory_candidates.get(str(args.candidate_id)).to_record(),
    }


# LLM: status 直接由装配在 Agent 上的唯一 CuratorService 给出，不另读并解释 state 文件。
# 函数用途: 构造 Curator 状态报告。
def _curator_status_payload(agent: Any, _args: Any) -> dict[str, object]:
    return {
        "ok": True,
        "command": "memory curator status",
        "curator": agent.memory_curator.status(),
    }


# LLM: request 与 run 使用固定 admin enum；自由文本不能创建隐藏触发分支。
# 函数用途: 构造管理员显式 Curator 运行报告。
def _curator_run_payload(agent: Any, args: Any) -> dict[str, object]:
    request = agent.memory_curator.request("admin")
    result = agent.memory_curator.run(reason="admin", force=bool(args.force))
    return {
        "ok": result.status == "succeeded",
        "command": "memory curator run",
        "request": request,
        "run": result.to_dict(),
        "curator": agent.memory_curator.status(),
    }


# LLM: recover 是人工恢复通道, 报告如实反映校验链结果; 拒绝与失败都不是「成功」。
# 函数用途: 构造人工恢复 Curator state 的结构化报告。
def _curator_recover_payload(agent: Any, _args: Any) -> dict[str, object]:
    result = agent.memory_curator.recover()
    return {
        "ok": result.get("status") == "recovered",
        "command": "memory curator recover",
        "recover": result,
    }


# LLM: report.ok 只表示无错误，plan.applied 保持 false；两者不能被 CLI 合并成一个含义。
# 函数用途: 构造只读 retention 计划报告。
def _retention_plan_payload(agent: Any, _args: Any) -> dict[str, object]:
    report = _retention_service(agent).plan()
    return {
        "ok": report.ok,
        "command": "memory retention plan",
        "retention": report.to_dict(),
    }


# LLM: apply 的成功必须来自 Service 报告且 applied=true；legal hold 安全停止不冒充已执行。
# 函数用途: 构造 retention 应用报告。
def _retention_apply_payload(agent: Any, _args: Any) -> dict[str, object]:
    report = _retention_service(agent).apply()
    return {
        "ok": report.ok and report.applied,
        "command": "memory retention apply",
        "retention": report.to_dict(),
    }


# LLM: ConversationStore root 只作为 RetentionService 的显式外部权威根，不扩大到 workspace/home 根。
# 函数用途: 从当前 Agent 依赖构造唯一 v2 RetentionService。
def _retention_service(agent: Any) -> MemoryRetentionService:
    conversation_root = getattr(getattr(agent.conversation_store, "storage", None), "root", None)
    roots = (Path(conversation_root),) if conversation_root is not None else ()
    return MemoryRetentionService(
        home_paths=agent.home_paths,
        candidates=agent.memory_candidates,
        long_term=agent.memory,
        conversation_roots=roots,
    )


# LLM: migrate/doctor 都复用同一构造方法，避免 legacy roots 在两个 CLI 路径漂移。
# 函数用途: 从当前 Agent 依赖构造一次性 MemoryMigrationService。
def _migration_service(agent: Any) -> MemoryMigrationService:
    roots = tuple(
        getattr(agent, "effective_workspace_roots", ())
        or getattr(agent, "workspace_roots", ())
        or (agent.root,)
    )
    daily = getattr(agent.memory_curator, "daily_store", None)
    if not isinstance(daily, DailyMemoryStore):
        raise RuntimeError("Memory Curator 未装配正式 DailyMemoryStore")
    lessons = getattr(agent, "memory_lessons", None)
    if not isinstance(lessons, LessonRepository):
        raise RuntimeError("Memory Promotion 未装配正式 LessonRepository")
    return MemoryMigrationService(
        home_paths=agent.home_paths,
        candidates=agent.memory_candidates,
        long_term=agent.memory,
        daily=daily,
        lessons=lessons,
        legacy_workspace_roots=roots,
    )


# LLM: migration report 内不含旧正文；CLI 不额外读取备份内容打印到终端。
# 函数用途: 构造 dry-run 或显式 apply 迁移报告。
def _migration_payload(agent: Any, args: Any) -> dict[str, object]:
    service = _migration_service(agent)
    report = service.apply() if bool(args.apply) else service.plan()
    return {
        "ok": report.ok,
        "command": "memory migrate",
        "mode": "apply" if bool(args.apply) else "dry-run",
        "migration": report.to_dict(),
    }


# LLM: doctor 只组合无正文健康摘要；候选正文、对话正文和工具大输出都不能出现在报告中。
# 函数用途: 汇总 Memory v2 正式组件的只读诊断。
def _doctor_payload(agent: Any, args: Any) -> dict[str, object]:
    index_path = _resolve_index_path(
        agent.root,
        getattr(args, "index", None),
        home_paths=getattr(agent, "home_paths", None),
    )
    routing = _build_routing_doctor(agent.root, index_path)
    migration = _migration_service(agent).plan()
    retention = _retention_service(agent).plan()
    ok = not routing["load_error"] and migration.ok and retention.ok
    return {
        "ok": ok,
        "command": "memory doctor",
        "owner_id": str(getattr(agent.home_paths, "owner_id", "") or "local/main"),
        "workspace_root": str(agent.root),
        "config": _memory_config_payload(agent.config),
        "config_warnings": _config_warnings(agent.config),
        "candidate": agent.memory_candidates.stats(),
        "curator": agent.memory_curator.status(),
        "routing": routing,
        "archive": _build_archive_doctor(agent.root, agent.config),
        "migration": migration.to_dict(),
        "retention": retention.to_dict(),
    }


# LLM: JSON key 保持机器协议；普通输出增加中文标题，字段逐项中文含义由 --help 和 Memory 文档维护。
# 函数用途: 输出统一管理命令结果。
def _print_admin_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY 管理")
    print(f"命令：{payload.get('command', 'memory')}")
    print(f"成功：{'是' if payload.get('ok') else '否'}")
    if payload.get("error_code"):
        print(f"错误码：{payload['error_code']}")
        print(f"说明：{payload.get('message', '')}")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


__all__ = [
    "cmd_memory_candidates_list",
    "cmd_memory_candidates_promote",
    "cmd_memory_candidates_review",
    "cmd_memory_curator_run",
    "cmd_memory_curator_status",
    "cmd_memory_migrate",
    "cmd_memory_retention_apply",
    "cmd_memory_retention_plan",
    "cmd_memory_v2_doctor",
]
