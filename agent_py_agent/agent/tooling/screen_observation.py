# LLM: 屏幕观察的来源无关核心（J16 片 B）：按后端给的窗口列表选目标、采样、OCR 候选、写快照、组装 plugin_observation.v1 载荷
#   （带几何扩展）；动作前按 ae 定稿做适配器层五项复核：按 _meta 代次找快照（找不到 / key 不在 → not_found）→ boot/instance →
#   可见、未最小化、同桌面 → 原点与尺寸相等 → 点击点不被遮挡（动作时重新查叠放）→ 重新截图的 scale 相等 → 候选区域摘要在容差内，
#   任一项不过 → stale，零副作用；
#   复核通过后立刻点击，中间不做任何别的 I/O；复核之前先问后端能不能真的点（ensure_click_permitted，macOS 是辅助功能权限）。
#   片 G：候选 = 后端给的无障碍控件 + OCR 文字区域，合并去重在 screen_ui_candidates；控件候选不比像素摘要，改比结构化事实
#   （role、enabled、外框、label 来源 sha、值是否可写）；type_into_candidate 只接受观察时带输入动作的控件：校验文字 → 权限 → 复核 →
#   点击 → 等焦点 →（全选）→ 输入，焦点确认后、真正打字（或删除）前各看一次取消；点击之后的失败在错误里带 clicked=true（已点击、未输入）。
#   控件的值（AXValue）不进快照、结果、日志，也不读回核对。
#   后端是鸭子类型（X11 / macOS 真后端 / 单测假后端），本模块不 import 任何桌面库。
#   截图由后端给 ScreenCapture（像素、采样方式、回退原因）；frame.scale 取截图自己的像素/点比（不取显示器的），复核时 scale 变了也判 stale；
#   非整数比时 scale 往上调到"点数 × scale ≥ 像素数"，宿主按 size × scale 校验贴边候选才不会误拒。
#   错误码集合开放（宿主只提升 stale / not_found，其它原样透传）：window_not_found | not_viewable | capture_failed | ocr_failed | occluded |
#   not_found | stale | missing_context | invalid_arguments | cancelled | screen_recording_not_permitted | accessibility_not_permitted |
#   focus_not_acquired | clear_unsupported | clear_failed | type_failed；
#   后端主动抛的 ObservationError 原样透传。
# 模块用途: "看一眼窗口、给出可点的候选、点之前再确认一遍没变"的全部判断逻辑，可在没有桌面的机器上用假后端完整测试。
from __future__ import annotations

