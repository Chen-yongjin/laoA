"""界面主题（皮肤）的**纯逻辑**用例：取值归一、QSS 健壮性、缺素材降级。

为什么单独一个文件、而且**不 import Qt**：这一层（`config.ui_theme` 与
`ui/theme.py` 的字符串部分）是"没有图形环境也必须正确"的部分 ——
CLI、打包前的静态检查、服务器版共存的机器上都可能没有 PySide6。
用到 QApplication 的用例（设置页下拉框切换、窗口换主题后照常刷新）
在 `tests/test_ui_smoke.py` 里，那边本来就有离屏 Qt 的脚手架。

这里盯的三件事：
1. **主题名归一**：默认银色、非法值回默认、环境变量覆盖与回退；
2. **QSS 本身是"健全"的**：花括号配平、没有空规则块、关键选择器都在
   （少一个 `}` 或写空一块，Qt 会把后面整段样式默默丢掉 —— 界面上表现为"半旧半新"，
   而不会报错，所以必须由用例来盯）；
3. **素材缺失不许崩**：纹理拿不到时只是不铺纹理，QSS 里连 `url()` 都不该出现。
"""

from __future__ import annotations

import re

import pytest

from laoa_trader import assets, config as config_mod
from laoa_trader.ui import theme

from tests._toml import p


# ── 1) 主题名归一（配置 / 环境变量）──


def test_theme_defaults_to_indigo_and_offers_every_palette(cfg) -> None:
    """默认是**午夜靛紫**（主人 2026-10-10 从六套配色里挑的），其余几套照样能选
    （原话："默认选午夜靛紫 —— 其它几套也可以放在程序包里供用户选择"）。

    这里同时钉住"取值表只有一处"：`config.UI_THEMES` 与 `theme.THEMES` 的键集合必须一致，
    而 `theme.PALETTES` 正好是除 `system`（空样式表）之外的那些。
    """
    assert config_mod.DEFAULT_UI_THEME == "indigo"
    assert config_mod.UI_THEMES == (
        "indigo", "sapphire", "amber", "rose", "mist", "paper", "silver", "system",
    )
    assert config_mod.Config().ui_theme == "indigo"
    assert cfg.ui_theme == "indigo"
    # 六套彩色皮肤一个都不能少（少一套 = 设置页里少一个选项）
    for name in (theme.THEME_INDIGO, theme.THEME_SAPPHIRE, theme.THEME_AMBER,
                 theme.THEME_ROSE, theme.THEME_MIST, theme.THEME_PAPER,
                 theme.THEME_SILVER, theme.THEME_SYSTEM):
        assert name in config_mod.UI_THEMES
    assert set(theme.THEMES) == set(config_mod.UI_THEMES)
    assert set(theme.PALETTES) == set(config_mod.UI_THEMES) - {"system"}


