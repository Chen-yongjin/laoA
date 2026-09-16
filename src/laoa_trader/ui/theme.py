"""界面主题（皮肤）：银色金属感 QSS + 一键切回系统默认。

为什么单独一个模块、而不是把样式表塞进 `ui/app.py`
--------------------------------------------------
1. **必须能一键切回**：看不到渲染结果的时候谁也说不准哪种好看，所以主题是
   "可切换的选项"（设置页下拉框，改完立即生效），而不是写死的皮肤。
   `THEMES` / `apply_theme` / `normalize_theme` / `current_theme` 就是那条安全绳。
2. **色值只有这一处**：QSS 里所有颜色都来自下面的 `SILVER_COLORS`，
   界面代码里不许再手写色值（`app.py` 只用 objectName 与布局）。
   涨跌语义色仍然以 `market.COLOR_UP/COLOR_DOWN` 为准 —— 它们是通过**控件级**
   `setStyleSheet("color:…")` 设的，优先级高于这里的应用级样式表，所以在银底上照旧生效，
   不会与主题打架。

设计取舍（保守而明确）
----------------------
- **不用整张位图皮肤**：位图在 150% 缩放下会糊，还要为每个控件维护 @2x 切图。
  这里只用 QSS（浅银灰底 + 1px 细边框 + 轻微纵向渐变），另加**一张极淡的拉丝纹理**
  程序化生成，只铺在"背景条"（顶部状态区 / 概览页页脚 / 表头）上。
- **对比度优先**：正文 `#1f2328` 在 `#f4f5f7` 上、次要文字 `#6b7280` 也仍然清晰；
  不做低对比的"高级灰"，也不做大面积深色（用户明确说不要花哨）。
- **native 优先的例外**：勾选框/单选框的指示器**不接管**（只改文字与间距）。
  用纯 QSS 画指示器的话，"勾选"只能变成一个实心方块（没有对勾图标），
  反而让人分不清"勾没勾"—— 看不出渲染结果时，这种"更好看但更容易误解"的事不做。
"""

from __future__ import annotations

from string import Template
from typing import Any

from laoa_trader.assets import ui_asset
from laoa_trader.config import DEFAULT_UI_THEME, UI_THEMES
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 主题名（与 `config.UI_THEMES` 同一份取值，避免两处各写一份）
THEME_SILVER = "silver"
THEME_SYSTEM = "system"

#: 下拉框里的中文名（用户看到的是这个，不是 silver/system）
THEME_LABELS: dict[str, str] = {
    THEME_SILVER: "银色（金属感）",
    THEME_SYSTEM: "系统默认",
}

#: 拉丝纹理的文件名（普通屏 / 高 DPI 两档）
TEXTURE_NAME = "brushed-metal.png"
TEXTURE_NAME_2X = "brushed-metal@2x.png"

# ── 银色主题的色板（**全程序只有这一处**定义界面色值）──
SILVER_COLORS: dict[str, str] = {
    # 底色
    "window": "#f4f5f7",          # 窗口底（浅银灰）
    "panel": "#ffffff",           # 面板 / 卡片
    "section": "#e9ecef",         # 分区标题底 / 背景条
    # 边框
    "border": "#c3c9d2",          # 浅边框
    "border_dark": "#a8b0ba",     # 深边框（hover 等）
    # 文字
    "text": "#1f2328",            # 正文
    "text_dim": "#6b7280",        # 次要文字
    "text_disabled": "#9aa1ab",   # 禁用文字
    # 普通按钮（金属渐变）
    "btn_top": "#fdfefe",
    "btn_bottom": "#e4e8ee",
    "btn_border": "#b3bac4",
    "btn_hover_top": "#ffffff",
    "btn_hover_bottom": "#eef1f6",
    "btn_press_top": "#dde2e9",
    "btn_press_bottom": "#c9d0d9",
    "btn_disabled_bg": "#f0f1f3",
    # 主操作按钮（【开始选股】：略深的金属蓝灰 + 白字）
    "primary_top": "#5b6b7d",
    "primary_bottom": "#46566a",
    "primary_hover_top": "#66788c",
    "primary_hover_bottom": "#4d5e73",
    "primary_press_top": "#3f4e60",
    "primary_press_bottom": "#36434f",
    "primary_border": "#3d4a5a",
    # 页签 / 表格 / 滚动条 / 进度条
    "tab_unselected": "#e7eaee",
    "tab_selected": "#ffffff",
    "tab_accent": "#8a95a3",
    "tab_hover": "#eef1f6",
    "alt_row": "#fafbfc",
    "grid": "#e3e6ea",
    "selection": "#dbe4ee",
    "selection_strong": "#c9d6e4",
    "focus": "#6b7c8f",
    "progress_top": "#8fa0b3",
    "progress_bottom": "#6b7c8f",
    "progress_track": "#eef1f4",
    "scrollbar": "#c3c9d2",
    "scrollbar_hover": "#a8b0ba",
    "tooltip_bg": "#fffdf0",
}

