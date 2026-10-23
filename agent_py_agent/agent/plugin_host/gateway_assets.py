# LLM: 只从目标Owner规范资产存储读取脚本、工作流与运行断点，绝不跨Owner回退或从请求载荷直接取代码执行。
# 模块用途: 为插件宿主v2生产组合提供注册资产核验、断点读取与受限资产执行端口。
from __future__ import annotations

import hashlib
import json
import time

from ..assets.script_store import ScriptStore
from ..assets.workflow_store import WorkflowStore
from ..tooling.sandbox import SandboxUnavailable
from ..workflows.engine import WorkflowEngine
from ..workflows.run_store import WorkflowRunStore
from ..workflows.script_runner import ScriptRunner
from .errors import HostAPIError
from .execution_policy import AssetGrant, require_asset
from .schemas import canonical_json


# LLM: 修订号优先读取描述符元数据中的正整数revision，其次读取纯数字version，缺省为1。
# 函数用途: 归一化已注册资产的整数修订号。
def _asset_revision(metadata: object, version: object) -> int:
    if isinstance(metadata, dict) and type(metadata.get("revision")) is int and metadata["revision"] >= 1:
        return metadata["revision"]
    if isinstance(version, str) and version.isdigit() and int(version) >= 1:
        return int(version)
    return 1


# LLM: 仅读取目标Owner私有ScriptStore（禁用全局extra_roots），同时返回授权AssetGrant与完整执行配置摘要。
# 函数用途: 读取当前Owner已注册脚本的AssetGrant与执行完整性指纹。
def _script_fingerprint(
    home, script_id: str, allowed_assets: tuple[AssetGrant, ...] = ()
) -> tuple[AssetGrant, str]:
    try:
        record = ScriptStore(home, extra_roots=()).get_script(script_id)
    except (ValueError, KeyError, OSError) as exc:
        raise HostAPIError("ASSET_NOT_FOUND", 404) from exc
    if record is None:
        raise HostAPIError("ASSET_NOT_FOUND", 404)
    rev = _asset_revision(record.get("metadata"), record.get("version"))
    code_digest = hashlib.sha256(str(record["code"]).encode("utf-8")).hexdigest()
    stable = {k: v for k, v in record.items() if k not in {"entrypoint_path", "runtime_summary"}}
    record_digest = hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()
    candidate = AssetGrant("script", script_id, rev, record_digest)
    grant = candidate if candidate in allowed_assets else AssetGrant("script", script_id, rev, code_digest)
    return grant, record_digest


# LLM: 仅读取目标Owner私有ScriptStore（禁用全局extra_roots），摘要由真实磁盘代码或规范记录计算。
# 函数用途: 读取并构造当前Owner已注册脚本的AssetGrant。
def _script_grant(home, script_id: str, allowed_assets: tuple[AssetGrant, ...] = ()) -> AssetGrant:
    return _script_fingerprint(home, script_id, allowed_assets)[0]


# LLM: 固定覆盖描述符（不含运行统计）、步骤与正文，不随兼容授权模式缩减校验范围。
# 函数用途: 计算工作流完整执行定义的规范化SHA-256摘要。
def _workflow_execution_digest(raw: dict) -> str:
    descriptor = raw.get("descriptor") or {}
    stable = {
        "descriptor": {k: v for k, v in descriptor.items() if k != "runtime_summary"},
        "steps": raw.get("steps"),
        "body_markdown": raw.get("body_markdown", ""),
    }
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


# LLM: 从规范化工作流字典计算摘要并比对已授权列表，保证Store定义与运行快照采用同一版本算法。
# 函数用途: 由工作流字典构造对应的AssetGrant。
def _workflow_grant_from_raw(
    workflow_id: str, raw: dict, allowed_assets: tuple[AssetGrant, ...] = ()
) -> AssetGrant:
    descriptor = raw["descriptor"]
    rev = _asset_revision(descriptor.get("metadata"), descriptor.get("version"))
    digest = _workflow_execution_digest(raw)
    body_digest = hashlib.sha256(canonical_json(raw["steps"]).encode("utf-8")).hexdigest()
    candidate = AssetGrant("workflow", workflow_id, rev, body_digest)
    if candidate in allowed_assets:
        return candidate
    return AssetGrant("workflow", workflow_id, rev, digest)


