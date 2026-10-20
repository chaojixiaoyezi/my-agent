# LLM: Gateway 拓扑只有主配置中的一个值；校验不得把非法输入归零或从请求、P4、Namespace推导部署配置。
# 模块用途: 严格校验宿主配置的工作进程数量，供配置加载与公开启动共用。
from __future__ import annotations


# LLM: 纯类型/范围检查，不读取环境或状态、不创建进程；bool不是合法整数配置，零明确保留单进程语义。
# 函数用途: 返回合法工作进程数，非法配置立即报中文错误，避免悄悄启用另一种拓扑。
def validate_gateway_worker_process_count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("gateway_worker_process_count 必须是非负整数，不能是布尔值；0 表示单进程。")
    return value
