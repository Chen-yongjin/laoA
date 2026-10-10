"""滚轮防误触（主人 2026-10-10：「把设置页的鼠标滚轮功能给限制掉，会无意触发」）。

要钉住的行为只有三条，但每条都是"用户会不会被自己坑到"的事：
1. **滚轮永远不改值**（下拉框 / 数字框 / 页签栏都一样）—— 判据**故意不看焦点**：
   "他刚点过那个框、接着往下滚、滚轮正好划过它"是设置页里一定会发生的场景，
   焦点判据挡不住它。改值有明确的两条路：点开下拉框挑、在数字框里输入或点上下箭头。
2. **键盘不受影响**：数字框里 ↑/↓ 照常加一减一（拦的是滚轮事件，不是键盘）。
3. **拦下来的滚动不能白费**：得转给最近的滚动区（设置页整页套着一层），
   于是"想翻页却改错了值"变成"页面照常往下翻"。

顺带钉住"不误伤"：表格与滚动条自己的滚轮照常工作 —— 过滤器把
`QScrollBar`（它也是 `QAbstractSlider`）圈进去的话，滚动条就滚不动了。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from laoa_trader.ui import wheel_guard as wg  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # 收尾：把守卫摘掉，免得影响别的用例（幂等那一条自己会再装）
    guard = getattr(app, wg._ATTR, None)
    if guard is not None:
        app.removeEventFilter(guard)
        delattr(app, wg._ATTR)


def _wheel(widget, notches: int = -1) -> None:
    """给控件发一格滚轮（notches 为负 = 向下滚）。"""
    event = QWheelEvent(
        QPointF(widget.rect().center()), QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, 0), QPoint(0, notches * 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(widget, event)


def test_wheel_never_changes_a_combo(qapp) -> None:
    """滚轮划过下拉框**不改值**（这条就是主人报的那个误触）。"""
    wg.install(qapp)
    box = QComboBox()
    box.addItems(["甲", "乙", "丙"])
    box.setCurrentIndex(1)
    box.show()
    qapp.processEvents()
    _wheel(box, -1)
    assert box.currentIndex() == 1
    _wheel(box, 1)
    assert box.currentIndex() == 1


def test_wheel_does_not_change_a_focused_combo_either(qapp) -> None:
    """**焦点也挡不住误触**，所以焦点不在判据里：刚点过它、又滚过它，值不该变。"""
    wg.install(qapp)
    box = QComboBox()
    box.addItems(["甲", "乙", "丙"])
    box.setCurrentIndex(1)
    box.show()
    box.setFocus()
    qapp.processEvents()
    assert box.hasFocus() is True
    _wheel(box, -1)
    assert box.currentIndex() == 1


def test_wheel_on_spinbox_does_not_change_value(qapp) -> None:
    """数字框同理 —— 设置页里「止损 %」「自选上限」被滚轮改掉是最要命的那类误触。"""
    wg.install(qapp)
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(8)
    spin.show()
    spin.setFocus()
    qapp.processEvents()
    _wheel(spin, -1)
    assert spin.value() == 8


def test_keyboard_still_changes_the_value(qapp) -> None:
    """拦的是**滚轮**，不是键盘：数字框里 ↑/↓ 照常加一减一（改值的正经路子）。"""
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent

    wg.install(qapp)
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(8)
    spin.show()
    spin.setFocus()
    qapp.processEvents()
    QApplication.sendEvent(spin, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down,
                                           Qt.KeyboardModifier.NoModifier))
    qapp.processEvents()
    assert spin.value() == 7

    # 点上下箭头（鼠标）也照常能用（那是明确的操作，不是误触）
    spin.stepDown()
    assert spin.value() == 6


def test_blocked_wheel_scrolls_the_containing_area_instead(qapp) -> None:
    """拦下来的滚动**转给最近的滚动区**：页面照常往下翻，不是"滚了没反应"。"""
    wg.install(qapp)
    area = QScrollArea()
    inner = QWidget()
    layout = QVBoxLayout(inner)
    for _ in range(60):
        filler = QWidget()
        filler.setFixedHeight(40)
        layout.addWidget(filler)
    box = QComboBox()
    box.addItems(["甲", "乙", "丙"])
    box.setCurrentIndex(1)
    layout.addWidget(box)
    area.setWidget(inner)
    area.resize(400, 200)
    area.show()
    qapp.processEvents()

    bar = area.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(0)
    _wheel(box, -1)
    qapp.processEvents()
    assert box.currentIndex() == 1        # 值没被改
    assert bar.value() > 0                # 但页面滚下去了


def test_tables_and_scrollbars_are_never_blocked(qapp) -> None:
    """**不误伤**：表格照常滚、滚动条自己照常滚（它不是"会改值的控件"）。"""
    wg.install(qapp)
    area = QScrollArea()
    inner = QWidget()
    layout = QVBoxLayout(inner)
    table = QTableWidget(80, 3)
    layout.addWidget(table)
    area.setWidget(inner)
    area.resize(400, 200)
    area.show()
    qapp.processEvents()

    guard = wg.install(qapp)
    before = guard.blocked
    # 判据只看"我们有没有插手"：表格与滚动条上的滚轮**一次都不许被拦**。
    # 不在这里断言"滚动条滚了多少"：离屏平台下表格的布局不一定算完，
    # 那种断言测的是 Qt 的排版时机，不是我们的过滤器（何况真机上它本来就滚）。
    _wheel(table, -1)
    qapp.processEvents()
    assert guard.blocked == before        # 表格上的滚轮没被拦

    _wheel(area.verticalScrollBar(), -1)
    qapp.processEvents()
    assert guard.blocked == before        # 滚动条上的滚轮也没被拦

    # 对照：同一个守卫在"会改值的控件"上确实会拦（否则上面两条就是废话）
    box = QComboBox()
    box.addItems(["甲", "乙", "丙"])
    layout.addWidget(box)
    qapp.processEvents()
    _wheel(box, -1)
    assert guard.blocked == before + 1


def test_tabbar_wheel_is_guarded_too(qapp) -> None:
    """页签栏也拦：滚轮划过页签会切页，那也是误触（设置页外面就是五个页签）。"""
    wg.install(qapp)
    tabs = QTabWidget()
    for name in ("一", "二", "三"):
        tabs.addTab(QWidget(), name)
    tabs.resize(400, 200)
    tabs.show()
    tabs.tabBar().setCurrentIndex(0)
    qapp.processEvents()
    _wheel(tabs.tabBar(), -1)
    assert tabs.tabBar().currentIndex() == 0     # 不因滚轮切页
    assert tabs.currentIndex() == 0


def test_install_is_idempotent(qapp) -> None:
    """装两次只生效一个：重复 `installEventFilter` 会让一格滚轮翻好几页。"""
    first = wg.install(qapp)
    second = wg.install(qapp)
    assert first is second
    assert wg.is_installed(qapp) is True
    box = QComboBox()
    box.addItems(["甲", "乙", "丙"])
    box.setCurrentIndex(1)
    box.show()
    qapp.processEvents()
    _wheel(box, -1)
    assert box.currentIndex() == 1
    assert first.blocked == 1             # **只拦了一次**（装两次就会是 2）


def test_main_window_installs_the_guard(ready_cfg, qapp) -> None:
    """主窗口一建起来就装上了（不是"要用户去点什么开关"）。"""
    from laoa_trader.ui import app as ui_app

    assert wg.is_installed(qapp) is False or True     # 前面用例可能已装过，这里只看结果
    window = ui_app.MainWindow(ready_cfg)
    try:
        assert wg.is_installed(qapp) is True
        assert isinstance(window.wheel_guard, wg.WheelGuard)
    finally:
        window._timer.stop()
        window._market_timer.stop()
        window._flash_timer.stop()
        window.scheduler.stop()
        window.quotes.stop()
        window.shutdown()
        window.tray.hide()
        window.close()
        window.deleteLater()
        qapp.processEvents()
