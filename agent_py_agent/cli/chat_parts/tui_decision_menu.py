# LLM: 决策菜单只走原认证配置运输；实验能力开关不授予许可，不提供 authorize UI，字段范围和 CAS 仍由原设置服务控制。
# 模块用途: 在现有 TUI 浮层设置用户/会话决策覆盖、恢复继承，并显式测试已保存的决策连接。
from __future__ import annotations

import json
import math

from prompt_toolkit.layout import HSplit, ScrollablePane
from prompt_toolkit.widgets import CheckboxList, Label, RadioList, TextArea

from ...agent.settings.decision_settings_schema import POINTS, validate_decision_field
from .tui_model_menu import _dialog, _request

# 接入点集合以 schema 登记的 POINTS 为唯一权威；这里只配中文显示名，缺显示名时直接显示原键，不能漏项或让菜单崩溃。
_POINT_NAMES = {"model_selection": "模型选择", "subagent_model": "子代理模型", "skill_tool": "Skill / 工具推荐",
                "pre_recall": "记忆召回前补充查询", "recall": "记忆召回后重排", "curator": "后台记忆整理（用户长期）",
                "curator_relation": "正式记忆关系建议（用户后台）", "external_material_order": "外部材料阅读优先级",
                "planning": "现有待办优先级", "delivery_quality": "交付复核焦点", "action_candidate": "动作候选",
                "skill_proposal_review": "Skill 提案审核顺序（用户长期）"}
_POINTS = {point: _POINT_NAMES.get(point, point) for point in POINTS}
_GENERAL = {"enabled": "总开关", "profile_id": "默认决策模型", "timeout_seconds": "前台单次上限（秒）",
            "stage_timeout_seconds": "前台阶段上限（秒）", "background_timeout_seconds": "后台阶段上限（秒）",
            "experiment_enabled": "实验能力（仍需独立授权）"}
_POINT_FIELDS = {"mode": "模式", "profile_id": "决策模型", "timeout_seconds": "单次上限（秒）",
                 "context_policy": "上下文减量策略", "optional_categories": "可选工具类别",
                 "candidate_profile_ids": "子代理执行模型候选"}
# 模式仍只有 off/observe/apply 三个存储值；界面按"开/关 + 观察模式"呈现（关=off，开+观察=observe，开+不观察=apply）
_MODES = {"off": "关", "observe": "开 · 观察模式", "apply": "开 · 正式使用"}
_CONTEXT_POLICIES = {"metadata": "仅精简名卡与推荐", "progressive": "名卡与可选工具渐进披露"}


# LLM: 来源是原 settings 的结构化字段，中文仅用于显示，不根据展示文本生成修改或权限。
# 函数用途: 把原字段继承路径显示为用户能理解的配置来源。
def _source(value: str) -> str:
    if value in {"owner", "thread"}:
        return "用户长期" if value == "owner" else "本会话"
    if value.startswith("inherit:"):
        _, field, source = value.split(":", 2)
        return f"继承{_GENERAL.get(field, field)} · {_source(source)}"
    return "能力默认配置" if value.startswith("capability_config.") else "记忆默认配置" if value.startswith("memory_config.") else "应用默认配置"


# LLM: 值只读服务端 effective，不能用本地默认或编辑框内容假装已保存生效。
# 函数用途: 读取一个已登记字段的当前有效值。
def _value(view: dict, field: str):
    value = view["effective"]
    for key in field.split("."):
        value = value[key]
    return value


# LLM: 展示与操作键分离；实验布尔值只展示能力开关，不能把开启文案当授权事实，不展示秘密或收费价格。
# 函数用途: 为字段菜单和恢复列表组合名称、准确布尔值及继承来源。
def _field_label(view: dict, field: str) -> str:
    parts = field.split(".")
    name = _GENERAL[field] if len(parts) == 1 else f"{_POINTS[parts[1]]} · {_POINT_FIELDS[parts[2]]}"
    value = _value(view, field)
    shown = ("开启" if value else "关闭") if field in {"enabled", "experiment_enabled"} else _MODES.get(value, value) if field.endswith(".mode") else value or "未绑定"
    if field.endswith((".optional_categories", ".candidate_profile_ids")):
        shown = json.dumps(value, ensure_ascii=False)
    elif field.endswith(".context_policy"):
        shown = _CONTEXT_POLICIES[value]
    return f"{name}：{shown}（{_source(view['sources'][field])}）"


