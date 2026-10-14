# LLM: G1 本机凭据只在宿主加载，配置 token 优先且只发一个头；失败必须在网络前拒绝，不能不带凭据降级发送。
# 模块用途: 给 TUI、CLI 和 IM 共用的身份头入口提供同一宿主凭据，不创建、修复或导出秘密。
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..user_space.home_layout import home_paths
from ..user_space.home_root import configured_home_root
from .local_client_token import LocalClientCredentialError, load_local_client_credential


# LLM: 仅保存宿主路径及可选部署 token，repr 不显示它们；构造不读秘密，发送入口才加载。
# 类用途: 在既有请求头构造时取得 G1 凭据，缺失或不可读时零网络请求。
@dataclass(frozen=True)
class GatewayClientCredentials:
    data_root: Path = field(repr=False)
    configured_token: str = field(default="", repr=False)

    # LLM: 配置 token 非空时沿部署合同使用，否则严读 G1；错误只带原因码，不降级空头、不重试。
    # 函数用途: 返回唯一认证头；读取失败在请求前中止，绝不回显凭据内容或路径。
    def headers(self) -> dict[str, str]:
        if self.configured_token:
            return {"X-Gateway-Token": self.configured_token}
        try:
            token = load_local_client_credential(self.data_root)
        except LocalClientCredentialError as exc:
            exc.args = (credential_error_message(exc),)
            raise
        return {"X-Gateway-Token": token}


# LLM: 文案只读取 G1 结构化原因；不插入底层异常、文件路径、内容或配置值，供控制和插件回执共用。
# 函数用途: 告诉用户为什么没有发送请求以及应让宿主检查什么。
def credential_error_message(exc: LocalClientCredentialError) -> str:
    if exc.reason_code == "LOCAL_CLIENT_CREDENTIAL_MISSING":
        action = "请先启动一次 Gateway，再重启客户端。"
    elif exc.reason_code == "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS":
        action = "请让宿主检查 Gateway 凭据的属主及私有权限，再重启客户端。"
    else:
        action = "请让宿主检查 Gateway 凭据文件完整性和可读性，再重启客户端。"
    return f"请求未发送（{exc.reason_code}）。{action}"


# LLM: 数据根优先沿原 Agent.home_paths.root，不能拿工作目录、队列根或 owner home 当全局 secrets 根；轻量替身用原配置解析器。
#   旧迁移开关不再属于运行配置，不能被存量 YAML 或模型属性重新打开。
# 函数用途: 取得当前宿主客户端的凭据来源，不在构造阶段读秘密或发请求。
def gateway_client_credentials(agent: object) -> GatewayClientCredentials:
    config = getattr(agent, "config", None)
    root = getattr(getattr(agent, "home_paths", None), "root", None)
    if not isinstance(root, (str, Path)):
        root = home_paths(configured_home_root(config)).root
    token = getattr(config, "gateway_auth_token", "")
    return GatewayClientCredentials(
        Path(root), token if isinstance(token, str) else "",
    )
