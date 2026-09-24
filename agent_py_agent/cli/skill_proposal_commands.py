# LLM: `skills` 命令树是用户查看、确认、拒绝自学习 Skill 提案的唯一入口；确认只调用 SkillProposalService.confirm，
#   必须带用户看到的 --expected-revision，从不 force guard。本模块不直接读写提案 JSON、Candidate 账本或 skills
#   目录，也不构造 Agent；owner 与 home 按配置解析，规则与轻量客户端相同。不要把这些处理器暴露成模型工具。
#   list 可选接自学习 S2 审核顺序点：只在其返回采用结果时重排展示并加宿主标签，否则输出与原来逐字节一致。
# 模块用途: 注册 `my-agent skills proposals list|show|confirm|reject`，输出中文说明或 --json 机器字段。
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from ..agent.capability.decision_skill_proposal_review import skill_proposal_review_order
from ..agent.capability.skill_proposals import (
    CODE_COMMITTED,
    PROPOSAL_PENDING,
    PROPOSAL_STATUSES,
    SkillProposalError,
    SkillProposalOutcome,
    SkillProposalService,
)
from ..agent.settings import load_config
from ..agent.settings.services.runtime_config_env import apply_runtime_config_environment
from ..agent.user_space.home_layout import home_paths
from ..agent.user_space.home_root import configured_home_root
from ..agent.user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)

_STATUS_HELP = "状态：pending_confirmation=待确认，committed=已确认并安装，rejected=已拒绝。"
_STATUS_LABELS = {"pending_confirmation": "待确认", "committed": "已确认并安装", "rejected": "已拒绝"}
_ID_HELP = "提案编号 proposal_id（24 位十六进制，可从 list 获得）"
_REVIEW_NOTE = "审核顺序：待确认提案已按决策模型建议排列，仅供参考；确认或拒绝仍需你逐条执行。"


# LLM: 命令上下文只携带当前 owner 的提案服务、展示用事实与决策宿主；开关状态只用于展示，不决定能否确认。
#   decision_host 只有已认证 owner 路径与配置，供审核顺序点读取原决策设置和调用原决策服务，不含 Agent 或提案写权限。
# 类用途: 保存一次 skills proposals 命令解析出的 owner 服务、开关状态和决策宿主。
@dataclass(frozen=True)
class _ProposalCommandContext:
    service: SkillProposalService
    owner_id: str
    self_learning_enabled: bool
    decision_host: object


# LLM: 顶层 skills 只挂 proposals 子树；不提供任何绕过用户确认直接写正式 Skill 的命令。
# 函数用途: 注册 skills 命令树。
def add_skill_subcommands(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "skills",
        help="管理自学习生成、等待用户确认的 Skill 提案",
        description="Skill 的用户确认入口。提案只有经 confirm 复核通过后才会安装为正式 owner Skill。",
    )
    areas = parser.add_subparsers(dest="skills_area", required=True)
    proposals = areas.add_parser(
        "proposals",
        help="查看、确认或拒绝自学习 Skill 提案",
        description="提案保存在当前 owner 的 data/skill_proposals/。" + _STATUS_HELP,
    )
    actions = proposals.add_subparsers(dest="skills_proposals_action", required=True)
    _add_list_and_show(actions)
    _add_resolution(
        actions, "confirm", "确认提案：复核版本、来源候选和安全扫描后安装为正式 owner Skill", cmd_skill_proposals_confirm
    )
    _add_resolution(actions, "reject", "拒绝提案：只改提案状态，不写任何 Skill", cmd_skill_proposals_reject)


# LLM: list/show 只读；--status 取值与服务端状态枚举一致。
# 函数用途: 注册列出与查看两个子命令。
def _add_list_and_show(actions: argparse._SubParsersAction) -> None:
    listing = actions.add_parser("list", help="按创建时间列出当前 owner 的提案")
    listing.add_argument("--status", choices=sorted(PROPOSAL_STATUSES), help="按提案状态过滤。" + _STATUS_HELP)
    listing.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    listing.set_defaults(func=cmd_skill_proposals_list)
    show = actions.add_parser("show", help="查看一条提案的来源任务、触发原因、拟保存内容和适用场景")
    show.add_argument("proposal_id", help=_ID_HELP)
    show.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    show.set_defaults(func=cmd_skill_proposals_show)


