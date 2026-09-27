# LLM: 聊天 `/settings` 的执行与中文回执，TUI 与飞书等 IM 共用（都经 Gateway 控制通道）。只有管理员可用：按控制范围解析出的
#   本机 local/main（已绑定管理员身份的 IM 私聊也解析到这里），因为全局参数对整个 Gateway 生效且含本机路径。读写统一走参数中心
#   （parameter_registry / parameter_changes），actor 记为 chat；回执经 mask_value 脱敏。普通异常不承诺“没有改动”。
#   改动须同步 control_commands._settings_command、command_catalog 的 settings 条目、TUI control_runtime 的文本还原与本地拒绝，
#   以及 test_settings_chat_control.py。
# 模块用途: 让管理员在 TUI 和 IM 里查找、查看、修改、恢复默认、查看记录并回滚参数。
from __future__ import annotations

from collections.abc import Callable

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..settings.config_io import load_simple_yaml
from ..settings.parameter_changes import (
    ChangeOrigin,
    parameter_history,
    reset_parameter,
    revert_change,
    set_parameter,
)
from ..settings.parameter_registry import (
    ParameterSpec,
    applied_value,
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


def _value(spec: ParameterSpec, value: object) -> str:
    text = mask_value(spec.key, value)
    return text if text != "" else "（空）"


# 函数用途: /settings —— 参数总数、可改范围、已改过的参数与最近修改。
def _overview(config: object, _argument: str) -> str:
    registry = parameter_registry()
    path = _user_path(config)
    stored = load_simple_yaml(path) if path.is_file() else {}
    changed = [(spec, stored[key]) for key, spec in registry.items()
               if key in stored and str(stored[key]).lower() != str(spec.default).lower()]
    writable = sum(spec.writable for spec in registry.values())
    lines = [f"参数中心：共 {len(registry)} 个参数，其中 {writable} 个可由 my-agent 或你在这里修改，其余属于安全边界。",
             f"用户配置：{path}（修改在重启 Gateway 后生效，发 /restart）", f"与默认值不同的参数：{len(changed)} 个"]
    lines += [f"- {spec.key} = {_value(spec, value)}（默认 {_value(spec, spec.default)}）" for spec, value in changed[:_LIST_LIMIT]]
    if len(changed) > _LIST_LIMIT:
        lines.append(f"……另有 {len(changed) - _LIST_LIMIT} 个，可用 /settings show <参数名> 逐个查看。")
    recent = parameter_history(user_path=path, limit=3)
    if recent:
        lines += ["最近修改："] + [_history_line(item) for item in recent]
    return "\n".join(lines + ["发 /settings search <关键词> 找参数，/settings show <参数名> 看说明。"])


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


# LLM: config 是 Gateway 启动配置，所以登记了派生规则的参数（如 max_tokens）按默认模型算实际使用值，并注明
#   /model 切换过的会话可能不同；派生公式只在参数中心 applied_value 背后的原权威位置。只读。
# 函数用途: /settings show <参数名> —— 说明、默认值、当前运行值、实际使用值（有派生规则时）、用户配置里的值与能否修改。
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
    applied_line = ([f"实际使用值：{applied[0]}（{applied[1]}；按默认模型计算，用 /model 切换过的会话可能不同）"]
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


# 函数用途: /settings set <参数名> <值> —— 修改一个参数并记账。有写文件副作用。
def _set(config: object, argument: str) -> str:
    key, _, value = argument.partition(" ")
    report = _checked(set_parameter(key, value, user_path=_user_path(config), origin=_ORIGIN))
    previous = report.get("previous")
    return (f"已把 {key} 改为 {report['saved']}（原来 {previous if previous is not None else '是默认值'}），"
            f"记录编号 {str(report['change_id'])[:8]}。{report['effect_text']}")


# 函数用途: /settings reset <参数名> —— 删除覆盖、恢复默认并记账。有写文件副作用。
def _reset(config: object, argument: str) -> str:
    report = _checked(reset_parameter(argument, user_path=_user_path(config), origin=_ORIGIN))
    return (f"已把 {argument} 恢复为默认值（原来 {report.get('previous')}），记录编号 {str(report['change_id'])[:8]}。"
            f"{report['effect_text']}")


def _history_line(item: dict[str, object]) -> str:
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


# 函数用途: /settings revert <记录编号> —— 撤销一次修改并记账。有写文件副作用。
def _revert(config: object, argument: str) -> str:
    report = _checked(revert_change(argument, user_path=_user_path(config), origin=_ORIGIN))
    now = report.get("saved") if report.get("saved") is not None else "默认值"
    return f"已回滚记录 {argument}：{report['key']} 现在是 {now}，新记录编号 {str(report['change_id'])[:8]}。{report['effect_text']}"


_HANDLERS: dict[str, Callable[[object, str], str]] = {
    "overview": _overview,
    "search": _search,
    "show": _show,
    "set": _set,
    "reset": _reset,
    "history": _history,
    "revert": _revert,
}


__all__ = ["SettingsControlError", "execute_settings_control", "run_settings_control"]
