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

两套皮肤（2026-10-08 新增「科技蓝（深色）」，并设为默认）
--------------------------------------------------------
- **科技蓝（`tech`）**：深海军蓝底 + 青色强调（主按钮、页签下划线、进度条、选中态），
  数据卡片是比底色亮一档的面板色，边界用细线而不是阴影 —— 目标是"有科技感但不像游戏 UI"。
  深色底上**涨跌色要换一档**（`#2e7d32` 在深蓝上只有 ~2:1，数字会糊掉），
  所以语义色按主题给（见 `value_color`）。
- **银色（`silver`）**：原来的金属浅灰皮肤，保留给"看不惯深色"的用户与打印/截图场景。
- **系统默认（`system`）**：清空样式表，回到 Qt 原生外观（安全绳）。

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
THEME_TECH = "tech"
THEME_SILVER = "silver"
THEME_SYSTEM = "system"

#: 候选深色配色（2026-10-10）：**只用于出预览图/内部渲染，不进设置页下拉框**。
#: 为什么这么分：配色是"看着挑"的东西，先用离屏渲染出几张图给主人比，
#: 挑中的那一套再并进 `TECH_COLORS`（那样 `UI_THEMES` 与所有既有用例都不用动）。
#: 主人定了之后这两个名字就可以删掉。
THEME_OBSIDIAN = "obsidian"      # 曜黑 + 电光蓝
THEME_GRAPHITE = "graphite"      # 石墨灰 + 紫罗兰

#: 下拉框里的中文名（用户看到的是这个，不是 tech/silver/system）
THEME_LABELS: dict[str, str] = {
    THEME_TECH: "科技蓝（深色）",
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
    # 主操作按钮（【开始筛选】：略深的金属蓝灰 + 白字）
    "primary_top": "#5b6b7d",
    "primary_bottom": "#46566a",
    "primary_hover_top": "#66788c",
    "primary_hover_bottom": "#4d5e73",
    "primary_press_top": "#3f4e60",
    "primary_press_bottom": "#36434f",
    "primary_border": "#3d4a5a",
    #: 主按钮上的字色。**不能写死白色**：深色主题里主按钮是亮青色，白字对比度只有 1.8:1
    "primary_text": "#ffffff",
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
    # ── 2026-10-10 新增：窗口/卡片层次与自绘标题栏（见下面「科技感升级」一节）──
    # 这些键**每套色板都必须有**：样式表模板是所有主题共用的一张（少一个键
    # `Template.substitute` 就 KeyError），候选配色也一样。
    "window_top": "#f8f9fb",          # 窗口底渐变（上）
    "window_bottom": "#eceff4",       # 窗口底渐变（下）
    "card_top": "#ffffff",            # 卡片渐变（上）
    "card_bottom": "#fbfcfe",         # 卡片渐变（下）
    "section_top": "#eef1f5",         # 背景条渐变（上）；下面那条用 section
    "titlebar_top": "#eef2f7",        # 自绘标题栏渐变（上）
    "titlebar_bottom": "#e2e8f0",     # 自绘标题栏渐变（下）
    "titlebar_border": "#c9d1db",     # 标题栏下边那条细线
    "titlebar_text": "#1f2328",       # 标题栏上的软件名
    "titlebar_dim": "#6b7280",        # 标题栏上的次要字（版本号等）
    "accent": "#5b6b7d",              # 强调色（分组标题、页签下划线）
    "accent_dim": "#c3c9d2",          # 强调色的弱化版（细边、禁用态）
    "hover_soft": "#eef1f6",          # 极轻的悬停底色（页签/表头/列表项）
    "close_hover": "#c62828",         # 关闭按钮悬停底色（红）
}

