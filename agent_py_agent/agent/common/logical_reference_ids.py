# LLM: 产品编号前缀的唯一共享常量：生成点与工具层守卫都从这里取，禁止在别处另写一份字面量。
#   这些编号是逻辑身份（Gateway 请求号、run 号、子代理号），永远不是文件系统路径段；
#   工具调用编号由模型供应商返回（call_xx / call_function_xx），产品只透传。
# 模块用途: 提供逻辑引用编号的前缀常量与"编号 + 冒号 + 工具调用编号"路径段判定，供守卫与生成点共用。

from __future__ import annotations

# 生成点：gateway_parts/io.new_gateway_request_id 拼 "gwreq-<时间戳>-<hex>"。
GATEWAY_REQUEST_ID_PREFIX = "gwreq-"
# 生成点：agent_core/runtime/run_params.py 与 agent_core/_finalization_service.py 拼 "run-<纳秒>"。
RUN_REQUEST_ID_PREFIX = "run-"
# 生成点：subagents / agent_core/runner 的 run 身份前缀（"subagent-…"，含 "subagent-run:" 等复合形态）。
SUBAGENT_ID_PREFIX = "subagent-"
# 工具调用编号前缀来自模型供应商的工具调用 id，产品透传不生成。
TOOL_CALL_ID_PREFIX = "call_"

# 逻辑引用编号的已知前缀集合；守卫只按这些前缀做结构化形态判定。
LOGICAL_ID_PREFIXES = (
    GATEWAY_REQUEST_ID_PREFIX,
    RUN_REQUEST_ID_PREFIX,
    SUBAGENT_ID_PREFIX,
)


# LLM: 判定只看结构化形态：路径段以产品编号前缀开头，且包含"冒号 + 工具调用编号前缀"——
#   这正是归档里 scoped_call_id（"<scope>:<call_id>"）的形态，普通文件名/相对路径不命中。
#   绝对路径不由本函数处理；调用方（写工具守卫）只对相对路径调用。
# 函数用途: 判断一个路径段是不是"逻辑引用编号"而不是真实文件/目录名。
def is_logical_reference_segment(segment: object) -> bool:
    text = str(segment or "").strip()
    if not text or f":{TOOL_CALL_ID_PREFIX}" not in text:
        return False
    return text.startswith(LOGICAL_ID_PREFIXES)
