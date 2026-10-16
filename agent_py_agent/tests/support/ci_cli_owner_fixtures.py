# LLM: 仅为离线 CLI 替身解析真实 owner 路径；复用公开配置与路径工厂，不创建身份、权限或正式资料。
# 模块用途: 让 CLI 夹具能读取同一临时 owner 的学习政策，避免缺字段替身或默认日常 home。
from pathlib import Path

from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import MyAgentHomePaths, home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)


# LLM: 仅解析明确临时 my_agent_home 与原本机 owner；不初始化目录、不签发 actor，也不替换任何政策读取。
# 函数用途: 返回与真实 CLI 相同类型的完整路径，让每个替身自行建立自己的临时 ConversationStore。
def isolated_cli_owner_paths(tmp_path: Path) -> MyAgentHomePaths:
    config = AgentConfig(my_agent_home=str(tmp_path / "home"))
    paths = home_paths(config.my_agent_home)
    assert paths.root == (tmp_path / "home").resolve()
    assert paths.root != (Path.home() / ".my-agent").resolve()
    owner = resolve_owner_home(paths.root, owner_identity_from_config(config))
    return home_paths_with_owner(paths, owner)
