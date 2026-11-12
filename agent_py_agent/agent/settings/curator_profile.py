# LLM: 后台辅助模型（统一用于记忆整理与技能自学习）由 memory_curator_model_profile 统一指定；
#   留空（""）表示关闭后台自动整理与自学习（0次后台LLM调用，不退回到主对话模型或会话模型）；
#   显式指定时，仅解析该特定模型档案（含管理员共享）。多用户共享模型时由进程内冷却表避免并发重试冲击。
# 模块用途: 解析后台辅助模型档案（记忆整理与技能自学习共用），并维护多用户共享冷却状态。
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..backends.errors import ModelNotConfiguredError
from .config_io import load_simple_yaml
from .model_profiles import model_profiles_path, read_model_profiles, selected_model_config
from .model_provider_schema import ModelProfileError
from .user_config_capability import user_config_path

# 多用户共享辅助模型冷却记录（最多保留256条，先进先出淘汰）:
# profile_id -> (strikes, retry_not_before_monotonic, last_error_code)
_SHARED_AUXILIARY_PROFILE_COOLDOWN: OrderedDict[str, tuple[int, float, str]] = OrderedDict()
_SHARED_AUXILIARY_LOCK = threading.Lock()


# LLM: 读取当前生效的后台辅助模型档案编号：优先读已保存的用户YAML配置（即刻感知/model变更），未指定或无文件时回退到config属性。
# 函数用途: 返回当前生效的后台辅助模型档案编号；空字符串表示关闭后台自动整理与自学习。
def effective_auxiliary_model_profile(config: object) -> str:
    try:
        path = user_config_path(config)
        if path is not None and path.is_file():
            raw = load_simple_yaml(path)
            if isinstance(raw, dict) and "memory_curator_model_profile" in raw:
                val = raw.get("memory_curator_model_profile")
                return "" if val is None else str(val).strip()
    except Exception:
        pass
    return str(getattr(config, "memory_curator_model_profile", "") or "").strip()


# LLM: 判断当前是否显式配置了后台辅助模型。
# 函数用途: 检查后台辅助模型是否处于开启状态。
def curator_profile_configured(config: object) -> bool:
    return bool(effective_auxiliary_model_profile(config))


# LLM: 解析辅助模型编号，若未配置返回None。
# 函数用途: 为调度器与服务提供已配置的辅助模型编号。
def resolve_curator_model_selection(config: object, agent: object | None = None) -> str | None:
    profile = effective_auxiliary_model_profile(config)
    return profile if profile else None


# LLM: 记录共享辅助模型提供者故障并计算阶梯退避时长（10m -> 30m -> 60m）。
# 函数用途: 登记模型失败，使其他共享此模型的用户在冷却期内跳过无效探测。
def record_auxiliary_profile_failure(
    profile_id: str,
    error_code: str,
    *,
    now_monotonic: float | None = None,
) -> int:
    pid = str(profile_id or "").strip()
    if not pid:
        return 0
    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _SHARED_AUXILIARY_LOCK:
        prev = _SHARED_AUXILIARY_PROFILE_COOLDOWN.get(pid)
        strikes = (prev[0] + 1) if prev is not None else 1
        step = 600 if strikes == 1 else (1800 if strikes == 2 else 3600)
        retry_not_before = now + step
        _SHARED_AUXILIARY_PROFILE_COOLDOWN[pid] = (strikes, retry_not_before, str(error_code or ""))
        _SHARED_AUXILIARY_PROFILE_COOLDOWN.move_to_end(pid)
        while len(_SHARED_AUXILIARY_PROFILE_COOLDOWN) > 256:
            _SHARED_AUXILIARY_PROFILE_COOLDOWN.popitem(last=False)
        return step


# LLM: 查询指定辅助模型当前剩余的冷却时间（秒）。
# 函数用途: 判断共享辅助模型是否仍处于冷却退避中。
def auxiliary_profile_cooldown_remaining(
    profile_id: str,
    *,
    now_monotonic: float | None = None,
) -> float:
    pid = str(profile_id or "").strip()
    if not pid:
        return 0.0
    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _SHARED_AUXILIARY_LOCK:
        entry = _SHARED_AUXILIARY_PROFILE_COOLDOWN.get(pid)
        if entry is None:
            return 0.0
        remaining = entry[1] - now
        return max(0.0, remaining)


# LLM: 清除指定辅助模型的冷却记录（探测成功或用户重新选定模型时调用）。
# 函数用途: 立即解除模型的共享冷却，使所有共享用户恢复后台任务。
def clear_auxiliary_profile_cooldown(profile_id: str) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        return
    with _SHARED_AUXILIARY_LOCK:
        _SHARED_AUXILIARY_PROFILE_COOLDOWN.pop(pid, None)


# LLM: 空值表示未指定（关闭）；default是部署配置而非档案编号；显式指定时仅解析该档案。
# 函数用途: 返回后台所需的完整配置，失败保留缺配置身份与结构化诊断，绝不切换服务商。
def curator_model_config(agent: object, profile_id: str):
    try:
        resolved_id = profile_id
        if resolved_id == "selected":
            from .model_profiles import _default_for_owner

            owner_sel = _default_profile_id(agent)
            resolved_id = _default_for_owner(agent) if owner_sel in {"", "default"} else owner_sel
        if not resolved_id or resolved_id == "default":
            raise ModelProfileError("请填写已保存的模型档案编号；部署默认不是模型档案。", reason="profile_not_found")
        return selected_model_config(agent, profile_id=resolved_id)
    except ModelProfileError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason=exc.reason) from exc
    except OSError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason="catalog_unreadable") from exc


# LLM: 严格统一辅助模型契约：空值直接报错，指定档案解析失败直接报错，零回退。
# 函数用途: 解析后台记忆整理实际要用的模型配置，不进行隐式回退。
def curator_model_config_with_fallback(agent: object, profile_id: str) -> tuple[object, str]:
    if not profile_id:
        raise ModelNotConfiguredError(profile_id="", profile_reason="profile_not_configured")
    return curator_model_config(agent, profile_id), ""


# LLM: 只投影编号、模型名和失效原因；设置查看不会探针或暴露连接字段。
# 函数用途: 为 TUI/IM 共用的设置回执提供档案说明。
def curator_profile_description(agent: object, profile_id: str) -> str:
    label = profile_id or "（空，后台辅助模型已关闭）"
    if not profile_id:
        return "（空，后台辅助模型已关闭；后台记忆整理与技能自学习已停用）"
    try:
        config, _ = curator_model_config_with_fallback(agent, profile_id)
    except ModelNotConfiguredError as exc:
        return f"{label}；不可用：{exc.profile_reason}"
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