# ── 科技蓝主题的色板（与银色**同一批键名**，所以同一张样式表模板能渲染两套皮肤）──
#
# 取色原则（2026-10-08）：
#   * 底：#0b1220（深海军蓝）→ 卡片 #121c2e → 背景条/表头 #16233a，三层差一档，
#     层次靠**明度**而不是阴影（深色界面上阴影基本看不见）；
#   * 强调：青色 #2ad4ea 只用在"要你点/正在发生"的地方（主按钮、页签选中、进度、
#     选中行边框），别处一律中性色 —— 满屏发光就不叫专业了；
#   * 文字对比度实测（在窗口底上）：正文 15.9:1、次要 7.4:1、涨跌 6.8/10.5:1，
#     全部高于 WCAG AA 的 4.5:1（深色皮肤最容易翻车的就是"深绿字配深蓝底"）；
#   * 主按钮是亮青底 + **深字**（#04222b 在青底上 9.2:1）—— 白字在青底上只有 1.8:1。
TECH_COLORS: dict[str, str] = {
    # 底色
    "window": "#0b1220",
    "panel": "#121c2e",
    "section": "#16233a",
    # 边框
    "border": "#23334c",
    "border_dark": "#33486a",
    # 文字
    "text": "#e6edf7",
    "text_dim": "#93a4bd",
    "text_disabled": "#7c8ea9",      # 比次要文字再暗一点，但在三种底色上都还看得清（≥4:1）
    # 普通按钮（深色卡片上略亮一档 + 细边）
    "btn_top": "#1b2946",
    "btn_bottom": "#15203a",
    "btn_border": "#2b3d5c",
    "btn_hover_top": "#22314f",
    "btn_hover_bottom": "#1a2742",
    "btn_press_top": "#101a2e",
    "btn_press_bottom": "#0d1526",
    "btn_disabled_bg": "#131c2c",
    # 主操作按钮（青色，要一眼看到）
    "primary_top": "#2ad4ea",
    "primary_bottom": "#12a4c2",
    "primary_hover_top": "#45dff2",
    "primary_hover_bottom": "#17b0cf",
    "primary_press_top": "#0f93b0",
    "primary_press_bottom": "#0b7d97",
    "primary_border": "#0b7f98",
    "primary_text": "#04222b",
    # 页签 / 表格 / 滚动条 / 进度条
    "tab_unselected": "#101a2c",
    "tab_selected": "#121c2e",
    "tab_accent": "#2ad4ea",
    "tab_hover": "#18243c",
    "alt_row": "#0f1a2b",
    "grid": "#1b2942",
    "selection": "#1d3a5c",
    "selection_strong": "#26507c",
    "focus": "#2ad4ea",
    "progress_top": "#2ad4ea",
    "progress_bottom": "#12a4c2",
    "progress_track": "#16233a",
    "scrollbar": "#2a3d59",
    "scrollbar_hover": "#3a5478",
    "tooltip_bg": "#16233a",
    # ── 2026-10-10 新增（键名与银色一致，见 SILVER_COLORS 的说明）──
    "window_top": "#0e1a2e",         # 比原来的纯色底亮一档：窗口有"上亮下暗"的纵深
    "window_bottom": "#070d18",
    "card_top": "#15233c",
    "card_bottom": "#101a2c",
    "section_top": "#1c2d4a",
    "titlebar_top": "#16233c",
    "titlebar_bottom": "#0f1a2d",
    "titlebar_border": "#23334c",
    "titlebar_text": "#e6edf7",
    "titlebar_dim": "#8fa3bf",
    "accent": "#2ad4ea",
    "accent_dim": "#1f4f60",
    "hover_soft": "#18243c",
    "close_hover": "#e5484d",
}

#: 深色底上的**语义色**（红涨绿跌）——比浅色底那两档亮一档。
#: 为什么必须分开：#2e7d32（深绿）在 #0b1220 上的对比度只有 ~2:1，盈亏数字会糊在背景里；
#: 而浅色主题上不能换色（用户与用例都按 #d32f2f / #2e7d32 核过）。
TECH_SEMANTIC: dict[str, str] = {
    "up": "#ff6b6b",      # 涨（6.8:1）
    "down": "#3ddc84",    # 跌（10.5:1）
}

