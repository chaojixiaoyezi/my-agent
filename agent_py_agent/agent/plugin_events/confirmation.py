# LLM: B1 确认事实及中文投影只读已验证清单；不解析 owner、不执行插件、不声称沙箱已施加。
#   修改须联测 plugin_runtime_facts、PluginEnableTool 原确认链和 test_plugin_manifest_v8。
# 模块用途: 让启用确认码覆盖订阅、提示正文与网络需求；工具参数只在 full 收紧门预览中展示。
from __future__ import annotations


# LLM: 旧包返回空扩展保持旧确认字节；四项事实不含动态宿主状态。
# 函数用途: 加入订阅权限事实，required 表示强制沙箱需求，不是运行证明。
def event_confirmation_facts(manifest) -> dict:
    if manifest.permissions is None:
        return {}
    return {"events": [event.to_payload() for event in manifest.events],
            "tool_gates": [gate.to_payload() for gate in manifest.tool_gates],
            "network": manifest.permissions.network, "sandbox": "required",
            "read_scope": "插件只能读它自己的目录、解释器所在目录和系统目录，读不到你家目录里的其它文件"}


# LLM: 仅展示结构化事实，不参与授权；旧包文案保持，强制要求不能冒充运行证据。
# 函数用途: 生成正文、完整参数、网络与强制沙箱的中文确认段。
def event_confirmation_lines(confirmation: dict) -> list[str]:
    if "events" not in confirmation:
        return []
    events = "、".join(event_description(event) for event in confirmation["events"]) or "无"
    lines = ["订阅事件：" + events,
             *[gate_description(gate) for gate in confirmation["tool_gates"]],
             "网络权限：" + ("允许联网" if confirmation["network"] else "禁止联网（含本机回环）"),
             "沙箱要求：强制使用插件进程沙箱；不可用时不得启用。"]
    read_scope = confirmation.get("read_scope")
    if read_scope:
        lines.append(read_scope)
    return lines


# LLM: 第一期只有提示观察允许 text；非提示即使收到旧式 text 事实也不展示参数承诺，不影响授权或确认码事实。
#   B6 的 /plugins info 展示复用本函数，保证确认码与展示同一措辞；改文案时联测 test_plugin_manifest_v8 与 B6 展示测试。
# 函数用途: 只为提示提交展示正文范围，其余观察事件统一说明不含正文。
def event_description(event: dict) -> str:
    if event["content"] != "text" or event["type"] != "prompt_submitted":
        return event["type"] + "（不含正文）"
    return event["type"] + "（可看提示文字，最多4000字）"


# LLM: full 已保证不含 effects；按效果订阅不交付完整参数，不猜当前工具存在性。B6 展示复用同一措辞。
# 函数用途: 展示一个收紧门的命中范围和可见参数。
def gate_description(gate: dict) -> str:
    scope = "、".join([*gate["tools"], *gate["effects"]])
    if gate["arguments"] == "full":
        return f"收紧门 {gate['id']}：能看到这些工具的完整参数：{scope}（脱敏后最多4000字）。"
    return f"收紧门 {gate['id']}：{scope}；只提供参数摘要哈希，不提供完整参数。"
