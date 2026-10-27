# LLM: 公共API按需取得原类型；SDK预载不顺带导入引擎/技能服务，签名冻结仍由runtime_sdk立即执行。
# 模块用途: 保持公开类导入兼容，避免短超时脚本在无关服务导入中耗尽预算。
from importlib import import_module

__all__ = ['ScriptExecutionResult', 'StepExecutionRecord', 'WorkflowRunRecord',
           'ScriptRunner', 'WorkflowEngine', 'WorkflowRunStore']


# LLM: 仅解析固定公开名并缓存原类身份；未知属性抛标准AttributeError，不替换SDK验证或执行器。
# 函数用途: 第一次使用公开类时才导入对应模块。
def __getattr__(name):
    modules = {'WorkflowEngine': 'engine', 'ScriptRunner': 'script_runner', 'WorkflowRunStore': 'run_store',
               'ScriptExecutionResult': 'models', 'StepExecutionRecord': 'models', 'WorkflowRunRecord': 'models'}
    if name not in modules:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module('.' + modules[name], __name__), name)
    globals()[name] = value
    return value