#: 主题名 → 语义色覆盖（没列到的主题走 `market.COLOR_UP/COLOR_DOWN`）
SEMANTIC_BY_THEME: dict[str, dict[str, str]] = {
    THEME_TECH: TECH_SEMANTIC,
}

# ── 候选配色（2026-10-10）：只用来给主人挑，挑完并入 TECH_COLORS ──────────
#
# 取色时的两条硬约束（不是我瞎讲究，A 股界面踩了就难看）：
#   1. **强调色不能是红/绿**：A 股红涨绿跌，强调色撞上语义色就会让人分不清
#      "这是涨"还是"这是按钮"。所以候选只用蓝、紫——青色的 `tech` 也是这个道理；
#   2. 正文对比度 ≥ 4.5:1（WCAG AA），深色皮肤最容易翻车的就是"深绿字配深蓝底"。
#
#: 曜黑 + 电光蓝：底压到近黑（#070a0f），全靠电光蓝 #4d9dff 提亮，冷、克制。
OBSIDIAN_COLORS: dict[str, str] = {
    "window": "#090d13",
    "panel": "#0e141c",
    "section": "#131b25",
    "border": "#1d2734",
    "border_dark": "#2c3b4d",
    "text": "#e7edf5",
    "text_dim": "#8d9cad",
    "text_disabled": "#75828f",
    "btn_top": "#141d29",
    "btn_bottom": "#0f1721",
    "btn_border": "#243040",
    "btn_hover_top": "#1a2531",
    "btn_hover_bottom": "#141e29",
    "btn_press_top": "#0c131b",
    "btn_press_bottom": "#090f16",
    "btn_disabled_bg": "#0d141c",
    "primary_top": "#4d9dff",
    "primary_bottom": "#2f7ae6",
    "primary_hover_top": "#68adff",
    "primary_hover_bottom": "#3a86f0",
    "primary_press_top": "#2a6cc9",
    "primary_press_bottom": "#245bb0",
    "primary_border": "#2a6cc9",
    "primary_text": "#04101f",
    "tab_unselected": "#0c121a",
    "tab_selected": "#0e141c",
    "tab_accent": "#4d9dff",
    "tab_hover": "#131c26",
    "alt_row": "#0b1119",
    "grid": "#182130",
    "selection": "#16304f",
    "selection_strong": "#1e4270",
    "focus": "#4d9dff",
    "progress_top": "#4d9dff",
    "progress_bottom": "#2f7ae6",
    "progress_track": "#131b25",
    "scrollbar": "#233042",
    "scrollbar_hover": "#33465e",
    "tooltip_bg": "#131b25",
    "window_top": "#0b1119",
    "window_bottom": "#05080c",
    "card_top": "#131c27",
    "card_bottom": "#0d141d",
    "section_top": "#17202c",
    "titlebar_top": "#0d151f",
    "titlebar_bottom": "#0a1119",
    "titlebar_border": "#1b2531",
    "titlebar_text": "#e7edf5",
    "titlebar_dim": "#93a3b5",
    "accent": "#4d9dff",
    "accent_dim": "#22456f",
    "hover_soft": "#16202c",
    "close_hover": "#e5484d",
}

