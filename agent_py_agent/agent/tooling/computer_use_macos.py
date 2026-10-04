# LLM: J16 片 E 的 macOS 真后端：只用 pyobjc（pywinctl 在 macOS 上带入）、mss、pyautogui 的公开接口，给
#   screen_observation.ScreenObserver 提供"窗口列表（底→顶）、上层矩形、窗口截图、OCR 文字区域、点击"五个事实，判定全在核心。
#   - 列窗口：OptionAll 保留最小化 / 别的桌面窗口和实例身份；叠放仅按 OnScreenOnly 的前→后顺序，转成底→顶，未在其中的项放末尾。
#     身份是 (kCGWindowNumber, owner PID)；layer==0 才算普通窗口。可见性仍按 IsOnscreen、alpha 和显示器中心判定。
#   - 遮挡：OnScreenAboveWindow 返回压在目标前面的矩形；仅当系统 Dock 层级且外框覆盖整个活动显示器时排除，不按程序名判断。
#     其它层级以及只覆盖屏幕一部分的 Dock 窗口仍是遮挡事实；observe / recheck 共用此入口。
#   - 权限：list_windows 先 CGPreflightScreenCaptureAccess()（只查不弹窗），没授权 → screen_recording_not_permitted，放在找窗口之前，
#     免得标题拿不到被误报 window_not_found。绝不调 CGRequestScreenCaptureAccess。
#   - 截图：主路径 ScreenCaptureKit 单窗口截图（macOS 14+，有 SCScreenshotManager），回调各带超时，超时后晚到的结果丢掉；
#     拿不到时退回 mss 区域截图并给结构化原因（unavailable / too_old / timeout / failed）。窗口不在可分享内容里 → capture_failed，
#     不回退（回退会把后面别的窗口拍成这个窗口的候选）；用户拒绝授权按 NSError 的 domain+code 判 → screen_recording_not_permitted。
#     mss 的 macOS 实现内部用已废弃的 CGWindowListCreateImage、按名义分辨率出图：回退截图 scale 由核心按像素/点比算出是 1。
#   - 点击权限：pyautogui 用 CGEventPost 发事件，宿主没有辅助功能权限时系统会悄悄丢掉、不报错。核心在复核之前调
#     ensure_click_permitted()，这里用 AXIsProcessTrusted()（只查不弹窗，绝不调带提示选项的 AXIsProcessTrustedWithOptions）问一次，
#     没权限 / 绑定导入失败 → accessibility_not_permitted（无法确认就不点）。
#   - 控件候选（片 G）：ui_scan / ui_facts / ui_focused / ui_select_all 交给 computer_use_macos_ax（只读遍历，唯一的写是全选）；
#     输入走既有的 computer_text_input.type_desktop_text（Quartz Unicode 键盘事件，不碰剪贴板），清空后 text 为空时按删除键
#     （kVK_Delete，物理键码与键盘布局无关）。ui_candidates_supported=True 是给适配器的结构化能力声明。
#   - 真实框架只经 load_real_macos_frameworks() 这一个入口拿；构造时注入假框架就不碰它。测试侧守卫在会话级把它换成直接失败。
#   改动同步 test_computer_use_macos 与设计稿 J16 第 3.2 节 macOS 口径。
# 模块用途: 把 macOS 桌面变成观察核心能理解的几个事实，自己不做任何判定；单测全用假 Quartz / 假 ScreenCaptureKit。
from __future__ import annotations

import importlib
import platform
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .computer_text_input import type_desktop_text
from .computer_use_macos_ax import element_facts, element_focused, scan_window, select_all
from .screen_observation import (
    SCREEN_REGION_CAPTURE,
    WINDOW_IMAGE_CAPTURE,
    ObservationError,
    ScreenCapture,
    TextRegion,
    WindowInfo,
    rect_contains,
)
from .screen_observation_store import UiFacts, WindowGeometry
from .screen_ocr import RapidOcrReader
from .screen_region_digest import PixelBuffer
from .screen_ui_candidates import UiScan

# ScreenCaptureKit 每个异步回调（查可分享内容、单窗口截图）最多等多久，超时退回区域截图
MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS = 5.0
# 有单窗口截图接口（SCScreenshotManager）的最低 macOS 主版本号（系统版本事实，低于它报 screencapturekit_too_old）
SCREENSHOT_MANAGER_MIN_MACOS_MAJOR_VERSION = 14
# macOS 删除键（kVK_Delete）的虚拟键码：物理键位，与键盘布局无关（系统协议值）
MACOS_DELETE_KEY_CODE_PROTOCOL_VALUE = 51


