"""公开状态页的轻量模型摘要；只读目录，不启动 Agent、后端或网络探针。"""

from __future__ import annotations

from types import SimpleNamespace

from ..settings.model_profiles import (
    _model_selection_projection,
    model_profiles_path,
    read_model_profiles,
)


def status_model_summary(server, *, trusted: bool = False) -> dict:
    """复用模型菜单的 available 判定；匿名请求只能看到配置是否可用。"""
    summary = {"model_configured": False, "default_model": ""}
    try:
        host = _configuration_host(server)
        if host is None:
            return {**summary, "model_summary_error": "MODEL_SUMMARY_SOURCE_UNAVAILABLE"}
        data = read_model_profiles(model_profiles_path(host.home_paths))
        # 空线程编号不会物化会话选择；共享/初始默认和凭据可用性仍由原公开投影决定。
        view = _model_selection_projection(host, data, "")
        if view["inventory_status"]["shared"] != "complete":
            return {**summary, "model_summary_error": "MODEL_SUMMARY_CATALOG_UNAVAILABLE"}
        selected = next((row for row in view["profiles"] if row["id"] == view["selected"]), None)
        if selected is not None:
            name = str(selected.get("model_name") or "")
            summary["model_configured"] = bool(name and selected.get("available", False))
            summary["default_model"] = name if trusted else ""
        return summary
    except Exception:  # noqa: BLE001 状态读取失败不能泄露凭据、路径或让公开入口返回500
        return {**summary, "model_configured": False, "default_model": "",
                "model_summary_error": "MODEL_SUMMARY_READ_FAILED"}


def _configuration_host(server):
    """只消费监听器原有依赖；C-only 用已注入配置和规范根解析同一 owner。"""
    control = getattr(server, "control_plane", None)
    if control is None:
        return getattr(server, "agent", None)
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )

    config = control.config
    home = home_paths(control.home_root)
    owner = resolve_owner_home(home.root, owner_identity_from_config(config))
    return SimpleNamespace(config=config, home_paths=home_paths_with_owner(home, owner))