# LLM: 可写范围只读同一次原服务 field_scopes；未登记字段不能因本地菜单支持而得到权限。
# 函数用途: 把界面支持的字段与服务端当前作用域交集用于表单，旧覆盖清理另走 reset。
def _fields(view: dict) -> list[str]:
    supported = list(_GENERAL) + [f"points.{point}.{field}" for point in _POINTS for field in _POINT_FIELDS]
    return [field for field in supported if view["scope"] in view.get("field_scopes", {}).get(field, [])]


# LLM: reset 可清理不再允许新写入的旧 thread 字段；明确区分旧 override 与当前有效值，不能暗示后台消费该覆盖。
# 函数用途: 为恢复继承列表显示原覆盖，并标出只供清理的历史字段。
def _reset_label(view: dict, field: str) -> str:
    label = _field_label(view, field)
    if field not in _fields(view):
        value = view["overrides"][view["scope"]][field]
        return f"{label} · 旧覆盖：{value}（此范围不消费，仅可清理）"
    return label


# LLM: 每次菜单选择均通过 RadioList 的结构化 value 返回，不把键盘输入送入聊天历史。
# 函数用途: 在原浮层展示可滚动选项和说明，Esc 返回。
async def _choose(app, title: str, rows: list[tuple], *, notice: str = "", default=None):
    choices = RadioList(rows, default=default, select_on_focus=True)
    return await _dialog(app, title, HSplit([Label(notice), ScrollablePane(HSplit([choices]), max_available_height=15)]),
                         (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)


# LLM: 作用域来自用户按钮，可信 session 只交原运输解析；不接受 owner/thread ID 输入，不调用普通 select。
# 函数用途: 从 /model 打开用户长期或本会话决策设置。
async def manage_decision_settings(app, agent, session_id: str) -> str:
    while True:
        scope = await _choose(app, "决策模型 · 设置范围", [("owner", "用户长期设置"), ("thread", "本会话临时设置")],
                              notice="决策默认关闭，只提供建议；不更改当前聊天模型。")
        if scope is None:
            return "决策设置已返回；当前聊天模型未切换。"
        await _manage_scope(app, agent, session_id, scope)


# LLM: 每轮重读同一 owner/thread 设置与 CAS；保存失败不会重放，连接失效不阻止进入开关和恢复继承。
# 函数用途: 编辑一个作用域的字段或接入点，显示已确认的有效值和来源。
async def _manage_scope(app, agent, session: str, scope: str) -> None:
    message = "时间修改不重置正在运行阶段的余额。"
    while True:
        view = await _request(app, agent, session, "decision_read", {"decision": {"scope": scope}})
        if not view.get("ok"):
            await _dialog(app, "决策设置读取失败", Label(str(view.get("message") or "请检查配置后重试。")), (("返回", None),))
            return
        rows = [(field, _field_label(view, field)) for field in _GENERAL if field in _fields(view)]
        rows.extend([("points", "逐接入点设置（模式 / 模型 / 时间 / 减量）"), ("reset", "逐字段恢复继承"),
                     ("providers", "管理服务商 / 决策模型"), ("provider_add", "新增服务商 / 决策模型"),
                     ("probe", "主动测试决策连接（会产生少量用量）")])
        notice = message + ("\n后台记忆整理及后台期限只在用户长期设置生效，请返回上一层修改。" if scope == "thread" else "")
        action = await _choose(app, "决策设置 · " + ("用户长期" if scope == "owner" else "本会话"), rows, notice=notice)
        if action is None:
            return
        message = await _scope_action(app, agent, session, view, action) or message


# LLM: 只分发已登记动作；providers 复用原用途表单，probe 有独立按钮；不能顺带启用或修改聊天选择。
# 函数用途: 执行一个决策设置动作并把确认结果交回重读菜单。
async def _scope_action(app, agent, session: str, view: dict, action: str) -> str:
    if action in _GENERAL:
        return await _edit_field(app, agent, session, view, action)
    if action == "points":
        return await _edit_point(app, agent, session, view)
    if action == "reset":
        fields = [field for field in view["overrides"][view["scope"]] if field in view.get("field_scopes", {})]
        if not fields:
            return "本范围没有字段覆盖，已全部继承上层配置。"
        field = await _choose(app, "恢复继承 · 选择一个覆盖字段", [(field, _reset_label(view, field)) for field in fields])
        return await _save(app, agent, session, view, field, reset=True) if field else ""
    if action in {"providers", "provider_add"}:
        from .tui_provider_menu import manage_providers

        return await manage_providers(app, agent, session, create=action == "provider_add")
    if action == "probe":
        return await _probe(app, agent, session)
    return ""


# LLM: 模式展示同时保留配置值与当前总开关下的实际模式，未测试网络不能声称连接在线。
# 函数用途: 选择一个真正由当前作用域控制的接入点，再编辑单字段。
async def _edit_point(app, agent, session: str, view: dict) -> str:
    rows = []
    for point, name in _POINTS.items():
        if f"points.{point}.mode" not in _fields(view):
            continue
        row = view["effective"]["points"][point]
        connection = "配置可用；未测试网络" if row["connection"]["configured"] else "未绑定或配置不可用；仍可修改"
        rows.append((point, f"{name} · 配置{_MODES[row['mode']]} / 当前{_MODES[row['effective_mode']]} · {connection}"))
    point = await _choose(app, "决策接入点", rows)
    if point is None:
        return ""
    field = await _choose(app, _POINTS[point], [(f"points.{point}.{field}", _field_label(view, f"points.{point}.{field}")) for field in _POINT_FIELDS if f"points.{point}.{field}" in _fields(view)])
    return await _edit_field(app, agent, session, view, field) if field else ""


# LLM: 模型列表只读原脱敏目录的 decision 用途；不可用配置仍可绑定，保存验证和共享撤销仍由原服务执行。
# 函数用途: 选择已有决策模型，清空为显式未绑定，恢复继承另走 reset。
async def _profile_choices(app, agent, session: str, current: str):
    result = await _request(app, agent, session, "list")
    if not result.get("ok"):
        return None
    rows = [("", "不绑定模型（明确清空此字段）")]
    rows.extend((row["id"], f"{row['model_name']} · {'共享' if row.get('shared') else '私有'} · "
                 f"{'配置可用；未测网络' if 'decision' in row.get('available_for', []) else '配置不可用，仍可保存引用'}")
                for row in result.get("profiles", []) if row.get("capability") == "decision")
    if current and current not in {key for key, _ in rows}:
        rows.append((current, f"当前引用不可用：{current}（可清空或恢复继承）"))
    return RadioList(rows, default=current, select_on_focus=True)


# LLM: UI 校验只减少误输，原服务仍是严格有限正数的权威；不接受 NaN/Inf/零，不发网络探测。
# 函数用途: 把用户输入的秒数转换成可提交数字。
def _seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("请输入有限且大于 0 的秒数。")
    return value


# LLM: 列表编辑只解码为结构化数组再交共用校验；字符串、bool、数字不能被静默转成类别或模型候选。
# 函数用途: 将本次表单文本转为原设置字段值，非法输入留在表单且不发保存请求。
def _field_text_value(field: str, text: str):
    if field.endswith((".optional_categories", ".candidate_profile_ids")):
        return validate_decision_field(field, json.loads(text))
    return _seconds(text)


# LLM: 开关与上下文策略用单选，其余（秒数、JSON 数组）用单行文本；取值仍由 _field_text_value 与设置服务校验。
# 函数用途: 为非模式、非模型编号的字段生成编辑控件。
def _plain_control(field: str, value):
    if field in {"enabled", "experiment_enabled"} or field.endswith(".context_policy"):
        rows = ([(False, "关闭"), (True, "开启")] if field in {"enabled", "experiment_enabled"} else
                list(_CONTEXT_POLICIES.items()))
        return RadioList(rows, default=value, select_on_focus=True)
    return TextArea(text=json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value), height=1, multiline=False)


