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

皮肤与配色（2026-10-10 定稿：默认「午夜靛紫」，其余几套也都能选）
------------------------------------------------------------------
- **六套彩色皮肤 + 银色 + 系统默认**，设置页下拉框里全都能选（`config.UI_THEMES`）：
  午夜靛紫（`indigo`，**默认**）、深海宝蓝（`sapphire`）、石墨琥珀金（`amber`）、
  暗夜玫红（`rose`）、哑光雾蓝（`mist`）、浅色高级灰（`paper`）、
  银色（`silver`）、系统默认（`system`）。
  主人 2026-10-10 的原话是"默认选午夜靛紫 —— 其它几套也可以放在程序包里供用户选择"，
  所以这几套**都是正式皮肤**，不是"临时候选"（早先那套科技蓝青色在挑配色时被否掉了）。
- **深色底上涨跌色要换一档**（`#2e7d32` 在深蓝上只有 ~2:1，数字会糊掉），
  所以语义色按主题给（见 `value_color`）。
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

#: 主题名（与 `config.UI_THEMES` 同一份取值，避免两处各写一份）。
#: 顺序 = 设置页下拉框的顺序 = 主人挑配色时的编号顺序（D/E/F/G/H/I）。
THEME_INDIGO = "indigo"          # 午夜靛紫（**默认**）
THEME_SAPPHIRE = "sapphire"      # 深海宝蓝
THEME_AMBER = "amber"            # 石墨琥珀金
THEME_ROSE = "rose"              # 暗夜玫红
THEME_MIST = "mist"              # 哑光雾蓝（低饱和）
THEME_PAPER = "paper"            # 浅色高级灰
THEME_SILVER = "silver"          # 银色（金属感，浅色）
THEME_SYSTEM = "system"          # 系统默认（空样式表）

#: 旧主题名 → 现在按哪套渲染。`tech` 是 2026-10-08~10-10 那套青色皮肤的配置值，
#: 主人自己的 `config.toml` 里就写着它 —— 不能因为改名就让他的界面"莫名其妙变回默认"。
LEGACY_THEME_ALIASES: dict[str, str] = {
    "tech": THEME_INDIGO,
}

