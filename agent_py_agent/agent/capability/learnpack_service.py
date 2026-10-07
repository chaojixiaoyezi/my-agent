# LLM: learnpack 的三条安装入口共用这里：开关开着时模型工具 package_install 走 install_now（宿主用专用 learnpack 线程身份执行）；
#   用户发回确认行 /plugins confirm <单号>（能力包也可以 /plugins#<包名> 安装 <单号>）时走 confirm_order，发 /plugins revert <包名>
#   （能力包也可以 /plugins#<包名> 退回）时走 revert_install（都用用户当前会话身份执行）；/plugins# 整段由 pack_commands 处理。三条都调 learnpack_installer.run_install，都只装 learnpack 存储里她自己打的、摘要和清单都对得上的包，都先查插件功能与
#   管理动作（_management_problem），都在 installs.jsonl 记账（含许可来源）。包名归属由 ownership_problem 裁决，并经
#   _InstallHooks.guard 在安装驱动读安装表的同一次快照上再判一次：同名（含只差大小写）但不是她打的已装包不替换（复审 M2）；
#   没装着、却留着别处同名插件的私有数据目录（她从没装过这个包名）也不装（卸载不删数据目录）。"她装过的包名"只在她的包进了
#   安装表之后、启用之前记。确认单抢执行权前先查前置条件（复审 S1），被同一个包后来的单或安装取代的旧单不执行（只有最新的
#   那一行有效）；每条宿主命令发出前先记单子进度，第二次确认
#   回放结果与请求编号（复审 M3）。身份与开关由调用方裁决：install_now 的调用方必须已确认本机管理员且该自动装；confirm_order、
#   revert_install 在这里再查一次管理员。改线程身份要同步 test_learnpack_install；不改 PluginManagement 的执行链。
# 模块用途: 把"装她打的包"接到宿主现成的插件管理命令链上，并保证待确认安装单只执行一次、结果如实可查。
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, replace

from ..plugin_package import inspect_plugin_package
from .learnpack_domain import installed_notice
from .learnpack_installer import (
    STATE_ENABLED,
    STATE_FAILED,
    STATE_NEEDS_USER,
    STATE_UNKNOWN,
    InstallConsent,
    InstallOutcome,
    InstallRequest,
    run_install,
    version_label,
)
from .learnpack_store import BuildRecord, InstallOrder, LearnpackStore
from .package_build import KIND_CAPABILITY_PACK

# 开关开着时宿主自动装包所用的专用会话身份：账记在单独线程，不改用户"最近会话"。
LEARNPACK_ACTOR = "learnpack"
LEARNPACK_CHANNEL = "learnpack"
LEARNPACK_CONVERSATION = "self-install"
# 包名被别处同名包占着、什么都没做（不写安装记录）。
STATE_ID_TAKEN = "refused_id_taken"
# 插件功能关着或管理动作被策略禁用、什么都没做（不写安装记录）。
STATE_BLOCKED = "refused_management_unavailable"
# 什么都没做就被拒的结果：不写安装记录、不算发过宿主命令。
REFUSED_STATES = frozenset({STATE_ID_TAKEN, STATE_BLOCKED})
# 算作"装上过"的安装结果（进过安装表）；失败、结果未确认的不算退回目标。
_INSTALLED_STATES = frozenset({STATE_ENABLED, STATE_NEEDS_USER})
# 退回命令里的版本摘要：照预览原样填的十六进制前缀，至少 8 位。
_SHA_PREFIX = re.compile(r"[0-9a-f]{8,64}")
# 回放已执行单时给人看的结果说法。
_STATE_LABELS = {STATE_ENABLED: "已装上并启用", STATE_NEEDS_USER: "已装上，还等你确认启用", STATE_FAILED: "没有成功",
                 STATE_UNKNOWN: "结果没法确认（可能已部分执行）", STATE_ID_TAKEN: "没有安装（包名被别处的同名包占着）"}


