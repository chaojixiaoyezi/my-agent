# LLM: 语义记忆“向量模型”的唯一选择入口（S1/S2，用户 10-02 拍板）。embedding_model_profile 与 memory_semantic_recall 是全局
#   配置（owner 作用域 agent 原样继承 Gateway 配置），所以只有完整的本机管理员（local/main）能改；写入走参数中心 set_parameter，
#   包在 user_settings_write_scope 里（与管理员 /settings 同一边界授权），每个键各记一笔账；保存后重启 Gateway 才生效
#   （嵌入客户端在组合根构建，不热加载），回执照实说明。tool_vector_search_enabled 不跟着变。
#   模型自配（manage_models）另守结构化规则：目标档案在本 owner 自己的目录里（不是共享引用）、嵌入用途可用，且端点主机
#   （scheme+host+port，去掉账号口令）与本 owner 当前默认对话模型相同 → 直接设；主机不同 → 不写，返回 needs_user_choice，
#   请用户在 /model → 选择模型 → 向量模型 里自己选；其余一律 PARAMETER_BOUNDARY。不读自然语言，只读身份、目录与地址。
#   改动同步 test_embedding_selection、tui_model_menu、gateway_parts/model_profile_service 与 manage_models。
# 模块用途: 列出可选的向量模型，在管理员菜单（TUI/IM）与 my-agent 自配两条入口里写入或关闭语义记忆。
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from urllib.parse import urlsplit

from ..user_space.owner_access import is_complete_local_admin_owner
from .config_io import load_simple_yaml
from .model_profiles import (
    model_profiles_path,
    public_model_profiles,
    read_model_profiles,
    selected_model_config,
)
from .model_provider_schema import ModelProfileError
from .parameter_changes import ChangeOrigin, WritePaths, set_parameter, user_settings_write_scope
from .shared_model_catalog import shared_profile_key
from .user_config_capability import EFFECT_GATEWAY_RESTART, effect_text, user_config_path

# TUI 菜单经 Gateway / 本地模型配置入口发来的三个操作名。
EMBEDDING_OPERATIONS = frozenset({"embedding_list", "embedding_select", "embedding_off"})
_PROFILE_KEY, _RECALL_KEY = "embedding_model_profile", "memory_semantic_recall"
_DEFAULT_PORTS = {"https": 443, "http": 80}
_ADMIN_ONLY = "向量模型是全局设置（所有用户共用），只有管理员（本机主账号）能改。"
# 模型改目录会动到当前向量模型主机时的拒绝原因码（ModelProfileError.reason），manage_models 据此回 needs_user_choice。
EMBEDDING_HOST_CHANGE_REASON = "embedding_host_change"
_HOST_CHANGE_MESSAGE = ("这次修改会把语义记忆正在用（或已保存、等重启生效）的向量模型换到另一个服务商主机，等于把记忆发给另一家；"
                        "没有修改。请用户自己在 /model 里处理（管理员在菜单里亲手改不受这个限制）。")


# LLM: TUI 菜单的分派：list 只读；select/off 走管理员写入。未知操作抛 ModelProfileError（菜单按可预期错误显示）。
# 函数用途: 执行菜单发来的向量模型操作，返回给菜单显示的结构化结果。
def execute_embedding_operation(host: object, operation: str, payload: dict) -> dict:
    if operation == "embedding_list":
        return embedding_choices(host)
    if operation == "embedding_select":
        return apply_embedding_choice(host, str(payload.get("profile_id") or "").strip(), actor="chat")
    if operation == "embedding_off":
        return apply_embedding_choice(host, "", actor="chat")
    raise ModelProfileError("未知的向量模型操作。")


# LLM: 只列本 owner 自己目录里嵌入用途可用的档案（不含共享引用、不含接口地址和密钥）；保存值读用户配置文件，运行值读进程配置，
#   两者不同说明还没重启生效。只读，不发请求。
# 函数用途: 返回向量模型的可选项、当前保存值、当前运行值和是否等待重启。
def embedding_choices(host: object) -> dict:
    listing = public_model_profiles(read_model_profiles(model_profiles_path(host.home_paths)), host.config)
    choices = [{"id": row["id"], "model_name": row["model_name"], "provider_name": row.get("provider_name", "")}
               for row in listing["profiles"] if row["id"] != "default" and "embedding" in (row.get("available_for") or [])]
    saved, running = _saved_values(host.config), _running_values(host.config)
    return {"ok": True, "choices": choices, "saved": saved, "running": running, "restart_pending": saved != running,
            "can_change": is_complete_local_admin_owner(host.home_paths), "message": _status_text(saved, running)}


