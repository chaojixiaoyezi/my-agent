# LLM: 后台模型只复用原 selected_model_config 的整组连接解析，空值仍取 owner 选择，不读线程选择，不创建 Agent 或发网络。
# 模块用途: 解析记忆整理的固定档案，并给设置页提供不含秘密的编号、型号和失效原因。
from __future__ import annotations

from ..backends.errors import ModelNotConfiguredError
from .model_profiles import selected_model_config
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


# LLM: 只投影编号、模型名和失效原因；设置查看不会探针或暴露连接字段，失效仍不回退。
# 函数用途: 为 TUI/IM 共用的设置回执提供档案说明。
def curator_profile_description(agent: object, profile_id: str) -> str:
    label = profile_id or "（空，沿用 owner 当前选中模型）"
    try:
        config = curator_model_config(agent, profile_id)
    except ModelNotConfiguredError as exc:
        return f"{label}；不可用：{exc.profile_reason}（未回退）"
    return f"{label}；模型：{config.model_name or '未配置'}"
