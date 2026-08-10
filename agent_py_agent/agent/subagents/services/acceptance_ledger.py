"""主链机器验收裁决持久化落账（R3 权威库主链接线）。

verify_done_acceptance 的机器裁决（不采信模型自报）之前只活在本轮 test
dict 与 task.verification_status/blockers 上，从不写 runtime.db 权威验收
账本——acceptance_contracts / validator_operations 两张表在生产链零消费
（真机实证：带验收断言的品牌单页任务「验收通过」后两表均 0 行）。本模块
把裁决结果编译/冻结/落账（带 G2 补尾的 assertion_key），使「机器实际
执行过什么、结果如何」成为可审计持久事实（3.txt §12 判定表「required
assertion 按 assertion ID 精确闭合」+ LLM-A「validator_evidence」要求）。

铁律：
- 纯落账，fail-silent：任何异常只记日志不上抛，绝不改变现有裁决/返工门/
  收口行为（主链收口仍走 ISSUE_UNVERIFIED_DONE → BLOCKED 机制）。
- 只取机器盖章的条目（test["verified_by"] == "machine_execution"）：
  模型自报/不可机验条目天然排除，落账信号与裁决信号同源（同一 dict）。
- 契约 task_run 级唯一（I.6 CAS 防分叉）：已有契约复用不重编——重放、
  双子代理同 TaskRun 收口、返工重开都不触发 CONTRACT_DIVERGED。
- 断言按 assertion_key 分组（编译器拒绝重复 key）：组内全过才 VERIFIED，
  任一失败 → FAILED（fail-closed，与 closeout key 判定单位一致）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...acceptance.compiler import ContractCompileError, compile_acceptance_contract
from ...acceptance.registry import resolve_validator
from ..models import SubAgentTask, TaskStatus, task_has_status

logger = logging.getLogger(__name__)

#: 主链机器裁决方式 → R3 注册表 ref（registry.py VALIDATOR_REGISTRY）。
#: 查不到 = 不可机验方式（command 等），不落账。
_METHOD_REF = {
    "file_check": "artifact_acceptance",
    "content_check": "artifact_acceptance",
    "static_site_check": "static_site_check",
    "artifact_integrity": "artifact_acceptance",
}

#: 可推断的 artifact kind 白名单：与 contracts/artifact_acceptance.py 的
#: 真实验证能力对齐（html/md/json/csv/docx/xlsx/pdf 均有原生校验器）；
#: 其余后缀/无路径 → '*'（通配，与编译器 kind 缺省语义一致）。
_INFERABLE_KINDS = frozenset({"html", "md", "json", "csv", "docx", "xlsx", "pdf"})

_MESSAGE_MAX_CHARS = 600


def _infer_artifact_kind(test: dict) -> str:
    """file_check/content_check 的 artifact_kind 推断；无法推断 → '*'。"""
    raw = str(test.get("file_path") or "").strip()
    if not raw:
        return "*"
    suffix = Path(raw).suffix.lower().lstrip(".")
    return suffix if suffix in _INFERABLE_KINDS else "*"


def _ledgerize_assertions(tests: list[dict]) -> list[dict]:
    """从机器盖章的 tests[] 生成断言分组（按 assertion_key 唯一）。

    返回 [{"validator", "artifact_kind", "ok"(组内全过), "message"(首个失败
    事实)}...]；无盖章/无可映射条目 → []。kind 规则：file_check/
    content_check 按文件扩展名推断；static_site_check（站点级）与
    artifact_integrity（产物集级）固定 '*'。
    """
    groups: dict[str, dict] = {}
    for test in tests or []:
        if not isinstance(test, dict):
            continue
        if test.get("verified_by") != "machine_execution":
            continue
        method = str(test.get("validation_method") or "").strip().lower()
        ref = _METHOD_REF.get(method)
        if ref is None:
            continue
        if method in ("static_site_check", "artifact_integrity"):
            kind = "*"
        else:
            kind = _infer_artifact_kind(test)
        key = f"{ref}::{kind}"
        group = groups.setdefault(
            key, {"validator": ref, "artifact_kind": kind, "ok": True, "message": ""}
        )
        ok = bool(test.get("ok"))
        group["ok"] = group["ok"] and ok
        if not ok and not group["message"]:
            group["message"] = str(test.get("message") or "").strip()[:_MESSAGE_MAX_CHARS]
    return list(groups.values())


def _contract_id(contract) -> str:
    return contract["contract_id"] if isinstance(contract, dict) else contract.contract_id


def _ledgerize(
    task: SubAgentTask,
    tests: list[dict],
    result,
    *,
    repo,
) -> None:
    """落账主流程；调用方已保证异常不外逸。"""
    if repo is None:
        return  # LOCAL_UNMANAGED：无权威库可写，跳过即正确语义
    if not task_has_status(task, TaskStatus.DONE):
        return
    if not bool(getattr(result, "checked", False)):
        return  # 机器未执行（NOT_DONE/无可机验条目/无工作区/沙箱不可用）——无可落账事实
    assertions = _ledgerize_assertions(tests)
    if not assertions:
        return
    agent_run = repo.agent_run_for_run_id(str(getattr(task, "id", "") or "").strip())
    if agent_run is None:
        logger.warning("acceptance ledger: 权威记录缺失(run=%s), 跳过落账", getattr(task, "id", ""))
        return
    task_run_id = str(agent_run["task_run_id"])
    agent_run_id = str(agent_run["agent_run_id"])
    attempt_id = str(agent_run["current_attempt_id"])

    active = repo.current_contract(task_run_id)
    if active is None:
        # 首冻者定契约（I.6 CAS 防分叉）；编译失败防御性跳过（理论不可达：
        # key 已去重 + ref 全命中注册表 + 全部 required）。
        try:
            compiled = compile_acceptance_contract(
                task_run_id=task_run_id,
                attempt_id=attempt_id,
                proposed={"assertions": assertions},
            )
            repo.freeze_contract(
                contract_id=compiled.contract_id,
                task_run_id=task_run_id,
                attempt_id=attempt_id,
                compiled=compiled.compiled,
                digest=compiled.digest,
                inert_legacy=compiled.inert_legacy,
            )
        except ContractCompileError as exc:
            logger.warning("acceptance ledger: 契约编译失败(task_run=%s): %s", task_run_id, exc)
            return
        active = compiled

    # 锚定：只写契约中真实存在的 assertion_key（差异断言不落账，不污染账本）。
    contract_keys = {
        str(a.get("assertion_key") or "") for a in (active["compiled"] or {}).get("assertions", [])
    } if isinstance(active, dict) else {
        str(a.get("assertion_key") or "")
        for a in (active.compiled or {}).get("assertions", [])
    }
    for group in assertions:
        key = f"{group['validator']}::{group['artifact_kind']}"
        if key not in contract_keys:
            continue
        entry = resolve_validator(group["validator"])
        if entry is None:
            continue
        existing = repo.validator_operation_by_assertion(
            contract_id=_contract_id(active), assertion_key=key, attempt_id=attempt_id
        )
        if existing is not None:
            if existing["status"] == "PENDING":
                # 中途崩溃残留：用当前裁决 settle 收尾（终态跳过=幂等）。
                _settle(repo, existing["operation_id"], group)
            continue
        operation_id = repo.create_validator_operation(
            attempt_id=attempt_id,
            agent_run_id=agent_run_id,
            contract_id=_contract_id(active),
            assertion_key=key,
            validator_ref=entry.name,
            validator_kind=entry.kind,
            code_digest=entry.code_digest,
            artifact_digests=[],
        )
        _settle(repo, operation_id, group)


def _settle(repo, operation_id: str, group: dict) -> None:
    """按组裁决 settle 终态：组内全过 VERIFIED(exit 0)；任一失败 FAILED(exit 1)。

    stderr_text 用机器盖章的失败事实（_stamp_machine_fact 拼接的 exit code
    + stderr 尾部，随 decision ledger 出站的同源文本）；stdout 不虚构（文件
    检查无 stdout 捕获）。exit_code 是结构化信号（0/1），不解析文本。
    """
    if group["ok"]:
        repo.settle_validator_operation(
            operation_id, status="VERIFIED", stdout_text="", exit_code=0
        )
    else:
        repo.settle_validator_operation(
            operation_id,
            status="FAILED",
            stderr_text=group["message"],
            exit_code=1,
        )


def ledgerize_done_acceptance(
    task: SubAgentTask,
    tests: list[dict],
    result,
    *,
    owner_home: str = "",
    repo=None,
) -> None:
    """把已完成的机器裁决持久化落账（fail-silent，绝不向上抛）。

    owner_home 与 verify_done_acceptance 语义一致，本函数不执行任何文件
    操作（机器执行已在裁决内完成），保留参数仅为签名对称与审计说明。
    """
    try:
        _ledgerize(task, tests, result, repo=repo)
    except Exception as exc:  # noqa: BLE001 —— 落账故障不得影响主链裁决/收口
        logger.warning(
            "acceptance ledger 落账失败(run=%s): %s", getattr(task, "id", ""), exc, exc_info=True
        )


__all__ = ["ledgerize_done_acceptance"]
