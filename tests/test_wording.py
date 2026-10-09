"""来源列里那个名字**不带任何前缀**：就写策略名本身（主人 2026-09-23：「把公式名称中的策略两个字去掉，无意义」）。

（前情：2026-09-22 主人说过"把公式都改成策略吧"，那版把来源列显示成 `策略·X`；
2026-09-23 他要求连这个前缀也去掉，于是 `公式·X` / 老的 `策略·X` / 老类名
三条路都剥成同一个名字，如 `尾盘匹配策略`、`短期反转`。）

这一条口径有两半，**必须分清楚**，否则改起来一定出错：

* **给人看的字**（控件文字、tooltip、占位符、菜单项、提示区、推送正文、桌面文件）
  → 一律说「策略」，来源列显示成 `尾盘超短策略` / `短期反转`（**不带前缀**）；
* **数据与标识符**（库里存的 `公式·X`、`formulas/` 目录、`enabled_formulas` 配置键、
  `Formula` / `formula_group` / `formulas.py`、CLI 参数名）→ **一个字都不许改**。
  升级时也没有任何数据迁移：老行里的 `公式·X` 显示时被剥成 `X`
  （`wording.display_strategy()`），所以升级前后同一只票只有一种写法。

"不出现公式"的**判据**（见 `test_no_formula_word_in_user_visible_text`）：
把主窗口里所有控件的 `text()/toolTip()/placeholderText()/whatsThis()` 与所有 `QAction`
的 `text()/toolTip()` 扫一遍，**任何一条都不许含"公式"二字**。
允许的例外只有两类，都写在下面的 `_ALLOWED` 里：
① 用户自己起的名字（他写"我的公式一"就该原样显示 —— 用例里不造这种名字，
   但判据要说明白"这不是漏网"）；② 开发日志（logger 的输出，不在控件上）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面文案用例")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTableWidget,
    QWidget,
)

from laoa_trader import pool, wording  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.strategy import formula_group  # noqa: E402
from laoa_trader.ui import formula_page as fp  # noqa: E402

#: 界面上允许出现的"公式"（目前**一个都没有**；留着这份清单是为了把判据写明白）。
#: 判据：控件上的字只要含"公式"就红 —— 用户自己起的名里带这两个字当然要原样显示，
#: 但那种情况是"他的内容"，不是我们写的文案；本文件里的用例不造这种名字，
#: 所以这里可以是空的。
_ALLOWED: tuple[str, ...] = ()


# ══════════════════════════════════════════════════════════════════════════
# 1) 显示口径：剥掉前缀、也不写「+自选」（数据一个字没动）
# ══════════════════════════════════════════════════════════════════════════


def test_display_strategy_strips_the_prefix_and_nothing_else() -> None:
    """剥前缀，**不动名字本身** —— 用户起的名字里带"策略/公式"也不许被改。

    口径变过两次（09-22 加前缀 → 09-23 去前缀），所以**两种老写法都要能剥**：
    库里存的 `公式·X`、更早版本显示过并写进过备注的 `策略·X`。
    """
    assert wording.display_strategy("公式·放量上攻") == "放量上攻"
    assert wording.display_strategy("策略·短期反转") == "短期反转"          # 老显示写法
    assert wording.display_strategy("尾盘匹配策略") == "尾盘匹配策略"       # 本来就干净
    assert wording.display_strategy("我的策略一") == "我的策略一"           # 用户的名字
    assert wording.display_strategy("公式·我的公式一") == "我的公式一"
    assert wording.display_strategy("公式·我的策略一") == "我的策略一"
    assert wording.display_strategy("") == ""


def test_stored_rows_display_without_any_prefix_on_all_three_surfaces() -> None:
    """库里存 `公式·X`、老行存类名 —— 显示端**三条路都不带前缀**，且三处同一个词。

    为什么三处都要钉：来源列、桌面导出文件、推送正文各自都能"自己拼一次"，
    上一轮漏的正是这种"一处改了、另一处没改"。
    """
    row = {"strategy": "公式·尾盘超短策略", "strategies": "公式·尾盘超短策略",
           "name": "甲样本", "symbol": "600001"}

    assert row["strategy"] == "公式·尾盘超短策略"            # 数据：一个字没改
    assert pool.source_label(row, None) == "尾盘超短策略"
    # ⚠️ 2026-09-23 起**不再拼「+自选」**：它在不在自选里不影响「来源」列
    assert pool.source_label(row, {"enabled": 1}) == "尾盘超短策略"
    assert pool.source_detail_lines(
        {**row, "source_label": "公式·尾盘超短策略"}
    )[0] == "来源：尾盘超短策略"
    # 09-22 那版写进备注/来源列的老写法，也要剥掉
    assert pool.source_label({"strategy": "策略·尾盘超短策略"}, None) == "尾盘超短策略"
    # 老内置策略的类名 → 中文名，同样不带前缀；在自选里就是 `名字+自选`
    assert pool.source_label({"strategy": "ReversalStrategy"}, None) == "短期反转"
    assert pool.source_label({"strategy": "ReversalStrategy"}, {"enabled": 1}) == "短期反转"
    # 纯手工自选仍是「自选」
    assert pool.source_label({"watchlist": True}, None) == "自选"
    # 用户自己起的名字里带"策略"两个字：原样保留
    assert pool.source_label({"strategy": "我的策略一"}, None) == "我的策略一"
    assert pool.source_label({"strategy": "我的策略一"}, {"enabled": 1}) == "我的策略一"

    # 三处同一个词：来源列 / 桌面导出文件 / 推送正文
    text = pool.pick_export_text([row], data_date="2026-09-11", day="2026-09-18", quotes={})
    assert "来源：尾盘超短策略" in text
    assert pool.push_tag(row) == "尾盘超短策略"

    # 内部判定仍然认「公式·」前缀（数据口径没变）
    assert formula_group.is_formula_strategy(row["strategy"]) is True
    assert formula_group.formula_name_of(row["strategy"]) == "尾盘超短策略"


def test_source_label_never_says_watchlist_for_a_strategy_pick() -> None:
    """**来源列只说"是哪条策略选出来的"**：它在不在自选里都不写「自选」。

    2026-09-23 主人原话："为什么要+自选 什么策略跑出来的 直接记录策略名称
    只有用户自己输入的才能算自选来源"。
    """
    picked = {"strategy": "公式·尾盘匹配策略", "strategies": "公式·尾盘匹配策略"}
    manual = {"symbol": "600002", "watchlist": True}

    # 选出来的票：在自选里 / 不在自选里，来源列**同一个词**
    assert pool.source_label(picked, None) == "尾盘匹配策略"
    assert pool.source_label(picked, {"enabled": 1}) == "尾盘匹配策略"
    assert pool.source_label(picked, {"enabled": 0}) == "尾盘匹配策略"
    # 只有用户自己加的才算「自选」
    assert pool.source_label(manual, {"enabled": 1}) == "自选"
    assert pool.source_label(manual, None) == "自选"
    # `source_kind()` 里的"组合档"随之消失（只剩 策略 / 公式 / 自选 三档）
    assert pool.source_kind(picked, {"enabled": 1}) == "公式"
    assert pool.source_kind(manual, {"enabled": 1}) == "自选"


def test_legacy_rows_with_the_old_watchlist_suffix_are_displayed_without_it() -> None:
    """老数据里存过 `X+自选`：显示时**剥掉尾巴**（否则同一只票两种写法）。"""
    row = {"strategy": "公式·尾盘超短策略", "source_label": "公式·尾盘超短策略+自选"}

    assert pool.source_detail_lines(row)[0] == "来源：尾盘超短策略"
    # 桌面导出文件同一条路（`_export_source` 也走 `display_source_label`）
    text = pool.pick_export_text([{"symbol": "600001", "name": "甲样本",
                                   "source_label": "短期反转+自选"}],
                                 data_date="2026-09-11", day="2026-09-18", quotes={})
    assert "来源：短期反转" in text


# ══════════════════════════════════════════════════════════════════════════
# 2) 扫一遍界面：控件上的字一个"公式"都不许有
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def page(cfg, qapp, tmp_path):
    """「策略筛选」页（公式目录指到 tmp_path，绝不碰仓库里那份）。"""
    storage.init_db(cfg.db_path)
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("# 用户自己的注释\n", encoding="utf-8")
    widget = fp.FormulaPage(cfg, directory=tmp_path / "formulas")
    widget.resize(1100, 720)
    widget.show()
    qapp.processEvents()
    yield widget
    # 收尾把线程等干净（与 test_formula_page.py 同一段注释：留着会拖慢后面所有用例）
    for attr in ("scorecard_worker", "preview_worker"):
        worker = getattr(widget, attr, None)
        if worker is not None and worker.isRunning():
            worker.wait(5_000)
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


@pytest.fixture()
def window(cfg, qapp, tmp_path):
    """一个**真实的主窗口**（建法与 `test_ui_smoke.py` 一致，收尾也照它收干净）。

    `cfg.source_path` 必须指到临时目录：主窗口起来后会真的读写配置，
    不指就会动到开发机上那份真实的 config.toml。
    """
    from laoa_trader.ui import app as ui_app

    storage.init_db(cfg.db_path)
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("# 用户自己的注释\n", encoding="utf-8")
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()
    yield win
    win.shutdown()
    win.close()
    win.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _widget_texts(root: QWidget) -> list[tuple[str, str]]:
    """窗口里所有**给人看**的字符串 → `[(从哪来的, 文本)]`。

    为什么连 `placeholderText()` 也要扫：输入框里那行灰字（"在这里写策略，例如…"）
    同样是用户会读的文案，漏掉它就等于漏掉一整类；
    `QAction` 单独扫是因为右键菜单项**不是** QWidget（`findChildren(QWidget)` 找不到）。
    """
    out: list[tuple[str, str]] = []
    for widget in [root, *root.findChildren(QWidget)]:
        name = type(widget).__name__
        for getter in ("text", "toolTip", "placeholderText", "whatsThis", "statusTip"):
            fn = getattr(widget, getter, None)
            if fn is None:
                continue
            try:
                value = fn()
            except Exception:  # noqa: BLE001 - 控件类型不支持这个取值方式
                continue
            if isinstance(value, str) and value:
                out.append((f"{name}.{getter}", value))
    for action in root.findChildren(QAction):
        for getter in ("text", "toolTip"):
            value = getattr(action, getter, lambda: "")()
            if value:
                out.append((f"QAction.{getter}", value))
    return out


def _assert_no_formula_word(pairs: list[tuple[str, str]]) -> None:
    bad = [(where, text) for where, text in pairs
           if wording.OLD_WORD in text and text not in _ALLOWED]
    assert not bad, "界面上还在说「公式」：\n" + "\n".join(
        f"  {where}: {text[:90]}" for where, text in bad[:20]
    )


def test_no_formula_word_in_user_visible_text(window, qapp) -> None:
    """主窗口（含「策略筛选」页与编辑器）里，**控件上任何一处都不许出现"公式"**。"""
    pairs = _widget_texts(window)
    assert pairs, "一个控件都没扫到，这条用例就没意义了"
    # canary：确认编辑器那一页真的被扫到了（它平时是隐藏的，但控件一直存在）
    blob = "\n".join(text for _, text in pairs)
    assert "策略名称" in blob and "策略筛选" in blob, "没扫到「策略筛选」页，判据不成立"
    _assert_no_formula_word(pairs)


def test_editor_page_texts_have_no_formula_word(page) -> None:
    """「策略编辑」展开之后再看一遍：那一块的文案最多（按钮、tooltip、占位符）。"""
    page.btn_edit.click()                      # 右侧面板 + 下半编辑器都展开
    QApplication.processEvents()

    pairs = _widget_texts(page)
    blob = "\n".join(text for _, text in pairs)
    assert "策略名称" in blob and "策略编辑器" in blob
    _assert_no_formula_word(pairs)


def test_editor_hints_and_module_level_texts_have_no_formula_word() -> None:
    """模块级的常量文案（提示语、按钮字、表头 tooltip）也一起扫。

    为什么单独扫：这些字符串**只有画出来才会进控件**，而有些（如 `EDITOR_HINT`）
    在特定状态下才显示 —— 用例不该赌"今天恰好显示着"。
    """
    texts: list[tuple[str, str]] = []
    for name in dir(fp):
        if not name.isupper():
            continue
        value = getattr(fp, name)
        if isinstance(value, str):
            texts.append((f"formula_page.{name}", value))
        elif isinstance(value, (tuple, list)):
            texts.extend((f"formula_page.{name}", item)
                         for item in value if isinstance(item, str))
    assert len(texts) > 30, f"扫到的常量太少（{len(texts)}），判据可能失效"
    _assert_no_formula_word(texts)

    # 表头（「来源」列等）与来源列文本也在内：表头是 `RESULT_COLUMNS`，上面已覆盖；
    # 这里再钉一条**真正渲染出来的**文本（表格列的写法只在这里能看出来）
    for column in range(len(fp.RESULT_COLUMNS)):
        assert wording.OLD_WORD not in fp.RESULT_COLUMNS[column]
    for tip in fp.RESULT_HEADER_TIPS:
        assert wording.OLD_WORD not in tip
    for token, tip, _caret, label in fp.FUNCTIONS + fp.VARIABLES + fp.OPERATORS:
        assert wording.OLD_WORD not in label, label
        assert wording.OLD_WORD not in tip, tip
