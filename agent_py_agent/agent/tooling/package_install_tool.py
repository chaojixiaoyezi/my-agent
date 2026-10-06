# LLM: learnpack 安装工具 package_install（只注册给本机管理员主代理，默认收起）。只接受 learnpack 存储里她自己打的包（按 sha256，
#   读回重算摘要）；能不能直接装只看对应开关的现读值（能力包看能力包开关，插件看插件开关），自然语言不参与。
#   开关开：宿主走管理员同一串 /plugins 命令装上并启用，代确认范围只取插件开关（运行她自己做的程序）；
#   开关关：只开一张待确认安装单，回执给出用户要原样发的那一行确认，什么都不装。
#   回执里的开关事实、命令原文都是给模型照抄提醒用户的；改字段要同步 learn-external-agent 技能与 test_learnpack_install。
# 模块用途: 让 my-agent 按你的开关把她打的包装上，或者准备好一行确认交给你。
from __future__ import annotations

import json

from ..capability.learnpack_installer import (
    STATE_ENABLED,
    STATE_NEEDS_USER,
    STATE_UNKNOWN,
    InstallConsent,
)
from ..capability.learnpack_service import (
    REFUSED_STATES,
    install_now,
    installed_entry,
    learnpack_owner,
    ownership_problem,
    package_runs_programs,
)
from ..capability.learnpack_store import BuildRecord, LearnpackStore
from ..capability.package_build import KIND_CAPABILITY_PACK
from ..capability.self_install_switches import (
    PACK_SELF_INSTALL_KEY,
    PLUGIN_SELF_INSTALL_KEY,
    SelfInstallSwitches,
    read_self_install_switches,
    switch_command,
    switch_facts,
)
from ..plugin_install_store import PluginInstallStore
from ..user_space.owner_access import is_complete_local_admin_owner
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PACKAGE_INSTALL_TOOL = "package_install"


# LLM: 只认本机管理员；包身份只认 learnpack 存储；开关现读。有副作用（开关开着时经宿主命令安装、启用，或写安装单）。
# 类用途: 按开关安装她打的包，或开一张待确认安装单。
class PackageInstallTool(BaseTool):
    model_spec = ToolModelSpec(
        name=PACKAGE_INSTALL_TOOL,
        description=(
            "安装用 package_build 打好的包（传它回执里的 sha256）。只能装你自己打的包。"
            "对应的自动装开关开着就直接装上并启用；关着只生成一张待确认安装单，回执里有用户要原样发的那一行确认。"
            "回执还给出开关现在的状态和开/关命令原文，照回执原文像教新手一样告诉用户：装了什么、怎么停用删除退回、"
            "怎么开关自动装；不要凭记忆说开关状态，不要替用户执行确认命令。"
        ),
        input_schema={
            "type": "object",
            "properties": {"sha256": {"type": "string", "description": "package_build 回执里的 sha256"}},
            "required": ["sha256"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=("把刚打好的能力包或插件装上", "开关关着时给用户准备一行确认"),
            avoid_when=("安装不是你自己打的包（那要管理员用 /plugins install）",),
            keywords=("安装", "能力包", "插件", "learnpack"),
            default_deferred=True,
            deferred_summary="安装你用 package_build 打好的包：开关开着直接装，关着给用户一行确认",
        ),
    )
    # 开关开着时经宿主命令改安装表；按原操作账去重，重放不重复安装。
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"),
                                       idempotency_policy=IdempotencyPolicy("operation"))

    # LLM: 构造不读写任何东西；身份与开关都在执行时现取。
    # 函数用途: 绑定当前 owner 的 agent。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 身份 → 包身份 → 包名归属（被别处同名包占着就不开单也不装）→ 开关，顺序固定；前面不过时什么都不写。
    # 函数用途: 安装或开单，并返回给模型照抄的回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        home = getattr(self._agent, "home_paths", None)
        if not is_complete_local_admin_owner(home):
            return _error("TOOL_PERMISSION_DENIED", "打包与自己装包第一期只对本机管理员开放。")
        store = LearnpackStore(home.owner_home_dir)
        record = store.build(str(params.get("sha256") or "").strip())
        if record is None:
            return _error("PACKAGE_INSTALL_NOT_SELF_BUILT",
                          "learnpack 存储里没有这个 sha256 的包（只能装你自己用 package_build 打的包）。")
        owner = learnpack_owner(self._agent)
        entries = tuple(PluginInstallStore(owner).snapshot())
        existing = installed_entry(entries, record.package_id)
        taken = ownership_problem(store, owner, record, entries)
        if taken is not None:
            return _error(taken.error_code, taken.message)
        switches = read_self_install_switches(self._agent)
        facts = {"package": _package_facts(store, record), "installed_now": _installed_facts(existing),
                 "switches": switch_facts(switches)}
        if not _can_install_now(record, switches, facts["package"]["runs_programs"]):
            return _order_receipt(store, record, facts)
        source = {"switch_capability_pack": switches.capability_pack, "switch_plugin": switches.plugin,
                  "config_version": switches.config_version}
        outcome = install_now(self._agent, record, InstallConsent(run_programs=switches.plugin, user_session=False), source)
        payload = {**facts, "state": outcome.state, "message": outcome.message, "next_command": outcome.next_command,
                   "request_ids": list(outcome.request_ids), "manage_commands": _manage_commands(record)}
        if outcome.state not in {STATE_ENABLED, STATE_NEEDS_USER}:
            return _failure(outcome, payload)
        return _ok(payload)


