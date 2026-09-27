# LLM: 聊天 `/skills` 的执行与中文回执，TUI 与飞书等 IM 共用（都经 Gateway 控制通道）。只处理当前控制范围解析出的 owner：
#   提案走 SkillProposalService（确认/拒绝必须带用户看到的版本号，服务端在锁内复核），自动总结 Skill 走
#   skill_learning_report（与 CLI 同一事实推导与闸门）；列提案时调用自学习审核顺序点 skill_proposal_review_order（与 CLI
#   同一入口）。不构造 Agent、不暴露给模型；回执不含本机绝对路径。改动须同步 control_commands._skills_command、
#   command_catalog 的 skills 条目、TUI control_runtime 的文本还原与本地拒绝，以及 test_skill_chat_control.py。
# 模块用途: 让用户在 TUI 和 IM 里查看、确认、拒绝技能提案，查看、回滚、删除自动总结的 Skill。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..capability.decision_skill_proposal_review import skill_proposal_review_order
from ..capability.skill_learning_publish import SkillLearningGateError
from ..capability.skill_learning_report import (
    apply_learned_skill_action,
    learned_skill_detail,
    learned_skills_report,
)
from ..capability.skill_learning_store import (
    EVENT_REMOVED,
    SkillLearningStore,
    SkillLearningStoreError,
)
from ..capability.skill_proposals import (
    CODE_COMMITTED,
    CODE_NOT_PENDING,
    CODE_REVISION_MISMATCH,
    CODE_TARGET_EXISTS,
    PROPOSAL_PENDING,
    SkillProposal,
    SkillProposalError,
    SkillProposalOutcome,
    SkillProposalService,
)
from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner

_KIND = "skills"
_SHORT_ID = 8
_BODY_MAX_CHARS = 3000
_EVENT_ROWS = 10
_STATUS_LABELS = {"pending_confirmation": "待确认", "committed": "已确认并安装", "rejected": "已拒绝"}
_STATE_LABELS = {"active": "生效中", "user_modified": "已被你修改（不再自动更新）", "missing": "文件已不存在"}
_REVIEW_NOTE = "已按决策模型建议的审核顺序排列，仅供参考；确认或拒绝仍需你逐条执行。"
_FAILURE_HINTS = {
    CODE_REVISION_MISMATCH: "提案已有更新，版本号对不上；请先用 /skills show 查看最新内容和版本再操作。",
    CODE_NOT_PENDING: "这条提案已经不是待确认状态，无需再处理。",
    CODE_TARGET_EXISTS: "同名 Skill 已经存在，这条提案不能安装；可以用 /skills reject 拒绝它。",
}
# 会改提案或 Skill 的子命令：改动可能在异常前已经生效（例如删除后追加账本失败），回执不能承诺“没有改动”。
_WRITE_OPERATIONS = frozenset({"confirm", "reject", "learned_revert", "learned_remove"})
_READ_FAILURE = "技能提案或自动 Skill 暂时读不到，请稍后重试。"
_WRITE_UNCONFIRMED = "这次操作的结果没能完整确认，可能已经生效；请先发 /skills 或 /skills learned 查看当前状态，再决定是否重试。"


# LLM: 只携带已解析 owner 的路径投影与配置；也作审核顺序点的决策宿主（与 CLI 的 decision_host 同形状）。
# 类用途: 一次 /skills 控制解析出的 owner 路径与配置。
@dataclass(frozen=True)
class SkillControlHost:
    home_paths: object
    config: object


# LLM: 只表示可预期的用户输入问题（编号找不到或不唯一），消息直接给用户看，不含路径。
# 类用途: /skills 的可预期失败。
class SkillControlError(Exception):
    pass


