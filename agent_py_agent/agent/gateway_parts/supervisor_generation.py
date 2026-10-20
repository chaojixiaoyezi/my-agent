# LLM: 监护只消费原P4/P6与真实出生，不持C/owner执行权；完整旧代关闭前不得新spawn，同C不换slot，Count0沿原门。
# 模块用途: 为原监护健康和整代恢复提供有界只读核验，不写账、不造controller或关闭证明。
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..settings.gateway_topology import validate_gateway_worker_process_count
from .daemon_metadata import process_identity_is_live
from .io import read_json_file_report
from .multiprocess_bootstrap_publish import read_worker_registry
from .multiprocess_bootstrap_ready import require_current_worker
from .multiprocess_generation_metadata import (
    generation_role_files,
    generation_source_files,
    read_generation_metadata,
    require_closed_generation,
)
from .multiprocess_records import deserialize_record, serialize_record
from .multiprocess_route import WorkerPartition


# LLM: 仅本次恢复内存中的完整原字节和配置窗口，不是持久权威、P2或原启动配置历史。
# 类用途: 保留停止前的准确旧代和配置，防止恢复时串到另一代。
@dataclass(frozen=True)
class ObservedGeneration:
    registry: bytes
    config: bytes
    count: int


# LLM: 正式Agent配置是唯一N来源；缺失/错误不降为Count0，测试替身须明确配置真实N。
# 函数用途: 读取并校验本监护对象的既有拓扑配置。
def configured_count(supervisor):
    return validate_gateway_worker_process_count(supervisor._agent.config.gateway_worker_process_count)


