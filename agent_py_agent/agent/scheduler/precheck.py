# LLM: 预检经唯一 ToolRegistry/Executor 执行，只接受当前声明为 read_only 的参数；不直接调用 handler、shell 或网络。
# 模块用途: 在模型工作前比较结构化观察摘要；检查失败与无变化分开，动作完成前不推进检查点。
from __future__ import annotations

import hashlib
import json
import time

from .execution_policy import normalize_precheck


# LLM: 调用时已持 scheduler 与后台 claim，停止与心跳均已注册；同 run 已有观察原样复用，不重跑预检。
# 函数用途: 返回是否需要继续模型工作，跳过或失败时沿原 TaskStore/run/wake 收口。
def admit_prechecked_work(agent, service, claim, signal, *, now: float) -> bool:
    spec = normalize_precheck(claim.run.get("precheck"))
    if spec is None:
        return True
    observation = claim.run.get("precheck_observation")
    if observation is None:
        observation = observe(agent, claim, spec)
        observation = service.repository.record_precheck(claim.run_id, claim.claim_id, observation)
    if observation["status"] == "changed":
        return True
    failed = observation["status"] == "failed"
    service.conversation_store.tasks.update_status({
        "task_id": claim.run_id, "status": "abandoned" if failed else "completed", "expected_status": "active", "now": now,
    })
    terminal = service.finish(claim, status="failed" if failed else "skipped", delivery_status="suppressed",
        delivery_reason="precheck_failed" if failed else "precheck_no_change",
        error_code="SCHEDULER_PRECHECK_FAILED" if failed else "", now=now)
    if terminal is not None:
        service.conversation_store.wakes.mark_handled(signal.wake_signal_id, now=now)
        from ..conversation.task_run_closeout import settle_terminal_task_run

        settle_terminal_task_run(getattr(getattr(agent, "subagents", None), "runtime_db", None),
                                 service.conversation_store, task_id=claim.run_id)
    return False


# LLM: 只保存摘要、工具编号和结构化失败码；原参数与工具输出仍由既有工具操作账保存，不进入元数据视图。
# 函数用途: 执行一次只读预检，比较原 job 的已确认 checkpoint，错误不伪装成无变化。
def observe(agent, claim, spec: dict) -> dict:
    try:
        output, call_id = _execute_read_only(agent, claim, spec)
        value = _json_pointer(json.loads(output), spec["json_pointer"]) if "json_pointer" in spec else output
        raw = json.dumps({"precheck": spec, "value": value}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        previous = claim.run.get("precheck_checkpoint") or {}
        status = "no_change" if previous.get("fingerprint") == fingerprint else "changed"
        return {"status": status, "fingerprint": fingerprint, "call_id": call_id, "observed_at": time.time()}
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - 预检失败沿原 run 留结构化原因，不等于无变化
        return {"status": "failed", "error_code": "SCHEDULER_PRECHECK_FAILED", "call_id": f"host-tool-{claim.run_id}",
                "error_type": type(exc).__name__, "observed_at": time.time()}


# LLM: schema、effect 与执行共用同一快照；原后台身份回调把真实 attempt 发布到原 claim，未知或写工具在 handler 前拒绝。
# 函数用途: 以原工具执行器运行显式读取，关联同一 run/claim 和可精确停止的 attempt，不创建输入消费轮。
def _execute_read_only(agent, claim, spec: dict) -> tuple[str, str]:
    from ..agent_core.runtime.loop_models import RunParams
    from ..agent_core.tool_only_execution import execute_read_only_tool
    from ..conversation.background_execution import _BackgroundRunControl

    thread = agent.conversation_store.threads.require(str(claim.run["thread_id"]))
    params = RunParams(request_id=claim.run_id, run_id=claim.run_id, task_id=claim.run_id,
        source="scheduler_precheck", root_user_prompt=str(claim.run["prompt"]),
        conversation_task_binding_callback=_BackgroundRunControl(claim.run_id, None),
        task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": claim.run_id},
        write_boundary={"task_id": claim.run_id, "execution_cwd": thread.cwd,
                        "execution_workspace_roots": list(thread.runtime_workspace_roots)})
    execution, output = execute_read_only_tool(agent, params, tool_name=spec["tool_name"], arguments=spec["arguments"])
    if not execution.result.ok:
        raise RuntimeError(execution.result.error_code or "PRECHECK_TOOL_FAILED")
    return output, execution.call.call_id


# LLM: JSON 指针只选显式路径，不用文本匹配猜变化；缺字段、非法索引或非 JSON 都是检查失败。
# 函数用途: 从预检结果中提取要比较的结构化值。
def _json_pointer(value, pointer: str):
    if not pointer:
        return value
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if "~" in token.replace("~1", "").replace("~0", ""):
            raise ValueError("JSON 指针转义无效")
        if isinstance(value, list):
            if not key.isdecimal() or (len(key) > 1 and key.startswith("0")):
                raise ValueError("JSON 指针数组索引无效")
            value = value[int(key)]
        else:
            value = value[key]
    return value
