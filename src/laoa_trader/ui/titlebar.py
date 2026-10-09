"""自绘标题栏 + 无边框窗口（**Windows 的原生交互一个都不丢**）。

为什么要自己画标题栏（主人 2026-10-10）
----------------------------------------
原话："新程序运行框体和颜色都很别扭……比如标题栏现在还用的 windows 自带的，
一点都不搭。" 深色皮肤配一条浅色的系统标题栏，就像"穿着西装戴草帽"——
窗口最上面那 30 像素是用户第一眼看到的东西，它必须是我们自己的设计。
所以这一版把标题栏（软件名 + 版本标签 + 最小化/最大化/关闭）画进客户区。

**关键取舍：不丢系统行为。** 常见的做法是 `Qt.FramelessWindowHint` 一设了事，
代价是拖动、双击最大化、边缘缩放、贴边吸附、Aero 抖动、任务栏行为全都不见了——
用户会立刻觉得"这软件不对劲"，比标题栏难看严重得多。这里用的是另一条路
（Windows 上叫 "custom chrome"）：

1. **窗口样式一个都不改**（仍然有 `WS_CAPTION / WS_THICKFRAME / WS_SYSMENU`），
   所以 DWM 的阴影、圆角、动画、贴边吸附、Snap 布局全是系统给的，原汁原味；
2. 收到 `WM_NCCALCSIZE` 时把**整个窗口矩形都声明成客户区**（返回 0）——
   系统于是"没有非客户区可画"，那条原生标题栏就不见了，而窗口样式还在；
3. 收到 `WM_NCHITTEST` 时自己回答"这一点的性质"：
   - 四边/四角 → `HTLEFT/HTTOP…`：**系统**来做缩放（DPI、最小尺寸、吸附都对）；
   - 标题栏区域 → `HTCAPTION`：**系统**来做拖动、双击最大化、右键系统菜单；
   - 那三个按钮 → `HTCLIENT`：交给 Qt，走我们自己的 `QPushButton` 逻辑；
   - 其它 → 不回答，交给 Qt 当普通客户区。

这样"看着是自己画的，动起来是系统的"。设置页的「窗口外观」可以随时切回
系统标题栏（见 `config.WINDOW_FRAMES`）——万一这台机器上自绘出了问题，
用户不用等我就能自己绕开。

这台机器上没法验证的事（**如实写在这里**）
------------------------------------------
Agent 这边只有 Linux + 离屏渲染，**Windows 上的拖动/缩放/最大化只能在用户的机器上验**。
所以做了三层兜底：
- 任何一步出异常 → 立刻停用自绘（`self.disabled`），退回系统标题栏继续跑，只记日志；
- `WM_NCCALCSIZE` 有没有真的让出非客户区，用 `nc_applied` 记着，启动后写进日志；
- 客户区尺寸与 Qt 期望的不一致时（Qt 以为还有 31 像素的标题栏，见 `sync_geometry`）
  用 `SetWindowPos` 校正一次 —— 这一步把"窗口被 Qt 撑大 31 像素"这类问题挡住。

非 Windows 平台（开发机/离屏测试）走 Qt 自己的 `startSystemMove()` 拖动，
控制器不做任何 ctypes 调用。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizeGrip,
    QWidget,
)

from laoa_trader.log import get_logger
from laoa_trader.ui import theme as theme_mod

logger = get_logger(__name__)

#: 标题栏高度（像素）。38 是"够气派但不吃屏幕"的那一档：
#: 1366×768 的笔记本上，客户区高度本来就紧张，再高就该挤压表格了。
TITLEBAR_HEIGHT = 38
#: 最小化/最大化/关闭三个按钮的宽度（Windows 11 的比例：宽 > 高，看着才像）
WINDOW_BUTTON_WIDTH = 46

#: 窗口外观两种模式（与 `config.WINDOW_FRAMES` 同一份取值）
FRAME_CUSTOM = "custom"
FRAME_SYSTEM = "system"

# ── Windows 消息与常量（只在这台机器是 Windows 时才会用到）─────────────
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200


class _RECT(ctypes.Structure):
    """Win32 `RECT`。"""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    """Win32 `NCCALCSIZE_PARAMS`（`rgrc[0]` 就是"系统建议的窗口矩形"）。"""

    _fields_ = [
        ("rgrc", _RECT * 3),
        ("lppos", ctypes.c_void_p),
    ]


class _MSG(ctypes.Structure):
    """Win32 `MSG` 里我们真正要读的那几个字段（后面的 time/pt 也留着，保持布局一致）。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