# LLM: 只读同选定配置原字节，拒链接/消失/超限；此摘要只约束本次恢复窗口，不推断旧C启动时配置。
# 函数用途: 捕获原公开启动入口即将读取的配置。
def _configuration_bytes(supervisor):
    path = Path(supervisor.config_path).expanduser().absolute()
    content = read_bytes_beneath(path.parent, (path.name,), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    if content is None:
        raise ValueError('supervisor_config_unconfirmed')
    return content


# LLM: 复用原YAML/runtime层解析和home优先级，按实际spawn env校当前N/home/规范Gateway根；raw首末不同即拒，不反推旧启动hash。
# 函数用途: 防止缓存Agent配置与公开子CLI将读取的真实配置分叉。
def current_configuration(supervisor):
    import os

    from ..settings import load_config
    from ..settings.services.runtime_config_env import apply_runtime_config_environment
    from ..user_space.home_layout import home_paths
    from ..user_space.home_root import configured_home_root
    from ..user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from ..user_space.runtime_paths import runtime_paths_for_agent

    raw = _configuration_bytes(supervisor)
    path = Path(supervisor.config_path).expanduser().resolve()
    home = Path(supervisor._agent.home_paths.root).resolve()
    environment = {**os.environ, 'MY_AGENT_HOME': str(home), 'MY_AGENT_CONFIG': str(path)}
    config = apply_runtime_config_environment(load_config(path), env=environment)
    if validate_gateway_worker_process_count(config.gateway_worker_process_count) != configured_count(supervisor):
        raise ValueError('supervisor_current_configuration_count_changed')
    base = home_paths(configured_home_root(config, env=environment))
    if base.root.resolve() != home:
        raise ValueError('supervisor_current_configuration_home_changed')
    # 与原CLI复用同一纯路径权威；显式已选home只反映spawn环境，不改全局环境或构造C快照/迁移许可。
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    scoped = home_paths_with_owner(base, owner)
    gateway = runtime_paths_for_agent(config, owner.home_dir, scoped)['gateway_workspace']
    if gateway.resolve() != supervisor._paths.root.resolve() or _configuration_bytes(supervisor) != raw:
        raise ValueError('supervisor_current_configuration_source_changed')
    return config, raw


# LLM: P4必须全N、同C真实出生/代次，绝不把部分登记或PID-only资料当健康/可恢复。
# 函数用途: 核本次C状态与原worker登记的静态绑定。
def _bound_registry(supervisor, state, count):
    registry = read_worker_registry(supervisor._paths.root)
    identity = state.get('process_identity')
    if (registry is None or type(identity) is not dict
            or registry['process_identity'] != {'schema_version': 'multiprocess_process_identity.v1', **identity}
            or state.get('pid') != identity.get('pid') or state.get('c_run_id') != registry['c_run_id']
            or type(state.get('worker_count')) is not int or state['worker_count'] != count
            or registry['worker_count'] != count
            or {row['worker'] for row in registry['workers']} != set(range(count))):
        raise ValueError('supervisor_generation_unconfirmed')
    return registry


# LLM: 当前C已由调用方验原PID/state/dispatcher；这里只复用原15s/ready/端口和每个真实W出生，首末同C/P4不写记录。
# 函数用途: 防止C仍running时死W或旧实例被监护判为健康。
def require_healthy_workers(supervisor, state, now):
    count = configured_count(supervisor)
    if count == 0:
        return
    registry = _bound_registry(supervisor, state, count)
    for row in registry['workers']:
        content = read_bytes_beneath(supervisor._paths.root, ('workers', f"w{row['worker']}.json"),
                                     max_bytes=16384, require_dir_fd=True, snapshot_retries=2)
        heartbeat = deserialize_record(content) if content is not None else None
        require_current_worker(registry, heartbeat, WorkerPartition(row['worker'], count, registry['c_run_id']), now)
        if process_identity_is_live(row['process_identity']) is not True:
            raise ValueError('supervisor_worker_birth_unconfirmed')
    if (read_worker_registry(supervisor._paths.root) != registry or _state(supervisor) != state
            or process_identity_is_live(registry['process_identity']) is not True):
        raise ValueError('supervisor_generation_changed')


# LLM: 原state reader的错误不可折叠为空；不删除旧记录，不收编任意dict或未知身份。
# 函数用途: 读本监护路径的规范Gateway状态。
def _state(supervisor):
    report = read_json_file_report(supervisor._paths.state, context='gateway.supervisor.generation.read')
    if report.load_error or type(report.payload) is not dict:
        raise ValueError('supervisor_generation_state_unconfirmed')
    return report.payload


# LLM: 捕获原完整P4/同C和配置窗口，C活/死/未知的停止判断仍由原精确stop入口决定；缺代次不猜。
# 函数用途: 为正N恢复保存停止前的原代次，Count0不增加新门。
def capture_generation(supervisor):
    count = configured_count(supervisor)
    if count == 0:
        return None
    state = _state(supervisor)
    registry = _bound_registry(supervisor, state, count)
    _config, raw = current_configuration(supervisor)
    observed = ObservedGeneration(serialize_record(registry), raw, count)
    require_unchanged_configuration(supervisor, observed)
    if _state(supervisor) != state or read_worker_registry(supervisor._paths.root) != registry:
        raise ValueError('supervisor_generation_changed')
    return observed


# LLM: 只核本次冻结N/raw；不宣称原C启动confighash可知，也不把配置相等当执行或死亡授权。
# 函数用途: 在停止/新启动前拒绝配置和拓扑变化。
def require_unchanged_configuration(supervisor, observed):
    _config, raw = current_configuration(supervisor)
    if configured_count(supervisor) != observed.count or raw != observed.config:
        raise ValueError('supervisor_configuration_changed')


# LLM: 原P6 reader验整树证据/真实物理死亡；另要求停后全source/role摘要精确相等，不借current额外进展语义吞新责任。
# 函数用途: 只有准确旧代已完整封存且首末不漂移，才允许原公开入口尝试下一代。
def require_closed_observation(supervisor, observed):
    require_unchanged_configuration(supervisor, observed)
    registry = deserialize_record(observed.registry)
    root, paths = supervisor._paths.root, supervisor._paths
    if read_worker_registry(root) != registry:
        raise ValueError('supervisor_generation_changed')
    before = generation_source_files(paths), generation_role_files(root, observed.count)
    history = require_closed_generation(paths, registry['c_run_id'], observed.count, registry['c_run_id'])
    closure = history[0]['closure']
    if closure['registry'] != registry or _digests(before[0]) != closure['source_files']:
        raise ValueError('supervisor_closed_sources_unconfirmed')
    if _digests(before[1]) != closure['role_markers']:
        raise ValueError('supervisor_closed_roles_unconfirmed')
    state = _state(supervisor)
    if (state.get('c_run_id') != registry['c_run_id'] or state.get('status') != 'stopped'
            or state.get('drain_complete') is not True):
        raise ValueError('supervisor_drain_unconfirmed')
    after = generation_source_files(paths), generation_role_files(root, observed.count)
    if (before != after or read_worker_registry(root) != registry
            or read_generation_metadata(root, registry['c_run_id']) != history):
        raise ValueError('supervisor_generation_changed')
    require_unchanged_configuration(supervisor, observed)


# LLM: 所有原完整字节均入摘要，不投影动态字段或把缺源当终态。
# 函数用途: 对比原P6封存清单的完整来源。
def _digests(files):
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


# LLM: 冷supervisor也不能绕过旧代P6；真正全新命名空间才可无旧closure，已有Count0/未知state不能冒充空槽。
# 函数用途: 在原公开spawn前核同路径没有未结清MP历史。
def require_startable_generation(supervisor):
    _config, _raw = current_configuration(supervisor)
    count = configured_count(supervisor)
    if count == 0:
        return
    if read_worker_registry(supervisor._paths.root) is None:
        if supervisor._paths.state.exists():
            _require_standalone_preflight(supervisor)
        return
    require_closed_observation(supervisor, capture_generation(supervisor))


# LLM: start已沿原完整健康门；这里额外拒同旧代/PID回执和配置窗口变化，不接管或停止未知新C。
# 函数用途: 确认原启动回执确实属于同N的新一代。
def require_successor(supervisor, observed, pid):
    require_unchanged_configuration(supervisor, observed)
    state = _state(supervisor)
    registry = _bound_registry(supervisor, state, observed.count)
    old = deserialize_record(observed.registry)
    if (state.get('pid') != pid or registry['c_run_id'] == old['c_run_id']
            or registry['process_identity'] == old['process_identity']):
        raise ValueError('supervisor_successor_unconfirmed')
    if not supervisor._is_gateway_healthy():
        raise ValueError('supervisor_successor_unconfirmed')


# LLM: 只保本S真实spawn的Popen及出生观察；不是PID重建对象，不授信号/业务/关闭权，缺出生仍保句柄供原child回收。
# 类用途: 防止启动等待返回后遗失直接子进程，Linux僵尸不能靠kill0判为仍运行。
@dataclass(frozen=True)
class SpawnedGateway:
    process: object
    identity: dict
    parent_pid: int


# LLM: 实际spawn后立即保原Popen；按配置N选原C producer同源birth（正N kernel/Count0原observer），ready不可晚补，超时仍保留。
# 函数用途: 保存本监护者创建的直接子进程，避免依赖subprocess全局隐式清理。
def retain_spawned_gateway(supervisor, process):
    import os
    import subprocess

    from .daemon_metadata import build_process_identity, process_host_id

    if type(process) is not subprocess.Popen:
        return
    child = SpawnedGateway(process, {}, os.getpid())
    supervisor._gateway_child = supervisor._gateway_child_origin = child
    if configured_count(supervisor) > 0:
        from .multiprocess_executor_identity import process_birth

        identity = {'host_id': process_host_id(), 'pid': process.pid, 'start_time': process_birth(process.pid)}
    else:
        identity = build_process_identity(process.pid)
    child = SpawnedGateway(process, identity, os.getpid())
    supervisor._gateway_child = supervisor._gateway_child_origin = child


# LLM: ready还须原Popen启动前已捕获birth与Gateway原PID记录一致；未知不补出生或把原Popen.pid直接当ready。
# 函数用途: 在健康回执返回前再次绑定准确原child。
def require_ready_child(supervisor, process, identity):
    import subprocess

    if type(process) is not subprocess.Popen:
        return
    child = getattr(supervisor, '_gateway_child', None)
    if (child is not getattr(supervisor, '_gateway_child_origin', None) or type(child) is not SpawnedGateway
            or child.process is not process or child.identity != identity or process.poll() is not None):
        raise ValueError('supervisor_original_child_unconfirmed')


# LLM: 只poll本S保留的准确原child；真实poll可reap自身child，但只有冻结birth相符且原身份已死才作target退出事实。
# 函数用途: 有界等本S启动的Gateway，外部C返回None继续原OS等待，不从PID构造或回收别代进程。
def wait_spawned_gateway_exit(supervisor, pid, identity, timeout):
    import os
    import subprocess
    import time

    child = getattr(supervisor, '_gateway_child', None)
    if child is None:
        return None
    if (child is not getattr(supervisor, '_gateway_child_origin', None) or type(child) is not SpawnedGateway
            or type(child.process) is not subprocess.Popen or child.parent_pid != os.getpid()
            or child.process.pid != pid or child.identity != identity):
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        code = child.process.poll()
        if type(code) is int and process_identity_is_live(identity) is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# LLM: 只从原PID记录绑定立即捕获的child；读取或出生未知返回未就绪，仍保原对象，不造ready回执。
# 函数用途: 在原等待预算中完成准确Gateway启动确认，避免等待循环内嵌身份处理。
def complete_gateway_start(supervisor, process, pid):
    from .daemon_control import read_pid_record_report

    record = read_pid_record_report(supervisor._paths.pid)
    value = record.payload or {}
    identity = {key: value.get(key) for key in ('host_id', 'pid', 'start_time')}
    try:
        if record.load_error:
            raise ValueError('supervisor_original_child_unconfirmed')
        require_ready_child(supervisor, process, identity)
    except ValueError:
        supervisor._log_error("Gateway原child出生未确认，保留句柄及记录。")
        return None
    supervisor._log_info(f"Gateway已就绪：pid={pid}")
    return pid


# LLM: 仅负向预核支持版Count0正常stop结构及无混MP的原strict来源；不核发variant/quiet/P2，真正死亡/native/SQL/同锁CAS仍由子C唯一capture。
# 函数用途: 让supervisor公开入口不永远阻断受支持的0到N启动，缺来源或混代仍零spawn。
def _require_standalone_preflight(supervisor):
    from ..common.strict_json import load_strict_json
    from .standalone_migration import _validate_stop
    from .standalone_migration_errors import StandaloneMigrationRejected
    from .standalone_sources import StandaloneSourceUnproven, _source_snapshot

    root = supervisor._paths.root
    try:
        before = _source_snapshot(root)
        if before[0] is None:
            raise ValueError('supervisor_standalone_stop_missing')
        # home已由当前配置门与实际spawn所用home核同；这里不再从变化的父环境重新选根。
        home = Path(supervisor._agent.home_paths.root).resolve()
        _validate_stop(load_strict_json(before[0]), root, home, Path(supervisor.config_path).resolve())
        if _source_snapshot(root) != before:
            raise ValueError('supervisor_standalone_source_changed')
    except (StandaloneMigrationRejected, StandaloneSourceUnproven) as error:
        raise ValueError('supervisor_standalone_preflight_unconfirmed') from error