# LLM: 确认与拒绝都必须显式给出 --expected-revision，旧版本号或已处理的提案会被拒绝。
# 函数用途: 注册一个需要提案编号和版本号的处理子命令。
def _add_resolution(
    actions: argparse._SubParsersAction,
    name: str,
    help_text: str,
    handler: Callable[[Any], int],
) -> None:
    parser = actions.add_parser(name, help=help_text)
    parser.add_argument("proposal_id", help=_ID_HELP)
    parser.add_argument(
        "--expected-revision",
        type=int,
        required=True,
        help="你查看时的提案版本号 revision；与当前版本不一致时拒绝操作",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=handler)


# LLM: 只读列出，不创建提案目录。
# 函数用途: 处理 `skills proposals list`。
def cmd_skill_proposals_list(args: Any) -> int:
    return _run(args, "skills proposals list", _list_payload)


# LLM: 只读查看，提案不存在或损坏时返回结构化错误码。
# 函数用途: 处理 `skills proposals show`。
def cmd_skill_proposals_show(args: Any) -> int:
    return _run(args, "skills proposals show", _show_payload)


# LLM: 用户显式确认；成功时服务端安装 Skill 并把提案标为 committed。
# 函数用途: 处理 `skills proposals confirm`。
def cmd_skill_proposals_confirm(args: Any) -> int:
    return _run(args, "skills proposals confirm", _confirm_payload)


# LLM: 用户显式拒绝；只改提案状态。
# 函数用途: 处理 `skills proposals reject`。
def cmd_skill_proposals_reject(args: Any) -> int:
    return _run(args, "skills proposals reject", _reject_payload)


