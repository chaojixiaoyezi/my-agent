# LLM: learnpack 给用户的宿主提示（conversation/host_notices 唯一存取），两种：
#   1) 开关关着她开了安装单：宿主把现在所有还有效的确认行（learnpack_service.open_orders，最新的在前）排成本轮提示，注明以这条为准
#      （2026-10-07 生产复测：她把早已执行过的单又交给用户）；新单和她做的已装能力包同领域时说怎么合并。来源 learnpack:orders，
#      新的替换旧的。
#   2) 她自己装上一个包（对应的自动装开关开着）时，宿主把
#   "装了什么、开关开着所以没问你、和她做的哪些已装能力包同领域、怎么删、怎么关掉自动装"排成本轮提示（queue_turn_host_notice），
#   随这一轮回复直接显示给用户（TUI 灰色系统行、飞书【提示】行），不经她转告——2026-10-07 生产复测：回执里的 user_notice 她不转告，
#   开关开着时用户可能不知道她陆续装了哪些包。文字只来自结构化事实（包名、版本、种类、安装状态、下一步命令、同领域判断），不读模型
#   回答。来源按包名分（learnpack:<包名>），同一个包只留最新一条。没有会话编号（比如直接调工具）时什么都不排。
#   改文案或字段要同步 test_learnpack_notices 与 learn-external-agent 技能。
# 模块用途: 她开了安装单或自己装了包时，由宿主直接告诉用户该发哪一行、装了什么。
from __future__ import annotations

from ..conversation.host_notices import queue_turn_host_notice
from .learnpack_domain import SAME_DOMAIN_LIST_LIMIT_COUNT, SameDomainPack
from .learnpack_installer import STATE_ENABLED, STATE_NEEDS_USER, InstallOutcome
from .learnpack_store import BuildRecord
from .package_build import KIND_CAPABILITY_PACK
from .self_install_switches import PACK_SELF_INSTALL_KEY, PLUGIN_SELF_INSTALL_KEY, switch_command

# 提示来源前缀：learnpack:<包名>，同一个包只留最新一条提示。
NOTICE_SOURCE_PREFIX = "learnpack:"
# 她自己装上并启用了。
NOTICE_CODE_SELF_INSTALLED = "self_installed"
# 她自己装上了，但启用还要用户确认。
NOTICE_CODE_SELF_INSTALLED_NEEDS_USER = "self_installed_needs_user"
# 安装单提示的来源：同一会话只留最新一条（列的是当时全部还有效的单）。
ORDERS_NOTICE_SOURCE = "learnpack:orders"
# 开了安装单、还有单在等用户确认。
NOTICE_CODE_ORDERS_OPEN = "orders_open"
# 安装单提示里最多列出的确认行条数。
ORDER_LIST_LIMIT_COUNT = 3


# LLM: 只在自己装的结果是"已启用"或"停在用户确认"时排提示（失败、被拒不排）；会话与本轮请求编号由 queue_turn_host_notice 从
#   本轮结构化属性取。返回是否排上。副作用：改写会话线程记录。
# 函数用途: 她自己装上包以后，排一条给用户看的宿主提示。
def queue_self_install_notice(agent: object, record: BuildRecord, outcome: InstallOutcome,
                              domain: list[SameDomainPack]) -> bool:
    if outcome.state not in {STATE_ENABLED, STATE_NEEDS_USER}:
        return False
    code = NOTICE_CODE_SELF_INSTALLED if outcome.state == STATE_ENABLED else NOTICE_CODE_SELF_INSTALLED_NEEDS_USER
    details = {"package_id": record.package_id, "version": record.version, "kind": record.kind,
               "sha256": record.sha256[:12], "same_domain_count": len(domain)}
    notice = queue_turn_host_notice(agent, NOTICE_SOURCE_PREFIX + record.package_id, code,
                                    self_install_text(record, outcome, domain), details=details)
    return notice is not None


