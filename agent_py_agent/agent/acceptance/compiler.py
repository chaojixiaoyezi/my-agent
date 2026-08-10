"""Contract Compiler（3.txt I.1-I.6）。

模型只能 propose acceptance assertions（I.1）；AcceptanceContract 由
框架在 dispatch 前编译、校验、冻结（I.2）：

- I.3：command/cwd/working_dir/裸程序结构性不进契约 —— 编译器把它们
  剥离到 inert_legacy（只作 evidence，I.4：任何读取不得触发 subprocess，
  编译器只做 dict 操作）。
- I.5：requiredness 不能被模型从 required 降为 advisory —— 注册表
  required_by_default 的 ref 被标 advisory 时升回 required。
- I.7：validator ref 必须命中可信注册表，unknown 直接拒绝。
- 冻结产物带 canonical digest；contract_id 由框架铸造（B.1）。

编译输出（compiled）结构：
    {
        "assertions": [ {artifact_kind, validator_ref, validator_kind,
                         code_digest, argv_template, required} ... ],
        "advisory_assertions": [...同结构, required=False 且未被升格...],
        "constraints": [...],
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..common.id_generator import new_id
from .registry import LEGACY_EXEC_KEYS, ValidatorEntry, resolve_validator


class ContractCompileError(ValueError):
    """编译/校验失败：模型 propose 不满足 I.3/I.5/I.7。"""


@dataclass(frozen=True)
class CompiledContract:
    """冻结契约（不可变）：digest 校验 + task_runs.current_contract_id CAS。"""

    contract_id: str
    task_run_id: str
    attempt_id: str
    compiled: dict[str, Any]
    digest: str
    inert_legacy: dict[str, Any] = field(default_factory=dict)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_of(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _compile_assertion(
    item: dict[str, Any],
    *,
    index: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """编译一条 propose 断言 → (required 断言, advisory 断言|None)。

    I.5：entry.required_by_default 且 propose required=False → 升回
    required（升格记录进 required 断言本身）。advisory 只用于非核心项。
    """
    ref = str(item.get("validator") or item.get("validator_ref") or "").strip()
    if not ref:
        raise ContractCompileError(f"assertions[{index}]: 缺 validator ref")
    entry = resolve_validator(ref)
    if entry is None:
        raise ContractCompileError(
            f"assertions[{index}]: unknown validator ref {ref!r}（I.7）"
        )
    artifact_kind = str(item.get("artifact_kind") or item.get("kind") or "").strip()
    proposed_required = bool(item.get("required", True))
    required = proposed_required or entry.required_by_default
    assertion = {
        # G2 补尾（3.txt「不能只记 validator_ref」）：断言稳定标识 = 验证器
        # 名 + 匹配 kind 的组合。同一契约下同 ref 不同 kind 是两条不同断言，
        # 行级记录必须能区分是哪条被验证/被 BLOCKED；kind 缺省用 '*'（与
        # snapshot 匹配「kind 缺省 → 全部文件」语义一致）。
        "assertion_key": f"{entry.name}::{artifact_kind or '*'}",
        "artifact_kind": artifact_kind,
        "validator_ref": entry.name,
        "validator_kind": entry.kind,
        "code_digest": entry.code_digest,
        "version": entry.version,
        "argv_template": list(entry.argv_template),
        "required": required,
    }
    if required:
        return assertion, None
    return None, assertion


def compile_acceptance_contract(
    *,
    task_run_id: str,
    attempt_id: str,
    proposed: dict[str, Any] | None,
) -> CompiledContract:
    """编译 + 校验 + 冻结模型 propose 的验收要求（I.2）。

    输入形态兼容模型现状（validation_contract dict）：
    - 顶层 `validator` → 单断言（artifact_kind 从顶层 kind/artifact_kind 取）；
    - `assertions` 列表 → 多条断言。
    任何 command/cwd/working_dir/executable/script/args/shell 字段被
    剥离到 inert_legacy（I.3/I.4），不进 compiled 执行面。
    """
    proposed = dict(proposed or {})
    task_run_id = str(task_run_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not task_run_id or not attempt_id:
        raise ContractCompileError("compile 需要 task_run_id 与 attempt_id")

    # I.3/I.4：执行字段剥离为 inert evidence（只读 dict 操作，零 subprocess）。
    inert_legacy: dict[str, Any] = {}
    for key in LEGACY_EXEC_KEYS:
        if key in proposed:
            inert_legacy[key] = proposed.pop(key)

    raw_assertions = proposed.get("assertions")
    if isinstance(raw_assertions, list) and raw_assertions:
        items: list[dict[str, Any]] = [
            dict(item) for item in raw_assertions if isinstance(item, dict)
        ]
        top_ref = str(proposed.get("validator") or "").strip()
        if top_ref:
            # 顶层 validator 与 assertions 并存 → 拒绝（协议歧义）。
            raise ContractCompileError(
                "validation_contract 不得同时带顶层 validator 与 assertions"
            )
    else:
        ref = str(proposed.get("validator") or "").strip()
        kind = str(
            proposed.get("artifact_kind") or proposed.get("kind") or ""
        ).strip()
        items = [{"validator": ref, "artifact_kind": kind}] if ref else []

    required: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        req, adv = _compile_assertion(item, index=index)
        if req is not None:
            required.append(req)
        if adv is not None:
            advisory.append(adv)

    # G2 补尾：assertion_key 全契约唯一（含 advisory）——同验证器同 kind 的
    # 重复断言是语义歧义（closeout 按 key 判定，重复会让「哪条算验证过」
    # 不可裁决），编译即拒绝；不同 kind 的多断言合法保留（key 可区分）。
    keys = [str(a["assertion_key"]) for a in [*required, *advisory]]
    dup = next((k for k in sorted(keys) if keys.count(k) > 1), None)
    if dup is not None:
        raise ContractCompileError(f"assertions 含重复断言标识 {dup!r}（同验证器同 kind 只能一条）")

    compiled: dict[str, Any] = {
        "assertions": required,
        "advisory_assertions": advisory,
        "constraints": proposed.get("constraints", []),
        "required_artifact_kinds": sorted(
            {str(a.get("artifact_kind") or "") for a in required if a.get("artifact_kind")}
        ),
    }
    if not required:
        raise ContractCompileError("契约必须至少一条 required assertion（I.11："
                                   "WORK_DONE ≠ VERIFIED，无验收断言不可交付）")
    digest = _digest_of(compiled)
    return CompiledContract(
        contract_id=new_id("contract_id"),
        task_run_id=task_run_id,
        attempt_id=attempt_id,
        compiled=compiled,
        digest=digest,
        inert_legacy=inert_legacy,
    )