#: 银色主题的样式表模板。
#: 用 `string.Template`（`$名字`）而不是 `str.format`：QSS 里大量 `{}`，
#: 用 format 就得把每个花括号都双写一遍，改样式时漏一个就报错（而且很难看出错在哪）。
_SILVER_TEMPLATE = Template(
    """
/* ── 底色与文字 ─────────────────────────────────────────── */
QWidget { background-color: $window; color: $text; }
QMainWindow, QDialog, QMessageBox, QInputDialog { background-color: $window; }
QLabel, QCheckBox, QRadioButton { background-color: transparent; color: $text; }
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled { color: $text_disabled; }
QFrame { background-color: transparent; }
QToolTip {
    background-color: $tooltip_bg; color: $text;
    border: 1px solid $btn_border; padding: 4px 6px;
}

/* ── 背景条：顶部状态区 / 概览页页脚 / 表头 ──
   唯一用"素材"的地方：极淡的横向拉丝纹理（拿不到素材时这一行整体不出现，
   底色仍是浅银灰，界面不会变成半透明破版） */
QWidget#statusArea, QWidget#marketFooter,
QHeaderView::section, QTableCornerButton::section {
    background-color: $section;
    $metal
}

/* ── 按钮 ──────────────────────────────────────────────── */
QPushButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_top, stop:1 $btn_bottom);
    border: 1px solid $btn_border; border-radius: 4px;
    padding: 4px 12px; color: $text; min-height: 20px;
}
QPushButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_hover_top, stop:1 $btn_hover_bottom);
    border-color: $border_dark;
}
QPushButton:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_press_top, stop:1 $btn_press_bottom);
    border-color: $border_dark;
}
QPushButton:checked {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_press_top, stop:1 $btn_press_bottom);
    border-color: $border_dark;
}
QPushButton:disabled {
    background-color: $btn_disabled_bg; color: $text_disabled;
    border-color: $border;
}
QPushButton:focus { border: 1px solid $focus; }
QPushButton:default { border: 1px solid $focus; }
/* 主操作按钮：用 objectName 精确命中（不靠"第几个按钮"这种位置关系） */
QPushButton#primaryAction {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $primary_top, stop:1 $primary_bottom);
    border: 1px solid $primary_border; color: #ffffff; font-weight: bold;
}
QPushButton#primaryAction:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $primary_hover_top, stop:1 $primary_hover_bottom);
}
QPushButton#primaryAction:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $primary_press_top, stop:1 $primary_press_bottom);
}
QPushButton#primaryAction:disabled {
    background-color: $btn_disabled_bg; color: $text_disabled;
    border-color: $border;
}

/* ── 输入类 ────────────────────────────────────────────── */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QAbstractSpinBox {
    background-color: $panel; color: $text;
    border: 1px solid $border; border-radius: 4px; padding: 3px 6px;
    selection-background-color: $selection_strong; selection-color: $text;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QComboBox:focus, QComboBox:on { border: 1px solid $focus; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QPlainTextEdit:disabled {
    background-color: $btn_disabled_bg; color: $text_disabled;
}
QLineEdit:read-only { background-color: $btn_disabled_bg; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right; width: 18px;
    border-left: 1px solid $border;
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_top, stop:1 $btn_bottom);
    border-top-right-radius: 3px; border-bottom-right-radius: 3px;
}
QComboBox QAbstractItemView {
    background-color: $panel; color: $text; border: 1px solid $border;
    selection-background-color: $selection; selection-color: $text; outline: none;
}
QSpinBox::up-button, QSpinBox::down-button, QAbstractSpinBox::up-button,
QAbstractSpinBox::down-button {
    width: 16px; border-left: 1px solid $border;
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_top, stop:1 $btn_bottom);
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background-color: $btn_hover_bottom; }

/* ── 勾选类：只改文字与间距，指示器留给系统画（纯 QSS 画不出对勾） ── */
QCheckBox, QRadioButton { spacing: 6px; padding: 2px 0; }
QGroupBox {
    border: 1px solid $border; border-radius: 4px;
    margin-top: 10px; padding: 8px 8px 6px 8px; color: $text;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: $text_dim; }

/* ── 页签 ──────────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid $border; border-radius: 4px;
    background-color: $panel; top: -1px;
}
QTabBar::tab {
    background-color: $tab_unselected; color: $text_dim;
    border: 1px solid $border; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
    padding: 5px 14px; margin-right: 2px;
}
QTabBar::tab:hover:!selected { background-color: $tab_hover; }
QTabBar::tab:selected {
    background-color: $tab_selected; color: $text;
    border-top: 2px solid $tab_accent;
}
QTabBar::tab:disabled { color: $text_disabled; }

/* ── 表格 ──────────────────────────────────────────────── */
QTableWidget, QTableView, QTreeView, QListView {
    background-color: $panel; alternate-background-color: $alt_row;
    gridline-color: $grid; border: 1px solid $border; border-radius: 4px;
    selection-background-color: $selection; selection-color: $text;
}
QTableWidget::item, QTableView::item, QTreeView::item { padding: 2px 4px; border: none; }
QTableWidget::item:selected, QTableView::item:selected {
    background-color: $selection; color: $text;
}
QHeaderView::section {
    color: $text_dim; padding: 4px 6px;
    border: none; border-right: 1px solid $border; border-bottom: 1px solid $border;
}
QHeaderView::section:hover { color: $text; }
QTableCornerButton::section { border: none; border-bottom: 1px solid $border; }

/* ── 滚动区与滚动条 ─────────────────────────────────────── */
QScrollArea { border: none; background-color: transparent; }
QScrollArea > QWidget > QWidget { background-color: transparent; }
QScrollBar:vertical {
    background-color: transparent; width: 12px; margin: 0;
    border: none; border-radius: 6px;
}
QScrollBar:horizontal {
    background-color: transparent; height: 12px; margin: 0;
    border: none; border-radius: 6px;
}
QScrollBar::handle:vertical {
    background-color: $scrollbar; min-height: 24px;
    border-radius: 5px; margin: 1px;
}
QScrollBar::handle:horizontal {
    background-color: $scrollbar; min-width: 24px;
    border-radius: 5px; margin: 1px;
}
QScrollBar::handle:hover { background-color: $scrollbar_hover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background-color: transparent; }

/* ── 进度条 ────────────────────────────────────────────── */
QProgressBar {
    background-color: $progress_track; color: $text;
    border: 1px solid $border; border-radius: 4px;
    text-align: center; min-height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $progress_top, stop:1 $progress_bottom);
    border-radius: 3px;
}

/* ── 菜单 / 对话框按钮区 ─────────────────────────────────── */
QMenu {
    background-color: $panel; color: $text;
    border: 1px solid $border; padding: 4px;
}
QMenu::item { padding: 5px 22px 5px 14px; border-radius: 3px; }
QMenu::item:selected { background-color: $selection; }
QMenu::item:disabled { color: $text_disabled; }
QMenu::separator { height: 1px; background-color: $border; margin: 4px 8px; }

/* ── 卡片（objectName 见 ui/app.py） ──────────────────────
   KPI 块与股票池卡片给"白底 + 细边框"；概览页的**指数条目**故意**不给**边框和底：
   它们是"名称 / 点位 / 涨跌幅"三列对齐的文本行（上一轮定稿的版式），
   十几个条目都套上卡片会让页面变成一片白盒子，反而更乱 */
QFrame#marketKpiCard, QFrame#poolCard {
    background-color: $panel; border: 1px solid $border; border-radius: 6px;
}
/* 小号灰字：用 objectName 精确上色（不依赖 palette 角色 —— 应用级样式表里
   `QLabel { color }` 会把角色的灰盖掉，写死在这里才稳定） */
QLabel#marketSectionTitle, QLabel#marketKpiTitle,
QLabel#marketEntryName, QLabel#statusTag { color: $text_dim; }

/* ── 提醒浮窗（QQ 式，objectName 见 ui/alert_popup.py）──────
   浮窗是**独立顶层窗口**：不加这条它会命中上面的 `QWidget` 规则，底色和桌面
   一样是浅银灰、又没有边框，浮在别的程序上面几乎看不见。这里给它白底 + 深边框。 */
QWidget#alertPopup {
    background-color: $panel; border: 1px solid $border_dark; border-radius: 8px;
}
QWidget#alertPopup QLabel { background-color: transparent; color: $text; }
QLabel#alertPopupTitle { font-weight: bold; }
QLabel#alertPopupFoot { color: $text_dim; }
/* 每一行提醒 = 一个"看起来像列表项"的按钮：左对齐、悬停才出底色（不画成按钮，
   否则五行提醒就是五个按钮，太吵） */
QPushButton#alertPopupRow {
    background-color: transparent; border: none; border-radius: 4px;
    padding: 6px 8px; text-align: left; color: $text;
}
QPushButton#alertPopupRow:hover { background-color: $selection; }
QPushButton#alertPopupRow:pressed { background-color: $selection_strong; }
"""
)


