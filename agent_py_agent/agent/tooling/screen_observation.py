# LLM: 屏幕观察的来源无关核心（J16 片 B）：按后端给的窗口列表选目标、采样、OCR 候选、写快照、组装 plugin_observation.v1 载荷
#   （带几何扩展）；动作前按 ae 定稿做适配器层五项复核：按 _meta 代次找快照（找不到 / key 不在 → not_found）→ boot/instance →
#   可见、未最小化、同桌面 → 原点与尺寸相等 → 点击点不被遮挡（动作时重新查叠放）→ 重新截图的 scale 相等 → 候选区域摘要在容差内，
#   任一项不过 → stale，零副作用；
#   复核通过后立刻点击，中间不做任何别的 I/O。后端是鸭子类型（X11 / macOS 真后端 / 单测假后端），本模块不 import 任何桌面库。
#   截图由后端给 ScreenCapture（像素、采样方式、回退原因）；frame.scale 取截图自己的像素/点比（不取显示器的），复核时 scale 变了也判 stale。
#   错误码集合开放（宿主只提升 stale / not_found，其它原样透传）：window_not_found | not_viewable | capture_failed | ocr_failed | occluded |
#   not_found | stale | missing_context | invalid_arguments | cancelled | screen_recording_not_permitted；后端主动抛的 ObservationError 原样透传。
# 模块用途: "看一眼窗口、给出可点的候选、点之前再确认一遍没变"的全部判断逻辑，可在没有桌面的机器上用假后端完整测试。
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..plugin_observation import (
    MAX_CANDIDATE_COUNT,
    MAX_LABEL_CHARS,
    OBSERVATION_META_VERSION,
    OBSERVATION_SCHEMA,
)
from .screen_observation_store import (
    CandidateSnapshot,
    SnapshotStore,
    WindowGeometry,
    WindowInstanceRegistry,
    WindowSnapshot,
)
from .screen_region_digest import PixelBuffer, grid_unchanged, region_grid

# 观察载荷里的固定取值：坐标空间、目标类型、候选角色、候选动作工具名
OBSERVATION_SPACE = "screen_points"
WINDOW_TARGET_KIND = "window"
OCR_ROLE = "ocr_text"
CLICK_ACTION = "click_candidate"
# 截图采样方式（frame.capture）：单窗口内容（被压住的部分也拍得到）/ 按屏幕区域截屏（压在上面的窗口也会被拍进去）
WINDOW_IMAGE_CAPTURE = "window_image"
SCREEN_REGION_CAPTURE = "screen_region"


# LLM: code 是稳定机器码，进 structuredContent.my_agent_observation_error.code；message 是中文说明，不含路径、标题正文或坐标。
# 类用途: 观察或复核失败的结构化错误。
class ObservationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 由后端按叠放次序（底→顶）给出；native_id 只在适配器内部使用；normal 表示普通应用窗口（非 dock/桌面/工具条）。
# 类用途: 一个窗口的采样时事实。
@dataclass(frozen=True)
class WindowInfo:
    native_id: object
    title: str
    geometry: WindowGeometry
    viewable: bool
    hidden: bool
    desktop: object
    current_desktop: object
    normal: bool = True

    # 函数用途: 现在能不能看见：已映射、没最小化、在当前桌面。
    def visible_now(self) -> bool:
        return self.viewable and not self.hidden and self.desktop in (None, self.current_desktop)


# 类用途: OCR 给出的一块文字及其在截图像素里的外框 [x, y, w, h]。
@dataclass(frozen=True)
class TextRegion:
    text: str
    region: tuple[int, int, int, int]


# LLM: 所有后端的 capture() 都返回它（不另留只返回像素的旧形状）。kind 是采样方式的短标记（进 frame.capture，宿主按短标记校验）；
#   fallback_reason 只在主路径拿不到、退回别的采样方式时给结构化原因码，进 observe 结果顶层的 capture_fallback，不进 frame 与观察载荷。
# 类用途: 一次截图的像素与采样事实。
@dataclass(frozen=True)
class ScreenCapture:
    buffer: PixelBuffer
    kind: str
    fallback_reason: str | None = None


# 函数用途: 全局点矩形 (x, y, w, h) 是否相交（面积相交，边相切不算）。
def rects_intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


