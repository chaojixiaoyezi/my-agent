# LLM: 聊天 `/skills` 的执行与中文回执，TUI 与飞书等 IM 共用（都经 Gateway 控制通道）。只处理当前控制范围解析出的 owner：
#   提案走 SkillProposalService（确认/拒绝必须带用户看到的版本号，服务端在锁内复核），自动总结 Skill 走
#   skill_learning_report（与 CLI 同一事实推导与闸门）；列提案时调用自学习审核顺序点 skill_proposal_review_order（与 CLI
#   同一入口）。不构造 Agent、不暴露给模型；回执不含本机绝对路径。改动须同步 control_commands._skills_command、
#   command_catalog 的 skills 条目、TUI control_runtime 的文本还原与本地拒绝，以及 test_skill_chat_control.py。
#   using 仅通过 owner_conversation_store 和 method_carry 操作当前线程隐藏登记，不加载正文或冷 Agent。
# 模块用途: 在TUI和IM展示两类技能候选及准确确认命令，管理已发布Skill和本会话方法沿用。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature

from ..capability.decision_skill_proposal_review import skill_proposal_review_order
from ..capability.skill_learning_proposals import list_learning_proposals
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
# 提案短 ID 取前 8 个字符：消息里展示够辨识又不占地方。
_SHORT_ID_CHARS = 8
# 提案正文最多 3000 字符：超长截断，避免回执消息过大。
_BODY_MAX_CHARS = 3000
_EVENT_ROWS = 10
_STATUS_LABELS = {"pending_confirmation": "待确认", "committed": "已确认并安装", "rejected": "已拒绝"}
_STATE_LABELS = {"archived":"已归档（可restore）", "disabled":"用户已停用", "cooled":"已冷藏（仍可检索）", "active": "生效中", "user_modified": "已被你修改（不再自动更新）", "missing": "文件已不存在"}
_REVIEW_NOTE = "已按决策模型建议的审核顺序排列，仅供参考；确认或拒绝仍需你逐条执行。"
_FAILURE_HINTS = {
    CODE_REVISION_MISMATCH: "提案已有更新，版本号对不上；请先用 /skills show 查看最新内容和版本再操作。",
    CODE_NOT_PENDING: "这条提案已经不是待确认状态，无需再处理。",
    CODE_TARGET_EXISTS: "同名 Skill 已经存在，这条提案不能安装；可以用 /skills reject 拒绝它。",
}
# 会改提案或 Skill 的子命令：改动可能在异常前已经生效（例如删除后追加账本失败），回执不能承诺“没有改动”。
_WRITE_OPERATIONS = frozenset({"confirm", "reject", "learned_revert", "learned_remove", "using_remove", "learning_proposal", "learning_maintenance", "transfer"})
_READ_FAILURE = "技能提案或自动 Skill 暂时读不到，请稍后重试。"
_WRITE_UNCONFIRMED = "这次操作的结果没能完整确认，可能已经生效；请先发 /skills 或 /skills learned 查看当前状态，再决定是否重试。"


# LLM: 只携带已解析 owner 的路径投影与配置；也作审核顺序点的决策宿主（与 CLI 的 decision_host 同形状）。
# 类用途: 一次 /skills 控制解析出的 owner 路径与配置。
@dataclass(frozen=True)
class SkillControlHost:
    home_paths: object
    config: object
    capability_config_path: str = ""
    actor_id: str = ""


# LLM: 只表示可预期的用户输入问题（编号找不到或不唯一），消息直接给用户看，不含路径。
# 类用途: /skills 的可预期失败。
class SkillControlError(Exception):
    pass