#: 石墨灰 + 紫罗兰：中性石墨底（#111318）+ 紫罗兰 #a78bfa，偏"专业工具"的安静气质。
GRAPHITE_COLORS: dict[str, str] = {
    "window": "#111318",
    "panel": "#191c22",
    "section": "#1f232b",
    "border": "#2a2f39",
    "border_dark": "#3a4150",
    "text": "#e9e9ee",
    "text_dim": "#9b9aa6",
    "text_disabled": "#83828d",
    "btn_top": "#23262e",
    "btn_bottom": "#1c1f26",
    "btn_border": "#343945",
    "btn_hover_top": "#2a2e37",
    "btn_hover_bottom": "#22262e",
    "btn_press_top": "#171a20",
    "btn_press_bottom": "#14161b",
    "btn_disabled_bg": "#1a1d23",
    "primary_top": "#a78bfa",
    "primary_bottom": "#8b5cf6",
    "primary_hover_top": "#b9a1fb",
    "primary_hover_bottom": "#9a6ef7",
    "primary_press_top": "#7c4fe0",
    "primary_press_bottom": "#6b41c4",
    "primary_border": "#7c4fe0",
    "primary_text": "#150a2e",
    "tab_unselected": "#15181d",
    "tab_selected": "#191c22",
    "tab_accent": "#a78bfa",
    "tab_hover": "#1e222a",
    "alt_row": "#15181e",
    "grid": "#23272f",
    "selection": "#33285a",
    "selection_strong": "#453374",
    "focus": "#a78bfa",
    "progress_top": "#a78bfa",
    "progress_bottom": "#8b5cf6",
    "progress_track": "#1f232b",
    "scrollbar": "#333846",
    "scrollbar_hover": "#454c5e",
    "tooltip_bg": "#1f232b",
    "window_top": "#171a20",
    "window_bottom": "#0d0f13",
    "card_top": "#1e222a",
    "card_bottom": "#171a20",
    "section_top": "#242833",
    "titlebar_top": "#191c23",
    "titlebar_bottom": "#14171c",
    "titlebar_border": "#2a2f39",
    "titlebar_text": "#e9e9ee",
    "titlebar_dim": "#9b9aa6",
    "accent": "#a78bfa",
    "accent_dim": "#413a63",
    "hover_soft": "#23262e",
    "close_hover": "#e5484d",
}

#: 主题名 → 色板（**所有能渲染的配色都在这里**：两套正式皮肤 + 两套候选）。
#: `theme_qss()` 只认这张表 —— 出预览图时可以直接渲染候选配色。
PALETTES: dict[str, dict[str, str]] = {
    THEME_TECH: TECH_COLORS,
    THEME_SILVER: SILVER_COLORS,
    THEME_OBSIDIAN: OBSIDIAN_COLORS,
    THEME_GRAPHITE: GRAPHITE_COLORS,
}

#: 出对比图用的顺序（第一张是现在的默认皮肤 = 现状）
PREVIEW_THEMES: tuple[str, ...] = (THEME_TECH, THEME_OBSIDIAN, THEME_GRAPHITE)

#: 深色底的主题（要自己画颜色的控件、热力图、自绘标题栏都按这个判断）。
#: **不能只写 THEME_TECH**：候选配色换上去之后 `system` 之外的全是深色。
DARK_THEMES: tuple[str, ...] = (THEME_TECH, THEME_OBSIDIAN, THEME_GRAPHITE)

for _name in DARK_THEMES:
    SEMANTIC_BY_THEME.setdefault(_name, TECH_SEMANTIC)

#: 背景条用的渐变：**由色板自己拼**（不再把色值写死在常量里 —— 换一套皮肤就得跟着改一次）
TECH_BAR_GRADIENT = (
    "background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,\n"
    "                                      stop:0 %s, stop:1 %s);"
) % (TECH_COLORS["section_top"], TECH_COLORS["section"])

