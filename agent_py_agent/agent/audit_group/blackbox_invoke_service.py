# LLM: 调用者权限与原件启停每次现查；私有快照不得序列化到caller目录。
# 模块用途: 编排跨owner四类资产黑盒执行并保存安全调用回执。
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from ..attempt.sandbox import SandboxUnavailableError
from ..plugin_host.errors import HostAPIError
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .access_policy import AuditGroupError, owner_identity
from .blackbox_execution import SnapshotExecutor, generated_artifacts, workspace_inventory
from .caller_controls import caller_policy, release_buffer, reserve_buffer, save_public_result
from .isolated_unit import IsolatedScriptUnit
from .safe_projection import scrub_private_result
from .source_snapshot import SourceAssetSnapshot


# LLM: monitor_rule只是monitor的准确规则引用别名，不授其它种类或模糊选择；显式rule_id优先于简写解析。
# 函数用途: 为所有调用入口归一资产类型及monitor:rule简写。
def normalize_asset_reference(kind, identifier, rule_id=None):
    rule_id = "" if rule_id is None else rule_id
    if type(rule_id) is not str:
        raise AuditGroupError("INVALID_RULE_ID", status_code=400)
    if kind == "monitor_rule":
        kind = "monitor"
        if not rule_id and type(identifier) is str and ":" in identifier:
            identifier, rule_id = identifier.split(":", 1)
    return kind, identifier, rule_id


# LLM: 宿主root构造，无来源owner假扮或源码公开接口。
# 类用途: 让审计组只使用他人资产并将脱敏结果归己。
class AuditGroupBlackboxInvokeService:
    # LLM: 初始化无副作用。
    # 函数用途: 固定宿主根。
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir).resolve()

    # LLM: 只有安全合同资产能执行，规则别名先归一，目标规则先选中再冻结source_owner依赖。
    # 函数用途: 执行一次资产并返回脱敏回执。
    def invoke_asset(self, caller_owner, *, source_owner, asset_type, asset_id,
                     expected_revision=None, inputs=None, rule_id="", timeout_s=30.0):
        asset_type, asset_id, rule_id = normalize_asset_reference(asset_type, asset_id, rule_id)
        if asset_type not in {"script", "skill", "workflow", "monitor"}:
            raise AuditGroupError("AUDIT_GROUP_ASSET_NOT_INVOCABLE", status_code=400)
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 3600:
            raise AuditGroupError("INVALID_TIMEOUT", status_code=400)
        inputs = {} if inputs is None else inputs
        if type(inputs) is not dict or len(json.dumps(inputs, allow_nan=False)) > 65536:
            raise AuditGroupError("INVALID_INPUTS", status_code=400)
        try:
            home, policy = caller_policy(self.root_dir, caller_owner, asset_type)
            if owner_identity(source_owner) == home.identity:
                raise AuditGroupError("AUDIT_GROUP_SELF_INVOCATION_UNSUPPORTED", status_code=409)
            snapshot = SourceAssetSnapshot.freeze(self.root_dir, source_owner, asset_type, asset_id, rule_id=rule_id)
            if expected_revision is not None and (type(expected_revision) not in (str, int)
                                                  or str(expected_revision) != str(snapshot.revision)):
                raise AuditGroupError("REVISION_CONFLICT", status_code=409)
            return self._execute(home, caller_owner, policy, snapshot, inputs, rule_id, timeout_s)
        except AuditGroupError:
            raise
        except (OwnerQuotaUnavailable, OwnerQuotaExceeded):
            raise AuditGroupError("AUDIT_GROUP_QUOTA_DENIED", status_code=409) from None
        except SandboxUnavailableError:
            raise AuditGroupError("AUDIT_GROUP_SANDBOX_UNAVAILABLE", status_code=503) from None
        except (OSError, ValueError, TypeError, KeyError, HostAPIError, RecursionError):
            raise AuditGroupError("AUDIT_GROUP_EXECUTION_UNAVAILABLE", status_code=503) from None

    # LLM: 代码仅驻内存，源复核始终保留冻结规则；独占workspace不扫描caller普通项目，清理后才保存回执。
    # 函数用途: 执行冻结依赖，整批发表脱敏产物，不扩大隔离单元写权限或混入并发私人文件。
    def _execute(self, home, caller, policy, snapshot, inputs, rule_id, timeout_s):
        identifier = "inv_" + uuid.uuid4().hex
        parts = ("data", "audit_group_exec", identifier)
        staging = home.home_dir.joinpath(*parts)
        size = sum(len(raw.get("code", "").encode()) for raw in snapshot._records.values())
        reserve_buffer(home, parts, min(16 * 1024 * 1024, size + 2 * 1024 * 1024))
        try:
            with TemporaryDirectory(prefix="workspace-", dir=staging) as directory:
                workspace = Path(directory)
                before = workspace_inventory(workspace)
                self._revalidate(caller, policy, snapshot, snapshot.kind)
                executor = SnapshotExecutor(snapshot, IsolatedScriptUnit(), workspace, staging, timeout_s,
                    authorize=lambda kind: self._revalidate(caller, policy, snapshot, kind))
                result = executor.run(snapshot.kind, snapshot.identifier, inputs, rule_id=rule_id)
                _home, current_policy = caller_policy(self.root_dir, caller, snapshot.kind)
                if current_policy != policy:
                    raise AuditGroupError("GOVERNANCE_POLICY_CHANGED", status_code=409)
                current = SourceAssetSnapshot.freeze(self.root_dir, snapshot.owner_id, snapshot.kind, snapshot.identifier,
                                                     rule_id=snapshot.rule_id)
                if current.fingerprint != snapshot.fingerprint:
                    raise AuditGroupError("REVISION_CONFLICT", status_code=409)
                public = {"invocation_id": identifier, "source_asset": {"owner_id": snapshot.owner_id,
                    "kind": snapshot.kind, "id": snapshot.identifier, "source_code_exposed": False},
                    "caller_owner_id": home.owner_id, "output_workspace_owner": home.owner_id,
                    "status": "succeeded", "result": scrub_private_result(result, snapshot._records),
                    "generated_artifacts": generated_artifacts(home, parts, before, snapshot._records, workspace=workspace)}
            save_public_result(home, parts, public)
            return public
        finally:
            release_buffer(home, parts)

    # LLM: 每个单元前重核真实caller、对应工具策略及同一选定规则；依赖禁用或变更不能继续后续步骤。
    # 函数用途: 将实时撤权和源版本复查放到每个执行单元边界。
    def _revalidate(self, caller, policy, snapshot, kind):
        _home, current = caller_policy(self.root_dir, caller, kind)
        if current != policy:
            raise AuditGroupError("GOVERNANCE_POLICY_CHANGED", status_code=409)
        observed = SourceAssetSnapshot.freeze(self.root_dir, snapshot.owner_id, snapshot.kind, snapshot.identifier,
                                              rule_id=snapshot.rule_id)
        if observed.fingerprint != snapshot.fingerprint:
            raise AuditGroupError("REVISION_CONFLICT", status_code=409)