import math
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..plugin_observation import (
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
from .screen_ui_candidates import (
    CLICK_ACTION,
    OCR_ROLE,
    TYPE_ACTION,
    MergedCandidate,
    UiScan,
    merge_candidates,
    sanitize_label,
)

# 观察载荷里的固定取值：坐标空间、目标类型
OBSERVATION_SPACE = "screen_points"
WINDOW_TARGET_KIND = "window"
# type_into_candidate 一次最多输入的字符数（超了整次拒绝、不截断：截一半输进去的是错误内容）
TYPE_INTO_TEXT_MAX_CHARS = 500
# 点击后最多等多久让控件拿到焦点（拿不到就 focus_not_acquired，不输入）
TYPE_INTO_FOCUS_WAIT_SECONDS = 0.5
# 等焦点时的轮询间隔
TYPE_INTO_FOCUS_POLL_SECONDS = 0.05
# 读控件树出了后端没归类的异常时，ui_tree.reason 用的原因码
UI_SCAN_FAILED_REASON = "ui_scan_failed"
# 截图采样方式（frame.capture）：单窗口内容（被压住的部分也拍得到）/ 按屏幕区域截屏（压在上面的窗口也会被拍进去）
WINDOW_IMAGE_CAPTURE = "window_image"
SCREEN_REGION_CAPTURE = "screen_region"


# LLM: code 是稳定机器码，进 structuredContent.my_agent_observation_error.code；message 是中文说明，不含路径、标题正文或坐标。
#   clicked=True 表示失败发生在已经点击之后（type_into 拿不到焦点、全选失败、点完被取消）：如实报"已点击、未输入"。
# 类用途: 观察或复核失败的结构化错误。
class ObservationError(Exception):
    def __init__(self, code: str, message: str, *, clicked: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.clicked = clicked


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


# LLM: 五步链都在这一个类里，后端只提供事实；observe、click_candidate、type_into_candidate 都有副作用（写快照 / 点击 / 键盘输入），
#   recheck 本身只读。
# 类用途: 观察一个窗口并给出候选；动作前复核后点击或输入。
class ScreenObserver:
    def __init__(self, backend: object, *, registry: WindowInstanceRegistry | None = None,
                 store: SnapshotStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.backend = backend
        self.registry = registry or WindowInstanceRegistry()
        self.store = store or SnapshotStore()
        self.clock = clock

    # LLM: 后端声明的结构化能力（能不能给出无障碍控件候选）；适配器按它决定注不注册 type_into_candidate，不按平台名判断。
    # 函数用途: 这个观察核心的后端能不能给出可输入的控件候选。
    @property
    def supports_ui_candidates(self) -> bool:
        return bool(self.backend.ui_candidates_supported)

    # LLM: 不切焦点、不激活、不动鼠标；window 为空取最前的普通可见窗口，给了只接受本进程发过的 win:<boot>:<n> 别名。
    #   候选 = 控件树（后端 ui_scan，读不全或读不到只降级、不失败，结果顶层带 ui_tree{status, reason}）+ OCR，合并去重见
    #   screen_ui_candidates。没有候选时结果里不带 my_agent_observation（不凭空造候选），但快照照样记。
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
        scan = self._ui_scan(target)
        bounds = (capture.buffer.width, capture.buffer.height)
        candidates = merge_candidates(scan.elements, self._ocr_rows(capture.buffer), geometry, bounds)
        ref = self.registry.ref_for(target.native_id)
        snapshot = WindowSnapshot(
            ref=ref, generation=self.store.next_generation(ref), native_id=target.native_id, captured_at=float(self.clock()),
            geometry=geometry, desktop=target.desktop, occluded=any(rects_intersect(other, rect) for other in above),
            candidates={item.key: _candidate_snapshot(item, capture.buffer) for item in candidates},
        )
        self.store.record(snapshot)
        result = self._result(snapshot, target, candidates, capture)
        if scan.status:
            result["ui_tree"] = {"status": scan.status, **({"reason": scan.reason} if scan.reason else {})}
        return result

    # LLM: 只读复核：按 _meta 找快照与候选（not_found），再重新采样逐项核对（任一项不符 → stale）；不点击。最后一项按候选来源分：
    #   OCR 候选比区域像素摘要；控件候选不比像素（输入框里光标会闪），改比后端重新读的结构化事实（控件没了也是 stale）。
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
        if candidate.facts is not None:
            if self.backend.ui_facts(candidate.native) != candidate.facts:
                raise ObservationError("stale", "控件已变（不在了，或角色、可用、外框、名称、可编辑有变化）")
        elif not grid_unchanged(candidate.grid, region_grid(capture.buffer, candidate.region)):
            raise ObservationError("stale", "候选区域的像素已变")
        return snapshot, candidate, point

    # LLM: 先问后端能不能真的点（没权限时系统会悄悄丢掉点击、工具却报已点击）：放在复核之前，零副作用报 accessibility_not_permitted，
    #   保持"复核完立刻点击"。复核通过后中间不做任何别的 I/O（只看一眼宿主是否已取消，已取消就零副作用返回 cancelled）；
    #   返回值只证明"提交了点击"，结果要靠下一次 observe 确认。
    # 函数用途: 按宿主复核过的候选点击一次。
    def click_candidate(self, meta: object, *, cancelled: Callable[[], bool] | None = None) -> dict[str, object]:
        self.backend.ensure_click_permitted()
        snapshot, candidate, point = self.recheck(meta)
        if cancelled is not None and cancelled():
            raise ObservationError("cancelled", "宿主已取消，未点击")
        self.backend.click(point[0], point[1])
        return {"clicked": {"window": snapshot.ref, "generation": snapshot.generation, "key": candidate.key}, "point": list(point)}

    # LLM: 顺序是合同：校验文字（零副作用）→ 点击权限 → 两层复核 → 候选真有输入动作、要清空时能全选 → 看取消 → 点击（让控件拿焦点）→
    #   等焦点（拿不到 → focus_not_acquired，已点击、未输入）→ [清空：看取消 → AX 全选，失败 → clear_failed] → 看取消 → 打字（text 为空
    #   时按删除键清掉选区）。点击之后的失败都带 clicked=true。不读回控件的值核对（防密码泄露），结果 application_verified=False，
    #   内容是否真的进去要靠下一次 observe。
    # 函数用途: 往宿主复核过的可编辑控件里输入一段显式文字，可选先清空原有内容。
    def type_into_candidate(self, meta: object, text: object, clear_existing: object = False, *,
                            cancelled: Callable[[], bool] | None = None) -> dict[str, object]:
        check_typed_text(text, clear_existing)
        self.backend.ensure_click_permitted()
        snapshot, candidate, point = self.recheck(meta)
        _require_typing_target(candidate, bool(clear_existing))
        stop = cancelled or _never_cancelled
        _raise_if_cancelled(stop, clicked=False)
        self.backend.click(point[0], point[1])
        if not self._wait_for_focus(candidate.native):
            raise ObservationError("focus_not_acquired", "已点击、未输入：控件没有拿到焦点", clicked=True)
        if clear_existing:
            _raise_if_cancelled(stop, clicked=True)
            if not self.backend.ui_select_all(candidate.native):
                raise ObservationError("clear_failed", "已点击、未输入：没能选中原有内容", clicked=True)
        _raise_if_cancelled(stop, clicked=True)
        _type_or_delete(self.backend, str(text))
        return {"typed": {"window": snapshot.ref, "generation": snapshot.generation, "key": candidate.key},
                "characters": len(str(text)), "clear_existing": bool(clear_existing), "application_verified": False}

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

    # LLM: 控件树读不到只降级不失败：后端主动给的 UiScan 原样用；后端抛了任何异常（BaseException 的测试防线除外）或形状不对，
    #   都当作一个控件都没有，ui_tree 记 unavailable + UI_SCAN_FAILED_REASON。
    # 函数用途: 读目标窗口的无障碍控件。
    def _ui_scan(self, target: WindowInfo) -> UiScan:
        try:
            scan = self.backend.ui_scan(target)
        except Exception:  # noqa: BLE001 控件树只是候选来源之一，读不到不能让整次观察失败
            scan = None
        return scan if isinstance(scan, UiScan) else UiScan(status="unavailable", reason=UI_SCAN_FAILED_REASON)

    # 函数用途: OCR 文字区域 → (label, region) 列表：去空标签、裁到截图范围、去零面积（数量上限在合并后统一截）。
    def _ocr_rows(self, buffer: PixelBuffer) -> list[tuple[str, tuple[int, int, int, int]]]:
        try:
            regions = list(self.backend.ocr(buffer))
        except Exception as exc:  # noqa: BLE001 同上，OCR 库异常只能变成结构化失败
            raise ObservationError("ocr_failed", f"OCR 失败：{type(exc).__name__}") from exc
        rows = [(sanitize_label(item.text), _clip_region(item.region, buffer)) for item in regions]
        return [(label, region) for label, region in rows if label and region is not None]

    # 函数用途: 点击后在 TYPE_INTO_FOCUS_WAIT_SECONDS 内轮询控件是否拿到焦点。
    def _wait_for_focus(self, native: object) -> bool:
        deadline = time.monotonic() + TYPE_INTO_FOCUS_WAIT_SECONDS
        while not self.backend.ui_focused(native):
            if time.monotonic() >= deadline:
                return False
            time.sleep(TYPE_INTO_FOCUS_POLL_SECONDS)
        return True

    # LLM: capture_fallback 放结果顶层（和 window、generation 并列）：宿主的 frame 只认固定键，多一个键整份拒绝。
    # 函数用途: 组装给模型的结果与 plugin_observation.v1 载荷（含 frame 与候选 region）。
    def _result(self, snapshot: WindowSnapshot, target: WindowInfo, candidates: Iterable[MergedCandidate],
                capture: ScreenCapture) -> dict[str, object]:
        frame = {"space": OBSERVATION_SPACE, **snapshot.geometry.frame_fields(), "captured_at": snapshot.captured_at,
                 "capture": capture.kind, "occluded": snapshot.occluded}
        rows = [{"key": item.key, "role": item.role, "label": item.label, "actions": list(item.actions), "region": list(item.region)}
                for item in candidates]
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
    scale = (covering_scale(buffer.width, width), covering_scale(buffer.height, height))
    if abs(buffer.height - height * scale[0]) > 1:
        raise ObservationError("capture_failed", "截图宽高比例与窗口外框不一致")
    return WindowGeometry(listed.origin, listed.size, scale)


# LLM: 宿主按 size × scale 算截图宽高再校验候选（x+w ≤ 宽）；非整数比时浮点乘回来可能比像素数小一点（71 点、124 像素 →
#   123.99999999999999），贴边候选会让整份观察被拒。这里把 scale 往上挪到乘回来不小于像素数为止（通常一个最小浮点步长）。
# 函数用途: 一个轴的截图缩放：像素 ÷ 点，并保证 点 × 缩放 ≥ 像素。
def covering_scale(pixels: int, points: int) -> float:
    scale = pixels / points
    while scale * points < pixels:
        scale = math.nextafter(scale, math.inf)
    return scale


# LLM: 输入文字的边界（ae 定稿）：字符串、≤ TYPE_INTO_TEXT_MAX_CHARS（超了拒绝、不截断）、不含控制字符（Cc，含换行和 Tab：单行框里
#   换行等于提交，Tab 会挪走焦点）和孤立代理码点（Cs）；空文字只在 clear_existing=true（清空）时允许。全部在任何副作用之前判。
# 函数用途: 校验 type_into_candidate 的 text 与 clear_existing，不合规就 invalid_arguments。
def check_typed_text(text: object, clear_existing: object) -> None:
    if not isinstance(text, str) or not isinstance(clear_existing, bool):
        raise ObservationError("invalid_arguments", "text 必须是字符串，clear_existing 必须是布尔值")
    if len(text) > TYPE_INTO_TEXT_MAX_CHARS:
        raise ObservationError("invalid_arguments", f"文字超过 {TYPE_INTO_TEXT_MAX_CHARS} 个字符，未输入（不截断）")
    if not text and not clear_existing:
        raise ObservationError("invalid_arguments", "没有要输入的文字")
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in text):
        raise ObservationError("invalid_arguments", "文字里有控制字符（含换行、Tab）或无效字符，未输入")


# 函数用途: 候选必须是观察时带输入动作的控件；要清空时控件得能全选（不能就 clear_unsupported），都在点击之前判。
def _require_typing_target(candidate: CandidateSnapshot, clear_existing: bool) -> None:
    if TYPE_ACTION not in candidate.actions or candidate.facts is None:
        raise ObservationError("invalid_arguments", "这个候选不能输入文字")
    if clear_existing and not candidate.facts.selection_settable:
        raise ObservationError("clear_unsupported", "这个控件不支持全选，无法清空原有内容；未点击")


# 函数用途: 打字（text 为空时按删除键清掉已全选的内容）；后端异常变成 type_failed（已点击，可能只输入了一部分）。
def _type_or_delete(backend: object, text: str) -> None:
    try:
        if text:
            backend.type_text(text)
        else:
            backend.press_delete()
    except Exception as exc:  # noqa: BLE001 键盘库的任何异常都只能变成结构化失败
        raise ObservationError("type_failed", f"已点击，输入中途失败，可能只输入了一部分：{type(exc).__name__}", clicked=True) from exc


# 函数用途: 宿主已取消就停下；clicked 说明停下时是否已经点过。
def _raise_if_cancelled(cancelled: Callable[[], bool], *, clicked: bool) -> None:
    if cancelled():
        raise ObservationError("cancelled", "宿主已取消，已点击、未输入" if clicked else "宿主已取消，未点击", clicked=clicked)


# 函数用途: 没有宿主取消判定时的默认值。
def _never_cancelled() -> bool:
    return False


# 函数用途: 合并后的候选 → 快照项：OCR 候选记像素摘要；控件候选记结构化事实与后端句柄（不记像素、不记值）。
def _candidate_snapshot(item: MergedCandidate, buffer: PixelBuffer) -> CandidateSnapshot:
    if item.element is None:
        return CandidateSnapshot(item.key, item.region, region_grid(buffer, item.region), item.actions)
    return CandidateSnapshot(item.key, item.region, None, item.actions, item.element.native, item.element.facts)


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
    "CLICK_ACTION", "OBSERVATION_SPACE", "OCR_ROLE", "SCREEN_REGION_CAPTURE", "TYPE_ACTION", "TYPE_INTO_FOCUS_POLL_SECONDS",
    "TYPE_INTO_FOCUS_WAIT_SECONDS", "TYPE_INTO_TEXT_MAX_CHARS", "UI_SCAN_FAILED_REASON", "WINDOW_IMAGE_CAPTURE", "WINDOW_TARGET_KIND",
    "ObservationError", "ScreenCapture", "ScreenObserver", "TextRegion", "WindowInfo", "captured_geometry", "check_typed_text", "click_point", "covering_scale",
    "point_in_rect", "rect_contains", "rects_intersect", "sanitize_label", "window_rect",
]
