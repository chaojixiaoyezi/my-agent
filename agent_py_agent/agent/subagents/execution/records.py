
from __future__ import annotations

"""Data models for real acceptance test execution evidence."""

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


@dataclass
class TestExecutionRecord:
    """Record one real validation attempt for closeout."""

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

    def __post_init__(self) -> None:
        self.stdout = _tail_text(self.stdout, self.MAX_CAPTURE_CHARS)
        self.stderr = _tail_text(self.stderr, self.MAX_CAPTURE_CHARS)
        self.validation_result = dict(self.validation_result or {})
        self.metadata = dict(self.metadata or {})

    @property
    def passed(self) -> bool:
        """Return whether the validation really executed and passed."""

        return bool(self.executed and self.validation_result.get("ok") is True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the record to a JSON-compatible dictionary."""

        return asdict(self)

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


def _tail_text(value: object, max_chars: int) -> str:
    """Return at most the last max_chars characters of value."""

    text = "" if value is None else str(value)
    if max_chars <= 0:
        return ""
    return text[-max_chars:]
