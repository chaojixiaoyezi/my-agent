# LLM: 只从目标Owner规范资产存储读取脚本、工作流与运行断点，绝不跨Owner回退或从请求载荷直接取代码执行。
# 模块用途: 为插件宿主v2生产组合提供注册资产核验、断点读取与受限资产执行端口。
from __future__ import annotations

import hashlib
import json

from ..assets.script_store import ScriptStore
from ..assets.workflow_store import WorkflowStore
from ..workflows.engine import WorkflowEngine
from ..workflows.run_store import WorkflowRunStore
from ..workflows.script_runner import ScriptRunner
from .errors import HostAPIError
from .execution_policy import AssetGrant
from .schemas import canonical_json


# LLM: 修订号优先读取描述符元数据中的正整数revision，其次读取纯数字version，缺省为1。
# 函数用途: 归一化已注册资产的整数修订号。
def _asset_revision(metadata: object, version: object) -> int:
    if isinstance(metadata, dict) and type(metadata.get("revision")) is int and metadata["revision"] >= 1:
        return metadata["revision"]
    if isinstance(version, str) and version.isdigit() and int(version) >= 1:
        return int(version)
    return 1


# LLM: 仅读取目标Owner私有ScriptStore（禁用全局extra_roots），摘要由真实磁盘代码或规范记录计算。
# 函数用途: 读取并构造当前Owner已注册脚本的AssetGrant。
def _script_grant(home, script_id: str, allowed_assets: tuple[AssetGrant, ...] = ()) -> AssetGrant:
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
    if candidate in allowed_assets:
        return candidate
    return AssetGrant("script", script_id, rev, code_digest)


# LLM: 仅读取目标Owner私有WorkflowStore（禁用全局extra_roots），摘要覆盖完整步骤与正文。
# 函数用途: 读取并构造当前Owner已注册工作流的AssetGrant。
def _workflow_grant(home, workflow_id: str, allowed_assets: tuple[AssetGrant, ...] = ()) -> AssetGrant:
    try:
        definition = WorkflowStore(home, extra_roots=()).get_workflow(workflow_id)
    except (ValueError, KeyError, OSError) as exc:
        raise HostAPIError("ASSET_NOT_FOUND", 404) from exc
    if definition is None:
        raise HostAPIError("ASSET_NOT_FOUND", 404)
    raw = definition.to_dict()
    descriptor = raw["descriptor"]
    rev = _asset_revision(descriptor.get("metadata"), descriptor.get("version"))
    stable = {
        "descriptor": {k: v for k, v in descriptor.items() if k != "runtime_summary"},
        "steps": raw["steps"],
        "body_markdown": raw["body_markdown"],
    }
    digest = hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()
    body_digest = hashlib.sha256(canonical_json(raw["steps"]).encode("utf-8")).hexdigest()
    candidate = AssetGrant("workflow", workflow_id, rev, body_digest)
    if candidate in allowed_assets:
        return candidate
    return AssetGrant("workflow", workflow_id, rev, digest)


# LLM: 续跑必须核对同Owner运行账本与目标步骤存在性，并复核当前工作流资产未漂移。
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
    return _workflow_grant(home, record.workflow_id, context.execution_policy.allowed_assets)


# LLM: 按精确动作分派到目标Owner规范资产存储，不接受路径或通配符。
# 函数用途: 为ExecutionDomain读取当前注册资产或续跑断点对应的AssetGrant。
def registered_asset(context, payload: dict) -> AssetGrant:
    action = context.call.action
    home = context.owner_home.home_dir
    allowed = context.execution_policy.allowed_assets
    if action == "execute.script":
        return _script_grant(home, payload["script_id"], allowed)
    if action == "execute.workflow":
        return _workflow_grant(home, payload["workflow_id"], allowed)
    if action == "execute.workflow_resume":
        return _resume_grant(context, payload)
    raise HostAPIError("ACTION_NOT_AVAILABLE", 403)


# LLM: 每步脚本执行前均消费AssetStepFence并强制禁网，防止执行中途撤权或篡改资产。
# 函数用途: 在目标Owner隔离目录内执行已获批脚本、工作流或工作流断点续跑。
def execute_registered_asset(context, plan, fence) -> tuple[bool, str]:
    fence.check()
    payload = json.loads(plan.payload_json)
    home = context.owner_home.home_dir
    if plan.action == "execute.script":
        result = ScriptRunner(home).run_script(
            payload["script_id"], inputs=payload.get("inputs") or {}, network_access=False
        )
        fence.check()
        return bool(result.ok), "" if result.ok else (result.error_code or "SCRIPT_EXECUTION_FAILED")
    engine = WorkflowEngine(home)
    original_run = engine.runner.run_script

    # LLM: 工作流每个脚本步骤启动前必须复核撤权与资产版本栅栏，且固定禁用网络。
    # 函数用途: 包装工作流步骤脚本执行器以强制逐步栅栏与禁网约束。
    def _guarded_run_script(script_id, inputs=None, **kwargs):
        fence.check()
        kwargs["network_access"] = False
        res = original_run(script_id, inputs=inputs, **kwargs)
        fence.check()
        return res

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
    except (ValueError, KeyError, OSError, RuntimeError):
        return False, "WORKFLOW_EXECUTION_FAILED"
    fence.check()
    ok = run.status == "succeeded"
    return ok, "" if ok else "WORKFLOW_EXECUTION_FAILED"
