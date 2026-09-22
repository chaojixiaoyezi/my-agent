# LLM: 本引用只定位原 PluginInstallStore，不缓存执行权、不创建第二份状态；宿主构造后冻结，联测启动、发送和撤销竞态。
# 模块用途: 让独立进程沿同一用户目录核对原插件代次，拒绝换用户、换路径或换代后继续执行。

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .common.path_segments import safe_path_segment
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallation
from .tooling.process_scope import ProcessActivationScope
from .user_space.owner_resolver import OwnerHomeResult, OwnerIdentity, resolve_owner_home


# LLM: root 与 identity 必须来自可信宿主，scope 只绑定原代；序列化用于私有启动信封，不能接受模型提供的权限地址。
# 类用途: 保存可跨进程传递的固定激活引用，每次使用仍读取唯一安装表。
@dataclass(frozen=True)
class PluginActivationRef:
    root: str
    identity: OwnerIdentity
    scope: ProcessActivationScope

    # LLM: 不把畸形身份清洗成另一个合法用户；原 resolver 与资源 scope 必须得出完全相同的规范地址。
    # 函数用途: 拒绝不完整、非规范或归属冲突的引用，构造不创建目录。
    def __post_init__(self) -> None:
        if (not isinstance(self.root, str) or not Path(self.root).is_absolute()
                or not isinstance(self.identity, OwnerIdentity) or not isinstance(self.scope, ProcessActivationScope)):
            raise ValueError("插件激活引用无效")
        identity = self.identity
        if not isinstance(identity.owner_kind, str):
            raise ValueError("插件用户类型无效")
        if identity.owner_kind == "main":
            if identity != OwnerIdentity.local_main():
                raise ValueError("插件主用户身份无效")
        elif identity.owner_kind not in {"user", "group"}:
            raise ValueError("插件用户类型无效")
        for value in (identity.provider, identity.owner_id):
            if not isinstance(value, str) or not value or safe_path_segment(value) != value:
                raise ValueError("插件用户身份不是规范路径段")
        self.owner()

    # LLM: 只接受宿主既有 owner 解析结果，不能从工作目录、首个业务会话或权限视图反推用户。
    # 函数用途: 在组合入口为同一安装的固定代次创建引用。
    @classmethod
    def from_owner(cls, owner: OwnerHomeResult, plugin_id: str, activation_id: str) -> PluginActivationRef:
        return cls(str(owner.root), owner.identity,
                   ProcessActivationScope(owner.owner_id, str(owner.home_dir), plugin_id, activation_id))

    # LLM: 每次重新解析并比对原绝对地址；根目录被重定向不能悄悄读取另一份安装权威。
    # 函数用途: 还原同一用户的规范路径，不初始化 Agent 或写文件。
    def owner(self) -> OwnerHomeResult:
        owner = resolve_owner_home(self.root, self.identity)
        if (str(owner.root) != self.root or owner.owner_id != self.scope.owner_id
                or str(owner.home_dir) != self.scope.owner_home):
            raise ValueError("插件激活引用与规范用户地址不符")
        return owner

    # LLM: preparing 只供启动、握手和发现；业务调用必须只接受 active，任一模式都不接受 revoked 或另一代。
    # 函数用途: 鲜活读取唯一安装表，允许同一准备代发布后沿原连接继续使用。
    def require(self, *, allow_preparing: bool = False) -> PluginInstallation:
        phases = frozenset({"preparing", "active"}) if allow_preparing else frozenset({"active"})
        return PluginInstallStore(self.owner()).require_activation(
            self.scope.plugin_id, self.scope.activation_id, phases=phases,
        )

    # LLM: 载荷只包含固定身份和可信规范地址，没有配置值或授权缓存；仅写宿主私有的一次性信封。
    # 函数用途: 将原引用交给独立 host，避免其猜测用户目录。
    def to_payload(self) -> dict:
        return {"root": self.root, "identity": asdict(self.identity), "scope": asdict(self.scope)}

    # LLM: 字段严格匹配当前引用协议；解码后仍要由资源层比对原 session 归属并鲜活核对安装表。
    # 函数用途: 还原私有信封中的引用，拒绝缺字段、附加字段和路径冲突。
    @classmethod
    def from_payload(cls, value: object) -> PluginActivationRef:
        if not isinstance(value, dict) or set(value) != {"root", "identity", "scope"}:
            raise ValueError("插件激活引用字段无效")
        identity, scope = value["identity"], value["scope"]
        for payload, shape in ((identity, OwnerIdentity), (scope, ProcessActivationScope)):
            if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(shape)}:
                raise ValueError("插件激活归属字段无效")
        return cls(value["root"], OwnerIdentity(**identity), ProcessActivationScope(**scope))
