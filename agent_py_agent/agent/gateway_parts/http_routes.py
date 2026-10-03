# LLM: HTTP分发唯一结构化路由表；G2a观察复用template/access_tier，禁止并排维护计数白名单；原处理器授权/副作用保持。
# 模块用途: 声明Gateway实际路由与四档属性，使观察键有界且不含请求编号。
from __future__ import annotations

from dataclasses import dataclass


# LLM: template保留旧exact/prefix/suffix语义；handler_module为空调用handler方法，否则惰性导入原服务。
# 类用途: 保存一条实际分发路由及其观察档位。
@dataclass(frozen=True)
class GatewayHTTPRoute:
    template: str
    handler_name: str
    access_tier: str
    handler_module: str = ""


GATEWAY_HTTP_ROUTES = {
    "GET": (
        GatewayHTTPRoute("/status", "_handle_status", "public"),
        GatewayHTTPRoute("/result/*", "_handle_result", "credential"),
        GatewayHTTPRoute("/input-status/*", "_handle_input_status", "credential"),
        GatewayHTTPRoute("/control-status/*", "_handle_control_status", "credential"),
        GatewayHTTPRoute("/progress/*", "_handle_progress", "credential"),
        GatewayHTTPRoute("/sessions/*/channels", "_handle_session_channels", "admin"),
        GatewayHTTPRoute("/admin/summary", "_handle_admin_summary", "admin"),
        GatewayHTTPRoute("/metrics", "_handle_metrics", "public"),
    ),
    "POST": (
        GatewayHTTPRoute("/ask", "_handle_ask", "credential"),
        GatewayHTTPRoute("/control", "_handle_control", "credential"),
        GatewayHTTPRoute("/client/memory", "_handle_client_memory", "credential"),
        GatewayHTTPRoute("/client/models", "handle_client_models", "credential", ".model_profile_service"),
        GatewayHTTPRoute("/client/plugins", "handle_client_plugins", "credential", ".plugin_command_service"),
        GatewayHTTPRoute("/plugin-host/query", "handle_plugin_host_query", "plugin_token", "..plugin_host_api"),
        GatewayHTTPRoute("/client/plugin-panels", "handle_client_plugin_panels", "credential", ".plugin_panels_http"),
        GatewayHTTPRoute("/client/permissions", "handle_client_approval_mode", "credential", ".approval_mode_service"),
        GatewayHTTPRoute("/client/history", "_handle_client_history", "credential"),
        GatewayHTTPRoute("/client/display-page", "handle_client_display_page", "credential", ".display_archive_service"),
        GatewayHTTPRoute("/client/notices", "_handle_client_notices", "credential"),
        GatewayHTTPRoute("/client/agent-view", "_handle_client_agent_view", "credential"),
        GatewayHTTPRoute("/client/goal", "handle_client_goal", "credential", ".http_handlers"),
        GatewayHTTPRoute("/client/agent-guidance", "_handle_client_agent_guidance", "credential"),
        GatewayHTTPRoute("/client/agent-permission", "_handle_client_agent_permission", "credential"),
        GatewayHTTPRoute("/client/agent-stop", "_handle_client_agent_stop", "credential"),
        GatewayHTTPRoute("/stop", "_handle_stop", "admin"),
        GatewayHTTPRoute("/sessions/*/bind", "_handle_session_bind", "admin"),
    ),
}


# LLM: 不做URL归一化以免改变旧路由语义；返回同一个路由对象作为处理器和计数模板的来源。
# 函数用途: 按方法与原请求路径找到实际路由，未知路径返回None。
def match_gateway_http_route(method: str, path: str) -> GatewayHTTPRoute | None:
    return next((route for route in GATEWAY_HTTP_ROUTES.get(method, ()) if _route_matches(route.template, path)), None)


# LLM: 星号保留原中间/尾段匹配；模板不是原始请求路径，不携带请求编号。
# 函数用途: 复用原exact/prefix/suffix条件匹配路由。
def _route_matches(template: str, path: str) -> bool:
    prefix, wildcard, suffix = template.partition("*")
    return path.startswith(prefix) and path.endswith(suffix) if wildcard else path == template
