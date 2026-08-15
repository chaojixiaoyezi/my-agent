"""R3 Acceptance（3.txt I 节）：Contract Compiler + 双 Validator。

- compiler：模型 propose assertions → 框架编译/校验/冻结契约（I.2）。
- registry：PureValidator 可信版本注册表（I.7），ref → 实现 + 版本 + digest。
- process_validator：SandboxedProcessValidator（I.8/I.9）固定 argv、最小
  env、禁网、只读 artifact；sandbox 不可用 fail closed → UNAVAILABLE。
- snapshot：validator 只验证内容寻址的 immutable artifact snapshot（H.9）。

R3 建立机制与测试；接入工具执行链路归 R4 cutover。
"""

from .compiler import (
    CompiledContract,
    ContractCompileError,
    compile_acceptance_contract,
)
from .process_validator import (
    ValidatorOutcome,
    run_process_validator,
    validator_platform_ready,
)
from .registry import (
    VALIDATOR_REGISTRY,
    ValidatorEntry,
    contract_validator_entries,
    resolve_validator,
)
from .runner import run_contract_validation
from .snapshot import ArtifactSnapshot, SnapshotMaterializeError, load_artifact_snapshot

__all__ = [
    "CompiledContract",
    "ContractCompileError",
    "compile_acceptance_contract",
    "ValidatorOutcome",
    "run_process_validator",
    "validator_platform_ready",
    "VALIDATOR_REGISTRY",
    "ValidatorEntry",
    "contract_validator_entries",
    "resolve_validator",
    "run_contract_validation",
    "ArtifactSnapshot",
    "SnapshotMaterializeError",
    "load_artifact_snapshot",
]