# LLM: 给 manage_models list 附带的语义记忆视图（只给本机管理员；普通 owner 改不了全局设置，返回 None 不引导）：运行值、保存值、
#   是否等待重启、可选嵌入档案（编号+模型名），以及按这些结构化事实选出的下一步提示。提示只是给模型的软引导，裁决仍在
#   model_set_embedding。只读，不发请求，不含接口地址和密钥。
# 函数用途: 让模型在列出模型时就看到语义记忆现在开没开、能选哪个、下一步调什么。
def semantic_memory_view(host: object) -> dict | None:
    listing = embedding_choices(host)
    if not listing["can_change"]:
        return None
    choices = [{"id": row["id"], "model_name": row["model_name"]} for row in listing["choices"]]
    return {"running": listing["running"], "saved": listing["saved"], "restart_pending": listing["restart_pending"],
            "choices": choices, "hint": _view_hint(listing["saved"], choices)}


# 函数用途: 按保存值和可选项给出下一步：已开 → 怎么关；有可选项 → 直接 set_embedding；没有 → 先加一个嵌入模型。
def _view_hint(saved: dict, choices: list[dict]) -> str:
    if saved["semantic_recall"] and saved["profile_id"]:
        return "语义记忆已开（重启 Gateway 后以保存值为准）；用户要关闭时直接 action=disable_embedding。"
    if not choices:
        return ("还没有用途为 embedding 的模型：先用 add 或 save_model 加一个（capability=embedding，例如 MiniMax embo-01），"
                "再 set_embedding。")
    return ("用户要开语义记忆时直接 action=set_embedding，profile_id 取 choices 里的 id：用户的要求就是授权，不用再请用户确认，"
            "也不要让用户自己去 /model 选。管理员与服务商主机由宿主核对，只有回执是 needs_user_choice 时，才请用户在 "
            "/model → 选择模型 → 向量模型 里自己选。保存后重启 Gateway 生效。")


# LLM: 管理员入口（TUI 菜单、IM /model vector）与模型自配共用的写入：选中 → 先写档案再开召回；关闭 → 先关召回再清档案，
#   任一步失败都停在“不会把记忆发出去”的一侧。非管理员直接拒绝，不写任何文件。有写文件副作用（用户配置 + 修改账本）。
# 函数用途: 把向量模型设成某个档案（并打开语义记忆），或关闭语义记忆（profile_id 为空）。
def apply_embedding_choice(host: object, profile_id: str, *, actor: str) -> dict:
    if not is_complete_local_admin_owner(host.home_paths):
        return {"ok": False, "error_code": "PARAMETER_BOUNDARY", "message": _ADMIN_ONLY}
    model_name = _own_embedding_profile(host, profile_id)["model_name"] if profile_id else ""
    path = user_config_path(host.config)
    if path is None:
        raise ModelProfileError("当前没有加载用户配置文件，不能保存向量模型设置。")
    steps = ((_PROFILE_KEY, profile_id), (_RECALL_KEY, "true")) if profile_id else ((_RECALL_KEY, "false"), (_PROFILE_KEY, ""))
    origin = ChangeOrigin(actor=actor, reason="/model 向量模型" if actor != "model" else "manage_models 向量模型")
    with user_settings_write_scope():
        failure, change_ids = _write_steps(steps, WritePaths(user_path=path), origin)
    if failure is not None:
        return {"ok": False, "error_code": str(failure.get("code") or ""), "change_ids": change_ids,
                "message": str(failure.get("error") or "向量模型设置没有保存。")}
    done = f"向量模型已设为 {model_name}，语义记忆已打开。" if profile_id else "已关闭语义记忆（只按关键词召回），向量模型已清空。"
    return {"ok": True, "profile_id": profile_id, "model_name": model_name, "semantic_recall": bool(profile_id),
            "restart_required": True, "change_ids": change_ids, "message": done + effect_text(EFFECT_GATEWAY_RESTART)}