# LLM: 仅读取目标Owner私有WorkflowStore（禁用全局extra_roots），摘要覆盖完整步骤与正文。
# 函数用途: 读取并构造当前Owner已注册工作流的AssetGrant。
def _workflow_grant(home, workflow_id: str, allowed_assets: tuple[AssetGrant, ...] = ()) -> AssetGrant:
    try:
        definition = WorkflowStore(home, extra_roots=()).get_workflow(workflow_id)
    except (ValueError, KeyError, OSError) as exc:
        raise HostAPIError("ASSET_NOT_FOUND", 404) from exc
    if definition is None:
        raise HostAPIError("ASSET_NOT_FOUND", 404)
    return _workflow_grant_from_raw(workflow_id, definition.to_dict(), allowed_assets)


# LLM: 冻结工作流引用的每个脚本步骤当前版本与完整执行指纹；若策略显式声明了同ID脚本授权则必须精确命中。
# 函数用途: 核验并冻结工作流步骤依赖的所有脚本AssetGrant与完整执行记录摘要。
def _freeze_workflow_step_scripts(home, steps, policy) -> dict[str, tuple[AssetGrant, str]]:
    frozen: dict[str, tuple[AssetGrant, str]] = {}
    allowed = policy.allowed_assets
    for step in steps or ():
        if not isinstance(step, dict):
            continue
        script_id = str(step.get("script_id") or "").strip()
        if not script_id or script_id in frozen:
            continue
        grant, record_digest = _script_fingerprint(home, script_id, allowed)
        pinned = [item for item in allowed if item.kind == "script" and item.asset_id == script_id]
        if pinned:
            if any(item.revision == grant.revision for item in pinned) and grant not in pinned:
                raise HostAPIError("ASSET_CHANGED", 409)
            require_asset(policy, grant)
        frozen[script_id] = (grant, record_digest)
    return frozen


# LLM: 续跑必须核对同Owner运行账本与目标步骤存在性，并复核断点快照与当前注册工作流完整定义一致且未漂移。
# 函数用途: 验证工作流断点并返回对应工作流的当前AssetGrant。
def _resume_grant(context, payload: dict) -> AssetGrant:
    home = context.owner_home.home_dir
    run_id = payload["workflow_run_id"]
    from_step_id = payload["from_step_id"]
    try:
        store = WorkflowRunStore(home)
        record = store.load_run(run_id)
        snapshot = store.load_workflow_snapshot(run_id)
    except (FileNotFoundError, ValueError, KeyError, OSError) as exc:
        raise HostAPIError("CHECKPOINT_NOT_FOUND", 404) from exc
    if record.owner_id not in {"", context.owner_home.owner_id}:
        raise HostAPIError("CHECKPOINT_NOT_FOUND", 404)
    if snapshot.get("descriptor", {}).get("id") != record.workflow_id:
        raise HostAPIError("CHECKPOINT_NOT_FOUND", 404)
    if not any(step.step_id == from_step_id for step in record.steps):
        raise HostAPIError("CHECKPOINT_NOT_FOUND", 404)
    allowed = context.execution_policy.allowed_assets
    current_def = WorkflowStore(home, extra_roots=()).get_workflow(record.workflow_id)
    if current_def is None:
        raise HostAPIError("ASSET_NOT_FOUND", 404)
    current_raw = current_def.to_dict()
    current = _workflow_grant_from_raw(record.workflow_id, current_raw, allowed)
    snap_grant = _workflow_grant_from_raw(record.workflow_id, snapshot, allowed)
    if current != snap_grant or _workflow_execution_digest(current_raw) != _workflow_execution_digest(snapshot):
        raise HostAPIError("ASSET_CHANGED", 409)
    _freeze_workflow_step_scripts(home, snapshot.get("steps"), context.execution_policy)
    return snap_grant


# LLM: 按精确动作分派到目标Owner规范资产存储，不接受路径或通配符。
# 函数用途: 为ExecutionDomain读取当前注册资产或续跑断点对应的AssetGrant。
def registered_asset(context, payload: dict) -> AssetGrant:
    action = context.call.action
    home = context.owner_home.home_dir
    allowed = context.execution_policy.allowed_assets
    if action == "execute.script":
        return _script_grant(home, payload["script_id"], allowed)
    if action == "execute.workflow":
        grant = _workflow_grant(home, payload["workflow_id"], allowed)
        definition = WorkflowStore(home, extra_roots=()).get_workflow(payload["workflow_id"])
        if definition is not None:
            _freeze_workflow_step_scripts(home, definition.to_dict().get("steps"), context.execution_policy)
        return grant
    if action == "execute.workflow_resume":
        return _resume_grant(context, payload)
    raise HostAPIError("ACTION_NOT_AVAILABLE", 403)


