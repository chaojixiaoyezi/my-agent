"""离线测试显式用户审核流程；生产remember本身仍只创建候选。"""
from types import SimpleNamespace

from agent_py_agent.agent.capability.memory_tool import (
    RememberTool,
    _promotion_result_with_scope,
    _remember_write_result,
)
from agent_py_agent.cli.memory_admin_commands import (
    _candidate_promote_payload,
    _candidate_review_payload,
)


# LLM: 先观察真实自动拒绝，再模拟用户调用原CLI审核及正式promote确认；不改候选文件或替换任何生产验证器。
# 函数用途: 测试正式提交/CAS时走明确的人工确认步骤，来源不合格仍返回原自动拒绝。
def confirm_candidate(service, candidate_id, *, confirmed=True):
    waiting = service.promote(candidate_id, automatic=True)
    if waiting.reason_code != 'AUTO_PROMOTION_CONFIRMATION_REQUIRED':
        return waiting
    host = SimpleNamespace(memory_promotion=service, memory_candidates=service.candidates)
    _candidate_review_payload(host, SimpleNamespace(candidate_id=candidate_id, reviewer='explicit-test-user',
        decision='approve', note='用户已检查该候选的精确内容、范围和目标版本'))
    return service.promote(candidate_id, reviewer='explicit-test-user', automatic=False, confirmed=confirmed)


# LLM: 这是测试中的多步用户工作流，不是模型工具或生产批准器；每次先调用原Remember生成候选，再独立审核精确候选。
# 类用途: 供删除崩溃/CAS回归复用真实remember→CLI审核→原promote确认链，避免把旧自动发布当产品行为。
class ConfirmedRememberWorkflow:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, params):
        original = RememberTool(self.agent).execute(params)
        if not original.ok or str(params.get('action') or '') == 'list':
            return original
        import json
        payload = json.loads(original.output)
        results = payload.get('results', [])
        if not results:
            return original
        projected = []
        for row in results:
            if row.get('reason_code') != 'AUTO_PROMOTION_CONFIRMATION_REQUIRED':
                projected.append(row)
                continue
            candidate_id = row['candidate_id']
            candidate = self.agent.memory_candidates.get(candidate_id)
            result = confirm_candidate(self.agent.memory_promotion, candidate_id)
            projected.append(_promotion_result_with_scope(result.to_dict(), candidate))
        return _remember_write_result(payload.get('action', 'add'), projected)
