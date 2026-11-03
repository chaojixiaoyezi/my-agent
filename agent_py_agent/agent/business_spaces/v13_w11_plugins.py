"""W11 插件与通知通道业务实现。

复用原生 PluginManagement 的目录和生命周期、PluginInstallStore 的配置校验与
CAS，只在 V13 边界做参数投影、递归脱敏和幂等回执；不新建第二份安装表、
执行器或授权来源，也不从展示文案推断连接状态。
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from uuid import uuid4

from ..plugin_configuration import PluginConfigureRequest
from ..plugin_host.errors import HostAPIError
from ..plugin_install_store import PluginInstallStore
from ..plugin_installation import PluginInstallationError
from ..plugin_management import PluginManagement
from ..plugin_manifest import canonical_plugin_settings
from ..runtime_db.operations import RuntimeConflictError
from ..user_space.home_layout import home_paths
from ..user_space.owner_access import is_complete_local_admin_owner
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
from .v13_store import v13_store_for


# LLM: 摘要只参与幂等键派生和回执指纹，不进入展示、日志或授权判断。
# 函数用途: 生成稳定的 sha256 十六进制摘要。
def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# LLM: 原生安装/命令回执要求稳定操作号；同一 owner、动作和幂等键的重送必须落到同一操作。
# 函数用途: 从 V13 调用身份派生原生可接受的操作编号。
def _operation_id(call):
    return "v13-" + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:40]


# LLM: 原生命令按 channel/conversation/actor 绑定线程，重送换 request_id 不能换线程。
# 函数用途: 为同一次幂等操作生成稳定的会话键。
def _conversation_id(call):
    if call.idempotency_key:
        return "v13-" + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:32]
    return "v13-read-" + _digest([call.owner_id, call.request_id])[:32]


# LLM: 时间只来自注入时钟，保证测试与运行时看到同一结构化事实。
# 函数用途: 生成 UTC ISO 时间戳。
def _clock_text(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 插件身份、权限、线程与工作目录全部来自当前已签发上下文；缺任一原生依赖即未就绪。
# 函数用途: 按 /client/plugins 同一原生入口组装 PluginManagement。
def _native_management(call):
    from ..gateway_parts.business_api_v13 import v13_require_access
    from ..plugin_management import plugin_management_context

    v13_require_access(call)
    host = call.context.host
    paths = getattr(host, "home_paths", None)
    config = getattr(host, "config", None)
    store = getattr(host, "conversation_store", None)
    if paths is None or config is None or store is None:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "插件宿主依赖尚未就绪。")
    identity = _identity_from_paths(paths)
    if identity is None:
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "当前宿主没有绑定规范 owner 身份。")
    if not all(hasattr(config, name) for name in ("enable_tools", "enable_plugins", "path_access_mode")):
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "当前宿主没有完整插件配置。")
    owner = resolve_owner_home(paths.root, identity)
    home = home_paths_with_owner(home_paths(paths.root), owner)
    try:
        native = plugin_management_context(
            owner, home, config, store.threads, actor_id=call.subject_id or "host",
            channel="client_business", conversation_id=_conversation_id(call),
            is_admin=_native_admin(call, home),
        )
    except (OSError, ValueError, TypeError):
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "插件管理上下文不可用。") from None
    return _LiveManagement(native, call)


# LLM: HTTP 管理资格只能读请求闭包的实时原生裁决；本地直连才沿完整 main 身份。
# 函数用途: 取得原生管理员状态，不把 local/main 的路径当 HTTP 管理员授权。
def _native_admin(call, home):
    native = getattr(call.context.host, "business_native_admin", None)
    return native() is True if callable(native) else is_complete_local_admin_owner(home)


# LLM: 原工具执行器仍是唯一执行链；每次真正 execute 前重新读取身份和原工具策略。
# 类用途: 在原管理工具的实际执行入口核验实时资格，不改全局类或缓存授权。
class _LiveManagement(PluginManagement):
    # LLM: 调用只保留在当前短生命周期实例，不进入宿主缓存。
    # 函数用途: 保存本次已签发调用以供实际执行边界重核。
    def __init__(self, context, call):
        super().__init__(context)
        self.call = call

    # LLM: 原工具及其 schema/runtime_policy 不变，仅在副作用开始前重核原资格。
    # 函数用途: 给当前原生管理工具附加实际执行边界核验。
    def _management_tool(self, repo, binding, arguments):
        tool = super()._management_tool(repo, binding, arguments)
        execute = tool.execute

        # LLM: 不捕获提交后的异常并伪装拒绝；失败及未知结果仍由原工具账处理。
        # 函数用途: 在原工具执行的最后入口重读权限与配置策略。
        def checked(params):
            from ..tooling.models import ToolHandlerOutcome

            try:
                _write_check(self.call, binding.request.command_name, arguments.get("plugin"))
            except HostAPIError:
                return ToolHandlerOutcome(binding.request.command_name, False, "原生实时资格已失效。",
                                          error_code="TOOL_DISABLED", effect_outcome="not_started")
            return execute(params)

        tool.execute = checked
        return tool


# LLM: 配置不执行工具，但原安装锁内最终提交仍需实时资格；不复制锁、CAS 或提交算法。
# 类用途: 在原安装 Store 的配置提交边界附加本次调用权限核验。
class _ConfigurationStore(PluginInstallStore):
    # LLM: owner 从原管理上下文取得，不接受客户端自报。
    # 函数用途: 将一次配置调用绑定到原 owner Store。
    def __init__(self, owner, call, plugin_id):
        super().__init__(owner)
        self.call, self.plugin_id = call, plugin_id

    # LLM: 该方法由原 _update_locked 在原安装锁中调用，授权失败先于任何配置写入。
    # 函数用途: 在安装表原子写前重核资格与原生 configure_allowed 策略。
    def _commit(self, before, after, text, receipt):
        _write_check(self.call, "plugin_configure", self.plugin_id)
        return super()._commit(before, after, text, receipt)


# LLM: 普通本地已登记用户保留本人私有管理；main 的 HTTP 管理角色撤销不能由路径补回。
# 函数用途: 在真实写入口重读原生资格、开关、工具策略及本人安装归属。
def _write_check(call, tool_name, plugin_id):
    from ..gateway_parts.business_api_v13 import v13_require_admin

    manager = _native_management(call)
    if manager.context.owner.identity == OwnerIdentity.local_main() and not manager.context.is_admin:
        v13_require_admin(call)
    if not manager._allowed(tool_name):
        raise HostAPIError("BUSINESS_ACCESS_DENIED", 403, "not_started", "原生插件管理策略拒绝本次操作。")
    if plugin_id:
        entry = _own_installation(manager, plugin_id)
        if entry is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
        if not manager.context.is_admin and entry.installation_scope != "private":
            raise HostAPIError("BUSINESS_ACCESS_DENIED", 403, "not_started", "只能管理本人私有插件。")
    return True


# LLM: home_paths.owner_id 是展示用规范串（local/main、providers/...），不能直接当 OwnerIdentity.owner_id。
# 函数用途: 从已绑定 home_paths 还原可解析的规范 owner 身份。
def _identity_from_paths(paths):
    provider = str(getattr(paths, "owner_provider", "") or "")
    kind = str(getattr(paths, "owner_kind", "") or "")
    display = str(getattr(paths, "owner_id", "") or "")
    if not (provider and kind and display):
        return None
    if provider == "local" and kind == "main":
        return OwnerIdentity.local_main()
    leaf = display.rsplit("/", 1)[-1]
    if provider == "local" and kind == "user":
        return OwnerIdentity.local_user(leaf)
    if kind == "user":
        return OwnerIdentity.provider_user(provider, leaf)
    if kind == "group":
        return OwnerIdentity.provider_group(provider, leaf)
    return None


# LLM: 配置和启停只改本人私有安装；他人或公共安装不能用本 owner 的写入口修改。
# 函数用途: 读取当前 owner 安装表中的一条记录。
def _own_installation(manager, plugin_id):
    for row in manager.installations.snapshot():
        if row.manifest.plugin_id == plugin_id:
            return row
    return None


# LLM: 目录允许普通用户看到 main 已公开条目，但只读且不能据此获得管理权。
# 函数用途: 在本人私有记录之后合入可见的公共安装记录。
def _visible_installation(manager, plugin_id):
    own = _own_installation(manager, plugin_id)
    if own is not None:
        return own
    owner = manager.context.owner
    if owner.identity == OwnerIdentity.local_main() or owner.identity.owner_kind != "user":
        return None
    from ..plugin_install_store import PluginInstallStore

    admin = resolve_owner_home(owner.root, OwnerIdentity.local_main())
    if not admin.plugins_dir.is_dir():
        return None
    for row in PluginInstallStore(admin).snapshot():
        if row.manifest.plugin_id == plugin_id and row.installation_scope == "public":
            return row
    return None


# LLM: 配置 schema 来自已安装清单的静态声明，缺声明按空对象投影，不从页面猜测字段。
# 函数用途: 返回插件的设置 schema。
def _settings_schema(entry):
    schema = entry.manifest.settings_schema if entry is not None else {}
    return schema if isinstance(schema, dict) else {}


# LLM: 分类只是展示归类，不是权限或执行判断；按清单声明文本归到已知类别，未知保留 other。
# 函数用途: 从插件声明推导目录分类。
def _category(plugin_id, summary, manifest):
    words = [plugin_id, summary]
    if manifest is not None:
        words.extend(action.name for action in manifest.actions)
        words.extend(tool.name for tool in manifest.tools)
        if manifest.capability is not None:
            words.extend(manifest.capability.keywords)
            words.append(manifest.capability.description)
    text = " ".join(word for word in words if isinstance(word, str)).casefold()
    groups = (
        (("feishu", "slack", "teams", "pagerduty", "webhook", "channel", "notify", "chat"), "channel"),
        (("misp", "otx", "abuseipdb", "intel", "threat", "indicator"), "intel"),
        (("respond", "response", "contain", "isolate", "quarantine", "block"), "response"),
    )
    for names, label in groups:
        if any(name in text for name in names):
            return label
    return "other"


# LLM: 安装记录是版本、激活代次和配置 schema 的唯一事实源；投影不读取包内容或 secret。
# 函数用途: 把一条原生安装记录投影为 V13PluginView。
def _view_from_installation(entry):
    manifest = entry.manifest
    schema = _settings_schema(entry)
    return {
        "plugin_id": manifest.plugin_id,
        "name": manifest.plugin_id,
        "version": manifest.version,
        "enabled": bool(entry.enabled),
        "installation_revision": entry.revision,
        "installation_ref": entry.installation_ref or None,
        "activation_id": entry.activation_id or None,
        "has_config_schema": bool(schema.get("properties")),
        "category": _category(manifest.plugin_id, manifest.summary, manifest),
    }


# LLM: 目录快照缺少 manifest 时仍如实投影可读身份，不补造安装代次或配置能力。
# 函数用途: 把原生命令声明投影为 V13PluginView。
def _view_from_command(manager, plugin):
    entry = _visible_installation(manager, plugin.plugin_id)
    if entry is not None:
        view = _view_from_installation(entry)
        view["version"] = plugin.package_version or view["version"]
        view["enabled"] = bool(plugin.enabled)
        view["installation_revision"] = plugin.installation_revision
        view["installation_ref"] = plugin.installation_ref or None
        view["activation_id"] = plugin.activation_id or None
        return view
    return {
        "plugin_id": plugin.plugin_id,
        "name": plugin.plugin_id,
        "version": plugin.package_version or "0",
        "enabled": bool(plugin.enabled),
        "installation_revision": plugin.installation_revision,
        "installation_ref": plugin.installation_ref or None,
        "activation_id": plugin.activation_id or None,
        "has_config_schema": False,
        "category": _category(plugin.plugin_id, plugin.summary, None),
    }


# LLM: 同片包模块需要同一安装记录投影；这里只适配形状，不授予读取配置正文的能力。
# 函数用途: 供兄弟模块把原生安装记录投影为 V13PluginView。
def plugin_view(raw):
    if hasattr(raw, "manifest"):
        return _view_from_installation(raw)
    return None


# LLM: 兄弟模块复用同一原生管理服务，避免各自复制策略、审批或安装表。
# 函数用途: 返回当前调用身份下的原生 PluginManagement。
def management(call):
    return _native_management(call)


# LLM: schema 里的 password/x-secret 是显式秘密声明；名称启发式只扩大遮罩范围，不会放宽。
# 函数用途: 递归收集设置 schema 中的秘密字段路径。
def _collect_secret_paths(schema, prefix, found):
    if not isinstance(schema, dict):
        return
    if schema.get("format") == "password" or schema.get("x-secret") is True:
        found.add(prefix.rstrip("."))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            if not isinstance(name, str):
                continue
            path = prefix + name
            explicit = isinstance(child, dict) and (
                child.get("format") == "password" or child.get("x-secret") is True)
            if explicit or _looks_secret(name):
                found.add(path)
            _collect_secret_paths(child, path + ".", found)
    items = schema.get("items")
    if isinstance(items, dict):
        _collect_secret_paths(items, prefix.rstrip(".") + "[].", found)


# LLM: 只按名称模式识别秘密，不读取值或正文；keyword 不是秘密字段。
# 函数用途: 判断一个设置字段名是否应按秘密处理。
def _looks_secret(name):
    lower = name.casefold()
    if "keyword" in lower:
        return False
    return any(word in lower for word in ("secret", "token", "password", "passwd", "key"))


# LLM: 数组元素用 [] 区分容器与元素，元素自身声明和对象属性使用同一规范路径。
# 函数用途: 返回当前 schema 的秘密字段路径集合。
def _secret_paths(schema):
    found = set()
    _collect_secret_paths(schema, "", found)
    return found


# LLM: 非空秘密值统一替换为 ***；空值保留空串，避免把"未设置"误显示为已设置。
# 函数用途: 按 schema 递归脱敏一份配置值。
def _mask_value(value, schema, prefix, secrets):
    if prefix.rstrip(".") in secrets and value not in (None, ""):
        return "***"
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        result = {}
        for key, item in value.items():
            path = prefix + str(key)
            child = properties.get(key) if isinstance(properties, dict) else None
            if path in secrets and item not in (None, ""):
                result[key] = "***"
            else:
                result[key] = _mask_value(item, child, path + ".", secrets)
        return result
    if isinstance(value, list):
        items = schema.get("items") if isinstance(schema, dict) else None
        return [_mask_value(item, items, prefix.rstrip(".") + "[].", secrets) for item in value]
    return value


# LLM: 回传 *** 表示沿用旧值；没有旧值时必须拒绝，绝不能把占位符写成真实配置。
# 函数用途: 把脱敏回传值合并到已存配置上。
def _merge_value(incoming, stored, schema, prefix, secrets):
    if prefix.rstrip(".") in secrets and incoming == "***":
        if stored not in (None, ""):
            return stored
        raise HostAPIError("INVALID_SCHEMA", 400, "not_started", "脱敏占位没有可保留的旧值。")
    if isinstance(incoming, dict):
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        result = {}
        for key, item in incoming.items():
            path = prefix + str(key)
            child = properties.get(key) if isinstance(properties, dict) else None
            if path in secrets and item == "***":
                if isinstance(stored, dict) and key in stored and stored[key] not in (None, ""):
                    result[key] = stored[key]
                else:
                    raise HostAPIError("INVALID_SCHEMA", 400, "not_started", "脱敏占位没有可保留的旧值。")
            elif isinstance(item, (dict, list)):
                base = stored.get(key) if isinstance(stored, dict) else None
                result[key] = _merge_value(item, base, child, path + ".", secrets)
            else:
                result[key] = item
        return result
    if isinstance(incoming, list):
        base_list = stored if isinstance(stored, list) else []
        items = schema.get("items") if isinstance(schema, dict) else None
        return [_merge_value(item, base_list[index] if index < len(base_list) else None,
                             items, prefix.rstrip(".") + "[].", secrets)
                for index, item in enumerate(incoming)]
    return incoming


# LLM: 已保存配置来自原安装表且已被原生 canonical 校验；损坏时明确未就绪而非空成功。
# 函数用途: 读取一条安装记录里的配置对象。
def _stored_values(entry):
    if entry is None or not entry.settings_json:
        return {}
    try:
        value = json.loads(entry.settings_json)
    except (TypeError, ValueError):
        raise HostAPIError("HOST_NOT_READY", 503, "not_started", "插件配置存储不可读。") from None
    return value if isinstance(value, dict) else {}


# LLM: 包源是宿主绑定的外部目录，只投影公开索引字段；缺绑定不是空目录而是 partial。
# 函数用途: 把包源返回的一项投影为 V13PackageIndexItem。
def _index_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("item")
    required = ("package_id", "title", "versions", "latest_version")
    if any(key not in raw for key in required):
        raise ValueError("item")
    kind = raw.get("kind", "plugin")
    if kind != "plugin" or not isinstance(raw["versions"], list):
        raise ValueError("item")
    return {
        "package_id": str(raw["package_id"]),
        "title": str(raw["title"]),
        "summary": str(raw.get("summary", "")),
        "kind": "plugin",
        "versions": [str(item) for item in raw["versions"]][:100],
        "latest_version": str(raw["latest_version"]),
        "compliance_frameworks": [str(item) for item in raw.get("compliance_frameworks", [])][:20],
        "installed_version": raw.get("installed_version"),
    }


# LLM: 缺失或坏包源只降覆盖，不伪造可安装目录。
# 函数用途: 从已绑定 package_source 读取插件可安装项与覆盖口径。
def _installable(call):
    providers = getattr(call.context.host, "business_v13_providers", None)
    provider = providers.get("package_source") if isinstance(providers, dict) else None
    if provider is None or not callable(getattr(provider, "index", None)):
        return [], "partial"
    try:
        page = provider.index(query="", kind="plugin", cursor=None, limit=50)
    except (OSError, ValueError, TypeError, RuntimeError):
        return [], "partial"
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        return [], "partial"
    items = []
    for raw in page["items"][:100]:
        try:
            items.append(_index_item(raw))
        except (KeyError, TypeError, ValueError):
            continue
    coverage = "complete" if len(items) == len(page["items"]) and page.get("next_cursor") is None else "partial"
    return items, coverage


# LLM: 原生 Store 也要配额锁，不能在 V13 quota 事务内调用；先耐久占位再执行，未知占位绝不自动重放。
# 函数用途: 用同一 plugin_operations 保存操作占位与脱敏回执，不持 V13 锁执行原生副作用。
def _receipted(call, mutate):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    store = v13_store_for(call)
    identity = _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])
    fingerprint = _digest([call.action, call.params, call.expected_revision])
    claim = uuid4().hex

    # LLM: claim 只标记本次执行者，不把参数或秘密存入回执；同键异参由原 Store 拒绝。
    # 函数用途: 在原配额与目录锁内耐久占用一次操作。
    def reserve(records):
        records[identity] = {"state": "pending", "claim": claim}
        return {"claim": claim}

    reserved = store.transaction("plugin_operations", reserve,
                                 idempotency_key="reserve-" + identity, params_digest=fingerprint)
    if reserved["claim"] != claim:
        previous = store.get("plugin_operations", identity)
        if previous and previous.get("state") == "completed":
            return previous["result"]
        if previous and previous.get("state") == "failed":
            error = previous["error"]
            raise HostAPIError(error["code"], error["status"], error["outcome"], "原插件操作已结束。")
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原插件操作尚未确认，禁止重复执行。")

    # LLM: 保存只含脱敏结果或协议错误字段；若此提交失败则 pending 保留，下一请求明确 unknown。
    # 函数用途: 在副作用退出后结束当前 claim，不改安装事实或重新执行。
    def finish(state, result):
        # LLM: 同一 claim 才能结束占位，避免晚到写入覆盖另一操作。
        # 函数用途: 将当前占位单调变为终态。
        def commit(records):
            if records.get(identity) != {"state": "pending", "claim": claim}:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原操作占位已变化。")
            records[identity] = {"state": state, "claim": claim, **result}
            return {"saved": True}

        return store.transaction("plugin_operations", commit,
                                 idempotency_key="finish-" + identity, params_digest=fingerprint)

    try:
        result = mutate({})
    except HostAPIError as error:
        finish("failed", {"error": {"code": error.code, "status": error.status, "outcome": error.outcome}})
        raise
    except Exception:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "原插件操作结果尚未确认。") from None
    try:
        finish("completed", {"result": result})
    except Exception:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "插件操作已有结果，但回执尚未确认。") from None
    return result


# LLM: 安装领域原因到公开错误码的映射只表达真实提交事实，未知提交不伪装未开始。
# 函数用途: 把 PluginInstallationError 转成 V13 HostAPIError。
def _installation_error(error):
    reason = getattr(error, "reason", "")
    commit_state = getattr(error, "commit_state", "")
    if commit_state == "unknown":
        return HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "插件操作结果尚未确认。")
    mapping = {
        "plugin_missing": ("RESOURCE_NOT_FOUND", 404),
        "permission_denied": ("BUSINESS_ACCESS_DENIED", 403),
        "revision_conflict": ("REVISION_CONFLICT", 409),
        "operation_conflict": ("IDEMPOTENCY_CONFLICT", 409),
        "invalid_settings": ("INVALID_SCHEMA", 400),
        "package_conflict": ("STATE_CONFLICT", 409),
        "activation_unsettled": ("STATE_CONFLICT", 409),
        "owner_conflict": ("RESOURCE_NOT_FOUND", 404),
        "quota_unavailable": ("HOST_NOT_READY", 503),
        "storage_unavailable": ("HOST_NOT_READY", 503),
    }
    code, status = mapping.get(reason, ("STATE_CONFLICT", 409))
    outcome = "committed" if commit_state == "committed" else "not_started"
    return HostAPIError(code, status, outcome, "插件操作未成功完成。")


# LLM: 原生回执的 state/error_code 是唯一判断依据，文案只用于说明。
# 函数用途: 把原生管理回执映射为 V13 错误。
def _lifecycle_error(outcome):
    code = outcome.get("error_code") if isinstance(outcome, dict) else ""
    if isinstance(outcome, dict) and (outcome.get("state") == "outcome_unknown"
                                     or outcome.get("effect_outcome") == "unknown"):
        return HostAPIError("OUTCOME_UNKNOWN", 503, "unknown", "插件操作结果尚未确认。")
    if code in {"PLUGIN_PERMISSION_DENIED", "COMMAND_ADMIN_REQUIRED", "TOOL_DISABLED",
                "PLUGIN_DISABLED", "CAPABILITY_DENIED"}:
        return HostAPIError("BUSINESS_ACCESS_DENIED", 403, "not_started", "当前身份不能执行该插件操作。")
    if code in {"RESOURCE_NOT_FOUND", "PLUGIN_NOT_FOUND", "plugin_missing"}:
        return HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
    if code in {"INVALID_COMMAND_ARGUMENTS", "INVALID_SCHEMA"}:
        return HostAPIError("INVALID_SCHEMA", 400, "not_started", "插件操作参数不符合要求。")
    state = outcome.get("state") if isinstance(outcome, dict) else ""
    if state in {"approval_required", "pending", "running"}:
        return HostAPIError("STATE_CONFLICT", 409, "not_started", "插件操作需要原生确认或尚未完成。")
    return HostAPIError("STATE_CONFLICT", 409, "not_started", "插件操作未成功完成。")


# LLM: 生命周期只走原 package_lifecycle；确认未完成时返回冲突而不是伪造启用成功。
# 函数用途: 执行插件启用或停用并返回当前安装投影。
def _lifecycle(call, action):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    plugin_id = call.params["plugin_id"]
    if _own_installation(_native_management(call), plugin_id) is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")

    # LLM: 事务内重建原生上下文，确保使用提交时的 is_admin/登记状态，而不是请求开始快照。
    # 函数用途: 在原管理服务上执行一次启停并投影结果。
    def mutate(records):
        v13_require_access(call)
        _write_check(call, "plugin_" + action, plugin_id)
        manager = _native_management(call)
        try:
            current, outcome = manager.package_lifecycle(plugin_id, action, operation_id=_operation_id(call))
        except RuntimeConflictError:
            raise HostAPIError("IDEMPOTENCY_CONFLICT", 409, "not_started", "同一幂等键已用于其它插件输入。") from None
        except PluginInstallationError as error:
            raise _installation_error(error) from None
        if current is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
        state = outcome.get("state") if isinstance(outcome, dict) else ""
        if state != "succeeded":
            raise _lifecycle_error(outcome)
        if action == "enable" and not current.enabled:
            raise HostAPIError("STATE_CONFLICT", 409, "not_started", "插件启用尚未确认生效。")
        if action == "disable" and current.enabled:
            raise HostAPIError("STATE_CONFLICT", 409, "not_started", "插件停用尚未确认生效。")
        return {"plugin": _view_from_installation(current)}

    return _receipted(call, mutate)


# LLM: 插件目录同时给出已安装项和宿主包源的可安装项；包源缺失只降覆盖口径。
# 函数用途: 实现 plugins.catalog。
def catalog(call):
    manager = _native_management(call)
    native = manager.catalog()
    plugins = [_view_from_list(manager, plugin) for plugin in native.plugins][:500]
    installable, coverage = _installable(call)
    actions = [{"name": action.name, "available": bool(action.available),
                "unavailable_reason": action.unavailable_reason or None}
               for action in native.management_actions][:20]
    return {"plugins": plugins, "installable": installable[:200], "management_actions": actions,
            "coverage": coverage}


# LLM: 列表复用同一 catalog 快照和同一投影，不额外读取包目录或安装文件。
# 函数用途: 实现 plugins.list 的目录投影。
def list_plugins(call):
    manager = _native_management(call)
    native = manager.catalog()
    return {"items": [_view_from_list(manager, plugin) for plugin in native.plugins][:500]}


# LLM: 启用/停用只走原 package_lifecycle，不接受客户端自报的审批或目标状态。
# 函数用途: 实现 plugins.enable。
def enable(call):
    return _lifecycle(call, "enable")


# LLM: 停用复用原生命周期，不自行删除环境或改变激活状态。
# 函数用途: 实现 plugins.disable。
def disable(call):
    return _lifecycle(call, "disable")


# LLM: 配置读取按静态 schema 脱敏；秘密字段只回 ***，空值不回填占位。
# 函数用途: 实现 plugins.config_get。
def config_get(call):
    manager = _native_management(call)
    plugin_id = call.params["plugin_id"]
    entry = _own_installation(manager, plugin_id)
    if entry is None:
        if _visible_installation(manager, plugin_id) is not None:
            raise HostAPIError("BUSINESS_ACCESS_DENIED", 403, "not_started", "只能读取本人插件的配置。")
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
    schema = _settings_schema(entry)
    secrets = _secret_paths(schema)
    values = _mask_value(_stored_values(entry), schema, "", secrets)
    return {"plugin_id": plugin_id, "config_schema": schema, "values": values,
            "secret_fields": sorted(secrets)[:100], "revision": entry.revision}


# LLM: 更新走原 configure 的 schema 校验、expected_revision CAS 和 op_key 回执；
# 回传 *** 从已存值恢复，绝不把占位符写进配置。
# 函数用途: 实现 plugins.config_update。
def config_update(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    manager = _native_management(call)
    plugin_id = call.params["plugin_id"]
    entry = _own_installation(manager, plugin_id)
    if entry is None:
        if _visible_installation(manager, plugin_id) is not None:
            raise HostAPIError("BUSINESS_ACCESS_DENIED", 403, "not_started", "只能修改本人插件的配置。")
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
    schema = _settings_schema(entry)
    secrets = _secret_paths(schema)
    merged = _merge_value(call.params["values"], _stored_values(entry), schema, "", secrets)
    try:
        canonical = canonical_plugin_settings(merged, schema)
    except (TypeError, ValueError, RecursionError):
        raise HostAPIError("INVALID_SCHEMA", 400, "not_started", "插件配置不符合声明。") from None

    # LLM: 事务内重读权威记录，CAS 与 schema 都由原安装服务裁决。
    # 函数用途: 执行一次原生 configure 并返回脱敏后的新配置。
    def mutate(records):
        v13_require_access(call)
        _write_check(call, "plugin_configure", plugin_id)
        try:
            result = _ConfigurationStore(manager.context.owner, call, plugin_id).configure(PluginConfigureRequest(
                plugin_id, entry.package_sha256, _operation_id(call), call.expected_revision, canonical))
        except PluginInstallationError as error:
            raise _installation_error(error) from None
        current = result.installation
        return {"plugin_id": plugin_id, "values": _mask_value(_stored_values(current), schema, "", secrets),
                "revision": current.revision}

    return _receipted(call, mutate)


# LLM: 只认已安装清单里声明且可用的 test/health 工具动作；display 类动作不执行。
# 函数用途: 返回插件声明的连接探测动作。
def _declared_probe(entry):
    for action in entry.manifest.actions:
        if action.name in {"test", "health"} and action.kind == "tool" and action.available:
            return action
    return None


# LLM: 原生飞书适配器只在宿主实际暴露 registry 时使用，不从插件 ID 猜通道或伪造发送。
# 函数用途: 解析插件对应的原生通道适配器。
def _channel_adapter(host, plugin_id):
    registry = getattr(host, "channel_registry", None)
    if registry is None:
        registry = getattr(getattr(host, "agent", None), "channel_registry", None)
    if registry is None or not callable(getattr(registry, "adapter_for", None)):
        return None
    try:
        adapter = registry.adapter_for(plugin_id)
    except (AttributeError, KeyError, ValueError, RuntimeError):
        return None
    if adapter is None or not callable(getattr(adapter, "send_message", None)):
        return None
    return adapter


# LLM: 连接结果只按原生 state/error_code 判断；命令返回成功文案但 state 非 succeeded 仍算失败。
# 函数用途: 执行声明的 test/health 动作并结构化投影结果。
def _declared_probe_result(call, manager, entry, action):
    started = time.monotonic()
    try:
        outcome = manager.command("/plugins@" + entry.manifest.plugin_id + " " + action.name,
                                  revision=manager.catalog().revision,
                                  request_id=_operation_id(call) + "-probe")
    except RuntimeConflictError:
        raise HostAPIError("IDEMPOTENCY_CONFLICT", 409, "not_started", "同一幂等键已用于其它探测输入。") from None
    latency = round((time.monotonic() - started) * 1000.0, 3)
    state = outcome.get("state") if isinstance(outcome, dict) else ""
    ok = state == "succeeded" and outcome.get("ok") is not False
    code = None if ok else (outcome.get("error_code") if isinstance(outcome, dict) else None) or "PROBE_FAILED"
    return {"plugin_id": entry.manifest.plugin_id, "ok": ok, "latency_ms": latency,
            "error_code": code,
            "detail": "已执行插件声明的 test/health 入口。" if ok else "插件声明的 test/health 入口未成功完成。",
            "tested_at": _clock_text(call)}


# LLM: 适配器 send_message 返回 True 才算送达；异常或其它返回不转成成功。
# 函数用途: 通过原生通道适配器执行一次显式测试发送。
def _adapter_probe_result(call, entry, adapter):
    from ..adapter.protocol import OutgoingMessage

    target = call.params.get("target") or call.subject_id or "owner"
    message = OutgoingMessage(
        channel=entry.manifest.plugin_id, user_id=str(target),
        content="【连通性测试】由 plugins.test_connection 明确触发的测试消息。",
        metadata={"delivery_idempotency_key": _operation_id(call)},
    )
    started = time.monotonic()
    try:
        delivered = adapter.send_message(target, message)
    except Exception:
        delivered = False
    latency = round((time.monotonic() - started) * 1000.0, 3)
    ok = delivered is True
    return {"plugin_id": entry.manifest.plugin_id, "ok": ok, "latency_ms": latency,
            "error_code": None if ok else "CHANNEL_DELIVERY_FAILED",
            "detail": "原生通道适配器已确认送达。" if ok else "原生通道适配器未确认送达。",
            "tested_at": _clock_text(call)}


# LLM: 没有声明入口也没有宿主通道适配器时如实返回 TEST_UNSUPPORTED，不用假成功占位。
# 函数用途: 实现 plugins.test_connection。
def test_connection(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    manager = _native_management(call)
    entry = _visible_installation(manager, call.params["plugin_id"])
    if entry is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404, "not_started", "插件未安装或不属于当前用户。")
    # LLM: 探测属于外部副作用，按幂等键复用同一原生命令/发送操作。
    # 函数用途: 在耐久回执内执行一次真实探测。
    def mutate(records):
        v13_require_access(call)
        declared = _declared_probe(entry)
        if declared is not None:
            return _declared_probe_result(call, _native_management(call), entry, declared)
        with ExitStack() as stack:
            adapter = _channel_adapter(call.context.host, entry.manifest.plugin_id)
            if adapter is None:
                host = call.services.background._complete_host(call.context, stack)
                if host is not None:
                    adapter = _channel_adapter(host, entry.manifest.plugin_id)
            v13_require_access(call)
            if adapter is not None:
                return _adapter_probe_result(call, entry, adapter)
            return {"plugin_id": entry.manifest.plugin_id, "ok": False, "latency_ms": None,
                    "error_code": "TEST_UNSUPPORTED",
                    "detail": "该插件未声明 test/health 入口，也没有可用的原生通道适配器。",
                    "tested_at": _clock_text(call)}

    return _receipted(call, mutate)


# LLM: 目录投影优先使用安装记录，其次才用命令声明；不把未安装项伪装成已安装。
# 函数用途: 把一条原生命令声明投影为 V13PluginView。
def _view_from_list(manager, plugin):
    return _view_from_command(manager, plugin)