# LLM: 与 TUI 直连组装插件管理同一取法：agent 带的 owner 结构化身份优先，没有就按配置算。只读。
# 函数用途: 找到当前 owner 的 home（安装表、插件数据目录都在它下面）。
def learnpack_owner(agent: object) -> object:
    from ..user_space.owner_resolver import owner_identity_from_config, resolve_owner_home

    owner = getattr(agent, "owner_identity", None) or owner_identity_from_config(agent.config)
    return resolve_owner_home(agent.home_paths.root, owner)


# LLM: 与 TUI 直连组装插件管理服务同一做法（plugin_management_context + 当前 owner 线程库），只是会话身份换成专用 learnpack；
#   管理员资格按 owner 结构化身份算，不看文本。只组装，不执行命令。
# 函数用途: 为"开关开着时自动装"组装一个管理员插件管理服务。
def learnpack_manager(agent: object) -> object:
    from ..plugin_management import PluginManagement, plugin_management_context
    from ..user_space.owner_access import is_complete_local_admin_owner

    home = agent.home_paths
    context = plugin_management_context(
        learnpack_owner(agent), home, agent.config, agent.conversation_store.threads,
        actor_id=LEARNPACK_ACTOR, channel=LEARNPACK_CHANNEL, conversation_id=LEARNPACK_CONVERSATION,
        is_admin=is_complete_local_admin_owner(home),
    )
    return PluginManagement(replace(context, live_registry=getattr(agent, "tools", None)))


# LLM: entries 是调用方读到的一次安装表快照（tuple）；按包名精确匹配。纯函数。
# 函数用途: 取现在装着的同名包（没装返回 None）。
def installed_entry(entries: tuple, package_id: str) -> object | None:
    return next((row for row in entries if row.manifest.plugin_id == package_id), None)


# LLM: 包名归属裁决，entries 必须来自调用方同一次读到的整张安装表快照。装着的同名包不是她打的（learnpack 存储里没有它的字节
#   记录），或装着只差大小写的同名包（不分大小写的文件系统上会共用插件数据目录）→ 拒绝，不替换、不继承（复审 M2）；没装着、
#   但这个包名的插件私有数据目录还在（查不了也算在）而她从没装过这个包名 → 拒绝（别处同名插件卸载后留下的，不能让她的包
#   接着用）。只读。返回 None 表示可以装。
# 函数用途: 判断这个包名能不能给她的包用。
def ownership_problem(store: LearnpackStore, owner: object, record: BuildRecord, entries: tuple) -> InstallOutcome | None:
    package_id = record.package_id
    clash = next((row for row in entries if row.manifest.plugin_id.casefold() == package_id.casefold()
                  and (row.manifest.plugin_id != package_id or store.build(row.package_sha256) is None)), None)
    if clash is not None:
        reason = f"已经装着一个同名的 {clash.manifest.plugin_id}（不是她做的，或包名只差大小写），不能用她的包替换它"
    elif installed_entry(entries, package_id) is not None or store.owns_id(package_id) or not _leftover_data(owner, package_id):
        return None
    else:
        reason = f"包名 {package_id} 下还留着别处同名插件卸载后的数据，她的包不能接着用"
    return InstallOutcome(STATE_ID_TAKEN, package_id, f"{reason}；请让她换个包名重新打包。",
                          error_code="PACKAGE_INSTALL_ID_TAKEN")


# LLM: 只有"确实不存在"（FileNotFoundError）才算没有残留；权限等其它任何错误都按有残留处理，出错时偏向拒绝（复审建议）。
#   文件系统分不分大小写由宿主平台决定，这里按实际路径查。只读。
# 函数用途: 判断这个包名的插件私有数据目录是不是还在。
def _leftover_data(owner: object, package_id: str) -> bool:
    from ..plugin_runtime import plugin_data_dir

    try:
        os.lstat(plugin_data_dir(owner, package_id))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