# LLM: Gateway 控制分派入口；可预期失败（都发生在改动之前）给结构化失败回执。其它异常不泄露路径或堆栈，按子命令是否写入
#   选回执：只读子命令回“暂时读不到”；写子命令可能在改动生效后才失败（如 append_event 抛 OSError），只说结果没能完整确认、
#   请先查当前状态，不承诺“没有改动”或直接重试。改动须同步 test_skill_chat_control.py 的提交后失败测试。
# 函数用途: 执行一条已解析的 /skills 控制并返回给用户的中文回执（确认、拒绝、回滚、删除会写文件）。
def execute_skill_control(base_agent: object, command: ConversationControlCommand, scope: object) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(_KIND, False, command.usage)
    try:
        text = run_skill_control(_scoped_host(base_agent, scope), command)
    except SkillControlError as exc:
        return ConversationControlResult(_KIND, False, str(exc))
    except SkillProposalError as exc:
        return ConversationControlResult(_KIND, False, _FAILURE_HINTS.get(exc.code, f"操作未完成（{exc.code}）。"))
    except (SkillLearningGateError, SkillLearningStoreError) as exc:
        return ConversationControlResult(_KIND, False, f"操作未完成（{getattr(exc, 'code', type(exc).__name__)}）。")
    except Exception:  # noqa: BLE001 - 控制回执不能带堆栈或本机路径。
        return ConversationControlResult(
            _KIND, False, _WRITE_UNCONFIRMED if command.operation in _WRITE_OPERATIONS else _READ_FAILURE)
    return ConversationControlResult(_KIND, True, text)


# LLM: 与 /model 文字控制同一 owner 解析（resolve_gateway_scope_owner → owner 路径投影）；只读，不创建目录。
# 函数用途: 按控制范围找到当前用户的 owner 路径与配置。
def _scoped_host(base_agent: object, scope: object) -> SkillControlHost:
    owner = resolve_gateway_scope_owner(base_agent, scope)
    base_home = base_agent.home_paths
    return SkillControlHost(home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner)), base_agent.config)


# LLM: 按解析器给出的 operation 分派；tokens 是解析器规范化后的参数（含子命令本身）；help 与未知子命令回用法。
#   确认/拒绝/回滚/删除有写文件副作用。
# 函数用途: 对已解析的 owner 执行 /skills 的一个子命令，返回中文回执。
def run_skill_control(host: SkillControlHost, command: ConversationControlCommand) -> str:
    if command.operation == "help":
        return command.usage
    handler = _HANDLERS.get(command.operation)
    if handler is None:
        raise SkillControlError(command.usage)
    return handler(host, command.value.split())


# 函数用途: 取当前 owner 的提案服务。
def _proposals_service(host: SkillControlHost) -> SkillProposalService:
    return SkillProposalService(host.home_paths, config=host.config)


# LLM: 只接受提案编号或其前缀（解析器已限定为 6—24 位十六进制），在本 owner 的提案里精确匹配；不唯一或找不到都拒绝。
# 函数用途: 把用户给的提案编号前缀解析成一条提案。
def _proposal_by_ref(service: SkillProposalService, ref: str) -> SkillProposal:
    matches = [item for item in service.list() if item.proposal_id.startswith(ref)]
    if not matches:
        raise SkillControlError(f"没有编号以 {ref} 开头的提案；可发 /skills proposals all 查看全部。")
    if len(matches) > 1:
        raise SkillControlError(f"编号 {ref} 对应多条提案，请多输入几位编号。")
    return matches[0]


# 函数用途: /skills —— 待确认的提案（可能按审核顺序排列）加自动总结 Skill 的数量。
def _overview(host: SkillControlHost, tokens: list[str]) -> str:
    lines = _proposal_list_lines(host, status=PROPOSAL_PENDING)
    report = learned_skills_report(SkillLearningStore.for_home(host.home_paths), host.config)
    lines += ["", f"自动总结的 Skill：{report['count']} 个（发 /skills learned 查看）。"]
    return "\n".join(lines)


# 函数用途: /skills proposals [all] —— 列出待确认或全部提案。
def _proposals(host: SkillControlHost, tokens: list[str]) -> str:
    return "\n".join(_proposal_list_lines(host, status=None if tokens[1:] == ["all"] else PROPOSAL_PENDING))