# LLM: 测试注入假框架时整份替换；quartz / screencapturekit 是模块对象（常量和函数都从它上面取，不写死数值），grabber 返回 mss
#   的截屏上下文，click 在全局点点击；screencapturekit / ax（ApplicationServices）为 None 表示导入失败；type_text 往当前焦点控件
#   输入一段文字（不清空、不碰剪贴板）。
# 类用途: macOS 后端用到的全部系统框架与系统版本。
@dataclass(frozen=True)
class MacFrameworks:
    quartz: Any
    screencapturekit: Any | None
    grabber: Callable[[], Any]
    click: Callable[[int, int], None]
    macos_version: tuple[int, ...]
    ax: Any | None
    type_text: Callable[[str], None]


# LLM: 唯一拿真实系统框架的入口（会 import pyobjc 的 Quartz / ScreenCaptureKit / ApplicationServices；mss、pyautogui 用到时才 import）。
#   Quartz 导入失败抛 ImportError，由后端变成结构化失败；ScreenCaptureKit 导入失败记 None，截图时退回区域截图并给原因；
#   ApplicationServices 导入失败记 None，控件树读不到（只出 OCR 候选），点击按没有辅助功能权限处理。
#   测试侧守卫在会话级把这个函数换成直接失败，测试永远碰不到真实屏幕。
# 函数用途: 加载真实的 macOS 框架。
def load_real_macos_frameworks() -> MacFrameworks:
    quartz = importlib.import_module("Quartz")
    screencapturekit, ax = _optional_module("ScreenCaptureKit"), _optional_module("ApplicationServices")
    return MacFrameworks(quartz, screencapturekit, lambda: importlib.import_module("mss").mss(),
                         lambda x, y: importlib.import_module("pyautogui").click(x, y), _macos_version(), ax,
                         lambda text: type_desktop_text(text, False))


