
# LLM: 能力配置的文件快照、agent 缓存读取和显式重载共用本模块；缓存须为 CapabilityConfig，失败不伪造配置。
#   agent 读取会缓存快照，显式重载可能更新 router.config；改动须复核编排、Compact 与运行配置测试。
# 模块用途: 读取并按文件版本重载能力配置，保持真实配置优先级，不让占位对象意外开启可选能力。

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import CapabilityConfig, load_capability_config
from .runtime_config_models import CapabilityConfigReloadResult, CapabilityConfigSnapshot


# LLM: capability 配置文件只有两个角色：随包默认（打包进发布物，只读）与用户位置
#   （<owner home>/config/capability_config.yaml，参数中心写入与展示的唯一目标）。
#   P18 验收发现旧实现把 <root>/agent_py_agent/config/capability_config.yaml 也当候选，
#   开发模式下 root 是仓库目录时它就是随包默认文件，参数中心可能写进随包默认；
#   生产 owner home 下参数中心又新建在旧第一候选，和文档 <owner home>/config/ 不一致，
#   且有人在 <owner home>/config/ 下放文件会被旧候选静默盖住。因此去掉候选逻辑。
# 函数用途: 返回唯一的用户 capability 配置位置（<root>/config/capability_config.yaml）。
def default_capability_config_path(root: str | Path) -> Path:
    return Path(root) / "config" / "capability_config.yaml"


# 函数用途: 返回随包默认 capability 配置文件（agent_py_agent/config/capability_config.yaml），只读永不被写。
def bundled_capability_config_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config" / "capability_config.yaml"


# LLM: 运行时“实际读哪一份”的统一答案：用户位置存在就用用户位置（写进去重启就能读到），
#   否则回落随包默认（只读、永不被写）。展示层 running_value 与运行时入口共用，
#   保证“默认值、运行值、写入目标”三者语义一致。
# 函数用途: 解析运行时实际读取的 capability 配置路径（用户位置优先，缺失时随包默认）。
def resolve_capability_config_path(user_path: Path | None = None, *, root: str | Path | None = None) -> Path:
    user = Path(user_path) if user_path is not None else (
        default_capability_config_path(root) if root is not None else None)
    if user is not None and user.exists():
        return user
    bundled = bundled_capability_config_path()
    return bundled if bundled.exists() else (user if user is not None else bundled)


# LLM: 参数中心写入口与展示层要写/读的就是这份文件：agent 上有 capability_config_path 用它的，否则按
#   agent.root 用 default_capability_config_path（与 capability_config_for_agent 同一逻辑），不能拿
#   agent_config 用户文件同目录猜——两者目录不同（owner home 与用户配置目录是两回事，2026-10-01 评审抓出）。
# 函数用途: 返回运行时实际读取的 capability 配置文件路径（按 agent 对象解析，没有 agent 信息时给默认路径）。
def capability_config_path_for(agent: object) -> Path:
    return Path(
        str(getattr(agent, "capability_config_path", "") or "")
        or default_capability_config_path(getattr(agent, "root", "."))
    )


