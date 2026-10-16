# LLM: 自我目录指引只沿实际冻结工具面，不查目录/权限正文，不从开关猜已有工具；普通与Compact共用。
# 模块用途: 以同一纯判据决定本次模型输入是否可提示self_map入口。


# LLM: 交集只收紧当前快照；模型不可见、不可用或未授权时均不提示不存在能力，不改变工具加载。
# 函数用途: 检查当前运行能否使用自我目录。
def self_map_available(snapshot, allowed_tools=None):
    if snapshot is None or (allowed_tools is not None and "self_map" not in allowed_tools):
        return False
    runtime = snapshot.runtime("self_map")
    return bool(runtime is not None and runtime.availability.available and runtime.exposure.model_visible
                and "self_map" in snapshot.available_tool_names)