# LLM: 列表调用审核顺序点（与 CLI 同一入口）；它返回 None 时保持服务原顺序，只有采用时才重排并加宿主标签。只读。
# 函数用途: 生成提案列表的聊天展示，每条待确认提案附上可直接复制的查看、确认、拒绝命令。
def _proposal_list_lines(host: SkillControlHost, *, status: str | None) -> list[str]:
    service = _proposals_service(host)
    proposals = service.list(status=status)
    review = skill_proposal_review_order(host, service, proposals)
    ordered = list(proposals if review is None else review.proposals)
    labels = {} if review is None else {entry.proposal_id: entry.label for entry in review.entries}
    switch = "开启" if getattr(host.config, "enable_self_learning", False) else "关闭"
    title = "待确认的技能提案" if status == PROPOSAL_PENDING else "技能提案"
    lines = [f"{title}：{len(ordered)} 条（自学习：{switch}）"]
    if not ordered:
        return lines + ["没有需要你处理的提案。"]
    if review is not None:
        lines.append(_REVIEW_NOTE)
    for index, proposal in enumerate(ordered, 1):
        lines += _proposal_entry_lines(index, proposal, labels.get(proposal.proposal_id, ""))
    return lines


# 函数用途: 生成列表里的一条提案（摘要一行、来源一行、操作一行）。
def _proposal_entry_lines(index: int, proposal: SkillProposal, label: str) -> list[str]:
    status = _STATUS_LABELS.get(proposal.status, proposal.status)
    lines = [f"{index}. {proposal.target.skill_name}［{status}］{proposal.draft.description}" + (f"〔{label}〕" if label else ""),
             f"   来源任务 {'、'.join(proposal.source.task_ids) or '无'}；版本 {proposal.revision}"]
    return lines + [f"   {_proposal_commands(proposal)}"]


# LLM: 只有待确认提案给出确认/拒绝命令，版本号取当前版本；编号用短前缀，服务端仍按完整编号与版本复核。
# 函数用途: 生成一条提案可直接复制的操作命令。
def _proposal_commands(proposal: SkillProposal) -> str:
    ref = proposal.proposal_id[:_SHORT_ID]
    commands = f"查看 /skills show {ref}"
    if proposal.status == PROPOSAL_PENDING:
        commands += f"　确认 /skills confirm {ref} {proposal.revision}　拒绝 /skills reject {ref} {proposal.revision}"
    return commands


# LLM: 展示确认前必须核对的事实；拟保存内容过长时只显示前 _BODY_MAX_CHARS 字并如实说明。只读。
# 函数用途: /skills show <编号> —— 查看一条提案的详情。
def _show(host: SkillControlHost, tokens: list[str]) -> str:
    proposal = _proposal_by_ref(_proposals_service(host), tokens[1])
    body = proposal.draft.body.rstrip()
    lines = [
        f"提案 {proposal.proposal_id}：{_STATUS_LABELS.get(proposal.status, proposal.status)}（版本 {proposal.revision}）",
        f"目标 Skill：{proposal.target.skill_name}（安装前必须不存在）",
        f"触发原因：{proposal.trigger}",
        f"来源任务：{'、'.join(proposal.source.task_ids) or '无'}；运行：{'、'.join(proposal.source.run_ids) or '无'}",
        f"描述：{proposal.draft.description}",
        f"适用场景：{proposal.draft.when_to_use}",
        "拟保存内容：",
        body[:_BODY_MAX_CHARS],
    ]
    if len(body) > _BODY_MAX_CHARS:
        lines.append(f"（内容共 {len(body)} 字，这里只显示前 {_BODY_MAX_CHARS} 字）")
    return "\n".join(lines + [_proposal_commands(proposal)])


# LLM: 版本号原样交给服务端锁内复核，这里不替用户读取最新版本；成功会安装 Skill（写文件）。
# 函数用途: /skills confirm <编号> <版本> —— 确认并安装一条提案。
def _confirm(host: SkillControlHost, tokens: list[str]) -> str:
    service = _proposals_service(host)
    proposal = _proposal_by_ref(service, tokens[1])
    return _outcome_text(service.confirm(proposal.proposal_id, int(tokens[2])), proposal)


# LLM: 版本号原样交给服务端复核；拒绝只改提案状态（写文件），不碰 Skill 目录。
# 函数用途: /skills reject <编号> <版本> —— 拒绝一条提案。
def _reject(host: SkillControlHost, tokens: list[str]) -> str:
    service = _proposals_service(host)
    proposal = _proposal_by_ref(service, tokens[1])
    return _outcome_text(service.reject(proposal.proposal_id, int(tokens[2])), proposal)