def silver_qss(metal_url: str | None = None) -> str:
    """银色主题的 QSS；`metal_url=None` 表示**不用纹理**（背景条退回纯色）。

    纹理只是锦上添花：素材没打进包、被删掉、换名字，对应那行 `background-image`
    直接不出现，背景条仍然是浅银色。缺素材绝不能让界面出错，也不能变成半透明的破版。
    """
    metal = ""
    if metal_url:
        metal = ("background-image: url(%s);\n"
                 "    background-repeat: repeat-x;\n"
                 "    background-position: top left;" % metal_url)
    return _SILVER_TEMPLATE.substitute(metal=metal, **SILVER_COLORS)


#: 模块导入时算一次（拿不到素材就是纯色版）；`apply_theme` 每次应用时会**重新**解析路径
SILVER_QSS = silver_qss()

#: 主题名 → 样式表。`system` 是空串：把样式表清空 = 回到系统原生外观（安全绳）
THEMES: dict[str, str] = {
    THEME_SILVER: SILVER_QSS,
    THEME_SYSTEM: "",
}

_CURRENT: dict[str, str] = {"name": DEFAULT_UI_THEME}


def normalize_theme(name: Any) -> str:
    """把配置/环境变量里的主题名收紧到合法取值；**非法值一律回默认**（银色）。

    为什么不让非法值抛错或留空：主题是"外观偏好"，写错一个字母不该让界面起不来 ——
    与 `config.pool_view` 同一套思路（写错就按默认来，用户看到的仍是一个能用的界面）。
    """
    value = str(name if name is not None else "").strip().lower()
    return value if value in UI_THEMES else DEFAULT_UI_THEME


