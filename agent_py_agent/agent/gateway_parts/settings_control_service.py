# LLM: 聊天 `/settings` 的执行与中文回执，TUI 与飞书等 IM 共用（都经 Gateway 控制通道）。只有管理员可用：按控制范围解析出的
#   本机 local/main（已绑定管理员身份的 IM 私聊也解析到这里），因为全局参数对整个 Gateway 生效且含本机路径。读写统一走参数中心
#   （parameter_registry / parameter_changes），actor 记为 chat；回执经 mask_value 脱敏。普通异常不承诺“没有改动”。
#   改动须同步 control_commands._settings_command、command_catalog 的 settings 条目、TUI control_runtime 的文本还原与本地拒绝，
#   以及 test_settings_chat_control.py。不带参数只列参数中心的常用层级（COMMON_KEYS），/settings all 才列全部。
# 模块用途: 让管理员在 TUI 和 IM 里查看常用或全部参数，查找、查看、修改、恢复默认、查看记录并回滚参数。
from __future__ import annotations

import re
from collections.abc import Callable

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..settings.config_io import load_simple_yaml
from ..settings.parameter_changes import (
    ChangeOrigin,
    applied_after_change,
    displayed_change,
    parameter_history,
    reset_parameter,
    revert_change,
    set_parameter,
)
from ..settings.parameter_registry import (
    ParameterSpec,
    applied_value,
    common_parameters,
    parameter_registry,
    search_parameters,
)
from ..settings.user_config_capability import (
    BOUNDARY_KEYS,
    effect_text,
    mask_value,
    user_config_path,
)
from ..user_space.owner_access import is_local_admin_owner
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner

_KIND = "settings"
_ORIGIN = ChangeOrigin("chat", "聊天 /settings")
_LIST_LIMIT = 20
_NOT_ADMIN = "只有管理员能查看和修改全局参数（本机 TUI，或已用 /admin 绑定管理员身份的私聊）。"
_UNCONFIRMED = "参数暂时读不到或这次操作没能完整确认；请先发 /settings history 查看当前状态，再决定是否重试。"
_BOUNDARY_TEXT = "属于安全边界（凭据、权限、身份、路径、外部地址、会运行代码的设置等），只能由用户在宿主入口或配置文件里改"
# 常用视图每项只取说明的第一句（遇到句号、分号、冒号或“ - ”列表开头就停），最多 40 字。
_BRIEF_STOP = re.compile(r"[。；：:;\n]| - ")
_BRIEF_MAX = 40


# LLM: 只表示可预期的用户输入问题，消息直接给用户看。
# 类用途: /settings 的可预期失败。
class SettingsControlError(Exception):
    pass


# LLM: Gateway 控制分派入口；非管理员一律拒绝（不暴露任何参数），可预期失败给原因，其它异常只说没能完整确认。有写文件副作用。
# 函数用途: 执行一条已解析的 /settings 控制并返回中文回执。
def execute_settings_control(base_agent: object, command: ConversationControlCommand, scope: object) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(_KIND, False, command.usage)
    try:
        if not _is_admin(_scoped_home(base_agent, scope)):
            return ConversationControlResult(_KIND, False, _NOT_ADMIN)
        text = run_settings_control(getattr(base_agent, "config", None), command)
    except SettingsControlError as exc:
        return ConversationControlResult(_KIND, False, str(exc))
    except Exception:  # noqa: BLE001 - 控制回执不能带堆栈或本机路径之外的内部细节。
        return ConversationControlResult(_KIND, False, _UNCONFIRMED)
    return ConversationControlResult(_KIND, True, text)


# LLM: 与 /skills、/model 文字控制同一 owner 解析；只读，不创建目录。
# 函数用途: 按控制范围找到当前发起者的 owner 路径投影。
def _scoped_home(base_agent: object, scope: object) -> object:
    owner = resolve_gateway_scope_owner(base_agent, scope)
    base_home = base_agent.home_paths
    return home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))


# LLM: 必须是已解析的完整身份（provider/kind/id 都是非空字符串）且为本机 local/main；缺字段不能因空值被当成管理员。
# 函数用途: 判断当前发起者是否是管理员。
def _is_admin(home: object) -> bool:
    identity = [getattr(home, field, None) for field in ("owner_provider", "owner_kind", "owner_id")]
    return all(type(item) is str and item.strip() for item in identity) and is_local_admin_owner(home)


# LLM: 按解析器给出的 operation 分派；config 是 Gateway 启动时加载的配置（当前运行值与用户配置路径的来源）。
# 函数用途: 对管理员执行 /settings 的一个子命令，返回中文回执。
def run_settings_control(config: object, command: ConversationControlCommand) -> str:
    if command.operation == "help":
        return command.usage
    handler = _HANDLERS.get(command.operation)
    if handler is None:
        raise SettingsControlError(command.usage)
    _operation, _, argument = command.value.partition(" ")
    return handler(config, argument.strip())


