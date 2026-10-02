"""J16 片 E：macOS 屏幕观察后端，全用假 Quartz / 假 ScreenCaptureKit / 假 mss，从不碰真实屏幕、也不触发屏幕录制授权弹窗。

锁定（ae 设计评审 2026-10-02）：
1. 列窗口先做屏幕录制权限预检（只查不弹窗），没授权直接 screen_recording_not_permitted，不去列窗口；前→后翻成底→顶，
   身份 (窗口号, PID)，layer==0 才算普通窗口，最小化 / alpha=0 / 不在任何显示器上都算不可见（not_viewable）。
2. 遮挡只看压在目标前面的窗口（OnScreenAboveWindow），alpha=0 的不算；点击前复核时点击点被压住 → stale。
3. scale 按窗口中心所在显示器取（副屏可以是负坐标、缩放 1），主路径按外框 × 显示器缩放出图，frame.scale 由核心按截图算。
4. ScreenCaptureKit 拿不到时退回 mss 区域截图（名义分辨率，frame.scale=1），结果顶层带 capture_fallback{reason}；
   窗口不在可分享内容里 → capture_failed 不回退；用户拒绝授权按 NSError domain+code 判，不看文字；回调超时后晚到的结果
   不会被下一次调用误用；观察走主路径、复核退回区域截图 → scale 变了判 stale。
5. 点击前（复核之前）先查辅助功能权限：没授权或绑定导入失败 → accessibility_not_permitted，不重新列窗口、不点击。
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_observation import parse_observation
from agent_py_agent.agent.tooling import computer_use_macos
from agent_py_agent.agent.tooling.computer_use_backends import select_backend
from agent_py_agent.agent.tooling.computer_use_macos import MacBackend, MacFrameworks
from agent_py_agent.agent.tooling.computer_use_x11 import X11Backend
from agent_py_agent.agent.tooling.screen_observation import (
    ObservationError,
    ScreenObserver,
    TextRegion,
)
from agent_py_agent.agent.tooling.screen_observation_store import WindowInstanceRegistry
from agent_py_agent.tests.test_screen_observation_core import _context, _meta

FORM, PANEL, BACKDROP, MENUBAR = 101, 102, 103, 104
FORM_RECT = (100, 200, 160, 100)  # 全局点：主屏（Retina，缩放 2）上的表单窗口
PATCH = (20, 10, 40, 12)  # 表单里“提交”按钮的位置（窗口内的点）
SCK_DOMAIN = "com.apple.ScreenCaptureKit.SCStreamErrorDomain"


# 类用途: 假 Quartz：列窗口（前→后）、显示器与显示模式、权限预检、CGImage 读取；常量与真实 pyobjc 同名。
class _Quartz:
    kCGWindowListOptionAll = 0
    kCGWindowListOptionOnScreenAboveWindow = 1 << 1
    kCGWindowListOptionOnScreenBelowWindow = 1 << 2
    kCGWindowListExcludeDesktopElements = 1 << 4
    kCGNullWindowID = 0
    kCGWindowNumber, kCGWindowOwnerPID, kCGWindowName = "kCGWindowNumber", "kCGWindowOwnerPID", "kCGWindowName"
    kCGWindowLayer, kCGWindowAlpha = "kCGWindowLayer", "kCGWindowAlpha"
    kCGWindowBounds, kCGWindowIsOnscreen = "kCGWindowBounds", "kCGWindowIsOnscreen"

    def __init__(self):
        self.permitted = True
        self.windows = [_row(MENUBAR, (0, 0, 1512, 25), layer=24, name="Menubar"), _row(FORM, FORM_RECT),
                        _row(BACKDROP, (50, 150, 400, 300), name="背景")]
        # 显示器编号 → (全局点矩形, 像素宽, 点宽)：主屏 Retina；副屏在主屏左边，原点为负，缩放 1
        self.displays = {1: ((0, 0, 1512, 982), 3024, 1512), 2: ((-1920, 0, 1920, 1080), 1920, 1920)}
        self.list_calls = []

    def CGPreflightScreenCaptureAccess(self):
        return self.permitted

    def CGRequestScreenCaptureAccess(self):
        raise AssertionError("后端绝不能触发屏幕录制授权弹窗")

    def CGWindowListCopyWindowInfo(self, option, relative):
        self.list_calls.append((option, relative))
        if option == self.kCGWindowListOptionAll | self.kCGWindowListExcludeDesktopElements and relative == self.kCGNullWindowID:
            return [dict(row) for row in self.windows]
        index = [row[self.kCGWindowNumber] for row in self.windows].index(relative)
        rows = {self.kCGWindowListOptionOnScreenAboveWindow: self.windows[:index],
                self.kCGWindowListOptionOnScreenBelowWindow: self.windows[index + 1:]}[option]
        return [dict(row) for row in rows if row.get(self.kCGWindowIsOnscreen)]

    def CGGetDisplaysWithPoint(self, point, max_count, displays, count):
        hits = [key for key, (rect, _px, _pt) in self.displays.items()
                if rect[0] <= point[0] < rect[0] + rect[2] and rect[1] <= point[1] < rect[1] + rect[3]][:max_count]
        return 0, tuple(hits), len(hits)

    def CGDisplayCopyDisplayMode(self, display):
        return SimpleNamespace(display=display)

    def CGDisplayModeGetWidth(self, mode):
        return self.displays[mode.display][2]

    def CGDisplayModeGetPixelWidth(self, mode):
        return self.displays[mode.display][1]

    def CGImageGetWidth(self, image):
        return image.width

    def CGImageGetHeight(self, image):
        return image.height

    def CGImageGetBitsPerPixel(self, image):
        return image.bits

    def CGImageGetBytesPerRow(self, image):
        return image.stride

    def CGImageGetDataProvider(self, image):
        return image

    def CGDataProviderCopyData(self, provider):
        return provider.data

    # 函数用途: 按窗口号找到一行窗口信息并改字段（测试用）。
    def update(self, number, **fields):
        row = next(row for row in self.windows if row[self.kCGWindowNumber] == number)
        for key, value in fields.items():
            if value is None:
                row.pop(key, None)
            else:
                row[key] = value


# 函数用途: 造一行 CGWindowListCopyWindowInfo 的窗口信息；facts 可改 pid / layer / alpha / onscreen。
def _row(number, rect, name="表单", **facts):
    facts = {"pid": 500, "layer": 0, "alpha": 1.0, "onscreen": True, **facts}
    q = _Quartz
    row = {q.kCGWindowNumber: number, q.kCGWindowOwnerPID: facts["pid"], q.kCGWindowLayer: facts["layer"], q.kCGWindowAlpha: facts["alpha"],
           q.kCGWindowName: name, q.kCGWindowBounds: {"X": rect[0], "Y": rect[1], "Width": rect[2], "Height": rect[3]}}
    if facts["onscreen"]:
        row[q.kCGWindowIsOnscreen] = True
    return row


# 函数用途: 造一张每像素 32 位 BGRA、每行带对齐填充的假 CGImage（浅灰底，“提交”按钮处深色）。
def _image(width, height, scale, pad=8):
    canvas = bytearray(bytes((240, 240, 240, 255)) * (width * height))
    px, py, pw, ph = (round(value * scale) for value in PATCH)
    for y in range(py, py + ph):
        canvas[(y * width + px) * 4:(y * width + px + pw) * 4] = bytes((30, 30, 30, 255)) * pw
    data = b"".join(bytes(canvas[y * width * 4:(y + 1) * width * 4]) + b"\0" * pad for y in range(height))
    return SimpleNamespace(width=width, height=height, bits=32, stride=width * 4 + pad, data=data)


# 类用途: 假 ScreenCaptureKit：可分享内容、内容过滤器、截图配置、单窗口截图；回调行为可换（立即 / 不回 / 报错）。
class _Kit:
    SCStreamErrorDomain = SCK_DOMAIN
    SCStreamErrorUserDeclined = -3801

    def __init__(self):
        self.shareable = [FORM, PANEL, BACKDROP]
        self.content_error = self.capture_error = None
        self.capture_none = False
        self.configs, self.content_handlers = [], []
        self.on_content = lambda handler: handler(SimpleNamespace(windows=lambda: [_scwindow(n) for n in self.shareable]), self.content_error)
        kit = self
        self.SCShareableContent = SimpleNamespace(
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=lambda exclude, onscreen, handler: (
                kit.content_handlers.append(handler), kit.on_content(handler)))
        self.SCContentFilter = SimpleNamespace(alloc=lambda: SimpleNamespace(initWithDesktopIndependentWindow_=lambda w: SimpleNamespace(window=w)))
        self.SCStreamConfiguration = SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: _Config()))
        self.SCScreenshotManager = SimpleNamespace(captureImageWithFilter_configuration_completionHandler_=self._capture)

    def _capture(self, content_filter, config, handler):
        self.configs.append(config)
        image = None if self.capture_none else _image(config.width, config.height, config.width / content_filter.window.points_width)
        handler(image, self.capture_error)


# 类用途: 假 SCStreamConfiguration：记下 setter 的值。
class _Config:
    def setWidth_(self, value):
        self.width = value

    def setHeight_(self, value):
        self.height = value

    def setShowsCursor_(self, value):
        self.shows_cursor = value

    def setIgnoreShadowsSingleWindow_(self, value):
        self.ignore_shadows = value


# 函数用途: 假 SCWindow（表单窗口宽 160 点，其它窗口宽度按表单算，只用于算假图里按钮的缩放）。
def _scwindow(number):
    return SimpleNamespace(windowID=lambda: number, points_width=FORM_RECT[2])


# 函数用途: 假 NSError（只有结构化的 domain / code 和一段说明文字）。
def _nserror(domain, code, text):
    return SimpleNamespace(domain=lambda: domain, code=lambda: code, localizedDescription=lambda: text)


# 类用途: 假 mss 截屏：按名义分辨率（点）出图，记下截了哪块。
class _Grabber:
    def __init__(self, grabs):
        self.grabs = grabs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, monitor):
        self.grabs.append(monitor)
        image = _image(monitor["width"], monitor["height"], 1, pad=0)
        rgb = bytearray(monitor["width"] * monitor["height"] * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = image.data[2::4], image.data[1::4], image.data[0::4]
        return SimpleNamespace(width=monitor["width"], height=monitor["height"], rgb=bytes(rgb))


# 函数用途: 假 OCR：在截图里按截图自己的缩放报出“提交”按钮的位置（截图像素）。
def _ocr(buffer):
    scale = buffer.width / FORM_RECT[2]
    return [TextRegion("提交", tuple(round(value * scale) for value in PATCH))]


# 函数用途: 假 AXIsProcessTrusted：给异常类就抛（模拟绑定导入失败），否则返回是否已授权。
def _accessibility(state):
    if isinstance(state, type) and issubclass(state, BaseException):
        raise state("ApplicationServices")
    return state


# 函数用途: 组装一套假框架的后端与观察核心；返回 (observer, quartz, kit, grabs, clicks)。
def _setup(*, kit="default", version=(15, 1), trusted=True):
    quartz, grabs, clicks = _Quartz(), [], []
    kit = _Kit() if kit == "default" else kit
    backend = MacBackend(MacFrameworks(quartz, kit, lambda: _Grabber(grabs), lambda x, y: clicks.append((x, y)), version,
                                       lambda: _accessibility(trusted)))
    backend._ocr = SimpleNamespace(read=_ocr)
    observer = ScreenObserver(backend, registry=WindowInstanceRegistry(boot="mac0"), clock=lambda: 1790000000.5)
    return observer, quartz, kit, grabs, clicks


# ---------------------------------------------------------------------------
# 列窗口与可见性
# ---------------------------------------------------------------------------

def test_permission_is_checked_before_listing_windows_and_never_prompts():
    observer, quartz, _kit, grabs, _clicks = _setup()
    quartz.permitted = False
    with pytest.raises(ObservationError) as denied:
        observer.observe()
    assert denied.value.code == "screen_recording_not_permitted" and quartz.list_calls == [] and grabs == [], "没授权就不列窗口、不截图"


def test_listing_is_bottom_to_top_with_identity_layer_and_visibility_facts():
    _observer, quartz, _kit, _grabs, _clicks = _setup()
    quartz.windows.insert(0, _row(201, (300, 300, 50, 50), alpha=0.0, name="透明"))
    quartz.windows.append(_row(202, (10, 10, 80, 80), onscreen=False, name="最小化"))
    quartz.windows.append(_row(203, (5000, 5000, 80, 80), name="不在任何显示器上"))
    quartz.windows.append(_row(204, (-1800, 100, 400, 300), pid=777, name="副屏"))
    windows = MacBackend(MacFrameworks(quartz, _Kit(), lambda: None, lambda x, y: None, (15, 1), lambda: True)).list_windows()
    assert [w.native_id for w in windows] == [(204, 777), (203, 500), (202, 500), (BACKDROP, 500), (FORM, 500), (MENUBAR, 500), (201, 500)], "前→后翻成底→顶"
    facts = {w.native_id[0]: (w.normal, w.viewable, w.geometry.scale, w.geometry.origin) for w in windows}
    assert facts[MENUBAR][0] is False and facts[FORM][0] is True, "layer==0 才算普通窗口"
    assert facts[FORM][1:] == (True, (2.0, 2.0), (100, 200)), "主屏 Retina"
    assert facts[204][1:] == (True, (1.0, 1.0), (-1800, 100)), "副屏：负原点、缩放 1"
    assert not facts[201][1] and not facts[202][1] and not facts[203][1], "alpha=0、不在屏上、不在任何显示器上都不可见"
    assert all(w.hidden is False and w.desktop is None for w in windows)


def test_minimized_or_transparent_targets_are_not_viewable_and_keep_their_identity():
    observer, quartz, _kit, _grabs, _clicks = _setup()
    alias = observer.observe()["window"]
    quartz.update(FORM, kCGWindowIsOnscreen=None)
    with pytest.raises(ObservationError) as minimized:
        observer.observe(alias)
    assert minimized.value.code == "not_viewable", "最小化 / 别的桌面 / 应用隐藏都算不可见，而不是找不到"
    quartz.update(FORM, kCGWindowIsOnscreen=True, kCGWindowAlpha=0.0)
    with pytest.raises(ObservationError) as transparent:
        observer.observe(alias)
    assert transparent.value.code == "not_viewable"
    quartz.update(FORM, kCGWindowAlpha=1.0)
    assert observer.observe(alias)["window"] == alias, "OptionAll 让窗口一直在列表里，实例身份不断"


# ---------------------------------------------------------------------------
# 主路径、多显示器、遮挡
# ---------------------------------------------------------------------------

def test_screencapturekit_on_retina_reports_window_image_and_clicks_in_global_points():
    observer, _quartz, kit, grabs, clicks = _setup()
    result = observer.observe()
    frame = result["frame"]
    assert (frame["capture"], frame["scale"], frame["origin"], frame["size"]) == ("window_image", [2.0, 2.0], [100, 200], [160, 100])
    assert "capture_fallback" not in result and grabs == []
    config = kit.configs[-1]
    assert (config.width, config.height, config.shows_cursor, config.ignore_shadows) == (320, 200, False, True)
    assert parse_observation(json.loads(json.dumps(result["my_agent_observation"])), _context()).frame["scale"] == [2.0, 2.0]
    observer.click_candidate(_meta(result))
    assert clicks == [(140, 216)], "按钮中心 (40,16) 点 + 窗口原点 (100,200)"


def test_a_window_on_the_secondary_display_uses_its_own_scale_and_negative_origin():
    observer, quartz, kit, _grabs, clicks = _setup()
    quartz.update(FORM, kCGWindowBounds={"X": -1700, "Y": 300, "Width": 160, "Height": 100})
    result = observer.observe()
    assert (result["frame"]["scale"], result["frame"]["origin"]) == ([1.0, 1.0], [-1700, 300])
    assert (kit.configs[-1].width, kit.configs[-1].height) == (160, 100), "按窗口所在显示器的缩放出图，不取主屏"
    observer.click_candidate(_meta(result))
    assert clicks == [(-1660, 316)]


def test_only_windows_in_front_of_the_target_occlude_it():
    observer, quartz, _kit, _grabs, clicks = _setup()
    quartz.windows.append(_row(301, (0, 0, 1000, 900), name="身后的大窗口"))
    assert observer.observe()["frame"]["occluded"] is False, "压在后面的窗口不算遮挡"
    quartz.windows.insert(0, _row(PANEL, (220, 250, 100, 100), layer=3, name="浮动面板"))
    assert observer.observe()["frame"]["occluded"] is True, "前面的窗口部分压住只记事实"
    quartz.windows.insert(0, _row(302, (0, 0, 1512, 982), alpha=0.0, name="全屏透明覆盖"))
    assert observer.observe()["frame"]["occluded"] is True, "alpha=0 的覆盖层不算，结论不变"
    quartz.update(PANEL, kCGWindowBounds={"X": 90, "Y": 190, "Width": 200, "Height": 200})
    with pytest.raises(ObservationError) as covered:
        observer.observe()
    assert covered.value.code == "occluded" and clicks == []


@pytest.mark.parametrize("state", [False, ImportError], ids=["not_trusted", "binding_missing"])
def test_click_without_accessibility_permission_fails_before_rechecking(state):
    observer, quartz, _kit, _grabs, clicks = _setup(trusted=state)
    meta = _meta(observer.observe())
    listed = len(quartz.list_calls)
    with pytest.raises(ObservationError) as denied:
        observer.click_candidate(meta)
    assert denied.value.code == "accessibility_not_permitted", "系统会悄悄丢掉点击，不能报已点击；确认不了也不点"
    assert len(quartz.list_calls) == listed and clicks == [], "复核之前就拒绝：不重新列窗口、不点击"


def test_a_window_moved_over_the_click_point_makes_the_click_stale():
    observer, quartz, _kit, _grabs, clicks = _setup()
    meta = _meta(observer.observe())
    quartz.windows.insert(0, _row(PANEL, (135, 210, 20, 20), layer=3, name="弹出菜单"))
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "stale" and clicks == []


# ---------------------------------------------------------------------------
# 回退与失败
# ---------------------------------------------------------------------------

# 各种“主路径拿不到”的情形：把一份假 ScreenCaptureKit 改成对应样子（返回 None 表示导入失败）。
_DEGRADATIONS = {
    "import_failed": lambda kit: None,
    "too_old": lambda kit: kit,
    "binding_missing": lambda kit: (delattr(kit, "SCScreenshotManager"), kit)[1],
    "error": lambda kit: (setattr(kit, "content_error", _nserror("NSOSStatusErrorDomain", -50, "参数错误")), kit)[1],
    "no_image": lambda kit: (setattr(kit, "capture_none", True), kit)[1],
    "timeout": lambda kit: (setattr(kit, "on_content", lambda handler: None), kit)[1],
}


# 函数用途: 按情形造一份退化的假框架，返回 (kit, 系统版本)；只有 too_old 用低于 14 的系统版本。
def _degraded(case):
    return _DEGRADATIONS[case](_Kit()), ((13, 6) if case == "too_old" else (15, 1))


@pytest.mark.parametrize("case,reason", [
    ("import_failed", "screencapturekit_unavailable"), ("too_old", "screencapturekit_too_old"),
    ("binding_missing", "screencapturekit_unavailable"), ("error", "screencapturekit_failed"),
    ("no_image", "screencapturekit_failed"), ("timeout", "screencapturekit_timeout"),
])
def test_fallback_uses_the_nominal_region_capture_and_always_gives_a_reason(monkeypatch, case, reason):
    monkeypatch.setattr(computer_use_macos, "MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS", 0.05)
    kit, version = _degraded(case)
    observer, _quartz, _kit, grabs, clicks = _setup(kit=kit, version=version)
    result = observer.observe()
    assert result["capture_fallback"] == {"reason": reason}
    assert (result["frame"]["capture"], result["frame"]["scale"]) == ("screen_region", [1.0, 1.0]), "mss 按名义分辨率出图，scale 由截图算出是 1"
    assert grabs == [{"left": 100, "top": 200, "width": 160, "height": 100}]
    observer.click_candidate(_meta(result))
    assert clicks == [(140, 216)], "回退截图上的候选也点得准"


def test_a_window_missing_from_shareable_content_fails_without_falling_back():
    observer, _quartz, kit, grabs, _clicks = _setup()
    kit.shareable = [BACKDROP]
    with pytest.raises(ObservationError) as info:
        observer.observe()
    assert info.value.code == "capture_failed" and grabs == [], "回退会把后面别的窗口拍成这个窗口的候选"


def test_user_declined_is_judged_by_error_domain_and_code_not_by_text():
    observer, _quartz, kit, grabs, _clicks = _setup()
    kit.content_error = _nserror(SCK_DOMAIN, -3801, "无关的说明文字")
    with pytest.raises(ObservationError) as declined:
        observer.observe()
    assert declined.value.code == "screen_recording_not_permitted" and grabs == [], "没权限时不回退（区域截图只会拍到壁纸）"
    kit.content_error = _nserror("NSCocoaErrorDomain", 3072, "The user declined screen recording")
    assert observer.observe()["capture_fallback"] == {"reason": "screencapturekit_failed"}, "文字里写着 declined 也不算"


def test_a_late_callback_from_a_timed_out_call_is_never_adopted_by_the_next_call(monkeypatch):
    monkeypatch.setattr(computer_use_macos, "MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS", 0.2)
    observer, _quartz, kit, _grabs, _clicks = _setup()
    kit.on_content = lambda handler: None
    assert observer.observe()["capture_fallback"] == {"reason": "screencapturekit_timeout"}
    first_handler = kit.content_handlers[-1]
    late = SimpleNamespace(windows=lambda: [_scwindow(FORM)])
    kit.on_content = lambda handler: threading.Timer(0.02, first_handler, args=(late, None)).start()
    second = observer.observe()
    assert second["capture_fallback"] == {"reason": "screencapturekit_timeout"} and second["frame"]["capture"] == "screen_region", \
        "上一次超时后晚到的结果不能被这一次当成自己的"
    pending = computer_use_macos._PendingCallback()
    pending.abandoned = True
    pending.deliver(late, None)
    assert pending.values is None and not pending.event.is_set(), "作废之后什么都写不进去"


def test_falling_back_between_observe_and_recheck_changes_the_scale_and_is_stale(monkeypatch):
    monkeypatch.setattr(computer_use_macos, "MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS", 0.05)
    observer, _quartz, kit, _grabs, clicks = _setup()
    meta = _meta(observer.observe())
    kit.on_content = lambda handler: None
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "stale" and clicks == [], "观察时 2.0、复核时 1.0：区域摘要不可比"


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------

def test_select_backend_by_platform_without_touching_the_screen():
    assert isinstance(select_backend("darwin"), MacBackend)
    assert isinstance(select_backend("linux"), X11Backend)
