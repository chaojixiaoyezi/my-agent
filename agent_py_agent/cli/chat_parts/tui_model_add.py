# LLM: /model「新增模型」唯一入口：先选 5 类之一；OpenAI Chat 再选用途（默认 agentic，也可 embedding），登录账号走
#   tui_model_auth.add_account；其余仍填连接（地址 + 密钥，请求头/会话头在「高级」里），经 discover 拉列表（不落盘）→ 勾选
#   → add_models 一次保存。非空目录时显式提供通用手动入口，可混选；空目录仍直接进入手动表单。embedding 用途作为结构化
#   capability 送同一个目录写入入口；对话请求保持原参数不变。
#   密钥只在掩码控件里短暂保留，关闭时清空；不调用模型、不切换会话模型。新增流程同步 test_tui_model_add、test_tui_manage_models 与
#   docs/design/TUI_MODEL_PROFILES.md；登录分支仍遵守 docs/design/MODEL_OAUTH.md。
# 模块用途: /model →「新增模型」：OpenAI Chat / Anthropic / OpenAI Responses / 登录账号 / Jev 决策五类，一次加多个模型。
from __future__ import annotations

import json

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label, RadioList, TextArea

from .tui_model_menu import _dialog, _request
from .tui_subscription_models import added_text, choose_models, with_ids

_TYPES = [
    ("openai_compatible", "OpenAI Chat 接口（DeepSeek、MiniMax、OpenCode 等大多数服务商）"),
    ("anthropic_compatible", "Anthropic 接口（Messages）"),
    ("openai_responses", "OpenAI Responses 接口"),
    ("auth", "登录账号（ChatGPT Plus / Pro 订阅、通用 OAuth）"),
    ("typesafe_decision", "Jev 决策模型（只给建议，不用于聊天）"),
]
_OPENAI_CAPABILITIES = [("agentic", "对话（默认）"), ("embedding", "Embedding（向量模型）")]
# 内置模板只填公开地址和会话头名称，密钥仍由用户填写；会话头的值由 my-agent 按会话生成，不复制别处的会话号。
_OPENCODE_GO = {"display_name": "OpenCode Go", "api_base": "https://opencode.ai/zen/go/v1", "session_header": "x-opencode-session"}


# LLM: 类型是结构化选项值；登录账号不填连接，直接进登录流程（成功后同样弹勾选框）。
# 函数用途: 「新增模型」入口：选类型，再按类型进入填连接或登录账号，返回给主菜单显示的结果文字。
async def add_models(app, agent, session: str) -> str:
    choices = RadioList(_TYPES, select_on_focus=True)
    kind = await _dialog(app, "新增模型 · 选择类型", choices,
                         (("下一步", lambda: choices.current_value), ("返回", None)), focus=choices)
    if kind is None:
        return ""
    if kind == "auth":
        from .tui_model_auth import add_account

        return await add_account(app, agent, session)
    return await _ConnectionForm(app, agent, session, kind).run()


