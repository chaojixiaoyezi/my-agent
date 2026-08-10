"""契约验收执行（3.txt I.11/H.9/A.8）。

- 每条 required assertion 落一次 ValidatorOperation（A.8：追 attempt_id，
  记 code digest/argv/stdout/stderr/artifact digest）。
- pure：进程内受信函数；process：只读沙箱固定 argv（I.8）。
- snapshot 无效（digest 不匹配 / 无匹配文件）→ FAILED，绝不拿
  live workspace 当输入（H.9）。
- 结果只产生 VERIFIED/FAILED/UNAVAILABLE/BLOCKED；交付判定由调用方
  聚合：required 断言全部 VERIFIED 才算验收通过（WORK_DONE ≠ VERIFIED，
  I.11）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..contracts.artifact_acceptance import (
    ArtifactAcceptanceReport,
    ArtifactAcceptanceRequest,
)
from .process_validator import ValidatorOutcome, run_process_validator
from .registry import ValidatorEntry
from .snapshot import ArtifactSnapshot


def _snapshot_paths_for_kind(snapshot: ArtifactSnapshot, artifact_kind: str) -> list[str]:
    """snapshot 内匹配 artifact_kind 的相对路径（kind 缺省 → 全部文件）。"""
    kind = str(artifact_kind or "").strip().lower()
    if not kind:
        return [str(item["rel_path"]) for item in snapshot.files]
    matches: list[str] = []
    for item in snapshot.files:
        rel = str(item["rel_path"]).lower()
        suffix = rel.rsplit(".", 1)[-1] if "." in rel else ""
        if kind in (suffix, rel) or rel.endswith(f"/{kind}"):
            matches.append(str(item["rel_path"]))
    return matches


def run_contract_validation(
    *,
    repo: Any,
    attempt_id: str,
    agent_run_id: str,
    contract: dict[str, Any],
    snapshot: ArtifactSnapshot,
    owner_home: Any,
    entries: dict[str, ValidatorEntry],
    contract_id: str = "",
) -> list[dict[str, Any]]:
    """执行契约全部 required 断言；每条结果带 operation_id（可审计）。

    H.9：先把已验证 snapshot 物化到独立临时目录，全部 validator 只读
    物化副本 —— live workspace 在验证后、执行前被改（TOCTOU）不影响
    验收输入；物化目录随本函数结束自动清理。
    """
    with tempfile.TemporaryDirectory(prefix="artifact-snapshot-") as tmp:
        materialized = snapshot.materialize(Path(tmp))
        return _run_assertions_against(
            repo=repo,
            attempt_id=attempt_id,
            agent_run_id=agent_run_id,
            contract=contract,
            snapshot=materialized,
            owner_home=owner_home,
            entries=entries,
            contract_id=contract_id,
        )


def _run_assertions_against(
    *,
    repo: Any,
    attempt_id: str,
    agent_run_id: str,
    contract: dict[str, Any],
    snapshot: ArtifactSnapshot,
    owner_home: Any,
    entries: dict[str, ValidatorEntry],
    contract_id: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for assertion in contract.get("assertions", []):
        ref = str(assertion.get("validator_ref") or "")
        entry = entries.get(ref)
        if entry is None:
            raise ValueError(f"断言引用未注册 validator: {ref!r}")
        kind = str(assertion.get("artifact_kind") or "")
        paths = _snapshot_paths_for_kind(snapshot, kind)
        artifact_digests = [
            str(item["digest"])
            for item in snapshot.files
            if str(item["rel_path"]) in paths
        ]
        operation_id = repo.create_validator_operation(
            attempt_id=attempt_id,
            agent_run_id=agent_run_id,
            contract_id=contract_id,
            validator_ref=ref,
            validator_kind=str(entry.kind),
            code_digest=str(entry.code_digest),
            argv=[],
            artifact_digests=artifact_digests,
        )
        if entry.kind == "process":
            outcome = _run_process_assertion(
                repo=repo,
                operation_id=operation_id,
                entry=entry,
                snapshot=snapshot,
                paths=paths,
                owner_home=owner_home,
            )
        else:
            outcome = _run_pure_assertion(
                repo=repo,
                operation_id=operation_id,
                entry=entry,
                snapshot=snapshot,
                paths=paths,
                assertion=assertion,
            )
        results.append(
            {
                "validator_ref": ref,
                "operation_id": operation_id,
                "status": outcome["status"],
            }
        )
    return results


def _run_pure_assertion(
    *,
    repo: Any,
    operation_id: str,
    entry: ValidatorEntry,
    snapshot: ArtifactSnapshot,
    paths: list[str],
    assertion: dict[str, Any],
) -> dict[str, str]:
    """进程内受信函数验收（I.7）。至少一个匹配文件 VERIFIED → 断言过。"""
    fn = entry.fn
    if fn is None:
        repo.settle_validator_operation(
            operation_id, status="BLOCKED", stderr_text="pure 条目缺实现"
        )
        return {"status": "BLOCKED", "detail": "pure 条目缺实现"}
    if not paths:
        repo.settle_validator_operation(
            operation_id,
            status="FAILED",
            stderr_text=f"snapshot 无匹配 artifact_kind={assertion.get('artifact_kind')} 的文件（H.9）",
        )
        return {"status": "FAILED", "detail": "无匹配文件"}
    verified_any = False
    last_report: ArtifactAcceptanceReport | None = None
    for rel in paths:
        request = ArtifactAcceptanceRequest(
            path=snapshot.shared_root / rel,
            workspace_root=snapshot.shared_root,
            validation_contract={"artifact_kind": assertion.get("artifact_kind")},
        )
        report = fn(request)
        last_report = report
        if report.ok:
            verified_any = True
            break
    if verified_any:
        repo.settle_validator_operation(
            operation_id, status="VERIFIED", stdout_text="pure validator ok", exit_code=0
        )
        return {"status": "VERIFIED", "detail": ""}
    details = "\n".join(f.to_dict()["code"] for f in (last_report.findings if last_report else ()))
    repo.settle_validator_operation(
        operation_id, status="FAILED", stderr_text=f"pure validator rejected: {details}"
    )
    return {"status": "FAILED", "detail": details}


def _run_process_assertion(
    *,
    repo: Any,
    operation_id: str,
    entry: ValidatorEntry,
    snapshot: ArtifactSnapshot,
    paths: list[str],
    owner_home: Any,
) -> dict[str, str]:
    if not paths:
        repo.settle_validator_operation(
            operation_id, status="FAILED", stderr_text="snapshot 无匹配文件"
        )
        return {"status": "FAILED", "detail": "无匹配文件"}
    # 固定 argv 的进程验收器只接受一个 snapshot 根参数；匹配文件传 env 不可靠，
    # 语义：validator 自己扫描 snapshot 根。传 snapshot 根 + 首个匹配文件相对路径。
    target = snapshot.path_for(paths[0])
    outcome = run_process_validator(
        entry=entry,
        artifact_snapshot=target if target is not None else snapshot.shared_root,
        owner_home=owner_home,
        artifact_digests=[
            str(item["digest"]) for item in snapshot.files if str(item["rel_path"]) in paths
        ],
    )
    repo.settle_validator_operation(
        operation_id,
        status=outcome.status,
        stdout_text=outcome.stdout,
        stderr_text=outcome.stderr,
        exit_code=outcome.exit_code,
        argv=outcome.argv,
        env=outcome.env,
    )
    return {"status": outcome.status, "detail": outcome.stderr}