# 函数用途: 矩形 inner 是否整个落在 outer 里。
def rect_contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and inner[0] + inner[2] <= outer[0] + outer[2]
            and inner[1] + inner[3] <= outer[1] + outer[3])


# 函数用途: 点是否在矩形里。
def point_in_rect(point: tuple[int, int], rect: tuple[int, int, int, int]) -> bool:
    return rect[0] <= point[0] < rect[0] + rect[2] and rect[1] <= point[1] < rect[1] + rect[3]


# 函数用途: 候选外框中心（截图像素）换成全局点。
def click_point(geometry: WindowGeometry, region: tuple[int, int, int, int]) -> tuple[int, int]:
    return (int(geometry.origin[0] + (region[0] + region[2] / 2) / geometry.scale[0]),
            int(geometry.origin[1] + (region[1] + region[3] / 2) / geometry.scale[1]))


# 函数用途: 窗口几何对应的全局点矩形。
def window_rect(geometry: WindowGeometry) -> tuple[int, int, int, int]:
    return (geometry.origin[0], geometry.origin[1], geometry.size[0], geometry.size[1])


# 函数用途: 标签只作外部数据：空白类控制字符当空格、其它控制字符去掉、折叠空白、截到 MAX_LABEL_CHARS；空串表示丢弃。
def sanitize_label(text: object) -> str:
    kept = "".join(" " if ch in "\t\n\r\x0b\x0c" else ch for ch in str(text or "") if ch in "\t\n\r\x0b\x0c" or (ord(ch) >= 32 and ord(ch) != 127))
    return " ".join(kept.split())[:MAX_LABEL_CHARS]