@pytest.mark.parametrize("raw", ["system", " silver ", "SILVER", "System"])
def test_theme_accepts_valid_values(raw) -> None:
    assert config_mod.Config(ui_theme=raw).ui_theme == raw.strip().lower()
    assert theme.normalize_theme(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", None, "银色", "neon", "SILVE", 0, "1", "indigoo"])
def test_theme_falls_back_to_default_on_bad_value(raw) -> None:
    """非法值一律回默认 —— 写错一个字母不该让界面起不来。"""
    assert config_mod.Config(ui_theme=raw).ui_theme == "indigo"
    assert theme.normalize_theme(raw) == "indigo"
    assert theme.theme_label(raw) == theme.THEME_LABELS["indigo"]


def test_old_tech_theme_name_maps_to_the_new_default() -> None:
    """旧值 `tech`（2026-10-08~10-10 那套青色皮肤）**翻译成现在的默认**。

    为什么必须认这个旧名：主人自己的 `config.toml` 里就写着 `ui_theme = "tech"`，
    不认它就会"回默认"—— 结果一样，但含义变了（他以为在选旧的青色，实际拿到靛紫）。
    认了它，升级后界面就是他挑的那套，且不会在配置里留下一个"非法值"。
    """
    assert theme.normalize_theme("tech") == "indigo"
    assert theme.normalize_theme(" TECH ") == "indigo"
    assert config_mod.Config(ui_theme="tech").ui_theme == "indigo"
    assert "tech" not in config_mod.UI_THEMES


def test_theme_from_config_file_and_env(tmp_path, monkeypatch) -> None:
    """config.toml 打底、环境变量覆盖；环境变量写错也回默认。"""
    path = tmp_path / "config.toml"
    # 路径必须转义：Windows 上 `C:\Users\...` 里的 `\U` 会让 tomllib 报
    # "Invalid hex value"、整个配置回退默认值 —— 那样断言的是默认值，不是被测行为
    path.write_text(
        f'data_dir = "{p(tmp_path)}"\nui_theme = "system"\n', encoding="utf-8"
    )
    monkeypatch.delenv("UI_THEME", raising=False)
    assert config_mod.load_config(path).ui_theme == "system"

    monkeypatch.setenv("UI_THEME", "silver")
    assert config_mod.load_config(path).ui_theme == "silver"     # 环境变量盖过文件

    monkeypatch.setenv("UI_THEME", "乱写的")
    assert config_mod.load_config(path).ui_theme == "indigo"     # 非法 → 回默认

    monkeypatch.setenv("UI_THEME", "System")
    assert config_mod.load_config(path).ui_theme == "system"     # 大小写不敏感


def test_theme_labels_are_chinese_and_distinct() -> None:
    """下拉框里的中文名：八项都要有、互不重复、默认那项写着"默认"。"""
    assert theme.theme_label("indigo") == "午夜靛紫（默认·深色）"
    assert theme.theme_label("silver") == "银色（金属感）"
    assert theme.theme_label("system") == "系统默认"
    labels = [theme.theme_label(name) for name in config_mod.UI_THEMES]
    assert len(set(labels)) == len(labels) == len(config_mod.UI_THEMES)
    assert "默认" in theme.theme_label(config_mod.DEFAULT_UI_THEME)


def test_current_theme_tracks_applied(monkeypatch) -> None:
    """`current_theme()` 反映**实际应用**的主题（不是"配置里写了什么"）。"""
    assert theme.current_theme() == "indigo"
    theme.apply_theme(_FakeApp(), "system")
    assert theme.current_theme() == "system"
    theme.apply_theme(_FakeApp(), "乱写的")
    assert theme.current_theme() == "indigo"          # 非法值回默认，不留在"乱写的"


class _FakeApp:
    """只有 `setStyleSheet` 的假 QApplication（这一层不需要真的 Qt）。"""

    def __init__(self) -> None:
        self.qss = None

    def setStyleSheet(self, qss: str) -> None:  # noqa: N802 - 与 Qt 的命名保持一致
        self.qss = qss


def test_apply_theme_sets_and_clears_stylesheet() -> None:
    """`system` 主题 = 空样式表（回到原生外观）——这就是"一键切回"的安全绳。"""
    app = _FakeApp()
    assert theme.apply_theme(app, "silver") == "silver"
    assert "qlineargradient" in app.qss and "QPushButton#primaryAction" in app.qss
    assert theme.apply_theme(app, "system") == "system"
    assert app.qss == ""


def test_apply_theme_swallows_broken_app() -> None:
    """`setStyleSheet` 抛异常（极端环境）也不能让程序崩 —— 换皮肤是外观操作。"""
    class Broken:
        def setStyleSheet(self, qss: str) -> None:  # noqa: N802
            raise RuntimeError("窗口系统坏了")

    assert theme.apply_theme(Broken(), "silver") == "silver"


# ── 2) QSS 健全性 ──


def _strip_comments(qss: str) -> str:
    return re.sub(r"/\*.*?\*/", "", qss, flags=re.S)


def test_qss_braces_are_balanced() -> None:
    """花括号配平：少一个 `}` 会让 Qt 默默丢掉后面一大段样式（界面"半旧半新"）。"""
    qss = _strip_comments(theme.SILVER_QSS)
    depth = 0
    for char in qss:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        assert depth >= 0, "出现了多余的 `}`"
    assert depth == 0, f"花括号没有配平（还剩 {depth} 层）"


def test_qss_has_no_empty_rule() -> None:
    """没有空规则块：`QPushButton { }` 这种在 Qt 里等于把该控件的默认样式清空。"""
    qss = _strip_comments(theme.SILVER_QSS)
    for block in re.findall(r"\{([^{}]*)\}", qss):
        assert block.strip(), "存在空的样式规则块"


REQUIRED_SELECTORS: tuple[str, ...] = (
    # 底色与文字
    "QWidget", "QMainWindow", "QLabel", "QDialog", "QToolTip", "QFrame",
    # 按钮（含主操作按钮的精确命中）
    "QPushButton", "QPushButton:hover", "QPushButton:pressed",
    "QPushButton:disabled", "QPushButton:default", "QPushButton#primaryAction",
    # 输入类
    "QLineEdit", "QComboBox", "QComboBox QAbstractItemView", "QSpinBox",
    "QCheckBox", "QRadioButton",
    # 页签 / 表格 / 滚动 / 进度 / 菜单 / 分组
    "QTabWidget::pane", "QTabBar::tab", "QTabBar::tab:selected",
    "QTableWidget", "QHeaderView::section", "QScrollArea",
    "QScrollBar:vertical", "QScrollBar::handle:vertical",
    "QProgressBar", "QProgressBar::chunk", "QMenu", "QGroupBox",
    # 概览页与股票池的卡片、背景条
    "QFrame#marketKpiCard", "QFrame#poolCard", "QWidget#statusArea",
    "QWidget#marketFooter", "QLabel#statusTag",
)


def test_qss_covers_every_selector_the_ui_uses() -> None:
    """项目实际用到的控件都要覆盖到（漏一个就会出现"半旧半新"的界面）。"""
    qss = _strip_comments(theme.SILVER_QSS)
    missing = [sel for sel in REQUIRED_SELECTORS if sel not in qss]
    assert missing == [], f"这些选择器没用样式覆盖：{missing}"


def test_qss_colors_come_from_one_palette() -> None:
    """色值只有 `SILVER_COLORS` 一处定义，模板里不许再手写十六进制色。"""
    qss = theme.silver_qss('"/tmp/x.png"')
    literal = {
        m.group(0).lower()
        for m in re.finditer(r"#[0-9a-fA-F]{6}", qss)
    }
    allowed = {value.lower() for value in theme.SILVER_COLORS.values()}
    allowed.add("#ffffff")          # 主操作按钮的白字（在模板里写死，语义色不是主题色）
    assert literal <= allowed, f"样式表里有来路不明的色值：{literal - allowed}"


def test_qss_keeps_semantic_colors_readable() -> None:
    """涨红跌绿仍然是那套语义色（银底上不许被主题改掉）。"""
    from laoa_trader import market

    assert (market.COLOR_UP, market.COLOR_DOWN) == ("#d32f2f", "#2e7d32")
    qss = _strip_comments(theme.SILVER_QSS)
    assert market.COLOR_UP.lower() not in qss.lower()      # 主题不去碰这两个色
    assert market.COLOR_DOWN.lower() not in qss.lower()


# ── 3) 素材（纹理）──


def test_texture_asset_exists_and_is_used() -> None:
    """纹理图随包分发，而且真的被写进 QSS（绝对路径 + 只横向平铺）。"""
    path = assets.ui_asset(theme.TEXTURE_NAME)
    assert path is not None and path.is_file(), "纹理素材没随包（build/make_ui_assets.py 生成）"
    assert path.parent.name == "ui"
    assert assets.ui_asset(theme.TEXTURE_NAME_2X) is not None, "缺 @2x 那张"

    qss = theme.silver_qss(theme.texture_url())
    assert path.as_posix() in qss or theme.TEXTURE_NAME_2X in qss
    assert "background-repeat: repeat-x" in qss
    # 绝对路径：相对路径在打包后会被解析到别的地方（素材"莫名丢了"）
    assert re.search(r'url\("[^"]*[/\\]assets[/\\]ui[/\\]brushed-metal', qss)


def test_missing_texture_falls_back_to_plain_color(monkeypatch) -> None:
    """**素材缺失不许出错**：连 `url()` 都不出现，背景条退回纯色。"""
    monkeypatch.setattr(theme, "ui_asset", lambda name: None)
    assert theme.texture_url() is None
    qss = theme.silver_qss(theme.texture_url())
    assert "url(" not in qss
    assert "background-color: #e9ecef" in qss          # 背景条仍有底色
    assert theme.apply_theme(_FakeApp(), "silver") == "silver"


def test_ui_asset_returns_none_for_unknown_name() -> None:
    assert assets.ui_asset("没有这张图.png") is None
    assert assets.ui_asset("") is None


# ── 3) 深色皮肤的共性与对比度 ──
#
# 深色皮肤最容易翻车的两件事：① 深绿字配深蓝底看不清；② 主按钮换成了亮青底却还在用白字。
# 这两条都用对比度/取色值钉住（改配色时不能只看"好不好看"）。


def _contrast(fg: str, bg: str) -> float:
    """WCAG 对比度（1~21）。`fg`/`bg` 都是 `#rrggbb`。"""

    def _lum(color: str) -> float:
        raw = color.lstrip("#")
        parts = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_indigo_skin_text_is_readable_on_its_backgrounds() -> None:
    """默认皮肤（午夜靛紫）：正文/次要文字/涨跌色在这几块深底上都要够对比（AA 4.5:1）。"""
    colors = theme.INDIGO_COLORS
    for key in ("text", "text_dim", "text_disabled"):
        for bg_key in ("window", "panel", "section", "alt_row"):
            ratio = _contrast(colors[key], colors[bg_key])
            # text_disabled 是"故意压低"的文字，放它到 AA 的边缘（4:1）就够
            floor = 4.0 if key == "text_disabled" else 4.5
            assert ratio >= floor, (key, bg_key, round(ratio, 2))
    for key in ("up", "down"):
        for bg_key in ("window", "panel"):
            ratio = _contrast(theme.DARK_SEMANTIC[key], colors[bg_key])
            assert ratio >= 4.5, (key, bg_key, round(ratio, 2))
    # 主按钮上的字只在**它自己的底色**上被读：亮青底 + 深字（白字在青底上只有 1.8:1）
    for bg_key in ("primary_top", "primary_bottom", "primary_hover_top", "primary_press_top"):
        ratio = _contrast(colors["primary_text"], colors[bg_key])
        assert ratio >= 4.5, ("primary_text", bg_key, round(ratio, 2))
    # 主按钮上的字是**算出来的**（`_primary_gradient` 连同悬停/按压四态一起比，
    # 在"亮底深字"与"深底亮字"里挑更稳的那条），所以这里不钉具体色值，只钉对比度
    assert theme.contrast_ratio(colors["primary_text"], colors["primary_top"]) >= 4.5


def test_indigo_skin_uses_its_accent_and_not_the_metal_texture() -> None:
    """强调色进样式表；深色底**不贴**那张浅色拉丝纹理（贴上去就是一块脏斑）。"""
    qss = theme.theme_qss("indigo")
    assert theme.INDIGO_COLORS["tab_accent"] in qss
    assert theme.TEXTURE_NAME not in qss                   # 不引用拉丝纹理
    assert "qlineargradient" in qss                        # 背景条用渐变
    # 银色那套仍然照旧带纹理（两条路互不影响）
    silver = theme.theme_qss("silver", '"/tmp/brushed.png"')
    assert "/tmp/brushed.png" in silver


def test_semantic_colors_follow_the_theme() -> None:
    """涨跌色按主题走：浅色皮肤沿用全项目那两档，深色皮肤用亮一档的。

    判据仍然只有一处（`market.value_color`）—— 这里钉的是"换主题换一档色"，
    以及**平盘/取不到时仍然是空串**（那是"不上色"，不是某种颜色）。
    """
    from laoa_trader import market

    app = _FakeApp()
    theme.apply_theme(app, "silver")
    assert theme.semantic("up") == market.COLOR_UP == "#d32f2f"
    assert theme.semantic("down") == market.COLOR_DOWN == "#2e7d32"
    assert theme.value_color(1.0) == market.COLOR_UP
    assert theme.value_color(-1.0) == market.COLOR_DOWN

    theme.apply_theme(app, "indigo")
    assert theme.semantic("up") == theme.DARK_SEMANTIC["up"]
    assert theme.semantic("down") == theme.DARK_SEMANTIC["down"]
    assert theme.value_color(1.0) == theme.DARK_SEMANTIC["up"]
    assert theme.value_color(-1.0) == theme.DARK_SEMANTIC["down"]

    # 平盘 / 取不到：**不上色**（空串），两种主题一致
    for name in ("silver", "indigo"):
        theme.apply_theme(app, name)
        assert theme.value_color(0) == ""
        assert theme.value_color(None) == ""


# ── 4) 每一套色板（主人 2026-10-10 挑配色时留下的那批）──

def test_every_palette_defines_every_key() -> None:
    """**每套色板都必须有同一批键**：样式表模板是所有配色共用的那一张，
    少一个键 `Template.substitute` 就是 KeyError（换一套配色 = 整个界面打不开）。

    这条看着琐碎，但这几套色板是**生成**出来的（`_dark_palette` / `_light_palette`），
    以后再加皮肤也是照着改生成器 —— 一旦少一个键，那套皮肤一点就是"界面起不来"。
    """
    reference = set(theme.SILVER_COLORS)
    for name, colors in theme.PALETTES.items():
        missing = reference - set(colors)
        extra = set(colors) - reference
        assert missing == set(), f"{name} 少了这些键：{sorted(missing)}"
        assert extra == set(), f"{name} 多了这些键：{sorted(extra)}"
        for key, value in colors.items():
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), (name, key, value)


