from __future__ import annotations

"""Stable host guidance carried once by the provider system channel."""

# LLM: provider补充只保留观察点与授权反例；核心执行规则在settings，保留独立system覆盖时的安全提示，不参与控制裁决。
# 模块用途: 在支持的供应商系统通道补足本机/远端、模拟/真实的证据差异，避免复述完整核心提示。

VERIFICATION_EVIDENCE_GUIDANCE = (
    "证据观察点：版本、输入和观察点未变时复用已有有效结果；定位后针对实际改动验证。"
    "当前主机进程、端口、0.0.0.0绑定或localhost成功不证明外部网络、另一台机器或真实用户可达；"
    "模拟器、替代环境、局部入口和单一身份也不能外推完整端到端。未取得目标观察点结果时明确写“未验证”，"
    "说明已验入口与缺少的复核，不写“应该可用”。编译不证明启动或业务链路；须针对当前版本而非旧进程或旧产物。"
    "观察到的失败与原因假设分开；没有明确权限拒绝或隔离证据，不把超时、空日志、无响应归因于沙箱。"
)

ACTION_AUTHORIZATION_GUIDANCE = (
    "授权反例：仅要求查看、解释或诊断时，做必要的只读检查，不自行修改、安装、部署或发送；"
    "明确修复/构建后不为正常步骤重复索要授权。"
    "只读分工须向子代理保留同样的检查范围，不能把查找问题改写成发现后直接修复；"
    "角色、工具可用性和 Full Access 都不代表新增业务目标的授权。"
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