# LLM: 安装驱动的三个回调绑定同一个 owner、存储和包：guard 在驱动读安装表的同一次快照上做包名归属裁决（只读）；note_step 在
#   每条宿主命令发出前记单子进度（有单号时）；mark_owned 在她的包进了安装表之后、启用之前记"这个包名她装过"（幂等）。
#   写不进去都抛 OSError，驱动据此不发命令或不启用。
# 类用途: 把包名归属检查、进度与归属落盘接到一次安装上。
@dataclass(frozen=True)
class _InstallHooks:
    store: LearnpackStore
    owner: object
    record: BuildRecord
    order_id: str = ""

    # LLM: 见类注释。只读。
    # 函数用途: 安装前的包名归属检查。
    def guard(self, entries: tuple) -> InstallOutcome | None:
        return ownership_problem(self.store, self.owner, self.record, entries)

    # LLM: 见类注释。有写文件副作用。
    # 函数用途: 记下即将发出的一条宿主命令。
    def note_step(self, request_id: str, text: str) -> None:
        if self.order_id:
            self.store.note_order_step(self.order_id, request_id, text)

    # LLM: 见类注释。有写文件副作用。
    # 函数用途: 记下这个包名现在装的是她的包。
    def mark_owned(self) -> None:
        self.store.remember_owned_id(self.record.package_id)

    # LLM: 包路径只取宿主存储里的。纯组装。
    # 函数用途: 生成交给安装驱动的请求。
    def request(self, consent: InstallConsent) -> InstallRequest:
        return InstallRequest(self.record, str(self.store.build_path(self.record.sha256)), consent,
                              self.note_step, self.guard, self.mark_owned)


# LLM: 调用方已确认该自动装；consent_source 记许可来源（开关现读值与配置版本），写进安装记录。插件功能关着、管理动作被禁用、
#   包名被占时什么都不做、不记账（复审建议：先查再发命令）。有副作用：经宿主命令安装、启用，并写包名归属与安装记录。
# 函数用途: 开关开着时立刻把她打的包装上。
def install_now(agent: object, record: BuildRecord, consent: InstallConsent, consent_source: dict) -> InstallOutcome:
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    manager = learnpack_manager(agent)
    blocked = _management_problem(manager)
    if blocked is not None:
        return InstallOutcome(STATE_BLOCKED, record.package_id, blocked[1], error_code=blocked[0])
    outcome = run_install(manager, _InstallHooks(store, manager.context.owner, record).request(consent))
    if outcome.state not in REFUSED_STATES:
        store.record_install(_install_entry(record, outcome, {"via": "self", **consent_source}))
    return outcome


# LLM: 用户发回的确认行就是同意这一张单（包括运行她自己做的程序）；联网与读写目录授权仍不代给（run_install 白名单保证）。
#   顺序：管理员 → 单号与包 → 已执行过就回放 → 前置条件（插件功能、管理动作、包名归属、过期单）→ 抢执行权 → 执行；
#   前面任何一步不过都不占单。返回 PluginManagement 回执用的原始字典。有副作用：经宿主命令安装、启用，写单子进度、结果与安装记录。
# 函数用途: 执行用户确认的一张待安装单。
def confirm_order(manager: object, order_id: str) -> dict[str, object]:
    if not manager.context.is_admin:
        return _rejected("PLUGIN_PERMISSION_DENIED", "只有管理员能确认安装单。")
    store = LearnpackStore(manager.context.owner.home_dir)
    order = store.order(order_id)
    record = store.build(order.sha256) if order is not None else None
    if order is None or record is None:
        return _rejected("PACKAGE_INSTALL_ORDER_NOT_FOUND", f"没有找到可执行的安装单 {order_id}（单号不对，或包已被改动）。")
    previous = store.order_outcome(order_id)
    if previous is not None:
        return _already_done(order, previous)
    hooks = _InstallHooks(store, manager.context.owner, record, order_id)
    blocked = _precondition_problem(manager, hooks, order)
    if blocked is not None:
        return blocked
    if not store.claim_order(order_id):
        return _already_done(order, store.order_outcome(order_id) or {})
    outcome = run_install(manager, hooks.request(InstallConsent(True, True)))
    store.finish_order(order_id, {"install_state": outcome.state, "message": outcome.message,
                                  "next_command": outcome.next_command, "request_ids": list(outcome.request_ids),
                                  "error_code": outcome.error_code})
    if outcome.state not in REFUSED_STATES:
        store.record_install(_install_entry(record, outcome, {"via": "order", "order_id": order_id}))
    notice = installed_notice(store, manager.context.owner, record) if outcome.state == STATE_ENABLED else ""
    return outcome_reply(outcome, notice)