#: 下拉框里的中文名（用户看到的是这个，不是 indigo/silver/system）
THEME_LABELS: dict[str, str] = {
    THEME_INDIGO: "午夜靛紫（默认·深色）",
    THEME_SAPPHIRE: "深海宝蓝（深色）",
    THEME_AMBER: "石墨琥珀金（深色）",
    THEME_ROSE: "暗夜玫红（深色）",
    THEME_MIST: "哑光雾蓝（深色·低饱和）",
    THEME_PAPER: "浅色高级灰",
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

#: 深色底上的**语义色**（红涨绿跌）——比浅色底那两档亮一档。
#: 为什么必须分开：#2e7d32（深绿）在 #0b1220 上的对比度只有 ~2:1，盈亏数字会糊在背景里；
#: 而浅色主题上不能换色（用户与用例都按 #d32f2f / #2e7d32 核过）。
DARK_SEMANTIC: dict[str, str] = {
    "up": "#ff6b6b",      # 涨（6.8:1）
    "down": "#3ddc84",    # 跌（10.5:1）
}

#: 主题名 → 语义色覆盖（没列到的主题走 `market.COLOR_UP/COLOR_DOWN`）。
#: 深色皮肤那几套在下面按 `DARK_THEMES` 一次性登记。
SEMANTIC_BY_THEME: dict[str, dict[str, str]] = {}

# ── 色板生成器：候选配色由"三块底色 + 一个强调色"推出来 ──────────────────
#
# 为什么用生成而不是一套套手抄：手抄一套就是 50 行容易漏键的数字，而"层次"本来就是
# **相对关系**（卡片比底色亮一档、边框比卡片再亮一档、悬停又比常态亮一档）——
# 写成公式既不容易错，也保证"换一批候选"不用重算几十个色值。
# 安全性由用例兜着：`test_every_palette_defines_every_key` 查键名齐全、
# `test_every_palette_text_is_readable` 查对比度（AA 4.5:1）。

def _mix(first: str, second: str, ratio: float) -> str:
    """两个 `#rrggbb` 线性混合（0 = 全取 first，1 = 全取 second）。"""
    a = [int(first[index:index + 2], 16) for index in (1, 3, 5)]
    b = [int(second[index:index + 2], 16) for index in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        round(x + (y - x) * ratio) for x, y in zip(a, b))


def _lighten(color: str, amount: float) -> str:
    return _mix(color, "#ffffff", amount)


def _darken(color: str, amount: float) -> str:
    return _mix(color, "#000000", amount)


def _luminance(color: str) -> float:
    """相对亮度（WCAG 的定义），只用来算对比度。"""
    parts = []
    for index in (1, 3, 5):
        value = int(color[index:index + 2], 16) / 255.0
        parts.append(value / 12.92 if value <= 0.03928
                     else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast_ratio(first: str, second: str) -> float:
    """两个颜色的对比度（1 ~ 21）。界面里"字看不看得清"就按它判（正文要 ≥ 4.5）。"""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


#: 下面这个函数的两个候选（亮底上用深字 / 深底上用亮字）
_TEXT_ON_DARK = "#0a1522"
_TEXT_ON_LIGHT = "#f7fbff"


def _primary_gradient(accent: str, accent_deep: str) -> dict[str, str]:
    """主按钮的"字色 + 四态"：两种方向各算一遍，取**最差状态对比度更高**的那种。

    两个方向是成对的，不能只挑字色：
      * 亮底 + 深字 → 悬停**提亮**、按下压暗（越亮越清楚）；
      * 深底 + 亮字 → 悬停/按下都**加深**（越深越清楚）。
    反着做就会出现"悬停之后字反而看不清了"，实测踩过。
    判据就是对比度，让色板自己算 —— 主人挑配色可能还要几轮，不能每套都手调。
    """
    plans = (
        # (字色, 悬停上, 悬停下, 按下上, 按下下)
        (_TEXT_ON_DARK, _lighten(accent, 0.16), _lighten(accent_deep, 0.12),
         _darken(accent, 0.14), _darken(accent_deep, 0.16)),
        (_TEXT_ON_LIGHT, _darken(accent, 0.08), _darken(accent_deep, 0.06),
         _darken(accent, 0.20), _darken(accent_deep, 0.18)),
    )

    def worst(plan: tuple[str, ...]) -> float:
        text = plan[0]
        return min(contrast_ratio(text, background)
                   for background in (accent, accent_deep, *plan[1:]))

    text, hover_top, hover_bottom, press_top, press_bottom = max(plans, key=worst)
    return {
        "primary_text": text,
        "primary_hover_top": hover_top,
        "primary_hover_bottom": hover_bottom,
        "primary_press_top": press_top,
        "primary_press_bottom": press_bottom,
    }


def _dark_palette(*, window: str, panel: str, section: str,
                  accent: str, accent_deep: str,
                  text: str = "#e9eff7", text_dim: str = "#94a4bb",
                  text_disabled: str = "#7d8ea6") -> dict[str, str]:
    """深色皮肤：由"窗口底 / 卡片底 / 背景条底 / 强调色（常态·按下）"推出全套色值。"""
    primary = _primary_gradient(accent, accent_deep)
    return {
        "window": window,
        "panel": panel,
        "section": section,
        "border": _lighten(panel, 0.14),
        "border_dark": _lighten(panel, 0.26),
        "text": text,
        "text_dim": text_dim,
        "text_disabled": text_disabled,
        "btn_top": _lighten(panel, 0.11),
        "btn_bottom": _lighten(panel, 0.04),
        "btn_border": _lighten(panel, 0.20),
        "btn_hover_top": _lighten(panel, 0.17),
        "btn_hover_bottom": _lighten(panel, 0.09),
        "btn_press_top": _darken(panel, 0.10),
        "btn_press_bottom": _darken(panel, 0.18),
        "btn_disabled_bg": _mix(panel, window, 0.5),
        "primary_top": accent,
        "primary_bottom": accent_deep,
        "primary_border": _darken(accent_deep, 0.10),
        **primary,
        "tab_unselected": _darken(panel, 0.06),
        "tab_selected": panel,
        "tab_accent": accent,
        "tab_hover": _lighten(panel, 0.08),
        "alt_row": _mix(panel, window, 0.45),
        "grid": _lighten(panel, 0.09),
        "selection": _mix(panel, accent, 0.28),
        "selection_strong": _mix(panel, accent, 0.44),
        "focus": accent,
        "progress_top": accent,
        "progress_bottom": accent_deep,
        "progress_track": section,
        "scrollbar": _lighten(panel, 0.22),
        "scrollbar_hover": _lighten(panel, 0.34),
        "tooltip_bg": section,
        "window_top": _lighten(window, 0.05),
        "window_bottom": _darken(window, 0.04),
        "card_top": _lighten(panel, 0.06),
        "card_bottom": _darken(panel, 0.03),
        "section_top": _lighten(section, 0.09),
        "titlebar_top": _lighten(section, 0.05),
        "titlebar_bottom": _darken(section, 0.05),
        "titlebar_border": _lighten(panel, 0.14),
        "titlebar_text": text,
        "titlebar_dim": text_dim,
        "accent": accent,
        "accent_dim": _mix(panel, accent, 0.42),
        "hover_soft": _lighten(panel, 0.08),
        "close_hover": "#e5484d",
    }


def _light_palette(*, window: str, panel: str, section: str,
                   accent: str, accent_deep: str,
                   text: str = "#1f2328", text_dim: str = "#5f6b7a",
                   text_disabled: str = "#9aa1ab") -> dict[str, str]:
    """浅色皮肤：同一批键名，层次的方向反过来（越靠前越亮，边框往下压）。"""
    primary = _primary_gradient(accent, accent_deep)
    return {
        "window": window,
        "panel": panel,
        "section": section,
        "border": _darken(section, 0.10),
        "border_dark": _darken(section, 0.20),
        "text": text,
        "text_dim": text_dim,
        "text_disabled": text_disabled,
        "btn_top": panel,
        "btn_bottom": _darken(panel, 0.10),
        "btn_border": _darken(panel, 0.24),
        "btn_hover_top": _lighten(panel, 0.02),
        "btn_hover_bottom": _darken(panel, 0.06),
        "btn_press_top": _darken(panel, 0.12),
        "btn_press_bottom": _darken(panel, 0.18),
        "btn_disabled_bg": _darken(panel, 0.05),
        "primary_top": accent,
        "primary_bottom": accent_deep,
        "primary_border": _darken(accent_deep, 0.10),
        **primary,
        "tab_unselected": section,
        "tab_selected": panel,
        "tab_accent": accent,
        "tab_hover": _darken(panel, 0.04),
        "alt_row": _darken(panel, 0.02),
        "grid": _darken(panel, 0.08),
        "selection": _mix(panel, accent, 0.14),
        "selection_strong": _mix(panel, accent, 0.26),
        "focus": accent,
        "progress_top": _lighten(accent, 0.10),
        "progress_bottom": accent_deep,
        "progress_track": _darken(panel, 0.06),
        "scrollbar": _darken(panel, 0.18),
        "scrollbar_hover": _darken(panel, 0.28),
        "tooltip_bg": panel,
        "window_top": _lighten(window, 0.30),
        "window_bottom": _darken(window, 0.04),
        "card_top": panel,
        "card_bottom": _darken(panel, 0.03),
        "section_top": _lighten(section, 0.30),
        "titlebar_top": _lighten(section, 0.35),
        "titlebar_bottom": section,
        "titlebar_border": _darken(section, 0.10),
        "titlebar_text": text,
        "titlebar_dim": text_dim,
        "accent": accent,
        "accent_dim": _lighten(accent, 0.55),
        "hover_soft": _darken(panel, 0.05),
        "close_hover": "#c62828",
    }


# ── 各套皮肤的色板（2026-10-10：主人挑定「午夜靛紫」为默认，其余几套也都能选）──
#
# 取色时的两条硬约束（不是我瞎讲究，A 股界面踩了就难看）：
#   1. **强调色不能是红/绿**：A 股红涨绿跌，强调色撞上语义色就会让人分不清
#      "这是涨"还是"这是按钮"。所以强调色只用蓝、紫、金、玫；
#   2. 正文对比度 ≥ 4.5:1（WCAG AA），深色皮肤最容易翻车的就是"深绿字配深蓝底"
#      —— 这几套都由用例按对比度核过（`tests/test_theme.py`）。
#
# ── 六套皮肤（主人 2026-10-10 挑的这批；全都会出现在设置页下拉框里）──
#
# 刻意拉开距离，覆盖"不同人/不同心情想要的方向"：四个有色调的（宝蓝/靛紫/琥珀金/玫红）、
# 一个**低饱和**的哑光灰蓝（"不想要花哨"）、一个**浅色**（"看不惯深色"）。
# 全部由 `_dark_palette` / `_light_palette` 生成：键名与层次关系完全同构。

#: 深海宝蓝：最保守的一条深色路（金融软件的老配色），蓝得稳、不刺眼。
SAPPHIRE_COLORS: dict[str, str] = _dark_palette(
    window="#08101f", panel="#101a2e", section="#16233c",
    accent="#2a63d6", accent_deep="#1f4fc4",
)

#: 午夜靛紫：比上一版那套紫罗兰更冷更沉，偏"设计工具"的气质。
INDIGO_COLORS: dict[str, str] = _dark_palette(
    window="#0b0a18", panel="#14132a", section="#1b1936",
    accent="#6a55e6", accent_deep="#4f39c9",
)

#: 石墨琥珀金：暖金强调，深棕灰底 —— 唯一一套"暖"的，像老式终端与铜质仪表。
AMBER_COLORS: dict[str, str] = _dark_palette(
    window="#100e0a", panel="#1a1712", section="#221d16",
    accent="#e0a63c", accent_deep="#c2871d",
    text="#f2ece1", text_dim="#a99c86", text_disabled="#8d8069",
)

#: 暗夜玫红：强调色是玫红（不是正红，不会跟"涨"混），有性格但底子很暗。
ROSE_COLORS: dict[str, str] = _dark_palette(
    window="#130c12", panel="#1d131b", section="#261822",
    accent="#f586bd", accent_deep="#e0629f",
)

#: 哑光雾蓝：**低饱和**那条路 —— 强调色故意发灰（#7ba7c9），像哑光金属而不是霓虹灯。
MIST_COLORS: dict[str, str] = _dark_palette(
    window="#0d1216", panel="#151c21", section="#1b242b",
    accent="#7ba7c9", accent_deep="#5b8aa8",
    text="#e6ecef", text_dim="#93a3ac", text_disabled="#7d8b93",
)

#: 浅色高级灰：**唯一一套浅色**（深色看不惯就走这条），底子偏冷、强调色靛蓝。
PAPER_COLORS: dict[str, str] = _light_palette(
    window="#f2f4f8", panel="#ffffff", section="#e8ecf3",
    accent="#4f46e5", accent_deep="#4338ca",
)

#: 主题名 → 色板（**所有能渲染的皮肤都在这里**；`system` 没有色板 —— 它是空样式表）。
#: `theme_qss()` 只认这张表；`config.UI_THEMES` 与它必须一一对应（有用例钉着）。
PALETTES: dict[str, dict[str, str]] = {
    THEME_INDIGO: INDIGO_COLORS,
    THEME_SAPPHIRE: SAPPHIRE_COLORS,
    THEME_AMBER: AMBER_COLORS,
    THEME_ROSE: ROSE_COLORS,
    THEME_MIST: MIST_COLORS,
    THEME_PAPER: PAPER_COLORS,
    THEME_SILVER: SILVER_COLORS,
}

#: 深色底的主题（要自己画颜色的控件、热力图、自绘标题栏都按这个判断）。
#: **不能只写默认那一个**：换任何一套深色皮肤都得换色，`PAPER`/`SILVER` 不在里面。
DARK_THEMES: tuple[str, ...] = (
    THEME_INDIGO, THEME_SAPPHIRE, THEME_AMBER, THEME_ROSE, THEME_MIST,
)

for _name in DARK_THEMES:
    SEMANTIC_BY_THEME.setdefault(_name, DARK_SEMANTIC)

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
    出对比图时按配色名直接渲染（`apply_palette`），不看用户能选什么。
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
    迟早出现"银色改了、别的皮肤没改"这种半新半旧的界面。

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
    value = LEGACY_THEME_ALIASES.get(value, value)      # 旧名（tech）先翻译
    return value if value in UI_THEMES else DEFAULT_UI_THEME


def theme_label(name: Any) -> str:
    """主题的中文名（设置页下拉框显示用）。"""
    return THEME_LABELS.get(normalize_theme(name), THEME_LABELS[DEFAULT_UI_THEME])

#: 模块导入时算一次（拿不到素材就是纯色版）；`apply_theme` 每次应用时会**重新**解析路径。
#: 放在 `normalize_theme()` 之后：`theme_qss()` 要用它把主题名收紧，而模块级这几行
#: 是在 import 时执行的 —— 顺序反了就是 `NameError`（本轮踩过）。
SILVER_QSS = silver_qss()
DEFAULT_QSS = theme_qss(DEFAULT_UI_THEME)

#: 主题名 → 样式表（键集合必须与 `config.UI_THEMES` 一致，有用例钉着）。
#: `system` 是空串：把样式表清空 = 回到系统原生外观（安全绳）
THEMES: dict[str, str] = {
    name: (DEFAULT_QSS if name == DEFAULT_UI_THEME else theme_qss(name))
    for name in UI_THEMES if name != THEME_SYSTEM
}
THEMES[THEME_SYSTEM] = ""


def current_theme() -> str:
    """当前生效的主题名（由 `apply_theme` 维护，默认午夜靛紫）。"""
    return _CURRENT["name"]


def is_dark() -> bool:
    """当前主题是不是**深色底**。

    给"要自己画颜色"的控件用（热力图、桌宠气泡、自绘浮窗）：它们画的是像素，
    不是 QSS，没法靠样式表跟着换色 —— 只能问一句"现在是不是深色底"，
    再决定用哪一档色（见 `data/market_map.block_color(dark=...)`）。
    `system` 按**浅色**处理：Qt 原生外观在 Windows 上是浅色的，猜深了会让文字看不见。
    判据是 `DARK_THEMES`：换任何一套深色皮肤都得跟着换色（浅色那几套不在里面）。
    """
    return current_theme() in DARK_THEMES


def semantic(kind: str) -> str:
    """当前主题下的语义色：`"up"`（涨）/ `"down"`（跌）。

    浅色主题沿用 `market.COLOR_UP/DOWN`（`#d32f2f` / `#2e7d32`，全项目核过的那两档）；
    深色主题换成亮一档的（见 `DARK_SEMANTIC` 的对比度说明）。**判据仍然只有一处**
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
    不该让界面起不来"，也让任意一套色板都能被直接渲染出来（出对比图用）。
    """
    return apply_palette(
        app,
        resolve_palette_name(name if name is not None else current_theme()),
    )


def apply_palette(app: Any = None, name: Any = None) -> str:
    """按**配色名**应用样式表（候选配色也能渲染），返回实际生效的名字。

    与 `apply_theme` 的差别只有一处：这个不把名字收紧到 `UI_THEMES`，
    所以 `apply_palette(app, "amber")` 能真的把那套色板画出来（出对比图用，
    不经过 `normalize_theme` 那道"用户能选什么"的收紧）。
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