def _user_path(config: object):
    path = user_config_path(config)
    if path is None:
        raise SettingsControlError("当前 Gateway 没有加载用户配置文件，不能查看覆盖或修改参数。")
    return path


# LLM: 值文字只由 mask_value 给出（布尔 true/false、数字照实、凭据遮住），与 user_config、config get 同一口径，
#   这里不再另写布尔、数字特判；空值在聊天里显示“（空）”，这是本出口唯一的界面差异（test_value_display_parity 钉住）。只读。
# 函数用途: 把一个参数值排成回执里给人看的文字。
def _value(spec: ParameterSpec, value: object) -> str:
    text = mask_value(spec.key, value)
    return text if text != "" else "（空）"


# LLM: 用户配置路径只来自 Gateway 启动配置（_user_path）；只读文件，不创建。
# 函数用途: 读出当前加载的用户配置（没有文件时为空）；没有加载用户配置时报可预期错误。
def _stored(config: object) -> dict:
    path = _user_path(config)
    return load_simple_yaml(path) if path.is_file() else {}


# LLM: 用户配置里的空值（None、YAML 留空读成的 []、空串）都按“空”比较，不能把留空误判成“改过”。只读。
# 函数用途: 把一个配置值换成判断“是否改过”用的比较文本。
def _compare_text(value: object) -> str:
    return "" if value is None or value == [] or value == "" else str(value).lower()


# LLM: 用户配置里写了、且与默认值不同才算改过（与原总览同一判据）。只读。
# 函数用途: 列出用户配置里改过的参数名。
def _changed_keys(registry: dict[str, ParameterSpec], stored: dict) -> list[str]:
    return [key for key, spec in registry.items()
            if key in stored and _compare_text(stored[key]) != _compare_text(spec.default)]


# LLM: 只做文字截取（遇到 _BRIEF_STOP 就停，超过 _BRIEF_MAX 加省略号），不改写说明内容。纯函数。
# 函数用途: 取参数说明的第一句当大白话标签；没有说明时如实写“暂无说明”。
def _brief(description: str) -> str:
    head = _BRIEF_STOP.split(description.strip(), maxsplit=1)[0].strip()
    if not head:
        return "（暂无说明）"
    return head if len(head) <= _BRIEF_MAX else head[:_BRIEF_MAX] + "…"


# LLM: 显示 Gateway 启动配置里的当前运行值；用户配置改过就注明默认值，改了还没重启就注明“发 /restart 后生效”。
#   值都经 mask_value 脱敏。只读。
# 函数用途: 把一个常用参数排成一行中文。
def _common_line(spec: ParameterSpec, config: object, stored: dict) -> str:
    running = getattr(config, spec.key, spec.default)
    marks = ""
    if spec.key in stored and _compare_text(stored[spec.key]) != _compare_text(spec.default):
        marks += f"（改过，默认 {_value(spec, spec.default)}）"
    if spec.key in stored and _compare_text(stored[spec.key]) != _compare_text(running):
        marks += f"（已改成 {_value(spec, stored[spec.key])}，发 /restart 后生效）"
    return f"- {spec.key} = {_value(spec, running)}{marks}：{_brief(spec.description)}"


# LLM: 未知/已删配置键只告警不生效，用户看不到就等于白配；这里把两个来源拼成给用户看的几行。只读，不解析消息文字。
#   两个来源先压成一层扁平列表，再过滤空项；项目约定新代码的嵌套不超过两层，所以不写 for→for→if 三层。
# 函数用途: 收集主配置与 capability 配置的告警，排成“配置告警 N 条”加逐条来源。
def _config_warning_lines(config: object) -> list[str]:
    sources = (
        ("agent 主配置", config),
        ("capability 配置", getattr(config, "capability_config", None)),
    )
    entries = [
        (source, str(item).strip())
        for source, holder in sources
        for item in (getattr(holder, "config_warnings", None) or [])
        if str(item).strip()
    ]
    if not entries:
        return []
    return [f"配置告警 {len(entries)} 条（下面这些配置键没生效，只是被忽略了）："] + [
        f"- [{source}] {text}" for source, text in entries
    ]