def test_every_palette_renders_a_qss_from_its_own_colors() -> None:
    """每套皮肤都渲染得出来，而且**用的确实是它自己的色**（不是悄悄退回默认那份）。"""
    for name, colors in theme.PALETTES.items():
        qss = theme.theme_qss(name)
        assert colors["window"] in qss
        assert colors["accent"] in qss
        assert colors["titlebar_top"] in qss
        if name != theme.THEME_SILVER:
            assert theme.TEXTURE_NAME not in qss      # 深色皮肤不贴浅色拉丝纹理


def test_palette_table_matches_the_selectable_themes() -> None:
    """候选配色**不进设置页下拉框**：`UI_THEMES` 仍是那三个（写错一个字母回默认）。

    2026-10-10 定稿后**六套都是正式皮肤**（主人要求"其它几套也可以放在程序包里供用户选择"），
    所以这条用例改成钉"色板表与可选表严格对应"：多一套没登记的色板（渲染得出来但选不到）
    或者少一套（选得到却渲染不出来）都会红。
    """
    assert set(theme.PALETTES) == set(config_mod.UI_THEMES) - {"system"}
    for name in theme.PALETTES:
        assert config_mod.Config(ui_theme=name).ui_theme == name
        assert theme.normalize_theme(name) == name
        assert theme.theme_label(name) != theme.THEME_LABELS[config_mod.DEFAULT_UI_THEME] \
            or name == config_mod.DEFAULT_UI_THEME