# LLM: Gateway 控制分派入口；可预期失败保留原结构化失败回执。其它异常不泄露路径或堆栈，按子命令是否写入
#   选回执：只读子命令回“暂时读不到”；写子命令可能在改动生效后才失败（如 append_event 抛 OSError），只说结果没能完整确认、
#   请先查当前状态，不承诺“没有改动”或直接重试。using 按当前认证范围裁决原线程，联测 conversation_method_commands。
# 函数用途: 执行一条已解析的 /skills 控制并返回给用户的中文回执（确认、拒绝、回滚、删除会写文件）。
def execute_skill_control(base_agent: object, command: ConversationControlCommand, scope: object) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(_KIND, False, command.usage)
    try:
        if command.operation == "transfer":
            from ..capability.skill_transfer_control import execute_transfer_control
            return execute_transfer_control(base_agent,scope,command.value.split()[1:])
        if command.operation in {"using", "using_remove"}:
            return _execute_using_control(base_agent, command, scope)
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


# LLM: owner 裁决与 learned 同源，线程只取认证 scope 的通道绑定，忽略 metadata 里自称的线程；关掉开关不写账。
# 函数用途: 查看或移除当前 TUI/IM 会话的在用方法，不构造 Agent 或创建新线程。
def _execute_using_control(base_agent, command, scope) -> ConversationControlResult:
    from ..capability.method_carry import remove_conversation_method, render_conversation_methods
    from ..capability.self_install_switches import read_fresh_capability_switch
    from .owner_conversation_store import owner_conversation_store

    host = _scoped_host(base_agent, scope)
    if not read_fresh_capability_switch(host, "conversation_method_carry_enabled"):
        return ConversationControlResult(_KIND, True, "会话沿用已关闭")
    store = owner_conversation_store(base_agent, host.home_paths, initialize=False)
    thread = store.threads.resolve(channel=scope.channel, channel_conversation_id=scope.conversation_id,
                                   channel_user_id=scope.user_id)
    thread_id = thread.thread_id if thread else ""
    if command.operation == "using":
        return ConversationControlResult(_KIND, True, render_conversation_methods(store, thread_id))
    removed = remove_conversation_method(store, thread_id, command.value.split(" ", 2)[2])
    return ConversationControlResult(_KIND, removed, "已从本会话移除该方法。" if removed else
                                     "本会话没有唯一匹配的方法；请用 /skills using 列出的完整编号移除。")


# LLM: 与 /model 文字控制同一 owner 解析（resolve_gateway_scope_owner → owner 路径投影）；只读，不创建目录。
#   现读开关沿当前 owner 的原配置位置；不能以服务 cwd 或基础 owner 的路径替代其它用户配置。
# 函数用途: 按控制范围找到当前用户的 owner 路径与配置。
def _scoped_host(base_agent: object, scope: object) -> SkillControlHost:
    from ..capability.runtime_config_reload import (
        capability_config_path_for,
        default_capability_config_path,
    )

    owner = resolve_gateway_scope_owner(base_agent, scope)
    base_home = base_agent.home_paths
    home = home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))
    same_owner = all(getattr(base_home, key) == getattr(home, key) for key in ("owner_provider", "owner_kind", "owner_id"))
    path = capability_config_path_for(base_agent) if same_owner else default_capability_config_path(home.owner_home_dir)
    return SkillControlHost(home, base_agent.config, str(path),str(scope.user_id))


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


# LLM: 同owner原候选为唯一状态源；可选溢出沿公开读口一次读取，不回填；all仅改变状态过滤，不授确认权。
# 函数用途: 取得当前owner完整待确认或全部自动学习候选，兼容原列表接口。
def _automatic_proposals(host, *, include_all=False):
    options = {'include_overflow': True} if 'include_overflow' in signature(list_learning_proposals).parameters else {}
    rows = list_learning_proposals(SkillLearningStore.for_home(host.home_paths), **options)
    return [row for row in rows if include_all or row['status'] == 'pending']