# LLM: 默认视图只列参数中心的常用层级（COMMON_KEYS），再用一句大白话说总数和怎么看全部；常用以外改过的只报个数，
#   详情在 /settings all。只读。
# 函数用途: /settings —— 列出常用参数与当前值，提示用 /settings all 看全部。
def _overview(config: object, _argument: str) -> str:
    registry = parameter_registry()
    stored = _stored(config)
    common = common_parameters()
    lines = [f"常用参数（{len(common)} 项，平时要调的基本都在这里）："]
    lines += [_common_line(spec, config, stored) for spec in common]
    others = [key for key in _changed_keys(registry, stored) if not registry[key].common]
    if others:
        lines.append(f"另外你还改过 {len(others)} 个其它参数，发 /settings all 查看。")
    lines += _config_warning_lines(config)
    lines.append(f"一共 {len(registry)} 项参数，这里只列常用的 {len(common)} 项，其余一般不用动；看全部发 /settings all。")
    lines.append("找参数：/settings search <关键词>；看说明：/settings show <参数名>；"
                 "修改：/settings set <参数名> <值>，改完发 /restart 重启 Gateway 后生效。")
    return "\n".join(lines)


# LLM: 全部视图 = 原总览（总数、可改范围、用户配置位置、改过的个数、最近修改）+ 按分类列出每个参数的当前运行值，
#   改过的标［改过］，不能在这里改的标［安全边界］。值都经 mask_value 脱敏；IM 适配器会按行切成多条消息。只读。
# 函数用途: /settings all —— 给管理员看全部参数。
def _all(config: object, _argument: str) -> str:
    registry = parameter_registry()
    path = _user_path(config)
    changed = set(_changed_keys(registry, _stored(config)))
    writable = sum(spec.writable for spec in registry.values())
    lines = [f"全部参数：共 {len(registry)} 个，其中 {writable} 个可由 my-agent 或你在这里修改，其余属于安全边界。",
             f"用户配置：{path}（修改在重启 Gateway 后生效，发 /restart）",
             f"与默认值不同的参数：{len(changed)} 个（下面标［改过］）"]
    recent = parameter_history(user_path=path, limit=3)
    if recent:
        lines += ["最近修改："] + [_history_line(item) for item in recent]
    lines += _config_warning_lines(config)
    lines += _category_lines(registry, config, changed)
    return "\n".join(lines + ["看说明发 /settings show <参数名>；只看常用参数发 /settings。"])


# LLM: 分类沿用登记表的 category，按参数第一次出现的顺序排，“其它”放最后；每项一行当前运行值与标记。只读。
# 函数用途: 按分类排出全部参数的清单行。
def _category_lines(registry: dict[str, ParameterSpec], config: object, changed: set[str]) -> list[str]:
    groups: dict[str, list[ParameterSpec]] = {}
    for spec in registry.values():
        groups.setdefault(spec.category, []).append(spec)
    lines: list[str] = []
    for category in sorted(groups, key=lambda name: name == "其它"):
        lines.append(f"【{category}】{len(groups[category])} 项")
        lines += [_all_line(spec, config, changed) for spec in groups[category]]
    return lines


# LLM: 值经 _value（凭据仍脱敏）；［安全边界］只看登记表的 writable，不按说明文字判断。只读。
# 函数用途: 把一个参数排成全部视图里的一行：当前运行值，改过标［改过］，不能在这里改标［安全边界］。
def _all_line(spec: ParameterSpec, config: object, changed: set[str]) -> str:
    marks = ("［改过］" if spec.key in changed else "") + ("" if spec.writable else "［安全边界］")
    return f"- {spec.key} = {_value(spec, getattr(config, spec.key, spec.default))}{marks}"


# 函数用途: /settings search <关键词> —— 按参数名与中文说明找参数。
def _search(config: object, argument: str) -> str:
    found = search_parameters(argument, limit=10)
    if not found:
        return f"没有找到和“{argument}”相关的参数。"
    lines = [f"和“{argument}”相关的参数（最多 10 个）："]
    for spec in found:
        flag = "可改" if spec.writable else "安全边界"
        summary = spec.description[:60] + ("…" if len(spec.description) > 60 else "")
        lines.append(f"- {spec.key}［{flag}］当前 {_value(spec, getattr(config, spec.key, spec.default))}：{summary or '（没有说明）'}")
    return "\n".join(lines)


# LLM: config 是 Gateway 启动配置，所以登记了派生规则的参数（如 max_tokens、推理强度）按默认模型算实际效果，并注明
#   /model 切换过的会话可能不同；派生公式只在参数中心 applied_value 背后的原权威位置。只读。
# 函数用途: /settings show <参数名> —— 说明、默认值、当前运行值、实际效果（有派生规则时）、用户配置里的值与能否修改。
def _show(config: object, argument: str) -> str:
    spec = parameter_registry().get(argument)
    if spec is None:
        raise SettingsControlError(f"没有名为 {argument} 的参数；可以发 /settings search <关键词> 找一找。")
    path = _user_path(config)
    stored = load_simple_yaml(path) if path.is_file() else {}
    override = _value(spec, stored[spec.key]) if spec.key in stored else "未覆盖（用默认值）"
    writable = (f"可以修改，{effect_text(spec.effect)}" if spec.writable
                else f"不能在这里修改：{BOUNDARY_KEYS.get(spec.key, _BOUNDARY_TEXT)}")
    applied = applied_value(spec.key, config)
    applied_line = ([f"实际效果：{applied[0]}（{applied[1]}；按默认模型计算，用 /model 切换过的会话可能不同）"]
                    if applied is not None else [])
    return "\n".join([
        f"{spec.key}（{spec.category}，{spec.value_type}）",
        f"说明：{spec.description or '（没有说明）'}",
        f"默认值：{_value(spec, spec.default)}；当前运行值：{_value(spec, getattr(config, spec.key, spec.default))}；"
        f"用户配置里：{override}",
        *applied_line,
        f"能否修改：{writable}",
    ])