# LLM: 必须在 user_settings_write_scope 里调用（边界授权由调用方给）；按顺序逐项写，遇到第一个失败就停，返回失败回执和已成功的
#   账本编号。有写文件副作用（用户配置 + 修改账本）。
# 函数用途: 依次写入向量模型的两项设置。
def _write_steps(steps: tuple, paths: WritePaths, origin: ChangeOrigin) -> tuple[dict | None, list[str]]:
    change_ids: list[str] = []
    for key, value in steps:
        report = set_parameter(key, value, paths=paths, origin=origin)
        if not report.get("ok"):
            return report, change_ids
        change_ids.append(str(report["change_id"]))
    return None, change_ids


# LLM: 模型自配的结构化裁决（用户 10-02 拍板）：只给本机管理员；只认本人目录里的嵌入档案；端点主机与默认对话模型一致才写，
#   不一致返回 needs_user_choice、不写文件。主机按 scheme+host+port 比较（去掉账号口令、默认端口补齐），不看路径。
#   “默认对话模型”只认组合根启动时记下的快照（remember_startup_chat_host，be 复审 S1）：同一次运行里先改默认、再设向量会被拦；
#   没有快照或当时解析不出来（档案停用、被删等）时主机记为未知，同样请用户自己选，不当成目标档案的错误。
# 函数用途: manage_models 设置向量模型：同主机直接设，主机不同请用户在 /model 里自己选，其余拒绝。
def model_set_embedding(host: object, profile_id: str) -> dict:
    if not is_complete_local_admin_owner(host.home_paths):
        return _boundary(_ADMIN_ONLY + "my-agent 只能替本机管理员设置。")
    target = _own_embedding_profile(host, profile_id)
    chat_host = str(getattr(host, "embedding_chat_host_snapshot", "") or "")
    if not target["host"] or target["host"] != chat_host:
        return {"ok": False, "status": "needs_user_choice", "error_code": "EMBEDDING_HOST_DIFFERS",
                "message": (f"这个向量模型的服务商主机（{target['host'] or '未知'}）和当前默认对话模型（{chat_host or '未知'}）"
                            "不同，等于把记忆发给另一家服务商；没有修改。请用户自己在 /model → 选择模型 → 向量模型 里选。")}
    return apply_embedding_choice(host, profile_id, actor="model")


# LLM: 关闭只会减少数据外发，但仍是全局配置，所以同样只给本机管理员。
# 函数用途: manage_models 关闭语义记忆。
def model_disable_embedding(host: object) -> dict:
    if not is_complete_local_admin_owner(host.home_paths):
        return _boundary(_ADMIN_ONLY)
    return apply_embedding_choice(host, "", actor="model")


# LLM: 共享引用（别人共享来的档案）和不在本人目录里的编号一律拒绝；能力、启用与凭据由 selected_model_config 按 embedding 用途校验，
#   失败抛 ModelProfileError（原因码在 reason 里）。只读目录，不发请求，不回传密钥。
# 函数用途: 确认一个档案是本人目录里可用的嵌入模型，返回模型名和端点主机。
def _own_embedding_profile(host: object, profile_id: str) -> dict:
    data = read_model_profiles(model_profiles_path(host.home_paths))
    if not profile_id or shared_profile_key(profile_id) or profile_id not in data["profiles"]:
        raise ModelProfileError("只能选你自己目录里的嵌入模型（别人共享来的不行）。", reason="profile_not_found")
    config = selected_model_config(host, profile_id=profile_id, capability="embedding")
    return {"model_name": str(getattr(config, "model_name", "") or ""),
            "host": _endpoint_host(str(getattr(config, "api_base", "") or ""))}


# LLM: 组合根（core._wire_memory_authorities）建 agent 时调用一次，和嵌入客户端同一时刻定下：记下当时本 owner 默认对话模型的
#   端点主机，写在 agent.embedding_chat_host_snapshot 上，model_set_embedding 只比对它。读不出来记空串（之后设向量一律请用户
#   自己选）。只读目录，不发请求；进程内不再刷新，重启才更新。
# 函数用途: 记下启动时默认对话模型的端点主机，供 my-agent 自配向量模型时比对。
def remember_startup_chat_host(agent: object) -> None:
    try:
        chat_host = _default_chat_host(agent)
    except (OSError, ValueError):
        chat_host = ""
    agent.embedding_chat_host_snapshot = chat_host


