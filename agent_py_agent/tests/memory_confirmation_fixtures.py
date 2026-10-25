"""离线测试的透明调用辅助器；正式审核、证据核验和提交均由生产实现负责。"""

# LLM: 只转发原晋升与 remember 入口，不补审核、不重建回执、不替生产逻辑推导权限。
# 模块用途: 让测试复用真实调用路径并原样取得生产结果。
from agent_py_agent.agent.capability.memory_tool import RememberTool


# LLM: 仅单次调用原自动晋升并完整转发 confirmed；不因返回原因码补人工审核或再次提交。
# 函数用途: 原样返回真实自动晋升结果，所有来源和确认规则由生产层负责。
def confirm_candidate(service, candidate_id, *, confirmed=True):
    return service.promote(candidate_id, automatic=True, confirmed=confirmed)


# LLM: 测试辅助器仅保存 agent 引用并转发 RememberTool，不解析输出或拼接第二次审核/晋升流程。
# 类用途: 让原有回归复用单次真实 remember 调用及原始回执。
class ConfirmedRememberWorkflow:
    # LLM: 保留调用方原 agent 对象，不复制状态或构造替代验证器。
    # 函数用途: 保存真实工具执行所需的 agent 引用。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 参数与工具回执直接透传，是否成功或需要补证据均由原工具判定。
    # 函数用途: 执行一次真实 remember 并返回原结果。
    def execute(self, params):
        return RememberTool(self.agent).execute(params)