# LLM: 表单控件只在本次新增存续期间持有密钥与请求头，run 结束必清空；「拉取模型列表」前只做非空与 JSON 形状检查，
#   地址、请求头名称等权威校验在服务端 connection_provider。按钮值都是结构化动作，不解析标签。
# 类用途: 「新增模型」里填写连接（地址、密钥，高级里的请求头和会话头）并完成拉列表、勾选、保存。
class _ConnectionForm:
    # LLM: form 持有一次新增流程状态；backend 决定是否展示用途选择，默认 capability 与旧连接流程相同。
    # 函数用途: 初始化新增连接表单和控件；只有 OpenAI Chat 支持在此流程里显式改为 Embedding。
    def __init__(self, app, agent, session: str, backend: str):
        self.app, self.agent, self.session, self.backend = app, agent, session, backend
        self.capability = "agentic"
        self.address = TextArea(height=1, multiline=False)
        self.key = TextArea(height=1, multiline=False, password=True)
        self.headers = TextArea(height=3, multiline=True)
        self.session_header = TextArea(height=1, multiline=False)
        self.notice = Label("")
        self.form = HSplit([
            Label("接口地址（如 https://api.deepseek.com；不要带 /chat/completions、/messages 这类后缀）"), self.address,
            Label("密钥（不会进入聊天记录）"), self.key, self.notice,
            Label("Tab 切换 ·「拉取模型列表」后勾选要用的模型，拉不到可手动填 · 请求头在「高级」· Esc 返回")])
        self.advanced = HSplit([
            Label('自定义请求头（JSON 对象，可空），如 {"HTTP-Referer": "https://example.com"}'), self.headers,
            Label("会话头名称（可空；值由 my-agent 按会话自动生成，如 OpenCode 的 x-opencode-session）"), self.session_header])

    # LLM: 只为 OpenAI Chat 加用途选择，首项是原 agentic 行为；Esc 不保存。循环直到保存成功或用户返回，finally 清空密钥和请求头。
    # 函数用途: 先选对话或向量用途，再显示连接表单并处理按钮；取消返回空串。
    async def run(self) -> str:
        actions = [("拉取模型列表", "fetch")]
        if self.backend != "typesafe_decision":
            actions.append(("OpenCode Go 模板", "template"))
        actions += [("高级", "advanced"), ("返回", None)]
        try:
            if self.backend == "openai_compatible":
                choices = RadioList(_OPENAI_CAPABILITIES, select_on_focus=True)
                self.capability = await _dialog(
                    self.app, "新增模型 · 选择用途", choices,
                    (("下一步", lambda: choices.current_value), ("返回", None)), focus=choices)
                if self.capability is None:
                    return ""
            while True:
                action = await _dialog(self.app, "新增模型 · 填写连接", self.form, tuple(actions), focus=self.address)
                if action is None:
                    return ""
                message = await self._act(action)
                if message:
                    return message
        finally:
            self.key.text = ""
            self.headers.text = ""

    # LLM: template 只填公开地址与会话头名称；advanced 只展示高级字段；fetch 才发网络请求（一次目录 GET）。
    # 函数用途: 执行一个表单按钮动作；返回非空文字表示流程结束。
    async def _act(self, action: str) -> str:
        if action == "template":
            self.address.text, self.session_header.text = _OPENCODE_GO["api_base"], _OPENCODE_GO["session_header"]
            self.notice.text = "已填入 OpenCode Go 的地址和会话头，请填写密钥。"
            return ""
        if action == "advanced":
            await _dialog(self.app, "新增模型 · 高级", self.advanced, (("完成", True),), focus=self.headers)
            return ""
        connection = self._connection()
        return await self._pick_and_save(connection) if connection else ""

    # LLM: 请求头必须是 JSON 对象文本，空文本表示没有；展示名只在地址仍是模板地址时沿用模板名，其余由服务端取主机名。
    # 函数用途: 把表单内容整理成一份连接（不保存）；缺项或格式错时在表单上提示并返回 None。
    def _connection(self) -> dict | None:
        try:
            headers = json.loads(self.headers.text) if self.headers.text.strip() else {}
        except ValueError:
            headers = None
        if not isinstance(headers, dict):
            self.notice.text = "「高级」里的请求头不是有效的 JSON 对象。"
            return None
        address = self.address.text.strip()
        if not address or not self.key.text.strip():
            self.notice.text = "请填写接口地址和密钥。"
            return None
        name = _OPENCODE_GO["display_name"] if address == _OPENCODE_GO["api_base"] else ""
        return {"model_backend": self.backend, "api_base": address, "api_key": self.key.text.strip(),
                "custom_headers": headers, "session_header": self.session_header.text.strip(), "display_name": name}

    # LLM: 目录读取与保存各一次请求；非空列表允许勾选模型并由回调补填，保存仍沿唯一 add_models 写入口且整批原子提交。
    #   空列表/失败保留直接手动表单；embedding 显式传 capability，agentic 参数保持旧形状。
    # 函数用途: 拉模型列表供勾选和可选手动补充（拉不到则直接手动填），随后一次保存并按用途提示下一步。
    async def _pick_and_save(self, connection: dict) -> str:
        catalog = await _request(self.app, self.agent, self.session, "discover", {"connection": connection})
        rows = catalog.get("models") if catalog.get("ok") else None
        if rows:
            picked = await choose_models(
                self.app, rows, "选择要添加的模型（可多选）",
                manual_entry=lambda: self._manual(catalog, from_nonempty_catalog=True))
        else:
            picked = await self._manual(catalog)
        if not picked:
            return ""
        payload = {"connection": connection, "models": with_ids(picked)}
        if self.capability == "embedding":
            payload["capability"] = "embedding"
        result = await _request(self.app, self.agent, self.session, "add_models", payload)
        if not result.get("ok"):
            self.notice.text = added_text(result, picked)
            return ""
        return added_text(result, picked, embedding=self.capability == "embedding")

    # LLM: 入口来源由调用方传结构化标志，非空目录补填不能伪称目录读取失败；错误详情仍只显示服务端已脱敏文本。
    #   手动模型名与上下文由服务端 validate_model 校验；取消返回 None，不写入目录。
    # 函数用途: 为目录缺项或读取失败提供手动填写表单，并按真实入口提示用户。
    async def _manual(self, catalog: dict, *, from_nonempty_catalog: bool = False) -> list[dict] | None:
        reason = str(catalog.get("message") or "接口没有列出模型。") if not catalog.get("ok") else "接口没有列出模型。"
        detail = str(catalog.get("provider_message") or "")
        name, window = TextArea(height=1, multiline=False), TextArea(text="128000", height=1, multiline=False)
        heading = "列表里没有的模型，可以在这里手动填写" if from_nonempty_catalog else f"没拿到模型列表：{reason}"
        body = HSplit([Label(heading + (f"\n服务商说明：{detail}" if detail else "")),
                       Label("可以手动填写模型名称（区分大小写）"), name, Label("上下文窗口（总 tokens）"), window])
        if not await _dialog(self.app, "新增模型 · 手动填写", body, (("添加", True), ("返回修改", None)), focus=name):
            return None
        if not name.text.strip():
            self.notice.text = "没有填写模型名称，未添加。"
            return None
        return [{"model_name": name.text.strip(), "display_name": name.text.strip(),
                 "model_context_window_tokens": window.text.strip()}]