def _checked(report: dict[str, object]) -> dict[str, object]:
    if not report.get("ok"):
        raise SettingsControlError(str(report.get("error") or "没有执行。"))
    return report


# LLM: 与 /settings show 同一口径（Gateway 启动配置即默认模型）；只对登记了派生规则的参数有这句（applied_after_change）。只读。
# 函数用途: 生成修改类回执末尾“按新值在默认模型上的实际效果”这一句，没有派生规则时为空串。
def _applied_text(report: dict[str, object], config: object) -> str:
    applied = applied_after_change(report, config)
    return "" if applied is None else f"按新值在默认模型上的实际效果：{applied[0]}（{applied[1]}；用 /model 切换过的会话可能不同）"


# LLM: 登记了派生规则的参数再附一句实际效果（_applied_text），让用户当下就知道改了是否真的起作用。有写文件副作用。
# 函数用途: /settings set <参数名> <值> —— 修改一个参数并记账。
def _set(config: object, argument: str) -> str:
    key, _, value = argument.partition(" ")
    report = _checked(set_parameter(key, value, user_path=_user_path(config), origin=_ORIGIN))
    previous = report.get("previous")
    return (f"已把 {key} 改为 {report['saved']}（原来 {previous if previous is not None else '是默认值'}），"
            f"记录编号 {str(report['change_id'])[:8]}。{report['effect_text']}{_applied_text(report, config)}")


# LLM: 与 set 同一口径附实际效果（按默认值算）。有写文件副作用。
# 函数用途: /settings reset <参数名> —— 删除覆盖、恢复默认并记账。
def _reset(config: object, argument: str) -> str:
    report = _checked(reset_parameter(argument, user_path=_user_path(config), origin=_ORIGIN))
    return (f"已把 {argument} 恢复为默认值（原来 {report.get('previous')}），记录编号 {str(report['change_id'])[:8]}。"
            f"{report['effect_text']}{_applied_text(report, config)}")


# LLM: 修改记录的回显出口，先经 displayed_change 脱敏（旧记录也可能含明文）。只读。
# 函数用途: 把一条修改记录排成一行中文。
def _history_line(item: dict[str, object]) -> str:
    item = displayed_change(item)
    before = item.get("previous") if item.get("previous") is not None else "默认"
    after = item.get("value") if item.get("value") is not None else "默认"
    return (f"- {str(item.get('id', ''))[:8]} {str(item.get('at', ''))[:16].replace('T', ' ')} {item.get('key')} "
            f"{item.get('action')}：{before} → {after}（{item.get('actor')}）")


# 函数用途: /settings history [参数名] —— 最近 20 条修改记录（最新在前）。
def _history(config: object, argument: str) -> str:
    items = parameter_history(user_path=_user_path(config), key=argument, limit=_LIST_LIMIT)
    if not items:
        return "还没有参数修改记录。"
    return "\n".join(["参数修改记录（最新在前）："] + [_history_line(item) for item in items]
                     + ["回滚一次修改：/settings revert <记录编号>"])


# LLM: 与 set 同一口径附实际效果（按回滚后的值算，回到默认时按默认值）。有写文件副作用。
# 函数用途: /settings revert <记录编号> —— 撤销一次修改并记账。
def _revert(config: object, argument: str) -> str:
    report = _checked(revert_change(argument, user_path=_user_path(config), origin=_ORIGIN))
    now = report.get("saved") if report.get("saved") is not None else "默认值"
    return (f"已回滚记录 {argument}：{report['key']} 现在是 {now}，新记录编号 {str(report['change_id'])[:8]}。"
            f"{report['effect_text']}{_applied_text(report, config)}")


_HANDLERS: dict[str, Callable[[object, str], str]] = {
    "overview": _overview,
    "all": _all,
    "search": _search,
    "show": _show,
    "set": _set,
    "reset": _reset,
    "history": _history,
    "revert": _revert,
}


__all__ = ["SettingsControlError", "execute_settings_control", "run_settings_control"]
