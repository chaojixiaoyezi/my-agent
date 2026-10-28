# LLM: 仅桥接当前owner原schedule服务；授权由门户capability和原工具owner/creator/群能力门共同核验，参数不得选择owner。
# 模块用途: 为Web提供认证通知目标及策略读写，不创建第二监控库、不停止或重执工作。
from __future__ import annotations

import json

from .errors import HostAPIError


# LLM: agent由组合根注入，不可从payload构造；缺准确owner运行时返回503，不能借main或其它用户执行器。
# 函数用途: 复用原schedule的结构化结果、CAS及安全错误码。
def _call(context, payload, agent, action):
    from ..scheduler.tool import ScheduleTool

    home = getattr(agent, "home_paths", None)
    if (home is None or home.owner_id != context.owner_home.owner_id
            or str(home.owner_home_dir) != str(context.owner_home.home_dir)):
        raise HostAPIError("HOST_NOT_READY", 503)
    params = {"action": action}
    if "monitor_id" in payload:
        params["job_id"] = payload["monitor_id"]
    for key in ("expected_version", "delivery_policy"):
        if key in payload:
            params[key] = payload[key]
    result = ScheduleTool(agent).execute(params)
    if not result.ok:
        raise HostAPIError(result.error_code or "HOST_NOT_READY", 403 if result.error_code in {
            "SCHEDULE_DELIVERY_TARGET_UNAUTHORIZED", "CHANNEL_ACTOR_SCOPE_MISMATCH", "CAPABILITY_DENIED"} else 409)
    return json.loads(result.output)


# LLM: 目录由当前owner真实绑定投影，不允许自行提交任意收件地址或选择别人的绑定。
# 函数用途: 列出可用的已认证通知路线。
def delivery_targets(context, payload, *, agent=None):
    return _call(context, payload, agent, "delivery_targets")


# LLM: get沿原工具只读owner授权，不写账或拉起新Agent，公开策略只有原delivery的投影别名。
# 函数用途: 读取一个原监控的通知策略和版本。
def get_delivery(context, payload, *, agent=None):
    return _call(context, payload, agent, "get")


# LLM: 更新沿原creator授权、群能力、expected_version和目标认证；只影响未来运行，不重放已冻结业务。
# 函数用途: 在原调度账本切换静默、本地、绑定或双线通知。
def update_delivery(context, payload, *, agent=None):
    return _call(context, payload, agent, "update_delivery")
