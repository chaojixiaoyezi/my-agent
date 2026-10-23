# LLM: 只保存已解析合法的 effort set；复用原群权限与线程锁，禁止探测、网络、加载业务 Agent。
# 模块用途: 为 Count0 与 C 提供同一短档位事务和真实持久回执。
from __future__ import annotations

import time

from ..conversation.control_commands import ConversationControlResult
from ..settings.reasoning_effort import set_thread_reasoning_level
from ..user_space.group_authorization_context import group_capability_guard


# LLM: C 调用方持原 source 门；Count0 同样走群策略锁及原线程 CAS，失败不返回已保存。
# 函数用途: 保存或清除本会话档位，回执只说待下次请求采用，不等探测。
def save_effort(host, thread, command, *, source=None):
    # LLM: 所有实际写均在同一来源生命周期内，返回 canonical 原子更新结果，不用旧读覆盖其它字段。
    # 函数用途: 按原权限保存线程档位。
    def persist():
        with group_capability_guard(host, "assistant.configure", thread_id=thread.thread_id, blocking=False):
            return set_thread_reasoning_level(host.conversation_store, thread.thread_id,
                                              "" if command.value == "default" else command.value, blocking=False)

    changed = persist() if source is None else source.perform(persist)
    return ConversationControlResult("effort", True, _saved_message(host, changed),
        details={"thread_id": changed.thread_id, "reasoning_effort": changed.reasoning_effort,
                 "saved_at": time.time(), "adoption_status": "pending"})


# LLM: 仅从已保存线程和档案配置解释换算，无 backend 构造或探测；说明是下次请求的预期字段，不冒充已采用。
# 函数用途: 保留档位及供应商映射回显，并明确当前响应保持冻结。
def _saved_message(host, changed):
    from ..backends.reasoning_control import LEVEL_LABELS, describe_config_reasoning_effect
    from ..settings.model_profiles import selected_model_config
    from ..settings.reasoning_effort import configured_reasoning_level

    level = changed.reasoning_effort or configured_reasoning_level(host.config)
    source = "本会话设置" if changed.reasoning_effort else "全局默认"
    lines = [f"本会话智能程度：{LEVEL_LABELS[level]}（{source}）。已保存，当前响应和已开始的工具保持不变，下次请求采用。"]
    try:
        config = selected_model_config(host, profile_id=changed.model_profile_id)
        lines.append(f"当前模型 {config.model_name}：{describe_config_reasoning_effect(level, config)}")
    except Exception:
        lines.append("当前会话还没有可用的模型；设置已保存，选择模型后生效。")
    return "\n".join(lines)


# LLM: 菜单的档案与版本条件必须随原收据进入同一 settings CAS；普通文字选择维持无条件最新写语义。
# 函数用途: 校验原菜单完整输入，不把无效版本或档案当成普通选择。
def model_selection_payload(scope, command, row):
    from ..settings.model_profiles import ModelProfileError

    supplied = (getattr(scope, "metadata", None) or {}).get("model_payload")
    if supplied is None:
        return {"profile_id": row["id"]}
    if (command.operation != "select" or not isinstance(supplied, dict)
            or set(supplied) - {"profile_id", "expected_revision"}
            or supplied.get("profile_id") != row["id"]):
        raise ModelProfileError("模型选择输入无效，请重新打开菜单。")
    revision = supplied.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ModelProfileError("模型选择版本无效，请重新打开菜单。")
    return {"profile_id": row["id"], "expected_revision": revision}
