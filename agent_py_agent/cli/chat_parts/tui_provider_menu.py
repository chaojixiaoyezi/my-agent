# LLM: Provider 表单仅调用认证配置 API；密码不进历史，网络测试必须通过独立明确按钮。
# 模块用途: 提供服务商编辑、多模型管理、目录发现和短连接测试，复用当前 TUI 浮层。
from __future__ import annotations

import json
from uuid import uuid4

from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout import HSplit, ScrollablePane
from prompt_toolkit.widgets import Checkbox, Label, RadioList, TextArea

from .tui_model_menu import _choose_interface, _dialog, _request


# LLM: 字段无持久 history，密码始终掩码；长度校验由唯一服务端 schema 负责。
# 函数用途: 创建一个可 Tab 切换的单行配置框。
def _field(text="", *, secret=False):
    return TextArea(text=str(text), height=1, multiline=False, password=secret)


# LLM: 仅渲染当前用户 API 的脱敏列表；ID 是操作对象，展示名称不做机器判断。
# 函数用途: 选择一项已有模型或服务商，Esc 返回。
async def _choose(app, title: str, rows: list[tuple]):
    if not rows:
        await _dialog(app, title, Label("暂无可用配置，请先新增。"), (("返回", None),))
        return None
    choices = RadioList(rows, select_on_focus=True)
    return await _dialog(app, title, choices, (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)


# LLM: 内置预设只提供公开端点和本产品 UA，不伪造客户端认证或复制截图会话号。
# 函数用途: 新建时可使用 OpenCode Go 的官方接入地址，密钥仍由用户填写。
async def _provider_preset(app) -> dict | None:
    preset = await _choose(app, "新增服务商 · 选择模板", [("custom", "自定义服务商"), ("go", "OpenCode Go")])
    if preset is None:
        return None
    return {"id": "opencode-go", "display_name": "OpenCode Go", "api_base": "https://opencode.ai/zen/go/v1",
            "session_header": "x-opencode-session"} if preset == "go" else {}


# LLM: 服务商用途完整读写，不能在编辑时丢 decision；空秘密保留，清除须明确，保存不发模型请求。
# 函数用途: 编辑服务商及聊天/嵌入/决策用途，长表单可滚动；关闭时清除内存密码。
async def _edit_provider(app, agent, session: str, existing: dict | None = None) -> str:
    preset = existing if existing is not None else await _provider_preset(app)
    if preset is None:
        return ""
    identity = _field(preset.get("id", ""))
    if existing:
        identity.buffer.read_only = to_filter(True)
    name, address = _field(preset.get("display_name", "")), _field(preset.get("api_base", ""))
    secret, session_header = _field(secret=True), _field(preset.get("session_header", ""))
    headers = TextArea(text="", height=3, multiline=True)
    enabled = Checkbox("启用服务商", checked=preset.get("enabled", True))
    agentic = Checkbox("Agentic（对话/工具）", checked="agentic" in preset.get("capabilities", ["agentic"]))
    embedding = Checkbox("Embedding（目录配置；不用于聊天）", checked="embedding" in preset.get("capabilities", []))
    decision = Checkbox("Decision（决策建议；不用于聊天）", checked="decision" in preset.get("capabilities", []))
    clear_key, clear_headers = Checkbox("清空已保存密钥"), Checkbox("清空已保存自定义头")
    notice = Label("")
    form = HSplit([Label("Provider ID（保存后不变）"), identity, Label("展示名"), name, Label("Base URL"), address,
        Label("API Key（留空保留；不会进入聊天记录）"), secret, enabled, agentic, embedding, decision,
        Label("稳定会话头名称（可空；值由 my-agent 生成）"), session_header,
        Label("自定义请求头 JSON（留空保留）"), headers, clear_key, clear_headers,
        Label("Tab 切换 · 长表单自动滚动 · Esc 不保存退出"), notice])
    try:
        while await _dialog(app, "编辑服务商" if existing else "新增服务商", ScrollablePane(form, max_available_height=22),
                           (("保存", True), ("退出", None)), focus=name if existing else identity):
            try:
                parsed = json.loads(headers.text) if headers.text.strip() else None
            except ValueError:
                notice.text = "自定义请求头不是有效 JSON 对象。"
                continue
            result = await _request(app, agent, session, "save_provider", {"provider_id": identity.text,
                "editing": existing is not None, "clear_key": clear_key.checked, "clear_headers": clear_headers.checked,
                "provider": {"display_name": name.text, "api_base": address.text, "api_key": secret.text,
                    "enabled": enabled.checked, "capabilities": [v for v, c in (("agentic", agentic), ("embedding", embedding), ("decision", decision)) if c.checked],
                    "custom_headers": parsed, "session_header": session_header.text}})
            if result.get("ok"):
                return "服务商已保存；可以为它新增多个模型。未发模型请求。"
            notice.text = str(result.get("message") or "保存未确认，请刷新列表。")
    finally:
        secret.text = ""
        headers.text = ""
    return ""


# 思考控制方式的表单选项；取值与 backends/reasoning_control.REASONING_CONTROLS 一致，文案只供人读。
_REASONING_CONTROL_CHOICES = [
    ("auto", "自动（只对已核对的服务商生效，其余视为不支持）"),
    ("effort", "按推理强度档位发送（reasoning_effort / output_config.effort）"),
    ("budget", "按思考预算发送（thinking.budget_tokens；部分服务商只按开/关生效）"),
    ("none", "不支持调节（不发任何推理参数）"),
]
# 结构化输出方式的表单选项；取值与 backends/structured_output_mode.STRUCTURED_OUTPUT_MODES 一致，文案只供人读。
_STRUCTURED_OUTPUT_CHOICES = [
    ("auto", "自动（已核对不支持 json_schema 的服务商改用 JSON 对象，其余用原生方式）"),
    ("native", "原生严格方式（OpenAI 兼容发 json_schema；Anthropic 兼容用强制工具信封）"),
    ("json_object", "JSON 对象（只适用于 OpenAI 兼容接口；schema 写进提示，结果由程序校验）"),
]

# LLM: 编辑保留原用途/协议；decision 不发送生成采样字段和用途标签，保存无网络，同步用途表单回归。
#   用途标签按原值预填，编辑其它字段时不会丢失；标签只作决策模型的参考材料，不影响路由。
#   输入模态同样预填、逗号分隔；它是压缩策略"能否随图摘要"的声明事实，留空表示未知（由探针判断），不是能力证明。
#   思考控制方式按原值预选（缺省 auto），只决定 /effort 与子代理 effort 的档位怎样发送，取值见 reasoning_control。
#   结构化输出方式同样按原值预选，只决定记忆整理等后台结构化调用的输出格式，取值见 structured_output_mode。
# 函数用途: 新增或编辑生成/决策模型；认证在 provider 管理，原 UUID 不变，决策设置另行绑定。
async def _edit_model(app, agent, session: str, provider_id: str, row: dict | None = None) -> str:
    backend = await _choose_interface(app, default=(row or {}).get("model_backend"), allow_auth=False, allow_decision=True)
    if backend is None:
        return ""
    data = row or {}
    name = _field(data.get("model_name", ""))
    window = _field(data.get("model_context_window_tokens") or 128000)
    temperature = _field(data.get("temperature", ""))
    top_p = _field(data.get("top_p", ""))
    queue = _field(data.get("model_queue_wait_seconds", ""))
    usage = _field(", ".join(data.get("usage_tags", ())))
    modalities = _field(", ".join(data.get("input_modalities", ())))
    reasoning = RadioList(_REASONING_CONTROL_CHOICES, default=data.get("reasoning_control", "auto"), select_on_focus=True)
    structured = RadioList(_STRUCTURED_OUTPUT_CHOICES, default=data.get("structured_output", "auto"), select_on_focus=True)
    enabled = Checkbox("启用模型", checked=data.get("enabled", True))
    is_decision = backend == "typesafe_decision"
    capability = RadioList([("decision", "Decision 决策建议")] if is_decision else
                          [("agentic", "Agentic 对话/工具"), ("embedding", "Embedding 目录配置")],
                          default="decision" if is_decision else data.get("capability", "agentic"), select_on_focus=True)
    notice = Label("")
    body = HSplit([Label("模型名称（区分大小写）"), name, Label("总上下文 tokens（按供应商说明填写）"), window,
                   *([Label("决策等待时间与接入点分别设置；保存不启用，也不发请求。")] if is_decision else [
                   Label("温度 0–2（留空沿用部署值；按供应商要求填写）"), temperature,
                   Label("top_p 0–1（留空沿用部署/供应商；Flash 思考下限 0.95）"), top_p,
                   Label("额外排队预算秒数（0–86400，留空继承；慢模型可增大）"), queue,
                   Label("用途标签（可选，逗号分隔的小写英文标识，如 long_document, low_cost；只供决策模型比较候选时参考）"), usage,
                   Label("输入模态（可选，如 text, image；留空=未知，含图历史压缩时用结构化探针判断能否随图摘要）"), modalities,
                   Label("思考控制（决定 /effort 智能程度怎样发送；不确定就选自动）"), reasoning,
                   Label("结构化输出（记忆整理等后台调用的输出格式；不确定就选自动）"), structured]),
                   enabled, capability, notice, Label("Tab 切换 · Esc 不保存返回")])
    identity = data.get("id") or str(uuid4())
    while await _dialog(app, "编辑模型" if data.get("id") else "新增模型", body,
                       (("保存", True), ("返回", None)), focus=name):
        result = await _request(app, agent, session, "save_model", {"profile_id": identity, "editing": bool(data.get("id")),
            "profile": {"provider_id": provider_id, "model_backend": backend, "model_name": name.text,
                        "model_context_window_tokens": window.text,
                        **({} if is_decision else {"temperature": temperature.text, "top_p": top_p.text,
                                                 "model_queue_wait_seconds": queue.text, "usage_tags": usage.text,
                                                 "input_modalities": modalities.text,
                                                 "reasoning_control": reasoning.current_value,
                                                 "structured_output": structured.current_value}),
                        "enabled": enabled.checked, "capability": capability.current_value}})
        if result.get("ok"):
            return "决策模型已保存；尚未启用或测试。" if is_decision else "模型已保存；选择后将在后续工作片生效。"
        notice.text = str(result.get("message") or "保存未确认。")
    return ""


# LLM: 目录发现明确发一次 GET，选中目录条目不自动启用；容量缺失必须让用户填写。
# 函数用途: 获取模型名称列表并打开所选模型的配置表单。
async def _discover_models(app, agent, session: str, provider: str) -> str:
    result = await _request(app, agent, session, "discover", {"provider_id": provider})
    if not result.get("ok"):
        return str(result.get("message") or "目录读取失败。")
    rows = result["models"]
    name = await _choose(app, "发现模型 · 选择后确认接口及容量", [(row["model_name"], row["model_name"]) for row in rows])
    if name is None:
        return ""
    row = next(row for row in rows if row["model_name"] == name)
    return await _edit_model(app, agent, session, provider, row)


# LLM: 删除须二次明确确认，宿主拒绝删除仍有引用的服务商和当前模型；取消无任何副作用。
# 函数用途: 确认移除一项配置，不影响已经冻结的运行工作片。
async def _delete(app, agent, session: str, operation: str, key: str) -> str:
    if not await _dialog(app, "删除配置", Label("删除后，引用此配置的旧任务再次恢复可能报配置不存在。\n确定删除？"),
                         (("删除", True), ("取消", None))):
        return ""
    field = "provider_id" if operation == "delete_provider" else "profile_id"
    result = await _request(app, agent, session, operation, {field: key})
    return "配置已删除。" if result.get("ok") else str(result.get("message") or "未确认删除。")


# LLM: 所有菜单动作先刷新同 owner 配置，外部修改/删除后不得操作另一项；配置和测试入口分离。
# 函数用途: 管理一个服务商下的多个模型、编辑服务商或发现目录。
async def manage_providers(app, agent, session: str, *, create=False) -> str:
    if create:
        return await _edit_provider(app, agent, session)
    result = await _request(app, agent, session, "list")
    if not result.get("ok"):
        return str(result.get("message") or "无法读取服务商。")
    providers = result.get("providers", [])
    provider = await _choose(app, "服务商列表", [(row["id"], f"{row['display_name']} · {row['id']} · {'启用' if row['enabled'] else '停用'}") for row in providers])
    if provider is None:
        return ""
    action = await _choose(app, "服务商管理", [("edit", "编辑服务商 / 密钥 / 请求头"), ("add", "新增模型"),
        ("models", "编辑或删除已有模型"), ("discover", "获取远端模型列表"), ("delete", "删除服务商")])
    if action == "edit":
        return await _edit_provider(app, agent, session, next(row for row in providers if row["id"] == provider))
    if action == "add":
        return await _edit_model(app, agent, session, provider)
    if action == "discover":
        return await _discover_models(app, agent, session, provider)
    if action == "delete":
        return await _delete(app, agent, session, "delete_provider", provider)
    if action == "models":
        models = [row for row in result["profiles"] if row.get("provider_id") == provider]
        key = await _choose(app, "模型列表", [(row["id"], row["model_name"]) for row in models])
        if key:
            operation = await _choose(app, "模型管理", [("edit", "编辑模型（名称 / 接口 / 上下文）"), ("delete", "删除模型")])
            if operation == "edit":
                return await _edit_model(app, agent, session, provider, next(row for row in models if row["id"] == key))
            if operation == "delete":
                return await _delete(app, agent, session, "delete_model", key)
    return ""


# LLM: 测试明确说明有模型请求；成功不自动切模型，不把 basic probe 当任务验收，结果只在浮层展示。
# 函数用途: 为选定模型发送短问候，让用户看到实际返回和耗时。
async def test_connection(app, agent, session: str) -> str:
    result = await _request(app, agent, session, "list")
    rows = [row for row in result.get("profiles", []) if row.get("available")]
    key = await _choose(app, "连接测试 · 选择模型", [(row["id"], f"{row['model_name']} · {row['model_backend']}") for row in rows])
    if key is None:
        return ""
    if not await _dialog(app, "连接测试", Label("将发送一条短问候，会产生少量模型用量。\n不做任务、不派子代理、不执行工具、不切换当前模型。"),
                         (("开始测试", True), ("返回", None))):
        return ""
    result = await _request(app, agent, session, "probe", {"profile_id": key, "session_id": session})
    message = str(result.get("message") or "测试结果未知，请不要反复点击。")
    body = f"{message}\n模型：{result.get('model_name', '')}\n耗时：{result.get('elapsed_seconds', '?')} 秒\n{result.get('reply', '')}"
    if result.get("error_type"):
        body += f"\n错误类型：{result['error_type']} · HTTP {result.get('status_code', 0)}"
        body += "\n" + str(result.get("provider_message") or "")
    await _dialog(app, "连接测试结果", TextArea(text=body, read_only=True, width=72, height=10), (("返回", None),))
    return message
