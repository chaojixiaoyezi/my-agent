# LLM: 能力包命令 /plugins#[包名] [查看|启用|停用|删除|退回|安装 <单号>] 的唯一处理处（TUI、IM 同一入口：PluginManagement.command
#   → learnpack_command 按 plugin_namespace 的 kind=pack 分流到这里）。列表与查看只读安装表与 learnpack 存储；启用/停用/删除转成
#   管理员同一条 /plugins enable|disable|remove 命令（沿用用户这次的请求编号，回执由宿主同一套排版给出，只把说明里的称呼换成
#   "能力包"）；"退回 [版本摘要]" 走
#   learnpack_service.revert_install（与 /plugins revert 同一实现，先预览、带版本才执行）；"安装 <单号>" 核对单子上的包名后走
#   confirm_order（与 /plugins confirm 同一实现）。她声明的来源与许可证只在单独一行里展示，并标明是她的原文、宿主未核实。动作词是结构化枚举（中文为主、英文同义），不做自然语言理解。改动作要同步 command_catalog 帮助、
#   plugin_completion 补全与 test_pack_commands。
# 模块用途: 让用户用 /plugins# 一套命令管理能力包，并标出哪些是 my-agent 自己做的。
from __future__ import annotations

import uuid

from ..command_arguments import CommandArgumentError, lex_command_arguments
from .learnpack_service import confirm_order, revert_install
from .learnpack_store import ORDER_ID_PATTERN, LearnpackStore
from .package_build import KIND_CAPABILITY_PACK

# 动作词（中文为主，英文同义）→ 规范动作名；不在表里的词按未知动作拒绝。
PACK_ACTIONS = {"查看": "view", "view": "view", "info": "view",
                "启用": "enable", "enable": "enable", "停用": "disable", "disable": "disable",
                "删除": "remove", "remove": "remove", "退回": "revert", "revert": "revert",
                "安装": "install", "install": "install"}
# 补全与帮助用的中文动作词与一句说明，顺序即展示顺序。
PACK_ACTION_WORDS = (("查看", "看详情和可用命令"), ("启用", "启用这个能力包"), ("停用", "停用但保留"),
                     ("删除", "删掉这个能力包"), ("退回", "退回她做的上一版"), ("安装", "确认安装单，后面跟单号"))
# 会改动安装状态的动作（只读的列表/查看以外都要管理员）。
_ADMIN_ACTIONS = frozenset({"enable", "disable", "remove", "revert", "install"})
_USAGE = "用法：/plugins#[包名] [查看|启用|停用|删除|退回 [版本摘要]|安装 <单号>]；不带包名列出全部能力包。"
# 每个动作允许的参数个数（最少, 最多）：安装要一个单号；退回可带一个版本摘要；其它不带参数。
_ARGUMENT_RANGE = {"install": (1, 1), "revert": (0, 1)}


# LLM: namespace 来自 plugin_namespace（kind=pack）；request_id、revision 是用户这次命令的请求编号与目录版本，启用/停用/删除转成
#   宿主命令时原样沿用：同一编号重发时宿主按原请求回放，目录已变时按宿主同一规则提示重新查看（和直接敲 /plugins 命令一样）。
#   返回原始回执字典：learnpack 自己的结果交给 PluginManagement._reply 排版；转发给宿主的命令把宿主已排好的最终回执装在
#   {"forwarded_reply": …} 里，由插件管理原样取出（按这个结构化标记判断，不看回执里有没有 kind，复审建议）。
# 函数用途: 执行一条能力包命令。
def run_pack_command(manager: object, namespace: object, request_id: str = "", revision: str = "") -> dict[str, object]:
    try:
        action, argument = _parse(namespace.body)
    except CommandArgumentError as exc:
        return _rejected("INVALID_COMMAND_ARGUMENTS", f"{exc}\n{_USAGE}")
    if not namespace.plugin_id:
        if namespace.body.strip():
            return _rejected("INVALID_COMMAND_ARGUMENTS", f"不带包名只能列出全部能力包。\n{_USAGE}")
        return {"ok": True, "message": render_pack_list(manager)}
    if action in _ADMIN_ACTIONS and not manager.context.is_admin:
        return _rejected("PLUGIN_PERMISSION_DENIED", "只有管理员能启用、停用、删除、退回或安装能力包。")
    if action == "install":
        return _install_order(manager, namespace.plugin_id, argument)
    entry = _pack_entry(manager, namespace.plugin_id)
    if entry is None:
        return _rejected("PACK_NOT_FOUND", f"没有装 {namespace.plugin_id} 这个能力包；不带包名发 /plugins# 看全部。")
    if action == "view":
        return {"ok": True, "message": render_pack_view(manager, entry)}
    if action == "revert":
        name = entry.manifest.plugin_id
        return revert_install(manager, name, argument, f"/plugins#{name} 退回")
    text = f"/plugins {action} {entry.manifest.plugin_id}"
    return {"forwarded_reply": manager.command(text, revision=revision or manager.catalog().revision,
                                               request_id=request_id or f"lp-{uuid.uuid4().hex}", subject="能力包")}


