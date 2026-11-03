"""显式产物合同子代理的跨工具只读空转观察，不根据任务正文猜测写入要求。"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

from ..capability.runtime_config_reload import capability_config_for_agent
from ..runtime_context import current_task_attributes
from ..subagents.context_bundle_contracts import declared_output_refs, required_file_contract
from ..subagents.context_bundle_refs import execution_cwd
from ..tooling.write_boundary import WRITE_TOOL_NAMES
from .artifact_locator import artifact_can_be_located
from .tool_guard.call_guardrail import _resolved_effect


# LLM: 只有显式产物义务启用守卫，不从可写权限或自然语言推断合同。
# 函数用途: 检查结构化文件合同和交付物定位合同。
def has_explicit_product_contract(params: object, *, task_attributes: dict | None = None) -> bool:
    """复用 runner 的显式文件合同；可写目录和工具权限本身不是产出义务。"""
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else task_attributes
    # 不把权限根投影成文件要求，也排除旧版 system_default_output_ref 内部报告槽。
    task = SimpleNamespace(attributes=attrs or {}, allowed_write_roots=[])
    if required_file_contract(task) or declared_output_refs(task):
        return True
    contract = getattr(params, "delivery_contract", None)
    artifacts = contract.get("artifacts") if isinstance(contract, dict) else None
    return isinstance(artifacts, list) and any(
        isinstance(item, dict) and artifact_can_be_located(item) for item in artifacts
    )


# LLM: 投影路径只是版本观察候选，不能把工具正文自报产物当成真实进展。
# 函数用途: 在正式记录入口累计只读调用，并按真实写尝试或产物版本变化重置。
def record_readonly_stall(agent: object, record: object, *, stage_progress: dict | None = None) -> None:
    """在正式归档入口消费原 action effect、阶段产物投影和 capability 配置。"""
    params = record.params
    if getattr(params, "context_scope", "") != "task_local":
        return
    if not has_explicit_product_contract(params, task_attributes=current_task_attributes(agent)):
        return
    config = capability_config_for_agent(agent)
    if config is None:
        return
    hint_calls = config.subagent_readonly_stall_hint_calls
    halt_calls = config.subagent_readonly_stall_halt_calls
    if hint_calls == 0 and halt_calls == 0:
        return
    # 沿现有本轮容器保存，replace/Compact 不丢计数；不创建第二份持久任务账。
    state_root = params.live_archive_state
    identity = (params.run_id, record.call.attempt_id)
    state = state_root.get("subagent_readonly_stall")
    if not isinstance(state, dict) or state.get("identity") != identity:
        state = {"identity": identity, "readonly_calls": 0, "hinted": False, "halt_notified": False}
        attrs = getattr(params, "task_attributes", None)
        attrs = attrs if isinstance(attrs, dict) else current_task_attributes(agent)
        task = SimpleNamespace(attributes=attrs or {}, allowed_write_roots=[],
                               effective_permissions={"owner_home": str(agent.root)})
        state["execution_cwd"] = execution_cwd(task)
        state["path_versions"] = _initial_path_versions(params, task, state["execution_cwd"])
        state_root["subagent_readonly_stall"] = state
    effect = _resolved_effect(params, record.call, record.result)
    stage_updated = _stage_artifact_updated(record, stage_progress, state)
    # 写尝试即清零，包括授权拒绝；未知/非只读动作也打断“连续全部只读”。
    if record.call.tool_name in WRITE_TOOL_NAMES or effect != "read_only" or stage_updated:
        state.update(readonly_calls=0, hinted=False, halt_notified=False)
        _clear_stall_halt(params)
        return
    count = state["readonly_calls"] + 1
    state["readonly_calls"] = count
    if hint_calls > 0 and count >= hint_calls and not state["hinted"]:
        state["hinted"] = True
        params.tool_context.append("[subagent-readonly-stall-hint]\n" + json.dumps({
            "schema_version": "subagent_readonly_stall.v1",
            "code": "SUBAGENT_READONLY_STALL", "run_id": params.run_id,
            "readonly_calls": count, "hint_calls": hint_calls, "halt_calls": halt_calls,
            "next_action": "produce_stage_artifact_or_report_blocker",
            "message": "本任务有显式产物合同，但连续只读且没有写尝试或阶段产物更新；请产出阶段文件或向直属父级说明真实阻塞。",
        }, ensure_ascii=False, sort_keys=True))
    if halt_calls > 0 and count >= halt_calls and params.repeated_failure_halt is None:
        object.__setattr__(params, "repeated_failure_halt", (record.call.tool_name, "code:SUBAGENT_READONLY_STALL", count))
        object.__setattr__(params, "repeated_failure_halt_exhausted", True)
        if not state["halt_notified"]:
            state["halt_notified"] = True
            params.tool_context.append("[subagent-readonly-stall-halt]\n" + json.dumps({
                "schema_version": "subagent_readonly_stall.v1", "code": "SUBAGENT_READONLY_STALL",
                "run_id": params.run_id, "readonly_calls": count, "next_action": "handoff_to_parent",
                "message": "本轮停止新的工具调用；仅依据已有证据说明已做、未做和阻塞，按未完成状态交直属父级接管。",
            }, ensure_ascii=False, sort_keys=True))


# LLM: 仅识别本守卫的结构化错误码，不清理其它停止原因。
# 函数用途: 判断当前熔断是否来自只读空转守卫。
def is_readonly_stall_halt(params: object) -> bool:
    halt = getattr(params, "repeated_failure_halt", None)
    return isinstance(halt, tuple) and len(halt) == 3 and halt[1] == "code:SUBAGENT_READONLY_STALL"


# LLM: 恢复进展只能撤销本守卫拥有的 halt。
# 函数用途: 清除只读空转熔断及对应 exhausted 标记。
def _clear_stall_halt(params: object) -> None:
    if is_readonly_stall_halt(params):
        object.__setattr__(params, "repeated_failure_halt", None)
        object.__setattr__(params, "repeated_failure_halt_exhausted", False)


# LLM: 只 stat 有界的显式合同路径，不定位搜索根、不遍历、不读取或 hash 文件内容。
# 函数用途: 在首次观察时保存合同文件基线，包括尚不存在的目标。
def _initial_path_versions(params: object, task: object, cwd: str, *, max_paths: int = 128) -> dict:
    refs = [*required_file_contract(task), *declared_output_refs(task)]
    contract = getattr(params, "delivery_contract", None)
    artifacts = contract.get("artifacts") if isinstance(contract, dict) else None
    if isinstance(artifacts, list):
        refs.extend(item.get("preferred_path") or item.get("path") for item in artifacts[:max_paths]
                    if isinstance(item, dict))
    versions: dict = {}
    for raw in refs[:max_paths]:
        path = _stage_path(raw, cwd)
        if path is not None:
            try:
                versions[str(path)] = _file_version(path)
            except OSError:
                # 无法观察不是“文件不存在”，不能制造下一次成功 stat 的假更新。
                continue
    return versions


# LLM: 相对引用与工具执行共享 cwd；URI、通配符不是可直接 stat 的文件目标。
# 函数用途: 将明确文件引用规范成单一路径，不扫描或扩展候选。
def _stage_path(raw: object, cwd: str) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "://" in raw or any(c in raw for c in "*?[]\x00"):
        return None
    try:
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            if not cwd:
                return None
            path = Path(cwd) / path
        return path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None


# LLM: 元数据版本证明观察窗口内的文件变化，不证明内容质量或最终验收。
# 函数用途: 读取普通文件 stat 指纹；缺失或非普通文件不构成阶段产物。
def _file_version(path: Path) -> tuple | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: 首次报告普通既有文件仅建立基线；同一版本重复报告、宿主自写进度投影永远不清零。
# 函数用途: 核验两个阶段候选的真实版本变化，并在有界观察表中消费该版本。
def _stage_artifact_updated(record: object, progress: dict | None, state: dict, *, max_paths: int = 128) -> bool:
    if not isinstance(progress, dict) or progress.get("schema_version") != "subagent_tool_progress.v1":
        return False
    result = record.result
    operation = result.operation
    fresh = result.ok and result.handler_executed and (
        operation is None or (operation.handler_executed and not operation.replayed and operation.status == "succeeded")
    )
    versions = state["path_versions"]
    updated = False
    # 这两个引用由宿主投影器生成；其目录中的监控文件不是业务产物。
    projection_roots = [ref.parent for key in ("latest_tool_progress_ref", "tool_progress_ledger_ref")
                        if (ref := _stage_path(progress.get(key), state["execution_cwd"])) is not None]
    for key in ("latest_written_path", "closeout_written_path"):
        path = _stage_path(progress.get(key), state["execution_cwd"])
        if path is None or any(path.is_relative_to(root) for root in projection_roots):
            continue
        ref = str(path)
        if ref not in versions and len(versions) >= max_paths:
            continue
        try:
            version = _file_version(path)
        except OSError:
            continue
        if ref in versions and version is not None and version != versions[ref]:
            updated = True
        # 即使失败或回放也消费已观察版本，避免下一次成功读取冒充这次写入。
        versions[ref] = version
    return bool(fresh and updated)