# 函数用途: 导入一个可选的系统框架绑定，导入失败返回 None。
def _optional_module(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


# LLM: 锁屏事实来自两个只读系统查询：CGSSessionCopyCurrentDictionary() 的 CGSSessionScreenIsLocked，以及活动显示器数
#   （锁屏时活动显示器为 0，等同于不可观察）。两条都只读、不弹授权框，失败（键缺失、返回类型不对、查询抛错）
#   一律按"无法确认"继续原流程——查不到锁屏不该把观察挡掉。仅 macOS 有这个概念，Linux X11 后端不动。
#   显示器数用 CGGetActiveDisplayList(0, None, count) 查总数：max=0 只写计数不填缓冲；返回的第三个值才是总数。
# 函数用途: 屏幕已锁定（或没有活动显示器）时抛 screen_locked，否则正常返回。
def _raise_if_screen_locked(quartz: Any) -> None:
    try:
        session = quartz.CGSessionCopyCurrentDictionary()
        locked = bool((session or {}).get("CGSSessionScreenIsLocked"))
        no_active_display = int(quartz.CGGetActiveDisplayList(0, None, None)[2] or 0) == 0
    except Exception:  # noqa: BLE001 查不到锁屏状态属于"无法确认"，不阻断观察
        return
    if locked or no_active_display:
        raise ObservationError("screen_locked", "屏幕已锁定，解锁后再观察")


# LLM: 每个方法都重新读系统事实，不缓存窗口状态（复核要新鲜事实）；frameworks 为 None 时第一次用才加载真实框架。
# 类用途: 观察核心的 macOS 后端。
class MacBackend:
    ui_candidates_supported = True

    def __init__(self, frameworks: MacFrameworks | None = None) -> None:
        self._frameworks = frameworks
        self._ocr = RapidOcrReader()

    # LLM: 权限预检后用 OptionAll 保留所有窗口身份；它的枚举顺序不可信，必须另查 OnScreenOnly 并按前→后结果整理。
    #   输出契约仍为底→顶；OnScreenOnly 未列到的最小化 / 其它桌面窗口保留在末尾，不参与当前可见目标排序。
    #   锁屏检查放在权限之后、列窗之前：锁屏时所有窗口都被系统盖住，这时候列窗只会得到 occluded 之类的次级结论，
    #   对模型和用户都没说明白；先给 screen_locked 更准确。检查是只读的（不弹窗），查不到就当"无法确认"继续原流程。
    # 函数用途: 列出窗口（叠放底→顶）及其当下事实。
    def list_windows(self) -> list[WindowInfo]:
        quartz = self._fw().quartz
        if not quartz.CGPreflightScreenCaptureAccess():
            raise ObservationError("screen_recording_not_permitted", "没有屏幕录制权限：请在系统设置的隐私与安全性里允许屏幕录制")
        _raise_if_screen_locked(quartz)
        all_options = quartz.kCGWindowListOptionAll | quartz.kCGWindowListExcludeDesktopElements
        screen_options = quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements
        all_rows = quartz.CGWindowListCopyWindowInfo(all_options, quartz.kCGNullWindowID) or []
        screen_rows = quartz.CGWindowListCopyWindowInfo(screen_options, quartz.kCGNullWindowID) or []
        stack_order = {
            int(row[quartz.kCGWindowNumber]): index
            for index, row in enumerate(screen_rows)
            if row.get(quartz.kCGWindowNumber) is not None
        }
        windows = [info for info in (_window_info(quartz, row) for row in all_rows) if info is not None]
        windows.sort(key=lambda info: (info.native_id[0] not in stack_order, -stack_order.get(info.native_id[0], 0)))
        return windows

    # LLM: OnScreenAboveWindow 是 observe / recheck 共用的上层事实源；只排除系统 Dock 层级且外框覆盖某整块显示器的窗，不能按名字判断。
    #   半透明窗口、浮窗、菜单及覆盖范围不完整的 Dock 仍返回，交核心按外框判断完全遮挡和 occluded。
    # 函数用途: 压在 native_id 前面的在屏窗口矩形（全局点，含标题栏、不含阴影），去掉 alpha=0 与 Dock 全屏背景窗。
    def above_rects(self, native_id: object) -> list[tuple[int, int, int, int]]:
        quartz = self._fw().quartz
        number = native_id[0]
        rows = quartz.CGWindowListCopyWindowInfo(quartz.kCGWindowListOptionOnScreenAboveWindow, number) or []
        ignored_dock_numbers = _full_display_dock_window_numbers(quartz, rows)
        return [rect for rect, row in ((_bounds_rect(quartz, row), row) for row in rows)
                if rect is not None and row.get(quartz.kCGWindowNumber) != number and _alpha(quartz, row) > 0
                and row.get(quartz.kCGWindowNumber) not in ignored_dock_numbers]

    # LLM: 主路径不可用或回调超时 / 失败才退回区域截图，并带原因；ObservationError（权限、窗口不可分享）原样抛给核心。
    # 函数用途: 截一张窗口图，返回像素与采样事实。
    def capture(self, info: WindowInfo) -> ScreenCapture:
        frameworks = self._fw()
        reason = _screencapturekit_unavailable_reason(frameworks)
        if reason is None:
            try:
                return ScreenCapture(_window_image(frameworks, info), WINDOW_IMAGE_CAPTURE)
            except _CaptureFallback as fallback:
                reason = fallback.reason
        return ScreenCapture(_screen_region(frameworks, info), SCREEN_REGION_CAPTURE, reason)

    # 函数用途: 截图里的文字区域（与 X11 共用的 RapidOCR 识别器）。
    def ocr(self, buffer: PixelBuffer) -> list[TextRegion]:
        return self._ocr.read(buffer)

    # 函数用途: 在全局点坐标点一下（复核通过后立刻调用，中间不做别的 I/O）。
    def click(self, x: int, y: int) -> None:
        self._fw().click(int(x), int(y))

    # LLM: 核心在复核之前调（不放在"复核通过 → 点击"之间）；只查不弹窗。绑定导入失败按没权限处理：确认不了就不点，
    #   免得系统丢掉点击、工具却报已点击，给自动执行留下假的成功事实。
    # 函数用途: 点击前确认宿主有辅助功能权限，没有就报 accessibility_not_permitted。
    def ensure_click_permitted(self) -> None:
        ax = self._fw().ax
        if ax is None or not ax.AXIsProcessTrusted():
            raise ObservationError("accessibility_not_permitted",
                                   "没有辅助功能权限（或无法确认）：系统会悄悄丢掉点击，请在系统设置的隐私与安全性里允许辅助功能")

    # 函数用途: 读目标窗口的无障碍控件（读不到只降级，见 computer_use_macos_ax.scan_window）。
    def ui_scan(self, info: WindowInfo) -> UiScan:
        return scan_window(self._fw().ax, info)

    # 函数用途: 复核用：重新读一个控件的结构化事实；控件已失效返回 None。
    def ui_facts(self, native: object) -> UiFacts | None:
        return element_facts(self._fw().ax, native)

    # 函数用途: 控件现在有没有焦点。
    def ui_focused(self, native: object) -> bool:
        return element_focused(self._fw().ax, native)

    # 函数用途: 在已拿到焦点的输入框里全选原有内容（AX 设选区并读回核对）。
    def ui_select_all(self, native: object) -> bool:
        ax = self._fw().ax
        return ax is not None and select_all(ax, native)

    # 函数用途: 往当前焦点控件输入文字（Quartz Unicode 键盘事件）。
    def type_text(self, text: str) -> None:
        self._fw().type_text(text)

    # 函数用途: 按一下删除键（清空时 text 为空，用它删掉已全选的内容）。
    def press_delete(self) -> None:
        quartz = self._fw().quartz
        for down in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, MACOS_DELETE_KEY_CODE_PROTOCOL_VALUE, down)
            if event is None:
                raise RuntimeError("系统未能创建键盘事件")
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    # 函数用途: 取框架：注入的假框架直接用，否则第一次用时加载真实框架（缺 pyobjc 变成结构化失败）。
    def _fw(self) -> MacFrameworks:
        if self._frameworks is None:
            try:
                self._frameworks = load_real_macos_frameworks()
            except ImportError as exc:
                raise ObservationError("capture_failed", f"缺少 macOS 观察依赖：{type(exc).__name__}") from exc
        return self._frameworks


