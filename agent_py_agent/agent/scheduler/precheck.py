# LLM: 预检经原后台参数、ToolRegistry/Executor 执行，只接受 read_only；cwd、Skill 和身份沿既有后台装配，不从 prompt 或工具参数推断。
# 模块用途: 在模型工作前比较完整观察；首次认证拒绝交原wake通知，普通失败与无变化保持原收口，不推进检查点。
from __future__ import annotations

import hashlib
import json
import time

from .execution_policy import normalize_precheck


# LLM: 只携带原ToolResult的错误码、调用编号及首次typed认证义务，不保留输出正文或从异常文案解析原因。
# 类用途: 将工具失败原因交给预检状态记录，便于纯元数据诊断。
class PrecheckToolFailure(RuntimeError):
    # LLM: 参数必须来自共享执行器的回执，不能传入用户提示词；异常仅作为宿主结构化失败载体。
    # 函数用途: 保存原错误码和真实工具调用编号，不复制工具正文。
    def __init__(self, error_code: str, call_id: str, result=None):
        super().__init__("只读预检工具执行失败")
        self.tool_error_code = error_code
        self.call_id = call_id
        from ..conversation.required_user_action import required_action_from_result
        self.required_user_action = required_action_from_result(result)


# LLM: 持原scheduler/BG claim；typed拒绝后通知热轮覆盖冻结和外发，已有观察不重跑预检，普通无义务不新建执行轮。
# 函数用途: 决定继续工作、原失败收口或仅表达认证通知；通知不重做工具也不推进检查点。
def admit_prechecked_work(agent, service, claim, signal, *, run_kwargs: dict, now: float, notify=None):
    spec = normalize_precheck(claim.run.get("precheck"))
    if spec is None:
        return True
    observation = claim.run.get("precheck_observation")
    if observation is None:
        observation = observe(agent, claim, spec, run_kwargs=run_kwargs)
        observation = service.repository.record_precheck(claim.run_id, claim.claim_id, observation)
    if observation["status"] == "changed":
        return True
    failed = observation["status"] == "failed"
    if failed and observation.get('required_user_action') is not None and notify is not None:
        from ..conversation.host_notification_scope import notification_scope
        from ..conversation.required_user_action import freeze_request_notice
        from ..conversation.runtime import _run_request
        request = _run_request(run_kwargs)
        with notification_scope(agent, request):
            if freeze_request_notice(agent, request, [observation['required_user_action']]):
                return notify(run_kwargs)
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


# LLM: 只保存摘要、工具编号及原工具错误码；原参数与输出仍归既有工具账，不进入元数据视图。
# 函数用途: 沿同一后台请求执行预检并比较 checkpoint，错误不伪装成无变化。
def observe(agent, claim, spec: dict, *, run_kwargs: dict) -> dict:
    try:
        output, call_id = _execute_read_only(agent, claim, spec, run_kwargs=run_kwargs)
        value = _json_pointer(json.loads(output), spec["json_pointer"]) if "json_pointer" in spec else output
        raw = json.dumps({"precheck": spec, "value": value}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        previous = claim.run.get("precheck_checkpoint") or {}
        status = "no_change" if previous.get("fingerprint") == fingerprint else "changed"
        return {"status": status, "fingerprint": fingerprint, "call_id": call_id, "observed_at": time.time()}
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - 预检失败沿原 run 留结构化原因，不等于无变化
        result = {"status": "failed", "error_code": "SCHEDULER_PRECHECK_FAILED", "call_id": f"host-tool-{claim.run_id}",
                  "error_type": type(exc).__name__, "observed_at": time.time()}
        if isinstance(exc, PrecheckToolFailure):
            result.update(tool_error_code=exc.tool_error_code, call_id=exc.call_id)
            if exc.required_user_action is not None:
                result['required_user_action'] = exc.required_user_action
        return result


# LLM: 原后台请求唯一装配 cwd、授权根、Skill 和任务属性；schema/effect 与执行同快照，真实 attempt 发布到原 claim。
# 函数用途: 沿后台实际工作目录执行只读工具并保留原因码，不创建输入消费轮或猜测相对路径。
def _execute_read_only(agent, claim, spec: dict, *, run_kwargs: dict) -> tuple[str, str]:
    from ..agent_core.runtime.loop_models import RunParams
    from ..agent_core.tool_only_execution import execute_read_only_tool
    from ..conversation.background_execution import _BackgroundRunControl
    from ..conversation.runtime import _background_task_attributes, _run_request

    thread = agent.conversation_store.threads.require(str(claim.run["thread_id"]))
    attributes = _background_task_attributes(thread.thread_id, _run_request(run_kwargs), agent, thread=thread)
    params = RunParams(request_id=claim.run_id, run_id=claim.run_id, task_id=claim.run_id,
        source="scheduler_precheck", root_user_prompt=str(claim.run["prompt"]),
        conversation_task_binding_callback=_BackgroundRunControl(claim.run_id, None),
        task_attributes=attributes, write_boundary={"task_id": claim.run_id})
    execution, output = execute_read_only_tool(agent, params, tool_name=spec["tool_name"], arguments=spec["arguments"])
    if not execution.result.ok:
        raise PrecheckToolFailure(execution.result.error_code or "PRECHECK_TOOL_FAILED", execution.call.call_id, execution.result)
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
