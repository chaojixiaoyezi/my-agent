"""工具操作的展示身份与账本身份使用同一份类型化引用。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolOperationReference:
    display_name: str
    domain: str
    action: str
    storage_namespace: str
    operation_id: str
    args_hash: str
    version: int = 1

    @property
    def storage_tool_name(self) -> str:
        # 执行器当前以同一个身份写入操作类型和幂等命名空间。
        return self.storage_namespace

    def to_dict(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "domain": self.domain,
            "action": self.action,
            "storage_namespace": self.storage_namespace,
            "operation_id": self.operation_id,
            "args_hash": self.args_hash,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> ToolOperationReference | None:
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            return None
        values = {key: value.get(key) for key in (
            "display_name", "domain", "action", "storage_namespace", "operation_id", "args_hash",
        )}
        if any(not isinstance(item, str) or (key != "action" and not item) for key, item in values.items()):
            return None
        return cls(**values)
