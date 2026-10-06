# LLM: 真实启用用例经原管理服务拿完整预览并回填授权身份；不替换准备/MCP，不更改产品默认或掩盖启动失败。
# 模块用途: 让老格式（v1–v6）可执行插件的组件验收沿新两步确认协议运行；夹具名带 legacy 防 v6/内容包用例误用，收紧默认的拒绝另由权限用例保护。

from dataclasses import replace

from agent_py_agent.agent.plugin_management import PluginManagement


# LLM: 仅给旧宽权限真实链路的测试使用；保留 owner/Store/身份/旧沙箱开关，不改 YAML 或共用 manager 默认。
# 函数用途: 显式选择测试的不受限模式，不能把这份夹具当成产品安全默认。
def unrestricted_service(service):
    return PluginManagement(replace(service.context, legacy_sandbox_default=False))


# LLM: 名字里的 legacy 说明只给老格式（v1–v6）真实链用；预览经真实 HostCommand/ToolExecutor，失败必须是确认门，不能把环境失败当预览。
# 函数用途: 取得下一次授权请求的完整事实，未确认时不会准备或启动插件。
def preview_legacy_enable(service, plugin_id, request_id="enable"):
    result = service.command(f"/plugins enable {plugin_id}", revision=service.catalog().revision, request_id=request_id)
    assert result["state"] == "failed" and result["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", result
    assert result["details"]["reason"] == "confirmation_required", result
    facts = result["details"]["confirmation"]
    assert facts["kind"] == "plugin_legacy_permissions"
    assert facts["authorization_id"].startswith("permit-")
    assert facts["confirm_command"] in result["message"]
    return result


# LLM: 名字里的 legacy 说明只给老格式（v1–v6）真实链用；命令/目录版本/请求编号均来自同次预览，不按插件名造授权、不只补 confirm_code、不直接绕到执行器。
# 函数用途: 用管理员最后预览的完整命令提交确认，原失败/未知原样返回，成功仍由各用例核验。
def confirm_legacy_enable(service, response):
    facts = response["details"]["confirmation"]
    return service.command(facts["confirm_command"], revision=facts["catalog_revision"], request_id=facts["authorization_id"])


# LLM: 老格式插件“启用后再测别的”用例的统一入口：先走真实预览拿完整确认事实，再用同次预览的确认命令提交，
#   断言最终 succeeded；额外参数（如 request_permission）原样透传给两步命令。不放宽任何原有断言，只把
#   “一条命令启用”换成两步确认协议（opp 两步确认是安全边界，测试不得绕过）。
# 函数用途: 以两步确认协议启用老格式插件，返回 (预览结果, 最终成功结果)，成功操作的编号是授权编号。
def enable_with_confirmation(service, plugin_id, request_id="enable", **kwargs):
    preview = service.command(f"/plugins enable {plugin_id}", revision=service.catalog().revision,
                              request_id=request_id, **kwargs)
    assert preview["state"] == "failed" and preview["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", preview
    assert preview["details"]["reason"] == "confirmation_required", preview
    facts = preview["details"]["confirmation"]
    result = service.command(facts["confirm_command"], revision=facts["catalog_revision"],
                             request_id=facts["authorization_id"], **kwargs)
    assert result["state"] == "succeeded", result
    return preview, result
