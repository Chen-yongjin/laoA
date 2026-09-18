"""「策略选股」页（`ui/formula_page.py`）的**离屏**界面测试。

这一页现在是三块（`docs/改版方案.md` TAB 4）：统一策略列表（内置 + 公式同一张表）、
按需展开的傻瓜式公式编辑器、以及【开始选股】。最容易做错、又最难在代码里看出来的
是**交互细节**，所以这里逐条钉住：

* 点按钮是**插到光标处**还是追加到末尾（追加也能"跑通"，但会把用户改到一半的公式
  静默改坏 —— 见 `FormulaPage.insert_token` 的注释）；
* `AND` 前后有没有空格（没有就变成 `A>1AND B` 这种"看不懂的语法错"）；
* 列表的两类行：内置策略的备注只能是**真实字段**（证据），公式的备注来自文件的注释头；
* 「状态」列写回**哪个键**：公式写 `enabled_formulas`，内置策略**同时**写
  `enabled_groups` + `enabled_strategies`（只写一个会踩交集语义的坑，见页面里的注释）；
* 右键菜单：内置可启停但**不可删**（置灰 + 理由），公式可删（二次确认）；
* 备注能写进公式文件的 `# 说明:` 注释头、也能从那里读回界面；
* 【开始选股】**只 emit `start_pick_requested`**，自己绝不跑流程；
* **选股结果不在这一页**（用户要求"只要显示策略"）：结果进「自选股池」+ 导出一份到桌面，
  这一页只留一句"结果去哪了"的说明；主窗口仍在调的那个 `show_pick_result()` 是空实现
  （留着以免 `ui/app.py` 那边 `AttributeError`）；
* 【试算】**在后台线程里跑**（用户实报过"窗口未响应"）。

这些只有把页面真的建出来、真的点一遍才测得到。用 Qt 的 `offscreen` 平台插件，
无显示器也能跑；没装 PySide6 的机器整个文件跳过（与 `test_ui_smoke.py` 同一约定）。
"""

from __future__ import annotations

import inspect
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过公式编辑器界面测试")

from PySide6.QtCore import Qt, QThread  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGroupBox,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
)

from laoa_trader import formulas as lib  # noqa: E402
from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.strategy import formula as fm  # noqa: E402
from laoa_trader.strategy import groups, rules  # noqa: E402
from laoa_trader.ui import formula_page as fp  # noqa: E402
from tests.conftest import workdays_ending  # noqa: E402

#: 小库里的三只票：甲/丙 一路上涨、乙 一路下跌（命中集合是确定的）
RISING = ("600001", "600003")

#: 表格里**固定占掉的行数**：5 条内置策略 + 1 行「竞价策略」（2026-09-18 内置的
#: 竞价扫描开关）。公式行从这一行之后开始 —— 用例里都写 `FIXED_ROWS` 而不是 5，
#: 免得下次再加一行固定行时又要满文件改数字。
FIXED_ROWS = 6