# 类用途: 主路径拿不到、应退回区域截图的内部信号，带结构化原因码。
class _CaptureFallback(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 每次等待一个独立对象：超时后标记作废，晚到的回调什么都写不进去；不同调用之间不共享任何状态。ScreenCaptureKit 的两个
#   完成回调都是 (结果, NSError) 两个参数。
# 类用途: 一次 ScreenCaptureKit 异步回调的等待点。
class _PendingCallback:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.values: tuple[Any, Any] | None = None
        self.abandoned = False

    # 函数用途: 完成回调：没作废才记下 (结果, 错误) 并唤醒等待方。
    def deliver(self, result: Any, error: Any) -> None:
        with self.lock:
            if self.abandoned:
                return
            self.values = (result, error)
        self.event.set()


# 函数用途: 发起一次带完成回调的调用并等 (结果, 错误)；超时作废这次等待并退回区域截图。
def _await_callback(start: Callable[[Callable[[Any, Any], None]], None]) -> tuple[Any, Any]:
    pending = _PendingCallback()
    start(pending.deliver)
    if not pending.event.wait(MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS):
        with pending.lock:
            pending.abandoned = True
        raise _CaptureFallback("screencapturekit_timeout")
    return pending.values or (None, None)


# LLM: 系统太老按 platform.mac_ver() 判（too_old）；导入失败或 pyobjc 没有 SCScreenshotManager 算 unavailable；系统版本取不到时以绑定为准。
# 函数用途: 主路径（ScreenCaptureKit 单窗口截图）此刻不可用的原因码，可用返回 None。
def _screencapturekit_unavailable_reason(frameworks: MacFrameworks) -> str | None:
    if frameworks.macos_version and frameworks.macos_version[0] < SCREENSHOT_MANAGER_MIN_MACOS_MAJOR_VERSION:
        return "screencapturekit_too_old"
    if frameworks.screencapturekit is None or not hasattr(frameworks.screencapturekit, "SCScreenshotManager"):
        return "screencapturekit_unavailable"
    return None


# LLM: 先查可分享内容找到这个窗口号（找不到 → capture_failed，不回退），再按外框 × 显示器缩放出一张不带光标、不带阴影的单窗口图。
# 函数用途: 用 ScreenCaptureKit 截一个窗口的内容（被压住的部分也拍得到）。
def _window_image(frameworks: MacFrameworks, info: WindowInfo) -> PixelBuffer:
    kit = frameworks.screencapturekit
    content, error = _await_callback(lambda done: kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, True, done))
    _raise_for_error(kit, error)
    windows = list(content.windows() or []) if content is not None else []
    window = next((item for item in windows if int(item.windowID()) == info.native_id[0]), None)
    if window is None:
        raise ObservationError("capture_failed", "窗口不在可分享内容里（可能刚关掉，或应用禁止截图）")
    content_filter = kit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    config = kit.SCStreamConfiguration.alloc().init()
    config.setWidth_(round(info.geometry.size[0] * info.geometry.scale[0]))
    config.setHeight_(round(info.geometry.size[1] * info.geometry.scale[1]))
    config.setShowsCursor_(False)
    config.setIgnoreShadowsSingleWindow_(True)
    image, error = _await_callback(lambda done: kit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(content_filter, config, done))
    _raise_for_error(kit, error)
    if image is None:
        raise _CaptureFallback("screencapturekit_failed")
    return _cgimage_rgb(frameworks.quartz, image)


