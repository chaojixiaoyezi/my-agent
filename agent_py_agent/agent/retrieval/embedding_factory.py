# LLM: 这里只承接原core嵌入客户端/降级/同客户端identity与唯一日志去重，不持有Agent、HTTP权限或长期资源cache；联验core wrappers和embedding/vector测试。
# 模块用途: 从真实配置和home构建可选嵌入通道，不发网络请求、不初始化owner目录。
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..settings.embedding_profile import embedding_model_config_for_resources

# LLM: 仅原owner/reason日志去重迁入此处，结构化diagnostics每次照写；不构成资源或权限缓存。
_PROFILE_WARNINGS_SEEN: set[tuple[str, str]] = set()
_PROFILE_WARNINGS_LOCK = threading.Lock()


# LLM: 配置决定档案编号，默认解析使用显式真实home；旧wrapper可注入原profile读取函数以保原调用点，不保存callback。
# 函数用途: 按档案原端点、凭据和型号创建MiniMax或OpenAI嵌入客户端，构造不发请求。
def embedding_client_for_resources(config: object, home_paths: object, *, profile_resolver: Callable | None = None):
    profile_id = str(getattr(config, "embedding_model_profile", "") or "").strip()
    if not profile_id:
        return None
    from .embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder

    resolved = (profile_resolver(profile_id) if profile_resolver is not None else
                embedding_model_config_for_resources(config, home_paths, profile_id))
    if resolved is None:
        return None
    model = str(getattr(resolved, "model_name", "") or "").strip()
    if not model:
        raise ValueError("embedding profile resolved without model_name")
    # 档案自带服务商端点与凭据；端点不再从聊天 api_base 推断，避免跨服务商混用。
    api_base = str(getattr(resolved, "api_base", "") or "")
    api_key = str(getattr(resolved, "api_key", "") or "")
    factory = MiniMaxEmbedder if model.startswith("embo") else OpenAICompatibleEmbedder
    return factory(api_base=api_base, model=model, api_key=api_key)


# LLM: 同进程只有此set/lock，键与原core相同；不影响每次结构化诊断或模型请求。
# 函数用途: 对同owner和原因仅放行第一条档案不可用日志。
def first_profile_warning_for_owner(owner_id: str, reason: str) -> bool:
    key = (str(owner_id or ""), reason)
    with _PROFILE_WARNINGS_LOCK:
        if key in _PROFILE_WARNINGS_SEEN:
            return False
        _PROFILE_WARNINGS_SEEN.add(key)
        return True


# LLM: client_factory/warning_once仅为原实际wrapper保调用点，不存储；普通初始化错误保持原降级，禁止增加网络或typed分类。
# 函数用途: 构造原可选记忆embedder并填写原诊断，关闭或档案失败时退回关键词。
def build_memory_embedder_for_resources(config: object, home_paths: object, *,
                                        diagnostics: dict[str, str] | None = None,
                                        client_factory: Callable | None = None,
                                        warning_once: Callable | None = None, logger=None):
    logger = logger if logger is not None else logging.getLogger(__name__)
    status = diagnostics if diagnostics is not None else {}
    status.update(state="disabled")
    if not getattr(config, "memory_semantic_recall", False):
        return None
    from ..backends.errors import ModelNotConfiguredError

    try:
        embedder = (client_factory() if client_factory is not None else
                    embedding_client_for_resources(config, home_paths))
    except ModelNotConfiguredError as exc:
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_PROFILE_UNAVAILABLE",
                      profile_id=exc.profile_id, profile_reason=exc.profile_reason)
        if (warning_once or (lambda reason: first_profile_warning_for_owner(
                getattr(home_paths, "owner_id", ""), reason)))(str(exc.profile_reason)):
            logger.warning("语义记忆档案不可用（%s）；当前使用关键词召回", exc.profile_reason)
        return None
    except Exception as exc:
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_INIT_FAILED", error_type=type(exc).__name__)
        logger.warning("语义记忆初始化失败（%s）；当前使用关键词召回", type(exc).__name__)
        return None
    if embedder is None:
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_MODEL_MISSING")
        logger.warning("语义记忆已开启，但未配置 embedding 模型档案；当前使用关键词召回")
        return None
    status.clear()
    status.update(state="configured")
    return embedder


# LLM: identity始终从同一次真实client生成；embedder_factory只适配原core调用点，identity缺失整个通道关闭，不保callback。
# 函数用途: 返回同生同灭的客户端和向量空间身份，保持原降级原因与零网络构造。
def memory_semantic_channel_for_resources(config: object, home_paths: object, status: dict[str, str], *,
                                          embedder_factory: Callable | None = None, logger=None):
    logger = logger if logger is not None else logging.getLogger(__name__)
    embedder = (embedder_factory() if embedder_factory is not None else
                build_memory_embedder_for_resources(config, home_paths, diagnostics=status, logger=logger))
    if embedder is None:
        return None, None
    from ..settings.embedding_profile import embedding_identity

    profile_id = str(getattr(config, "embedding_model_profile", "") or "")
    identity = embedding_identity(profile_id, embedder)
    if identity is None:
        status.clear()
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE")
        logger.warning("语义记忆无法确定嵌入身份；当前使用关键词召回")
        return None, None
    return embedder, identity
