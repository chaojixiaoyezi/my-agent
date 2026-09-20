# LLM: 这是 runner-retry 场景的固定后端，只供离线场景注入，不注册为用户模型。
# 模块用途: 首次调用抛出临时错误，下一次返回固定回复，供调度恢复场景使用。
from __future__ import annotations

import json

from ...agent.backends import ModelResponse


# LLM: 仅用于离线场景，调用计数属于当前实例；不得作为真实 provider 注册。
# 类用途: 让第一轮任务失败、下一轮返回固定回复，供重试诊断使用。
class ScenarioRetryBackend:

    name = "scenario_retry_backend"

    # LLM: 每个场景实例单独记调用次数，不能跨实验复用计数。
    # 函数用途: 初始化固定后端的模拟调用状态。
    def __init__(self) -> None:
        self.calls = 0

    # LLM: 这里只模拟后端响应；特殊诊断回复不计任务调用，保持旧场景行为且不产生网络请求。
    # 函数用途: 为失败诊断返回固定说明，并让任务调用依次失败和成功。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if "输出严格 JSON 格式" in prompt:
            return ModelResponse(
                text=json.dumps(
                    {
                        "analysis_reason": "场景测试模拟临时 runner 失败，允许重试。",
                        "root_cause": "transient_runner_failure",
                        "suggested_params": {},
                        "should_retry": True,
                        "should_split": False,
                        "confidence": 0.9,
                    },
                    ensure_ascii=False,
                ),
                backend=self.name,
            )
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        return ModelResponse(text=_runner_retry_success_text(), backend=self.name)


# LLM: 固定回复仍保留旧场景数据，正式 runner 不把其中的状态和验收声明作为事实。
# 函数用途: 构造第二次模拟任务调用的固定正文，现有验收断言债务另行处理。
def _runner_retry_success_text() -> str:
    text = (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "DONE",\n'
        '  "summary": "runner 在第二次尝试中完成，已生成可验收证据。",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "第二次 runner 尝试成功", "ok": true}],\n'
        '  "evidence_packets": [_RETRY_PACKET_],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [_RETRY_TEST_],\n'
        '  "patches": [],\n'
        '  "lessons": ["临时 runner 错误可以由父代理有限重试恢复"],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )
    text = text.replace(
        "_RETRY_PACKET_",
        _packet("evpkt-runner-retry", "第二次 runner 尝试成功并可验收", "runner retry scenario"),
    )
    return text.replace("_RETRY_TEST_", _file_check_test("runner retry", "第二次尝试通过"))


# LLM: 本函数只序列化离线样例，不登记或验证生产证据。
# 函数用途: 生成固定回复中的示例证据字段。
def _packet(packet_id: str, claim: str, checked_scope: str) -> str:
    return json.dumps({
        "id": packet_id,
        "claim": claim,
        "checked_scope": checked_scope,
        "evidence_refs": ["runner_result.json"],
        "artifact_refs": ["output.json"],
        "confidence": 0.9,
    }, ensure_ascii=False)


# LLM: 返回的声明只属于旧场景正文，不执行文件检查，也不能证明任务质量。
# 函数用途: 保留重试场景的固定样例数据，避免清理另一场景时改变它的行为。
def _file_check_test(name: str, summary: str) -> str:
    return json.dumps({
        "name": name,
        "validation_method": "file_check",
        "file_path": "state.json",
        "ok": True,
        "summary": summary,
    }, ensure_ascii=False)
