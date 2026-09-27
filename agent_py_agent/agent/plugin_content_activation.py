# LLM: 内容激活是唯一安装记录的无进程变体，不包含环境、解释器或资源退出事实；旧进程协议仍由 PluginActivation 管理。
# 模块用途: 固定能力内容的启用代次，使停用、换版和重装后的旧读取引用失效。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields

CONTENT_ACTIVATION_SCHEMA = "plugin_content_activation.v1"


# LLM: 身份来自宿主原操作与安装快照，phase 只表示内容读取准入；不能据此授予脚本或工具执行权。
# 类用途: 记录纯内容包的固定启用身份和发布状态，不构造虚假的环境计划。
@dataclass(frozen=True)
class PluginContentActivation:
    operation_id: str
    plugin_id: str
    package_sha256: str
    installation_revision: int
    settings_revision: int
    phase: str = "active"

    # LLM: 版本与摘要必须完整；同一安装/操作的代次不随撤销变化，读取方仍须检查唯一安装表。
    # 函数用途: 拒绝畸形操作身份、内容身份和状态。
    def __post_init__(self) -> None:
        if (not isinstance(self.operation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.operation_id)
                or not isinstance(self.plugin_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", self.plugin_id)
                or not isinstance(self.package_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.package_sha256)
                or type(self.installation_revision) is not int or self.installation_revision < 1
                or type(self.settings_revision) is not int or not 0 <= self.settings_revision <= self.installation_revision
                or self.phase not in {"active", "revoked"}):
            raise ValueError("内容激活身份或状态无效")

    # LLM: 代次绑定原操作和原安装，卸载重装不能通过相同包摘要恢复旧权限；phase 不参与固定身份。
    # 函数用途: 为启用、撤销和调用前后核对提供稳定的内容代次。
    @property
    def activation_id(self) -> str:
        return _digest({key: value for key, value in self.to_payload().items() if key != "phase"})

    # LLM: 回执摘要覆盖当前状态，防止把 active 的回执与 revoked 记录拼接。
    # 函数用途: 计算唯一安装表的提交内容摘要。
    @property
    def content_sha256(self) -> str:
        return _digest(self.to_payload())

    # LLM: 使用显式新协议识别内容变体，原进程激活的无版本 payload 不改写。
    # 函数用途: 生成不含个人地址与运行资源的持久记录。
    def to_payload(self) -> dict:
        return {"schema_version": CONTENT_ACTIVATION_SCHEMA, **asdict(self)}

    # LLM: 严格版本与字段，不允许旧进程字段或未来属性被丢弃后当作当前状态。
    # 函数用途: 从原安装表恢复无进程内容激活。
    @classmethod
    def from_payload(cls, value: object) -> PluginContentActivation:
        if (not isinstance(value, dict) or value.get("schema_version") != CONTENT_ACTIVATION_SCHEMA
                or set(value) != {"schema_version", *(field.name for field in fields(cls))}):
            raise ValueError("内容激活字段无效")
        return cls(**{key: item for key, item in value.items() if key != "schema_version"})


# LLM: 规范摘要仅绑定本模块结构化身份，不依赖可变说明、默认 dataclass 扩展或当前时间。
# 函数用途: 对固定结构生成稳定的 SHA256 标识。
def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