# LLM: 只投影名称/修订/摘要及原控制命令，不展示或发布正文；历史候选仅给查看，不给无效确认命令。
# 函数用途: 生成自动学习候选可直接审阅操作的聊天列表。
def _automatic_proposal_lines(rows):
    if not rows:
        return []
    lines = ['', f'自动学习候选：{len(rows)} 条（确认前不写正式Skill）']
    labels = {'pending': '待确认', 'committed': '已确认并安装', 'rejected': '已拒绝'}
    for index, row in enumerate(rows, 1):
        identifier, revision = row['proposal_id'], row['revision']
        lines.append(f"{index}. {row['decision']['name']} 修订 {revision}［{labels.get(row['status'], row['status'])}］{row['decision']['description']}")
        commands = f'查看 /skills learned proposal-show {identifier}'
        if row['status'] == 'pending':
            commands += f'　确认 /skills learned confirm {identifier} {revision}　拒绝 /skills learned reject {identifier} {revision}'
        lines.append(f'   {commands}')
    return lines


# LLM: 原子代理提案格式保持；自动候选存在时直接列名称/修订/摘要和命令，候选为空时原输出逐字保持。
# 函数用途: /skills展示两类待审提案，再汇总已发布数量，不会自动确认。
def _overview(host: SkillControlHost, tokens: list[str]) -> str:
    drafts = _automatic_proposals(host)
    lines = _proposal_list_lines(host, status=PROPOSAL_PENDING, include_empty_notice=not drafts)
    lines += _automatic_proposal_lines(drafts)
    report = learned_skills_report(SkillLearningStore.for_home(host.home_paths), host.config)
    lines += ["", f"自动总结的 Skill：{report['count']} 个（发 /skills learned 查看）。"]
    lines.append(f"待确认学习候选：{len(drafts)}份（/skills learned proposals 查看，确认前不写正式Skill）。")
    return "\n".join(lines)


# LLM: 原提案与自动学习候选分别沿原服务读取，all包括历史；无自动候选时保持原格式。
# 函数用途: /skills proposals [all]展示两类待确认或全部提案及有效操作命令。
def _proposals(host: SkillControlHost, tokens: list[str]) -> str:
    include_all = tokens[1:] == ['all']
    drafts = _automatic_proposals(host, include_all=include_all)
    lines = _proposal_list_lines(host, status=None if include_all else PROPOSAL_PENDING, include_empty_notice=not drafts)
    return '\n'.join(lines + _automatic_proposal_lines(drafts))


# LLM: 原审核顺序点及格式不变；仅在另有自动候选时省略空提示，由结构化候选存在性决定，绝不解析文案。
# 函数用途: 生成原子代理提案列表及命令，避免有自动候选却称没有待办。
def _proposal_list_lines(host: SkillControlHost, *, status: str | None, include_empty_notice: bool = True) -> list[str]:
    service = _proposals_service(host)
    proposals = service.list(status=status)
    review = skill_proposal_review_order(host, service, proposals)
    ordered = list(proposals if review is None else review.proposals)
    labels = {} if review is None else {entry.proposal_id: entry.label for entry in review.entries}
    switch = "开启" if getattr(host.config, "enable_self_learning", False) else "关闭"
    title = "待确认的技能提案" if status == PROPOSAL_PENDING else "技能提案"
    lines = [f"{title}：{len(ordered)} 条（自学习：{switch}）"]
    if not ordered:
        return lines + (["没有需要你处理的提案。"] if include_empty_notice else [])
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
    ref = proposal.proposal_id[:_SHORT_ID_CHARS]
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
    return _outcome_text(service.confirm(proposal.proposal_id, int(tokens[2]),actor=host.actor_id or "user"), proposal)


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
    return f"已拒绝提案 {proposal.proposal_id[:_SHORT_ID_CHARS]}（{proposal.target.skill_name}）。"


