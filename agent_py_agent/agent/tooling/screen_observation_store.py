# LLM: 适配器进程内的窗口实例登记与快照环（J16 片 B 复核的"当时"一侧）。boot 每个进程随机，窗口原生 ID（XID / 窗口号）在上次列表里
#   不存在、这次出现就算新实例（ID 复用也算新）；每个实例只留最近 SNAPSHOT_RETAIN_COUNT 代快照，只存几何、可见事实和逐候选的
#   {region, grid}，不存整幅图；跟踪窗口数超过 TRACKED_WINDOWS_MAX_COUNT 按最近采样淘汰，列表里消失的窗口连快照一起删。
#   这里不做任何 X11 / 系统调用，也不判定过期：找快照只按 (ref, generation) 精确命中，是否仍有效由 screen_observation 逐项复核。
# 模块用途: 记住"上次观察时窗口长什么样"，供动作前复核对照；纯内存、无副作用，可用假数据单测。
from __future__ import annotations

import secrets
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field

# 每个窗口实例保留的最近快照代数：宿主只放行最新观察，多留几代是为了并发的几个 run 都还能找到自己那一代
SNAPSHOT_RETAIN_COUNT = 4
# 同时跟踪的窗口实例上限，超过按最近一次采样淘汰
TRACKED_WINDOWS_MAX_COUNT = 64
# 适配器启动时生成的 boot 随机串字节数（十六进制后 16 位）
BOOT_TOKEN_BYTES = 8


# 类用途: 一个窗口在采样时的几何：全局原点（点）、尺寸（点）、每点像素数（缩放）。
@dataclass(frozen=True)
class WindowGeometry:
    origin: tuple[int, int]
    size: tuple[int, int]
    scale: tuple[float, float]

    # 函数用途: 观察载荷 frame 里的几何部分。
    def frame_fields(self) -> dict[str, list[float]]:
        return {"origin": list(self.origin), "size": list(self.size), "scale": list(self.scale)}


# 类用途: 一个候选在快照里的事实：提供方 key、截图像素外框、量化灰度格（screen_region_digest.region_grid）。
@dataclass(frozen=True)
class CandidateSnapshot:
    key: str
    region: tuple[int, int, int, int]
    grid: bytes


# LLM: generation 形如 <boot>-<instance>-<seq>，ref 形如 win:<boot>:<instance>；native_id 只在适配器内部用来重新找到窗口，
#   不进任何对外载荷。
# 类用途: 一次采样留下的完整快照。
@dataclass(frozen=True)
class WindowSnapshot:
    ref: str
    generation: str
    native_id: object
    captured_at: float
    geometry: WindowGeometry
    desktop: object
    occluded: bool
    candidates: dict[str, CandidateSnapshot] = field(default_factory=dict)


# LLM: 实例号单调递增、永不复用；observe_listing 必须每次采样前用完整的当前窗口列表调用，缺席的 ID 立即遗忘。
# 类用途: 给窗口原生 ID 配稳定的实例身份（boot + 实例号）。
class WindowInstanceRegistry:
    def __init__(self, boot: str | None = None) -> None:
        self.boot = boot or secrets.token_hex(BOOT_TOKEN_BYTES)
        self._instances: dict[object, int] = {}
        self._next_instance = 1

    # 函数用途: 用当前窗口列表更新登记：新出现的 ID 发新实例号，消失的 ID 遗忘；返回被遗忘的 ref 列表。
    def observe_listing(self, native_ids: Iterable[object]) -> list[str]:
        present = list(dict.fromkeys(native_ids))
        gone = [self.ref_for(native_id) for native_id in self._instances if native_id not in present]
        self._instances = {native_id: self._instances.get(native_id) or self._allocate() for native_id in present}
        return gone

    # 函数用途: 已登记窗口的实例号，没登记返回 None。
    def instance_of(self, native_id: object) -> int | None:
        return self._instances.get(native_id)

    # 函数用途: 已登记窗口的稳定引用 win:<boot>:<instance>；未登记抛 KeyError。
    def ref_for(self, native_id: object) -> str:
        return f"win:{self.boot}:{self._instances[native_id]}"

    # 函数用途: 按引用反查原生 ID，找不到返回 None。
    def native_for(self, ref: str) -> object | None:
        return next((native_id for native_id in self._instances if self.ref_for(native_id) == ref), None)

    # 函数用途: 发下一个实例号。
    def _allocate(self) -> int:
        instance, self._next_instance = self._next_instance, self._next_instance + 1
        return instance


# LLM: 快照只按 (ref, generation) 精确命中；环按 ref 保序（OrderedDict），record 把该 ref 挪到最新并在超量时淘汰最老的；
#   forget 由采样方在 observe_listing 之后调用，把已消失窗口的环删掉。
# 类用途: 每个窗口实例的最近几代快照。
class SnapshotStore:
    def __init__(self) -> None:
        self._rings: OrderedDict[str, deque[WindowSnapshot]] = OrderedDict()
        self._sequence: dict[str, int] = {}

    # 函数用途: 给某个窗口实例发下一代代次号 <boot>-<instance>-<seq>。
    def next_generation(self, ref: str) -> str:
        self._sequence[ref] = self._sequence.get(ref, 0) + 1
        return f"{ref.removeprefix('win:').replace(':', '-')}-{self._sequence[ref]}"

    # 函数用途: 存一代快照（有副作用：淘汰最老的窗口环）。
    def record(self, snapshot: WindowSnapshot) -> None:
        ring = self._rings.pop(snapshot.ref, None) or deque(maxlen=SNAPSHOT_RETAIN_COUNT)
        ring.append(snapshot)
        self._rings[snapshot.ref] = ring
        while len(self._rings) > TRACKED_WINDOWS_MAX_COUNT:
            oldest, _ = self._rings.popitem(last=False)
            self._sequence.pop(oldest, None)

    # 函数用途: 按引用和代次精确找快照，找不到返回 None。
    def find(self, ref: str, generation: str) -> WindowSnapshot | None:
        return next((item for item in self._rings.get(ref, ()) if item.generation == generation), None)

    # 函数用途: 删掉已消失窗口的环与代次计数。
    def forget(self, refs: Iterable[str]) -> None:
        for ref in refs:
            self._rings.pop(ref, None)
            self._sequence.pop(ref, None)

    # 函数用途: 当前跟踪的窗口引用（最旧在前）。
    def tracked(self) -> list[str]:
        return list(self._rings)


__all__ = [
    "BOOT_TOKEN_BYTES", "SNAPSHOT_RETAIN_COUNT", "TRACKED_WINDOWS_MAX_COUNT", "CandidateSnapshot", "SnapshotStore",
    "WindowGeometry", "WindowInstanceRegistry", "WindowSnapshot",
]
