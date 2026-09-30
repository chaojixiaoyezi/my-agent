# LLM: Provider 表单仅调用认证配置 API；密码不进历史，网络测试必须通过独立明确按钮。连接和登录账号在同一个列表里管理；
#   表单只放常用字段，其余进「高级」（控件一次建好，两处共享同一份值，保存时整组提交）。
#   改动须同步 test_tui_manage_models、test_decision_model_profiles、test_model_usage_tags 等表单回归。
# 模块用途: /model →「管理已有模型」（改连接、账号、编辑/删除模型、从连接再添加模型）和「连接测试」。
from __future__ import annotations

import json
from collections import Counter
from uuid import uuid4

from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout import HSplit, ScrollablePane
from prompt_toolkit.widgets import Checkbox, CheckboxList, Label, RadioList, TextArea

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


# LLM: 服务商用途完整读写，不能在编辑时丢 decision；空秘密保留，清除须明确，保存不发模型请求。
#   编号只读显示；会话头、自定义请求头、清空选项和用途在「高级」里。
# 函数用途: 修改一个已有连接的名称、地址、密钥、请求头和用途；关闭时清除内存里的密码和请求头。
async def _edit_provider(app, agent, session: str, existing: dict) -> str:
    identity = _field(existing["id"])
    identity.buffer.read_only = to_filter(True)
    name, address = _field(existing.get("display_name", "")), _field(existing.get("api_base", ""))
    secret, session_header = _field(secret=True), _field(existing.get("session_header", ""))
    headers = TextArea(text="", height=3, multiline=True)
    enabled = Checkbox("启用这个连接", checked=existing.get("enabled", True))
    capabilities = existing.get("capabilities", ["agentic"])
    agentic = Checkbox("Agentic（对话/工具）", checked="agentic" in capabilities)
    embedding = Checkbox("Embedding（目录配置；不用于聊天）", checked="embedding" in capabilities)
    decision = Checkbox("Decision（决策建议；不用于聊天）", checked="decision" in capabilities)
    clear_key, clear_headers = Checkbox("清空已保存密钥"), Checkbox("清空已保存自定义头")
    notice = Label("")
    form = HSplit([Label("连接编号（不可改）"), identity, Label("名称"), name, Label("接口地址"), address,
                   Label("密钥（留空保留原密钥；不会进入聊天记录）"), secret, enabled, notice,
                   Label("Tab 切换 · 请求头、会话头和用途在「高级」· Esc 不保存退出")])
    advanced = HSplit([Label("会话头名称（可空；值由 my-agent 按会话生成）"), session_header,
                       Label(f"自定义请求头 JSON（留空保留；已保存：{', '.join(existing.get('header_names', [])) or '无'}）"), headers,
                       clear_key, clear_headers, Label("用途"), agentic, embedding, decision])
    try:
        while True:
            action = await _dialog(app, "修改连接", form, (("保存", True), ("高级", "advanced"), ("退出", None)), focus=name)
            if action is None:
                return ""
            if action == "advanced":
                await _dialog(app, "修改连接 · 高级", ScrollablePane(advanced, max_available_height=20), (("完成", True),),
                              focus=session_header)
                continue
            try:
                parsed = json.loads(headers.text) if headers.text.strip() else None
            except ValueError:
                notice.text = "「高级」里的自定义请求头不是有效 JSON 对象。"
                continue
            result = await _request(app, agent, session, "save_provider", {"provider_id": identity.text,
                "editing": True, "clear_key": clear_key.checked, "clear_headers": clear_headers.checked,
                "provider": {"display_name": name.text, "api_base": address.text, "api_key": secret.text,
                    "enabled": enabled.checked, "capabilities": [v for v, c in (("agentic", agentic), ("embedding", embedding), ("decision", decision)) if c.checked],
                    "custom_headers": parsed, "session_header": session_header.text}})
            if result.get("ok"):
                return "连接已保存；未发模型请求。"
            notice.text = str(result.get("message") or "保存未确认，请刷新列表。")
    finally:
        secret.text = ""
        headers.text = ""


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
_GENERATION_BACKENDS = [("openai_compatible", "OpenAI 风格（Chat Completions）"),
                        ("anthropic_compatible", "Anthropic 风格（Messages）"),
                        ("openai_responses", "OpenAI Responses（响应 / 工具流）")]