# LLM: 只按结构化结果码选择文案；失败不抛出，转成 SkillControlError 让分派层回失败回执。
# 函数用途: 把确认或拒绝的结果转成一行中文回执。
def _outcome_text(outcome: SkillProposalOutcome, proposal: SkillProposal) -> str:
    if not outcome.ok:
        raise SkillControlError(_FAILURE_HINTS.get(outcome.code, f"没有执行（{outcome.code}）。"))
    if outcome.code == CODE_COMMITTED:
        return f"已确认并安装 Skill：{proposal.target.skill_name}。"
    return f"已拒绝提案 {proposal.proposal_id[:_SHORT_ID]}（{proposal.target.skill_name}）。"


# LLM: 事实来自 skill_learning_report（与 CLI 同一推导）；只读。
# 函数用途: /skills learned —— 列出自动总结的 Skill，每个附上查看、回滚、删除命令。
def _learned(host: SkillControlHost, tokens: list[str]) -> str:
    report = learned_skills_report(SkillLearningStore.for_home(host.home_paths), host.config)
    switch = "开启" if report["self_learning_enabled"] else "关闭"
    limit = report["daily_limit"] or "不限"
    lines = [f"自动总结的 Skill：{report['count']} 个（自学习：{switch}；待处理请求 {report['pending_requests']} 条；"
             f"今日总结 {report['calls_today']}/{limit} 次）"]
    if not report["skills"]:
        return "\n".join(lines + ["还没有自动总结的 Skill。"])
    for index, item in enumerate(report["skills"], 1):
        name = item["name"]
        lines.append(f"{index}. {name} 第 {item['version']} 版［{_STATE_LABELS.get(item['state'], item['state'])}］"
                     f"更新于 {item['updated_at']}")
        lines.append(f"   查看 /skills learned show {name}　回滚 /skills learned revert {name}　删除 /skills learned remove {name}")
    return "\n".join(lines)


# LLM: 名字先在登记表里精确查找（不在表里拒绝，不用它拼路径）；只显示最近 _EVENT_ROWS 条事件。只读。
# 函数用途: /skills learned show <名称> —— 查看一个自动总结 Skill 的登记信息与最近事件。
def _learned_show(host: SkillControlHost, tokens: list[str]) -> str:
    detail = learned_skill_detail(SkillLearningStore.for_home(host.home_paths), tokens[2], limit=_EVENT_ROWS)
    item = detail["skill"]
    lines = [
        f"{item['name']}：第 {item['version']} 版，{_STATE_LABELS.get(item['state'], item['state'])}",
        f"创建于 {item['created_at']}，更新于 {item['updated_at']}",
        f"来源运行：{'、'.join(item['source_run_ids']) or '无'}",
        "最近事件：",
    ]
    for event in detail["events"]:
        code = f" {event.get('code')}" if event.get("code") else ""
        lines.append(f"- {event.get('at')} {event.get('event')} 第 {event.get('version')} 版{code} {event.get('reason') or ''}".rstrip())
    return "\n".join(lines)


# LLM: 回滚与删除走发布闸门（找不到或被用户改过时拒绝），成功后写账本（写文件副作用）。
# 函数用途: /skills learned revert|remove <名称> —— 回滚或删除一个自动总结的 Skill。
def _learned_action(host: SkillControlHost, tokens: list[str]) -> str:
    result = apply_learned_skill_action(SkillLearningStore.for_home(host.home_paths), tokens[2], tokens[1])
    if result["code"] == EVENT_REMOVED:
        return f"已删除 {result['skill_name']}（已归档），以后不再自动生成同名 Skill。"
    return f"已把 {result['skill_name']} 退回到第 {result['version']} 版。"


_HANDLERS: dict[str, Callable[[SkillControlHost, list[str]], str]] = {
    "overview": _overview,
    "proposals": _proposals,
    "show": _show,
    "confirm": _confirm,
    "reject": _reject,
    "learned": _learned,
    "learned_show": _learned_show,
    "learned_revert": _learned_action,
    "learned_remove": _learned_action,
}


__all__ = ["SkillControlError", "SkillControlHost", "execute_skill_control", "run_skill_control"]
