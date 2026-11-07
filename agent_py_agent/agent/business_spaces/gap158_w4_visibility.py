"""W4 工具/技能/插件可见性（owner 级，不新建第二份可见性存储）。

权威位置只有原策略事实：工具是 owner ``tool_policy.json`` 的 ``disabled_tools``，
技能是 ``skill_policy.json`` 的 ``disabled_skills``，插件走原生 ``PluginManagement`` 的
enable/disable 生命周期。本模块只做结构化投影与最小 CAS 写入，不复制策略库、
不创建影子目录，也不从展示文案推断授权。

边界：
- owner 身份只来自真实 ``V13Call``（``owner_home``/``owner_id`` 已由
  ``v13_require_access`` 核验），调用参数里的任何 id 都不参与身份判断；
- 列表固定包含被禁用的条目（否则禁用之后就再也无法重新打开），
  工具目录直接读原生工具注册表，技能目录直接读原生 skill 发现；
- 部署强制关闭的项（例如网络关闭时被强制停用的联网工具）不能被 owner 抬回可见，
  这类请求按合同返回 ``STATE_CONFLICT``；
- 技能可见性只表示 ``disabled_skills`` 策略闸门；资产自身 ``skill_meta.enabled``
  是更上层的资产开关，不属于本动作，避免越权改写技能正文与资产元数据。
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..user_space.owner_policy import (
    _effective_disabled_tools,
    cas_update_owner_skill_policy_disabled_skills,
    cas_update_owner_tool_policy_disabled_tools,
    owner_policy_document_sha256,
    read_owner_policy_bundle,
    resolve_effective_owner_policy,
)
from .v13_store import _page_rows


# LLM: 每个入口先核原签发的活跃上下文；本模块不自行发明授权。
# 函数用途: 在当前调用上执行 V13 实时准入检查。
def _require(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    return v13_require_access(call)


# LLM: owner 目录由宿主绑定，且必须与调用自身的 owner_home 一致；参数不能指定 owner。
# 函数用途: 取得本次调用已核验的 owner 路径句柄。
def _home(call):
    paths = getattr(call.context.host, "home_paths", None)
    if paths is None or getattr(paths, "owner_home_dir", None) is None:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "当前宿主没有绑定 owner 目录。")
    if Path(paths.owner_home_dir).resolve() != Path(call.owner_home).resolve():
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return paths


# LLM: 分页参数按合同 1..100、默认 50；越界属于请求问题，不能静默截断或放大。
# 函数用途: 校验并返回分页大小。
def _limit(params):
    limit = params.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HostAPIError("INVALID_SCHEMA")
    return limit


# LLM: 策略里的列表只承认字符串；缺失或坏形状按空集读取，写入侧才会拒绝坏原件。
# 函数用途: 把策略字段规范化成去空白的字符串集合。
def _string_set(value):
    if not isinstance(value, (list, tuple)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


# LLM: 工具目录就是原生工具注册表里已注册的全部工具（含被禁用的那些），
#   这样关掉一个工具之后仍然能在列表里重新打开；注册表只来自宿主绑定的完整 owner Agent。
# 函数用途: 枚举部署内全部工具编号。
def _tool_ids(call):
    provider = getattr(call.context, "host_provider", None)
    if not callable(provider):
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "当前宿主没有绑定工具注册表入口。")
    try:
        with ExitStack() as stack:
            agent = provider(stack)
            registry = getattr(agent, "tools", None)
            tools = getattr(registry, "tools", None)
    except HostAPIError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "工具注册表不可用。") from error
    if not isinstance(tools, dict) or not tools:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "工具注册表尚未就绪。")
    return sorted(str(name) for name in tools)


# LLM: builtin 根的原生声明在既有 W11 分片里；复用同一发现入口，不另造技能来源。
# 函数用途: 返回原生技能描述符（包含已禁用条目）。
def _skill_descriptors(call):
    from ..assets.skill_adapter import list_owner_skill_descriptors
    from .v13_w11_skills_learning import _builtin_roots

    try:
        return list_owner_skill_descriptors(Path(call.owner_home).absolute(), _builtin_roots())
    except OSError as error:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "技能目录不可用。") from error


# LLM: 面板展示名、资产 id 与稳定引用都能定位同一条技能；只用于过滤，不改身份来源。
# 函数用途: 在原生技能描述符里定位一次准确匹配。
def _match_skill(descriptors, name):
    for descriptor in descriptors:
        if str(descriptor.get("name") or "") == name:
            return descriptor
    for descriptor in descriptors:
        metadata = descriptor.get("metadata") or {}
        stable = str(metadata.get("stable_id") or "")
        if name in {str(descriptor.get("id") or ""), stable} or stable.rsplit(":", 1)[-1] == name:
            return descriptor
    return None


# LLM: 启用时要连同历史别名写法一起移除，否则旧写法仍会把技能挡住。
# 函数用途: 构造一条技能的全部等价引用。
def _skill_aliases(descriptor, canonical):
    metadata = descriptor.get("metadata") or {}
    stable = str(metadata.get("stable_id") or "")
    aliases = [str(descriptor.get("id") or "")]
    if stable:
        aliases.extend([stable, "owner:" + canonical])
    return tuple(alias for alias in dict.fromkeys(aliases) if alias and alias != canonical)


# LLM: 工具列表的 visible 取合并后的有效策略（owner 列表 + 部署强制），
#   同时把两层拆开返回给写入口，才能区分"owner 自己关的"和"部署强制关的"。
# 函数用途: 投影工具可见性行并返回 owner 配置集合与有效集合。
def _tool_rows(call, home):
    bundle = read_owner_policy_bundle(home)
    configured = _string_set(bundle.tool_policy.get("disabled_tools"))
    effective = set(resolve_effective_owner_policy(home).disabled_tools)
    rows = [{"kind": "tool", "id": tool_id, "visible": tool_id not in effective,
             "source": "tool_registry"} for tool_id in _tool_ids(call)]
    return rows, configured, effective


# LLM: 技能列表同样包含被禁用的条目，visible 只表达 disabled_skills 策略闸门。
# 函数用途: 投影技能可见性行并返回描述符与禁用集合。
def _skill_rows(call, home):
    descriptors = _skill_descriptors(call)
    disabled = _string_set(read_owner_policy_bundle(home).skill_policy.get("disabled_skills"))
    rows = []
    for descriptor in descriptors:
        name = str(descriptor.get("name") or "")
        if not name:
            continue
        aliases = {name, *_skill_aliases(descriptor, name)}
        rows.append({"name": name, "visible": not bool(aliases & disabled),
                     "source": str((descriptor.get("metadata") or {}).get("source") or "")})
    return rows, descriptors, disabled


# LLM: 插件目录复用原生安装表可见性（本人私有 + 已公开条目），不读别的 owner 私有安装。
# 函数用途: 返回当前调用可见的插件安装行。
def _plugin_manager(call):
    from .v13_w11_plugins import _native_management

    return _native_management(call)


# 函数用途: 投影插件可见性行。
def _plugin_rows(call):
    from ..plugin_management import _visible_entries

    manager = _plugin_manager(call)
    rows = [{"kind": "plugin", "id": str(entry.manifest.plugin_id),
             "visible": bool(entry.enabled), "source": str(entry.installation_scope)}
            for entry in _visible_entries(manager)]
    return sorted(rows, key=lambda row: row["id"])


# LLM: CAS 重试只处理"别人刚提交过"的瞬时竞争；持续失败按依赖未就绪处理，不假装成功。
# 函数用途: 反复"读摘要→条件写"直到本次修改真正落地。
def _cas_write(path, write_once):
    for _attempt in range(5):
        if write_once(owner_policy_document_sha256(path)):
            return
    raise HostAPIError("HOST_NOT_READY", 503, "not_started", "策略文件持续被并发改写，请重试。")


# LLM: 分页游标绑定按 owner/kind/当前行集合派生；集合变化后旧游标明确过期而不是错位。
# 函数用途: 把行集合切成合同要求的一页。
def _page(call, rows, binding, params):
    try:
        result = _page_rows(rows, binding, cursor=params.get("cursor"), limit=_limit(params))
    except BusinessError as error:
        # 游标记为进程内键：过期按 503 让客户端从第一页重来，其它参数问题仍是 400。
        raise HostAPIError(error.code, 503 if error.code == "CURSOR_EXPIRED" else 400) from error
    page = {"items": result["items"], "coverage": result["coverage"]}
    if result["next_cursor"] is not None:
        page["next_cursor"] = result["next_cursor"]
    return page


# 读接口：kind=tool|plugin 时按 id 过滤，分页沿用 V13 HMAC 游标。
def visibility_item_get(call):
    """返回工具/插件可见性分页；owner 身份只来自调用本身，未知 id 表现为空页。"""

    _require(call)
    home = _home(call)
    params = call.params
    kind = params.get("kind")
    if kind not in ("tool", "plugin"):
        raise HostAPIError("INVALID_SCHEMA")
    rows = _tool_rows(call, home)[0] if kind == "tool" else _plugin_rows(call)
    wanted = params.get("id")
    if isinstance(wanted, str) and wanted:
        rows = [row for row in rows if row["id"] == wanted]
    return _page(call, rows, [str(call.owner_home), call.owner_id, call.subject_id, kind, rows], params)


# 写接口：tool 改 owner 策略列表；plugin 走原生启停生命周期。
def visibility_item_toggle(call):
    """切换一个工具或插件的可见性，返回同形 DTO；部署强制关闭的工具拒绝恢复。"""

    _require(call)
    home = _home(call)
    params = call.params
    kind, item_id, visible = params.get("kind"), params.get("id"), params.get("visible")
    if kind not in ("tool", "plugin") or type(item_id) is not str or not item_id or type(visible) is not bool:
        raise HostAPIError("INVALID_SCHEMA")
    if kind == "plugin":
        return {"item": _plugin_toggle(call, item_id, visible)}
    return {"item": _tool_toggle(call, home, item_id, visible)}


# LLM: 工具可见性只改 owner tool_policy.disabled_tools；注册表里没有的编号明确不存在。
# 函数用途: 原子切换一个工具的 disabled 成员关系。
def _tool_toggle(call, home, tool_id, visible):
    if tool_id not in _tool_ids(call):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "工具不存在。")
    def write_once(digest):
        _require(call)
        policy = resolve_effective_owner_policy(home)
        # 强制限制必须独立于 owner 的名单计算；两层同时禁用时也不能被名单差集抵消。
        forced = _effective_disabled_tools({}, policy.network_enabled, None)
        if visible and tool_id in forced:
            raise HostAPIError("STATE_CONFLICT", 409, "not_started", "该工具由部署强制停用，owner 不能恢复其可见性。")
        return cas_update_owner_tool_policy_disabled_tools(home, tool_id, visible, expected_sha256=digest)

    try:
        _cas_write(home.owner_tool_policy_json, write_once)
    except HostAPIError:
        raise
    except ValueError as error:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "工具策略不可用。") from error
    current = set(resolve_effective_owner_policy(home).disabled_tools)
    return {"kind": "tool", "id": tool_id, "visible": tool_id not in current, "source": "tool_registry"}


# LLM: 技能可见性沿用技能发现全集，未知名称表现为空页，未知切换目标明确不存在。
# 函数用途: 返回技能可见性分页。
def visibility_skill_get(call):
    """返回技能可见性分页；name 为空表示全部，未知 name 返回空页。"""

    _require(call)
    home = _home(call)
    params = call.params
    rows, descriptors, _disabled = _skill_rows(call, home)
    wanted = params.get("name")
    if isinstance(wanted, str) and wanted:
        matched = _match_skill(descriptors, wanted)
        rows = [row for row in rows if matched is not None and row["name"] == str(matched.get("name") or "")]
    return _page(call, rows, [str(call.owner_home), call.owner_id, call.subject_id, "skill", rows], params)


# LLM: 技能可见性只改 owner skill_policy.disabled_skills，不越权改资产正文/资产侧开关。
# 函数用途: 原子切换一个技能的 disabled 成员关系。
def visibility_skill_toggle(call):
    """切换一个技能的可见性，返回同形 DTO；未知技能按 RESOURCE_NOT_FOUND 处理。"""

    _require(call)
    home = _home(call)
    params = call.params
    name, visible = params.get("name"), params.get("visible")
    if type(name) is not str or not name or type(visible) is not bool:
        raise HostAPIError("INVALID_SCHEMA")
    descriptors = _skill_descriptors(call)
    descriptor = _match_skill(descriptors, name)
    if descriptor is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "技能不存在。")
    canonical = str(descriptor.get("name") or name)
    try:
        _cas_write(home.owner_skill_policy_json, lambda digest: cas_update_owner_skill_policy_disabled_skills(
            home, canonical, _skill_aliases(descriptor, canonical), visible, expected_sha256=digest))
    except HostAPIError:
        raise
    except ValueError as error:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "技能策略不可用。") from error
    disabled = _string_set(read_owner_policy_bundle(home).skill_policy.get("disabled_skills"))
    return {"item": {"name": canonical, "visible": canonical not in disabled,
                     "source": str((descriptor.get("metadata") or {}).get("source") or "")}}


# LLM: 插件启停只沿原生安装表与命令/审批链；本函数不复制安装状态机。
# 函数用途: 通过原生 PluginManagement 切换一个已安装插件的启停。
def _plugin_toggle(call, plugin_id, visible):
    from ..plugin_disable_tool import PLUGIN_DISABLE_TOOL
    from ..plugin_enable_tool import PLUGIN_ENABLE_TOOL
    from ..plugin_installation import PluginInstallationError
    from .v13_w11_plugins import _native_management, _operation_id, _write_check

    manager = _native_management(call)
    _write_check(call, PLUGIN_ENABLE_TOOL if visible else PLUGIN_DISABLE_TOOL, plugin_id)
    try:
        entry, result = manager.package_lifecycle(
            plugin_id, "enable" if visible else "disable", operation_id=_operation_id(call))
    except PluginInstallationError as error:
        raise HostAPIError("STATE_CONFLICT", 409, "not_started", "插件启停被原生生命周期拒绝。") from error
    if entry is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前主人。")
    if (result or {}).get("state") != "succeeded" or bool(entry.enabled) != visible:
        raise HostAPIError("STATE_CONFLICT", 409, "not_started", "插件启停没有达到请求状态。")
    return {"kind": "plugin", "id": str(entry.manifest.plugin_id), "visible": bool(entry.enabled),
            "source": str(entry.installation_scope)}


__all__ = [
    "visibility_item_get",
    "visibility_item_toggle",
    "visibility_skill_get",
    "visibility_skill_toggle",
]