# LLM: 五步链都在这一个类里，后端只提供事实；observe 与 click_candidate 都有副作用（写快照 / 点击），recheck 本身只读。
# 类用途: 观察一个窗口并给出候选；动作前复核后点击。
class ScreenObserver:
    def __init__(self, backend: object, *, registry: WindowInstanceRegistry | None = None,
                 store: SnapshotStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.backend = backend
        self.registry = registry or WindowInstanceRegistry()
        self.store = store or SnapshotStore()
        self.clock = clock

    # LLM: 不切焦点、不激活、不动鼠标；window 为空取最前的普通可见窗口，给了只接受本进程发过的 win:<boot>:<n> 别名。
    #   没有候选时结果里不带 my_agent_observation（不凭空造候选），但快照照样记。
    # 函数用途: 采样一个窗口并返回给模型的结果（含观察载荷）。
    def observe(self, window: str | None = None) -> dict[str, object]:
        windows = self._refresh_listing()
        target = self._target(windows, window)
        if not target.visible_now():
            raise ObservationError("not_viewable", "窗口当前不可见（未映射、已最小化或不在当前桌面）")
        above = list(self.backend.above_rects(target.native_id))
        rect = window_rect(target.geometry)
        if any(rect_contains(other, rect) for other in above):
            raise ObservationError("occluded", "窗口被上层窗口完全盖住")
        capture, geometry = self._capture(target)
        candidates = self._candidates(capture.buffer)
        ref = self.registry.ref_for(target.native_id)
        snapshot = WindowSnapshot(
            ref=ref, generation=self.store.next_generation(ref), native_id=target.native_id, captured_at=float(self.clock()),
            geometry=geometry, desktop=target.desktop, occluded=any(rects_intersect(other, rect) for other in above),
            candidates={item.key: item for item in (CandidateSnapshot(key, region, region_grid(capture.buffer, region))
                                                     for key, _label, region in candidates)},
        )
        self.store.record(snapshot)
        return self._result(snapshot, target, candidates, capture)

    # LLM: 只读复核：按 _meta 找快照与候选（not_found），再重新采样逐项核对（任一项不符 → stale）；不点击。
    # 函数用途: 判定一个候选此刻是否仍可点，返回快照、候选与全局点击点。
    def recheck(self, meta: object) -> tuple[WindowSnapshot, CandidateSnapshot, tuple[int, int]]:
        ref, generation, key = _meta_fields(meta)
        snapshot = self.store.find(ref, generation)
        candidate = snapshot.candidates.get(key) if snapshot is not None else None
        if snapshot is None or candidate is None:
            raise ObservationError("not_found", "没有这一代的观察或候选")
        windows = self._refresh_listing()
        info = next((item for item in windows if item.native_id == snapshot.native_id), None)
        if info is None or self.registry.ref_for(info.native_id) != ref:
            raise ObservationError("stale", "窗口实例已变")
        if not info.visible_now():
            raise ObservationError("stale", "窗口已不可见")
        if (info.geometry.origin, info.geometry.size) != (snapshot.geometry.origin, snapshot.geometry.size):
            raise ObservationError("stale", "窗口位置或尺寸已变")
        point = click_point(snapshot.geometry, candidate.region)
        if any(point_in_rect(point, other) for other in self.backend.above_rects(info.native_id)):
            raise ObservationError("stale", "点击点被上层窗口盖住")
        capture, geometry = self._capture(info)
        if geometry.scale != snapshot.geometry.scale:
            raise ObservationError("stale", "截图缩放已变（换了采样方式或显示器），区域摘要不可比")
        if not grid_unchanged(candidate.grid, region_grid(capture.buffer, candidate.region)):
            raise ObservationError("stale", "候选区域的像素已变")
        return snapshot, candidate, point

    # LLM: 复核通过后立刻点击，中间不做任何别的 I/O（只看一眼宿主是否已取消，已取消就零副作用返回 cancelled）；返回值只证明
    #   "提交了点击"，结果要靠下一次 observe 确认。
    # 函数用途: 按宿主复核过的候选点击一次。
    def click_candidate(self, meta: object, *, cancelled: Callable[[], bool] | None = None) -> dict[str, object]:
        snapshot, candidate, point = self.recheck(meta)
        if cancelled is not None and cancelled():
            raise ObservationError("cancelled", "宿主已取消，未点击")
        self.backend.click(point[0], point[1])
        return {"clicked": {"window": snapshot.ref, "generation": snapshot.generation, "key": candidate.key}, "point": list(point)}

    # 函数用途: 拉一次窗口列表并同步实例登记与快照环（消失的窗口一起忘掉）。
    def _refresh_listing(self) -> list[WindowInfo]:
        windows = list(self.backend.list_windows())
        self.store.forget(self.registry.observe_listing(item.native_id for item in windows))
        return windows

    # 函数用途: 选目标窗口：给了别名按别名找，否则取叠放最顶的普通可见窗口。
    def _target(self, windows: list[WindowInfo], window: str | None) -> WindowInfo:
        if window:
            native_id = self.registry.native_for(str(window))
            target = next((item for item in windows if native_id is not None and item.native_id == native_id), None)
        else:
            target = next((item for item in reversed(windows) if item.normal and item.visible_now()), None)
        if target is None:
            raise ObservationError("window_not_found", "没有这个窗口" if window else "当前没有可见的普通窗口")
        return target

    # LLM: 后端主动抛的 ObservationError（如 screen_recording_not_permitted）原样透传，其它异常一律变成 capture_failed。
    #   返回的几何是“列表里的原点、尺寸 + 截图自己的像素/点比”：scale 是这张截图的事实，不是显示器的。
    # 函数用途: 抓窗口像素并算出这次截图的几何；形状不对或宽高比例对不上都算 capture_failed。
    def _capture(self, target: WindowInfo) -> tuple[ScreenCapture, WindowGeometry]:
        try:
            capture = self.backend.capture(target)
        except ObservationError:
            raise
        except Exception as exc:  # noqa: BLE001 后端库的任何异常都只能变成结构化失败
            raise ObservationError("capture_failed", f"截图失败：{type(exc).__name__}") from exc
        if not isinstance(capture, ScreenCapture) or not isinstance(capture.buffer, PixelBuffer) or not capture.kind:
            raise ObservationError("capture_failed", "截图结果形状不对")
        return capture, captured_geometry(target.geometry, capture.buffer)

    # 函数用途: OCR 文字区域 → (key, label, region) 列表：去空标签、裁到截图范围、去零面积，最多 MAX_CANDIDATE_COUNT 个。
    def _candidates(self, buffer: PixelBuffer) -> list[tuple[str, str, tuple[int, int, int, int]]]:
        try:
            regions = list(self.backend.ocr(buffer))
        except Exception as exc:  # noqa: BLE001 同上，OCR 库异常只能变成结构化失败
            raise ObservationError("ocr_failed", f"OCR 失败：{type(exc).__name__}") from exc
        rows = []
        for index, item in enumerate(regions, 1):
            label, region = sanitize_label(item.text), _clip_region(item.region, buffer)
            if label and region is not None:
                rows.append((f"t{index}", label, region))
        return rows[:MAX_CANDIDATE_COUNT]

    # LLM: capture_fallback 放结果顶层（和 window、generation 并列）：宿主的 frame 只认固定键，多一个键整份拒绝。
    # 函数用途: 组装给模型的结果与 plugin_observation.v1 载荷（含 frame 与候选 region）。
    def _result(self, snapshot: WindowSnapshot, target: WindowInfo, candidates: Iterable[tuple[str, str, tuple[int, int, int, int]]],
                capture: ScreenCapture) -> dict[str, object]:
        frame = {"space": OBSERVATION_SPACE, **snapshot.geometry.frame_fields(), "captured_at": snapshot.captured_at,
                 "capture": capture.kind, "occluded": snapshot.occluded}
        rows = [{"key": key, "role": OCR_ROLE, "label": label, "actions": [CLICK_ACTION], "region": list(region)}
                for key, label, region in candidates]
        result: dict[str, object] = {"window": snapshot.ref, "generation": snapshot.generation, "title": sanitize_label(target.title),
                                     "frame": frame, "candidate_count": len(rows)}
        if capture.fallback_reason:
            result["capture_fallback"] = {"reason": capture.fallback_reason}
        if rows:
            result["my_agent_observation"] = {"schema": OBSERVATION_SCHEMA, "target": {"ref": snapshot.ref, "generation": snapshot.generation},
                                              "frame": frame, "candidates": rows}
        return result


# LLM: scale 按截图算：宽 = 像素宽 ÷ 点宽，高 = 像素高 ÷ 点高；两轴差出一个像素以上（不是同一个缩放）就判 capture_failed。
#   主路径按显示器缩放出图（Retina 为 2），按名义分辨率出图的回退为 1；核心的 click_point = origin + region / scale 两种都对。
# 函数用途: 由列表几何与截图像素算出这次截图的几何（原点、尺寸照抄，scale 取截图自己的像素/点比）。
def captured_geometry(listed: WindowGeometry, buffer: PixelBuffer) -> WindowGeometry:
    width, height = listed.size
    if width <= 0 or height <= 0:
        raise ObservationError("capture_failed", "窗口外框面积不是正数")
    scale = (buffer.width / width, buffer.height / height)
    if abs(buffer.height - height * scale[0]) > 1:
        raise ObservationError("capture_failed", "截图宽高比例与窗口外框不一致")
    return WindowGeometry(listed.origin, listed.size, scale)


# 函数用途: 校验宿主附的 _meta 观察上下文形状，返回 (ref, generation, key)。
def _meta_fields(meta: object) -> tuple[str, str, str]:
    if not isinstance(meta, dict):
        raise ObservationError("missing_context", "缺少宿主附的观察上下文")
    target = meta.get("target")
    if (meta.get("version") != OBSERVATION_META_VERSION or not isinstance(target, dict) or not isinstance(meta.get("key"), str)
            or not isinstance(target.get("ref"), str) or not isinstance(target.get("generation"), str)):
        raise ObservationError("missing_context", "观察上下文形状不对")
    return target["ref"], target["generation"], meta["key"]


# 函数用途: 把 OCR 外框裁到截图范围内并取整；零面积返回 None。
def _clip_region(region: object, buffer: PixelBuffer) -> tuple[int, int, int, int] | None:
    try:
        x, y, w, h = (int(round(float(value))) for value in region)
    except (TypeError, ValueError):
        return None
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(buffer.width, x + w), min(buffer.height, y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


__all__ = [
    "CLICK_ACTION", "OBSERVATION_SPACE", "OCR_ROLE", "SCREEN_REGION_CAPTURE", "WINDOW_IMAGE_CAPTURE", "WINDOW_TARGET_KIND",
    "ObservationError", "ScreenCapture", "ScreenObserver", "TextRegion", "WindowInfo", "captured_geometry", "click_point",
    "point_in_rect", "rect_contains", "rects_intersect", "sanitize_label", "window_rect",
]