# LLM: 不占单的前置检查（复审 S1 与过期单）：插件功能与管理动作同 _management_problem；包名归属同 ownership_problem；
#   被同一个包后来的单或安装取代的旧单不执行（_superseded_by），回执写出是被哪一版取代的。只读。返回 None 表示可以执行。
# 函数用途: 确认单执行前检查会不会白白占掉单子。
def _precondition_problem(manager: object, hooks: _InstallHooks, order: InstallOrder):
    blocked = _management_problem(manager)
    if blocked is not None:
        return _rejected(*blocked)
    refused = hooks.guard(tuple(manager.installations.snapshot()))
    if refused is not None:
        return _rejected(refused.error_code, refused.message)
    replaced = _superseded_by(hooks.store, order)
    if replaced:
        return _rejected("PACKAGE_INSTALL_ORDER_STALE",
                         f"这张单（{order.package_id} {order.version}）开出之后，这个包又出过新单或换过版本（包括退回），"
                         f"最近一次是 {replaced}；旧单不再执行，请让她重新出一张单。")
    return None


# LLM: 插件功能开关与管理动作可用性和 /plugins 目录同一口径，错误码与宿主一致（功能关着 PLUGIN_DISABLED，安装/更新/启用/停用
#   任一被策略禁用 TOOL_DISABLED）。只读。返回 (错误码, 说明)，None 表示可以执行。
# 函数用途: 检查现在能不能完整执行一串插件管理命令。
def _management_problem(manager: object) -> tuple[str, str] | None:
    from ..plugin_management import management_ready

    if not manager.context.enabled:
        return "PLUGIN_DISABLED", "插件功能现在没开，什么都没做；打开后再来。"
    if not management_ready(manager):
        return "TOOL_DISABLED", "安装、启用这些插件管理动作现在被策略禁用，什么都没做；放开后再来。"
    return None


# LLM: 结构化判据：这张单开出之后（序号严格更大），同一个包又出过别的字节的单，或 learnpack 装过、退回过别的字节（任何一条
#   安装记录，不论停在哪一步），这张单就被取代了。只有最新的那一行有效：旧行不会悄悄换掉后来的决定；她有意给旧版重新出的单
#   是最新的，照样有效。同一份字节的单与记录不算。先后只比存储里的递增序号，不看时间（复审：时钟回拨会颠倒）。只读。
#   返回取代它的最近一次的"版本（摘要前 12 位）"，没被取代返回空串。
# 函数用途: 判断这张单是不是已被同一个包后来的单或安装取代，并说出被哪一版取代。
def _superseded_by(store: LearnpackStore, order: InstallOrder) -> str:
    events = [(other.seq, other.version, other.sha256) for other in store.orders_for(order.package_id)]
    events += [(row["seq"], str(row.get("version") or ""), str(row.get("sha256") or "")) for row in store.installs()
               if row.get("package_id") == order.package_id and isinstance(row.get("seq"), int)]
    later = [event for event in events if event[2] != order.sha256 and event[0] > order.seq]
    if not later:
        return ""
    _seq, version, sha256 = max(later)
    return f"{version}（{sha256[:12]}）"


# LLM: 还在等用户确认的安装单：没执行过（没有 done 文件）、也没被同一个包后来的单或安装取代（_superseded_by 同一判断）。
#   按开单序号从新到旧。只读。安装回执的 open_orders 与宿主提示都用它，让给用户的确认行对得上存储里的事实（2026-10-07 生产复测：
#   她把早已执行过的单又交给用户）。
# 函数用途: 列出现在还有效、等用户确认的安装单。
def open_orders(store: LearnpackStore) -> list[InstallOrder]:
    rows = [order for order in store.all_orders()
            if store.order_outcome(order.order_id) is None and not _superseded_by(store, order)]
    return sorted(rows, key=lambda order: order.seq, reverse=True)


