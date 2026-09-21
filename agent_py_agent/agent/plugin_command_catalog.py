# LLM: 目录是宿主声明的不可变投影；scope_ref/revision 只绑定展示与过期判断，不能成为权限、安装或执行状态。
# 模块用途: 将同一份命令声明传给客户端，验证收到的结构和摘要，避免客户端按旧目录解释新动作。

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields

from .command_arguments import ArgumentSpec, CommandActionSpec
from .command_catalog import COMMAND_INDEX
from .plugin_commands import PluginCommandSpec

_SCHEMA = "plugin_command_catalog.v1"


# LLM: 这里只冻结已由宿主过滤的声明，不加载插件或保存第二份安装表；版本变化须进入同一摘要。
# 类用途: 为帮助、补全和提交保存同一份当前作用域命令目录。
@dataclass(frozen=True)
class PluginCommandCatalog:
    scope_ref: str
    management_actions: tuple[CommandActionSpec, ...] = COMMAND_INDEX["plugins"].actions
    plugins: tuple[PluginCommandSpec, ...] = ()

    # LLM: 构造和反序列化共用唯一性约束；目录中重复身份不能按顺序挑选或静默覆盖。
    # 函数用途: 在目录可见前拒绝空作用域、可变集合和歧义声明。
    def __post_init__(self) -> None:
        if not isinstance(self.scope_ref, str) or not self.scope_ref.strip():
            raise ValueError("插件目录缺少宿主作用域引用")
        for collection, kind, key in (
            (self.management_actions, CommandActionSpec, "name"),
            (self.plugins, PluginCommandSpec, "plugin_id"),
        ):
            if not isinstance(collection, tuple) or any(
                not isinstance(item, kind) for item in collection
            ):
                raise ValueError("插件目录必须包含不可变声明")
            if len({getattr(item, key) for item in collection}) != len(collection):
                raise ValueError("插件目录存在重复身份")

    # LLM: 摘要包含宿主作用域及完整声明，不包含当前时间；相同目录必须稳定，不能将其当作签名或授权令牌。
    # 函数用途: 为客户端选中的声明生成可重复比较的版本标记。
    @property
    def revision(self) -> str:
        raw = json.dumps(
            self._declarations(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # LLM: 每次投影只复制不可变声明，不含 handler、密钥、宿主路径或可执行对象。
    # 函数用途: 汇集需要跨进程传输并参与版本计算的目录字段。
    def _declarations(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA,
            "scope_ref": self.scope_ref,
            "management_actions": [asdict(action) for action in self.management_actions],
            "plugins": [asdict(plugin) for plugin in self.plugins],
        }

    # LLM: 返回独立 JSON 值，客户端不能通过修改投影改变宿主快照；版本在相同声明上计算。
    # 函数用途: 生成 HTTP 与客户端缓存共用的公开目录载荷。
    def to_payload(self) -> dict[str, object]:
        return json.loads(
            json.dumps(
                {**self._declarations(), "revision": self.revision},
                ensure_ascii=False,
                allow_nan=False,
            )
        )

    # LLM: 网络载荷必须显式匹配 v1 并通过原声明校验；未知或过期协议不按默认值补成成功。
    # 函数用途: 从宿主回执重建只读目录，损坏声明或摘要不符时明确失败。
    @classmethod
    def from_payload(cls, payload: object) -> PluginCommandCatalog:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "scope_ref",
            "revision",
            "management_actions",
            "plugins",
        }:
            raise ValueError("插件目录字段不完整或存在未知字段")
        if payload["schema_version"] != _SCHEMA:
            raise ValueError("不支持的插件目录版本")
        try:
            result = cls(
                payload["scope_ref"],
                tuple(_action(item) for item in _list(payload["management_actions"])),
                tuple(_plugin(item) for item in _list(payload["plugins"])),
            )
            if payload["revision"] != result.revision:
                raise ValueError("插件目录摘要不匹配")
            return result
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            raise ValueError("插件目录声明无效") from exc


# LLM: JSON 数组不能从字符串、对象或任意可迭代值推断；仅转换已验证的声明集合。
# 函数用途: 为目录中的动作、参数和候选列表提供一致的类型检查。
def _list(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("声明集合必须是数组")
    return value


# LLM: 序列化字段与 dataclass 同源，但客户端必须收到全部字段；布尔值和描述文本禁止隐式强制转换。
# 函数用途: 校验一条跨进程声明的字段集合和基础类型。
def _record(
    value: object, kind: type, *, strings: tuple[str, ...], booleans: tuple[str, ...] = ()
) -> dict:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(kind)}:
        raise ValueError("声明字段不完整或存在未知字段")
    if any(not isinstance(value[key], str) for key in strings) or any(
        type(value[key]) is not bool for key in booleans
    ):
        raise ValueError("声明字段类型错误")
    return dict(value)


# LLM: 参数的类型、默认值和候选约束继续由 ArgumentSpec 裁决；这里只恢复 JSON 丢失的不可变集合形态。
# 函数用途: 读取一个参数声明，不接受字符串冒充开关或数组。
def _argument(value: object) -> ArgumentSpec:
    row = _record(
        value,
        ArgumentSpec,
        strings=("name", "summary", "value_type"),
        booleans=("required", "multiple", "path"),
    )
    row["options"] = tuple(_list(row["options"]))
    row["choices"] = tuple(_list(row["choices"]))
    if isinstance(row["default"], list):
        row["default"] = tuple(row["default"])
    return ArgumentSpec(**row)


# LLM: 动作 kind/target 仍只是声明；读取目录不能取得或执行对应 handler。
# 函数用途: 将公开动作的参数恢复为经过同一合同验证的描述。
def _action(value: object) -> CommandActionSpec:
    row = _record(
        value,
        CommandActionSpec,
        strings=("name", "summary", "kind", "target"),
        booleans=("available",),
    )
    row["arguments"] = tuple(_argument(item) for item in _list(row["arguments"]))
    return CommandActionSpec(**row)


# LLM: 插件版本和激活引用仅保留宿主声明，实际执行仍需原生命周期的当前准入与撤销核对。
# 函数用途: 读取插件静态信息及其动作，不导入实现或连接执行端点。
def _plugin(value: object) -> PluginCommandSpec:
    row = _record(
        value,
        PluginCommandSpec,
        strings=("plugin_id", "summary", "default_action", "package_version", "activation_id"),
        booleans=("enabled",),
    )
    row["actions"] = tuple(_action(item) for item in _list(row["actions"]))
    return PluginCommandSpec(**row)
