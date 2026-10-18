# LLM: 模型配置、插件与C历史共用原owner会话路径；显式config/home叶子不加载Agent、不另算路径或复制线程账本。
# 模块用途: 按原运行目录解析器组装会话Store；被动历史只读打开，不能创建冷用户目录。

from __future__ import annotations

from types import SimpleNamespace


# LLM: 冷 owner 沿原 canonical 地址组装 Store；initialize=False 不建目录，模型配置与插件管理均不初始化 Agent。
# 函数用途: 取得当前 owner 的原线程存储，避免轻量管理入口借用基础 owner 的路径。
def owner_conversation_store(base_agent: object, scoped_home: object, *, initialize: bool = True):
    from ..settings.thread_model_selection import default_model_profile_id
    from .request_worker import _config_without_runtime_paths

    same_owner = all(getattr(base_agent.home_paths, field, None) == getattr(scoped_home, field, None)
                     for field in ("owner_provider", "owner_kind", "owner_id"))
    if same_owner and getattr(base_agent, "conversation_store", None) is not None:
        return base_agent.conversation_store
    config = base_agent.config if same_owner else _config_without_runtime_paths(base_agent)
    host = SimpleNamespace(config=config, home_paths=scoped_home)
    return owner_conversation_store_from_config(
        config, scoped_home, initialize=initialize, model_default=lambda: default_model_profile_id(host),
    )


# LLM: config和home须由原owner裁决给出；调用方负责foreign覆盖清空。本叶子只复用唯一runtime_paths，不初始化Agent或推导第二目录。
#   initialize=False保留原只读语义；model_default只沿原wrapper注入，历史读取不能创建线程或触发该回调。
# 函数用途: 从显式配置与规范owner home打开原会话库，供无业务Agent的C读取已有历史。
def owner_conversation_store_from_config(config, scoped_home, *, initialize=False, model_default=None):
    from ..conversation.store import ConversationStore
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent

    paths = resolve_runtime_paths_for_agent(config, scoped_home.owner_home_dir, scoped_home).paths
    return ConversationStore(paths["conversation_workspace"], initialize=initialize, model_default=model_default)