# LLM: 第一个词是动作（缺省为查看）；"安装"必须带一个单号，"退回"可带一个版本摘要，其余动作不带参数，多余参数一律拒绝。纯函数。
# 函数用途: 把命令正文拆成规范动作与参数。
def _parse(body: str) -> tuple[str, str]:
    tokens = [token.value for token in lex_command_arguments(body)]
    if not tokens:
        return "view", ""
    action = PACK_ACTIONS.get(tokens[0].lower())
    if action is None:
        raise CommandArgumentError("unknown_action", f"能力包没有“{tokens[0]}”这个动作。")
    low, high = _ARGUMENT_RANGE.get(action, (0, 0))
    if not low <= len(tokens) - 1 <= high:
        raise CommandArgumentError("invalid_arguments", "“安装”后面跟一个单号，“退回”可以跟一个版本摘要，其它动作不带参数。")
    return action, tokens[1] if len(tokens) > 1 else ""


# LLM: 只认安装表里的纯内容包；同名插件不当能力包。只读。
# 函数用途: 找到某个已装能力包的安装记录。
def _pack_entry(manager: object, package_id: str):
    return next((row for row in manager.installations.snapshot()
                 if row.manifest.plugin_id == package_id and row.manifest.is_content_only), None)


# LLM: 单号格式和单子归属都要对上（单子上的包名必须就是命令里的包名，而且是能力包的单；插件的单只认 /plugins confirm，
#   复审建议），才交给同一个确认单入口。只读核对后执行。
# 函数用途: 执行"/plugins#<包名> 安装 <单号>"。
def _install_order(manager: object, package_id: str, order_id: str) -> dict[str, object]:
    if ORDER_ID_PATTERN.fullmatch(order_id) is None:
        return _rejected("INVALID_COMMAND_ARGUMENTS", "单号格式不对，请照她给的那一行原样发送。")
    order = LearnpackStore(manager.context.owner.home_dir).order(order_id)
    if order is not None and order.package_id != package_id:
        return _rejected("PACKAGE_INSTALL_ORDER_NOT_FOUND", f"单号 {order_id} 不是 {package_id} 的安装单。")
    if order is not None and order.kind != KIND_CAPABILITY_PACK:
        return _rejected("PACKAGE_INSTALL_ORDER_NOT_FOUND", f"单号 {order_id} 是插件的安装单，请发 /plugins confirm {order_id}。")
    return confirm_order(manager, order_id)


# LLM: "她做的"只按结构化事实判断：当前装的字节摘要在 learnpack 存储里有打包记录。只读。
# 函数用途: 列出全部已装能力包（版本、启用状态、是否她做的、来源与许可证）。
def render_pack_list(manager: object) -> str:
    store = LearnpackStore(manager.context.owner.home_dir)
    rows = [row for row in manager.installations.snapshot() if row.manifest.is_content_only]
    if not rows:
        return "当前没有装能力包。她学完外部 agent 做好能力包后，会给你装包的那一行命令。"
    lines = ["已装能力包（/plugins#<包名> 查看详情）："]
    for row in sorted(rows, key=lambda item: item.manifest.plugin_id):
        record = store.build(row.package_sha256)
        state = ("启用" if row.enabled else "停用") + ("，她做的" if record is not None else "")
        lines.append(f"- {row.manifest.plugin_id} {row.manifest.version}（{state}）")
        if record is not None:
            lines.append(f"  {_declared_line(record)}")
    return "\n".join(lines)


# LLM: 只读安装表与 learnpack 存储；管理命令原文按 /plugins# 写，用户照抄即可。
# 函数用途: 展示一个能力包的详情和可用的管理命令。
def render_pack_view(manager: object, entry) -> str:
    manifest, capability = entry.manifest, entry.manifest.capability
    record = LearnpackStore(manager.context.owner.home_dir).build(entry.package_sha256)
    lines = [f"{manifest.plugin_id} {manifest.version}（{'启用' if entry.enabled else '停用'}）：{manifest.summary}",
             f"适用：{capability.description}", f"关键词：{'、'.join(capability.keywords) or '（无）'}",
             f"带检查程序：{'是' if _runs_checkers(capability) else '否'}；指纹 {entry.package_sha256[:12]}"]
    if record is not None:
        lines.extend([f"她做的（打包于 {record.built_at}）", _declared_line(record)])
    name = manifest.plugin_id
    lines.append(f"管理：/plugins#{name} 启用｜/plugins#{name} 停用｜/plugins#{name} 删除｜/plugins#{name} 退回")
    return "\n".join(lines)


# LLM: 与启用确认、package_runs_programs 同一口径：声明了要运行包内代码的检查程序才算（只有交付物规则不算）。纯函数。
# 函数用途: 判断能力包带不带要运行的检查程序。
def _runs_checkers(capability) -> bool:
    verification = getattr(capability, "verification", None)
    return verification is not None and bool(verification.runs_package_code)


# LLM: 她声明的来源与许可证单独成行、标明是她的原文且宿主未核实（打包时已挡住换行、控制与格式字符，出不了这一行）。纯函数。
# 函数用途: 生成"她声明的来源与许可证"那一行。
def _declared_line(record) -> str:
    return f"她声明的来源与许可证（原文，宿主未核实）：{record.origin}；{record.license}"


# LLM: 只组装字典；用于没有执行任何宿主命令的拒绝。纯函数。
# 函数用途: 生成拒绝回执。
def _rejected(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "state": "rejected", "error_code": code, "message": message}


__all__ = ["PACK_ACTIONS", "PACK_ACTION_WORDS", "render_pack_list", "render_pack_view", "run_pack_command"]