# LLM: 她在这个包名下装上过（结果进过安装表：已启用或停在启用确认）的各个版本，按每个版本第一次装上的序号排先后；只认
#   learnpack 存储里摘要与清单对得上、包名相同的。"上一版"按这个顺序取、不按最后一条记录，重新装回旧版后也不会来回跳（复审）。
#   只读。打包回执的"同号提醒"也只看这些版本（capability/learnpack_build_notes）。
# 函数用途: 列出她在这个包名下装上过的版本，从早到晚。
def installed_versions(store: LearnpackStore, package_id: str) -> list[BuildRecord]:
    first: dict[str, int] = {}
    for row in store.installs():
        seq, sha256 = row.get("seq"), str(row.get("sha256") or "")
        if row.get("package_id") == package_id and row.get("state") in _INSTALLED_STATES and isinstance(seq, int):
            first[sha256] = min(first.get(sha256, seq), seq)
    records = [store.build(sha256) for sha256 in sorted(first, key=first.__getitem__)]
    return [record for record in records if record is not None and record.package_id == package_id]


# LLM: 现在装着的版本在 installed_versions 里的前一个；现在装着的不是她装上过的版本就没有上一版。连续退回一路往前，退到
#   第一版为止。只读。
# 函数用途: 找到现在装着的版本的上一版（她做的），找不到返回 None。
def previous_build(store: LearnpackStore, current: object) -> BuildRecord | None:
    if current is None:
        return None
    versions = installed_versions(store, current.manifest.plugin_id)
    shas = [record.sha256 for record in versions]
    index = shas.index(current.package_sha256) if current.package_sha256 in shas else 0
    return versions[index - 1] if index > 0 else None


# LLM: 两段式（复审建议）：不带版本时只预览——说清会退到哪一版、会不会运行它自带的程序，并给出带版本摘要的那一行；带版本的
#   那一行才执行，目标只能是她在这个包名下装上过的版本。现在没装着这个包时一律不适用（不借退回把删掉的包装回来，复审 5 轮）；
#   现在装着的就是目标且已启用时什么都不做，同一行可以放心重发；装着目标但没启用（比如上次中途退出）就只补启用。用户发带版本的那一行就是同意运行该版本自带的程序，联网与读写目录授权仍不代给
#   （run_install 白名单）。管理员与插件功能在这里再查，什么都没做时不写记录。有副作用（执行时）：经宿主命令停用、更新、
#   启用，写包名归属与 via=revert 安装记录。
#   command_prefix 是预览末行命令去掉版本摘要的前半段（能力包用 "/plugins#<包名> 退回"），缺省为 "/plugins revert <包名>"。
# 函数用途: 预览或执行把她做的包退回到她做的某个旧版本。
def revert_install(manager: object, package_id: str, target: str = "", command_prefix: str = "") -> dict[str, object]:
    if not manager.context.is_admin:
        return _rejected("PLUGIN_PERMISSION_DENIED", "只有管理员能退回。")
    blocked = _management_problem(manager)
    if blocked is not None:
        return _rejected(*blocked)
    store = LearnpackStore(manager.context.owner.home_dir)
    current = installed_entry(tuple(manager.installations.snapshot()), package_id)
    if current is None:
        return _rejected("PACKAGE_REVERT_UNAVAILABLE", f"现在没装着 {package_id}，退回不适用；要重新装请让她重新出一张单。")
    record = _revert_target(store, package_id, target) if target else previous_build(store, current)
    if record is None:
        return _rejected("PACKAGE_REVERT_UNAVAILABLE", f"{package_id} 没有可退回的版本（只有她做的、在这个包名下装上过、"
                         "还留着的版本才能退回；带版本时请照预览那一行原样发）。")
    if not target:
        return _revert_preview(store, current, record, f"{command_prefix or f'/plugins revert {package_id}'} {record.sha256[:12]}")
    if current.package_sha256 == record.sha256 and current.enabled:
        return {"ok": True, "state": STATE_ENABLED, "message": f"已经是 {version_label(record)}，而且已启用；什么都没做。"}
    hooks = _InstallHooks(store, manager.context.owner, record)
    outcome = run_install(manager, hooks.request(InstallConsent(True, True)))
    if outcome.state not in REFUSED_STATES:
        store.record_install(_install_entry(record, outcome, {"via": "revert"}))
    return outcome_reply(outcome)