# LLM: 两个勾选项只是呈现，保存时换算回唯一的 mode 值：不勾"开启"=off，开启且勾"观察模式"=observe，开启不勾=apply。
#   新开启的点默认勾观察（先观察再正式使用）；关闭时观察勾选不参与保存。
# 函数用途: 生成点位"开启 / 观察模式"勾选框，并返回把当前勾选换算成模式值的函数。
def _mode_control(value: str):
    control = CheckboxList([("enabled", "开启"), ("observe", "观察模式（只记录建议，不采用）")])
    # 构造后再设勾选：若用 default_values，光标会停在第一个已勾项，空格就会先切掉"观察模式"而不是"开启"
    control.current_values = [key for key, on in (("enabled", value != "off"), ("observe", value != "apply")) if on]
    return control, lambda: "off" if "enabled" not in control.current_values else "observe" if "observe" in control.current_values else "apply"


# LLM: 每次只提交原字段/CAS；两个布尔开关都不构造授权信封，实验能力编辑必须说明当前联网不可用。
#   点位模式用"开启 / 观察模式"两个勾选项编辑，存储仍是原 off/observe/apply。
# 函数用途: 编辑普通开关、实验能力或点级配置，保存后读回，不触发实验或探测。
async def _edit_field(app, agent, session: str, view: dict, field: str) -> str:
    value = _value(view, field)
    mode_value = None
    if field.endswith("profile_id"):
        control = await _profile_choices(app, agent, session, value)
        if control is None:
            return "模型目录不可用；仍可返回修改开关或恢复继承。"
    elif field.endswith(".mode"):
        control, mode_value = _mode_control(value)
    else:
        control = _plain_control(field, value)
    notice = Label("这里只开启能力，不建立实验许可；当前联网实验不可用。" if field == "experiment_enabled" else
                   "候选请填原模型目录 ID 的 JSON 字符串数组；[] 表示全部当前授权生成模型。" if field.endswith(".candidate_profile_ids") else
                   "类别请填 JSON 字符串数组，如 [\"plugins\", \"自定义类别\"]；[] 不额外收起。" if field.endswith(".optional_categories") else
                   "减量可改变缓存前缀/schema；原搜索和权限不变，仅在开启并采用建议时生效。" if field.endswith(".context_policy") else
                   "空格或回车勾选；关=不调用，开+观察=只记录建议，开+不勾观察=正式使用（采用前仍会复核）。Tab 到保存。"
                   if field.endswith(".mode") else
                   "恢复继承会删除本范围覆盖；清空模型只表示不绑定。")
    while True:
        action = await _dialog(app, "编辑决策设置", HSplit([Label(_field_label(view, field)), control, notice]),
                               (("保存", "save"), ("恢复继承", "reset"), ("返回", None)), focus=control)
        if action is None:
            return ""
        if action == "reset":
            return await _save(app, agent, session, view, field, reset=True)
        try:
            chosen = (mode_value() if mode_value is not None else control.current_value if isinstance(control, RadioList)
                      else _field_text_value(field, control.text))
        except ValueError:
            notice.text = "请输入合法的 JSON 模型编号数组，修改尚未保存。" if field.endswith(".candidate_profile_ids") else "请输入 JSON 字符串数组，修改尚未保存。" if field.endswith(".optional_categories") else "请输入有限且大于 0 的秒数，修改尚未保存。"
            continue
        return await _save(app, agent, session, view, field, value=chosen)