# LLM: "从 agent 对象取 capability 配置"的唯一权威入口：优先 agent 上的运行时快照
#   _capability_config_runtime_snapshot，否则按 capability_config_path/默认路径加载并把
#   快照缓存回 agent（副作用）。缓存里不是 CapabilityConfig 的对象一律不认，
#   改走文件加载，防止非配置对象的属性被当成开关（2026-09-27 能力包合入时发现）。
#   用户位置不存在是常态（生产 Gateway 的 owner home 下通常没有这份配置）：回落读随包默认
#   （只读、不缓存），值与 dataclass 默认一致；这样“建好用户文件后下一次调用就能读到”，
#   不必重启，也保证展示层 running_value 与运行时看到同一组默认值。
#   只有读取失败或格式错误才返回 None，限流类调用方仍要自己落到默认值。
#   旧候选位置（<root>/agent_py_agent/config/capability_config.yaml）不再当用户配置读：
#   那里存在非随包默认文件时给结构化告警（配置告警里能看到），不静默合并。
#   新增运行时读 capability 配置的地方应一律走这里，不要从 agent.config 或 capability_router.config 上找。
# 函数用途: 运行时想读 capability_config.yaml 里的开关时，从 agent 拿配置对象；没有配置文件时给默认值。
def capability_config_for_agent(agent: object):
    snapshot = getattr(agent, "_capability_config_runtime_snapshot", None)
    config = getattr(snapshot, "config", None)
    # 只认真正的 CapabilityConfig：替身对象上自动生成的属性不是配置，不能让它的“真值”打开能力开关。
    if isinstance(config, CapabilityConfig):
        return config
    root = getattr(agent, "root", None)
    user_path = Path(
        str(getattr(agent, "capability_config_path", "") or "")
        or default_capability_config_path(root or ".")
    )
    if user_path.exists():
        try:
            snapshot = load_capability_config_snapshot(user_path)
        except (OSError, TypeError, ValueError):
            return None
        _attach_legacy_location_warning(snapshot.config, root)
        try:
            agent._capability_config_runtime_snapshot = snapshot
        except AttributeError:
            pass
        return snapshot.config
    # 用户位置缺失：读随包默认（只读、不缓存），之后建好用户文件时下一次调用就能读到。
    try:
        loaded = load_capability_config(bundled_capability_config_path())
    except (OSError, TypeError, ValueError):
        return CapabilityConfig()
    _attach_legacy_location_warning(loaded, root)
    return loaded


# LLM: 旧候选 <root>/agent_py_agent/config/capability_config.yaml 只在“不是随包默认文件”时告警：
#   开发模式下 root 是仓库目录，那里就是随包默认本身（路径或内容相同都算），属正常只读来源，不能误报。
# 函数用途: 旧候选位置存在非随包默认文件时给出结构化告警文字，否则返回 None。
def _legacy_location_warning(root: object) -> str | None:
    if root is None:
        return None
    legacy = Path(root) / "agent_py_agent" / "config" / "capability_config.yaml"
    bundled = bundled_capability_config_path()
    if not legacy.exists() or legacy.resolve() == bundled.resolve():
        return None
    try:
        if legacy.read_bytes() == bundled.read_bytes():
            return None
    except OSError:
        pass
    return (f"旧位置 {legacy} 有一份 capability 配置文件（不是随包默认），已不再读取；"
            f"用户 capability 配置统一放在 {Path(root) / 'config' / 'capability_config.yaml'}。")


# 函数用途: 把旧位置告警挂到加载出的 CapabilityConfig.config_warnings（配置告警展示能看到）。
def _attach_legacy_location_warning(config: CapabilityConfig, root: object) -> None:
    warning = _legacy_location_warning(root)
    if warning and warning not in (config.config_warnings or []):
        config.config_warnings = [*config.config_warnings, warning]


def capability_config_version(config_path: str | Path) -> str:
    path = Path(config_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


def load_capability_config_snapshot(config_path: str | Path) -> CapabilityConfigSnapshot:
    path = Path(config_path)
    stat = path.stat()
    return CapabilityConfigSnapshot(
        path=path,
        config=load_capability_config(path),
        version=capability_config_version(path),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def reload_capability_config_if_changed(
    snapshot: CapabilityConfigSnapshot,
    *,
    router: object | None = None,
) -> CapabilityConfigReloadResult:
    current_version = capability_config_version(snapshot.path)
    if current_version == snapshot.version:
        return CapabilityConfigReloadResult(snapshot=snapshot, changed=False, message="unchanged")
    reloaded = load_capability_config_snapshot(snapshot.path)
    if router is not None and hasattr(router, "config"):
        router.config = reloaded.config
    return CapabilityConfigReloadResult(snapshot=reloaded, changed=True, message="reloaded")