# LLM: 生成模型的「高级」字段一次建好；表单与高级页共享同一组控件，保存时整组提交，不因没打开高级而丢原值。
#   用途标签、输入模态、思考控制、结构化输出的语义见 _edit_model 注释；决策模型不建这些控件。
# 类用途: 持有一个生成模型编辑表单的高级字段，并把它们整理成 save_model 的字段。
class _GenerationFields:
    def __init__(self, data: dict, backend: str):
        self.backend = RadioList(_GENERATION_BACKENDS, default=backend, select_on_focus=True)
        self.temperature, self.top_p = _field(data.get("temperature", "")), _field(data.get("top_p", ""))
        self.queue = _field(data.get("model_queue_wait_seconds", ""))
        self.usage = _field(", ".join(data.get("usage_tags", ())))
        self.modalities = _field(", ".join(data.get("input_modalities", ())))
        self.levels = _field(", ".join(data.get("reasoning_levels", ())))
        self.reasoning = RadioList(_REASONING_CONTROL_CHOICES, default=data.get("reasoning_control", "auto"), select_on_focus=True)
        self.structured = RadioList(_STRUCTURED_OUTPUT_CHOICES, default=data.get("structured_output", "auto"), select_on_focus=True)
        self.capability = RadioList([("agentic", "Agentic 对话/工具"), ("embedding", "Embedding 目录配置")],
                                    default=data.get("capability", "agentic"), select_on_focus=True)
        self.body = HSplit([Label("接口类型（一般不用改）"), self.backend, Label("用途"), self.capability,
            Label("温度 0–2（留空沿用部署值；按供应商要求填写）"), self.temperature,
            Label("top_p 0–1（留空沿用部署/供应商；Flash 思考下限 0.95）"), self.top_p,
            Label("额外排队预算秒数（0–86400，留空继承；慢模型可增大）"), self.queue,
            Label("用途标签（可选，逗号分隔的小写英文标识，如 long_document, low_cost；只供决策模型比较候选时参考）"), self.usage,
            Label("输入模态（可选，如 text, image；留空=未知，含图历史压缩时用结构化探针判断能否随图摘要）"), self.modalities,
            Label("思考控制（决定 /effort 智能程度怎样发送；不确定就选自动）"), self.reasoning,
            Label("服务商支持的思考档位（可选，逗号分隔，如 low, medium, high；Responses 接口用它对应 /effort，留空=未声明）"), self.levels,
            Label("结构化输出（记忆整理等后台调用的输出格式；不确定就选自动）"), self.structured])

    # 函数用途: 把高级字段整理成 save_model 的 profile 字段（取值合法性由服务端校验）。
    def values(self) -> dict:
        return {"model_backend": self.backend.current_value, "capability": self.capability.current_value,
                "temperature": self.temperature.text, "top_p": self.top_p.text, "model_queue_wait_seconds": self.queue.text,
                "usage_tags": self.usage.text, "input_modalities": self.modalities.text, "reasoning_levels": self.levels.text,
                "reasoning_control": self.reasoning.current_value, "structured_output": self.structured.current_value}


# LLM: 编辑保留原用途/协议；decision 不发送生成采样字段和用途标签，保存无网络，同步用途表单回归。
#   用途标签按原值预填，编辑其它字段时不会丢失；标签只作决策模型的参考材料，不影响路由。
#   输入模态同样预填、逗号分隔；它是压缩策略"能否随图摘要"的声明事实，留空表示未知（由探针判断），不是能力证明。
#   思考控制方式按原值预选（缺省 auto），只决定 /effort 与子代理 effort 的档位怎样发送，取值见 reasoning_control。
#   结构化输出方式同样按原值预选，只决定记忆整理等后台结构化调用的输出格式，取值见 structured_output_mode。
#   原记录已有接口类型就不再先弹接口选择（接口在「高级」里改）；没有时先选接口。
# 函数用途: 新增或编辑生成/决策模型；认证在账号管理，原 UUID 不变，决策设置另行绑定。
async def _edit_model(app, agent, session: str, provider_id: str, row: dict | None = None) -> str:
    data = row or {}
    backend = data.get("model_backend") or await _choose_interface(app, allow_decision=True)
    if backend is None:
        return ""
    is_decision = backend == "typesafe_decision"
    name = _field(data.get("model_name", ""))
    window = _field(data.get("model_context_window_tokens") or 128000)
    enabled = Checkbox("启用模型", checked=data.get("enabled", True))
    extra = None if is_decision else _GenerationFields(data, backend)
    notice = Label("")
    body = HSplit([Label("模型名称（区分大小写）"), name, Label("总上下文 tokens（按供应商说明填写）"), window, enabled, notice,
                   Label("决策等待时间与接入点在「选择模型 → 决策模型」里设置；保存不启用，也不发请求。" if is_decision
                         else "温度、思考控制等在「高级」· Tab 切换 · Esc 不保存返回")])
    identity = data.get("id") or str(uuid4())
    title = "编辑模型" if data.get("id") else "新增模型"
    actions = (("保存", True), ("返回", None)) if is_decision else (("保存", True), ("高级", "advanced"), ("返回", None))
    while True:
        action = await _dialog(app, title, body, actions, focus=name)
        if action is None:
            return ""
        if action == "advanced":
            await _dialog(app, title + " · 高级", ScrollablePane(extra.body, max_available_height=20), (("完成", True),),
                          focus=extra.backend)
            continue
        profile = {"provider_id": provider_id, "model_backend": backend, "model_name": name.text,
                   "model_context_window_tokens": window.text, "enabled": enabled.checked,
                   **({"capability": "decision"} if extra is None else extra.values())}
        result = await _request(app, agent, session, "save_model", {"profile_id": identity, "editing": bool(data.get("id")),
                                                                  "profile": profile})
        if result.get("ok"):
            return "决策模型已保存；尚未启用或测试。" if is_decision else "模型已保存；选择后将在后续工作片生效。"
        notice.text = str(result.get("message") or "保存未确认。")


