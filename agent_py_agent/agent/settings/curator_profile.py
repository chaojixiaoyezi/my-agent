# LLM: 后台模型只复用原 selected_model_config 的整组连接解析（按 agent.home_paths 读该 owner 自己的目录，含管理员共享），
#   不创建 Agent 或发网络。指定档案时用它；它在本 owner 解析不到时（2026-10-02 3a 定）改用本 owner 默认模型并留原因。
#   留空时（2026-10-02 用户拍板）按“消息来源会话的主代理模型”整理：curator_thread_profiles 只读各会话的
#   ConversationThread.model_profile_id，没选过的用 owner 默认，选了但不可用的按结构化原因退回 owner 默认。
# 模块用途: 解析记忆整理的固定档案或各会话的模型，并给设置页提供不含秘密的编号、型号和失效原因。
from __future__ import annotations

from dataclasses import dataclass

from ..backends.errors import ModelNotConfiguredError
from .model_profiles import model_profiles_path, read_model_profiles, selected_model_config
from .model_provider_schema import ModelProfileError


# LLM: 空值是唯一的沿用入口；default 是部署配置而非档案编号，显式填写不能绕过存在校验；共享仍由原解析器授权。
# 函数用途: 返回后台所需的完整配置，失败保留缺配置身份与结构化诊断，绝不切换服务商。
def curator_model_config(agent: object, profile_id: str):
    try:
        if profile_id == "default":
            raise ModelProfileError("请填写已保存的模型档案编号；部署默认不是模型档案。", reason="profile_not_found")
        return selected_model_config(agent, profile_id=profile_id) if profile_id else selected_model_config(agent)
    except ModelProfileError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason=exc.reason) from exc
    except OSError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason="catalog_unreadable") from exc


# LLM: 指定档案在本 owner 的目录（含管理员共享）解析不到时改用本 owner 默认模型（curator_model_config 留空的同一解析），
#   返回（配置, 退回原因）；没退回时原因为空。默认模型也解析不到时抛指定档案自己的 ModelNotConfiguredError（保留它的编号
#   和原因）。留空时不退回，原样抛。调用方：组合根建整理后端、设置查看；改动同步 test_curator_model_profile.py。
# 函数用途: 解析记忆整理实际要用的模型配置，指定档案在本用户不可用时改用本用户默认模型。
def curator_model_config_with_fallback(agent: object, profile_id: str) -> tuple[object, str]:
    try:
        return curator_model_config(agent, profile_id), ""
    except ModelNotConfiguredError as exc:
        if not profile_id:
            raise
        fixed_error = exc
    try:
        return curator_model_config(agent, ""), str(fixed_error.profile_reason or "profile_unavailable")
    except ModelNotConfiguredError:
        raise fixed_error from None


# LLM: 只投影编号、模型名和失效原因；设置查看不会探针或暴露连接字段。指定档案在本用户不可用时写明改用了哪个默认模型。
# 函数用途: 为 TUI/IM 共用的设置回执提供档案说明。
def curator_profile_description(agent: object, profile_id: str) -> str:
    label = profile_id or "（空，按消息来源会话的主代理模型整理；会话没选或不可用时用 owner 当前选中模型）"
    try:
        config, fallback = curator_model_config_with_fallback(agent, profile_id)
    except ModelNotConfiguredError as exc:
        return f"{label}；不可用：{exc.profile_reason}" + ("（本用户默认模型也不可用）" if profile_id else "")
    if fallback:
        return f"{label}；本用户不可用：{fallback}（改用本用户默认模型：{config.model_name or '未配置'}）"
    return f"{label}；模型：{config.model_name or '未配置'}"


# LLM: 不可变；thread_profiles 只含“用自己模型”的会话（编号 → 生效档案），没列出的会话用 owner 默认；
#   fallback_reasons 只记选了模型却不可用、退回默认的会话（没选过不算退回）；configs 是各生效档案的完整连接配置。
# 类用途: 一批会话在记忆整理时各用哪个模型的只读解析结果。
@dataclass(frozen=True)
class CuratorThreadProfiles:
    default_profile_id: str
    thread_profiles: dict[str, str]
    fallback_reasons: dict[str, str]
    configs: dict[str, object]


# LLM: 只读：直接 threads.load，不经 thread_model_profile_id（它会给空引用迁移写库）；不构造后端、不联网。
#   会话选的档案与 owner 默认相同就归默认组；解析失败按 ModelProfileError.reason（档案被删、停用、撤销共享等）、
#   目录读不了记 catalog_unreadable、连接字段缺（如订阅登出）记 model_configuration_missing，都退回默认并留原因。
# 函数用途: 给记忆整理解析一批会话各自的主代理模型。
def curator_thread_profiles(agent: object, thread_ids: tuple[str, ...]) -> CuratorThreadProfiles:
    default_id = _default_profile_id(agent)
    profiles: dict[str, str] = {}
    reasons: dict[str, str] = {}
    configs: dict[str, object] = {}
    # 同一档案只解析一次：编号 → （失败原因, 配置）。
    resolved: dict[str, tuple[str, object | None]] = {}
    for thread_id in thread_ids:
        thread = agent.conversation_store.threads.load(thread_id)
        selected = str(getattr(thread, "model_profile_id", "") or "") if thread is not None else ""
        if not selected or selected == default_id:
            continue
        if selected not in resolved:
            resolved[selected] = _thread_profile_config(agent, selected)
        failed_reason, config = resolved[selected]
        if failed_reason:
            reasons[thread_id] = failed_reason
        else:
            profiles[thread_id], configs[selected] = selected, config
    return CuratorThreadProfiles(default_id, profiles, reasons, configs)


# 函数用途: 解析一个会话档案；返回（失败原因, 配置），可用时原因为空。
def _thread_profile_config(agent: object, profile_id: str) -> tuple[str, object | None]:
    from ..backends.factory import model_configuration_missing

    try:
        config = selected_model_config(agent, profile_id=profile_id)
    except ModelProfileError as exc:
        return str(exc.reason or "profile_unavailable"), None
    except OSError:
        return "catalog_unreadable", None
    if model_configuration_missing(config.model_backend, config):
        return "model_configuration_missing", None
    return "", config


# LLM: 只读；和主代理同一条默认解析（model_profiles._default_for_owner：普通用户在管理员指定初始模型时用它，否则部署配置）。
#   owner 自己选过默认模型时返回空串（不另记）；目录读不了也返回空串，由默认组的原解析器报错。来源词与 /model 列表的
#   default_source 一致（admin_initial），部署配置记 deployment_default。没有自己档案的 owner（model-less）靠它看出默认从哪来。
# 函数用途: 告诉记忆整理“owner 默认模型”是从哪来的，写进运行记录。
def curator_default_model_source(agent: object) -> str:
    from .model_profiles import _default_for_owner

    if _default_profile_id(agent) != "default":
        return ""
    try:
        return "deployment_default" if _default_for_owner(agent) == "default" else "admin_initial"
    except (ModelProfileError, OSError):
        return ""


# 函数用途: 读 owner 当前选中的模型编号；目录读不了时返回空串（默认组照常用原解析器报错）。
def _default_profile_id(agent: object) -> str:
    try:
        return str(read_model_profiles(model_profiles_path(agent.home_paths)).get("selected") or "")
    except (ModelProfileError, OSError):
        return ""