def _seed_trend_db(db_path: Path) -> None:
    """3 只票 × 30 个交易日：甲/丙 每天 +1%、乙 每天 −1%。

    不用 `tests/conftest.py` 里那份"给 5 条内置策略准备"的库：这一页要断言的是
    "试算命中哪几只"，需要**一眼能看懂的走势**。
    """
    storage.init_db(db_path)
    days = workdays_ending("2026-09-11", 30)
    plan = {
        "600001": ("甲样本", 10.0, 0.01),
        "600002": ("乙样本", 20.0, -0.01),
        "600003": ("丙样本", 5.0, 0.01),
    }
    with storage.connect(db_path) as conn:
        storage.write_stock_basic(conn, [(s, meta[0], "银行") for s, meta in plan.items()])
        rows = []
        for symbol, (_name, base, drift) in plan.items():
            price = base
            for day in days:
                price = price * (1 + drift)
                rows.append((symbol, day, price * 0.99, price * 1.01, price * 0.98,
                             price, 1_000_000.0, price * 1_000_000.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def page_cfg(cfg: Config, tmp_path: Path) -> Config:
    """界面用的配置：数据目录里有一份小库，config.toml 落在 tmp_path。"""
    _seed_trend_db(cfg.db_path)
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text(
        "# 用户自己的注释（保存设置后必须还在）\n"
        'enabled_groups = ["short"]\n'
        'my_own_key = "别动我"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture()
def page(page_cfg: Config, tmp_path: Path, qapp):
    """建出这一页（公式目录指向 tmp_path，**绝不碰仓库里那份**）。"""
    widget = fp.FormulaPage(page_cfg, directory=tmp_path / "formulas")
    widget.resize(1100, 720)
    widget.show()
    qapp.processEvents()
    yield widget
    # ── 收尾必须把线程**等干净** ──
    # 留一个还在跑的 QThread 给下一个用例：它的信号会打到已经删掉的控件上，
    # 而且事件循环里一直有活儿 —— 整个文件的耗时会成倍劣化（界面用例踩过这个坑）。
    for attr in ("scorecard_worker", "preview_worker"):
        worker = getattr(widget, attr, None)          # 成绩单线程已随界面一起移除
        if worker is not None and worker.isRunning():
            worker.wait(5_000)
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _put_caret(widget, position: int) -> None:
    """把光标放到指定位置（模拟用户用鼠标/方向键点到了公式中间）。"""
    cursor = widget.editor.textCursor()
    cursor.setPosition(position)
    widget.editor.setTextCursor(cursor)


def _click(widget, token: str) -> None:
    """点右侧面板上某个按钮（按 token 取，不爬布局层级）。"""
    widget.palette_buttons[token].click()


def _open_editor(page) -> None:
    """展开公式编辑器（改版后它默认收起，点【策略编辑】才出现）。

    为什么测试也要走这一步而不是直接把控件 `show()`：焦点、Tab 键、插入位置
    这三样都要求控件**真的可见**（Qt 的 `hasFocus()` 对隐藏控件恒为 False）。
    手动 show 会让这些用例在一个界面上根本不存在的状态里通过。
    """
    page.btn_edit.click()
    assert page.bottom_stack.isVisible() is True
    assert page.bottom_stack.currentWidget() is page.editor_page


def _spin_until(qapp, predicate, timeout: float = 5.0) -> bool:
    """转事件循环直到条件成立（后台线程的信号要靠它回主线程）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


#: 试算线程最多等多久（超时**大声失败**，而不是"睡一觉再赌它跑完了"）
PREVIEW_TIMEOUT = 15.0


def _wait_preview(page, qapp, timeout: float = PREVIEW_TIMEOUT) -> None:
    """等【试算】的后台线程落地（`preview_worker` 被置回 None 就是"跑完了"）。

    为什么要等而不用 `time.sleep`：线程什么时候结束取决于机器快慢，固定 sleep 要么
    白等、要么偶发变红。这里转事件循环（信号得靠它回主线程）+ 超时断言，
    失败时把当时的提示文本一起打出来，一眼能看出卡在哪一步。
    """
    assert _spin_until(qapp, lambda: page.preview_worker is None, timeout), (
        f"试算线程 {timeout:.0f} 秒还没落地：hint={page.hint_text!r}"
    )


# ══════════════════════════════════════════════════════════════════════════
# 1) 点一下就插入（插在**光标处**，不是末尾）
# ══════════════════════════════════════════════════════════════════════════


def test_click_inserts_at_caret_not_at_end(page) -> None:
    """**这一页最核心的一条**：点变量 = 插到光标处，不是追加到末尾。"""
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")
    _put_caret(page, 3)          # 光标落在 `C>M|A(C,5)`
    page.editor.setFocus()

    _click(page, "V")

    assert page.editor.toPlainText() == "C>MVA(C,5)"     # 插在光标处
    assert page.editor.textCursor().position() == 4      # 光标停在刚插入的字符之后
    assert page.editor.hasFocus() is True                # 焦点回到编辑框，接着就能打字


def test_click_appends_when_caret_is_at_end(page) -> None:
    """光标在末尾时自然是追加（两种位置都要对，别只满足其中一种）。"""
    page.editor.setPlainText("C>MA(C,5)")
    cursor = page.editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    page.editor.setTextCursor(cursor)

    _click(page, "O")

    assert page.editor.toPlainText() == "C>MA(C,5)O"
    assert page.editor.textCursor().position() == len("C>MA(C,5)O")


def test_function_button_inserts_skeleton_with_caret_inside(page) -> None:
    """函数按钮：插入 `MA()` 并把光标放在括号**里面**（接着打字就是参数）。"""
    page.editor.clear()

    _click(page, "MA")

    assert page.editor.toPlainText() == "MA()"
    assert page.editor.textCursor().position() == 3      # 在 `(` 与 `)` 中间
    page.editor.insertPlainText("C")                     # 直接接着打参数
    assert page.editor.toPlainText() == "MA(C)"
    assert page.editor.toPlainText().count("(") == page.editor.toPlainText().count(")")


def test_zero_arg_function_puts_caret_after_parentheses(page) -> None:
    """无参函数（连板 / 涨停天数 / 量比）：光标放在 `)` 之后，不用用户再按方向键。"""
    page.editor.clear()

    _click(page, "连板")

    assert page.editor.toPlainText() == "连板()"
    assert page.editor.textCursor().position() == len("连板()")
    page.editor.insertPlainText(">=2")
    assert page.editor.toPlainText() == "连板()>=2"


def test_if_skeleton_has_three_args_room(page) -> None:
    """三参函数同样是"括号里等参数"，不用为每个函数写一套逻辑。"""
    page.editor.clear()

    _click(page, "IF")

    assert page.editor.toPlainText() == "IF()"
    assert page.editor.textCursor().position() == 3


def test_operator_click_inserts_at_caret(page) -> None:
    page.editor.setPlainText("C>MA")
    _put_caret(page, 1)

    _click(page, ">=")

    assert page.editor.toPlainText() == "C>=>MA"      # 插在光标处（`C` 与 `>MA` 之间）


# ══════════════════════════════════════════════════════════════════════════
# 2) AND / OR / NOT 自动补空格
# ══════════════════════════════════════════════════════════════════════════


def test_and_operator_adds_spaces_around_it(page) -> None:
    """`A>1` 后面点 AND → `A>1 AND `，**不能**粘成 `A>1AND`。"""
    page.editor.setPlainText("A>1")
    cursor = page.editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    page.editor.setTextCursor(cursor)

    _click(page, "AND")

    assert page.editor.toPlainText() == "A>1 AND "
    _click(page, "V")                                    # 紧接着插变量
    assert page.editor.toPlainText() == "A>1 AND V"      # 依然有空格


def test_logic_operator_in_the_middle_of_formula(page) -> None:
    """光标在公式中间时也要补空格（前后都看）。"""
    page.editor.setPlainText("A>1B")
    _put_caret(page, 3)

    _click(page, "AND")

    assert page.editor.toPlainText() == "A>1 AND B"


def test_logic_operator_does_not_double_existing_space(page) -> None:
    """已经有一个空格时不再加第二个（`A>1  AND` 也很难看）。"""
    page.editor.setPlainText("A>1 B")
    _put_caret(page, 4)

    _click(page, "OR")

    assert page.editor.toPlainText() == "A>1 OR B"


def test_not_operator_adds_trailing_space(page) -> None:
    page.editor.clear()

    _click(page, "NOT")

    assert page.editor.toPlainText() == "NOT "


def test_short_operators_are_not_padded(page) -> None:
    """`+`/`>` 这类短符号不加空格（`V * 1.5` 那种写法反而更难读）。"""
    page.editor.clear()

    _click(page, "*")

    assert page.editor.toPlainText() == "*"


# ══════════════════════════════════════════════════════════════════════════
# 3) 编辑框本身：等宽字体 / Tab 插空格
# ══════════════════════════════════════════════════════════════════════════


def test_editor_uses_monospace_font(page) -> None:
    """等宽字体：括号能不能配对，全靠字符对齐看得出来。"""
    font = page.editor.font()
    assert font.fixedPitch() is True
    assert font.styleHint() == QFont.StyleHint.Monospace


def test_tab_inserts_spaces_and_keeps_focus(page) -> None:
    """`Tab` 插 4 个空格、**不跳焦点**（跳了的话用户正在打的公式会跑到名称框）。"""
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")
    _put_caret(page, 0)
    page.editor.setFocus()

    QTest.keyClick(page.editor, Qt.Key.Key_Tab)

    assert page.editor.toPlainText() == fp.TAB_SPACES + "C>MA(C,5)"
    assert page.editor.tabChangesFocus() is False
    assert page.editor.hasFocus() is True


# ══════════════════════════════════════════════════════════════════════════
# 4) 右侧面板：中文 tooltip / 一行 4 列 / 可滚动 / 固定宽度
# ══════════════════════════════════════════════════════════════════════════


def _font_size(widget) -> int:
    """控件的字号（按点算；字体是按像素定义的时候取像素 —— 那时 pointSize() 是 -1）。"""
    font = widget.font()
    return font.pointSize() if font.pointSize() > 0 else font.pixelSize()


def test_palette_covers_variables_functions_operators(page) -> None:
    tokens = set(page.palette_buttons)
    assert {"C", "O", "H", "L", "V", "AMO", "PRE", "INDUSTRY"} <= tokens
    assert {"MA", "EMA", "REF", "HHV", "LLV", "SUM", "COUNT", "CROSS", "ABS",
            "MAX", "MIN", "IF", "STD", "BARSLAST", "涨停天数", "连板", "量比"} <= tokens
    assert {"+", "-", "*", "/", ">", "<", ">=", "<=", "=", "!=",
            "AND", "OR", "NOT", "(", ")"} <= tokens


def test_every_palette_button_has_chinese_tooltip(page) -> None:
    """每个按钮都要有中文 tooltip（小白靠它知道"这个按钮是什么"）。"""
    for token, button in page.palette_buttons.items():
        tip = button.toolTip()
        assert tip, f"{token} 没有 tooltip"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in tip), f"{token} 的 tooltip 不是中文"
    # "是什么 + 一个例子"：这两条要求点名了例子，抽查几个
    assert "例：REF(C,1)" in page.palette_buttons["REF"].toolTip()
    assert "例：MA(C,5)" in page.palette_buttons["MA"].toolTip()
    assert "自动补空格" in page.palette_buttons["AND"].toolTip()


def test_palette_is_four_columns_scrollable_and_fixed_width(page) -> None:
    boxes = [box.title() for box in page.palette_panel.findChildren(QGroupBox)]
    # 四组、且**顺序固定**：用户 2026-09-18 要求"把排除区放在最下面"
    assert boxes == ["变量", "函数", "运算符", "排除"]
    for box in page.palette_panel.findChildren(QGroupBox):
        # 一行 4 个（用户第二次要求："改成一行 4 列"）—— 52 个按钮两列要滚很久
        assert box.layout().columnCount() == fp.PALETTE_COLUMNS == 4, \
            f"{box.title()} 组不是一行 {fp.PALETTE_COLUMNS} 个"
    # 函数多 → 必须能滚动；宽度固定 → 按钮文字不会被压扁
    assert page.palette_panel.findChild(QScrollArea) is not None
    assert page.palette_panel.width() == fp.PANEL_WIDTH


def test_palette_buttons_are_small_and_labelled_in_chinese(page) -> None:
    """按钮：**小一圈** + 字是**中文**（用户 2026-09-18 要求）。

    原话是"把按键大小都缩小，按键字符都翻译成中文，可以快速上手"：
    原来的按钮上写的是 `C` / `MA` / `AND` / `ST`，刚上手的人看不懂；
    现在写「收盘价」「均线」「并且」，点下去插进编辑框的仍是引擎认的语法。
    两件事都必须同时成立 —— **中文按钮 + 语法看不出来** = 用户永远学不会写公式，
    所以下面同时钉住"按钮是中文"与"tooltip 里写着插入的是什么语法"。
    """
    for token, button in page.palette_buttons.items():
        label = button.text()
        assert any("\u4e00" <= ch <= "\u9fff" for ch in label), (
            f"{token} 的按钮文字还是英文/符号（{label!r}）"
        )
        # 语法没丢：tooltip 第一步就写着"插入 XX"
        assert token in button.toolTip(), f"{token} 的 tooltip 里找不到插入的语法"
        # 尺寸：比默认小一圈（高度受主题 QSS 影响，所以同时钉住"最高"和"实际"两个值）。
        # 为什么必须看实际高度：主题里 `QPushButton { padding: 4px 12px; min-height: 20px }`
        # 一度把 setFixedHeight(22) 顶回 30px（实测）—— 只看 maximumHeight() 会漏掉这个 bug。
        assert button.maximumHeight() == fp.BUTTON_HEIGHT
        assert button.height() <= fp.BUTTON_HEIGHT, f"{token} 还是默认高度（QSS 没让路？）"
        assert _font_size(button) < _font_size(page)
    # 抽查几个最容易"翻了但翻错"的
    assert page.palette_buttons["C"].text() == "收盘价"
    assert page.palette_buttons["V"].text() == "成交量"
    assert page.palette_buttons["MA"].text() == "均线"
    assert page.palette_buttons["CROSS"].text() == "上穿"
    assert page.palette_buttons["AND"].text() == "并且"
    assert page.palette_buttons[">="].text() == "大于等于"


def test_palette_buttons_cannot_be_squashed_by_the_theme(page_cfg, tmp_path, qapp) -> None:
    """**在真实主题下**量一遍按钮的渲染尺寸与文字（用户实报过"全变长条、字看不见"）。

    这次事故的成因值得记下来：`theme.py` 里给这批按钮加的那条
    `QPushButton#paletteButton` 一旦命中，通用 `QPushButton` 里的 `min-height: 20px`
    就不再生效；按钮的最小高度变成 0 之后，外层 `QScrollArea` 会把 52 个按钮
    **压扁塞进视口** —— 实测每个只剩 4 像素高（字看不见、也点不中）。

    为什么原来那些断言拦不住：它们量的是 `maximumHeight()`（当时是**正确**的 22）
    与 `button.height() <= 22`（4 <= 22 也成立）。所以这一条只认两样东西：
    **布局之后真实渲染出来的高度**，以及**文字确实放得下**。
    """
    from laoa_trader.ui import theme

    before = qapp.styleSheet()
    try:
        theme.apply_theme(qapp)                       # 与主窗口启动时同一条路径
        fresh = fp.FormulaPage(page_cfg, directory=tmp_path / "公式-主题")
        fresh.resize(1280, 800)
        fresh.show()
        fresh.btn_edit.click()                        # 右侧面板要展开才谈得上"看得见"
        qapp.processEvents()

        heights = {box.height() for box in fresh.palette_buttons.values()}
        assert heights == {fp.BUTTON_HEIGHT}, f"按钮被压扁/撑高：{heights}"
        area = fresh.palette_panel.findChild(QScrollArea)
        assert area is not None and area.verticalScrollBar().maximum() > 0, \
            "按钮被塞进视口（没有滚动条）= 已经被压扁了"
        for token, box in fresh.palette_buttons.items():
            assert box.isVisibleTo(fresh) and box.isEnabled(), f"{token} 点不到（不可见/禁用）"
            need = QFontMetrics(box.font()).horizontalAdvance(box.text())
            assert box.width() - need > 4, f"{token} 的文字放不下（会被截断）"
        fresh.close()
    finally:
        qapp.setStyleSheet(before)                    # 别把主题留给后面的用例


def test_chinese_button_still_inserts_the_engine_syntax(page) -> None:
    """点「成交量」插进去的是 `V`（不是"成交量"三个字）—— 这是最容易翻错的一处。

    每条给三样：(按钮上的字, 点完编辑框里应该是什么, 用这个语法补成的完整公式能编译)。
    函数那条点完是 `MA()` 骨架（光标在括号里），所以断言的是骨架形状。
    """
    _open_editor(page)
    cases = (
        ("V", "成交量", "V", "X:=V\nC>MA(C,5)"),
        ("PRE", "昨收价", "PRE", "C>PRE*1.05"),
        ("MA", "均线", "MA()", "C>MA(C,5)"),          # 骨架：括号是空的，光标在里面
        ("AND", "并且", "AND ", "C>MA(C,5) AND V>MA(V,5)"),
        ("ST=0", "非 ST", "ST=0", "C>MA(C,5) AND ST=0"),
    )
    for token, label, expect_text, full in cases:
        page.editor.setPlainText("")
        _click(page, token)
        assert page.palette_buttons[token].text() == label
        assert page.editor.toPlainText() == expect_text, f"点【{label}】插入的不对"
        assert label not in page.editor.toPlainText(), "把中文插进公式了"
        # 插进去的语法是引擎认的：补成一个完整公式，必须能编译
        fm.compile_formula(full)


def test_exclude_palette_buttons_say_non_and_insert_flag_zero(page) -> None:
    """「排除」那一组：按钮写的字是**非 X**，插进去的是 `X=0`。

    用户 2026-09-18 的原话是"这几个按钮意思不明" —— 原来的按钮文字就是字段名
    （`ST` / `北交所` / `科创`），看起来像"筛选出这些"，而它其实是**排除**。
    所以按钮文字与插入文本**必须不同**：写字面意思，插引擎认的表达式。
    """
    _open_editor(page)
    expected = {
        "ST=0": "非 ST",
        "北交所=0": "非北交所",
        "科创=0": "非科创板",
        "沪市=0": "非沪市",
        "深市=0": "非深市",
        "创业板=0": "非创业板",
    }
    for token, label in expected.items():
        assert token in page.palette_buttons, f"「排除」组缺少 {token} 这个按钮"
        assert page.palette_buttons[token].text() == label

    for token, label in expected.items():
        page.editor.setPlainText("")
        _click(page, token)          # 插到光标处（空文本 = 插在开头）
        assert page.editor.toPlainText() == token, f"点【{label}】插入的不是 {token}"
        # 插进去的必须是**引擎认的**东西：编译一次，能过才算数
        fm.compile_formula(f"M5:=MA(C,5)\nC>M5 AND {token}")


def test_exclude_buttons_are_flags_the_engine_knows(page) -> None:
    """这六个字段都在引擎的 `FLAG_FIELDS` 里（否则按钮插进去就是"未知字段"）。"""
    assert set(fm.FLAG_FIELDS) == {"ST", "科创", "北交所", "沪市", "深市", "创业板"}
    for token in ("ST=0", "北交所=0", "科创=0", "沪市=0", "深市=0", "创业板=0"):
        field = token.split("=")[0]
        assert field in fm.FLAG_FIELDS


def test_page_hint_is_two_lines_and_gray(page) -> None:
    """顶部那行灰字说明要在（且是"点一下就知道下一步"这种一句话级别）。"""
    assert "状态" in fp.PAGE_HINT and "开始选股" in fp.PAGE_HINT
    assert len(fp.PAGE_HINT.splitlines()) <= 2
    assert page.page_hint.objectName() == "statusTag"     # 小号灰字（主题里定义）
    # 编辑器自己那行说明（旧版顶部那句话，现在跟着编辑器一起展开）**一个字都没丢**
    assert "点右边的按钮就能插入" in fp.EDITOR_HINT
    assert "最后一行是选股条件" in fp.EDITOR_HINT
    assert page.editor_hint.text() == fp.EDITOR_HINT


# ══════════════════════════════════════════════════════════════════════════
# 5) 校验
# ══════════════════════════════════════════════════════════════════════════


def test_validate_shows_fields_functions_and_min_history(page) -> None:
    page.editor.setPlainText("M5:=MA(C,5)\nV5:=MA(V,5)\nC>M5 AND V>V5*1.5")

    page.btn_validate.click()

    hint = page.hint_text
    assert "校验通过" in hint
    assert "字段" in hint and "C" in hint and "VOL" in hint
    assert "函数" in hint and "MA" in hint
    assert "至少需要 5 根 K 线" in hint
    assert page.hint_label.text() == hint or hint.startswith(page.hint_label.text()[:20])


def test_validate_shows_chinese_line_and_column(page) -> None:
    """报错是**引擎给的中文原文**：带行号列号 + 近似建议，界面不翻译。"""
    page.editor.setPlainText("C>MAA(C,5)")

    page.btn_validate.click()

    assert "第 1 行第 3 列" in page.hint_text
    assert "未知函数" in page.hint_text and "MAA" in page.hint_text
    assert "是不是想写" in page.hint_text


def test_validate_empty_formula_tells_what_to_do(page) -> None:
    page.editor.clear()

    page.btn_validate.click()

    assert "公式还是空的" in page.hint_text
    assert "点右边的按钮" in page.hint_text


def test_validate_warns_about_limit_up_history(page) -> None:
    """用到 `连板()` → 提示本地涨停池"早期日期读到 0"这个坑。"""
    page.editor.setPlainText("连板()>=2")

    page.btn_validate.click()

    assert "涨停池" in page.hint_text
    assert "早期日期" in page.hint_text


def test_validate_does_not_warn_for_plain_formula(page) -> None:
    """没用 `连板()`/`涨停天数()` 的公式**不能**被贴这条提醒（否则提醒就不值钱了）。"""
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_validate.click()

    assert "校验通过" in page.hint_text
    assert "涨停池" not in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 6) 试算：**后台线程**跑，文案与改造前逐字一致
# ══════════════════════════════════════════════════════════════════════════


def test_preview_returns_immediately_and_shows_progress(page, qapp) -> None:
    """【试算】点下去**马上返回**：按钮先灰掉、进度条先转起来、结果稍后才出现。

    这就是"不卡界面"的证据。改造前是在按钮回调里同步扫全库（真实 3 年库实测 3.7 秒、
    全市场 5000+ 只要 7~8 秒），窗口整个冻住、连"正在算"都看不见。
    """
    _open_editor(page)          # 编辑器默认收起着；收起时进度条的 isVisible() 恒为 False
    page.editor.setPlainText("C>MA(C,5)")

    page.on_preview()

    # on_preview() 已经返回了，但活儿还没干完 —— 这几条就是"异步"的直接证据
    assert page.preview_worker is not None, "试算必须在后台线程里跑"
    assert page.preview_worker.isRunning() is True
    assert page.btn_preview.isEnabled() is False     # 跑完之前不给再点
    assert page.progress.isVisible() is True         # 有看得见的"正在跑"
    assert "正在运行" in page.hint_text

    _wait_preview(page, qapp)
    assert page.btn_preview.isEnabled() is True      # 跑完把按钮还回来
    assert page.progress.isVisible() is False
    assert "最近交易日" in page.hint_text            # 结果是在线程落地之后才写的


def test_preview_runs_off_the_gui_thread(page, qapp, monkeypatch) -> None:
    """**试算的计算发生在工作线程里**：干活的 `QThread.currentThread()` 不是界面线程。

    这是"被人改回同步调用"就会立刻变红的那条：它不看时间、不看文案，
    只看"干活的是哪个线程"—— 所以不会因为机器快慢偶发变红。

    顺带钉住另一件事：这个替身**没有 `progress_cb` 参数**（`preview_hits` 本来就不收），
    所以谁要是让 worker 无脑塞 `progress_cb`，这里会因为函数压根没被调用而变红。
    """
    seen: dict = {}

    def fake_preview_hits(formula, db_path, *, limit, **kwargs):   # 界面还会传 cfg
        seen["thread"] = QThread.currentThread()
        seen["formula"] = formula
        seen["db_path"] = db_path
        seen["limit"] = limit
        return {"date": "2026-09-11", "count": 0, "hits": [], "shown": 0,
                "scanned": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr(lib, "preview_hits", fake_preview_hits)
    page.editor.setPlainText("C>MA(C,5)")

    page.on_preview()
    _wait_preview(page, qapp)

    assert seen["thread"] is not None, "试算工作函数根本没被调用"
    assert seen["thread"] is not qapp.thread(), "试算又跑回界面线程了（窗口会冻住）"
    assert seen["db_path"] == page.cfg.db_path
    assert seen["limit"] == fp.PREVIEW_LIMIT
    assert seen["formula"].min_history == 5          # 交给线程的是**编译好**的公式


def test_preview_reports_known_hits_as_name_and_code(page, page_cfg, qapp) -> None:
    """试算：`名称（代码）` 格式 + 命中数就是小库里的那两只上涨票。"""
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    hint = page.hint_text
    assert "最近交易日 2026-09-11" in hint
    assert "命中 2 只" in hint
    # 标的写法是**半角** `名称(代码)`（改版方案第四节：全项目一套写法）
    assert "甲样本(600001)" in hint and "丙样本(600003)" in hint
    assert "乙样本(600002)" not in hint            # 下跌那只不该出现


def test_preview_with_no_hits_says_why(page, qapp) -> None:
    page.editor.setPlainText("C>MA(C,5)*100")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    assert "没有命中" in page.hint_text


def test_preview_no_hit_message_is_byte_for_byte_the_same(page, qapp,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """没命中那句：**逐字**钉住（后台化只该换线程，不该换字）。"""
    monkeypatch.setattr(lib, "preview_hits", lambda *a, **k: {
        "date": "2026-09-11", "count": 0, "hits": [], "shown": 0,
        "scanned": 7, "skipped": 3, "errors": []})
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    assert page.hint_text == (
        "最近交易日 2026-09-11：没有命中（扫了 7 只，3 只因数据不足跳过）"
    )


def test_preview_hit_message_keeps_truncation_hint_and_skipped_errors(
        page, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """命中那一支的三种附加文案（只列前 N 只 / 涨停池提醒 / 算不出来的票）逐字钉住。"""
    monkeypatch.setattr(lib, "preview_hits", lambda *a, **k: {
        "date": "2026-09-11", "count": 5, "shown": 2,
        "hits": [{"symbol": "600001", "name": "甲样本"},
                 {"symbol": "600003", "name": "丙样本"}],
        "scanned": 30, "skipped": 1,
        "errors": ["乙样本(600002)：本地涨停池还没攒够"]})
    page.editor.setPlainText("连板()>=2")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    assert page.hint_text == (
        "最近交易日 2026-09-11 命中 5 只：甲样本(600001)、丙样本(600003)"
        " …（只列前 2 只）"
        "\n⚠️ " + lib.LIMIT_UP_HINT
        # 错误那一行是**原样透传** `preview_hits()` 给的文本（写法由库里决定）
        + "\n（1 只票算不出来，已跳过：乙样本(600002)：本地涨停池还没攒够）"
    )


def test_preview_reports_broken_formula_instead_of_running(page) -> None:
    """公式写错：**根本不起线程**（不用等后台；报错就是报错）。"""
    page.editor.setPlainText("C>MAA(C,5)")

    page.btn_preview.click()

    assert "未知函数" in page.hint_text
    assert page.preview_worker is None
    assert page.btn_preview.isEnabled() is True
    assert page.progress.isVisible() is False


def test_preview_result_belongs_to_the_clicked_formula(page, qapp,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """等待期间接着改编辑框，**不会**把结果显示成另一条公式的答案。

    做法：把工作函数卡住（在工作线程里等一个 Event），期间把编辑框换成另一条公式，
    再放行。文案里的"涨停池提醒"只可能来自按下按钮那一刻的那条公式
    —— 这正是"先编译一次、把快照交给线程"要保住的东西。
    """
    started, release = threading.Event(), threading.Event()

    def slow_preview_hits(formula, db_path, *, limit, **kwargs):   # 界面还会传 cfg
        started.set()
        assert release.wait(PREVIEW_TIMEOUT), "测试没有放行工作线程"
        return {"date": "2026-09-11", "count": 0, "hits": [], "shown": 0,
                "scanned": 1, "skipped": 0, "errors": []}

    monkeypatch.setattr(lib, "preview_hits", slow_preview_hits)
    page.editor.setPlainText("连板()>=2")
    page.on_preview()
    assert started.wait(PREVIEW_TIMEOUT), "试算线程没起来"
    snapshot = page.preview_formula

    page.editor.setPlainText("C>MA(C,5)")        # 用户在等待期间改了公式
    release.set()
    _wait_preview(page, qapp)

    assert "涨停池" in page.hint_text             # 提醒来自"按下按钮时"的那条公式
    assert lib.limit_up_hint(snapshot) == lib.LIMIT_UP_HINT
    assert lib.limit_up_hint(page.compile_current()) == ""   # 编辑框里现在这条没有提醒


def test_preview_double_click_only_starts_one_worker(page, qapp,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """连点两次：**只有一个**后台线程在跑（第二次只提示"还在跑"）。"""
    started, release = threading.Event(), threading.Event()
    calls: list = []

    def slow_preview_hits(formula, db_path, *, limit, **kwargs):   # 界面还会传 cfg
        calls.append(1)
        started.set()
        assert release.wait(PREVIEW_TIMEOUT), "测试没有放行工作线程"
        return {"date": "2026-09-11", "count": 0, "hits": [], "shown": 0,
                "scanned": 1, "skipped": 0, "errors": []}

    monkeypatch.setattr(lib, "preview_hits", slow_preview_hits)
    page.editor.setPlainText("C>MA(C,5)")
    page.on_preview()
    assert started.wait(PREVIEW_TIMEOUT), "试算线程没起来"
    first = page.preview_worker

    page.on_preview()        # 第二次（按钮已灰，但直接调 handler 也要拦住）

    assert page.preview_worker is first
    assert len(calls) == 1
    assert "请稍候" in page.hint_text

    release.set()
    _wait_preview(page, qapp)
    assert "最近交易日" in page.hint_text          # 跑完照样把结果写上


def test_preview_data_error_is_reported_as_data_problem(page, qapp,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """库不存在（`FormulaDataError`）：说**数据**的事，让用户去下载数据。"""
    def boom(formula, db_path, *, limit, **kwargs):               # 界面还会传 cfg
        raise fm.FormulaDataError("本地数据库不存在：/x/trader.db（请先在界面点【下载数据】）")

    monkeypatch.setattr(lib, "preview_hits", boom)
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")

    page.on_preview()
    _wait_preview(page, qapp)

    assert page.hint_text == "❌ 本地数据库不存在：/x/trader.db（请先在界面点【下载数据】）"
    assert page.btn_preview.isEnabled() is True     # 失败也要把按钮还回来
    assert page.progress.isVisible() is False       # 进度条不能挂在界面上


def test_preview_unexpected_error_is_reported_in_chinese(page, qapp,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """其它异常（程序问题）：带上类型名说清楚，界面不崩、按钮也还回来。"""
    def boom(formula, db_path, *, limit, **kwargs):               # 界面还会传 cfg
        raise RuntimeError("库文件被占用")

    monkeypatch.setattr(lib, "preview_hits", boom)
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")

    page.on_preview()
    _wait_preview(page, qapp)

    assert page.hint_text == "❌ 运行失败：RuntimeError: 库文件被占用"
    assert page.btn_preview.isEnabled() is True
    assert page.progress.isVisible() is False


# ══════════════════════════════════════════════════════════════════════════
# 6.5) 【运行】+【导出选股结果】（用户 2026-09-18 要求）
#
# 用户原话：「把策略编辑下面的试算直接改成运行，后面再加上导出选股结果
# （导出到桌面文档）」。所以这一段钉住三件事：
#   1. 按钮上的字是【运行】（"试算"这个词从界面上消失）；
#   2. 导出的是**上一次【运行】的全量命中**（提示区只列 20 只，文件里是全部）；
#   3. 导出的版式与「开始选股」建池时那份**同一个函数**产的（来源列写
#      `公式·<公式名>`，与「自选股池」表格里同一个词）。
# ══════════════════════════════════════════════════════════════════════════


def _fake_preview(monkeypatch, hits, *, shown=None, date="2026-09-11", count=None) -> None:
    """把 `preview_hits` 换成确定性的返回值（不碰库、不起真线程的活儿）。"""
    payload = {
        "date": date,
        "count": len(hits) if count is None else count,
        "hits": list(hits),
        "shown": len(hits) if shown is None else shown,
        "scanned": 3,
        "skipped": 0,
        "errors": [],
        "notes": [],
    }
    monkeypatch.setattr(lib, "preview_hits", lambda *a, **k: dict(payload))


def _run(page, qapp) -> None:
    """点【运行】并等它落地（后面的用例都走这一条）。"""
    page.btn_preview.click()
    _wait_preview(page, qapp)


def test_pick_buttons_are_run_and_export(page) -> None:
    """按钮文案：原来叫【试算】，用户要求改成【运行】；右边多一个【导出选股结果】。"""
    assert page.btn_preview.text().startswith("运行")
    assert "试算" not in page.btn_preview.text()
    assert page.btn_export.text() == "导出选股结果"
    # 用户点之前要知道"导出的是什么、导到哪去"
    tip = page.btn_export.toolTip()
    assert "桌面" in tip and "运行" in tip
    # 提示区那句"下一步"也要跟着改口，否则用户按提示找不到【试算】这个按钮
    assert "试算" not in fp.EDITOR_HINT and "运行" in fp.EDITOR_HINT


def test_export_after_run_writes_every_hit_to_the_desktop(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """点【导出选股结果】→ 桌面目录里出现那份文件，里面是**全部**命中。

    `shown=1` 是故意的：界面提示区只列 1 只（真实场景是 20 只），文件里必须 3 只都有
    —— "文件里少几只"在界面上完全看不出来，只能靠这条断言守。
    """
    hits = [{"symbol": "600001", "name": "甲样本"},
            {"symbol": "600002", "name": "乙样本"},
            {"symbol": "600003", "name": "丙样本"}]
    _fake_preview(monkeypatch, hits, shown=1)
    toasts: list[str] = []
    page.status_cb = toasts.append        # 页面调它时会现取，所以直接挂上就行
    page.name_edit.setText("尾盘选股策略")
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)

    assert page.last_run is not None
    assert page.last_run["name"] == "尾盘选股策略"
    assert len(page.last_run["hits"]) == 3           # 全量存下来给导出用
    assert "只列前 1 只" in page.hint_text            # 界面显示确实截断了

    page.btn_export.click()

    # 文件落在 `tests/conftest.py` 那个"绝不写真人桌面"的守卫指定的目录里
    files = sorted((tmp_path / "desktop-export").glob("*.txt"))
    assert len(files) == 1, f"应当只导出 1 个文件，实际 {files}"
    raw = files[0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "桌面文件必须是带 BOM 的 utf-8（记事本中文不乱码）"
    assert b"\r\n" in raw, "桌面文件用 CRLF（Windows 记事本双击就看）"
    text = raw.decode("utf-8-sig")
    assert "共 3 只（策略 3 · 自选 0）" in text
    for symbol in ("600001", "600002", "600003"):
        assert f"({symbol})" in text, f"{symbol} 没写进文件（提示区截断不能影响导出）"
    # 「来源」列与「自选股池」表格同一个词：公式选中 → `公式·<公式名>`
    assert "来源：公式·尾盘选股策略" in text
    assert "行情日 2026-09-11" in text
    assert "现价" in text                    # 库里有两个交易日 → 现价与涨跌幅都算得出来
    # 成败都要看得见：提示区写清楚 + 标题区弹一句
    assert "已导出选股结果" in page.hint_text and files[0].name in page.hint_text
    assert str(files[0]) in page.hint_text
    assert any("已导出" in t for t in toasts), toasts


def test_export_without_a_run_asks_to_run_first(page, tmp_path: Path) -> None:
    """还没点过【运行】：告诉他先跑一遍，**不导出空文件**。"""
    _open_editor(page)

    page.btn_export.click()

    assert "先点【运行】" in page.hint_text
    assert not (tmp_path / "desktop-export").exists()


def test_export_with_no_hits_says_there_is_nothing(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """【运行】命中 0 只：明说"没有可导出的结果"，也不落文件。"""
    _fake_preview(monkeypatch, [], count=0)
    toasts: list[str] = []
    page.status_cb = toasts.append
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)
    assert "没有命中" in page.hint_text

    page.btn_export.click()

    assert "没有可导出的结果" in page.hint_text
    assert any("没有可导出的结果" in t for t in toasts), toasts
    assert not list((tmp_path / "desktop-export").glob("*.txt"))


def test_export_failure_is_loud_not_silent(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """写盘失败：提示区 + 标题区都要说（产物在桌面，"没反应"等于什么都没说）。"""
    _fake_preview(monkeypatch, [{"symbol": "600001", "name": "甲样本"}])
    toasts: list[str] = []
    page.status_cb = toasts.append
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)

    def boom(*args, **kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(fp.pool_mod, "export_pick_file", boom)

    page.btn_export.click()

    assert page.hint_text.startswith("❌ 导出失败：RuntimeError: 磁盘满了")
    assert any("导出失败" in t for t in toasts), toasts


def test_export_never_uses_a_previous_runs_result(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """新一轮【运行】一开始，上一轮的结果就作废（导出只导"我刚看过的那一份"）。"""
    _fake_preview(monkeypatch, [{"symbol": "600001", "name": "甲样本"}])
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)
    assert page.last_run is not None

    # 第二次运行：把工作函数卡住，趁"正在跑"点导出
    started, release = threading.Event(), threading.Event()

    def slow_preview_hits(formula, db_path, *, limit, **kwargs):
        started.set()
        assert release.wait(PREVIEW_TIMEOUT), "测试没有放行工作线程"
        return {"date": "2026-09-11", "count": 1, "shown": 1,
                "hits": [{"symbol": "600002", "name": "乙样本"}],
                "scanned": 1, "skipped": 0, "errors": []}

    monkeypatch.setattr(lib, "preview_hits", slow_preview_hits)
    page.on_preview()
    assert started.wait(PREVIEW_TIMEOUT), "运行线程没起来"

    page.btn_export.click()

    # 正在跑：既不能导上一轮（那是陈结果），也不能说"还没运行过"（他刚点过）
    assert "运行还没跑完" in page.hint_text
    assert not list((tmp_path / "desktop-export").glob("*.txt"))

    release.set()
    _wait_preview(page, qapp)
    assert page.last_run["hits"] == [{"symbol": "600002", "name": "乙样本"}]


def test_export_source_says_unnamed_when_the_formula_has_no_name(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """没填公式名：【来源】写「公式·未命名公式」，而不是留一段空白。"""
    _fake_preview(monkeypatch, [{"symbol": "600001", "name": "甲样本"}])
    page.name_edit.setText("")                       # 名称框留空
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)

    page.btn_export.click()

    files = sorted((tmp_path / "desktop-export").glob("*.txt"))
    assert len(files) == 1
    assert "来源：公式·未命名公式" in files[0].read_text(encoding="utf-8-sig")


# ══════════════════════════════════════════════════════════════════════════
# 7) 【看成绩单】【复制成绩单】**已按改版方案从界面移除**
# ══════════════════════════════════════════════════════════════════════════
#
# 原本这里有 3 个用例（后台线程跑成绩单 / 失败说中文 / 复制成绩单）。
# 删掉的理由（`docs/改版方案.md` TAB 4 与第七节第 3 条）：数据只有 6 个月，
# 而成绩单自己有 250 个交易日的样本门槛 —— 放在界面上永远只会显示"样本不足"。
# **能力没有消失**：`formulas.run_scorecard()` 保留（`tests/test_formula_lib.py`
# 里那几条成绩单用例一条没少），CLI `--scorecard` 照旧。


def test_scorecard_entry_points_are_gone_from_the_page(page) -> None:
    """界面里不能再有成绩单的入口（按钮/线程/结果字段），否则就是"改版没改干净"。"""
    assert not hasattr(page, "btn_scorecard")
    assert not hasattr(page, "btn_copy_scorecard")
    assert not hasattr(page, "scorecard_worker")
    assert not hasattr(page, "scorecard_text")
    assert not hasattr(page, "on_scorecard")
    # 库函数留着（CLI 与将来长样本回测还要用）
    assert callable(lib.run_scorecard)


# ══════════════════════════════════════════════════════════════════════════
# 8) 保存 / 另存为 / 删除
# ══════════════════════════════════════════════════════════════════════════


def test_save_requires_a_name(page) -> None:
    """名称必填：空名拒绝、**提示用户**、而且不会偷偷存出一个无名文件。"""
    page.name_edit.setText("   ")
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save.click()

    assert "请先填公式名称" in page.hint_text
    assert lib.formula_files(page.directory) == []
    assert page.editor.toPlainText() == "C>MA(C,5)"      # 公式还在，用户不用重打


def test_save_sanitizes_name_and_reads_back(page) -> None:
    """名字里的非法字符被安全化，回显给用户的就是磁盘上的名字；存完能被读回。"""
    page.name_edit.setText(" 5日线/放量 ")
    page.editor.setPlainText("M5:=MA(C,5)\nC>M5")

    page.btn_save.click()

    assert page.name_edit.text() == "5日线_放量"
    assert [p.name for p in page.directory.iterdir()] == ["5日线_放量.txt"]
    specs = lib.formula_files(page.directory)
    assert [spec.name for spec in specs] == ["5日线_放量"]
    assert specs[0].ok
    assert specs[0].source.strip() == "M5:=MA(C,5)\nC>M5"
    # 列表里立刻出现（内置 5 条 + 竞价策略常驻在前，公式追加在后）
    assert page.table.rowCount() == FIXED_ROWS + 1
    assert page.table.item(FIXED_ROWS, 0).text() == "5日线_放量"
    # 2026-09-18 用户要求「成功不需要提示，失败再提示」：保存成功时提示区是空的
    # （列表里多出来的那一行本身就是反馈）
    assert page.hint_text == ""


def test_save_overwrite_asks_first_and_can_be_cancelled(page,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """重名覆盖要**二次确认**：点"否"就不许动原文件，点"是"才覆盖。"""
    page.name_edit.setText("重名测试")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
    page.editor.setPlainText("C<MA(C,5)")                 # 改了内容，准备覆盖

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    page.btn_save.click()
    assert lib.formula_files(page.directory)[0].source.strip() == "C>MA(C,5)"
    assert "取消" in page.hint_text

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.btn_save.click()
    assert lib.formula_files(page.directory)[0].source.strip() == "C<MA(C,5)"
    assert len(list(page.directory.iterdir())) == 1       # 覆盖，不是新增一份


def test_save_as_uses_new_name_and_keeps_old(page, monkeypatch: pytest.MonkeyPatch) -> None:
    """另存为：换个名字再存一份，原来那份不动。"""
    page.name_edit.setText("原名")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()

    monkeypatch.setattr(fp.QInputDialog, "getText", lambda *a, **k: ("新名", True))
    page.editor.setPlainText("C<MA(C,5)")
    page.btn_save_as.click()

    specs = {spec.name: spec.source.strip() for spec in lib.formula_files(page.directory)}
    assert specs == {"原名": "C>MA(C,5)", "新名": "C<MA(C,5)"}
    assert page.name_edit.text() == "新名"


def test_save_as_cancelled_changes_nothing(page, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp.QInputDialog, "getText", lambda *a, **k: ("", False))
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save_as.click()

    assert lib.formula_files(page.directory) == []


def test_delete_removes_file_after_confirm(page, monkeypatch: pytest.MonkeyPatch) -> None:
    page.name_edit.setText("要删的")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.btn_delete.click()

    assert lib.formula_files(page.directory) == []
    # 5 条内置策略 + 竞价策略常驻（它们**不可删**），被删掉的那条公式行没了
    assert page.table.rowCount() == FIXED_ROWS
    assert all(page.table.item(row, 0).text() != "要删的" for row in range(FIXED_ROWS))
    assert page.editor.toPlainText() == ""
    assert "已删除" in page.hint_text


def test_delete_can_be_cancelled(page, monkeypatch: pytest.MonkeyPatch) -> None:
    page.name_edit.setText("别删我")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    page.btn_delete.click()

    assert len(lib.formula_files(page.directory)) == 1


# ══════════════════════════════════════════════════════════════════════════
# 9) 统一策略列表：列头、两类行、备注来源
# ══════════════════════════════════════════════════════════════════════════


def _row_index(page, name: str) -> int:
    """按名称找表格行号（**不写死行号**：内置 5 条在前面，公式的先后由文件名定）。"""
    for row in range(page.table.rowCount()):
        if page.table.item(row, 0).text() == name:
            return row
    raise AssertionError(f"列表里没有「{name}」")


def _notes(page) -> dict[str, str]:
    """{行名: 备注列文本}（按列头取，不猜列号）。"""
    return {page.table.item(row, 0).text(): page.table.item(row, 1).text()
            for row in range(page.table.rowCount())}


def _write_formula(folder: Path, name: str, body: str, description: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.txt"
    path.write_text(lib.formula_text(name, body, description), encoding="utf-8")
    return path


def test_list_columns_are_exactly_name_note_state(page) -> None:
    """列头就是用户给定的三个字：`名称 | 备注 | 状态`（不多一列也不少一列）。"""
    columns = [page.table.horizontalHeaderItem(i).text()
               for i in range(page.table.columnCount())]

    assert columns == list(fp.LIST_COLUMNS) == ["名称", "备注", "状态"]


def test_builtin_rows_come_first_and_show_real_evidence(page) -> None:
    """内置 5 条常驻在前（紧随其后是「竞价策略」那一行），备注列写的是代码里已有的证据。"""
    assert page.table.rowCount() == FIXED_ROWS
    assert [row.key for row in page.rows] == fp.builtin_order() + [fp.AUCTION_KEY] == [
        "LadderPullbackStrategy", "ReversalStrategy", "DryUpExpansionStrategy",
        "FirstLimitUpStrategy", "LowPriceStrategy", fp.AUCTION_KEY,
    ]
    assert [row.name for row in page.rows] == [
        "连板回踩低吸", "短期反转", "地量后放量变盘", "首板缩量整理", "低价股",
        fp.AUCTION_NAME,
    ]

    notes = _notes(page)
    ultra = fp.groups.GROUPS["ultra"]
    swing = fp.groups.GROUPS["swing"]
    short = fp.groups.GROUPS["short"]
    # ⛔ 组默认关闭 → 备注就是那句停用理由（一个字都没加）
    assert notes["连板回踩低吸"] == "⛔ 默认关闭：" + ultra.disabled_reason
    assert notes["低价股"] == "⛔ 默认关闭：" + swing.disabled_reason
    # ⚠️ 策略自己写了 evidence_note（正 α 只在"开盘买"口径存在）→ 原样搬过来
    assert notes["地量后放量变盘"] == (
        "⚠️ " + rules.STRATEGIES["DryUpExpansionStrategy"].evidence_note
    )
    assert notes["首板缩量整理"] == (
        "⚠️ " + rules.STRATEGIES["FirstLimitUpStrategy"].evidence_note
    )
    # 两套口径都为正的那条：evidence 字段的语义 + **组**的实测区间（标明是组数字）
    assert notes["短期反转"] == "两套口径都为正 · 组实测 " + short.note.split("：", 1)[1]
    # 数字只能来自上面这些字段：备注里出现的每个数字，都能在源字段里找到
    # （「竞价策略」那一行的数字来自 config 的竞价参数，不在这份源字段里，单独跳过）
    for name, note in notes.items():
        if name == fp.AUCTION_NAME:
            continue
        spec = fp.rules_mod.STRATEGIES[[
            row.key for row in page.rows if row.name == name][0]]
        source = " ".join([
            str(getattr(spec, "evidence_note", "") or ""), ultra.disabled_reason,
            swing.disabled_reason, short.note, ultra.note, swing.note,
        ])
        for chunk in note.replace("⚠️", "").replace("⛔", "").replace("：", " ").split():
            if any(ch.isdigit() for ch in chunk):
                assert chunk in source, f"{name} 的备注里出现了源字段里没有的数字：{chunk!r}"


def test_builtin_rows_are_readonly_and_status_column_is_a_checkbox(page) -> None:
    """「状态」列是勾选框（勾上 = 参与选股），勾选状态来自 `groups.resolve_from_config()`。"""
    from PySide6.QtWidgets import QCheckBox

    boxes = {}
    for index, row in enumerate(page.rows):
        holder = page.table.cellWidget(index, 2)
        box = holder.findChild(QCheckBox)
        assert box is not None, f"{row.name} 的「状态」列不是勾选框"
        boxes[row.key] = box.isChecked()
        assert "参与选股" in box.toolTip()

    enabled = fp.builtin_enabled(page.cfg)
    assert boxes == {row.key: (row.key in enabled) for row in page.rows}
    # 出厂默认只开 `short`（三条），超短与波段那两条默认关
    assert boxes["ReversalStrategy"] is True
    assert boxes["LadderPullbackStrategy"] is False
    assert boxes["LowPriceStrategy"] is False


def test_formula_rows_are_appended_after_builtins_with_file_note(page) -> None:
    """自定义公式追加在内置之后，「备注」列 = 公式文件里的 `# 说明:`。"""
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)", "站上5日线且放量")
    page.reload()

    assert page.table.rowCount() == FIXED_ROWS + 1
    assert page.table.item(FIXED_ROWS, 0).text() == "放量上攻"
    assert page.table.item(FIXED_ROWS, 1).text() == "站上5日线且放量"


def test_list_marks_broken_formula_with_reason(page) -> None:
    """语法错的公式在列表里标出来并给原因（一条坏公式不影响别的）。"""
    _write_formula(page.directory, "坏公式", "C>MAA(C,5)")
    _write_formula(page.directory, "好公式", "C>MA(C,5)", "没问题的")

    page.reload()

    assert page.table.rowCount() == FIXED_ROWS + 2
    notes = _notes(page)
    assert notes["好公式"] == "没问题的"
    assert "⛔ 语法错" in notes["坏公式"]
    assert "未知函数" in notes["坏公式"]
    # 全文（含行号列号）在 tooltip 里
    assert "第 1 行" in page.table.item(_row_index(page, "坏公式"), 1).toolTip()
    # 编译不过的公式**勾了也跑不了**：勾选框置灰，而不是"勾上却没有反应"
    from PySide6.QtWidgets import QCheckBox

    broken = page.table.cellWidget(_row_index(page, "坏公式"), 2).findChild(QCheckBox)
    assert broken.isEnabled() is False


def test_list_shows_runtime_error_from_last_run(page, monkeypatch) -> None:
    """运行期出错（数据不够之类）也要在列表里标出来 —— 只有日志是不够的。"""
    _write_formula(page.directory, "用连板的", "连板()>=2")
    monkeypatch.setattr(fp.formula_group, "last_status",
                        lambda: {"用连板的": "3 只票算不出来：本地涨停池还没攒够"})

    page.reload()

    row = _row_index(page, "用连板的")
    assert page.table.item(row, 1).text().startswith("⚠️ 运行时出错")
    assert "涨停池" in page.table.item(row, 1).toolTip()


def test_clicking_builtin_row_opens_readonly_detail(page, qapp) -> None:
    """单击内置策略行 → **只读详情**（条件说明 + 证据 + 当前状态），可复制。"""
    page.on_open_editor()                                  # 先把编辑器打开
    page.editor.setPlainText("C>MA(C,5)")                  # 用户正在写的草稿
    page.select_row("LowPriceStrategy")
    qapp.processEvents()

    assert page.bottom_stack.currentWidget() is page.detail_page
    assert page.detail_view.isReadOnly() is True
    assert page.detail_text.startswith("低价股（LowPriceStrategy）")
    assert "条件说明" in page.detail_text
    assert inspect.getdoc(rules.STRATEGIES["LowPriceStrategy"]) in page.detail_text
    assert "证据" in page.detail_text
    assert fp.groups.GROUPS["swing"].disabled_reason in page.detail_text
    assert "当前状态：☐ 未参与选股" in page.detail_text
    # 内置策略**不进编辑器**：用户手里的草稿一个字都没被换掉
    assert page.editor.toPlainText() == "C>MA(C,5)"
    assert page.name_edit.text() == ""

    page.btn_copy_detail.click()
    assert QApplication.clipboard().text() == page.detail_text


def test_builtin_detail_state_follows_the_checkbox(page, qapp) -> None:
    """勾上内置策略后，详情里的"当前状态"立刻跟着变（两处说法不能打架）。"""
    page.select_row("LowPriceStrategy")
    qapp.processEvents()
    assert "未参与选股" in page.detail_text

    page._builtin_boxes["LowPriceStrategy"].setChecked(True)
    qapp.processEvents()

    assert "当前状态：✅ 参与选股" in page.detail_text
    assert "swing" in page.detail_text            # 状态说明里写着现在启用了哪些组


def test_selecting_row_loads_formula_into_editor(page, qapp) -> None:
    """点公式行 → 展开编辑器并载入（名称 + 备注 + 正文），可以直接改再保存。"""
    page.name_edit.setText("甲公式")
    page.note_edit.setText("甲的备注")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
    page.name_edit.setText("乙公式")
    page.note_edit.clear()
    page.editor.setPlainText("C<MA(C,5)")
    page.btn_save.click()
    page.on_close_panel()

    page.select_row("甲公式")
    qapp.processEvents()

    assert page.bottom_stack.currentWidget() is page.editor_page
    assert page.name_edit.text() == "甲公式"
    assert page.note_edit.text() == "甲的备注"        # 备注也跟着载入
    assert page.editor.toPlainText() == "C>MA(C,5)"
    assert "已载入" in page.hint_text


def test_reload_does_not_clobber_editor_draft_or_open_panels(page, qapp) -> None:
    """主窗口选完股会调 `reload()`：它**绝不能**换掉用户正在写的草稿或弹出编辑器。

    旧版 `reload()` 会顺手选中第一行并把它载入编辑框；在"列表在上、编辑器按需展开"
    的布局里，那会变成"每次选完股，编辑器自己弹出来并顶掉我正在写的公式"。
    """
    page.editor.setPlainText("写到一半的草稿")
    page.name_edit.setText("草稿")
    assert page.bottom_stack.isVisible() is False          # 编辑器本来是收起的

    page.reload()
    qapp.processEvents()

    assert page.editor.toPlainText() == "写到一半的草稿"
    assert page.name_edit.text() == "草稿"
    assert page.bottom_stack.isVisible() is False
    assert page.selected_row() is None                     # 也不会偷偷选中一行


def test_reload_keeps_enabled_checkbox_state_from_config(page, page_cfg) -> None:
    page_cfg.enabled_formulas = ["甲公式"]
    page.name_edit.setText("甲公式")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()

    page.reload()

    assert page._row_boxes["甲公式"].isChecked() is True
    assert page._builtin_boxes["ReversalStrategy"].isChecked() is True   # 内置那三条照旧
    assert page._builtin_boxes["LowPriceStrategy"].isChecked() is False


# ══════════════════════════════════════════════════════════════════════════
# 10) 勾「参与选股」→ 写回 config.toml（**三种键**）
# ══════════════════════════════════════════════════════════════════════════


def test_enable_checkbox_writes_config_toml(page, page_cfg, qapp) -> None:
    """勾公式 → config.toml 出现 enabled_formulas，**用户注释与未知键都保留**。

    公式在这里**直接落文件**、不走编辑器保存：编辑器保存会按 2026-09-18 的新行为
    自动勾上「参与选股」（用户实报"存了却不参与"之后改的），而这条用例要测的是
    "勾选动作写回哪个键"，所以需要一个**存在但未启用**的公式当起点。
    """
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()
    box = page._row_boxes["放量上攻"]
    assert box.isChecked() is False            # 直接落文件的公式默认**不参与**

    box.setChecked(True)
    qapp.processEvents()

    text = page_cfg.source_path.read_text(encoding="utf-8")
    assert 'enabled_formulas = ["放量上攻"]' in text
    assert "enabled_groups" not in text.split("enabled_formulas")[0].replace(
        'enabled_groups = ["short"]', "")     # 勾公式**不动**内置那两个键
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert page_cfg.enabled_formulas == ["放量上攻"]
    assert "已加入选股" in page.hint_text


def test_uncheck_enable_removes_from_config(page, page_cfg, qapp,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")   # 同上的理由：落文件，不经过编辑器保存
    page.reload()
    box = page._row_boxes["放量上攻"]
    box.setChecked(True)
    qapp.processEvents()

    box.setChecked(False)
    qapp.processEvents()

    assert page_cfg.enabled_formulas == []
    assert "enabled_formulas = []" in page_cfg.source_path.read_text(encoding="utf-8")
    assert "已退出选股" in page.hint_text


def test_enable_write_failure_reverts_checkbox(page, page_cfg, qapp,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """写不进去（只读盘）时把勾选退回去 —— 界面显示的必须与生效的一致。"""
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")   # 落文件，不经过编辑器保存
    page.reload()
    box = page._row_boxes["放量上攻"]

    def boom(*_args, **_kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr("laoa_trader.config.save_settings", boom)
    box.setChecked(True)
    qapp.processEvents()

    assert box.isChecked() is False
    assert "保存失败" in page.hint_text


def test_builtin_check_writes_both_group_and_strategy_keys(page, page_cfg, qapp) -> None:
    """勾内置策略 → **同时**写 `enabled_groups` 与 `enabled_strategies`。

    为什么两个都要写（这页最容易踩的坑）：`groups.resolve()` 在两者都非空时取**交集**，
    而 `enabled_groups` 的出厂值是 `["short"]`。只写 `enabled_strategies` 的话，
    勾上「低价股」（swing 组）会算出**空交集** → 整轮选股被跳过（"勾了却不跑"）。
    """
    box = page._builtin_boxes["LowPriceStrategy"]
    assert box.isChecked() is False

    box.setChecked(True)
    qapp.processEvents()

    text = page_cfg.source_path.read_text(encoding="utf-8")
    assert 'enabled_groups = ["short", "swing"]' in text
    assert ('enabled_strategies = ["ReversalStrategy", "DryUpExpansionStrategy", '
            '"FirstLimitUpStrategy", "LowPriceStrategy"]') in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    # 两条路都要真的生效：解析出来的策略就是这四条（交集不为空）
    assert fp.builtin_enabled(page_cfg) == {
        "ReversalStrategy", "DryUpExpansionStrategy", "FirstLimitUpStrategy",
        "LowPriceStrategy",
    }
    assert "已参与选股" in page.hint_text
    assert "enabled_groups" in page.hint_text and "enabled_strategies" in page.hint_text


def test_uncheck_one_short_member_keeps_the_other_two(page, page_cfg, qapp) -> None:
    """出厂配置（只写组、没写策略）下取消勾选**一条**：另外两条必须还在。"""
    page._builtin_boxes["ReversalStrategy"].setChecked(False)
    qapp.processEvents()

    assert page_cfg.enabled_groups == ["short"]
    assert page_cfg.enabled_strategies == [
        "DryUpExpansionStrategy", "FirstLimitUpStrategy"
    ]
    assert fp.builtin_enabled(page_cfg) == {
        "DryUpExpansionStrategy", "FirstLimitUpStrategy"
    }                                          # 旧的「组=short 全选」语义被收紧成两条


def test_uncheck_all_builtins_writes_explicit_off(page, page_cfg, qapp) -> None:
    """全部取消勾选 → 写 `enabled_groups = ["none"]`（= 只盯自选股）。

    为什么不能写空列表：`resolve()` 里"两个键都空"是**全选**这个安全默认，
    写空会反过来变成"五条策略一起跑"—— 与用户的意图正好相反。
    """
    for box in list(page._builtin_boxes.values()):
        box.setChecked(False)
    qapp.processEvents()

    assert page_cfg.enabled_groups == [fp.OFF_GROUP_KEY] == ["none"]
    assert page_cfg.enabled_strategies == []
    selection = groups.resolve_from_config(page_cfg)
    assert selection.explicit_off is True
    assert selection.strategies == ()
    assert "只盯自选股" in page.hint_text


def test_builtin_write_failure_reverts_checkbox(page, page_cfg, qapp,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """内置策略写不进去时同样退回勾选（界面不能显示成"已经开了"）。"""
    box = page._builtin_boxes["LowPriceStrategy"]

    def boom(*_args, **_kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr("laoa_trader.config.save_settings", boom)
    box.setChecked(True)
    qapp.processEvents()

    assert box.isChecked() is False
    assert "保存失败" in page.hint_text
    assert fp.builtin_enabled(page_cfg) == {
        "ReversalStrategy", "DryUpExpansionStrategy", "FirstLimitUpStrategy"
    }                                          # 配置一个字都没变


# ══════════════════════════════════════════════════════════════════════════
# 11) 右键菜单：启用/关闭、删除（内置不可删）
# ══════════════════════════════════════════════════════════════════════════


def test_row_menu_for_formula_offers_toggle_and_delete(page, monkeypatch) -> None:
    """公式行：菜单里是【启用】+【删除】；删除**先二次确认**，确认后文件才没。"""
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()

    picked = page.row_menu(page.row_of("放量上攻"))
    assert [action.text() for action in picked.menu.actions()] == ["启用", "删除"]
    assert picked.delete.isEnabled() is True

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    picked.delete.trigger()

    assert lib.formula_files(page.directory) == []
    assert "已删除" in page.hint_text
    assert page.table.rowCount() == FIXED_ROWS             # 只剩固定的那几行


def test_row_menu_delete_can_be_cancelled(page, monkeypatch) -> None:
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    page.row_menu(page.row_of("放量上攻")).delete.trigger()

    assert len(lib.formula_files(page.directory)) == 1
    assert "已取消删除" in page.hint_text


def test_row_menu_on_builtin_cannot_delete(page, page_cfg) -> None:
    """内置策略：删除项**置灰并写明理由**（不是悄悄消失 —— 那会像"程序坏了"）。"""
    picked = page.row_menu(page.row_of("LowPriceStrategy"))

    assert [action.text() for action in picked.menu.actions()] == [
        "启用", fp.MENU_DELETE_BUILTIN,
    ]
    assert fp.MENU_DELETE_BUILTIN == "删除（内置策略不可删）"
    assert picked.delete.isEnabled() is False
    assert "只能启用/关闭" in picked.delete.toolTip()
    # 菜单里没有【删除】的落点，点它也不会动内置策略或配置
    before = page_cfg.source_path.read_text(encoding="utf-8")
    picked.delete.trigger()                    # 置灰动作触发是空操作
    assert page_cfg.source_path.read_text(encoding="utf-8") == before
    assert len(page.rows) == FIXED_ROWS        # 内置策略（与竞价那一行）还在列表里


def test_row_menu_toggle_matches_and_updates_the_row_state(page, page_cfg, qapp) -> None:
    """菜单第一项**按当前状态只出现一个**，触发之后状态与配置同步变化。"""
    picked = page.row_menu(page.row_of("LowPriceStrategy"))
    assert picked.toggle.text() == "启用"      # 它现在是关的

    picked.toggle.trigger()
    qapp.processEvents()

    assert page_cfg.enabled_groups == ["short", "swing"]
    assert page._builtin_boxes["LowPriceStrategy"].isChecked() is True
    # 状态变了 → 菜单文案必须跟着变（不跟着变就等于告诉用户相反的事实）
    assert page.row_menu(page.row_of("LowPriceStrategy")).toggle.text() == "关闭"
    assert "已参与选股" in page.hint_text

    page.row_menu(page.row_of("LowPriceStrategy")).toggle.trigger()
    qapp.processEvents()
    assert page_cfg.enabled_groups == ["short"]
    assert page._builtin_boxes["LowPriceStrategy"].isChecked() is False


# ══════════════════════════════════════════════════════════════════════════
# 9.5) 「竞价策略」那一行（用户 2026-09-18："不是公式，是把「竞价扫描」做成策略"）
#
# 它排在 5 条内置策略之后、公式之前；勾它开关的是**盘中竞价扫描**（`intraday_auction`），
# 而**不是**"参与选股" —— 这几条用例里最要紧的就是把这条界线钉死：
# 勾完以后 `enabled_groups` / `enabled_strategies` / `enabled_formulas` 一个都不许变。
# ══════════════════════════════════════════════════════════════════════════


def test_auction_row_sits_between_builtins_and_formulas(page) -> None:
    """位置：5 条内置之后、公式之前；备注列写的是**现读配置**的口径。"""
    _write_formula(page.directory, "我的公式", "C>MA(C,5)")
    page.reload()

    names = [page.table.item(row, 0).text() for row in range(page.table.rowCount())]
    assert names[:5] == ["连板回踩低吸", "短期反转", "地量后放量变盘", "首板缩量整理", "低价股"]
    assert names[5] == fp.AUCTION_NAME
    assert names[6] == "我的公式"
    # 备注列的数字来自 config（不是界面编的）：改一个数，备注跟着变
    note = page.table.item(5, 1).text()
    assert "涨幅 2.0~9.0%" in note
    # 「不参与选股」必须在**最前面**：备注列会被省略号截断，结论不能被截掉
    assert note.startswith("只做盘中提示、不参与选股")
    page.cfg.auction_min_pct = 5.0
    page.reload()
    assert "涨幅 5.0~9.0%" in page.table.item(5, 1).text()


def test_auction_row_toggle_writes_intraday_auction_only(page, page_cfg, qapp) -> None:
    """勾上它 → 只写 `intraday_auction`；**选股那三个键一个字都不动**。"""
    groups_before = list(page_cfg.enabled_groups)
    strategies_before = list(page_cfg.enabled_strategies)
    formulas_before = list(page_cfg.enabled_formulas)

    page._auction_box.setChecked(True)
    qapp.processEvents()

    assert page_cfg.intraday_auction is True
    assert page_cfg.enabled_groups == groups_before          # 不参与选股 = 这三个键不变
    assert page_cfg.enabled_strategies == strategies_before
    assert page_cfg.enabled_formulas == formulas_before
    assert "竞价策略已开启" in page.hint_text
    assert "不参与选股" in page.hint_text
    # 写进 config.toml 的就是那个键
    assert "intraday_auction" in page_cfg.source_path.read_text(encoding="utf-8")

    page._auction_box.setChecked(False)
    qapp.processEvents()
    assert page_cfg.intraday_auction is False
    assert "竞价策略已关闭" in page.hint_text


def test_auction_row_checkbox_follows_config(page, page_cfg) -> None:
    """「状态」列反映 `intraday_auction`（设置页改过也一样看得到）。"""
    assert page._auction_box.isChecked() is False
    page_cfg.intraday_auction = True
    page.reload()
    assert page._auction_box.isChecked() is True
    assert page.row_of(fp.AUCTION_KEY).enabled is True


def test_clicking_auction_row_opens_readonly_detail_not_editor(page, qapp) -> None:
    """单击它 → 只读详情（口径 + 两条硬限制），**绝不**把编辑器顶出来。"""
    page.on_open_editor()
    page.editor.setPlainText("C>MA(C,5)")          # 用户正在写的草稿

    page.select_row(fp.AUCTION_KEY)

    assert page.bottom_stack.currentWidget() is page.detail_page
    assert "竞价扫描开关" in page.detail_title.text()
    assert "无法回测" in page.detail_text and "不参与选股" in page.detail_text
    assert page.editor.toPlainText() == "C>MA(C,5)"      # 草稿没被动过
    assert "不是选股策略" in page.hint_text


def test_auction_row_menu_cannot_delete_and_says_why(page) -> None:
    """右键菜单：开关的文案说的是"竞价扫描"，删除项置灰并写明理由。"""
    picked = page.row_menu(page.row_of(fp.AUCTION_KEY))
    assert picked.toggle.text() == "启用"
    assert "竞价扫描" in picked.toggle.toolTip()
    assert picked.delete.text() == fp.MENU_DELETE_AUCTION == "删除（竞价策略是内置设置，不可删）"
    assert picked.delete.isEnabled() is False
    assert "系统设置" in picked.delete.toolTip()


def test_right_click_wires_to_show_menu_for_the_clicked_row(page, qapp, monkeypatch) -> None:
    """右键某一行 → 弹的是**那一行**的菜单（顺带钉住：右键**不展开**编辑器）。"""
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()
    captured: list = []
    monkeypatch.setattr(page, "_show_menu",
                        lambda menu, pos: captured.append(menu))
    page.table.customContextMenuRequested.emit(
        page.table.visualItemRect(page.table.item(FIXED_ROWS, 0)).center()
    )

    assert len(captured) == 1
    assert captured[0].objectName() == "rowMenu:formula:放量上攻"
    assert page.selected_row().key == "放量上攻"
    assert page.bottom_stack.isVisible() is False       # 右键不该把编辑器顶出来


# ══════════════════════════════════════════════════════════════════════════
# 12) 【开始选股】：只 emit 信号（流程在主窗口里）
# ══════════════════════════════════════════════════════════════════════════


def test_start_pick_only_emits_the_signal(page, monkeypatch) -> None:
    """点【开始选股】→ `start_pick_requested` 出手；**这一页自己不跑流程**。"""
    from laoa_trader import scheduler

    def boom(*_args, **_kwargs):               # 谁在这里调选股流程，这条就会炸
        raise AssertionError("公式页自己跑起了选股流程（应该只 emit 信号）")

    monkeypatch.setattr(scheduler, "run_daily", boom)
    seen: list = []
    page.start_pick_requested.connect(lambda: seen.append("go"))
    told: list = []
    page.status_cb = told.append

    page.btn_start_pick.click()

    assert seen == ["go"]
    assert told and "开始选股" in told[0]


def test_start_pick_signal_is_a_real_signal_on_the_page(page) -> None:
    """信号得挂在 `FormulaPage` 上（主窗口 `connect` 的就是它）。"""
    from PySide6.QtCore import SignalInstance

    assert isinstance(page.start_pick_requested, SignalInstance)
    assert hasattr(fp.FormulaPage, "start_pick_requested")


# ══════════════════════════════════════════════════════════════════════════
# 13) 选股结果**不在这一页**（用户要求）+ 主窗口那个兼容方法 + "结果去哪了"要说清
# ══════════════════════════════════════════════════════════════════════════
#
# 用户原话（2026-09-17）：「策略选股只要显示策略，不显示选股结果，选股结果直接进
# 自选股池，可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」
#
# 所以旧版这一页上的「本次选股结果」表 + 一句话结论 + 【全部加为自选】按钮，
# 以及它们的渲染/写库方法（`_refresh_result` / `_render_result` / `_load_result_from_db` /
# `_result_row` / `on_add_all_to_watchlist`）**整体删掉**（这一节原来那两个"结果区"
# 用例与两个"加自选"用例随之作废，见报告）。取而代之要钉住三件事：
#
#   1. 那一块**真的没了**（控件与方法都不在）—— 用户是明确要求删的，
#      一条断言就能挡住"哪天顺手又加回来"；
#   2. `show_pick_result()` **还在**：主窗口 `_on_pipeline_done()` 会在每轮选股后调它，
#      删掉它迟早变成 `AttributeError`（见那个方法的 docstring）；
#   3. 用户必须知道结果去哪了（自选股池 + 桌面文件）—— 文案写在**界面上**，
#      不能只躺在 docstring 里（docstring 用户看不见）。


def test_result_area_is_gone_from_the_page(page) -> None:
    """这一页只有策略列表：没有结果表、没有小结、【全部加为自选】按钮。

    名单里**连数据属性（`result_rows` / `result_date`）也要**：只删控件、把那份数据
    留在页面上，下一个人想"顺手再画出来"就只差几行 —— 变异验证时正是这一条先漏了。
    """
    for name in ("result_box", "result_table", "result_summary", "result_hint",
                 "btn_add_all", "result_rows", "result_date"):
        assert not hasattr(page, name), f"「{name}」应该已经从「策略选股」页删掉"

    # 全页只有一张表（策略列表），列还是用户给的那三列
    assert page.findChildren(QTableWidget) == [page.table]
    assert [page.table.horizontalHeaderItem(i).text()
            for i in range(page.table.columnCount())] == list(fp.LIST_COLUMNS)

    texts = [button.text() for button in page.findChildren(QPushButton)]
    assert "全部加为自选" not in texts
    assert "策略编辑" in texts and "开始选股" in texts
    # 「本次选股结果」那个 QGroupBox 也不能留着（哪怕藏起来也算没删干净）
    assert all("选股结果" not in box.title() for box in page.findChildren(QGroupBox))


def test_removed_result_methods_are_really_gone() -> None:
    """结果区的渲染/写库方法一起删掉（留半套比全留着更危险：能改库却不显示）。"""
    for name in ("_refresh_result", "_render_result", "_load_result_from_db",
                 "_result_row", "on_add_all_to_watchlist"):
        assert not hasattr(fp.FormulaPage, name), name
    assert not hasattr(fp, "RESULT_COLUMNS")            # 那两列的列头常量也带走


def test_show_pick_result_is_kept_so_the_main_window_never_crashes(page, page_cfg,
                                                                   qapp) -> None:
    """主窗口还在调 `show_pick_result(...)` → 方法在、签名不缩水、空调用不炸。

    `ui/app.py::_on_pipeline_done()` 里是 `getattr(page, "show_pick_result", None)` → 有就调。
    今天删掉它不会炸（那边容错了），但哪天接线改成直接调用就是 `AttributeError`
    把"跑完显示结论"那一段带崩 —— 所以这里把"这个方法必须存在"钉住。
    """
    assert callable(getattr(fp.FormulaPage, "show_pick_result", None))
    params = inspect.signature(fp.FormulaPage.show_pick_result).parameters
    assert list(params) == ["self", "rows", "data_date"]
    assert params["rows"].default is None
    assert params["data_date"].kind is inspect.Parameter.KEYWORD_ONLY   # 主窗口按关键字传

    # 三种调用形态都不能出事：report（dict）、行列表、None
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600001", "name": "甲样本",
                                     "strategy": "ReversalStrategy"}]})
    page.show_pick_result([{"symbol": "600001", "strategy": "ReversalStrategy"}])
    page.show_pick_result(None)
    qapp.processEvents()

    # **空实现**：不写库（旧版这里会把票写进 watchlist），也不新增任何控件
    with storage.connect(page_cfg.db_path) as conn:
        assert storage.load_watchlist(conn, enabled_only=False) == []
    assert page.findChildren(QTableWidget) == [page.table]


def test_show_pick_result_docstring_says_where_the_result_goes() -> None:
    """docstring 要写清"界面不再显示 + 结果去哪"（看代码的人得知道用户要的东西没丢）。"""
    doc = fp.FormulaPage.show_pick_result.__doc__ or ""
    assert "空实现" in doc
    assert "自选股池" in doc and "桌面" in doc


def test_page_tells_the_user_where_the_results_go(page) -> None:
    """结果区删了，但"结果去哪了"必须在**界面上**说清（不许静默消失）。

    三处文案：顶部灰字、【开始选股】的 tooltip、点下去那一刻的提示 —
    少一处，用户点完【开始选股】就会以为"什么都没发生"。
    """
    assert "自选股池" in page.page_hint.text() and "桌面" in page.page_hint.text()

    tip = page.btn_start_pick.toolTip()
    assert "自选股池" in tip and "桌面" in tip

    told: list[str] = []
    page.status_cb = told.append
    page.btn_start_pick.click()
    assert told and "自选股池" in told[0] and "桌面" in told[0]




# ══════════════════════════════════════════════════════════════════════════
# 14) 编辑器：按需展开 / 备注写进文件注释头
# ══════════════════════════════════════════════════════════════════════════


def test_editor_is_collapsed_until_asked_for(page) -> None:
    """编辑器默认收起（用户给定："点击打开策略编辑器"），点【策略编辑】才展开。"""
    assert page.bottom_stack.isVisible() is False

    page.btn_edit.click()

    assert page.bottom_stack.isVisible() is True
    assert page.bottom_stack.currentWidget() is page.editor_page

    page.btn_close_editor.click()
    assert page.bottom_stack.isVisible() is False


def test_note_is_written_into_the_file_comment_header(page) -> None:
    """备注 → 公式文件的 `# 说明:` 注释头（存完回显、读回界面也是同一句）。"""
    page.name_edit.setText("放量上攻")
    page.note_edit.setText("站上5日线并且放量")
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save.click()

    text = (page.directory / "放量上攻.txt").read_text(encoding="utf-8")
    assert "# 说明: 站上5日线并且放量" in text
    assert lib.formula_files(page.directory)[0].description == "站上5日线并且放量"
    assert page.table.item(FIXED_ROWS, 1).text() == "站上5日线并且放量"    # 列表备注列
    # 重新载入界面 → 备注从文件读回（两处不会各说各话）
    page.note_edit.clear()
    page.table.clearSelection()          # 同一行再点一次不会触发"选中变化"（Qt 语义）
    page.select_row("放量上攻")
    assert page.note_edit.text() == "站上5日线并且放量"


def test_note_left_empty_gets_auto_described(page) -> None:
    """备注留空 → 用库自己生成的"用到的字段/函数"（小白不用手写说明），并回显给用户。"""
    page.name_edit.setText("放量上攻")
    page.note_edit.clear()
    page.editor.setPlainText("M5:=MA(C,5)\nC>M5")

    page.btn_save.click()

    text = (page.directory / "放量上攻.txt").read_text(encoding="utf-8")
    assert "# 说明: " in text and "字段：C" in text
    assert page.note_edit.text().startswith("字段：")


def test_note_with_newline_is_flattened(page) -> None:
    """备注里的换行必须**拍平**：注释头只有一行，换行会把公式体挤坏。

    真踩过的那种坑：`# 说明: 第一行\\n第二行` 之后，第二行不以 `#` 开头 ——
    引擎读文件时把它当成**公式正文**，用户下次打开这条公式看到的是"未知字段"报错，
    而他明明没有改过公式。
    """
    page.name_edit.setText("多行备注")
    page.note_edit.setText("第一行\n第二行")
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save.click()

    text = (page.directory / "多行备注.txt").read_text(encoding="utf-8")
    assert "# 说明: 第一行 第二行" in text
    assert text.count("\n") == text.count("# ") + 2       # 注释头没被拆成两行
    spec = lib.formula_files(page.directory)[0]
    assert spec.ok is True                               # 公式照样能编译
    assert spec.source.strip() == "C>MA(C,5)"
    assert spec.description == "第一行 第二行"


def test_overlong_note_is_rejected_not_truncated(page) -> None:
    """备注超长：**拒绝保存并说清楚**（静默截断会让用户以为自己写的还在）。"""
    page.name_edit.setText("超长备注")
    page.note_edit.setText("字" * (fp.MAX_NOTE_CHARS + 1))
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save.click()

    assert "备注太长" in page.hint_text
    assert f"最多 {fp.MAX_NOTE_CHARS} 字" in page.hint_text
    assert lib.formula_files(page.directory) == []        # 什么都没写下去
    assert page.editor.toPlainText() == "C>MA(C,5)"       # 公式也还在


# ══════════════════════════════════════════════════════════════════════════
# 15) 载入示例
# ══════════════════════════════════════════════════════════════════════════


def test_load_sample_falls_back_to_builtin_when_dir_empty(page) -> None:
    """目录里没有示例文件时给一条内置的兜底公式（小白第一步一定走得通）。"""
    page.on_load_sample()

    assert page.bottom_stack.currentWidget() is page.editor_page
    assert page.editor.toPlainText() == fp.SAMPLE_TEXT
    assert page.name_edit.text() == fp.SAMPLE_NAME
    assert "已载入" in page.hint_text
    page.btn_validate.click()
    assert "校验通过" in page.hint_text


def test_load_sample_prefers_the_bundled_sample(page) -> None:
    """目录里有示例公式（随包分发的那条）时，载入它本体。"""
    _write_formula(page.directory, "放量上攻", "C>MA(C,5) AND V>MA(V,5)*1.5", "示例")

    page.on_load_sample()

    assert page.editor.toPlainText() == "C>MA(C,5) AND V>MA(V,5)*1.5"
    assert page.note_edit.text() == "示例"
    assert "放量上攻" in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 16) 提示区：可复制、超长截断
# ══════════════════════════════════════════════════════════════════════════


def test_hint_area_is_selectable_and_multiline(page) -> None:
    page.editor.setPlainText("连板()>=2")
    page.btn_validate.click()

    flags = page.hint_label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert "\n" in page.hint_label.text()              # 多行
    assert page.hint_label.wordWrap() is True


def test_long_hint_is_trimmed_but_kept_in_full(page) -> None:
    """很长的提示（多行错误之类）：界面只显示前几行，**完整文本留档**可复制。"""
    long_text = "\n".join(f"第 {i} 行" for i in range(1, 31))

    page._set_hint(long_text)

    assert page.hint_text == long_text                 # 完整文本留档（可选中复制）
    assert len(page.hint_label.text().splitlines()) == fp.HINT_MAX_LINES + 1
    assert f"还有 {30 - fp.HINT_MAX_LINES} 行没显示" in page.hint_label.text()


# ── 保存之后：用户必须**一眼看得出它进了列表、而且参与选股**（2026-09-18 用户实报）──
#
# 用户原话：「编辑器保存了 策略里就不显示，这个问题很严重」。
# 我在源码环境里复现不出来（列表确实会立刻多一行），但顺着这句话查出三件事会让用户
# 产生同样的感觉，所以三条都改掉并钉住：
#   1. 公式追加在**列表末尾**，窗口小的时候它在视野外 → 保存后要滚到那一行并说出第几行；
#   2. 新存的公式**默认不参与选股**（要用户自己再找一个勾选框勾一下）→ 现在保存即参与；
#   3. 保存**失败**时只写提示区、不弹 toast，而提示区在编辑器底部、窗口小就看不见 →
#      现在失败一定弹 toast，并写出公式目录的绝对路径与最可能的原因。


def test_saving_a_formula_shows_it_at_the_bottom_unchecked(page, page_cfg, qapp) -> None:
    """保存 → 它**自动出现在策略列表最下面**、**默认不勾选**、并且**不出任何提示**。

    用户 2026-09-18 的原话（两句，都是产品要求）：
      * 「保存就自动显示在策略最下面，默认不勾选」；
      * 「成功不需要提示，失败再提示」。
    所以这条用例同时钉三件事：进了列表最后一行、勾选框没被替用户勾上、
    **提示区是空的**（"列表里多出来的那一行"就是成功的反馈，再讲一遍只是噪音）。
    """
    page.name_edit.setText("放量上攻（量比版）")
    page.editor.setPlainText("VR:=量比()\nC>=3 AND C<=50 AND VR>=1.5")

    page.btn_save.click()
    qapp.processEvents()

    # ① 在列表**最后一行**（内置在前、公式在后）
    assert page.rows[-1].name == "放量上攻（量比版）"
    assert page.table.item(page.table.rowCount() - 1, 0).text() == "放量上攻（量比版）"
    # ② 默认不勾选（参不参与由用户决定），也就没写进 enabled_formulas
    box = page._row_boxes["放量上攻（量比版）"]
    assert box.isChecked() is False
    assert page_cfg.enabled_formulas == []
    # ③ 成功**不出提示**
    assert page.hint_text == ""


def test_saving_successfully_clears_an_old_failure_hint(page, page_cfg, qapp,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """上一次失败留下的红字，下一次保存成功时要**收掉**（否则一直挂在屏幕上像还在报错）。"""
    def boom(*_args, **_kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(lib, "save_formula", boom)
    page.name_edit.setText("第一次")
    page.editor.setPlainText("C>O")
    page.btn_save.click()
    qapp.processEvents()
    assert page.hint_text, "失败必须留下提示"

    monkeypatch.undo()
    page.editor.setPlainText("C>O")
    page.btn_save.click()
    qapp.processEvents()
    assert page.hint_text == ""


def test_saving_a_broken_formula_saves_it_as_a_draft(page, page_cfg, qapp) -> None:
    """编译不过的公式也**照样存下来**（不拦着用户存草稿），只是**不参与选股**。

    这条是"用户拿还没接进引擎的字段写公式"时的正确反馈：文件在、列表里有它，
    勾选框是灰的、鼠标停上去能看到引擎给的中文原因（例如 `流通市值` 现在还不存在）。
    """
    page.name_edit.setText("六条件版")
    # 用一个**真的写错**的字段名（`流通市值` 现在已经是合法字段了，不再是错误例子）
    page.editor.setPlainText("流通市值X>=10 AND C>=3")

    page.btn_save.click()
    qapp.processEvents()

    assert (page.directory / "六条件版.txt").exists(), "草稿要存下来（不拦着用户）"
    box = page._row_boxes["六条件版"]
    assert box.isChecked() is False
    assert box.isEnabled() is False, "编译不过的勾选框是灰的（勾上也跑不了）"
    assert page_cfg.enabled_formulas == []
    # "存下去了但跑不了"算**有问题**，要说一句（不然用户看着那把灰勾不知道原因）——
    # 这句话里带着引擎给的中文原因与行列号
    assert "公式有错" in page.hint_text and "流通市值X" in page.hint_text


def test_save_failure_shows_the_formula_dir(
    page, page_cfg, qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不进去时：提示里必须带**公式目录的绝对路径**，而且**要弹 toast**。

    为什么这两条都要：提示区在编辑器底部，窗口小一点就看不见 —— 用户会以为"存上了"，
    然后在策略列表里找不到（这正是用户报"保存了却不显示"最可能的样子）。
    带路径是因为下一步他要自己去看那个目录（或把程序搬到可写目录）。
    """
    def boom(*_args, **_kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(lib, "save_formula", boom)
    # 这一页的 `_toast` 是交给外部回调去显示的（主窗口把它挂到标题区的运行状态上），
    # 所以这里把回调接过来，验证"失败真的往外说了"
    toasts: list[str] = []
    page.status_cb = toasts.append
    page.name_edit.setText("写不进去的")
    page.editor.setPlainText("C>O")

    page.btn_save.click()
    qapp.processEvents()

    assert "没存上" in page.hint_text
    assert str(page.directory) in page.hint_text            # 路径要写出来（去哪个目录找）
    assert any("没存上" in t for t in toasts), f"失败必须弹 toast（不然看不见提示区）：{toasts}"


def test_syntax_error_is_announced_outside_the_hint_area(page, qapp) -> None:
    """语法错误必须**弹到标题区的运行状态**（不只是写在页面最下面的提示区）。

    用户 2026-09-18 实报："可以再测试的时候给个提示啊 公式有语法错误" ——
    其实【试算】/【校验】本来就把中文原因写进提示区了，但**提示区在整个页面最下面**
    （窗口小、或用户正看着上面那半屏时，那句话等于没写）。所以编译失败时**同时**
    调一次 `_toast`（主窗口把它显示在标题区的运行状态里，那里永远看得见）。
    这条用例把"两条路都要弹"钉住。
    """
    toasts: list[str] = []
    page.status_cb = toasts.append
    page.editor.setPlainText("流通市值X>=10 AND C>=3")    # 写错的字段名（会编译失败）

    # ①【试算】这条路
    page.on_preview()
    qapp.processEvents()
    assert "流通市值X" in page.hint_text, "提示区里要有中文原因"
    assert any("流通市值X" in t for t in toasts), f"试算时也要弹出来：{toasts}"

    # ②【校验】这条路
    toasts.clear()
    page.on_validate()
    qapp.processEvents()
    assert any("流通市值X" in t for t in toasts), f"校验时也要弹出来：{toasts}"

    # ③ 正常公式**不该弹**（成功不打扰）
    toasts.clear()
    page.editor.setPlainText("C>=3 AND C<=50")
    page.on_validate()
    qapp.processEvents()
    assert toasts == []


def test_preview_passes_the_config_so_snapshot_fields_work(page, page_cfg, qapp,
                                                          monkeypatch) -> None:
    """【试算】必须把 `cfg` 传进 `preview_hits` —— 否则用到**市值/换手**的公式永远 0 只。

    这是"漏接一根线"的典型：`preview_hits` 早就支持按需取快照，但界面调它时没传
    `cfg`，于是那条路根本不会去取数 —— 表现是"公式明明对、就是一只都选不出来，
    而且连'取不到快照'那句提示都不出现"。用户只会以为公式写错了。
    这条用例把整条链路钉住：**界面 → 取快照 → 喂进公式 → 出票**。
    """
    from datetime import date, timedelta

    from laoa_trader.data import sources, storage

    # 造一只票的库（试算要按"最后一根 = 全市场最新行情日"过滤）
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(10)]
    with storage.connect(page_cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600001", "甲样本", "银行")])
        storage.write_daily_raw(conn, [
            ("600001", day, 10.0, 10.2, 9.8, 10.0, 1e6, 1e7) for day in days])
        storage.write_calendar(conn, days)

    # 快照：这只票市值 50 亿（合格）
    monkeypatch.setattr(sources, "snapshot_map",
                        lambda _cfg, symbols=None: {"600001": {"circ_mktcap": 50.0,
                                                              "turnover_rate": 8.0}})
    monkeypatch.setattr(sources, "supplement_map", lambda *a, **k: None)

    _open_editor(page)
    page.editor.setPlainText("流通市值>=10 AND 流通市值<=300")
    page.on_preview()
    _wait_preview(page, qapp)

    assert "命中 1 只" in page.hint_text, page.hint_text
    assert "甲样本(600001)" in page.hint_text


def test_preview_says_why_when_snapshot_is_unavailable(page, page_cfg, qapp,
                                                       monkeypatch) -> None:
    """取不到快照时，那句"市值/换手现在取不到"要**单独一行**说出来。

    不能混进"某只票算不出来"那一条 —— 那是逐票错误，混进去会被渲染成
    "1 只票算不出来，已跳过：⚠️ 市值/换手…"，票数是假的、原因也被张冠李戴。
    """
    from datetime import date, timedelta

    from laoa_trader.data import sources, storage

    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(10)]
    with storage.connect(page_cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600001", "甲样本", "银行")])
        storage.write_daily_raw(conn, [
            ("600001", day, 10.0, 10.2, 9.8, 10.0, 1e6, 1e7) for day in days])
        storage.write_calendar(conn, days)

    monkeypatch.setattr(sources, "snapshot_map", lambda _cfg, symbols=None: {})
    monkeypatch.setattr(sources, "supplement_map", lambda *a, **k: None)

    _open_editor(page)
    page.editor.setPlainText("流通市值>=10")
    page.on_preview()
    _wait_preview(page, qapp)

    assert "市值/换手" in page.hint_text
    assert "只票算不出来" not in page.hint_text      # 不许说成一个假的票数