# LLM: 事实来自 skill_learning_report（与 CLI 同一推导）；只读展示含溢出请求与有效使用次数。
# 函数用途: /skills learned —— 列出自动总结的 Skill，每个附上查看、回滚、删除命令。
def _learned(host: SkillControlHost, tokens: list[str]) -> str:
    report = learned_skills_report(SkillLearningStore.for_home(host.home_paths), host.config)
    switch = "开启" if report["self_learning_enabled"] else "关闭"
    limit = report["daily_limit"] or "不限"
    overflow = int(report.get("overflow_requests") or 0)
    usage = report.get("skill_usage") if isinstance(report.get("skill_usage"), dict) else {}
    effective = int(usage.get("effective_uses") or 0)
    lines = [f"自动总结的 Skill：{report['count']} 个（自学习：{switch}；待处理请求 {report['pending_requests']} 条，溢出 {overflow} 条；"
             f"今日总结 {report['calls_today']}/{limit} 次；有效使用 {effective} 次）"]
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
        f"正文hash：{item['sha256']}",
        "最近事件：",
    ]
    for observation in item.get('maintenance',{}).get('observations',[])[-3:]:
        if observation.get('authority')=='model':
            lines.append(f"采用任务：{observation['task_id']}；有效反馈：/skills learned feedback {item['name']} {observation['sha256']} effective {observation['task_id']}")
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



# LLM: 此入口只收已认证用户control，展示包括归档pending；确认不暴露给模型工具，原owner和revision不变。
# 函数用途: 让普通/群IM用户查看并确认或拒绝自己的学习候选，不依赖管理员CLI。
def _learning_proposal(host, tokens):
    from ..capability.skill_learning_proposals import (
        learning_proposal,
        list_learning_proposals,
        resolve_learning_proposal,
    )
    store = SkillLearningStore.for_home(host.home_paths)
    action = tokens[1]
    if action == 'proposals':
        rows = [row for row in list_learning_proposals(store,include_overflow=True) if row['status']=='pending']
        return '\n'.join(f"{row['proposal_id']} 修订{row['revision']}：{row['decision']['name']}；查看 /skills learned proposal-show {row['proposal_id']}" for row in rows) or '没有待确认学习候选。'
    row = learning_proposal(store,tokens[2])
    if action == 'proposal-show':
        return f"{row['decision']['name']} 修订{row['revision']}\n来源任务：{row['request'].get('task_id','')}；运行：{row['request'].get('run_id','')}\n原因：{row['decision']['reason']}\n适用场景：{row['decision']['when_to_use']}\n{row['decision']['description']}\n{row['decision']['body'][:3000]}\n确认：/skills learned confirm {row['proposal_id']} {row['revision']}\n拒绝：/skills learned reject {row['proposal_id']} {row['revision']}"
    result = resolve_learning_proposal(store,tokens[2],int(tokens[3]),decision=action,guard_config=host.config,actor_id=host.actor_id)
    return '学习候选已确认并安装。' if result['status']=='committed' else '已拒绝学习候选。'


# LLM: 固定/停用/恢复/反馈属于用户显式control；不解析聊天成功失败，不把模型采用声明变成人类确认。
# 函数用途: 执行原owner的维护动作，反馈绑定用户给出的准确正文hash与任务编号。
def _learning_maintenance(host,tokens):
    from ..capability.skill_learning_maintenance import (
        LearningObservationSource,
        control_learning,
        observe_learning,
    )
    store = SkillLearningStore.for_home(host.home_paths)
    if tokens[1]=='feedback':
        result = observe_learning(store,tokens[2],sha256=tokens[3],outcome=tokens[4],task_id=tokens[5],source=LearningObservationSource(authority='human',actor_id=host.actor_id,receipt_id='user-control:'+tokens[5]+':'+tokens[3]))
        return f"已登记该版本反馈：{result['quality']}。"
    control_learning(store,tokens[2],operation=tokens[1])
    return '已更新该Skill维护状态。'

_HANDLERS: dict[str, Callable[[SkillControlHost, list[str]], str]] = {
    "learning_proposal": _learning_proposal,
    "learning_maintenance": _learning_maintenance,
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
