# LLM: 仅显式离线迁移消费旧键；不自动读取环境或旧文件、不创建第二套凭据仓库，不在运行时回退旧配置。
# 模块用途: 复用 provider 记录保存和解析准确环境引用，并提供不隐式落盘的旧来源迁移。
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from .config_layers import ConfigLayer, compact_policy_percent


# LLM: 只认准确 env 引用，不允许文件、命令、URL 或表达式；报错不回显输入。
# 函数用途: 从已保存 provider 引用中取得合法环境名。
def provider_environment_name(reference: object) -> str:
    from .model_provider_schema import ModelProfileError

    if reference in (None, ""):
        return ""
    if type(reference) is not str or not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]{0,254}", reference):
        raise ModelProfileError("服务商凭据引用无效，只接受准确环境变量引用。")
    return reference[4:]


# LLM: 运行时只解析 provider 自己的单个引用，空环境不转读静态凭据或其他来源；不落盘、不打印秘密。
# 函数用途: 为实际请求获取 provider 的唯一凭据值，保留原静态记录的文本语义。
def provider_api_key(provider: Mapping, *, environ: Mapping | None = None) -> str:
    import os

    name = provider_environment_name(provider.get("api_key_ref"))
    if not name:
        return provider.get("api_key", "")
    environment = os.environ if environ is None else environ
    value = environment.get(name, "")
    return value.strip() if type(value) is str else ""


# LLM: 仅显式离线迁移使用，environ 由宿主提供；非空环境只存引用且不保留未采用的静态值，不建新仓库。
# 函数用途: 把原静态值与环境名称转换为可以存回原 provider 的凭据字段。
def legacy_provider_credential(inline: str, environment_name: str, *, environ: Mapping) -> dict:
    from .model_provider_schema import ModelProfileError

    if type(inline) is not str or type(environment_name) is not str:
        raise ModelProfileError("旧模型凭据配置格式无效。")
    name = environment_name.strip()
    if name:
        provider_environment_name("env:" + name)
        value = environ.get(name, "")
        if type(value) is str and value.strip():
            return {"api_key": "", "api_key_ref": "env:" + name}
    return {"api_key": inline}


# LLM: 输入必须是调用者显式读取的原始层，不注入 schema 默认；同层新键优先、跨层顺序完全保留。
# 函数用途: 纯内存转换旧压缩比例，返回独立的新来源层，不修改输入或写盘。
def migrate_legacy_compact_layers(layers: list[ConfigLayer] | tuple[ConfigLayer, ...]) -> list[ConfigLayer]:
    migrated = []
    for layer in layers:
        values = dict(layer.values)
        if "memory_compact_auto_trigger_percent" in values:
            legacy = values.pop("memory_compact_auto_trigger_percent")
            if "memory_compact_trigger_percent" not in values:
                if type(legacy) is str and legacy.strip().isascii() and legacy.strip().lstrip("+-").isdigit():
                    legacy = int(legacy)
                if type(legacy) is not int:
                    raise ValueError("旧压缩配置类型无效，未迁移。")
                values["memory_compact_trigger_percent"] = compact_policy_percent(legacy, 80)
        migrated.append(replace(layer, values=values))
    return migrated


# LLM: 仅离线迁移需要原字段名；运行默认、schema 和投影不得调用此映射读取旧配置。
# 函数用途: 描述二十五个旧平铺字段到原模型目录决策覆盖的准确对应关系。
def decision_config_fields() -> dict[str, tuple[str, str]]:
    from .decision_settings_schema import GENERAL_FIELDS, POINTS, decision_point_fields

    result = {key: ("agent", "decision_" + key) for key in GENERAL_FIELDS}
    for point in POINTS:
        domain = "capability" if point in {"subagent_model", "skill_tool"} else "memory" if point in {"pre_recall", "recall", "curator", "curator_relation"} else "agent"
        prefix = "memory_decision_" if domain == "memory" else "decision_"
        for name in decision_point_fields(point):
            if name not in {"timeout_seconds", "profile_id"}:
                result[f"points.{point}.{name}"] = (domain, f"{prefix}{point}_{name}")
    return result


# LLM: 只迁移显式值，不读取 runtime context 或补默认；严格 schema 拒绝含糊开关、非有限期限和无效点位。
# 函数用途: 从已显式读取的两份旧 YAML 得到可并入 catalog 的决策覆盖，不修改输入或写盘。
def migrate_legacy_decision_overrides(agent_values: dict, capability_values: dict) -> dict:
    from .decision_settings_schema import validate_decision_field

    overrides = {}
    for path, (domain, key) in decision_config_fields().items():
        source = capability_values if domain == "capability" else agent_values
        if key not in source:
            continue
        value = source[key]
        if path.endswith("timeout_seconds") and type(value) is str and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
            value = float(value)
        overrides[path] = validate_decision_field(path, value)
    return overrides


