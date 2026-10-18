# LLM: 固定原typed plan和policy摘要；callback先T后原retention锁，目标状态/指纹仍由main原executor复核。
# 模块用途: 为唯一MemoryRetentionService组合逐步短提交，不建立第二scanner、状态或授权。
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..common.json_io import locked_json_path
from .retention_models import MemoryRetentionReport
from .retention_scan import file_fingerprint


# LLM: 只表示原扫描策略字节变化，不代表执行门未知、进入情况或重新执行许可。
# 类用途: 向调用方明确拒绝继续消费已漂移的保留策略快照。
class RetentionPolicyChangedError(RuntimeError):
    error_code = "MEMORY_RETENTION_POLICY_CHANGED"


# LLM: 固定plan只取原Service；不持T扫描，逐批候选/动作/审计重核原policy和业务锁，保main清单引用保护。
# 函数用途: 把既有保留计划接到原请求短提交回调。
def apply_with_transition(service, plan, now, transition):
    from .retention import _has_policy_level_error, _without_errored_subtrees
    from .retention_apply import execute_retention_plan, record_retention_report

    lock_anchor = Path(service.home.owner_trash_dir) / ".memory-retention-v2"

    # LLM: 原transition同步准入后再取业务锁，拒旧plan套新policy；不在业务锁内倒取T。
    # 函数用途: 在固定策略快照下完成一个原保留提交步骤。
    def commit(operation):
        # LLM: 此body位于原T短窗口，只有policy原字节一致才执行原目标CAS。
        # 函数用途: 在原保留锁内复核同次策略并提交原操作。
        def execute():
            with locked_json_path(lock_anchor):
                if file_fingerprint(Path(service.home.owner_retention_json)) != plan.policy_fingerprint:
                    raise RetentionPolicyChangedError("original_retention_policy_changed")
                return operation()
        return transition(execute)

    if plan.legal_hold or _has_policy_level_error(plan.errors):
        stopped = MemoryRetentionReport(applied=False, actions=(), errors=plan.errors,
            legal_hold=plan.legal_hold, policy_fingerprint=plan.policy_fingerprint)
        commit(lambda: record_retention_report(home=service.home, report=stopped, now=now))
        return stopped
    executable = replace(_without_errored_subtrees(plan), isolated_errors=plan.errors)
    applied = execute_retention_plan(home=service.home, candidates=service.candidates, plan=executable,
        now=now, allowed_external_roots=service.conversation_roots, commit_transition=commit,
        audit_transition=commit)
    if plan.errors and applied.errors != plan.errors:
        return replace(applied, errors=(*applied.errors, *plan.errors))
    return applied
