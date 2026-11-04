"""单步研判的隔离模式选择，不建立会话、不回忆记忆或挂载工具。"""


# LLM: 选择只读结构化参数；没有最小子代理入口时必须记录明确降级。
# 函数用途: 返回本次研判使用的隔离模式事实。
def resolve_analysis_isolation(run_params, *, step_config=None):
    config = step_config if isinstance(step_config, dict) else {}
    value = (run_params.get('analysis_isolation') if isinstance(run_params, dict)
             else getattr(run_params, 'analysis_isolation', None))
    value = value if value is not None else config.get('analysis_isolation')
    requested = value if value in ('stateless_skill', 'lightweight_subagent') else 'stateless_skill'
    # 基线只有完整SubAgentManager.create_run；profile字段均是模型/材料投影，不是最小执行入口。
    return {'requested': requested, 'mode': 'stateless_skill',
            'note': 'minimal_profile_entry_unavailable' if requested == 'lightweight_subagent' else ''}
