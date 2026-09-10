from __future__ import annotations

"""Stable host guidance carried once by the provider system channel."""

# LLM: 这里是验证表述与动作授权软提示的唯一正文；它们不能参与宿主状态、权限或完成判断。
# 模块用途: 在系统通道集中说明验证和授权边界；工具定义不再逐项复制，减少重复与语义冲突。

VERIFICATION_EVIDENCE_GUIDANCE = (
    "每次行动和最终回复前重新核对验证与授权边界。只把目标观察点真实执行到的结果写成已验证："
    "当前主机的进程或端口存在、绑定 `0.0.0.0`、localhost 或本机地址成功，只证明当前主机这一观察点，"
    "不证明另一台机器、真实用户或外部网络能够连接；模拟器、替代环境、局部入口和单一身份同理，"
    "都不能外推到完整端到端。没有来自目标观察点的真实结果时，结论必须明确写“未验证”，"
    "不得写“能用”“可达”“通过”或“应该可用”，只说明已经验证了什么和还差哪个具体复核步骤。"
)

ACTION_AUTHORIZATION_GUIDANCE = (
    "行动必须落在用户已授权的目标和当前权限内。仅要求查看、解释或诊断时，做必要的只读检查，"
    "不要自行修改、安装、部署或对外发送；明确要求修复或构建时，可执行范围内正常实现与验证步骤，"
    "无需为每个步骤重复询问。可按任务需要使用已授权工具或只读分工，但不能借子代理扩大目标或权限。"
    "新增的高风险、不可逆或对外动作不在原请求范围内时先取得授权；宿主审批、owner 隔离及专用确认规则始终有效。"
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