# LLM: manage_models 写模型目录时的写前检查（be 复审 M1，3a 定做法 a）：当前向量档案——用户配置里的保存值和进程里的运行值都算——
#   写前、写后解析出的端点主机只要不一样（被删、挪到别的服务商、服务商地址改了、或从无到有），就抛
#   ModelProfileError(reason=EMBEDDING_HOST_CHANGE_REASON)，目录不落盘；工具映射成 needs_user_choice。只读用户配置。
# 函数用途: 生成“不许模型把向量模型悄悄换到别的服务商主机”的目录写前检查，交给 model_profiles.model_profile_write_check。
def catalog_write_check(host: object) -> Callable[[dict, dict], None]:
    ids = (_saved_values(host.config)["profile_id"], _running_values(host.config)["profile_id"])
    return partial(_check_embedding_hosts, frozenset(value for value in ids if value))


# 函数用途: 比较写前写后受保护档案的端点主机，有任何一个变了就拒绝。
def _check_embedding_hosts(protected: frozenset, before: dict, after: dict) -> None:
    if any(_catalog_host(before, profile_id) != _catalog_host(after, profile_id) for profile_id in protected):
        raise ModelProfileError(_HOST_CHANGE_MESSAGE, reason=EMBEDDING_HOST_CHANGE_REASON)


# 函数用途: 按目录快照算一个本人档案的端点主机（档案或服务商不在就是空串）；不解析共享引用。
def _catalog_host(data: dict, profile_id: str) -> str:
    model = data.get("profiles", {}).get(profile_id)
    provider = data.get("providers", {}).get(model.get("provider_id")) if isinstance(model, dict) else None
    return _endpoint_host(str(provider.get("api_base") or "")) if isinstance(provider, dict) else ""


# 函数用途: 取本 owner 默认对话模型（新会话默认）的端点主机；解析不出返回空串。只读目录。
def _default_chat_host(host: object) -> str:
    try:
        config = selected_model_config(host)
    except ModelProfileError:
        return ""
    return _endpoint_host(str(getattr(config, "api_base", "") or ""))


# 函数用途: 端点主机 = 小写 scheme://主机:端口（去掉账号口令、补齐默认端口）；解析不出返回空串。
def _endpoint_host(api_base: str) -> str:
    try:
        parts = urlsplit(api_base.strip())
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    except ValueError:
        return ""
    if not (parts.scheme and parts.hostname and port):
        return ""
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}:{port}"


# 函数用途: 读用户配置文件里保存的两项（没有覆盖按默认：空档案、关）。
def _saved_values(config: object) -> dict:
    path = user_config_path(config)
    stored = load_simple_yaml(path) if path is not None and path.is_file() else {}
    profile = stored.get(_PROFILE_KEY)
    recall = stored.get(_RECALL_KEY)
    return {"profile_id": "" if profile in (None, []) else str(profile).strip(),
            "semantic_recall": str(recall).strip().lower() in {"true", "1", "yes", "on"} if recall not in (None, []) else False}


# 函数用途: 读当前进程加载的两项。
def _running_values(config: object) -> dict:
    return {"profile_id": str(getattr(config, _PROFILE_KEY, "") or "").strip(),
            "semantic_recall": bool(getattr(config, _RECALL_KEY, False))}


# 函数用途: 生成“现在用哪个、保存了哪个、要不要重启”的一句说明。
def _status_text(saved: dict, running: dict) -> str:
    def describe(values: dict) -> str:
        return f"档案 {values['profile_id']}，语义记忆开" if values["semantic_recall"] and values["profile_id"] else "关闭（只按关键词）"

    text = f"当前运行：{describe(running)}。"
    return text if saved == running else text + f"已保存、重启 Gateway 后生效：{describe(saved)}。"


# 函数用途: 返回统一的“安全边界，不能由模型修改”结果。
def _boundary(message: str) -> dict:
    return {"ok": False, "error_code": "PARAMETER_BOUNDARY", "message": message}


__all__ = ["EMBEDDING_HOST_CHANGE_REASON", "EMBEDDING_OPERATIONS", "apply_embedding_choice", "catalog_write_check",
           "embedding_choices", "execute_embedding_operation", "model_disable_embedding", "model_set_embedding",
           "remember_startup_chat_host", "semantic_memory_view"]
