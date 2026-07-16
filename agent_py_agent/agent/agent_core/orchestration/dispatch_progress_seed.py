"""create_subagents 派工时把每个子代理自动登记成 task_progress 待办(学 终端应用 TodoWrite 的结构化外化)。

为什么在这里种:真机实锤(A1×3 全新用户)模型光靠提示词几乎不主动建清单(3/3 零调用),于是
completion 的"开放待办不当轮自动收口"与 final_exit 的 todo 续航(_todo_persistence_decision)
全部空转。派工是模型【自己宣告的计划】——把它原样落成账本(每个子代理一条 in_progress +
一条"整合成可运行成品"收尾项),不猜任务类型、不外加数量配额(R9-safe:条目=模型自己的决定,
不是外部写死的指标)。账本落在本轮 run 的 ledger(与 task_progress 工具同一 run_id 解析链),
同轮的收口闸 / 出口续航都读得到;后续轮模型用 task_progress 勾 / 改。种子永不抛错——失败绝不
影响派工本身。

covers 绑定侧(P1 + P-bigbuild)也在本模块:绑定回执(dispatch_coverage_binding)、清单账本
的主账本回落(_binding_ledger_targets:子/孙代理派工现场也看得到任务主清单 open 项)、goal
字面 id 的 covers 落难兜底(autobind_covers_from_goal_ids)。同一防御姿态:全部只读账本或只改
派工参数,主账本的打勾只由主代理侧对账(dispatch_coverage_reconcile)完成;失败永不影响派工。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ...task_progress import read_task_progress, task_progress_status_is_closed, write_task_progress
from ..runner.context import current_subagent_run_id
from ..runtime.task_identity import durable_task_id, progress_ledger_id

_INTEGRATION_ITEM_ID = "integrate-and-verify"
_MAX_BINDING_OPEN_TARGETS = 24
# goal 字面 id 兜底的最短 id 长度:太短的 id(如"1""a")在任意文本里撞车概率高,不兜。
_GOAL_ID_MIN_LEN = 4

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

COVERS_PARENT_LEDGER_UNKNOWN_NOTE = (
    "unknown_covers_ids 里的 id 在任务主清单(ledger_run_id 那本账)里不存在,绑定不会生效;"
    '用 task_progress(action=read, run_id=ledger_run_id 的值) 查正确的清单项 id 后重绑。'
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
    ledger_run_id, targets = _binding_ledger_targets(agent, root, run_id)
    if not targets:
        return None
    known_ids = {str(target.get("id") or "").strip() for target in targets}
    open_ids = [
        str(target.get("id") or "").strip()
        for target in targets
        if not task_progress_status_is_closed(target.get("status"))
    ]
    bound: dict[str, list[str]] = {}
    auto_bound: dict[str, list[str]] = {}
    unknown: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "").strip()
        covers = _task_covers(task)
        if not task_id or not covers:
            continue
        bound[task_id] = covers
        if goal_ids := _task_goal_auto_bound(task):
            auto_bound[task_id] = goal_ids
        unknown.extend(target_id for target_id in covers if target_id not in known_ids)
    if not bound and not open_ids:
        return None
    payload: dict[str, Any] = {
        "open_target_ids": open_ids[:_MAX_BINDING_OPEN_TARGETS],
        "open_count": len(open_ids),
    }
    if ledger_run_id != run_id:
        payload["ledger_run_id"] = ledger_run_id
    if bound:
        payload["bound"] = bound
    if auto_bound:
        payload["auto_bound_from_goal"] = auto_bound
    if unknown:
        payload["unknown_covers_ids"] = list(dict.fromkeys(unknown))[:12]
        payload["note"] = COVERS_PARENT_LEDGER_UNKNOWN_NOTE if ledger_run_id != run_id else COVERS_UNKNOWN_NOTE
    elif not bound:
        payload["note"] = COVERS_BINDING_NOTE
    return payload


def _binding_ledger_targets(agent: object, root: Path, run_id: str) -> tuple[str, list[dict[str, Any]]]:
    """covers 绑定对账用的清单账本:本 run 的账优先;本 run 没立 coverage 且这是任务树里的
    深层 run(params.task_id≠run_id,子代理/孙代理)时,回落读【任务主账本】(task_id 那本,
    A2 种需求清单的账)。为什么(P-bigbuild):大工程模型递归乱派、层层转包,主代理大现场
    绑不上 covers;树深处的派工小现场反而干净(一次派几个具体活)——把主清单 open 项带到
    每一层派工现场,孙代理绑的 covers 由收口树归并(own_done_children 后代闭包)收回父清单。
    只读不写:主账本的打勾仍只由主代理侧对账完成,单写者纪律不破。"""
    targets = _coverage_targets(read_task_progress(root, run_id))
    if targets:
        return run_id, targets
    task_id = _run_task_id(agent)
    if task_id and task_id != run_id:
        targets = _coverage_targets(read_task_progress(root, task_id))
        if targets:
            return task_id, targets
    return run_id, []


def _coverage_targets(progress: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = progress.get("coverage") if isinstance(progress, dict) else None
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    return [target for target in targets if isinstance(target, dict)] if isinstance(targets, list) else []


def _run_task_id(agent: object) -> str:
    return durable_task_id(getattr(agent, "_current_run_params", None))


def _task_covers(task: object) -> list[str]:
    attrs = getattr(task, "attributes", None)
    covers = attrs.get("covers") if isinstance(attrs, dict) else None
    if not isinstance(covers, list | tuple):
        return []
    return [str(item).strip() for item in covers if str(item or "").strip()]


def _task_goal_auto_bound(task: object) -> list[str]:
    attrs = getattr(task, "attributes", None)
    bound = attrs.get("covers_auto_bound") if isinstance(attrs, dict) else None
    if not isinstance(bound, list | tuple):
        return []
    return [str(item).strip() for item in bound if str(item or "").strip()]


def autobind_covers_from_goal_ids(agent: object, items: list) -> int:
    """派工参数落难兜底(P-bigbuild):item 没带 covers、goal 文本却字面写了清单项 id
    (如"实现 req-03 用户管理模块")→ 系统自动补绑 covers=[这些 id]。真机形态:模型知道
    对应关系(把 id 写进了 goal)却丢了 covers 参数(create_subagents 格式驾驭弱是实锤)。
    判据=字面 token 等值+词边界,id 是立账时造的结构化标识,非自然语言/关键词匹配——守铁律。
    只补空缺:显式带 covers 的 item 一字不动;账本(含主账本回落)没有 open 项就整个不跑。
    补绑同时在 item attributes 记 covers_auto_bound(审计+回执回显)。永不抛错。"""
    try:
        return _autobind(agent, items)
    except Exception:  # noqa: BLE001 - 兜底是增强,失败绝不影响派工
        logging.getLogger(__name__).warning("goal-id covers autobind failed", exc_info=True)
        return 0


def _autobind(agent: object, items: list) -> int:
    root = _progress_root(agent)
    run_id = _current_run_id(agent)
    if root is None or not run_id or not items:
        return 0
    _ledger_run_id, targets = _binding_ledger_targets(agent, root, run_id)
    open_ids = [
        target_id
        for target in targets
        if not task_progress_status_is_closed(target.get("status"))
        and len(target_id := str(target.get("id") or "").strip()) >= _GOAL_ID_MIN_LEN
    ]
    if not open_ids:
        return 0
    bound_items = 0
    for item in items:
        params = getattr(item, "params", None)
        if not isinstance(params, dict) or _params_covers(params):
            continue
        matched = _goal_literal_ids(str(getattr(item, "goal", "") or ""), open_ids)
        if not matched:
            continue
        params["covers"] = matched
        attrs = params.get("attributes")
        if isinstance(attrs, dict) or "attributes" not in params:
            attrs = attrs if isinstance(attrs, dict) else {}
            attrs["covers_auto_bound"] = matched
            params["attributes"] = attrs
        bound_items += 1
    return bound_items


def _params_covers(params: dict[str, Any]) -> list[str]:
    covers = params.get("covers")
    if not isinstance(covers, list | tuple):
        return []
    return [str(item).strip() for item in covers if str(item or "").strip()]


def _goal_literal_ids(goal: str, open_ids: list[str]) -> list[str]:
    """goal 文本里字面出现(词边界)的清单项 id。边界只认标识符字符——中文/标点/空白紧邻
    都算边界("实现req-03模块"命中),req-03 不会误命中 req-030/xreq-03。"""
    if not goal:
        return []
    return [
        target_id
        for target_id in open_ids
        if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(target_id) + r"(?![A-Za-z0-9_-])", goal)
    ]


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
    # 与 task_progress 工具、需求 seed、收口闸共用唯一账本键。主代理停止后恢复会换
    # request/run_id，但 conversation_task_id 不变；子代理仍按自己的 scoped run 隔离。
    params = getattr(agent, "_current_run_params", None)
    return progress_ledger_id(
        agent,
        params,
        scoped_id=current_subagent_run_id(agent),
    ) or "main"


__all__ = [
    "COVERS_BINDING_NOTE",
    "COVERS_PARENT_LEDGER_UNKNOWN_NOTE",
    "COVERS_UNKNOWN_NOTE",
    "DISPATCH_SEED_NOTE",
    "autobind_covers_from_goal_ids",
    "dispatch_coverage_binding",
    "seed_dispatch_task_progress",
]
