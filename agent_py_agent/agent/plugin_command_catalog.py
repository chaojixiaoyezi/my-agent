# LLM: 目录是宿主声明的不可变投影；JSON 读取共用 command_declarations，scope_ref/revision 不能成为权限或安装状态。
# 模块用途: 将同一份命令声明传给客户端，验证收到的结构和摘要，避免客户端按旧目录解释新动作。

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .command_arguments import CommandActionSpec
from .command_catalog import COMMAND_INDEX
from .command_declarations import (
    command_action_from_payload,
    declaration_list,
    plugin_command_from_payload,
)
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

    # LLM: 网络载荷必须显式匹配 v1 并通过公共声明读取器；保持既有摘要，不为包入口增补 wire 默认字段。
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
                tuple(
                    command_action_from_payload(item)
                    for item in declaration_list(payload["management_actions"])
                ),
                tuple(
                    plugin_command_from_payload(item)
                    for item in declaration_list(payload["plugins"])
                ),
            )
            if payload["revision"] != result.revision:
                raise ValueError("插件目录摘要不匹配")
            return result
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            raise ValueError("插件目录声明无效") from exc