def test_every_palette_text_is_readable() -> None:
    """**每套配色**的正文/次要文字都要看得清（WCAG AA 4.5:1），主按钮上的字也一样。

    候选配色是"由三块底色 + 一个强调色"生成的（见 `theme._dark_palette`），
    生成的最大风险就是"某个底色上字糊了"。这条用例把判据钉死在**对比度**上：
    以后再加几套候选、或把某套并成正式皮肤，糊字的组合直接红。
    """
    for name, colors in theme.PALETTES.items():
        for bg_key in ("window", "panel", "section"):
            ratio = _contrast(colors["text"], colors[bg_key])
            assert ratio >= 4.5, (name, "text", bg_key, round(ratio, 2))
        for bg_key in ("window", "panel"):
            ratio = _contrast(colors["text_dim"], colors[bg_key])
            assert ratio >= 4.0, (name, "text_dim", bg_key, round(ratio, 2))
        ratio = _contrast(colors["titlebar_text"], colors["titlebar_top"])
        assert ratio >= 4.5, (name, "titlebar_text", round(ratio, 2))


def test_candidate_primary_buttons_are_readable_in_all_states() -> None:
    """候选配色的主按钮：**常态**两种底色都要 ≥4.5，悬停/按压 ≥3.4（粗体大字那档）。

    主按钮上的字色是算出来的（`_primary_gradient` 会在"亮底深字"与"深底亮字"
    两条路里挑更清楚的那条，并顺着字色做悬停/按压）。这条用例验的就是那个算法：
    强调色改一个值就可能滑到糊字的区间，得有人盯着。
    """
    for name, colors in theme.PALETTES.items():
        if name == theme.THEME_SILVER or name == theme.THEME_PAPER:
            continue                    # 浅色皮肤的主按钮（银/浅灰）另有专门断言
        text = colors["primary_text"]
        for key in ("primary_top", "primary_bottom"):
            ratio = _contrast(text, colors[key])
            assert ratio >= 4.5, (name, key, round(ratio, 2))
        for key in ("primary_hover_top", "primary_hover_bottom",
                    "primary_press_top", "primary_press_bottom"):
            ratio = _contrast(text, colors[key])
            assert ratio >= 3.4, (name, key, round(ratio, 2))