# LLM: 删除须二次明确确认，宿主拒绝删除仍有引用的服务商和当前模型；取消无任何副作用。
# 函数用途: 确认移除一项配置，不影响已经冻结的运行工作片。
async def _delete(app, agent, session: str, operation: str, key: str) -> str:
    if not await _dialog(app, "删除配置", Label("删除后，引用此配置的旧任务再次恢复可能报配置不存在。\n确定删除？"),
                         (("删除", True), ("取消", None))):
        return ""
    field = "provider_id" if operation == "delete_provider" else "profile_id"
    result = await _request(app, agent, session, operation, {field: key})
    return "配置已删除。" if result.get("ok") else str(result.get("message") or "未确认删除。")


# 「管理已有模型」列表里"删除模型"一行的动作值；服务商编号不可能以两个下划线开头（validate_provider_id）。
_DELETE_MODELS = "__delete_models__"


# LLM: 只列自己的原记录（不含部署默认行和 shared: 别名）；二次确认后逐个走 delete_model 原入口，宿主拒绝删除当前默认模型，
#   单个失败不影响其它，结果按回执 ok 区分。删除后引用它的会话下一次明确报"配置不存在"，不偷换模型。
# 函数用途: 勾选一个或多个模型并删除，返回给用户看的结果文字。
async def _delete_models(app, agent, session: str, listing: dict) -> str:
    rows = [row for row in listing.get("profiles", []) if row["id"] != "default" and not row.get("shared")]
    if not rows:
        return "还没有可以删除的模型。"
    box = CheckboxList([(row["id"], f"{row['model_name']} · {row.get('provider_name', '')}") for row in rows])
    picked = await _dialog(app, "删除模型（可多选）", HSplit([Label("空格勾选要删的模型，Tab 到「删除」再回车；新会话默认模型不能删。"), box]),
                           (("删除", lambda: list(box.current_values)), ("返回", None)), focus=box)
    if not picked or not await _dialog(app, "确认删除", Label(f"将删除 {len(picked)} 个模型；引用它们的会话之后会提示重新选择模型。\n确定删除？"),
                                       (("删除", True), ("取消", None))):
        return ""
    names = {row["id"]: row["model_name"] for row in rows}
    failed = []
    for key in picked:
        result = await _request(app, agent, session, "delete_model", {"profile_id": key})
        if not result.get("ok"):
            failed.append(f"{names[key]}（{result.get('message') or '未确认'}）")
    text = f"已删除 {len(picked) - len(failed)} 个模型。"
    return text + (f"没删成：{'、'.join(failed)}" if failed else "")


# LLM: 行文字只给人看，编号是操作对象；登录账号显示登录状态，API 连接显示地址。
# 函数用途: 生成「管理已有模型」列表里一行连接/账号的文字，如「DeepSeek · https://… · 2 个模型」。
def _provider_label(row: dict, counts: Counter) -> str:
    state = ("已登录" if row.get("signed_in") else "未登录") if row.get("auth_mode") in {"chatgpt", "oauth_device"} else row["api_base"]
    return f"{row['display_name']} · {state} · {counts.get(row['id'], 0)} 个模型{'' if row['enabled'] else ' · 已停用'}"


