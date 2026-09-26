# LLM: 显式连接测试复用原决策后端、worker、准入和账本；不读取任务正文、不应用建议或修改任何模型/开关。
# 模块用途: 在原模型菜单里测试已保存的决策连接，即使总开关关闭也可测试，错误回执不带上游秘密。
from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

from ..backends.decision_protocol import DecisionBinding, DecisionRequest
from ..backends.typesafe_decision import decision_backend_from_profile
from ..common.cancellation import ToolCancelled
from ..conversation.auxiliary_model_call import settle_standalone_model_usage
from ..conversation.decision_model_call import DecisionModelDisallowed, invoke_decision_model_call
from ..conversation.decision_policy import connection_revision, cooldown_state, decision_owner_ref
from .decision_settings import execute_decision_settings_operation
from .decision_settings_projection import decision_profile
from .decision_settings_schema import positive_seconds, profile_reference
from .model_profiles import model_profiles_path, read_model_profiles


# LLM: 秒数为显式用户输入，整次网络共用准备前 deadline；当前线程由认证宿主传入，payload 不可指定 owner/run/thread。
#   管理员禁用本 owner 的 Jev 时由调用边界硬门拒绝（不联网），这里给出明确说明而不是通用失败。
# 函数用途: 发一次无聊天材料的原生选择题；成功清除此连接冷却，返回诊断/用量并沿原独立调用入口结算。
def probe_decision_model(agent: object, payload: dict, *, thread_id: str = "") -> dict:
    started = time.monotonic()
    deadline = started + positive_seconds(payload.get("timeout_seconds", 4.0))
    profile_id = profile_reference(payload.get("profile_id", ""))
    request_id = "decision-probe:" + uuid4().hex
    params = SimpleNamespace(request_id=request_id, run_id="", task_id="", thread_id=thread_id,
        task_attributes={"conversation_thread_id": thread_id}, live_archive_state={})
    result = {"ok": False, "profile_id": profile_id, "model_name": "", "elapsed_seconds": 0.0,
              "usage": {"input_tokens": None}, "error_type": "", "message": ""}
    authorized = False
    try:
        # 只做原身份/设置读取，不沿普通模型选择初始化；锁忙立即失败，也不要求已启用决策。
        execute_decision_settings_operation(agent, "read", {}, thread_id=thread_id, blocking=False)
        authorized = True
        config = decision_profile(agent, read_model_profiles(model_profiles_path(agent.home_paths)), profile_id)
        result["model_name"] = config["model_name"]
        owner, revision = decision_owner_ref(agent), connection_revision(config)
        backend = decision_backend_from_profile(config)
        request = DecisionRequest(DecisionBinding("connection_probe", owner, request_id, revision, "probe.v1", thread_id=thread_id),
            {"purpose": "explicit_connection_test", "value": "ready"},
            {"connection": {"type": "choice", "instructions": "选择与 state.value 一致的选项。", "criteria": {
                "ready": "值为 ready", "unknown": "无法判断"}}})
        response = invoke_decision_model_call(agent, params, request, backend, deadline=deadline,
            resource_key=("decision_probe", owner, thread_id, profile_id, revision))
        answer = response.answers[0]
        ok = not answer.error_code and answer.kind == "choice" and answer.value == "ready"
        if ok:
            cooldown_state((owner, profile_id, revision), revision, retry=True)
        result.update(ok=ok, usage={"input_tokens": None, **response.usage},
            error_type="" if ok else "DecisionProbeAnswerInvalid",
            message="原生接口测试通过；仅验证本次连接与协议，不代表任务判断质量。" if ok else "请求已返回，但测试题未得到有效答案。")
    except (InterruptedError, ToolCancelled):
        raise
    except DecisionModelDisallowed as exc:
        result.update(error_type=type(exc).__name__, message="管理员已关闭当前用户的决策模型使用权，未发起连接。原设置未修改。")
    except Exception as exc:
        result.update(error_type=type(exc).__name__, message="决策连接测试失败；请检查可用配置、服务状态和等待时间。原设置未修改。")
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if authorized and thread_id:
            store = agent.conversation_store
            settle_standalone_model_usage(SimpleNamespace(agent=agent, store=store,
                thread=SimpleNamespace(thread_id=thread_id), request_id=request_id, run_id="", task_id=""),
                source="decision_probe", usage_only=True)
    return result