# LLM: 优先读取授权budgets中的max_duration_seconds，兼容max_wall_time_ms，限制在[0.1, 3600]秒。
# 函数用途: 计算单次受限资产执行的授权总时长上限。
def _budget_timeout_seconds(context) -> float:
    budgets = getattr(getattr(context.source, "execution_policy", None), "budgets", None) or {}
    if "max_duration_seconds" in budgets:
        raw_seconds = float(budgets["max_duration_seconds"])
    elif "max_wall_time_ms" in budgets:
        raw_seconds = float(budgets["max_wall_time_ms"]) / 1000.0
    else:
        raw_seconds = 30.0
    return max(0.1, min(3600.0, raw_seconds))


# LLM: 每步脚本执行前均消费AssetStepFence并强制禁网、受限读写根与声明时长预算，防止执行中途撤权或篡改步骤脚本。
# 函数用途: 在目标Owner隔离目录内执行已获批脚本、工作流或工作流断点续跑。
def execute_registered_asset(context, plan, fence) -> tuple[bool, str]:
    fence.check()
    payload = json.loads(plan.payload_json)
    home = context.owner_home.home_dir
    policy = context.execution_policy
    max_timeout = _budget_timeout_seconds(context)
    deadline = time.monotonic() + max_timeout
    runner = ScriptRunner(home, extra_write_roots=policy.write_roots, public_read_roots=policy.read_roots, restrict_to_roots=True)
    if plan.action == "execute.script":
        try:
            result = runner.run_script(
                payload["script_id"],
                inputs=payload.get("inputs") or {},
                timeout_seconds=max_timeout,
                network_access=False,
            )
        except SandboxUnavailable as exc:
            fence.check()
            raise HostAPIError("HOST_NOT_READY", 503, "not_started") from exc
        except (ValueError, KeyError, OSError, RuntimeError):
            fence.check()
            return False, "SCRIPT_EXECUTION_FAILED"
        fence.check()
        return bool(result.ok), "" if result.ok else (result.error_code or "SCRIPT_EXECUTION_FAILED")
    if plan.action == "execute.workflow":
        definition = WorkflowStore(home, extra_roots=()).get_workflow(payload["workflow_id"])
        steps = definition.to_dict().get("steps") if definition is not None else ()
    else:
        steps = WorkflowRunStore(home).load_workflow_snapshot(payload["workflow_run_id"]).get("steps")
    frozen_scripts = _freeze_workflow_step_scripts(home, steps, policy)
    engine = WorkflowEngine(home)
    engine.runner = runner
    original_run = runner.run_script
    host_errors: list[HostAPIError] = []

    # LLM: 工作流每个脚本步骤启动前后必须复核撤权、剩余总时长与步骤脚本完整指纹栅栏，且固定禁用网络。
    # 函数用途: 包装工作流步骤脚本执行器以强制逐步栅栏、脚本哈希校验、总预算剩余时长与禁网约束。
    def _guarded_run_script(
        script_id,
        inputs=None,
        *,
        timeout_seconds: int | float | None = None,
        override_data_sources=None,
        network_access: bool | None = None,
    ):
        try:
            fence.check()
            current = _script_fingerprint(home, script_id, policy.allowed_assets)
            if frozen_scripts.get(script_id) != current:
                raise HostAPIError("ASSET_CHANGED", 409)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostAPIError("EXECUTION_TIMEOUT", 504)
            eff_timeout = max(0.05, remaining if timeout_seconds is None else min(float(timeout_seconds), remaining))
            res = original_run(
                script_id,
                inputs=inputs,
                timeout_seconds=eff_timeout,
                override_data_sources=override_data_sources,
                network_access=False,
            )
            fence.check()
            if _script_fingerprint(home, script_id, policy.allowed_assets) != current:
                raise HostAPIError("ASSET_CHANGED", 409)
            return res
        except HostAPIError as exc:
            host_errors.append(exc)
            raise

    engine.runner.run_script = _guarded_run_script
    try:
        if plan.action == "execute.workflow":
            run = engine.run_workflow(
                payload["workflow_id"],
                inputs=payload.get("inputs") or {},
                trigger_kind="plugin",
                owner_id=context.owner_home.owner_id,
            )
        else:
            run = engine.resume_workflow_run(
                payload["workflow_run_id"],
                from_step_id=payload["from_step_id"],
                resolution_origin="system",
            )
    except HostAPIError:
        raise
    except (ValueError, KeyError, OSError, RuntimeError):
        if host_errors:
            raise host_errors[0] from None
        return False, "WORKFLOW_EXECUTION_FAILED"
    if host_errors:
        raise host_errors[0]
    fence.check()
    ok = run.status == "succeeded"
    return ok, "" if ok else "WORKFLOW_EXECUTION_FAILED"