# LLM: 读包清单判断会不会运行程序。宿主写的事实（版本、会不会运行程序）单独一行在前；她声明的来源单独一行、标明是她的原文、
#   宿主未核实（打包时已挡住换行、控制与格式字符，所以它出不了自己那一行，复审 6 轮：引号挡不住她自己写的收尾引号）；
#   回执最后一行是带版本摘要的执行命令（IM 回执末行就是要发的那一行）。只读。
# 函数用途: 生成退回预览回执。
def _revert_preview(store: LearnpackStore, current: object, record: BuildRecord, line: str) -> dict[str, object]:
    note = ("装上后会运行它自带的程序（宿主代你确认运行她自己做的程序；联网和读写别的目录仍要你另外确认）"
            if package_runs_programs(store, record) else "它不运行程序")
    message = (f"现在装着 {current.manifest.plugin_id} {current.manifest.version}（{current.package_sha256[:12]}）。"
               f"退回会换成她做的 {version_label(record)}；{note}。\n"
               f"她声明的来源（原文，宿主未核实）：{record.origin}\n确认退回请发：\n{line}")
    return {"ok": True, "state": "revert_preview", "message": message}


# LLM: 版本摘要按十六进制前缀匹配她在这个包名下装上过的版本，必须恰好对上一个。只读。
# 函数用途: 把退回命令里的版本摘要解析成她的某个旧版本。
def _revert_target(store: LearnpackStore, package_id: str, target: str) -> BuildRecord | None:
    if _SHA_PREFIX.fullmatch(target) is None:
        return None
    matches = [record for record in installed_versions(store, package_id) if record.sha256.startswith(target)]
    return matches[0] if len(matches) == 1 else None


# LLM: "她做的"只按结构化事实：装着的字节摘要在 learnpack 存储里有打包记录。只列包名（受包名规则约束），不放她写的来源文字，
#   也不给插件指 /plugins#（那只管能力包，复审建议）。只读。
# 函数用途: 给 /plugins list 末尾补一行"my-agent 自己做的"。
def self_made_note(owner_home: object, entries: list) -> str:
    store = LearnpackStore(owner_home)
    names = [row.manifest.plugin_id for row in entries if store.build(row.package_sha256) is not None]
    return f"\nmy-agent 自己做的：{'、'.join(names)}" if names else ""


# LLM: 读包清单判断，与启用工具同一口径：插件有入口就会运行程序；能力包只有声明的检查程序要运行包内代码时才算。只读。
# 函数用途: 告诉用户这个包装上后会不会运行程序。
def package_runs_programs(store: LearnpackStore, record: BuildRecord) -> bool:
    manifest = inspect_plugin_package(store.build_path(record.sha256).read_bytes()).manifest
    if record.kind == KIND_CAPABILITY_PACK:
        verification = getattr(manifest.capability, "verification", None)
        return verification is not None and bool(verification.runs_package_code)
    return manifest.entry is not None


# LLM: 宿主回执用的字典：成功与否只看结构化 state；停在确认不是失败，不带错误码（IM 回执末行才能保持是那行命令）。
#   notice 是装上以后的用户提醒（同领域，learnpack_domain.installed_notice），只在装上时由调用方给，接在说明后面。纯函数。
# 函数用途: 把一次安装结果排成插件命令回执。
def outcome_reply(outcome: InstallOutcome, notice: str = "") -> dict[str, object]:
    reply: dict[str, object] = {"ok": outcome.state == STATE_ENABLED, "state": outcome.state,
                                "message": outcome.message + (f"\n{notice}" if notice else ""),
                                "learnpack_install": asdict(outcome)}
    if outcome.next_command:
        reply["message"] = f"{outcome.message}\n{outcome.next_command}"
    if outcome.error_code and outcome.state != STATE_NEEDS_USER:
        reply["error_code"] = outcome.error_code
    return reply


