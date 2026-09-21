# LLM: 模型配置与插件管理共用原 owner 会话路径，不加载冷 Agent，也不复制线程账本。
# 模块用途: 按原配置与 owner 布局组装轻量会话 Store，可禁止创建冷用户目录。

from __future__ import annotations

from types import SimpleNamespace


# LLM: 冷 owner 沿原 canonical 地址组装 Store；initialize=False 不建目录，模型配置与插件管理均不初始化 Agent。
# 函数用途: 取得当前 owner 的原线程存储，避免轻量管理入口借用基础 owner 的路径。
def owner_conversation_store(base_agent: object, scoped_home: object, *, initialize: bool = True):
    from ..conversation.store import ConversationStore
    from ..settings.thread_model_selection import default_model_profile_id
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
    from .request_worker import _config_without_runtime_paths

    same_owner = all(getattr(base_agent.home_paths, field, None) == getattr(scoped_home, field, None)
                     for field in ("owner_provider", "owner_kind", "owner_id"))
    if same_owner and getattr(base_agent, "conversation_store", None) is not None:
        return base_agent.conversation_store
    config = base_agent.config if same_owner else _config_without_runtime_paths(base_agent)
    paths = resolve_runtime_paths_for_agent(config, scoped_home.owner_home_dir, scoped_home).paths
    host = SimpleNamespace(config=config, home_paths=scoped_home)
    return ConversationStore(paths["conversation_workspace"], initialize=initialize,
                             model_default=lambda: default_model_profile_id(host))