def theme_label(name: Any) -> str:
    """主题的中文名（设置页下拉框显示用）。"""
    return THEME_LABELS.get(normalize_theme(name), THEME_LABELS[DEFAULT_UI_THEME])


def current_theme() -> str:
    """当前生效的主题名（由 `apply_theme` 维护，默认银色）。"""
    return _CURRENT["name"]


def texture_url() -> str | None:
    """纹理图在 QSS 里的 `url("...")` 片段；**拿不到素材返回 None**。

    两个坑：
    1. QSS 的 url() 必须是**绝对路径** —— 相对路径按"进程当前目录"解析，
       打包成 exe 之后用户从别处双击运行就会解析到别的地方（表现为"素材莫名丢了"）；
    2. 高 DPI（150% 缩放）下优先用 @2x 那张，纹理才不会被拉糊。
    """
    name = TEXTURE_NAME
    try:  # 没装 Qt / 没有屏幕信息时按普通屏处理（不影响正确性，只是选了 1x 图）
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is not None and float(screen.devicePixelRatio()) >= 1.5:
            name = TEXTURE_NAME_2X
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"取屏幕缩放比失败（按普通屏处理）：{exc}")
    path = ui_asset(name) or ui_asset(TEXTURE_NAME)
    if path is None:
        logger.debug(f"没有界面素材 {name}（背景条退回纯色，不影响使用）")
        return None
    return '"' + path.as_posix() + '"'


def apply_theme(app: Any = None, name: Any = None) -> str:
    """把主题应用到 `QApplication`，返回**实际生效**的主题名。

    Args:
        app: QApplication；传 None 时自己取 `QApplication.instance()`。
        name: 主题名；传 None 用当前主题（`current_theme()`）。

    为什么每次应用都重新生成银色 QSS（而不是直接用 `THEMES["silver"]`）：
    纹理路径要按**当前**的素材情况与屏幕缩放解析 —— 打包后路径不同、
    素材缺失要能退回纯色，所以这里不走缓存那份字符串。
    """
    theme = normalize_theme(name if name is not None else current_theme())
    if theme == THEME_SILVER:
        qss = silver_qss(texture_url())
    else:
        qss = THEMES[THEME_SYSTEM]        # 空串 = 清空样式表，回到系统原生
    try:
        if app is None:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
    except Exception as exc:  # noqa: BLE001 - 换皮肤绝不能让程序起不来
        logger.warning(f"应用界面主题失败（已忽略）：{type(exc).__name__}: {exc}")
        return theme
    _CURRENT["name"] = theme
    logger.debug(f"界面主题已应用：{theme}")
    return theme
