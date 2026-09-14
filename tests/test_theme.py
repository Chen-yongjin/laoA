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


# ── 1) 主题名归一（配置 / 环境变量）──


def test_theme_defaults_to_silver(cfg) -> None:
    """默认是银色主题（用户点名要的那套）；取值表只有 config 一份。"""
    assert config_mod.DEFAULT_UI_THEME == "silver"
    assert config_mod.UI_THEMES == ("silver", "system")
    assert config_mod.Config().ui_theme == "silver"
    assert cfg.ui_theme == "silver"
    # 界面层的取值表引用同一份定义，不会两处各写各的
    assert theme.THEME_SILVER in config_mod.UI_THEMES
    assert set(theme.THEMES) == set(config_mod.UI_THEMES)


@pytest.mark.parametrize("raw", ["system", " silver ", "SILVER", "System"])
def test_theme_accepts_valid_values(raw) -> None:
    assert config_mod.Config(ui_theme=raw).ui_theme == raw.strip().lower()
    assert theme.normalize_theme(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", None, "银色", "silver ", "neon", "SILVE", 0, "1"])
def test_theme_falls_back_to_default_on_bad_value(raw) -> None:
    """非法值一律回默认 —— 写错一个字母不该让界面起不来。"""
    assert config_mod.Config(ui_theme=raw).ui_theme == "silver"
    assert theme.normalize_theme(raw) == "silver"
    assert theme.theme_label(raw) == theme.THEME_LABELS["silver"]


def test_theme_from_config_file_and_env(tmp_path, monkeypatch) -> None:
    """config.toml 打底、环境变量覆盖；环境变量写错也回默认。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{tmp_path}"\nui_theme = "system"\n', encoding="utf-8"
    )
    monkeypatch.delenv("UI_THEME", raising=False)
    assert config_mod.load_config(path).ui_theme == "system"

    monkeypatch.setenv("UI_THEME", "silver")
    assert config_mod.load_config(path).ui_theme == "silver"     # 环境变量盖过文件

    monkeypatch.setenv("UI_THEME", "乱写的")
    assert config_mod.load_config(path).ui_theme == "silver"     # 非法 → 回默认

    monkeypatch.setenv("UI_THEME", "System")
    assert config_mod.load_config(path).ui_theme == "system"     # 大小写不敏感


def test_theme_labels_are_chinese_and_distinct() -> None:
    assert theme.theme_label("silver") == "银色（金属感）"
    assert theme.theme_label("system") == "系统默认"


def test_current_theme_tracks_applied(monkeypatch) -> None:
    """`current_theme()` 反映**实际应用**的主题（不是"配置里写了什么"）。"""
    assert theme.current_theme() == "silver"
    theme.apply_theme(_FakeApp(), "system")
    assert theme.current_theme() == "system"
    theme.apply_theme(_FakeApp(), "乱写的")
    assert theme.current_theme() == "silver"          # 非法值回默认，不留在"乱写的"


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
