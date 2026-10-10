"""鼠标滚轮防误触：滚轮停在下拉框/数字框上时**不改值**，让页面照常滚动。

为什么要有这个模块（主人 2026-10-10）
--------------------------------------
原话：「把设置页的鼠标滚轮功能给限制掉，会无意触发。」

「系统设置」页有四组、二十多个控件，里面一堆下拉框（界面主题、窗口外观、音色）
与数字框（自选上限、四个 T 阈值、止损止盈、天数）。Qt 的默认行为是：
**鼠标滚轮停在下拉框/数字框上就直接改它的值**（跟焦点无关）——
用户想往下翻看「T策略」那一组，滚轮正好划过「止损 %」，止损就被改成了别的数；
更糟的是他看不出来（数字只变了一点点），点了【保存设置】才生效。
这类误触在设置页上特别容易发生，因为这些控件是**纵向排一列**的。

怎么防（判据只有一条：**滚轮永远不改值**）
------------------------------------------
装一个应用级事件过滤器（`install()`），把落在下面这些控件上的滚轮事件一律拦下：
`QComboBox`、`QAbstractSpinBox`（含 QSpinBox / QDoubleSpinBox / QTimeEdit / QDateEdit）、
`QSlider`、`QDial`、`QTabBar`。

**为什么不写成"没焦点时才拦"**：焦点判据挡不住主人遇到的那个场景 —— 他刚点过某个下拉框
（于是它拿着焦点），接着往下滚页面，滚轮正好划过它 → 值还是被改了，而且他看不出来。
设置页里这些控件是纵向排一列的，"划过刚点过的那个"简直一定会发生。
所以这里的口径是**滚轮不承担"改设置"这件事**：
- 改值仍然有明确的两条路：**点开下拉框挑**、**在数字框里输入或用上下箭头**（键盘也能用：
  数字框里 ↑/↓ 就是加一减一）；下拉框展开后的那个列表**照常能滚**（它是独立弹层）；
- **拦下来之后不"吃掉"这个滚动**：把它转给最近的滚动区（设置页整页套着一层
  `QScrollArea`），所以"想翻页却改错了值"变成"页面照常往下翻"——这才符合直觉；
- **不动滚动条**：`QScrollBar` 也是 `QAbstractSlider`，但滚轮在滚动条／表格上本来就该滚动，
  所以这里只列 `QSlider`（写成 `QAbstractSlider` 会把滚动条一起圈进来，那是个坑）；
- 表格、列表、滚动区一律不拦（该滚的还是要滚）。

为什么用事件过滤器而不是给每个控件套子类：这个项目里下拉框/数字框散在五个页面、
十几个地方（还有将来新加的），漏一个就是一个"还会误触发"的洞；
过滤器只有这一处，判据也只有这一处。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDial,
    QSlider,
    QTabBar,
    QWidget,
)

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 滚轮停在它们上面**不许改值**（没焦点时）
GUARDED_TYPES: tuple[type, ...] = (
    QComboBox,
    QAbstractSpinBox,
    QSlider,
    QDial,
    QTabBar,
)

#: 滚一格的滚动距离 = 滚动条单步 × 这个系数（Qt 自己的默认也是"三行"）
LINES_PER_NOTCH = 3
#: 一格滚轮的角度（Qt 约定：一格 = 120）
NOTCH_ANGLE = 120.0

#: 装到 QApplication 上的那个守卫（幂等的判据就看这个属性）
_ATTR = "_luweik_wheel_guard"


class WheelGuard(QObject):
    """应用级事件过滤器：拦住"会改值"的滚轮事件（见模块说明）。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: 拦下过多少次（测试与排查用：日志里能看出这个开关到底有没有在工作）
        self.blocked = 0

    # ── Qt 接口 ──
    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt 命名
        try:
            return self._filter(obj, event)
        except Exception as exc:  # noqa: BLE001 - 过滤器里抛异常会让整个界面动作失灵
            logger.debug(f"处理滚轮事件失败（放行）：{exc}")
            return False

    def _filter(self, obj: Any, event: Any) -> bool:
        if event is None or event.type() != QEvent.Type.Wheel:
            return False
        target = self._guarded_ancestor(obj)
        if target is None:
            return False
        # ⚠️ 这里**故意不看焦点**：焦点判据挡不住"刚点过它、又滚过它"那个场景
        # （值还是被改，而且用户看不出来）。改值有明确的两条路：点开下拉框挑、
        # 在数字框里输入或用上下箭头（见模块说明）。
        self.blocked += 1
        area = self._scroll_area(target)
        if area is not None:
            self._scroll_by(area, event)
        return True                 # 吃掉这个事件：控件拿不到，就不会改值

    # ── 判据 ──
    @staticmethod
    def _guarded_ancestor(obj: Any) -> QWidget | None:
        """从收到事件的对象往上找第一个"会改值"的控件。

        为什么要往上找：下拉框内部的输入框、数字框内部的行编辑才是真正的事件接收者，
        只看 `obj` 本身会漏。
        """
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None:
            if isinstance(widget, GUARDED_TYPES):
                return widget
            parent = widget.parentWidget()
            widget = parent
        return None

    @staticmethod
    def _scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
        """最近的滚动区（设置页整页套着一层）——拦下来的滚动送给它。"""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    @staticmethod
    def _scroll_by(area: QAbstractScrollArea, event: Any) -> None:
        """按这一格滚轮把滚动区挪一下（触控板的像素滚动也认）。"""
        bar = area.verticalScrollBar()
        if bar is None or bar.maximum() <= bar.minimum():
            return
        pixels = float(event.pixelDelta().y() or 0)
        if pixels:
            bar.setValue(int(bar.value() - pixels))
            return
        notches = float(event.angleDelta().y() or 0) / NOTCH_ANGLE
        if not notches:
            return
        step = max(1, int(bar.singleStep())) * LINES_PER_NOTCH
        bar.setValue(int(bar.value() - notches * step))


def install(app: Any = None) -> WheelGuard | None:
    """把守卫装到 `QApplication` 上（**幂等**：重复调用只装一次）。返回那个守卫。

    幂等很重要：主窗口可能被建多次（测试、将来的多窗口），重复 `installEventFilter`
    会让同一个事件被过滤多遍（滚动就会一格翻好几页）。
    """
    app = app if app is not None else QApplication.instance()
    if app is None:
        return None
    existing = getattr(app, _ATTR, None)
    if isinstance(existing, WheelGuard):
        return existing
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    setattr(app, _ATTR, guard)
    logger.debug("已启用滚轮防误触（下拉框/数字框没焦点时滚轮不改值）")
    return guard


def is_installed(app: Any = None) -> bool:
    """守卫装上了没（测试用）。"""
    app = app if app is not None else QApplication.instance()
    return isinstance(getattr(app, _ATTR, None), WheelGuard) if app is not None else False