def _user32() -> Any:
    """取 user32（非 Windows 返回 None）。"""
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


class WindowButton(QAbstractButton):
    """自绘的窗口按钮（最小化 / 最大化-还原 / 关闭）。

    为什么不用 `QPushButton` + 字体符号（`─ □ ✕`）：字体符号在不同字号/DirectWrite
    下粗细和位置都不一样，Windows 11 那三根 1px 细线用字符画不出来（要么太粗、
    要么偏半像素发虚）。QPainter 画线是确定的像素，且悬停底色能精确控制
    （关闭悬停整块变红，是 Windows 用户的条件反射）。
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind                      # "min" / "max" / "close"
        self._maximized = False
        self.setObjectName({
            "min": "titleBarMin",
            "max": "titleBarMax",
            "close": "titleBarClose",
        }.get(kind, "titleBarButton"))
        self.setFixedSize(WINDOW_BUTTON_WIDTH, TITLEBAR_HEIGHT)
        # 不给焦点：点了最小化之后焦点不该停在按钮上（回车会再点一次）
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("最小化" if kind == "min" else "关闭")
        if kind == "max":
            self.set_maximized(False)

    # ── 状态 ──
    def set_maximized(self, value: bool) -> None:
        """在"最大化/还原"之间切图标（同一个按钮画两种图形）。"""
        self._maximized = bool(value)
        if self._kind == "max":
            self.setToolTip("还原" if self._maximized else "最大化")
        self.update()

    def is_maximized(self) -> bool:
        return self._maximized

    # ── 画 ──
    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt 接口
        colors = theme_mod.chrome_colors()
        hovered = self.underMouse()
        closing = self._kind == "close"
        painter = QPainter(self)
        try:
            background = None
            if closing and (hovered or self.isDown()):
                background = QColor(colors["close_hover"])
            elif hovered:
                background = QColor(colors["hover"])
            if background is not None:
                painter.fillRect(self.rect(), background)
            # 关闭按钮变红之后字形必须是白的（红底上再叠主题色会糊）
            color = QColor("#ffffff") if background is not None and closing \
                else QColor(colors["text"])
            pen = QPen(color)
            pen.setWidthF(1.0)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            # 半像素偏移：1px 的线落在整数坐标上会跨两行像素、看着发虚
            cx = self.width() / 2.0 + 0.5
            cy = self.height() / 2.0 + 0.5
            half = 4.0
            if self._kind == "min":
                painter.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
            elif self._kind == "close":
                painter.drawLine(QPointF(cx - half, cy - half),
                                 QPointF(cx + half, cy + half))
                painter.drawLine(QPointF(cx - half, cy + half),
                                 QPointF(cx + half, cy - half))
            elif self._maximized:                     # 还原：两个叠起来的方框
                painter.drawRect(QRect(int(cx - half), int(cy - half + 2),
                                       int(half * 2) - 1, int(half * 2) - 3))
                painter.drawLine(QPointF(cx - half + 2, cy - half),
                                 QPointF(cx + half, cy - half))
                painter.drawLine(QPointF(cx + half, cy - half),
                                 QPointF(cx + half, cy + half - 2))
            else:                                     # 最大化：一个方框
                painter.drawRect(QRect(int(cx - half), int(cy - half),
                                       int(half * 2), int(half * 2)))
        finally:
            painter.end()


class TitleBar(QWidget):
    """窗口最上面那一条：强调色小竖条 + 图标 + 软件名 + 版本标签 + 三个窗口按钮。

    它自己**不做任何窗口操作**：只发信号，由主窗口决定（见 `ui/app.py` 的接线）——
    这样这个控件可以被单独测试，也不会偷偷持有窗口的引用。
    """

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, title: str, tag: str = "", icon: QIcon | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        # 纯 QWidget 子类要自己开这个属性，否则样式表里的 background 不生效
        # （只有 QFrame/QAbstractButton 这类自带绘制的控件才不用）——一行漏掉就是"标题栏底色没变"
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLEBAR_HEIGHT)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(10)

        # 强调色小竖条：整条标题栏唯一的"彩"，正好把软件名框成一块铭牌
        accent = QFrame()
        accent.setObjectName("titleBarAccent")
        accent.setFixedSize(3, 16)
        row.addWidget(accent)

        if icon is not None and not icon.isNull():
            icon_label = QLabel()
            icon_label.setObjectName("titleBarIcon")
            icon_label.setFixedSize(18, 18)
            icon_label.setPixmap(icon.pixmap(18, 18))
            row.addWidget(icon_label)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("titleBarTitle")
        font = QFont(self.title_label.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1.0)
        self.title_label.setFont(font)
        row.addWidget(self.title_label)

        self.tag_label = QLabel(tag)
        self.tag_label.setObjectName("titleBarTag")
        row.addWidget(self.tag_label)

        row.addStretch(1)

        self.btn_min = WindowButton("min", self)
        self.btn_max = WindowButton("max", self)
        self.btn_close = WindowButton("close", self)
        for button in (self.btn_min, self.btn_max, self.btn_close):
            row.addWidget(button)

        self._buttons = (self.btn_min, self.btn_max, self.btn_close)
        self.btn_min.clicked.connect(self.minimize_requested)
        self.btn_max.clicked.connect(self.maximize_requested)
        self.btn_close.clicked.connect(self.close_requested)

        # 拖动兜底：Windows 上由 HTCAPTION 让**系统**拖（下面这两条根本收不到事件），
        # 其它平台、或自绘失效时靠这里（`startSystemMove` 也是系统级的移动循环）
        self._drag_origin: QPoint | None = None
        self.set_active(True)

    # ── 对外 ──
    def window_buttons(self) -> tuple[WindowButton, WindowButton, WindowButton]:
        """三个按钮（测试与主窗口接线都要能拿到）。"""
        return self.btn_min, self.btn_max, self.btn_close

    def set_maximized(self, value: bool) -> None:
        self.btn_max.set_maximized(value)

    def set_active(self, active: bool) -> None:
        """窗口被激活/失活时软件名的明暗（失活还亮着会让人以为窗口是活的）。"""
        colors = theme_mod.chrome_colors()
        self.title_label.setStyleSheet(
            "color: %s;" % (colors["text"] if active else colors["dim"]))
        self.tag_label.setStyleSheet("color: %s;" % colors["dim"])

    def hit_local(self, pos: QPoint) -> bool:
        """某个窗口内坐标是不是落在"标题栏的空白处"（按钮上不算：那是客户区）。"""
        if not self.geometry().contains(pos):
            return False
        for button in self._buttons:
            top_left = button.mapTo(self.parentWidget() or self, QPoint(0, 0))
            if QRect(top_left, button.size()).contains(pos):
                return False
        return True

    # ── 交互（兜底路径）──
    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            handle = window.windowHandle() if window is not None else None
            if handle is not None and hasattr(handle, "startSystemMove"):
                # 系统级的移动循环：贴边吸附、多屏 DPI 都由系统处理，比手写 setPos 稳
                self._drag_origin = event.globalPosition().toPoint()
                if handle.startSystemMove():
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ChromeController:
    """把"自绘标题栏"接到 Windows 消息上（非 Windows 平台什么都不做）。

    只用**一个**入口给主窗口调：`native_event()`（主窗口的 `nativeEvent` 转给它）。
    任何异常都会把 `disabled` 打开并记日志 —— 界面宁可退回系统标题栏，也不能因为
    窗口消息处理出错而崩掉（那是用户连关都关不掉的窗口）。
    """

    def __init__(self, window: Any, titlebar: TitleBar | None) -> None:
        self.window = window
        self.titlebar = titlebar
        self.enabled = sys.platform == "win32"
        self.disabled = not self.enabled
        #: `WM_NCCALCSIZE` 有没有真的把非客户区让出来（启动后写进日志）
        self.nc_applied = False
        self._user32 = _user32()
        self._hwnd: int | None = None
        self._logged = False

    # ── 生命周期 ──
    def attach(self) -> bool:
        """开始接管；返回"是不是真的在自绘"。"""
        if not self.enabled:
            return False
        try:
            handle = self.window.windowHandle()
            if handle is None:
                # 窗口还没 show 出来时拿不到 HWND：等下一次（`sync_geometry` 会再过一遍）
                return False
            self._hwnd = int(handle.winId())
            return self._hwnd != 0
        except Exception as exc:  # noqa: BLE001
            self._fail("取窗口句柄失败", exc)
            return False

    def detach(self) -> None:
        """切回系统标题栏：不再回答命中测试（原生标题栏原样回来）。"""
        self._hwnd = None
        self.nc_applied = False
        logger.info("窗口外观：已切回系统标题栏（自绘标题栏不再接管窗口消息）")

    def _fail(self, what: str, exc: BaseException) -> None:
        self.disabled = True
        logger.warning(f"自绘标题栏停用（{what}）：{type(exc).__name__}: {exc}")

    # ── 尺寸校正 ──
    def resize_border(self) -> int:
        """系统认定的"可拖动缩放"的边宽（含 DPI 缩放）。"""
        if self._user32 is None or not self._hwnd:
            return 0
        try:
            padded = int(self._user32.GetSystemMetrics(SM_CXPADDEDBORDER))
            frame = int(self._user32.GetSystemMetrics(SM_CXSIZEFRAME))
            return max(4, padded + frame)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"取缩放边框宽度失败（按 8 像素处理）：{exc}")
            return 8

    def window_rect(self) -> tuple[int, int, int, int] | None:
        """窗口外框（屏幕坐标）。"""
        if self._user32 is None or not self._hwnd:
            return None
        try:
            rect = _RECT()
            if not self._user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
                return None
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"取窗口外框失败：{exc}")
            return None

    def client_offset(self) -> tuple[int, int]:
        """客户区左上角在**窗口外框**里的偏移。

        为什么要算：`WM_NCCALCSIZE` 被我们接住时客户区 == 外框（偏移 0,0）；
        万一没接住（系统或 Qt 抢先处理了），客户区就落在原生标题栏下面，
        此时所有基于 Qt 坐标的命中测试都要加上这个偏移，否则标题栏会"错位 31 像素"。
        """
        if self._user32 is None or not self._hwnd:
            return (0, 0)
        try:
            client = _RECT()
            if not self._user32.GetClientRect(self._hwnd, ctypes.byref(client)):
                return (0, 0)
            frame = self.window_rect()
            if frame is None:
                return (0, 0)
            _, _, frame_w, frame_h = frame
            client_w = client.right - client.left
            client_h = client.bottom - client.top
            border = max(0, (frame_w - client_w) // 2)
            return (border, max(0, frame_h - client_h - border))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"算客户区偏移失败（按 0 处理）：{exc}")
            return (0, 0)

    def sync_geometry(self) -> str:
        """把窗口外框校正到 Qt 期望的客户区尺寸；返回处理结果（给日志用）。

        背景：Qt 看着 `WS_CAPTION` 就以为窗口上面还有 31 像素的标题栏，
        `resize(w, h)` 于是去 `SetWindowPos` 了一个 (w+16, h+31) 的外框。
        自绘模式下客户区**就是**外框，所以窗口会比要求的大一圈。
        量一下、对不上就用 `SetWindowPos` 直接改回 Qt 要的尺寸，
        改动之后 Qt 会收到 WM_SIZE，双方重新对齐（不会来回打架）。
        """
        if not self.enabled or self.disabled or not self.attach():
            return "off"
        if self._hwnd is None:
            return "off"
        try:
            if not self.nc_applied:
                # 接管晚了（窗口已经建出来、WM_NCCALCSIZE 已经过去）→ 让系统重算一次
                self.force_frame_recalc()
            if self._user32.IsZoomed(self._hwnd):        # 最大化时尺寸归系统管
                return "maximized"
            frame = self.window_rect()
            if frame is None:
                return "unknown"
            x, y, width, height = frame
            want_w = int(self.window.width())
            want_h = int(self.window.height())
            want_x = int(self.window.x())
            want_y = int(self.window.y())
            off_x, off_y = self.client_offset()
            # 客户区_offset 归 0 说明 WM_NCCALCSIZE 生效了（Qt 坐标 == 外框坐标）
            same_box = off_x == 0 and off_y == 0
            if same_box:
                target = (want_x, want_y, want_w, want_h)
            else:
                border = max(0, off_x)
                target = (want_x - border, want_y - off_y,
                          want_w + border * 2, want_h + off_y + border)
            if (abs(x - target[0]) <= 1 and abs(y - target[1]) <= 1
                    and abs(width - target[2]) <= 1 and abs(height - target[3]) <= 1):
                return "ok" if same_box else "offset"
            self._user32.SetWindowPos(
                self._hwnd, 0, target[0], target[1], target[2], target[3],
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
            logger.info(
                "自绘标题栏：窗口外框已校正 "
                f"{width}×{height} → {target[2]}×{target[3]}"
                f"（客户区偏移 {off_x},{off_y}）")
            return "fixed" if same_box else "offset"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"校正窗口外框失败（不影响使用）：{exc}")
            return "error"

    def force_frame_recalc(self) -> None:
        """让系统**重算一次非客户区**：把 `WM_NCCALCSIZE` 再送一遍。

        为什么需要这一手：窗口消息是在窗口创建/显示前后送到窗口过程的，而我们的接管
        可能晚一步（构造顺序、Qt 自己的处理）。`SWP_FRAMECHANGED` 会强制 Windows
        重新计算非客户区 —— 于是 `WM_NCCALCSIZE` 会再进来一次，"晚了也能补上"。
        """
        if self._user32 is None or not self._hwnd:
            return
        try:
            self._user32.SetWindowPos(
                self._hwnd, 0, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE
                | SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"强制重算非客户区失败（不影响使用）：{exc}")

    def report(self) -> str:
        """一行"自绘标题栏到底生效了没"的诊断文本（写日志、也进自检报告）。"""
        if not self.enabled:
            return "自绘标题栏：非 Windows 平台，未接管窗口消息"
        if self.disabled:
            return "自绘标题栏：已停用（接管失败，正在用系统标题栏）"
        state = "生效" if self.nc_applied else "未生效（非客户区没让出来）"
        offset = self.client_offset()
        return (f"自绘标题栏：{state}；客户区偏移 {offset[0]},{offset[1]}；"
                f"缩放边框 {self.resize_border()} 像素")

    # ── 命中测试 ──
    def _is_maximized(self) -> bool:
        try:
            return bool(self.window.isMaximized())
        except Exception:  # noqa: BLE001
            return False

    def _titlebar_box(self) -> tuple[int, int, int, int] | None:
        """标题栏在**窗口外框坐标**里的矩形。"""
        titlebar = self.titlebar
        if titlebar is None or not titlebar.isVisible():
            return None
        try:
            parent = titlebar.parentWidget() or self.window
            top_left = titlebar.mapTo(self.window, QPoint(0, 0))
            off_x, off_y = self.client_offset()
            return (top_left.x() + off_x, top_left.y() + off_y,
                    titlebar.width(), titlebar.height())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"算标题栏位置失败：{exc}")
            return None

    def _in_window_button(self, x: int, y: int) -> bool:
        titlebar = self.titlebar
        if titlebar is None:
            return False
        try:
            off_x, off_y = self.client_offset()
            for button in titlebar.window_buttons():
                top_left = button.mapTo(self.window, QPoint(0, 0))
                box = QRect(top_left.x() + off_x, top_left.y() + off_y,
                            button.width(), button.height())
                if box.contains(x, y):
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"算窗口按钮位置失败：{exc}")
        return False

    def hit_test(self, x: int, y: int) -> int | None:
        """屏幕坐标 (x, y) → 命中结果；`None` 表示"不表态，交给 Qt"。

        判据顺序**不能反**：先缩放边（四角优先），再标题栏，再按钮。
        反过来会让"贴着窗口最上面拖动"变成"想缩放却拖动了窗口"。
        """
        frame = self.window_rect()
        if frame is None:
            return None
        left, top, width, height = frame
        local_x = x - left
        local_y = y - top
        if self._is_maximized():
            border = 0                       # 最大化时四边不该能拖（与系统一致）
        else:
            border = self.resize_border()
        if border and 0 <= local_x < width and 0 <= local_y < height:
            near_left = local_x < border
            near_right = local_x >= width - border
            near_top = local_y < border
            near_bottom = local_y >= height - border
            if near_top and near_left:
                return HTTOPLEFT
            if near_top and near_right:
                return HTTOPRIGHT
            if near_bottom and near_left:
                return HTBOTTOMLEFT
            if near_bottom and near_right:
                return HTBOTTOMRIGHT
            if near_left:
                return HTLEFT
            if near_right:
                return HTRIGHT
            if near_top:
                return HTTOP
            if near_bottom:
                return HTBOTTOM
        box = self._titlebar_box()
        if box is not None:
            bx, by, bw, bh = box
            if bx <= local_x < bx + bw and by <= local_y < by + bh:
                if self._in_window_button(local_x, local_y):
                    return HTCLIENT          # 按钮归 Qt 管（有悬停、有点击）
                return HTCAPTION             # 拖动/双击最大化/右键菜单归系统管
        return None

    def native_event(self, event_type: Any, message: Any) -> tuple[bool, int]:
        """主窗口 `nativeEvent()` 的转接；返回 `(已处理, 结果)`。"""
        if not self.enabled or self.disabled or self._hwnd is None:
            return False, 0
        try:
            msg = _MSG.from_address(int(message))
        except Exception as exc:  # noqa: BLE001
            self._fail("读窗口消息失败", exc)
            return False, 0
        try:
            if msg.message == WM_NCCALCSIZE and msg.wParam:
                return self._on_calc_size(msg)
            if msg.message == WM_NCHITTEST:
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                result = self.hit_test(x, y)
                if result is None:
                    return False, 0
                if not self._logged:
                    self._logged = True
                    logger.info(f"自绘标题栏：{self.report()}")
                return True, result
        except Exception as exc:  # noqa: BLE001 - 窗口消息里绝不能抛出去
            self._fail("处理窗口消息失败", exc)
        return False, 0

    def _on_calc_size(self, msg: _MSG) -> tuple[bool, int]:
        """`WM_NCCALCSIZE`：把整个窗口矩形声明成客户区（等于把原生标题栏"擦掉"）。

        最大化那一条不能漏：最大化时系统给的窗口矩形是**工作区再往外撑一圈边框**，
        直接返回 0 会让内容被切掉几个像素（右边和下边尤其明显）——
        所以最大化时把建议矩形往内收一圈边框，正好落回工作区。
        """
        try:
            params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
            if self._user32 is not None and self._user32.IsZoomed(msg.hwnd):
                border = self.resize_border()
                params.rgrc[0].left += border
                params.rgrc[0].top += border
                params.rgrc[0].right -= border
                params.rgrc[0].bottom -= border
            if not self.nc_applied:
                self.nc_applied = True
                logger.info("自绘标题栏：已接管非客户区（原生标题栏不再绘制）")
            return True, 0
        except Exception as exc:  # noqa: BLE001
            self._fail("处理 WM_NCCALCSIZE 失败", exc)
            return False, 0


def apply_window_frame(window: Any, mode: str, *, titlebar: TitleBar | None,
                       controller: ChromeController | None = None) -> tuple[str, Any]:
    """按 `mode` 装/卸自绘标题栏；返回 `(实际生效的模式, 控制器)`。

    "实际生效"可能与要求的不同：非 Windows 平台上自绘标题栏照画（拖动靠 Qt 兜底），
    但窗口消息不接管。调用方按返回值决定要不要显示状态栏提示。
    """
    want = str(mode or "").strip().lower()
    if want not in (FRAME_CUSTOM, FRAME_SYSTEM):
        want = FRAME_CUSTOM
    if titlebar is not None:
        titlebar.setVisible(want == FRAME_CUSTOM)
    if want == FRAME_SYSTEM:
        if controller is not None:
            controller.detach()
        return FRAME_SYSTEM, controller
    controller = controller or ChromeController(window, titlebar)
    controller.attach()
    # 非 Windows：给窗口右下角塞一个 QSizeGrip（系统不给缩放边，只能自己来；
    # 这是给开发机用的，发行版只有 Windows）
    if sys.platform != "win32" and not getattr(window, "_luweik_size_grip", None):
        try:
            grip = QSizeGrip(window)
            grip.setObjectName("windowSizeGrip")
            grip.resize(14, 14)
            grip.move(max(0, window.width() - 14), max(0, window.height() - 14))
            grip.raise_()
            window._luweik_size_grip = grip
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"加缩放手柄失败（不影响使用）：{exc}")
    return FRAME_CUSTOM, controller