# LLM: rows 是调用方按 open_orders 算好的结构化行（order_id、package_id、version、confirm_line），刚开的单在第一行。会话与本轮
#   请求编号由 queue_turn_host_notice 从本轮结构化属性取。返回是否排上。副作用：改写会话线程记录。
# 函数用途: 她开了安装单以后，把现在全部有效的确认行排成一条给用户看的宿主提示。
def queue_orders_notice(agent: object, record: BuildRecord, rows: list[dict[str, object]],
                        domain: list[SameDomainPack]) -> bool:
    if not rows:
        return False
    details = {"package_id": record.package_id, "order_ids": ",".join(str(row["order_id"]) for row in rows),
               "open_count": len(rows), "same_domain_count": len(domain)}
    notice = queue_turn_host_notice(agent, ORDERS_NOTICE_SOURCE, NOTICE_CODE_ORDERS_OPEN,
                                    orders_text(record, rows, domain), details=details)
    return notice is not None


# LLM: 纯函数；先说刚做好的是哪个包、还没装，再列确认行（最多 ORDER_LIST_LIMIT_COUNT 行，多出的只报个数），最后说同领域怎么合并。
# 函数用途: 拼"等你确认的安装单"的提示正文。
def orders_text(record: BuildRecord, rows: list[dict[str, object]], domain: list[SameDomainPack]) -> str:
    kind = "能力包" if record.kind == KIND_CAPABILITY_PACK else "插件"
    shown = "；".join(f"{row['package_id']} {row['version']}：{row['confirm_line']}" for row in rows[:ORDER_LIST_LIMIT_COUNT])
    more = f"（还有 {len(rows) - ORDER_LIST_LIMIT_COUNT} 张更早的单没列出）" if len(rows) > ORDER_LIST_LIMIT_COUNT else ""
    text = (f"她做好了{kind} {record.package_id} {record.version}，还没装。等你确认的安装单以这条为准（她以前给过的其它行可能"
            f"已经执行过或作废了，每行只执行一次）：{shown}{more}。")
    if domain:
        names = "、".join(f"{item.package_id} {item.version}" for item in domain[:SAME_DOMAIN_LIST_LIMIT_COUNT])
        text += f"新做的 {record.package_id} 和她做的 {names} 同领域，想合并就别发它那行，跟她说“把 {record.package_id} 并进 {domain[0].package_id}”。"
    return text


# LLM: 纯函数；先说装了什么（最要紧，提示超长截断时保留），再说同领域、怎么删、怎么关自动装。能力包用 /plugins# 写法，插件用
#   /plugins 写法；停在用户确认时带上宿主给的确认命令原文。
# 函数用途: 拼"她自己装上了哪个包"的提示正文。
def self_install_text(record: BuildRecord, outcome: InstallOutcome, domain: list[SameDomainPack]) -> str:
    pack = record.kind == KIND_CAPABILITY_PACK
    kind = "能力包" if pack else "插件"
    head = f"她自己装上了{kind} {record.package_id} {record.version}（{kind}自动装开关开着，所以没问你）"
    if outcome.state == STATE_NEEDS_USER:
        head += f"，还没启用：启用要你确认，确认就发 {outcome.next_command}"
    parts = [head + "。"]
    if domain:
        names = "、".join(f"{item.package_id} {item.version}" for item in domain[:SAME_DOMAIN_LIST_LIMIT_COUNT])
        parts.append(f"它和她做的 {names} 同领域，想合并就跟她说“把 {record.package_id} 并进 {domain[0].package_id}”。")
    remove = f"/plugins#{record.package_id} 删除" if pack else f"/plugins remove {record.package_id}"
    parts.append(f"要删就发 {remove}；{'/plugins#' if pack else '/plugins list'} 能看到全部。")
    parts.append(f"不想让她自己装，发 {switch_command(PACK_SELF_INSTALL_KEY if pack else PLUGIN_SELF_INSTALL_KEY, False)}")
    return "".join(parts)


__all__ = ["NOTICE_CODE_ORDERS_OPEN", "NOTICE_CODE_SELF_INSTALLED", "NOTICE_CODE_SELF_INSTALLED_NEEDS_USER",
           "NOTICE_SOURCE_PREFIX", "ORDERS_NOTICE_SOURCE", "orders_text", "queue_orders_notice",
           "queue_self_install_notice", "self_install_text"]