# LLM: 输入是显式读取的旧原始值，不是展开后的 runtime 配置；旧窗口0只在这里转null，不补厂商/型号或默认容量。
# 函数用途: 将十六个旧连接字段校验成原 provider 和模型记录，完全未配置时不制造模型。
def _legacy_model_records(raw: dict, *, provider_id: str, environ: Mapping) -> tuple[dict, dict] | None:
    from .model_provider_schema import ModelProfileError, validate_model, validate_provider

    if raw.get("model_backend") == "echo":
        if any(raw.get(key) for key in ("api_key_env", "model_custom_headers", "model_session_header")):
            raise ModelProfileError("离线模型不能包含环境凭据或网络请求头配置。")
        provider = validate_provider({"transport": "offline", "display_name": "显式离线 Echo",
                                      **{key: raw[key] for key in ("api_base", "api_key") if key in raw}})
        model = {"provider_id": provider_id, "model_name": raw.get("model_name") or "echo", "model_backend": "echo", "capability": "agentic"}
    else:
        credential = legacy_provider_credential(raw.get("api_key", ""), raw.get("api_key_env", ""), environ=environ)
        identity = (raw.get("model_backend", ""), raw.get("api_base", ""), raw.get("model_name", ""))
        if not any(identity) and not credential.get("api_key") and not credential.get("api_key_ref"):
            return None
        if not all(identity):
            raise ModelProfileError("旧模型连接不完整，请先核对接口类型、基础地址和模型名；未写入目录。")
        provider = validate_provider({"display_name": "旧部署模型", "api_base": identity[1], **credential,
                                      "custom_headers": raw.get("model_custom_headers", {}),
                                      "session_header": raw.get("model_session_header", ""), "capabilities": ["agentic"]})
        model = {"provider_id": provider_id, "model_backend": identity[0], "model_name": identity[2], "capability": "agentic"}
    for old, new in (("model_context_window_tokens", "model_context_window_tokens"),
                     ("model_queue_wait_seconds", "model_queue_wait_seconds"),
                     ("model_input_modalities", "input_modalities"), ("temperature", "temperature"), ("top_p", "top_p"),
                     ("model_reasoning_control", "reasoning_control"), ("model_reasoning_levels", "reasoning_levels"),
                     ("model_reasoning_update_items", "reasoning_update_items"), ("model_structured_output", "structured_output")):
        if old in raw:
            model[new] = raw[old]
    window = model.get("model_context_window_tokens")
    if window == [] or type(window) is int and window == 0 or type(window) is str and window.strip() in {"", "0", "null"}:
        model["model_context_window_tokens"] = None
    return provider, validate_model(model)


# LLM: 仅完整宿主管理员显式离线调用；不删除旧源、不新建凭据仓库，目录锁内检查真实代次，当前用户选择和决策覆盖优先。
# 函数用途: 将旧模型与决策原子导入原目录，返回不含秘密的结果；同输入重跑不改字节或目录代次。
def import_legacy_owner_configuration(home_paths, *, agent_values: dict, capability_values: dict,
                                      environ: Mapping, expected_generation: str | None) -> dict:
    from uuid import NAMESPACE_URL, uuid5

    from ..common.global_write_guard import require_global_writer
    from ..common.json_io import locked_json_path
    from ..common.nofollow_fs import ensure_private_dir
    from ..user_space.home_layout import MyAgentHomePaths
    from ..user_space.owner_access import is_complete_local_admin_owner
    from .model_profiles import _save_profiles, model_profiles_path, read_model_profiles
    from .model_provider_schema import ModelProfileError, validate_catalog_generation

    if not isinstance(home_paths, MyAgentHomePaths) or not is_complete_local_admin_owner(home_paths):
        raise ModelProfileError("旧部署导入仅限完整宿主管理员显式离线执行。", reason="migration_forbidden")
    require_global_writer("model_catalog_migration")
    if type(agent_values) is not dict or type(capability_values) is not dict or not isinstance(environ, Mapping):
        raise ModelProfileError("导入必须提供明确读取的旧配置映射和环境快照。")
    if expected_generation is not None:
        validate_catalog_generation(expected_generation)
    path = model_profiles_path(home_paths)
    profile_id = str(uuid5(NAMESPACE_URL, "my-agent:e905:legacy-model:" + str(path.resolve())))
    provider_id = "legacy-" + profile_id
    records = _legacy_model_records(agent_values, provider_id=provider_id, environ=environ)
    overrides = migrate_legacy_decision_overrides(agent_values, capability_values)
    if records is None and not overrides:
        return {"changed": False, "profile_id": None, "decision_fields": []}
    ensure_private_dir(path.parent)
    with locked_json_path(path):
        data = read_model_profiles(path)
        if data["catalog_generation"] != expected_generation:
            raise ModelProfileError("模型目录已改变，未执行旧配置导入。", reason="catalog_conflict")
        changed = _merge_legacy_catalog(data, records, profile_id, provider_id, overrides, environ=environ)
        if changed:
            _save_profiles(path, data)
        return {"changed": changed, "profile_id": profile_id if records else None,
                "catalog_generation": data["catalog_generation"], "decision_fields": sorted(overrides)}


# LLM: 仅由已认证且持原目录锁的离线导入调用；已有用户改动不覆盖，不从模型名称推断相同连接或认证。
# 函数用途: 合并已校验的旧记录和决策覆盖，准确报告是否需要一次保存。
def _merge_legacy_catalog(data: dict, records: tuple[dict, dict] | None, profile_id: str, provider_id: str,
                          overrides: dict, *, environ: Mapping) -> bool:
    from .model_provider_schema import ModelProfileError

    changed = False
    if records is not None:
        provider, model = records
        previous_provider, previous_model = data["providers"].get(provider_id), data["profiles"].get(profile_id)
        if previous_provider is not None and previous_provider != provider or previous_model is not None and previous_model != model:
            raise ModelProfileError("原导入目标已有改动，未覆盖用户模型设置。", reason="migration_conflict")
        if previous_model is None:
            data["providers"][provider_id], data["profiles"][profile_id] = provider, model
            if data["selected"] == "default" and (provider.get("transport") == "offline" or provider_api_key(provider, environ=environ)):
                data["selected"] = profile_id
            changed = True
    current = data["decision_settings"]
    merged = {**overrides, **current["overrides"]}
    if merged != current["overrides"]:
        data["decision_settings"] = {**current, "overrides": merged, "revision": current["revision"] + 1}
        changed = True
    return changed
