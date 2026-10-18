# LLM: 这里只承接原私卡的纯owner/config/runtime_paths解析；不创建资源或签权，保留原配置快照，私卡与未来领域组合必须共用该算法。
# 模块用途: 为原owner取得配置、规范home和运行路径，保留foreign覆盖清空，不初始化Agent或存储。
from dataclasses import replace

from ..owner_scoped_pool import _config_with_owner
from ..user_space.home_layout import home_paths
from ..user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from ..user_space.runtime_paths import resolve_runtime_paths_for_agent


# LLM: control/owner必须由调用方原来源给出；仅返回原三元组，foreign overrides清空和同owner配置语义不得分叉，修改须同验原私卡与模型菜单。
# 函数用途: 纯计算原owner资源位置与配置，不创建目录、Store、线程、封印或权限。
def resolve_owner_resource_configuration(control, owner):
    base = home_paths(control.home_root)
    home = home_paths_with_owner(base, resolve_owner_home(control.home_root, owner))
    config = _config_with_owner(control.config, owner)
    if owner != owner_identity_from_config(control.config):
        fields = resolve_runtime_paths_for_agent(control.config, base.owner_home_dir, base).paths
        config = replace(config, **{key: "" for key in fields if hasattr(config, key)})
    paths = resolve_runtime_paths_for_agent(config, home.owner_home_dir, home).paths
    return home, config, paths
