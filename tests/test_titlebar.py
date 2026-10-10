"""自绘标题栏与无边框窗口（主人 2026-10-10：**"标题栏还用 Windows 自带的一点都不搭"**）。

这份用例要钉住三件事：
1. **外观**：标题栏该有的东西都在（强调条 / 图标 / 软件名 / 版本标签 / 三个按钮），
   QSS 里有它的样式，色值来自色板（不是随手写的十六进制）；
2. **不丢系统行为**：窗口**不能**设 `FramelessWindowHint` —— 拖动、双击最大化、
   边缘缩放、贴边吸附全靠 Windows 原生样式，这条一旦被改掉，用户会觉得"软件不对劲"
   （见 `ui/titlebar.py` 的模块说明）；
3. **命中测试的判据顺序**：四角 → 四边 → 标题栏 → 按钮 → 不表态。
   顺序反了就会出现"想在窗口最上面拖动，结果变成缩放"这种别扭事。

Windows 专有的那一半（真去调 `SetWindowPos`）在这台 Linux 机器上跑不了，
所以那一半只验"非 Windows 时它是安静的"（`enabled is False`、`sync_geometry() == "off"`），
真机行为由日志里的诊断行兜底（`ChromeController.report()`）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from laoa_trader import config as config_mod  # noqa: E402
from laoa_trader.ui import theme as theme_mod  # noqa: E402
from laoa_trader.ui import titlebar as tb  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ══════════════════════════════════════════════════════════════════════════
# 1) 配置：窗口外观这个开关
# ══════════════════════════════════════════════════════════════════════════

def test_window_frame_defaults_to_custom_and_tightens_bad_values() -> None:
    """默认自绘标题栏；写错（`Custom` / `自绘` / 空）一律回默认，不许表现成"窗口没标题栏"。"""
    assert config_mod.DEFAULT_WINDOW_FRAME == tb.FRAME_CUSTOM == "custom"
    assert config_mod.WINDOW_FRAMES == (tb.FRAME_CUSTOM, tb.FRAME_SYSTEM)
    assert config_mod.Config().window_frame == "custom"
    assert config_mod.Config(window_frame="SYSTEM").window_frame == "system"
    for raw in ("自绘", "Custom ", "", "native", None):
        assert config_mod.Config(window_frame=raw).window_frame == "custom"


def test_window_frame_reads_env_var(monkeypatch, tmp_path) -> None:
    """环境变量能盖过配置文件（分发后"窗口出问题"要能不改文件就绕开）。"""
    monkeypatch.delenv("WINDOW_FRAME", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('window_frame = "custom"\n', encoding="utf-8")
    assert config_mod.load_config(path).window_frame == "custom"
    monkeypatch.setenv("WINDOW_FRAME", "system")
    assert config_mod.load_config(path).window_frame == "system"
    monkeypatch.setenv("WINDOW_FRAME", "乱写")     # 环境变量写错也回默认（构造后再归一）
    assert config_mod.load_config(path).window_frame == "custom"


# ══════════════════════════════════════════════════════════════════════════
# 2) 标题栏控件本身
# ══════════════════════════════════════════════════════════════════════════

def test_titlebar_shows_brand_and_three_window_buttons(qapp) -> None:
    """软件名 + 版本小字 + 三个按钮，且是**固定高度**（高度由布局之外的常量说了算）。"""
    bar = tb.TitleBar("luweik决策系统", tag="单机版 · v9.9.9")
    assert bar.objectName() == "titleBar"
    assert bar.height() == tb.TITLEBAR_HEIGHT
    assert bar.title_label.text() == "luweik决策系统"
    assert bar.title_label.objectName() == "titleBarTitle"
    assert bar.tag_label.text() == "单机版 · v9.9.9"
    assert bar.tag_label.objectName() == "titleBarTag"
    assert bar.title_label.font().bold() is True          # 软件名加粗（层级里最高那级）
    names = [button.objectName() for button in bar.window_buttons()]
    assert names == ["titleBarMin", "titleBarMax", "titleBarClose"]
    for button in bar.window_buttons():
        assert button.width() == tb.WINDOW_BUTTON_WIDTH
        assert button.height() == tb.TITLEBAR_HEIGHT
        assert button.toolTip()                          # 三个按钮都要有中文提示


def test_titlebar_buttons_emit_their_signals(qapp) -> None:
    """三个按钮只发信号（自己不碰窗口）—— 主窗口那边接线，控件因此可以单独测试。"""
    bar = tb.TitleBar("x")
    fired: list[str] = []
    bar.minimize_requested.connect(lambda: fired.append("min"))
    bar.maximize_requested.connect(lambda: fired.append("max"))
    bar.close_requested.connect(lambda: fired.append("close"))
    bar.btn_min.click()
    bar.btn_max.click()
    bar.btn_close.click()
    assert fired == ["min", "max", "close"]


def test_maximize_button_switches_between_maximize_and_restore(qapp) -> None:
    """同一个按钮画两种图形：最大化 ↔ 还原（提示文字也要跟着换，否则用户不敢点）。"""
    bar = tb.TitleBar("x")
    assert bar.btn_max.is_maximized() is False
    assert bar.btn_max.toolTip() == "最大化"
    bar.set_maximized(True)
    assert bar.btn_max.is_maximized() is True
    assert bar.btn_max.toolTip() == "还原"
    bar.set_maximized(False)
    assert bar.btn_max.toolTip() == "最大化"


def test_window_buttons_are_painted_never_focusable(qapp) -> None:
    """自绘按钮：不吃焦点（点了最小化之后按回车不该再点一次）。"""
    bar = tb.TitleBar("x")
    for button in bar.window_buttons():
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_titlebar_colors_come_from_the_active_palette(qapp) -> None:
    """自绘标题栏的颜色**也**来自色板：换一套配色（含候选配色）就得跟着换。"""
    original = theme_mod.current_theme()
    try:
        for name, colors in theme_mod.PALETTES.items():
            theme_mod.apply_palette(qapp, name)
            chrome = theme_mod.chrome_colors()
            assert chrome["accent"] == colors["accent"]
            assert chrome["top"] == colors["titlebar_top"]
            assert chrome["border"] == colors["titlebar_border"]
            for value in chrome.values():
                assert value.startswith("#") and len(value) == 7
    finally:
        theme_mod.apply_theme(qapp, original)


def test_titlebar_styles_exist_in_every_palette(qapp) -> None:
    """两张皮肤都要有标题栏的样式（漏一个就会出现"标题栏没有底色"的半成品界面）。"""
    for name in ("indigo", "silver"):
        qss = theme_mod.theme_qss(name)
        for selector in ("QWidget#titleBar", "QLabel#titleBarTitle",
                         "QFrame#titleBarAccent", 'QWidget#windowBody[frame="custom"]'):
            assert selector in qss, f"{name} 主题缺 {selector}"


# ══════════════════════════════════════════════════════════════════════════
# 3) 命中测试（判据顺序是这一块的全部意义）
# ══════════════════════════════════════════════════════════════════════════

class _FakeChrome(tb.ChromeController):
    """把"问系统"的那几个方法换成写死的值，专门验判据顺序（真机行为测不了）。"""

    def __init__(self, window, titlebar, *, maximized=False, border=8):  # noqa: D107
        super().__init__(window, titlebar)
        self.enabled = True
        self.disabled = False
        self._hwnd = 1
        self._maximized = maximized
        self._border = border

    def window_rect(self):  # noqa: D102 - 见类注释
        return (100, 50, 800, 600)

    def resize_border(self):  # noqa: D102
        return self._border

    def _is_maximized(self):  # noqa: D102
        return self._maximized

    def _titlebar_box(self):  # noqa: D102
        return (0, 0, 800, tb.TITLEBAR_HEIGHT)

    def _in_window_button(self, x, y):  # noqa: D102
        return y < tb.TITLEBAR_HEIGHT and x >= 800 - 3 * tb.WINDOW_BUTTON_WIDTH


def _hit(**kwargs):
    window = QWidget()
    bar = tb.TitleBar("x")
    chrome = _FakeChrome(window, bar, **kwargs)
    return chrome, (lambda x, y: chrome.hit_test(100 + x, 50 + y))


def test_corners_win_over_edges_and_caption(qapp) -> None:
    """四个角 → 四角缩放；贴着最上面拖动**不会**变成"拖动窗口"（顺序反了就会）。"""
    _, hit = _hit()
    assert hit(2, 2) == tb.HTTOPLEFT
    assert hit(798, 2) == tb.HTTOPRIGHT
    assert hit(2, 598) == tb.HTBOTTOMLEFT
    assert hit(798, 598) == tb.HTBOTTOMRIGHT


def test_edges_resize_and_middle_does_not(qapp) -> None:
    """四条边各自对应一个方向；窗口中间什么都不表态（交给 Qt 当普通客户区）。"""
    _, hit = _hit()
    assert hit(2, 300) == tb.HTLEFT
    assert hit(798, 300) == tb.HTRIGHT
    assert hit(400, 1) == tb.HTTOP
    assert hit(400, 598) == tb.HTBOTTOM
    assert hit(400, 300) is None


def test_caption_region_and_window_buttons(qapp) -> None:
    """标题栏空白处 = 系统拖动（HTCAPTION）；三个按钮 = 客户区（Qt 自己收点击）。"""
    _, hit = _hit()
    assert hit(400, 20) == tb.HTCAPTION
    assert hit(790, 20) == tb.HTCLIENT        # 落在关闭按钮那一片上
    assert hit(400, tb.TITLEBAR_HEIGHT + 4) is None   # 标题栏下边就不是标题栏了


def test_maximized_window_has_no_resize_edges(qapp) -> None:
    """最大化之后四边不该还能拖（与系统一致）：原来该缩放的角落变成"拖动窗口"。"""
    _, hit = _hit(maximized=True)
    assert hit(2, 2) == tb.HTCAPTION          # 左上角原来是缩放角，现在整块算标题栏
    assert hit(400, 20) == tb.HTCAPTION       # 标题栏那一行照旧能拖动
    assert hit(2, 300) is None                # 左边框不再是缩放边（也不在标题栏里）
    assert hit(400, 598) is None              # 底部同理


def test_hit_test_outside_the_window_says_nothing(qapp) -> None:
    """窗口外的点不表态（真机上系统不会问窗外，但判据不能给出"往右缩放"这种胡话）。"""
    chrome = _FakeChrome(QWidget(), tb.TitleBar("x"))
    assert chrome.hit_test(5000, 5000) is None
    assert chrome.hit_test(0, 0) is None


# ══════════════════════════════════════════════════════════════════════════
# 4) 非 Windows 上必须"安静"（真机行为在这台机器上验不了）
# ══════════════════════════════════════════════════════════════════════════

def test_controller_is_inert_off_windows(qapp) -> None:
    """Linux/离屏：不碰任何 ctypes、消息一个字都不处理，只老实报告"没接管"。"""
    import sys

    if sys.platform == "win32":
        pytest.skip("这条验的是非 Windows 平台的安静降级")
    chrome = tb.ChromeController(QWidget(), tb.TitleBar("x"))
    assert chrome.enabled is False
    assert chrome.disabled is True
    assert chrome.attach() is False
    assert chrome.sync_geometry() == "off"
    assert chrome.window_rect() is None
    assert chrome.client_offset() == (0, 0)
    assert chrome.native_event(None, 0) == (False, 0)
    assert "未接管" in chrome.report()


def test_system_mode_hides_the_titlebar(qapp) -> None:
    """系统标题栏那一档把自绘标题栏整体藏起来（不能出现两条标题栏）。"""
    window = QWidget()
    bar = tb.TitleBar("x")
    mode, controller = tb.apply_window_frame(
        window, tb.FRAME_SYSTEM, titlebar=bar, controller=None)
    assert mode == tb.FRAME_SYSTEM
    assert bar.isHidden() is True
    mode, _ = tb.apply_window_frame(window, "乱七八糟", titlebar=bar, controller=controller)
    assert mode == tb.FRAME_CUSTOM          # 非法值按默认（自绘）来
    assert bar.isHidden() is False


# ══════════════════════════════════════════════════════════════════════════
# 5) 主窗口里的接线（标题栏挂上去了没、有没有把系统行为丢掉）
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def frame_window(ready_cfg, qapp):
    """一个真的主窗口（自绘标题栏那一档）。收尾与 `test_ui_smoke` 是同一套。

    不 `show()`：这一组验的是"控件挂对了没"，不需要真窗口；
    也因此不会触发离屏平台上那些"没有真实显示器"的告警。
    """
    from laoa_trader.ui import app as ui_app

    assert ui_app.QT_AVAILABLE is True
    window = ui_app.MainWindow(ready_cfg)
    try:
        yield window
    finally:
        window._timer.stop()
        window._market_timer.stop()
        window._auction_timer.stop()
        window._flash_timer.stop()
        window.scheduler.stop()
        window.quotes.stop()
        window.scores.stop()            # 评分线程要**先停**（见 test_ui_smoke 的同一条注释）
        window.shutdown()
        window.tray.hide()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_has_the_custom_titlebar(frame_window) -> None:
    """主窗口顶上那一条是我们画的：软件名在最上面，状态区不再重复软件名。"""
    window = frame_window
    assert window.window_frame == tb.FRAME_CUSTOM
    assert window.titlebar is not None
    assert window.titlebar.objectName() == "titleBar"
    assert window.titlebar.isHidden() is False          # 自绘模式下它是显示着的
    assert window.titlebar.title_label.text() == window.app_title_label.text()
    assert window.titlebar.tag_label.text().startswith("单机版")
    # 状态区里那个软件名被收起来了（同一件事不说两遍）；它**还在**（切回系统标题栏要用）
    assert window.app_title_label.isHidden() is True
    assert window.centralWidget().objectName() == "windowBody"
    assert window.centralWidget().property("frame") == "custom"


def test_main_window_does_not_drop_native_window_behaviour(frame_window) -> None:
    """**这条是这一版最要紧的约束**：不设 `FramelessWindowHint`。

    自绘标题栏靠"系统样式一个不改 + 自己回答命中测试"实现（见 ui/titlebar.py），
    拖动、双击最大化、边缘缩放、贴边吸附、阴影全是系统给的。
    谁要是把这里改成 `Qt.FramelessWindowHint`，上面那些行为会一起消失 ——
    那时候用户会说"这软件不对劲"，比标题栏难看严重得多。
    """
    window = frame_window
    flags = window.windowFlags()
    assert not (flags & Qt.WindowType.FramelessWindowHint)
    assert flags & Qt.WindowType.WindowMinimizeButtonHint
    assert flags & Qt.WindowType.WindowMaximizeButtonHint
    assert flags & Qt.WindowType.WindowCloseButtonHint


def test_titlebar_buttons_are_wired_to_the_window(frame_window, qapp) -> None:
    """三个按钮真的接到了窗口上（最大化/还原这一路走通，图标也跟着换）。"""
    window = frame_window
    window.titlebar.maximize_requested.emit()
    qapp.processEvents()
    assert window.isMaximized() is True
    assert window.titlebar.btn_max.is_maximized() is True
    window.titlebar.maximize_requested.emit()
    qapp.processEvents()
    assert window.isMaximized() is False


def test_window_frame_choice_is_saved_with_the_other_settings(frame_window) -> None:
    """设置页那个「窗口外观」下拉框：改了就进一键保存的键集合（重启后生效）。"""
    window = frame_window
    assert window.frame_box.currentData() == "custom"
    window.frame_box.setCurrentIndex(window.frame_box.findData("system"))
    updates = window._collect_settings_updates()
    assert updates["window_frame"] == "system"
    # 界面上的提示要说清"重启才生效"——不然用户会以为点了没反应
    texts = [child.text() for child in window.settings_page.findChildren(QLabel)]
    assert any("重启" in text for text in texts)


def test_falls_back_to_the_system_frame_when_takeover_fails(frame_window, qapp) -> None:
    """自绘没接管成功 → 本次运行**退回系统标题栏**，而不是挂着两条标题栏。

    这是"这台机器上验不了 Windows"的那条安全绳：`WM_NCCALCSIZE` 没拿到就说明原生标题栏
    还在画，此时我们那条品牌栏留在下面比"没换成"更难看。退回时软件名要回到状态区 ——
    否则窗口就没有名字了。
    """
    window = frame_window
    window.chrome.nc_applied = False
    window._sync_window_chrome()               # 非 Windows 上 `enabled` 是 False，直接跳过
    if window.chrome.enabled:                  # 真在 Windows 上跑时才会真的退
        assert window.window_frame == tb.FRAME_SYSTEM
    # 手动走一遍退回路径（这条路径在 Linux 上同样要正确）
    window._fallback_system_frame()
    assert window.window_frame == tb.FRAME_SYSTEM
    assert window.titlebar.isHidden() is True
    assert window.app_title_label.isHidden() is False
    assert window.centralWidget().property("frame") == "system"
    assert window.chrome.enabled is False or window.chrome._hwnd is None
    qapp.processEvents()