# LLM: 统一错误边界：服务拒绝保留其结果码，其它异常转成有界诊断码；退出码只由 ok 决定。
# 函数用途: 解析 owner、执行一个子命令并输出结果。
def _run(
    args: Any,
    command: str,
    operation: Callable[[_ProposalCommandContext, Any], dict[str, object]],
) -> int:
    try:
        payload = operation(_owner_context(args), args)
    except SkillProposalError as exc:
        payload = {"ok": False, "error_code": exc.code, "message": str(exc), "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001 - CLI 必须返回有界诊断而不是堆栈。
        payload = {"ok": False, "error_code": "SKILL_PROPOSAL_CLI_" + type(exc).__name__.upper(), "message": str(exc)}
    payload = {"command": command, **payload}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload.get("ok") else 1


# LLM: 与轻量客户端同一 owner 解析：配置层→home 根→可信 owner→owner 路径投影；不初始化 Agent、不创建目录。
#   决策宿主沿 Gateway 设置服务同一形状（owner 路径 + 配置），读取决策设置和模型目录时不写文件。
# 函数用途: 按配置找到当前 owner 的提案服务和决策宿主。
def _owner_context(args: Any) -> _ProposalCommandContext:
    config = apply_runtime_config_environment(load_config(args.config))
    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    scoped = home_paths_with_owner(base, owner)
    return _ProposalCommandContext(
        service=SkillProposalService(scoped, config=config),
        owner_id=owner.owner_id,
        self_learning_enabled=bool(config.enable_self_learning),
        decision_host=SimpleNamespace(home_paths=scoped, config=config),
    )


# LLM: 列表返回完整提案记录，供用户确认前核对；开关状态只作展示。审核顺序点返回 None 时字段与原输出完全相同；
#   采用时只重排待确认提案的位置并追加 review_order 块，count、记录内容和其它字段不变。
# 函数用途: 构造 list 的输出。
def _list_payload(context: _ProposalCommandContext, args: Any) -> dict[str, object]:
    proposals = context.service.list(status=args.status)
    review = skill_proposal_review_order(context.decision_host, context.service, proposals)
    payload: dict[str, object] = {
        "ok": True,
        "owner_id": context.owner_id,
        "proposal_dir": str(context.service.directory),
        "self_learning_enabled": context.self_learning_enabled,
        "status_filter": args.status or "",
        "count": len(proposals),
        "proposals": [item.to_record() for item in (proposals if review is None else review.proposals)],
    }
    if review is not None:
        payload["review_order"] = review.to_payload()
    return payload


# LLM: 返回完整提案，含来源、触发原因、草稿正文与适用场景。
# 函数用途: 构造 show 的输出。
def _show_payload(context: _ProposalCommandContext, args: Any) -> dict[str, object]:
    return {"ok": True, "proposal": context.service.show(args.proposal_id).to_record()}


# LLM: 版本号原样传给服务端复核，CLI 不自行读取当前版本替用户确认。
# 函数用途: 构造 confirm 的输出。
def _confirm_payload(context: _ProposalCommandContext, args: Any) -> dict[str, object]:
    return _outcome_payload(context.service.confirm(args.proposal_id, args.expected_revision))


# LLM: 版本号原样传给服务端复核。
# 函数用途: 构造 reject 的输出。
def _reject_payload(context: _ProposalCommandContext, args: Any) -> dict[str, object]:
    return _outcome_payload(context.service.reject(args.proposal_id, args.expected_revision))


# LLM: 成功写 code，失败写 error_code，与其它管理命令一致；detail 只含结构化事实。
# 函数用途: 把服务端结果转成 CLI 输出字典。
def _outcome_payload(outcome: SkillProposalOutcome) -> dict[str, object]:
    return {
        "ok": outcome.ok,
        ("code" if outcome.ok else "error_code"): outcome.code,
        "proposal": outcome.proposal.to_record() if outcome.proposal is not None else None,
        "detail": dict(outcome.detail),
    }


# LLM: --json 输出稳定机器字段；普通输出只给人看，不作为任何机器判断依据。
# 函数用途: 打印命令结果。
def _print_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("\n".join(_human_lines(payload)))


# LLM: 只做展示分派，不改变结果。
# 函数用途: 按命令结果选择中文展示内容。
def _human_lines(payload: dict[str, object]) -> list[str]:
    if not payload.get("ok"):
        detail = json.dumps(payload.get("detail") or {}, ensure_ascii=False, sort_keys=True)
        return [f"操作未完成：{payload.get('error_code')}", f"细节：{detail}"]
    if "proposals" in payload:
        return _list_lines(payload)
    proposal = payload.get("proposal") or {}
    if payload.get("code"):
        return [_resolution_line(payload.get("code"), proposal), _proposal_lines(proposal)[0]]
    return _proposal_lines(proposal)


# LLM: 每条提案一行摘要加一行描述，完整内容用 show 查看。只有 payload 带 review_order 时才多一行说明并在
#   对应条目后附宿主固定标签（按 proposal_id 结构化映射，不读模型文案）；否则与原展示逐字节一致。
# 函数用途: 生成提案列表的中文展示。
def _list_lines(payload: dict[str, object]) -> list[str]:
    switch = "开启" if payload.get("self_learning_enabled") else "关闭"
    lines = [f"Skill 提案（owner {payload.get('owner_id')}，共 {payload.get('count')} 条；自学习开关：{switch}）"]
    review = payload.get("review_order")
    labels = {row["proposal_id"]: row["label"] for row in review["order"]} if review else {}
    if review:
        lines.append(_REVIEW_NOTE)
    for item in payload.get("proposals") or []:
        status = _STATUS_LABELS.get(item["status"], item["status"])
        tasks = "、".join(item["source"]["task_ids"])
        label = labels.get(item["proposal_id"])
        lines.append(f"- {item['proposal_id']} [{status}] 版本 {item['revision']} 目标 {item['target']['skill_name']} 来源任务 {tasks}"
                     + (f" 〔{label}〕" if label else ""))
        lines.append(f"  {item['draft']['description']}")
    return lines


# LLM: 展示确认前必须核对的全部事实；只有待确认提案才给出带当前版本号的确认命令。
# 函数用途: 生成单条提案的中文详情。
def _proposal_lines(proposal: dict) -> list[str]:
    source, target, draft = proposal["source"], proposal["target"], proposal["draft"]
    lines = [
        f"提案 {proposal['proposal_id']}：{_STATUS_LABELS.get(proposal['status'], proposal['status'])}（版本 {proposal['revision']}）",
        f"触发原因：{proposal['trigger']}",
        f"来源：候选 {source['candidate_id']}；任务 {'、'.join(source['task_ids'])}；运行 {'、'.join(source['run_ids'])}",
        f"目标 Skill：{target['skill_name']}（安装前必须不存在）",
        f"描述：{draft['description']}",
        f"适用场景：{draft['when_to_use']}",
        "拟保存内容：",
        draft["body"].rstrip(),
    ]
    if proposal["status"] == PROPOSAL_PENDING:
        lines.append(
            f"确认命令：my-agent skills proposals confirm {proposal['proposal_id']} --expected-revision {proposal['revision']}"
        )
    return lines


# LLM: 只按结构化结果码选择文案。
# 函数用途: 生成确认或拒绝成功后的一行结果。
def _resolution_line(code: object, proposal: dict) -> str:
    if code == CODE_COMMITTED:
        return f"已确认并安装 Skill：{proposal.get('receipt', {}).get('skill_path')}"
    return f"已拒绝提案 {proposal.get('proposal_id')}"


__all__ = [
    "add_skill_subcommands",
    "cmd_skill_proposals_confirm",
    "cmd_skill_proposals_list",
    "cmd_skill_proposals_reject",
    "cmd_skill_proposals_show",
]