# LLM: 能直接装 = 对应开关开着，且包里要运行的程序有代确认许可（插件开关）。包里有程序而插件开关关着时改为出单，
#   不在半路停下、不去停用正在用的旧版本（复审 S3）。纯函数。
# 函数用途: 判断这次能不能不经用户确认一路装完。
def _can_install_now(record: BuildRecord, switches: SelfInstallSwitches, runs_programs: bool) -> bool:
    switch_on = switches.capability_pack if record.kind == KIND_CAPABILITY_PACK else switches.plugin
    return switch_on and (not runs_programs or switches.plugin)


# LLM: 开关关着：开单，回执给确认行与"以后都让她装"的开关命令；什么都不装。有写文件副作用（安装单）。
# 函数用途: 生成待确认安装单回执。
def _order_receipt(store: LearnpackStore, record: BuildRecord, facts: dict) -> ToolHandlerOutcome:
    order = store.create_order(record)
    key = PACK_SELF_INSTALL_KEY if record.kind == KIND_CAPABILITY_PACK else PLUGIN_SELF_INSTALL_KEY
    switch_on = facts["switches"][key]
    reason = ("包里有要运行的程序，而插件自动装开关关着，所以要用户确认这一次" if switch_on
              else "自动装开关关着，没有安装")
    installed = facts["installed_now"]
    current = (f"现在装着的同名包是 {installed['version']}（{'启用' if installed['enabled'] else '停用'}中），确认后换成这一版。"
               if installed else "")
    payload = {**facts, "state": "awaiting_user_confirmation", "order_id": order.order_id,
               "user_confirm_command": confirm_line(record, order.order_id),
               "enable_auto_install_command": switch_command(key if not switch_on else PLUGIN_SELF_INSTALL_KEY, True),
               "message": f"{reason}。{current}把 user_confirm_command 原样给用户：他发回来才装，这张单隔多久都有效、只执行一次；"
                          "想以后都让你自己装，给他 enable_auto_install_command。"}
    return _ok(payload)


