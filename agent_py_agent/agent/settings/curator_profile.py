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
from .model_profiles import (
    model_profile_generation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from .model_provider_schema import ModelProfileError, ModelProfileGeneration
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
        resolved_id = _effective_profile_id(agent, profile_id)
        if not resolved_id or resolved_id == "default":
            raise ModelProfileError("请填写已保存的模型档案编号；部署默认不是模型档案。", reason="profile_not_found")
        return selected_model_config(agent, profile_id=resolved_id)
    except ModelProfileError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason=exc.reason) from exc
    except OSError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason="catalog_unreadable") from exc


# LLM: "selected" 跟随 owner 目录当前选中项（未选或 default 时取 owner 默认）；其他编号原样返回。
# 函数用途: 把后台辅助模型选择器解析成本次实际要用的档案编号。
def _effective_profile_id(agent: object, profile_id: str) -> str:
    if profile_id != "selected":
        return profile_id
    from .model_profiles import _default_for_owner

    owner_sel = _default_profile_id(agent)
    return _default_for_owner(agent) if owner_sel in {"", "default"} else owner_sel


# LLM: 沿 model_profile_generation 合同：目录每次保存都换新代次，改默认、停用档案或服务商、撤销共享都会换代次；
#   生效档案停用、不存在、目录读不了或代次未知时返回 None，调用方不得复用旧后端。只读目录、不建后端、不含凭据。
# 函数用途: 返回后台辅助模型当前生效档案的目录代次，作为单槽后端缓存的复用键。
def curator_profile_generation(agent: object, profile_id: str) -> ModelProfileGeneration | None:
    try:
        resolved_id = _effective_profile_id(agent, profile_id)
        if not resolved_id or resolved_id == "default":
            return None
        return model_profile_generation(agent, resolved_id)
    except (ModelProfileError, OSError):
        return None


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


# 函数用途: 读 owner 当前选中的模型编号；目录读不了时返回空串（默认组照常用原解析器报错）。
def _default_profile_id(agent: object) -> str:
    try:
        return str(read_model_profiles(model_profiles_path(agent.home_paths)).get("selected") or "")
    except (ModelProfileError, OSError):
        return ""


# 后台记忆提取停用原因到中文说明；键是结构化原因码，展示层只查表，不解析文本。
_EXTRACTION_REASON_TEXT = {
    "owner_memory_disabled": "记忆总开关已关闭",
    "curator_disabled": "后台记忆整理开关 memory_curator_enabled 已关闭",
    "profile_not_configured": "还没有设置后台辅助模型",
    "profile_unavailable": "设置的后台辅助模型现在用不了",
    "cooldown": "后台辅助模型刚连续出错，正在冷却",
}


# LLM: 状态读取只看开关、档案编号、本 owner 模型目录与本进程冷却表：不建后端、不连网络、不读记忆正文。
#   顺序与网关准入（_curator_run_allowed）一致：总闸 → 整理开关 → 档案 → 档案可用 → 冷却。
#   memory_enabled 由调用方按已解析的 owner 策略传入；冷却表是进程内的，别的进程里的冷却这里看不到。
# 函数用途: 给 /memory、候选列表与自我目录统一给出后台记忆提取是否在跑及停用原因。
def curator_extraction_status(host: object, *, memory_enabled: bool = True) -> dict[str, object]:
    config = getattr(host, "config", None)
    if not memory_enabled:
        return {"extraction": "disabled", "disabled_reason": "owner_memory_disabled"}
    if not bool(getattr(config, "memory_curator_enabled", True)):
        return {"extraction": "disabled", "disabled_reason": "curator_disabled"}
    profile_id = effective_auxiliary_model_profile(config)
    if not profile_id:
        return {"extraction": "disabled", "disabled_reason": "profile_not_configured"}
    try:
        model_config = curator_model_config(host, profile_id)
    except ModelNotConfiguredError as exc:
        return {"extraction": "disabled", "disabled_reason": "profile_unavailable", "profile_id": profile_id,
                "profile_reason": str(exc.profile_reason or "")}
    except (AttributeError, TypeError, ValueError) as exc:
        # 宿主缺 home/config 等结构时说不清开没开，标 unknown 并带异常类型，不能当成已关闭或已开启。
        return {"extraction": "unknown", "disabled_reason": "status_unreadable", "profile_id": profile_id,
                "error_type": type(exc).__name__}
    remaining = auxiliary_profile_cooldown_remaining(profile_id)
    if remaining > 0:
        return {"extraction": "disabled", "disabled_reason": "cooldown", "profile_id": profile_id,
                "cooldown_remaining_seconds": int(remaining) + 1}
    return {"extraction": "enabled", "profile_id": profile_id,
            "model": str(getattr(model_config, "model_name", "") or "")}


# LLM: 只按结构化字段拼一行中文；未知原因原样显示原因码，不猜。设置入口写清 TUI 菜单与管理员命令。
# 函数用途: 把后台记忆提取状态渲染成 /memory 输出里的一行说明。
def curator_extraction_status_line(status: dict[str, object]) -> str:
    if status.get("extraction") == "enabled":
        label = str(status.get("model") or status.get("profile_id") or "")
        return f"后台记忆提取：已开启（后台辅助模型 {label}），聊天里提到的偏好和事实会在后台自动整理。"
    if status.get("extraction") == "unknown":
        return f"后台记忆提取：状态读不出（{status.get('error_type') or '原因未知'}），本次没有改动任何设置。"
    reason = str(status.get("disabled_reason") or "")
    text = _EXTRACTION_REASON_TEXT.get(reason, reason or "原因未知")
    if reason == "cooldown":
        hint = f"约 {status.get('cooldown_remaining_seconds', 0)} 秒后自动重试。"
    elif reason in {"profile_not_configured", "profile_unavailable"}:
        hint = ("可在终端 /model →「默认模型与共享」→「后台辅助模型」选择，"
                "或由管理员 /settings set memory_curator_model_profile <档案编号>（会额外消耗 token）。")
    elif reason == "curator_disabled":
        hint = "管理员可用 /settings set memory_curator_enabled true 打开。"
    else:
        hint = ""
    return f"后台记忆提取：已停用（{text}）；只有你明确让我记住的内容才会保存。{hint}"
