# LLM: 嵌入服务只按 /model 档案引用解析连接，空值 = 不建客户端、只走关键词；档案失效明确失败，不回退到聊天模型。
#   服务商凭据与端点全部来自档案，_embedding_client 不再读任何 embedding_* 平铺键。机器判断只读结构化 reason。
# 模块用途: 解析嵌入服务的固定模型档案，并给设置页提供不含秘密的编号、型号与失效原因。
from __future__ import annotations

from ..backends.errors import ModelNotConfiguredError
from .model_profiles import selected_model_config
from .model_provider_schema import ModelProfileError


# LLM: 空值是唯一的"不用语义"入口，不沿用 owner 选中模型（与 curator 不同：curator 空值沿用选中，嵌入空值=关闭）；
#   default 是部署配置而非档案编号，显式填写不能绕过存在校验；能力校验由 selected_model_config 按 embedding 用途执行。
# 函数用途: 返回嵌入客户端所需的完整连接配置；profile_id 为空时返回 None（只走关键词）。
def embedding_model_config(agent: object, profile_id: str):
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return None
    try:
        if profile_id == "default":
            raise ModelProfileError("请填写已保存的模型档案编号；部署默认不是模型档案。", reason="profile_not_found")
        return selected_model_config(agent, profile_id=profile_id, capability="embedding")
    except ModelProfileError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason=exc.reason) from exc
    except OSError as exc:
        raise ModelNotConfiguredError(profile_id=profile_id, profile_reason="catalog_unreadable") from exc


# LLM: 只投影编号、模型名和失效原因；设置查看不会探针或暴露连接字段，失效仍不回退。
# 函数用途: 为 TUI/IM 共用的设置回执提供嵌入档案说明。
def embedding_profile_description(agent: object, profile_id: str) -> str:
    label = profile_id or "（空，只走关键词）"
    try:
        config = embedding_model_config(agent, profile_id)
    except ModelNotConfiguredError as exc:
        return f"{label}；不可用：{exc.profile_reason}（未回退）"
    if config is None:
        return label
    return f"{label}；模型：{config.model_name or '未配置'}"


# LLM: 向量库身份 = 档案编号 + 服务商 + 模型名 + 维度，缺任一关键字段都不构成可用身份；供 P14 写元数据与一致性校验。
# 函数用途: 由档案解析结果生成向量库元数据身份，profile_id 为空时返回 None（不写元数据、不启用语义身份）。
def embedding_identity(agent: object, profile_id: str, dim: int) -> dict[str, object] | None:
    profile_id = str(profile_id or "").strip()
    if not profile_id or not dim:
        return None
    try:
        config = embedding_model_config(agent, profile_id)
    except ModelNotConfiguredError:
        return None
    if config is None:
        return None
    return {
        "profile_id": profile_id,
        "provider": str(getattr(config, "model_backend", "") or ""),
        "model_name": str(getattr(config, "model_name", "") or ""),
        "dim": int(dim),
    }


__all__ = [
    "embedding_identity",
    "embedding_model_config",
    "embedding_profile_description",
]