# LLM: 用户确认行的唯一拼法；单号由宿主生成。能力包用 /plugins#<包名> 安装 <单号>，插件用 /plugins confirm <单号>（两种写法宿主
#   都认，按用户"能力包用 #、插件用 @"的分法给）。纯函数。
# 函数用途: 给出用户确认某张安装单要发的那一行。
def confirm_line(record: BuildRecord, order_id: str) -> str:
    if record.kind == KIND_CAPABILITY_PACK:
        return f"/plugins#{record.package_id} 安装 {order_id}"
    return f"/plugins confirm {order_id}"


# LLM: 命令原文给用户照抄：能力包用 /plugins# 写法（查看、停用、删除、退回），插件用 /plugins 管理动作（停用、删除、退回）。
#   只拼文字。纯函数。
# 函数用途: 装好以后管理这个包的命令原文。
def _manage_commands(record: BuildRecord) -> dict[str, str]:
    name = record.package_id
    if record.kind == KIND_CAPABILITY_PACK:
        return {"view": f"/plugins#{name} 查看", "disable": f"/plugins#{name} 停用",
                "remove": f"/plugins#{name} 删除", "revert": f"/plugins#{name} 退回"}
    return {"disable": f"/plugins disable {name}", "remove": f"/plugins remove {name}", "revert": f"/plugins revert {name}"}


# LLM: existing 来自同一次读到的安装表；没装返回 None。纯组装。
# 函数用途: 回执里"现在装着的同名包"的版本、摘要和是否启用。
def _installed_facts(existing: object) -> dict[str, object] | None:
    if existing is None:
        return None
    return {"version": existing.manifest.version, "sha256": existing.package_sha256, "enabled": bool(existing.enabled)}


# LLM: 只读打包记录与包清单。
# 函数用途: 回执里的包身份与"会不会运行程序"。
def _package_facts(store: LearnpackStore, record: BuildRecord) -> dict[str, object]:
    return {"kind": record.kind, "package_id": record.package_id, "version": record.version, "sha256": record.sha256,
            "origin": record.origin, "license": record.license, "runs_programs": package_runs_programs(store, record)}


# LLM: 结构化回执给模型照抄；纯组装。
# 函数用途: 生成成功回执。
def _ok(payload: dict) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(PACKAGE_INSTALL_TOOL, True, json.dumps({"ok": True, **payload}, ensure_ascii=False),
                              result_envelope={PACKAGE_INSTALL_TOOL: payload})


# LLM: 结果没法确认 → PACKAGE_INSTALL_UNCONFIRMED；什么都没做就被拒（插件功能关着、管理动作被禁用、驱动里再查到包名被占）→
#   照用拒绝时的登记码；其余明确失败 → PACKAGE_INSTALL_FAILED。step_error_code 是停下那一步宿主给的码。纯组装。
# 函数用途: 把没装成的安装结果转成失败回执。
def _failure(outcome: object, payload: dict) -> ToolHandlerOutcome:
    if outcome.state == STATE_UNKNOWN:
        code = "PACKAGE_INSTALL_UNCONFIRMED"
    elif outcome.state in REFUSED_STATES:
        code = outcome.error_code
    else:
        code = "PACKAGE_INSTALL_FAILED"
    return _error(code, outcome.message, {**payload, "step_error_code": outcome.error_code}, bool(outcome.request_ids))


# LLM: sent 表示已经发出过宿主命令（effect_outcome=unknown，可能已部分生效），否则 not_started；带 payload 时正文是 JSON。纯组装。
# 函数用途: 生成失败回执（可附已发生的事实）。
def _error(code: str, message: str, payload: dict | None = None, sent: bool = False) -> ToolHandlerOutcome:
    body = json.dumps({"ok": False, "error_code": code, "message": message, **(payload or {})}, ensure_ascii=False)
    return ToolHandlerOutcome(PACKAGE_INSTALL_TOOL, False, body if payload else message, error_code=code,
                              effect_outcome="unknown" if sent else "not_started",
                              result_envelope={PACKAGE_INSTALL_TOOL: payload or {}})


__all__ = ["PACKAGE_INSTALL_TOOL", "PackageInstallTool", "confirm_line"]
