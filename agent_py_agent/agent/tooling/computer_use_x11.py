# LLM: J16 片 B 的 Linux X11 真后端：只用 python-xlib（pywinctl 带入）、mss、RapidOCR、pyautogui 的公开接口，给
#   screen_observation.ScreenObserver 提供"窗口列表（按叠放底→顶）、上层矩形、窗口截图、OCR 文字区域、点击"五个事实；不切焦点、
#   不激活、不移动鼠标（click 除外）。所有桌面库都在方法里惰性 import：没有 DISPLAY 的进程也能构造后端，错误在调用时变成
#   ObservationError。坐标：X11 全局坐标就是像素，缩放固定 1；几何取客户区（translate_coords 到根窗口），遮挡矩形加 _NET_FRAME_EXTENTS 边框。
#   不调上游 computer_control_mcp 的内部对象。真实验证只在 Linux 车道容器（Xvfb + openbox）做，Mac 上只用假后端。
# 模块用途: 把 X11 桌面变成观察核心能理解的几个事实，自己不做任何判定。
from __future__ import annotations

from typing import Any

from .screen_observation import TextRegion, WindowInfo
from .screen_observation_store import WindowGeometry
from .screen_region_digest import PixelBuffer

# _NET_WM_DESKTOP 里"所有桌面"的 EWMH 协议约定值（协议常数，不进常数目录）
_ALL_DESKTOPS_PROTOCOL_VALUE = 0xFFFFFFFF


