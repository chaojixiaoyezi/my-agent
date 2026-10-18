# LLM: 内部C共用原CLI配置/根解析与认证门，只绑定真实lease；实际选中home在冷配置内显式冻结，不以配置或None Agent授业务权，修改须联查首次输入和原CLI构造。
# 模块用途: 在原Gateway生命周期建立不含Agent的控制依赖，业务构造仍由原P2许可后调用。
from __future__ import annotations

import copy
import os
import time

from ..agent.auth.configuration import validate_gateway_auth_configuration
from ..agent.gateway_parts.control_plane import GatewayControlPlane
from ..agent.gateway_parts.daemon_control import get_running_pid, write_pid_record
from ..agent.gateway_parts.http_service import GatewayLocalCredentialRequired
from ..agent.gateway_parts.local_client_token import LocalClientCredentialError
from ..agent.gateway_parts.paths import gateway_paths_from_root
from ..agent.user_space.home_layout import home_paths
from ..agent.user_space.home_root import ConfiguredHomeSnapshot, configured_home_root
from .common import agent_configuration
from .workspace_resolution import runtime_paths_for_config


# LLM: C配置/constructor roots沿原CLI同次解析并在原lease内核根；完整roots tuple供原launch运输，不靠home或后改args/config重建；原home typed快照及后续权限门保持，无业务Agent副作用。
# 函数用途: 为真实持锁C组装冷控制面，并保存稍后原P2许可才调用的业务factory。
def build_control_plane(args, paths, controller, factory):
    from ..agent.core import configure_canonical_background_contracts

    config, roots = agent_configuration(args)
    selected_home = home_paths(configured_home_root(config)).root
    config.my_agent_home = str(selected_home)
    config.host_home_snapshot = ConfiguredHomeSnapshot(selected_home, config.my_agent_home)
    actual = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    if actual.root.resolve() != paths.root.resolve():
        raise ValueError("gateway_root_changed")
    configure_canonical_background_contracts()
    frozen_args = copy.copy(args)
    frozen_args._gateway_frozen_config = copy.deepcopy(config)
    frozen_args.workspace_root = [str(root) for root in roots]
    return GatewayControlPlane(
        config, selected_home, paths,
        controller.count, controller.c_run_id, lambda: factory(frozen_args),
        agent_root=roots[0],
        workspace_roots=tuple(roots),
    )


# LLM: 认证部署与G1仍沿main唯一门，先于运行目录/state/恢复/HTTP；无Agent不跳过多owner隔离，不输出配置值或轮换坏凭据。
# 函数用途: 用冻结C配置检查原认证部署，再按原持锁来源准备本机凭据。
def preflight_control_credential(control):
    validate_gateway_auth_configuration(control.config)
    if not bool(getattr(control.config, "gateway_require_local_credential", False)):
        return
    try:
        control.prepare_local_credential()
    except LocalClientCredentialError as error:
        raise GatewayLocalCredentialRequired(error.reason_code) from error


# LLM: 原PID/state仅投影当前C，不覆盖角色未知作为授权；不消费restart/恢复processing或attempt，不物化owner，调用方须仍持原C租约并接完整收尾。
# LLM: 初始state也复用原C规范目标末门，不从可变paths.state取得写入位置；PID/请求目录仍沿原启动流程。
# 函数用途: 创建原C控制运行目录与PID状态，业务恢复明确延期；这些文件不证明ready或旧树退出。
def setup_control_run(context):
    from .gateway_coordinated_run import coordinated_state_path, publish_coordinated_state

    coordinated_state_path(context)
    paths = context.paths
    pid = os.getpid()
    existing = get_running_pid(paths.pid)
    if existing and existing != pid:
        raise RuntimeError(f"gateway already running pid={existing}")
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    write_pid_record(paths.pid)
    publish_coordinated_state(context, {"status": "starting", "pid": pid,
                                "gateway_workspace": str(paths.root), "subagent_workspace": "",
                                "owner_recovery_deferred": True, "owner_business_ready": False,
                                "requeued_requests": 0, "failed_processing_requests": 0,
                                "started_at": time.time()})
    print(f"[gateway-run] status=starting pid={pid}", flush=True)
    return 0, pid
