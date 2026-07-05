"""create_subagents 派工时把每个子代理自动登记成 task_progress 待办(学 终端应用 TodoWrite 的结构化外化)。

为什么在这里种:真机实锤(A1×3 全新用户)模型光靠提示词几乎不主动建清单(3/3 零调用),于是
completion 的"开放待办不当轮自动收口"与 final_exit 的 todo 续航(_todo_persistence_decision)
全部空转。派工是模型【自己宣告的计划】——把它原样落成账本(每个子代理一条 in_progress +
一条"整合成可运行成品"收尾项),不猜任务类型、不外加数量配额(R9-safe:条目=模型自己的决定,
不是外部写死的指标)。账本落在本轮 run 的 ledger(与 task_progress 工具同一 run_id 解析链),
同轮的收口闸 / 出口续航都读得到;后续轮模型用 task_progress 勾 / 改。种子永不抛错——失败绝不
影响派工本身。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...task_progress import read_task_progress, task_progress_status_is_closed, write_task_progress
from ..runner.context import current_subagent_run_id

_INTEGRATION_ITEM_ID = "integrate-and-verify"
_MAX_BINDING_OPEN_TARGETS = 24

DISPATCH_SEED_NOTE = (
    "已把每个子代理登记为 task_progress 待办(含最后的整合验证项)。"
    "每验收整合完一块就用 task_progress 把对应项标 done(真做完才标);全部 done 才收尾交付。"
)

COVERS_BINDING_NOTE = (
    "coverage 清单还有 open 项(见 open_target_ids)。把清单里的活派给子代理时,在对应 item 带 "
    'covers=[该项 id](如 covers:["req-03"]);子代理完成后系统按 id 自动把该项标 done。'
    "每个子代理只绑它自己负责的项。"
)

COVERS_UNKNOWN_NOTE = (
    "unknown_covers_ids 里的 id 在当前 coverage 清单里不存在,绑定不会生效;"
    "用 task_progress(action=read) 查正确的清单项 id 后重绑。"
)


def seed_dispatch_task_progress(agent: object, tasks: list) -> dict[str, Any] | None:
    """把本次派出的子代理种进当前 run 的 task_progress 账本;返回 {run_id, seeded} 或 None。"""
    try:
        return _seed(agent, tasks)
    except Exception:  # noqa: BLE001 - 账本种子是增强,失败绝不影响派工
        logging.getLogger(__name__).warning("dispatch task_progress seed failed", exc_info=True)
        return None


def _seed(agent: object, tasks: list) -> dict[str, Any] | None:
    root = _progress_root(agent)
    run_id = _current_run_id(agent)
    if root is None or not run_id or not tasks:
        return None
    existing_ids = {
        str(item.get("id") or "")
        for item in read_task_progress(root, run_id).get("items", [])
        if isinstance(item, dict)
    }
    items = [_task_item(task) for task in tasks]
    items = [item for item in items if item["id"] and item["id"] not in existing_ids]
    if _INTEGRATION_ITEM_ID not in existing_ids:
        items.append(
            {
                "id": _INTEGRATION_ITEM_ID,
                "title": "整合所有子代理产出为可运行成品并亲手跑通验证",
                "status": "pending",
                "next": "等子代理全部完成后自己动手整合、run_command 跑通再交付",
            }
        )
    if not items:
        return None
    write_task_progress(root, run_id, {"items": items, "summary": f"已派 {len(tasks)} 个子代理并登记为待办"})
    return {"run_id": run_id, "seeded": len(items)}


def dispatch_coverage_binding(agent: object, tasks: list) -> dict[str, Any] | None:
    """派工回执里的 coverage 绑定反馈(P1):回显各子代理绑了哪些清单项 id、警示绑错的 id、
    没绑且清单还有 open 项时给一次结构化用法提醒。纯回显/校对,不拦派工;永不抛错。"""
    try:
        return _coverage_binding(agent, tasks)
    except Exception:  # noqa: BLE001 - 回执反馈是增强,失败绝不影响派工
        logging.getLogger(__name__).warning("dispatch coverage binding feedback failed", exc_info=True)
        return None


def _coverage_binding(agent: object, tasks: list) -> dict[str, Any] | None:
    root = _progress_root(agent)
    run_id = _current_run_id(agent)
    if root is None or not run_id or not tasks:
        return None
    coverage = read_task_progress(root, run_id).get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    targets = [target for target in targets if isinstance(target, dict)] if isinstance(targets, list) else []
    if not targets:
        return None
    known_ids = {str(target.get("id") or "").strip() for target in targets}
    open_ids = [
        str(target.get("id") or "").strip()
        for target in targets
        if not task_progress_status_is_closed(target.get("status"))
    ]
    bound: dict[str, list[str]] = {}
    unknown: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "").strip()
        covers = _task_covers(task)
        if not task_id or not covers:
            continue
        bound[task_id] = covers
        unknown.extend(target_id for target_id in covers if target_id not in known_ids)
    if not bound and not open_ids:
        return None
    payload: dict[str, Any] = {
        "open_target_ids": open_ids[:_MAX_BINDING_OPEN_TARGETS],
        "open_count": len(open_ids),
    }
    if bound:
        payload["bound"] = bound
    if unknown:
        payload["unknown_covers_ids"] = list(dict.fromkeys(unknown))[:12]
        payload["note"] = COVERS_UNKNOWN_NOTE
    elif not bound:
        payload["note"] = COVERS_BINDING_NOTE
    return payload


def _task_covers(task: object) -> list[str]:
    attrs = getattr(task, "attributes", None)
    covers = attrs.get("covers") if isinstance(attrs, dict) else None
    if not isinstance(covers, list | tuple):
        return []
    return [str(item).strip() for item in covers if str(item or "").strip()]


def _task_item(task: object) -> dict[str, str]:
    task_id = str(getattr(task, "id", "") or "").strip()
    goal = " ".join(str(getattr(task, "goal", "") or "").split())
    return {
        "id": task_id,
        "title": f"子代理[{task_id[-8:] or '?'}]:{goal[:60] or '(无goal)'}",
        "status": "in_progress",
        "next": "完成后验收其产出并整合",
    }


def _progress_root(agent: object) -> Path | None:
    # 与 task_progress_gate._progress_root 同规:owner home 优先,回落 agent.root。
    owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _current_run_id(agent: object) -> str:
    # canonical run_id = 本 run 的 RunParams.run_id(runtime_mixin 在 run 入口挂 agent._current_run_params,
    #   整个 run 稳定)。这也是交付闸读的那本(closeout.params.run_id)——种子与闸/出口续航必须同本账。
    #   【必须优先它】:create_subagents 同步 auto-start 子代理时会 set/restore thread-local 上下文,
    #   restore 回主代理的【空】上下文,种子在其后读 current_subagent_run_id 会拿到空→误落 "main"
    #   (真机实锤:种子进 main 账本、模型/闸在 req_ 账本,对不上)。_current_run_params 不受该 restore 影响。
    #   【后台唤醒轮】(source=="background_main_agent")按 task_id 落主任务的账(与工具/收尾门同键,
    #   见 task_progress_gate._run_id 的账本跨唤醒轮分裂说明):唤醒轮里补派的子代理待办要记回主账。
    params = getattr(agent, "_current_run_params", None)
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        task_id = str(getattr(params, "task_id", "") or "").strip()
        if task_id:
            return task_id
    run_id = str(getattr(params, "run_id", "") or "").strip()
    if run_id:
        return run_id
    return str(
        current_subagent_run_id(agent)
        or getattr(agent, "_main_agent_run_id", "")
        or getattr(agent, "_current_request_id", "")
        or "main"
    ).strip()


__all__ = [
    "COVERS_BINDING_NOTE",
    "COVERS_UNKNOWN_NOTE",
    "DISPATCH_SEED_NOTE",
    "dispatch_coverage_binding",
    "seed_dispatch_task_progress",
]