# LLM: display 可注入（测试 / 复用），默认第一次用时按 DISPLAY 打开；每个方法都重新读属性，不缓存窗口状态（复核要新鲜事实）。
# 类用途: 观察核心的 X11 后端。
class X11Backend:
    def __init__(self, display: Any | None = None) -> None:
        self._display = display
        self._ocr_engine: Any | None = None

    # 函数用途: 按 _NET_CLIENT_LIST_STACKING 底→顶列出客户端窗口及其当下事实。
    def list_windows(self) -> list[WindowInfo]:
        display, root = self._root()
        current = _first(self._property(root, "_NET_CURRENT_DESKTOP"))
        windows = []
        for xid in self._property(root, "_NET_CLIENT_LIST_STACKING"):
            info = self._window_info(display, root, int(xid), current)
            if info is not None:
                windows.append(info)
        return windows

    # 函数用途: 叠放在 native_id 之上的窗口矩形（全局像素，含边框）：客户端列表里排在它后面且可见的，加 override-redirect 的可见子窗口。
    def above_rects(self, native_id: object) -> list[tuple[int, int, int, int]]:
        display, root = self._root()
        stacking = [int(xid) for xid in self._property(root, "_NET_CLIENT_LIST_STACKING")]
        position = stacking.index(int(native_id)) if int(native_id) in stacking else len(stacking)
        rects = [self._frame_rect(display, root, xid) for xid in stacking[position + 1:] if self._viewable(display, xid)]
        for child in root.query_tree().children:
            attributes = child.get_attributes()
            if attributes.override_redirect and attributes.map_state == self._x().IsViewable:
                rects.append(self._frame_rect(display, root, child.id))
        return [rect for rect in rects if rect is not None]

    # 函数用途: 用 mss 按窗口客户区矩形截图（RGB 原始字节）。
    def capture(self, info: WindowInfo) -> PixelBuffer:
        import mss

        origin, size = info.geometry.origin, info.geometry.size
        with mss.mss() as grabber:
            shot = grabber.grab({"left": int(origin[0]), "top": int(origin[1]), "width": int(size[0]), "height": int(size[1])})
        return PixelBuffer(int(shot.width), int(shot.height), bytes(shot.rgb))

    # 函数用途: RapidOCR 公开调用 RapidOCR()(image) 的文字区域；box 四点取外框。
    def ocr(self, buffer: PixelBuffer) -> list[TextRegion]:
        import numpy

        image = numpy.frombuffer(buffer.rgb, dtype=numpy.uint8).reshape(buffer.height, buffer.width, 3)[:, :, ::-1]
        result, _elapsed = self._engine()(numpy.ascontiguousarray(image))
        regions = []
        for box, text, _score in result or []:
            xs, ys = [float(point[0]) for point in box], [float(point[1]) for point in box]
            regions.append(TextRegion(str(text), (round(min(xs)), round(min(ys)), round(max(xs) - min(xs)), round(max(ys) - min(ys)))))
        return regions

    # 函数用途: 在全局像素坐标点一下（复核通过后立刻调用，中间不做别的 I/O）。
    def click(self, x: int, y: int) -> None:
        import pyautogui

        pyautogui.click(int(x), int(y))

    # 函数用途: 惰性创建并缓存 OCR 引擎（加载模型较慢）。
    def _engine(self) -> Any:
        if self._ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr_engine = RapidOCR()
        return self._ocr_engine

    # 函数用途: 惰性打开 X 连接并返回 (display, root)。
    def _root(self) -> tuple[Any, Any]:
        if self._display is None:
            from Xlib import display as xdisplay

            self._display = xdisplay.Display()
        return self._display, self._display.screen().root

    # 函数用途: Xlib 常量模块（IsViewable 等）。
    @staticmethod
    def _x() -> Any:
        from Xlib import X

        return X

    # 函数用途: 读一个窗口属性的完整值列表；没有返回空列表。
    def _property(self, window: Any, name: str) -> list[Any]:
        display = self._display
        reply = window.get_full_property(display.intern_atom(name), self._x().AnyPropertyType)
        return list(reply.value) if reply is not None else []

    # 函数用途: 一个客户端窗口的 WindowInfo；窗口已消失（XError）返回 None。
    def _window_info(self, display: Any, root: Any, xid: int, current: object) -> WindowInfo | None:
        from Xlib.error import XError

        window = display.create_resource_object("window", xid)
        try:
            attributes, geometry, origin = window.get_attributes(), window.get_geometry(), root.translate_coords(window, 0, 0)
            states = self._property(window, "_NET_WM_STATE")
            types = self._property(window, "_NET_WM_WINDOW_TYPE")
            desktop = _first(self._property(window, "_NET_WM_DESKTOP"))
            title = self._title(window)
        except XError:
            return None
        intern = display.intern_atom
        return WindowInfo(
            native_id=xid, title=title,
            geometry=WindowGeometry((int(origin.x), int(origin.y)), (int(geometry.width), int(geometry.height)), (1, 1)),
            viewable=attributes.map_state == self._x().IsViewable, hidden=intern("_NET_WM_STATE_HIDDEN") in states,
            desktop=None if desktop in (None, _ALL_DESKTOPS_PROTOCOL_VALUE) else desktop, current_desktop=current,
            normal=not types or intern("_NET_WM_WINDOW_TYPE_NORMAL") in types,
        )

    # 函数用途: 窗口标题：优先 UTF-8 的 _NET_WM_NAME，退回 WM_NAME；只作外部数据。
    def _title(self, window: Any) -> str:
        raw = self._property(window, "_NET_WM_NAME")
        if raw:
            return bytes(raw).decode("utf-8", errors="replace")
        name = window.get_wm_name()
        return name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name or "")

    # 函数用途: 窗口是否可见（已映射且没有 _NET_WM_STATE_HIDDEN）；已消失算不可见。
    def _viewable(self, display: Any, xid: int) -> bool:
        from Xlib.error import XError

        window = display.create_resource_object("window", xid)
        try:
            return (window.get_attributes().map_state == self._x().IsViewable
                    and display.intern_atom("_NET_WM_STATE_HIDDEN") not in self._property(window, "_NET_WM_STATE"))
        except XError:
            return False

    # 函数用途: 窗口含边框的全局矩形（客户区 + _NET_FRAME_EXTENTS）；已消失返回 None。
    def _frame_rect(self, display: Any, root: Any, xid: int) -> tuple[int, int, int, int] | None:
        from Xlib.error import XError

        window = display.create_resource_object("window", xid)
        try:
            geometry, origin = window.get_geometry(), root.translate_coords(window, 0, 0)
            extents = [int(value) for value in self._property(window, "_NET_FRAME_EXTENTS")[:4]] or [0, 0, 0, 0]
        except XError:
            return None
        left, right, top, bottom = (extents + [0, 0, 0, 0])[:4]
        return (int(origin.x) - left, int(origin.y) - top, int(geometry.width) + left + right, int(geometry.height) + top + bottom)


# 函数用途: 属性值列表的第一项，没有返回 None。
def _first(values: list[Any]) -> object:
    return values[0] if values else None


__all__ = ["X11Backend"]