def test_dark_and_light_palette_lists_agree_with_the_colors() -> None:
    """`DARK_THEMES` 必须**恰好**是那几套深色皮肤：漏一套 → 热力图与自绘标题栏按
    浅色画（深底上贴浅色块），多一套 → 浅色皮肤被当深色处理，两种都是"看着就是坏的"。"""
    dark_names = {name for name, colors in theme.PALETTES.items()
                  if theme._luminance(colors["window"]) < 0.2}
    assert set(theme.DARK_THEMES) == dark_names
    assert "system" not in theme.PALETTES          # 系统默认那档没有色板（空样式表）


def test_dark_palettes_all_report_dark() -> None:
    """`is_dark()` 认的是 `DARK_THEMES` 那一批：候选配色换上去后热力图/自绘控件也要换色。

    `apply_palette(app=None, …)` 在没有 QApplication 的环境里只更新"当前主题"这个名字
    （`app` 为 None 就不去设样式表），所以这条用例不需要 Qt。
    """
    original = theme.current_theme()
    try:
        for name in theme.DARK_THEMES:
            theme.apply_palette(None, name)
            assert theme.current_theme() == name
            assert theme.is_dark() is True
            assert theme.semantic("up") == theme.DARK_SEMANTIC["up"]
        theme.apply_palette(None, "silver")
        assert theme.is_dark() is False
        assert theme.semantic("up") == market_color_up()
    finally:
        theme.apply_palette(None, original)


def market_color_up() -> str:
    """浅色皮肤下"涨"的色值（`market.COLOR_UP`，全项目核过的那一档）。"""
    from laoa_trader import market

    return market.COLOR_UP
