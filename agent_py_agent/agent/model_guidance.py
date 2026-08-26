from __future__ import annotations

"""Stable host guidance shared by provider system channels and native tools."""

# LLM: 这里是验证表述与动作授权软提示的唯一正文；它们不能参与宿主状态、权限或完成判断。
# 模块用途: 让完整 Prompt 和可能改状态的原生工具共用同一套用户授权边界，避免两处文案逐渐打架。

VERIFICATION_EVIDENCE_GUIDANCE = (
    "每次行动和最终回复前重新核对验证与授权边界。只把目标观察点真实执行到的结果写成已验证："
    "当前主机的进程或端口存在、绑定 `0.0.0.0`、localhost 或本机地址成功，只证明当前主机这一观察点，"
    "不证明另一台机器、真实用户或外部网络能够连接；模拟器、替代环境、局部入口和单一身份同理，"
    "都不能外推到完整端到端。没有来自目标观察点的真实结果时，结论必须明确写“未验证”，"
    "不得写“能用”“可达”“通过”或“应该可用”，只说明已经验证了什么和还差哪个具体复核步骤。"
)

ACTION_AUTHORIZATION_GUIDANCE = (
    "只有用户明确要求相应的修改、创建、启动、停止、安装、部署、派工或其他状态变更时，"
    "才可以采取对应的有副作用动作。若用户只要求查看、核对、确认、检查、诊断、解释、比较、"
    "对比或汇报，必须保持只读；任何工具若无法只读使用就不要调用。不得写文件、改配置、"
    "启动或停止服务、安装或部署、创建后台进程、任务或派发子代理，也不要在发现问题后自行修复。"
)

VERIFICATION_EVIDENCE_BOUNDARY = (
    f"{VERIFICATION_EVIDENCE_GUIDANCE}{ACTION_AUTHORIZATION_GUIDANCE}"
)


# LLM: capability flag 是 system 指令是否真实进入 provider 请求的唯一裁决；旧 fake/第三方后端不能被假装支持。
# 函数用途: 为模型调用和上下文统计返回实际会发送的宿主规则，不支持独立 system 通道时返回空串。
def provider_system_instruction(backend: object) -> str:
    supports_system = bool(getattr(backend, "supports_system_instructions", False))
    supports_options = bool(
        getattr(backend, "supports_provider_request_options", False)
    )
    if not (supports_system and supports_options):
        return ""
    return VERIFICATION_EVIDENCE_BOUNDARY


__all__ = [
    "ACTION_AUTHORIZATION_GUIDANCE",
    "VERIFICATION_EVIDENCE_BOUNDARY",
    "VERIFICATION_EVIDENCE_GUIDANCE",
    "provider_system_instruction",
]