# LLM: 只认 NSError 的结构化 domain + code：用户拒绝授权（SCStreamErrorUserDeclined）不回退（没权限时区域截图只会拍到壁纸）；
#   其它错误退回区域截图。不看 localizedDescription。
# 函数用途: 把 ScreenCaptureKit 回调里的错误分成"没权限"和"可回退"两类。
def _raise_for_error(kit: Any, error: Any) -> None:
    if error is None:
        return
    if error.domain() == kit.SCStreamErrorDomain and error.code() == kit.SCStreamErrorUserDeclined:
        raise ObservationError("screen_recording_not_permitted", "用户拒绝了屏幕录制授权")
    raise _CaptureFallback("screencapturekit_failed")


# LLM: ScreenCaptureKit 出的是每像素 32 位 BGRA，每行可能有对齐填充；不是 32 位就判 capture_failed。用 bytearray 切片换通道（不依赖 numpy）。
# 函数用途: 把 CGImage 变成行优先 RGB 的 PixelBuffer。
def _cgimage_rgb(quartz: Any, image: Any) -> PixelBuffer:
    width, height = int(quartz.CGImageGetWidth(image)), int(quartz.CGImageGetHeight(image))
    if int(quartz.CGImageGetBitsPerPixel(image)) != 32:
        raise ObservationError("capture_failed", "截图像素格式不是 32 位")
    stride = int(quartz.CGImageGetBytesPerRow(image))
    data = bytes(quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(image)))[: stride * height]
    rows = data if stride == width * 4 else b"".join(data[row * stride: row * stride + width * 4] for row in range(height))
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = rows[2::4], rows[1::4], rows[0::4]
    return PixelBuffer(width, height, bytes(rgb))


# 函数用途: 退回 mss 按窗口外框（全局点）区域截屏；mss 在 macOS 上按名义分辨率出图，scale 由核心按像素/点比算。
def _screen_region(frameworks: MacFrameworks, info: WindowInfo) -> PixelBuffer:
    origin, size = info.geometry.origin, info.geometry.size
    with frameworks.grabber() as grabber:
        shot = grabber.grab({"left": int(origin[0]), "top": int(origin[1]), "width": int(size[0]), "height": int(size[1])})
    return PixelBuffer(int(shot.width), int(shot.height), bytes(shot.rgb))


# LLM: 身份 (窗口号, PID)：窗口号被别的进程复用时算新实例；scale 取窗口中心所在显示器的显示模式（像素宽 ÷ 点宽），
#   不取主屏；中心不在任何显示器上就算不可见。标题没屏幕录制权限时为空，只作外部数据。
# 函数用途: 把一行窗口信息变成 WindowInfo；缺窗口号返回 None。
def _window_info(quartz: Any, row: Any) -> WindowInfo | None:
    number = row.get(quartz.kCGWindowNumber)
    rect = _bounds_rect(quartz, row)
    if number is None or rect is None:
        return None
    scale = _display_scale(quartz, (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2))
    viewable = bool(row.get(quartz.kCGWindowIsOnscreen, False)) and _alpha(quartz, row) > 0 and scale is not None
    return WindowInfo(
        native_id=(int(number), int(row.get(quartz.kCGWindowOwnerPID) or 0)), title=str(row.get(quartz.kCGWindowName) or ""),
        geometry=WindowGeometry((rect[0], rect[1]), (rect[2], rect[3]), (scale or 1.0, scale or 1.0)),
        viewable=viewable, hidden=False, desktop=None, current_desktop=None,
        normal=int(row.get(quartz.kCGWindowLayer) or 0) == 0,
    )


