# LLM: 嵌入服务只按 /model 档案引用解析连接，空值 = 不建客户端、只走关键词；档案失效明确失败，不回退到聊天模型。
#   服务商凭据与端点全部来自档案，_embedding_client 不再读任何 embedding_* 平铺键。机器判断只读结构化 reason。
# 模块用途: 解析嵌入服务的固定模型档案，给设置页提供不含秘密的编号、型号与失效原因，并由嵌入客户端算向量库空间身份。
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

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


# LLM: 向量空间身份只从"真正要发请求的那个客户端对象"取：档案编号 + 线路协议 + 端点摘要 + 模型名，与建客户端
#   共用同一次档案解析，不再二次读目录（P14 第 1/2 条）。端点只留去掉账号口令后的摘要，密钥和请求头永不进身份。
#   维度不在这里，由 VectorStore 按实际向量长度确定（第 5 条）。任一字段缺失返回 None：生产调用方必须关闭语义通道并
#   给出结构化诊断，不能把 None 当成旧兼容放行。改字段要同步 VectorStore 的身份比对与 test_vector_identity。
# 函数用途: 由嵌入客户端生成向量库的空间身份；缺字段返回 None。
def embedding_identity(profile_id: str, embedder: object) -> dict[str, str] | None:
    profile_id = str(profile_id or "").strip()
    protocol = str(getattr(embedder, "protocol", "") or "").strip()
    model_name = str(getattr(embedder, "model", "") or "").strip()
    endpoint_digest = _endpoint_digest(str(getattr(embedder, "api_base", "") or ""))
    if not (profile_id and protocol and model_name and endpoint_digest):
        return None
    return {
        "profile_id": profile_id,
        "protocol": protocol,
        "endpoint_digest": endpoint_digest,
        "model_name": model_name,
    }


# LLM: 只规范大小写不敏感的部分（scheme、主机）和末尾斜杠，路径与查询原样参与；先剥掉 userinfo（账号口令）再摘要，
#   所以换主机、端口、路径就换空间，换账号不换。没有 scheme 或主机返回空串，调用方据此判身份不可用。
# 函数用途: 把端点地址规范化后取 sha256 十六进制摘要，作为不含凭据的连接身份。
def _endpoint_digest(api_base: str) -> str:
    parts = urlsplit(api_base.strip())
    host = parts.netloc.rpartition("@")[2].lower()
    if not (parts.scheme and host):
        return ""
    normalized = urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), parts.query, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "embedding_identity",
    "embedding_model_config",
    "embedding_profile_description",
]