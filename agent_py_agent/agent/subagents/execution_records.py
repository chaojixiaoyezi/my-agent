# LLM: Real acceptance test execution record models; keep this file side-effect free.
# 模块用途: 定义父级验收真实执行证据的数据结构，不负责执行命令或写入文件。

from __future__ import annotations

"""Data models for real acceptance test execution evidence."""

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


# LLM: TestExecutionRecord is the stable evidence payload produced later by TestExecutor.
# 类用途: 保存单条验收测试的真实执行结果；它只做字段归一化和序列化，不执行命令、不读取文件。
@dataclass
class TestExecutionRecord:
    """Record one real validation attempt for parent acceptance."""

    MAX_CAPTURE_CHARS: ClassVar[int] = 4000
    __test__: ClassVar[bool] = False

    test_name: str = ""
    command: str = ""
    executed: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    executed_at: str = ""
    error: str = ""
    validation_method: str = ""
    validation_result: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: __post_init__ keeps captured process output bounded before records enter reports or JSON.
    # 函数用途: 在对象创建时截断 stdout/stderr，并复制可变字典，避免大输出继续撑大内存或 prompt。
    def __post_init__(self) -> None:
        self.stdout = _tail_text(self.stdout, self.MAX_CAPTURE_CHARS)
        self.stderr = _tail_text(self.stderr, self.MAX_CAPTURE_CHARS)
        self.validation_result = dict(self.validation_result or {})
        self.metadata = dict(self.metadata or {})

    # LLM: passed derives parent acceptance truth from the real execution flag and validation result.
    # 函数用途: 给验收层读取统一布尔结果；未执行的记录即使 validation_result 写 ok 也不能算通过。
    @property
    def passed(self) -> bool:
        """Return whether the validation really executed and passed."""

        return bool(self.executed and self.validation_result.get("ok") is True)

    # LLM: to_dict returns a JSON-compatible shape without derived fields or class constants.
    # 函数用途: 转成后续 test_execution.json 可直接写入的字典；不会额外读取正文或展开 artifact。
    def to_dict(self) -> dict[str, Any]:
        """Serialize the record to a JSON-compatible dictionary."""

        return asdict(self)

    # LLM: from_dict tolerates missing/future fields and keeps ruff-clean forward references for CI.
    # 函数用途: 从 JSON 字典恢复记录；未知字段先忽略，扩展字段应放进 metadata 或 validation_result。
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TestExecutionRecord:
        """Create a record from stored JSON data."""

        if not isinstance(data, dict):
            return cls()
        allowed = {
            "test_name",
            "command",
            "executed",
            "exit_code",
            "stdout",
            "stderr",
            "duration_seconds",
            "executed_at",
            "error",
            "validation_method",
            "validation_result",
            "metadata",
        }
        values = {key: data[key] for key in allowed if key in data}
        return cls(**values)


# LLM: _tail_text is deliberately local to keep capture truncation consistent for stdout and stderr.
# 函数用途: 保留文本尾部，通常错误信息和 pytest 汇总在末尾；输入非字符串时会先转成字符串。
def _tail_text(value: object, max_chars: int) -> str:
    """Return at most the last max_chars characters of value."""

    text = "" if value is None else str(value)
    if max_chars <= 0:
        return ""
    return text[-max_chars:]