# LLM: 原 CAS 同时比较 owner/thread；失败一律回到读取，不重新用新 revision 自动重放用户旧操作。
# 函数用途: 提交字段 patch 或恢复继承，成功只依据原设置回执。
async def _save(app, agent, session: str, view: dict, field: str, *, value=None, reset=False) -> str:
    decision = {"scope": view["scope"], "expected_revision": view["revision"], **({"fields": [field]} if reset else {"changes": {field: value}})}
    result = await _request(app, agent, session, "decision_reset" if reset else "decision_patch", {"decision": decision})
    if not result.get("ok"):
        return str(result.get("message") or "修改未确认。") + " 已返回重读，请核对后重新选择；未自动重放修改。"
    return "已恢复继承，以下为重新读取的生效值。" if reset else "已保存，以下为重新读取的生效值。"


# LLM: 测试必须由明确按钮触发，秒数只作用本次 probe；返回不含价格，输出 token 展示位置留白。
# 函数用途: 为已保存决策模型发一次限时原生测试，结果只在浮层显示，不启用设置或切换模型。
async def _probe(app, agent, session: str) -> str:
    choices = await _profile_choices(app, agent, session, "")
    if choices is None:
        return "无法读取决策模型目录。"
    selected = await _dialog(app, "决策连接测试 · 选择模型", choices,
                             (("选择模型", lambda: choices.current_value), ("返回", None)), focus=choices)
    if not selected:
        return "未选择决策模型，未发送请求。"
    seconds = TextArea(text="4.0", height=1, multiline=False)
    notice = Label("只有点击开始测试才发送一次决策请求，会产生少量用量；不启用设置、不切聊天模型。")
    while True:
        action = await _dialog(app, "主动测试决策连接", HSplit([Label(f"已选模型：{selected}"), Label("本次测试上限（秒）"), seconds, notice]),
                               (("开始测试", True), ("返回", None)), focus=seconds)
        if action is None:
            return ""
        try:
            timeout = _seconds(seconds.text)
        except ValueError:
            notice.text = "请选择已保存的决策模型，并输入有限正秒数。"
            continue
        result = await _request(app, agent, session, "decision_probe", {"profile_id": selected, "timeout_seconds": timeout})
        message = str(result.get("message") or "测试结果未知，请重读后再决定是否重试。")
        usage = result.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        input_text = "未知" if input_tokens is None else str(input_tokens)
        body = f"{message}\n模型：{result.get('model_name', '')}\n耗时：{result.get('elapsed_seconds', '?')} 秒\n错误类型：{result.get('error_type') or '无'}\n决策输入：{input_text}\n决策输出：\n"
        await _dialog(app, "决策连接测试结果", TextArea(text=body, read_only=True, height=8, width=72), (("返回", None),))
        return message