# LLM: 所有动作先刷新同 owner 配置，外部修改/删除后不得操作另一项；登录账号交给 tui_model_auth.manage_account。
# 函数用途: 「管理已有模型」入口：选一个连接或账号，再选要做的事。
async def manage_models(app, agent, session: str) -> str:
    result = await _request(app, agent, session, "list")
    if not result.get("ok"):
        return str(result.get("message") or "无法读取模型配置。")
    providers = result.get("providers", [])
    # 管理员列表里自己共享出去的模型还会以 shared: 别名再出现一次，管理和计数只算自己的原记录。
    counts = Counter(row.get("provider_id") for row in result["profiles"] if not row.get("shared"))
    provider = await _choose(app, "管理已有模型 · 选择连接或账号", [
        *[(row["id"], _provider_label(row, counts)) for row in providers], (_DELETE_MODELS, "删除模型（勾选一个或多个）")])
    if provider is None:
        return ""
    if provider == _DELETE_MODELS:
        return await _delete_models(app, agent, session, result)
    row = next(row for row in providers if row["id"] == provider)
    if row.get("auth_mode") in {"chatgpt", "oauth_device"}:
        from .tui_model_auth import manage_account

        return await manage_account(app, agent, session, row)
    action = await _choose(app, f"连接操作 · {row['display_name']}", [
        ("models", "编辑或删除这个连接下的模型"), ("add", "从这个连接再添加模型（拉列表勾选）"),
        ("edit", "修改地址 / 密钥 / 请求头"), ("delete", "删除这个连接（需先删掉其下模型）")])
    if action == "models":
        return await manage_provider_models(app, agent, session, provider)
    if action == "add":
        return await _add_from_provider(app, agent, session, row)
    if action == "edit":
        return await _edit_provider(app, agent, session, row)
    return await _delete(app, agent, session, "delete_provider", provider) if action == "delete" else ""


# LLM: 列表重新读取，只列属于这个服务商的自己的原记录（不含 shared: 别名，别名不能编辑）；编辑与删除都走原保存入口，编号不变。
# 函数用途: 选一个模型，再编辑或删除它。
async def manage_provider_models(app, agent, session: str, provider: str) -> str:
    result = await _request(app, agent, session, "list")
    models = [row for row in result.get("profiles", []) if row.get("provider_id") == provider and not row.get("shared")]
    key = await _choose(app, "模型列表", [(row["id"], f"{row['model_name']} · {row['model_backend']}"
                                              f"{'' if row.get('enabled', True) else ' · 已停用'}") for row in models])
    if not key:
        return ""
    operation = await _choose(app, "模型管理", [("edit", "编辑模型（名称 / 上下文 / 高级参数）"), ("delete", "删除模型")])
    if operation == "edit":
        return await _edit_model(app, agent, session, provider, next(row for row in models if row["id"] == key))
    return await _delete(app, agent, session, "delete_model", key) if operation == "delete" else ""


# LLM: 目录读取明确发一次 GET；接口类型由用户选（默认取这个连接下已有模型的接口），不按地址猜；读不到列表时退回单个模型表单。
# 函数用途: 从一个已保存的连接拉模型列表，勾选后一次添加。
async def _add_from_provider(app, agent, session: str, row: dict) -> str:
    from .tui_subscription_models import added_text, choose_models, with_ids

    listing = await _request(app, agent, session, "list")
    known = [item["model_backend"] for item in listing.get("profiles", [])
             if item.get("provider_id") == row["id"] and not item.get("shared")]
    backend = await _choose_interface(app, default=known[0] if known else None, allow_decision=True)
    if backend is None:
        return ""
    catalog = await _request(app, agent, session, "discover", {"provider_id": row["id"]})
    if not catalog.get("ok") or not catalog.get("models"):
        return await _edit_model(app, agent, session, row["id"], {"model_backend": backend})
    picked = await choose_models(app, catalog["models"], "选择要添加的模型（可多选）")
    if not picked:
        return ""
    result = await _request(app, agent, session, "add_models", {"provider_id": row["id"], "model_backend": backend,
                                                              "models": with_ids(picked)})
    return added_text(result, picked)


# LLM: 测试明确说明有模型请求；成功不自动切模型，不把 basic probe 当任务验收，结果只在浮层展示。
#   决策模型转交决策菜单的限时测试（同一个 decision_probe 入口），不走普通问候。
# 函数用途: 为选定模型发送短问候（或限时决策测试），让用户看到实际返回和耗时。
async def test_connection(app, agent, session: str) -> str:
    result = await _request(app, agent, session, "list")
    # 「默认」行没有可测的已保存连接（部署配置不走这里；管理员初始模型另有 shared: 行），不列。
    rows = [row for row in result.get("profiles", []) if row["id"] != "default"
            and (row.get("available") or "decision" in row.get("available_for", []))]
    key = await _choose(app, "连接测试 · 选择模型", [(row["id"], f"{row['model_name']} · "
                                                     f"{'决策' if row.get('capability') == 'decision' else row['model_backend']}") for row in rows])
    if key is None:
        return ""
    if next(row for row in rows if row["id"] == key).get("capability") == "decision":
        from .tui_decision_menu import probe_selected

        return await probe_selected(app, agent, session, key)
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