# 函数用途: 一行窗口信息的外框（全局点，取整）；没有外框或面积不是正数返回 None。
def _bounds_rect(quartz: Any, row: Any) -> tuple[int, int, int, int] | None:
    bounds = row.get(quartz.kCGWindowBounds) or {}
    x, y, w, h = (round(float(bounds.get(key, 0) or 0)) for key in ("X", "Y", "Width", "Height"))
    return (x, y, w, h) if w > 0 and h > 0 else None


# 函数用途: 一行窗口信息的透明度（缺省按 0，即看不见）。
def _alpha(quartz: Any, row: Any) -> float:
    return float(row.get(quartz.kCGWindowAlpha) or 0)


# LLM: 只忽略系统报告的 Dock 层级且外框完整覆盖至少一块活动显示器的行；无层级或屏幕几何事实时一律保留为遮挡候选。
# 函数用途: 找出覆盖整块显示器的系统 Dock 背景窗口号，不读程序名或标题。
def _full_display_dock_window_numbers(quartz: Any, rows: list[Any]) -> set[object]:
    if not rows:
        return set()
    dock_level = quartz.CGWindowLevelForKey(quartz.kCGDockWindowLevelKey)
    dock_rows = [row for row in rows if int(row.get(quartz.kCGWindowLayer) or 0) == dock_level]
    if not dock_rows:
        return set()
    display_bounds = _active_display_bounds(quartz)
    if not display_bounds:
        return set()
    return {
        row[quartz.kCGWindowNumber]
        for row in dock_rows
        if row.get(quartz.kCGWindowNumber) is not None
        and (rect := _bounds_rect(quartz, row)) is not None
        and any(rect_contains(rect, display) for display in display_bounds)
    }


# LLM: 两阶段读取活动显示器 ID，失败或结果不完整时返回空集，让调用方保守保留遮挡，不猜系统窗口身份。
# 函数用途: 读取当前活动显示器的全局点外框，供判断窗口是否盖住整块显示器。
def _active_display_bounds(quartz: Any) -> list[tuple[int, int, int, int]]:
    try:
        error, _displays, display_count = quartz.CGGetActiveDisplayList(0, None, None)
        if error or not display_count:
            return []
        error, displays, display_count = quartz.CGGetActiveDisplayList(int(display_count), None, None)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return []
    if error or not displays:
        return []
    return [
        rect for display in displays[:int(display_count)]
        if (rect := _display_bounds_rect(quartz, display)) is not None
    ]


# LLM: CGDisplayBounds 使用全局点 CGRect；坏几何不可用于安全排除遮挡，必须由 active display 汇总忽略。
# 函数用途: 把一个 CoreGraphics 显示器外框规范化为正面积的整数全局矩形。
def _display_bounds_rect(quartz: Any, display: object) -> tuple[int, int, int, int] | None:
    try:
        bounds = quartz.CGDisplayBounds(display)
        origin, size = bounds.origin, bounds.size
        x, y = round(float(origin.x)), round(float(origin.y))
        width, height = round(float(size.width)), round(float(size.height))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


# 函数用途: 某个全局点所在显示器的缩放（像素宽 ÷ 点宽）；点不在任何显示器上返回 None。
def _display_scale(quartz: Any, point: tuple[float, float]) -> float | None:
    error, displays, count = quartz.CGGetDisplaysWithPoint(point, 1, None, None)
    if error or not count:
        return None
    mode = quartz.CGDisplayCopyDisplayMode(displays[0])
    width = quartz.CGDisplayModeGetWidth(mode) if mode is not None else 0
    return quartz.CGDisplayModeGetPixelWidth(mode) / width if width else None


# 函数用途: 当前 macOS 版本号元组；取不到返回空元组。
def _macos_version() -> tuple[int, ...]:
    release = platform.mac_ver()[0]
    return tuple(int(part) for part in release.split(".") if part.isdigit())


__all__ = ["MacBackend", "MacFrameworks", "load_real_macos_frameworks"]