# LLM: 第二次确认如实回放：结果按中文说法给；还在"执行中"（可能进程中途退出）说正在执行或已中断，并给出已记下的请求编号
#   与下一步命令。纯函数。
# 函数用途: 同一张单再次确认时的回执。
def _already_done(order: InstallOrder, outcome: dict[str, object]) -> dict[str, object]:
    ids = [str(step.get("request_id")) for step in outcome.get("steps", []) if isinstance(step, dict)]
    ids = [*dict.fromkeys([*ids, *(str(item) for item in outcome.get("request_ids", []))])]
    state = str(outcome.get("install_state") or "")
    label = _STATE_LABELS.get(state, state) if state else "正在执行或已中断（结果未确认）"
    lines = [f"安装单 {order.order_id}（{order.package_id} {order.version}）已经执行过（或正在执行），同一张单只执行一次。",
             f"当时的结果：{label}。"]
    if outcome.get("message"):
        lines.append(f"当时的说明：{outcome['message']}")
    if ids:
        lines.append("宿主请求编号：" + "、".join(ids) + "（可用 /plugins status <编号> 在当时确认的会话里查）")
    if outcome.get("next_command"):
        lines.append(str(outcome["next_command"]))
    return {"ok": False, "state": "already_executed", "error_code": "PACKAGE_INSTALL_ORDER_USED", "message": "\n".join(lines)}


# LLM: 只组装字典；用于前置检查不过的情形，调用方保证此时没有执行任何宿主命令。纯函数。
# 函数用途: 生成拒绝回执（什么都没做、不占单）。
def _rejected(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "state": "rejected", "error_code": code, "message": message}


# LLM: 安装记录：她做的包、来源与许可证、这次的许可来源（via 与开关现读值/单号）、上一版摘要、宿主命令编号。纯组装。
# 函数用途: 生成一条安装记录。
def _install_entry(record: BuildRecord, outcome: InstallOutcome, consent_source: dict) -> dict[str, object]:
    return {"package_id": record.package_id, "kind": record.kind, "version": record.version, "sha256": record.sha256,
            "origin": record.origin, "license": record.license, "order_id": "", **consent_source,
            "state": outcome.state, "previous_sha256": outcome.previous_sha256,
            "request_ids": list(outcome.request_ids)}


# LLM: 能力包入口（/plugins#，kind=pack）整段交给 pack_commands（request_id、revision 是用户这次命令的请求编号与目录版本，
#   转发宿主命令时原样沿用）；
#   其余只认结构化解析结果：管理命名空间里的 confirm（带单号）与 revert（带包名）动作。解析失败或不是这两个动作返回 None，
#   交回插件管理原流程（原流程会给出参数错误的标准回执）。有副作用的执行在 confirm_order / revert_install / pack_commands。
# 函数用途: 识别并执行 learnpack 的插件命令（/plugins confirm、/plugins revert、/plugins#），返回原始回执。
def learnpack_command(manager: object, text: str, request_id: str = "", revision: str = "") -> dict[str, object] | None:
    from ..command_arguments import CommandArgumentError
    from ..plugin_commands import parse_plugin_command, plugin_namespace

    namespace = plugin_namespace(text)
    if namespace is not None and namespace.kind == "pack":
        from .pack_commands import run_pack_command

        return run_pack_command(manager, namespace, request_id, revision)
    if namespace is None or namespace.plugin_id:
        return None
    try:
        parsed = parse_plugin_command(text)
    except CommandArgumentError:
        return None
    if parsed is None or parsed.help_requested or parsed.action is None:
        return None
    if parsed.action.name == "confirm":
        return confirm_order(manager, parsed.arguments.values["order"])
    if parsed.action.name == "revert":
        return revert_install(manager, parsed.arguments.values["plugin"], parsed.arguments.values.get("target") or "")
    return None


__all__ = [
    "LEARNPACK_ACTOR",
    "LEARNPACK_CHANNEL",
    "LEARNPACK_CONVERSATION",
    "REFUSED_STATES",
    "STATE_BLOCKED",
    "STATE_ID_TAKEN",
    "confirm_order",
    "install_now",
    "installed_entry",
    "installed_versions",
    "learnpack_command",
    "learnpack_manager",
    "learnpack_owner",
    "open_orders",
    "outcome_reply",
    "ownership_problem",
    "package_runs_programs",
    "previous_build",
    "revert_install",
    "self_made_note",
]
