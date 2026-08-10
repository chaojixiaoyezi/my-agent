"""PureValidator 可信版本注册表（3.txt I.7）。

模型 propose 的 validator ref 必须命中本注册表（编译期校验，I.7
fail-closed：unknown ref 直接拒绝，绝不执行任意裸程序）。每条目绑定
实现 + 版本 + code digest（函数源码 sha256），契约冻结时把 ref 解析为
(kind, code_digest, argv_template) 写进契约 —— 验收行为与具体实现
版本绑定，实现升级必须显式换版本。

- kind="pure"：进程内受信函数（无 subprocess）。
- kind="process"：SandboxedProcessValidator，固定 argv 模板（I.8），
  模板与实现一起受 digest 保护。产品内置列表为空；部署方在受信环境
  注册。R3 测试用注入条目验证机制。
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..contracts.artifact_acceptance import (
    validate_by_artifact_kind,
    validate_static_site_artifact,
    _validate_xlsx_request,
    _validate_pdf_request,
)

#: I.3：模型永远不能提交的执行字段（编译器剥离为 inert evidence）。
#: 注意 registry 是纯校验，不执行这些字段。
LEGACY_EXEC_KEYS = frozenset(
    {"command", "cwd", "working_dir", "executable", "script", "args", "shell"}
)


@dataclass(frozen=True)
class ValidatorEntry:
    """注册表条目：ref 名 → 受信实现（版本 + digest 绑定）。"""

    name: str
    kind: str                    # "pure" | "process"
    version: str
    code_digest: str
    # I.5：核心验收器 required_by_default=True 时，模型标 advisory
    # 也会被编译器升回 required（requiredness 只能升不能降）。
    required_by_default: bool = True
    # pure：ArtifactValidator 签名 (ArtifactAcceptanceRequest) -> Report
    fn: Callable[..., Any] | None = None
    # process：固定 argv 模板；执行时末尾追加 artifact snapshot 路径。
    argv_template: tuple[str, ...] = ()


def _function_digest(fn: Callable[..., Any]) -> str:
    source = inspect.getsource(fn)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _pure_entry(name: str, fn: Callable[..., Any], *, version: str = "1") -> ValidatorEntry:
    return ValidatorEntry(
        name=name,
        kind="pure",
        version=version,
        code_digest=_function_digest(fn),
        fn=fn,
    )


#: 可信版本注册表（只读）。新增条目 = 产品发布变更，非模型运行时动作。
VALIDATOR_REGISTRY: dict[str, ValidatorEntry] = {
    "artifact_acceptance": _pure_entry("artifact_acceptance", validate_by_artifact_kind),
    "static_site_check": _pure_entry("static_site_check", validate_static_site_artifact),
    "spreadsheet_acceptance": _pure_entry("spreadsheet_acceptance", _validate_xlsx_request),
    "document_acceptance": _pure_entry("document_acceptance", _validate_pdf_request),
}


def resolve_validator(ref: str) -> ValidatorEntry | None:
    """按 ref 查注册表；unknown → None（编译期拒绝，I.7）。"""
    return VALIDATOR_REGISTRY.get(str(ref or "").strip())


def contract_validator_entries(refs: list[str]) -> list[ValidatorEntry]:
    """批量解析（编译用）：全部命中才返回；任一 unknown → 抛 ValueError。"""
    entries: list[ValidatorEntry] = []
    for ref in refs:
        entry = resolve_validator(ref)
        if entry is None:
            raise ValueError(f"未知 validator ref: {ref!r}（I.7：必须命中可信注册表）")
        entries.append(entry)
    return entries