#: 银色主题的样式表模板。
#: 用 `string.Template`（`$名字`）而不是 `str.format`：QSS 里大量 `{}`，
#: 用 format 就得把每个花括号都双写一遍，改样式时漏一个就报错（而且很难看出错在哪）。
_SILVER_TEMPLATE = Template(
    """
/* ── 底色与文字 ─────────────────────────────────────────── */
QWidget { background-color: $window; color: $text; }
QMainWindow, QDialog, QMessageBox, QInputDialog { background-color: $window; }
/* 窗口主体（objectName 见 ui/app.py 的 _build_ui）：上亮下暗的极轻渐变。
   为什么给"主体"而不是 QMainWindow：QMainWindow 上面盖着 central widget，
   画在 QMainWindow 上根本看不见（被 central 的纯色底盖掉了）。 */
QWidget#windowBody {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $window_top, stop:1 $window_bottom);
}
/* 自绘标题栏（objectName 见 ui/titlebar.py）：**无边框模式**下这一条就是窗口的"脸"。
   主人在 2026-10-10 说"标题栏还用 Windows 自带的一点都不搭"，于是菜单栏级别的
   品牌区改由我们自己画（系统标题栏那套还能在设置页里切回来）。 */
QWidget#titleBar {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $titlebar_top, stop:1 $titlebar_bottom);
    border-bottom: 1px solid $titlebar_border;
}
QWidget#titleBar QLabel { background-color: transparent; color: $titlebar_text; }
QLabel#titleBarTitle { color: $titlebar_text; }
QLabel#titleBarTag { color: $titlebar_dim; }
/* 右上角的最小化/最大化/关闭是**自绘**的（QPainter，不是 QSS）：Windows 11 那三根
   细线用字体符号画不准，见 ui/titlebar.py 的 WindowButton。这里只声明它们的底色透明。 */
QWidget#titleBar QAbstractButton { background-color: transparent; border: none; }
/* 软件名左边那道强调色小竖条 */
QFrame#titleBarAccent { background-color: $accent; border: none; border-radius: 2px; }
QLabel#titleBarIcon { background-color: transparent; }
/* 自绘标题栏模式下，窗口四周补一条 1 像素的细边（原生标题栏没了，靠它划出窗口的边界）。
   `[frame="custom"]` 是动态属性选择器，属性由 `ui/app.py` 设在 central widget 上；
   系统标题栏那一档不命中这条 —— 那一档有系统外框，再来一条内嵌线就是"双边框"。 */
QWidget#windowBody[frame="custom"] { border: 1px solid $titlebar_border; }
QLabel, QCheckBox, QRadioButton { background-color: transparent; color: $text; }
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled { color: $text_disabled; }
QFrame { background-color: transparent; }
QToolTip {
    background-color: $tooltip_bg; color: $text;
    border: 1px solid $btn_border; border-radius: 6px; padding: 5px 7px;
}

/* ── 背景条：顶部状态区 / 概览页页脚 / 表头 ──
   唯一用"素材"的地方：极淡的横向拉丝纹理（拿不到素材时这一行整体不出现，
   底色仍是浅银灰，界面不会变成半透明破版） */
QWidget#statusArea {
    background-color: $section;
    $metal
    border: 1px solid $border; border-radius: 10px;
}
QWidget#marketFooter,
QHeaderView::section, QTableCornerButton::section {
    background-color: $section;
    $metal
}

/* ── 按钮 ──────────────────────────────────────────────── */
QPushButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $btn_top, stop:1 $btn_bottom);
    border: 1px solid $btn_border; border-radius: 6px;
    padding: 4px 12px; color: $text; min-height: 20px;
}
/* 「策略筛选」右侧元素区（变量 / 函数 / 运算符 / 排除，四组 52 个按钮）要**小一圈**：
   用户 2026-09-18 要求"把按键大小都缩小"。
   ⚠️ 这条规则里的 `min-height` **必须给一个具体值**（别写 0px、也别省）：
   一旦 #paletteButton 命中，通用规则里的 `min-height: 20px` 就不再对这批按钮生效，
   而按钮的最小高度一变成 0，外面那个 QScrollArea 就会把整块内容**压扁到视口里**
   （实测：52 个按钮全被压成 4 像素高的长条 —— 字看不见、也点不中，用户实报过）。
   `padding: 0px 3px` + `min-height: 18px` = 20 像素的实际高度（与代码里的
   `formula_page.BUTTON_HEIGHT` 对齐；有测试按"渲染出来的高度"钉住它）。
   横向 padding 只留 3px：一行 4 列之后每个按钮只有 70 多像素宽，中文标签要占大头。 */
QPushButton#paletteButton {
    padding: 0px 3px; min-height: 18px; border-radius: 3px;
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
    border: 1px solid $primary_border; border-radius: 8px;
    color: $primary_text; font-weight: bold;
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
    border: 1px solid $border; border-radius: 10px;
    background-color: $panel;
    margin-top: 10px; padding: 8px 8px 6px 8px; color: $text;
}
/* 分组标题用**强调色**：一页里十几个分组，标题就是视觉骨架（原来跟正文一个色，
   整页糊成一片） */
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: $accent; }

/* ── 页签 ──────────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid $border; border-radius: 10px;
    background-color: $panel; top: -1px;
}
/* 页签改成"下划线"式（原来是文件夹标签那套）：底不再分块、选中项由一条 2px 的
   强调色下划线说话 —— 五个页签从"五个小方块"变成一条整齐的导航，更像现代控制台。
   `border-bottom: 2px solid transparent` 是**占位**：不预留这 2px，
   选中时多出来的下划线会把整排页签顶高一档，切换页签会看到文字上下跳。 */
QTabBar::tab {
    background-color: transparent; color: $text_dim;
    border: none; border-bottom: 2px solid transparent;
    padding: 6px 16px; margin-right: 4px;
}
QTabBar::tab:hover:!selected {
    background-color: $hover_soft; color: $text;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: transparent; color: $text;
    border-bottom: 2px solid $tab_accent; font-weight: bold;
}
QTabBar::tab:disabled { color: $text_disabled; }

/* ── 表格 ──────────────────────────────────────────────── */
QTableWidget, QTableView, QTreeView, QListView {
    background-color: $panel; alternate-background-color: $alt_row;
    gridline-color: $grid; border: 1px solid $border; border-radius: 8px;
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
QHeaderView::section:hover { color: $text; background-color: $hover_soft; }
QTableCornerButton::section { border: none; border-bottom: 1px solid $border; }

/* 「本次筛选结果」表（objectName 见 ui/formula_page.py 的 RESULT_TABLE_OBJECT）：
   **只给这一张表**把单元格与表头的内边距放宽一点。
   为什么单独给它（用户 2026-09-21 实报"框体有点小，字显示不全"）：这一页的列宽是
   `ResizeToContents` 按内容算的，而通用规则只有 `padding: 2px 4px` / `4px 6px` ——
   实测文字与列宽**几乎没有余量**（市值列 28 像素顶着 28 像素的表头），
   换台机器（Windows 微软雅黑更宽）或系统缩放 125% 就会切字。
   多给的这几像素会被 `ResizeToContents` 吃进列宽里，所以文字两边真的空出来了；
   别的表（自选标的、持仓、大盘）**不在这条规则的射程内**，版式不变。 */
QTableWidget#resultTable::item { padding: 2px 8px; }
QTableWidget#resultTable QHeaderView::section { padding: 4px 9px; }

/* ── 滚动区与滚动条 ─────────────────────────────────────── */
QScrollArea { border: none; background-color: transparent; }
QScrollArea > QWidget > QWidget { background-color: transparent; }
QScrollBar:vertical {
    background-color: transparent; width: 10px; margin: 0;
    border: none; border-radius: 5px;
}
QScrollBar:horizontal {
    background-color: transparent; height: 10px; margin: 0;
    border: none; border-radius: 5px;
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
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                      stop:0 $card_top, stop:1 $card_bottom);
    border: 1px solid $border; border-radius: 10px;
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


def resolve_palette_name(name: Any) -> str:
    """把 `name` 收紧到**能渲染的**配色名（`PALETTES` 里有色板的那种）。

    与 `normalize_theme()` 的分工：那个管"用户能选的皮肤"（`config.UI_THEMES`，
    写错一律回默认）；这个管"**渲染得出来**的配色"（多了两套只用于出对比图的候选）。
    出预览图时按候选名渲染，但设置页下拉框里不会出现它们（见 `PREVIEW_THEMES`）。
    """
    value = str(name if name is not None else "").strip().lower()
    if value == THEME_SYSTEM:        # 空样式表那一档：保留原样，行为与改版前一致
        return THEME_SYSTEM
    return value if value in PALETTES else DEFAULT_UI_THEME


def palette(name: Any = None) -> dict[str, str]:
    """取某套配色的色板（默认取当前生效的那套）。**界面代码不要再手写色值。**"""
    key = resolve_palette_name(name if name is not None else current_theme())
    if key not in PALETTES:
        key = DEFAULT_UI_THEME
    return PALETTES[key]


def chrome_colors() -> dict[str, str]:
    """自绘标题栏要用的那几个颜色（那是 QPainter 画的，QSS 管不着）。

    `system` 那一档按**浅色**给：那一档用的是系统标题栏（我们不画），万一被打开
    也不该画成深色的 —— Windows 原生外观在浅色模式下是浅底的。
    """
    key = current_theme() if current_theme() in PALETTES else THEME_SILVER
    colors = PALETTES[key]
    return {
        "top": colors["titlebar_top"],
        "bottom": colors["titlebar_bottom"],
        "border": colors["titlebar_border"],
        "text": colors["titlebar_text"],
        "dim": colors["titlebar_dim"],
        "hover": colors["hover_soft"],
        "pressed": colors["btn_press_bottom"],
        "close_hover": colors["close_hover"],
        "accent": colors["accent"],
        "border_soft": colors["border"],
    }


def _bar_gradient(colors: dict[str, str]) -> str:
    """背景条（状态区 / 表头）的渐变：**由色板拼出来**，不再写死色值。

    深色皮肤不能贴银色那张浅色拉丝纹理（贴上去就是一块脏斑），所以除银色之外的
    主题都走这里：一段同色系的纵向渐变。
    """
    return ("background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,\n"
            "                                      stop:0 %s, stop:1 %s);"
            % (colors["section_top"], colors["section"]))


def theme_qss(name: str, metal_url: str | None = None) -> str:
    """按主题名渲染样式表；`metal_url=None` 表示**不用纹理**（背景条退回纯色）。

    所有皮肤共用同一张模板（`_SILVER_TEMPLATE`）与同一批键名，只是换一份色板：
    这样"按钮长什么样、圆角多少、间距多少"只有一处定义 —— 复制一份模板出来改，
    迟早出现"银色改了、科技蓝没改"这种半新半旧的界面。

    纹理只是锦上添花：素材没打进包、被删掉、换名字，对应那行 `background-image`
    直接不出现，背景条仍然是纯色。缺素材绝不能让界面出错，也不能变成半透明的破版。
    深色皮肤**不用**那张拉丝纹理，改用一段同色系渐变（见 `_bar_gradient`）。
    """
    theme = resolve_palette_name(name)
    if theme in (THEME_SILVER, THEME_SYSTEM):
        colors = SILVER_COLORS
        metal = ""
        if metal_url:
            metal = ("background-image: url(%s);\n"
                     "    background-repeat: repeat-x;\n"
                     "    background-position: top left;" % metal_url)
    else:
        colors = PALETTES[theme]
        metal = _bar_gradient(colors)
    return _SILVER_TEMPLATE.substitute(metal=metal, **colors)


def silver_qss(metal_url: str | None = None) -> str:
    """银色主题的 QSS（保留这个名字：一批用例与截图脚本按它取"浅色皮肤"的渲染结果）。"""
    return theme_qss(THEME_SILVER, metal_url)



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

#: 模块导入时算一次（拿不到素材就是纯色版）；`apply_theme` 每次应用时会**重新**解析路径。
#: 放在 `normalize_theme()` 之后：`theme_qss()` 要用它把主题名收紧，而模块级这几行
#: 是在 import 时执行的 —— 顺序反了就是 `NameError`（本轮踩过）。
SILVER_QSS = silver_qss()
TECH_QSS = theme_qss(THEME_TECH)

#: 主题名 → 样式表。`system` 是空串：把样式表清空 = 回到系统原生外观（安全绳）
THEMES: dict[str, str] = {
    THEME_TECH: TECH_QSS,
    THEME_SILVER: SILVER_QSS,
    THEME_SYSTEM: "",
}


def current_theme() -> str:
    """当前生效的主题名（由 `apply_theme` 维护，默认科技蓝）。"""
    return _CURRENT["name"]


def is_dark() -> bool:
    """当前主题是不是**深色底**。

    给"要自己画颜色"的控件用（热力图、桌宠气泡、自绘浮窗）：它们画的是像素，
    不是 QSS，没法靠样式表跟着换色 —— 只能问一句"现在是不是深色底"，
    再决定用哪一档色（见 `data/market_map.block_color(dark=...)`）。
    `system` 按**浅色**处理：Qt 原生外观在 Windows 上是浅色的，猜深了会让文字看不见。
    判据是 `DARK_THEMES`（不是"等于 tech"）：候选配色换上去之后也全是深色底。
    """
    return current_theme() in DARK_THEMES


def semantic(kind: str) -> str:
    """当前主题下的语义色：`"up"`（涨）/ `"down"`（跌）。

    浅色主题沿用 `market.COLOR_UP/DOWN`（`#d32f2f` / `#2e7d32`，全项目核过的那两档）；
    深色主题换成亮一档的（见 `TECH_SEMANTIC` 的对比度说明）。**判据仍然只有一处**
    （`market.value_color`），这里只负责"换个主题换一档色"。
    """
    from laoa_trader import market          # 延迟导入：market 不依赖主题，避免环

    key = "up" if str(kind).lower() in ("up", "涨") else "down"
    base = market.COLOR_UP if key == "up" else market.COLOR_DOWN
    return SEMANTIC_BY_THEME.get(current_theme(), {}).get(key, base)


def value_color(change_pct: Any) -> str:
    """涨跌 → 颜色（**按当前主题给**）。

    与 `market.value_color()` 是同一套判据（>0 红、<0 绿、平盘或取不到返回空串），
    差别只在色值按主题调一档 —— 界面代码用这一个，别直接调 `market.value_color`
    （否则深色主题上会出现"深绿字配深蓝底"那种看不清的组合）。
    """
    from laoa_trader import market

    base = market.value_color(change_pct)
    if not base:
        return base
    if base == market.COLOR_UP:
        return semantic("up")
    if base == market.COLOR_DOWN:
        return semantic("down")
    return base


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

    送进来的名字按"**渲染得出来**"收紧（不认识就回默认）：这既保住了"写错一个字母
    不该让界面起不来"，也让候选配色能被真的画出来（出对比图用，见 `PREVIEW_THEMES`）。
    用户**能选**的范围仍然是 `config.UI_THEMES`（设置页下拉框与 `theme_label` 用它）。
    """
    return apply_palette(
        app,
        resolve_palette_name(name if name is not None else current_theme()),
    )


def apply_palette(app: Any = None, name: Any = None) -> str:
    """按**配色名**应用样式表（候选配色也能渲染），返回实际生效的名字。

    与 `apply_theme` 的差别只有一处：这个不把名字收紧到 `UI_THEMES`，
    所以 `apply_palette(app, "obsidian")` 能真的把那套候选配色画出来（出对比图用）。
    """
    theme = resolve_palette_name(name if name is not None else current_theme())
    if theme == THEME_SYSTEM:
        qss = THEMES[THEME_SYSTEM]        # 空串 = 清空样式表，回到系统原生
    else:
        # 每次应用都重新渲染（纹理路径要按**当前**素材与屏幕缩放解析，见 docstring）
        qss = theme_qss(theme, texture_url() if theme == THEME_SILVER else None)
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
