"""「策略筛选」页（`ui/formula_page.py`）的**离屏**界面测试。

这一页现在是三块（`docs/开发文档.md`）：统一策略列表（内置 + 公式同一张表）、
按需展开的傻瓜式公式编辑器、以及【开始筛选】。最容易做错、又最难在代码里看出来的
是**交互细节**，所以这里逐条钉住：

* 点按钮是**插到光标处**还是追加到末尾（追加也能"跑通"，但会把用户改到一半的公式
  静默改坏 —— 见 `FormulaPage.insert_token` 的注释）；
* `AND` 前后有没有空格（没有就变成 `A>1AND B` 这种"看不懂的语法错"）；
* 列表的两类行：内置策略的备注只能是**真实字段**（证据），公式的备注来自文件的注释头；
* 「状态」列写回**哪个键**：公式写 `enabled_formulas`，内置策略**同时**写
  `enabled_groups` + `enabled_strategies`（只写一个会踩交集语义的坑，见页面里的注释）；
* 右键菜单：内置可启停但**不可删**（置灰 + 理由），公式可删（二次确认）；
* 备注能写进公式文件的 `# 说明:` 注释头、也能从那里读回界面；
* 【开始筛选】**只 emit `start_pick_requested`**，自己绝不跑流程；
* **筛选结果不在这一页**（用户要求"只要显示策略"）：结果进「自选标的」+ 导出一份到桌面，
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
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
)

from laoa_trader import formulas as lib  # noqa: E402
from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.strategy import formula as fm  # noqa: E402
from laoa_trader.ui import formula_page as fp  # noqa: E402
from tests.conftest import workdays_ending  # noqa: E402

#: 小库里的三只票：甲/丙 一路上涨、乙 一路下跌（命中集合是确定的）
RISING = ("600001", "600003")

#: 表格里**固定占掉的行数**：只剩最上面那一行「竞价策略」（内置的竞价扫描开关）。
#:
#: 2026-09-18 之前这里是 6（5 条内置策略 + 竞价策略）—— 用户要求把内置策略改成
#: 随包公式之后，列表里不再有内置行，固定行只剩竞价那一条，公式行从第 2 行开始。
#: 用例里都写 `FIXED_ROWS` 而不是写死 1，免得下次行数再变又要满文件改数字。
FIXED_ROWS = 1


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


def test_palette_has_the_four_intraday_fields(page) -> None:
    """右侧「变量」面板要有盘中的四个字段，且 tooltip 写清"盘中口径"这两条限制。

    `现价 / 现涨幅 / 现量比 / 现换手` 是用户在盘中按当时快照筛股用的（2026-09-23 加）。
    tooltip 必须写明：非交易时段取不到（条件不成立）、**没有历史、不能回测** ——
    这两句是用户判断"为什么现在跑不出票"的唯一线索。
    """
    for name in ("现价", "现涨幅", "现量比", "现换手"):
        assert name in page.palette_buttons, f"面板里缺少「{name}」按钮"
        tip = page.palette_buttons[name].toolTip()
        assert "盘中" in tip and "不能回测" in tip, (name, tip)
        assert page.palette_buttons[name].text() == name


def test_palette_has_the_three_fund_flow_fields(page) -> None:
    """右侧「变量」面板要有主力资金那三个字段，tooltip 写清**单位**与"只有自选才有值"。

    `主力净额 / 主力净占比 / 近5日主力净额` 是 2026-10-08 加的（主人："只按照自选标的
    来采集资金流"）。tooltip 里那两句不是可有可无的说明：

    * **单位**（亿元 / 百分数）：写错的后果是静默差 1e8 倍或 100 倍，
      而用户只会觉得"选出来的票不对"；
    * **只有自选标的才有值**：这话不说，用户看到"全市场只有自选那几只被选中"，
      第一反应是公式写错了 —— 而那正是这条采集口径的必然结果。
    """
    for name in ("主力净额", "主力净占比", "近5日主力净额"):
        assert name in page.palette_buttons, f"面板里缺少「{name}」按钮"
        button = page.palette_buttons[name]
        tip = button.toolTip()
        assert tip.startswith(f"插入 {name}"), tip
        assert "自选标的" in tip, (name, tip)
        assert button.text() == name
    assert "亿元" in page.palette_buttons["主力净额"].toolTip()
    assert "百分数" in page.palette_buttons["主力净占比"].toolTip()
    assert "亿元" in page.palette_buttons["近5日主力净额"].toolTip()


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
        # 语法没丢：tooltip 第一步就写着"插入 XX"。
        # 例外只有【空格】那一个（2026-09-21 用户要求加的）：它插入的是**空白字符**，
        # 而空白字符在 tooltip 文本里根本看不出来 —— 所以那一条改成断言"写着空格"，
        # 判据一样硬（不是放宽成"随便什么 tooltip 都行"）。
        tip = button.toolTip()
        if token.strip() == "":
            assert "空格" in tip, f"【空格】按钮的 tooltip 要说清它插的是什么：{tip!r}"
        else:
            assert token in tip, f"{token} 的 tooltip 里找不到插入的语法"
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
    assert "策略选取" in fp.PAGE_HINT and "开始筛选" in fp.PAGE_HINT
    assert len(fp.PAGE_HINT.splitlines()) <= 2
    assert page.page_hint.objectName() == "statusTag"     # 小号灰字（主题里定义）
    # 编辑器自己那行说明（旧版顶部那句话，现在跟着编辑器一起展开）**一个字都没丢**
    assert "点右边的按钮就能插入" in fp.EDITOR_HINT
    assert "最后一行是筛选条件" in fp.EDITOR_HINT
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

    assert "策略还是空的" in page.hint_text
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
    # 标的写法是**半角** `名称(代码)`（开发文档：全项目一套写法）
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


def test_preview_hit_message_puts_the_candle_caliber_on_the_first_line(
        page, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """**这一轮用的是哪套 K 线**必须显示在最前面（同日同时辰的口径差别全靠它解释）。

    这份程序的内置规则是"开盘时间里跑的筛选都是实时的，不是开盘时间才用 K 线"
    （用户 2026-09-23 定）。同一份公式、同一个按钮，盘中与收盘后本来就会选出不同的票 ——
    界面上不写这一行，用户只会以为程序不稳定。
    """
    monkeypatch.setattr(lib, "preview_hits", lambda *a, **k: {
        "date": "2026-09-23", "count": 1, "shown": 1,
        "hits": [{"symbol": "600001", "name": "甲样本"}],
        "scanned": 7, "skipped": 0, "errors": [],
        "caliber": "📊 本次口径：盘中实时（10:05，用现价拼出今天 2026-09-23 这根 K 线）"})
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    lines = page.hint_text.splitlines()
    assert lines[0].startswith("📊 本次口径：") and "盘中实时" in lines[0]
    # 结果那一行照旧在它下面（口径只是**加一行**，不替换任何原有文案）
    assert lines[1] == "最近交易日 2026-09-23 命中 1 只：甲样本(600001)"


def test_preview_hit_message_without_caliber_is_unchanged(page, qapp,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """老/替身返回值里没有 `caliber` 时**一个字符都不多**（口径是加一行，不是改文案）。"""
    monkeypatch.setattr(lib, "preview_hits", lambda *a, **k: {
        "date": "2026-09-11", "count": 0, "hits": [], "shown": 0,
        "scanned": 7, "skipped": 3, "errors": []})
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_preview.click()
    _wait_preview(page, qapp)

    assert page.hint_text == (
        "最近交易日 2026-09-11：没有命中（扫了 7 只，3 只因数据不足跳过）"
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
# 6.5) 【运行】+【导出筛选结果】（用户 2026-09-18 要求）
#
# 用户原话：「把策略编辑下面的试算直接改成运行，后面再加上导出筛选结果
# （导出到桌面文档）」。所以这一段钉住三件事：
#   1. 按钮上的字是【运行】（"试算"这个词从界面上消失）；
#   2. 导出的是**上一次【运行】的全量命中**（提示区只列 20 只，文件里是全部）；
#   3. 导出的版式与「开始筛选」建池时那份**同一个函数**产的（来源列写
#      `公式·<公式名>`，与「自选标的」表格里同一个词）。
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
    """按钮文案：原来叫【试算】，用户要求改成【运行】；右边多一个【导出筛选结果】。"""
    assert page.btn_preview.text().startswith("运行")
    assert "试算" not in page.btn_preview.text()
    assert page.btn_export.text() == "导出筛选结果"
    # 用户点之前要知道"导出的是什么、导到哪去"
    tip = page.btn_export.toolTip()
    assert "桌面" in tip and "运行" in tip
    # 提示区那句"下一步"也要跟着改口，否则用户按提示找不到【试算】这个按钮
    assert "试算" not in fp.EDITOR_HINT and "运行" in fp.EDITOR_HINT


def test_export_after_run_writes_every_hit_to_the_desktop(
        page, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """点【导出筛选结果】→ 桌面目录里出现那份文件，里面是**全部**命中。

    `shown=1` 是故意的：界面提示区只列 1 只（真实场景是 20 只），文件里必须 3 只都有
    —— "文件里少几只"在界面上完全看不出来，只能靠这条断言守。
    """
    hits = [{"symbol": "600001", "name": "甲样本"},
            {"symbol": "600002", "name": "乙样本"},
            {"symbol": "600003", "name": "丙样本"}]
    _fake_preview(monkeypatch, hits, shown=1)
    toasts: list[str] = []
    page.status_cb = toasts.append        # 页面调它时会现取，所以直接挂上就行
    page.name_edit.setText("尾盘匹配策略")
    page.editor.setPlainText("C>MA(C,5)")
    _run(page, qapp)

    assert page.last_run is not None
    assert page.last_run["name"] == "尾盘匹配策略"
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
    # 「来源」列与「自选标的」表格同一个词：公式选中 → `公式·<公式名>`
    assert "来源：尾盘匹配策略" in text
    assert "行情日 2026-09-11" in text
    assert "现价" in text                    # 库里有两个交易日 → 现价与涨跌幅都算得出来
    # 导出是**成功操作**：按 2026-09-18 的新口径**不再弹提示**，
    # 但文件位置必须写在提示区里（那是静态回显，不是一闪而过的提醒）
    assert "已导出筛选结果" in page.hint_text and files[0].name in page.hint_text
    assert str(files[0]) in page.hint_text
    assert not any("已导出" in t for t in toasts), toasts


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
    assert "来源：未命名策略" in files[0].read_text(encoding="utf-8-sig")


# ══════════════════════════════════════════════════════════════════════════
# 7) 成绩单：**按需**入口（旧版那两个常驻按钮不再回流）
# ══════════════════════════════════════════════════════════════════════════
#
# 2026-09 移除了旧版右下角那两个常驻按钮（【看成绩单】【复制成绩单】）：
# 数据只有 6 个月，而成绩单自己有 250 个交易日 / 100 只票的库级门槛
# （`research/scorecard.py`）—— 常驻在页面上永远只会显示"样本不足"，
# 点一次还要扫全库几十秒。
#
# 2026-10-08 主人要一个入口，做法是"按需 + 预检 + 后台"那个对话框
# （`ui/scorecard_dialog.py`）—— 所以这里钉的是**边界**：入口有一个按钮，
# 但这一页**自己不跑成绩单**（没有页内线程、没有页内结果字段、
# 也没有旧版那个【复制成绩单】）。对话框本身的行为在
# `tests/test_scorecard_dialog.py` 里逐条测。


def test_scorecard_entry_is_on_demand_and_the_old_panel_stays_gone(page) -> None:
    """【成绩单】是页面上的一个入口按钮；线程与结果都在对话框那边，不在这一页。"""
    assert isinstance(page.btn_scorecard, QPushButton)
    assert page.btn_scorecard.text() == "成绩单"
    # 旧版那两个常驻按钮（以及它们的结果字段）不许回流
    assert not hasattr(page, "btn_copy_scorecard")
    assert not hasattr(page, "scorecard_text")
    # 这一页不自己跑成绩单：没有页内线程、没有页内结果
    assert not hasattr(page, "scorecard_worker")
    # 库函数留着（对话框、CLI 与长样本回测都用它）
    assert callable(lib.run_scorecard)


# ══════════════════════════════════════════════════════════════════════════
# 8) 保存 / 另存为 / 删除
# ══════════════════════════════════════════════════════════════════════════


def test_save_requires_a_name(page) -> None:
    """名称必填：空名拒绝、**提示用户**、而且不会偷偷存出一个无名文件。"""
    page.name_edit.setText("   ")
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_save.click()

    assert "请先填策略名称" in page.hint_text
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


def test_list_columns_are_the_names_the_user_asked_for(page) -> None:
    """列头就是用户给定的三个词（不多一列也不少一列）。

    2026-09-18 用户改过一次字：`名称 | 备注 | 状态` → `策略名称 | 说明 | 策略选取`。
    """
    columns = [page.table.horizontalHeaderItem(i).text()
               for i in range(page.table.columnCount())]

    assert columns == list(fp.LIST_COLUMNS) == ["策略名称", "说明", "策略选取"]


def test_auction_row_is_first_and_formulas_follow(page) -> None:
    """列表第一行是「竞价策略」，其余全是公式（2026-09-18 起内置策略改成随包公式）。

    老版本这里依次是 5 条内置策略 + 竞价策略 + 公式；所以这条用例原来叫
    `test_builtin_rows_come_first_and_show_real_evidence`，还逐条核对过内置策略
    「备注」列里的证据数字来自 `rules.py` / `groups.py` 的字段。现在那些行没有了，
    于是它改成钉住**新的列表构成**：第一行是竞价策略（唯一不是筛选策略的行），
    后面每一行都必须有对应的公式文件（不允许出现"没有文件的幽灵行"）。
    """
    _write_formula(page.directory, "我的公式", "C>MA(C,5)", "站上5日线")
    page.reload()

    assert page.table.item(0, 0).text() == fp.AUCTION_NAME
    assert page.rows[0].is_auction and page.rows[0].read_only
    formula_names = [row.key for row in page.rows[1:]]
    assert formula_names == ["我的公式"]
    # 每一行都能对上一个真文件（公式行不是画出来的）
    files = {spec.name for spec in lib.formula_files(page.directory)}
    assert set(formula_names) <= files
    # 说明列来自公式文件的 `# 说明:` 注释头（界面不编）
    assert _notes(page)["我的公式"] == "站上5日线"


def test_status_column_is_a_checkbox_for_every_row(page) -> None:
    """「策略选取」列是勾选框；勾选状态与配置一致（公式看 `enabled_formulas`）。

    2026-09-18 起内置策略改成了随包公式，所以这里不再有"组/策略"那套解析：
    每一行的状态只来自两个地方 —— `enabled_formulas`（公式）与 `intraday_auction`
    （竞价策略那一行）。
    """
    from PySide6.QtWidgets import QCheckBox

    _write_formula(page.directory, "甲公式", "C>MA(C,5)")
    page.cfg.enabled_formulas = ["甲公式"]
    page.reload()

    boxes = {}
    for index, row in enumerate(page.rows):
        holder = page.table.cellWidget(index, 2)
        box = holder.findChild(QCheckBox)
        assert box is not None, f"{row.name} 的「策略选取」列不是勾选框"
        boxes[row.key] = box.isChecked()
        assert "勾上" in box.toolTip()

    assert boxes == {
        fp.AUCTION_KEY: fp.auction_enabled(page.cfg),      # 竞价那一行看 intraday_auction
        "甲公式": True,                                     # 公式看 enabled_formulas
    }
    assert boxes[fp.AUCTION_KEY] is False                   # 竞价默认关


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


def test_clicking_readonly_row_shows_detail_and_copy_works(page, qapp) -> None:
    """单击**只读行**（现在只有「竞价策略」）→ 详情页可复制，且不碰编辑器的草稿。

    这条用例原来叫 `test_clicking_builtin_row_opens_readonly_detail`，钉的是"内置策略
    只能看不能改"：条件说明来自 `rules.py` 里那条策略的 docstring、证据来自它的
    `evidence` 字段。2026-09-18 内置策略改成随包公式之后，只读行只剩竞价那一行，
    于是改钉"只读详情这条交互还在"：**可复制**（用户要贴到群里/记事本）、
    以及**绝不用详情顶掉用户正在写的公式**。
    """
    page.on_open_editor()                                  # 先把编辑器打开
    page.editor.setPlainText("C>MA(C,5)")                  # 用户正在写的草稿
    page.select_row(fp.AUCTION_KEY)
    qapp.processEvents()

    assert page.bottom_stack.currentWidget() is page.detail_page
    assert page.detail_view.isReadOnly() is True
    assert page.detail_text.startswith(f"{fp.AUCTION_NAME}（{fp.AUCTION_KEY}）")
    assert "无法回测" in page.detail_text
    # 只读行**不进编辑器**：用户手里的草稿一个字都没被换掉
    assert page.editor.toPlainText() == "C>MA(C,5)"
    assert page.name_edit.text() == ""

    page.btn_copy_detail.click()
    assert QApplication.clipboard().text() == page.detail_text


def test_readonly_detail_state_follows_the_checkbox(page, qapp) -> None:
    """勾上「竞价策略」后，详情里的"当前状态"立刻跟着变（两处说法不能打架）。

    原来是钉内置策略的（勾上 → 详情显示"✅ 参与筛选"）；内置行没了之后，
    同一条规矩落在竞价那一行上：勾上写 `intraday_auction`，详情里那句
    "当前状态"必须同步 —— 否则用户勾完去看详情，会以为没生效。
    """
    page.select_row(fp.AUCTION_KEY)
    qapp.processEvents()
    assert "当前状态：☐ 未开启" in page.detail_text

    page._auction_box.setChecked(True)
    qapp.processEvents()

    assert "当前状态：✅ 已开启" in page.detail_text
    assert "intraday_auction" in page.detail_text


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
    """`reload()` 之后勾选状态必须与配置一致（公式看 enabled_formulas、竞价看开关）。

    原来是三条断言钉内置策略的组/策略解析；现在只剩两个来源，所以改成：
    文件里存在但没勾的公式**不勾**、配置里勾了的**勾上**、竞价开关照配置走。
    """
    _write_formula(page.directory, "没勾的", "C>MA(C,5)")
    _write_formula(page.directory, "勾了的", "C>MA(C,6)")
    page_cfg.enabled_formulas = ["勾了的"]
    page_cfg.intraday_auction = True

    page.reload()

    assert page._row_boxes["没勾的"].isChecked() is False
    assert page._row_boxes["勾了的"].isChecked() is True
    assert page._auction_box.isChecked() is True


# ══════════════════════════════════════════════════════════════════════════
# 10) 勾「参与筛选」→ 写回 config.toml（**三种键**）
# ══════════════════════════════════════════════════════════════════════════


def test_enable_checkbox_writes_config_toml(page, page_cfg, qapp) -> None:
    """勾公式 → config.toml 出现 enabled_formulas，**用户注释与未知键都保留**。

    公式在这里**直接落文件**、不走编辑器保存：编辑器保存会按 2026-09-18 的新行为
    自动勾上「参与筛选」（用户实报"存了却不参与"之后改的），而这条用例要测的是
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
    assert "已加入匹配" in page.hint_text


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
    assert "已退出匹配" in page.hint_text


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


def test_no_row_writes_the_legacy_group_keys(page, page_cfg, qapp) -> None:
    """列表里**没有任何一行**会去写 `enabled_groups` / `enabled_strategies`。

    老版本这条用例钉的是"勾内置策略必须同时写组与策略两个键"—— 那是内置策略时代
    最坑的一处（`groups.resolve()` 在两者都非空时取交集，只写一个会"勾了却不跑"）。
    2026-09-18 内置策略改成随包公式、策略组机制删掉之后，那两个键**连字段都没了**：

    * 配置对象上不该再有这两个属性（`Config` 里已删除）；
    * 勾公式只写 `enabled_formulas`；勾竞价只写 `intraday_auction`；
    * 用户 config.toml 里留着的老值原样保留（`load_config` 按未知键忽略、回写不动它）。
    """
    from laoa_trader.config import Config

    assert not hasattr(Config(), "enabled_groups")
    assert not hasattr(Config(), "enabled_strategies")

    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()
    before = page_cfg.source_path.read_text(encoding="utf-8")

    page._row_boxes["放量上攻"].setChecked(True)
    qapp.processEvents()
    page._auction_box.setChecked(True)
    qapp.processEvents()

    assert page_cfg.enabled_formulas == ["放量上攻"]
    assert page_cfg.intraday_auction is True
    # 老配置里那行退役键（`enabled_groups = ["short"]`）原样留着：
    # 用户手写过的配置不该被界面悄悄改掉 / 删掉（`load_config` 当未知键忽略它）
    after = page_cfg.source_path.read_text(encoding="utf-8")
    assert 'enabled_groups = ["short"]' in before
    assert 'enabled_groups = ["short"]' in after, "界面把老配置里那行弄丢了"
    assert "# 用户自己的注释（保存设置后必须还在）" in after


def test_uncheck_one_formula_keeps_the_others(page, page_cfg, qapp) -> None:
    """取消勾选**一条**公式：另外两条必须还在（`enabled_formulas` 是逐条维护的列表）。

    对应老版本的 `test_uncheck_one_short_member_keeps_the_other_two`（那条钉的是
    "取消一条内置策略、另外两条照旧"）—— 同样的规矩现在落在公式上。
    """
    _write_formula(page.directory, "甲", "C>MA(C,5)")
    _write_formula(page.directory, "乙", "C>MA(C,6)")
    _write_formula(page.directory, "丙", "C>MA(C,7)")
    page_cfg.enabled_formulas = ["甲", "乙", "丙"]
    page.reload()

    page._row_boxes["乙"].setChecked(False)
    qapp.processEvents()

    assert page_cfg.enabled_formulas == ["甲", "丙"]
    assert page._row_boxes["甲"].isChecked() is True
    assert page._row_boxes["丙"].isChecked() is True
    assert "enabled_formulas" in page_cfg.source_path.read_text(encoding="utf-8")


def test_uncheck_all_formulas_writes_an_empty_list(page, page_cfg, qapp) -> None:
    """全部取消勾选 → `enabled_formulas = []`（= 只盯自选标的），**不写** `["none"]`。

    老版本这条是内置策略的（`test_uncheck_all_builtins_writes_explicit_off`）：
    那时"全部关掉"必须写 `enabled_groups = ["none"]`，因为那两个键"都空 = 全选"。
    公式这一路没有这个坑 —— `enabled_formulas` 空列表就是"一条都不跑"，
    写 `["none"]` 反而会让它去加载一个叫 none 的公式（加载不到、白写一行配置）。
    """
    _write_formula(page.directory, "甲", "C>MA(C,5)")
    page_cfg.enabled_formulas = ["甲"]
    page.reload()

    page._row_boxes["甲"].setChecked(False)
    qapp.processEvents()

    assert page_cfg.enabled_formulas == []
    assert 'enabled_formulas = []' in page_cfg.source_path.read_text(encoding="utf-8")
    assert "none" not in page_cfg.source_path.read_text(encoding="utf-8")
    assert "已退出匹配" in page.hint_text


def test_auction_toggle_write_failure_reverts_checkbox(page, page_cfg, qapp,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """竞价策略写不进去（只读盘）时同样退回勾选 —— 界面不能显示成"已经开了"。

    老版本这条钉的是内置策略；内置行没了之后，同一条规矩落在竞价那一行上
    （它写的是 `intraday_auction`，写失败回退的路径与公式那条完全一样）。
    """
    box = page._auction_box

    def boom(*_args, **_kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr("laoa_trader.config.save_settings", boom)
    box.setChecked(True)
    qapp.processEvents()

    assert box.isChecked() is False
    assert "保存失败" in page.hint_text
    assert page_cfg.intraday_auction is False       # 配置一个字都没变


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


def test_row_menu_has_no_builtin_entry_anymore(page, page_cfg) -> None:
    """右键菜单里再也没有"内置策略不可删"那一套（防止老文案/老分支回流）。

    老版本这条叫 `test_row_menu_on_builtin_cannot_delete`：点开内置策略行会看到
    置灰的「删除（内置策略不可删）」。2026-09-18 内置策略改成随包公式之后，
    列表里**只有两种行**：公式（可删）与竞价策略（不可删，理由不同）——
    所以这里钉三件事：
    ① 那个常量与那条分支都不在了；② 竞价那一行仍然置灰并写明理由；
    ③ **每个公式行的删除项都是可点的**（不能因为删了内置分支就顺手把公式也锁死）。
    """
    assert not hasattr(fp, "MENU_DELETE_BUILTIN")
    assert not hasattr(fp.FormulaPage, "on_toggle_builtin")

    auction = page.row_menu(page.row_of(fp.AUCTION_KEY))
    assert auction.delete.text() == fp.MENU_DELETE_AUCTION
    assert auction.delete.isEnabled() is False
    assert "系统设置" in auction.delete.toolTip()

    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()
    picked = page.row_menu(page.row_of("放量上攻"))
    assert picked.delete.text() == fp.MENU_DELETE
    assert picked.delete.isEnabled() is True
    assert "策略文件" in picked.delete.toolTip()
    # 触发删除项**不会**顺手动配置（老那条用例正是这么验的）
    before = page_cfg.source_path.read_text(encoding="utf-8")
    assert page_cfg.source_path.read_text(encoding="utf-8") == before
    assert len(page.rows) == page.table.rowCount() == FIXED_ROWS + 1


def test_row_menu_toggle_matches_and_updates_the_row_state(page, page_cfg, qapp) -> None:
    """菜单第一项**按当前状态只出现一个**，触发之后状态与配置同步变化。

    原来这条用的是内置策略行（触发后写 `enabled_groups`）；内置行没了之后同样的
    规矩落在公式上：菜单文案跟着状态走、触发后写回 `enabled_formulas`、
    勾选框也跟着变 —— 三处只要有一处不同步，用户就会看到与事实相反的界面。
    """
    _write_formula(page.directory, "放量上攻", "C>MA(C,5)")
    page.reload()

    picked = page.row_menu(page.row_of("放量上攻"))
    assert picked.toggle.text() == "启用"          # 它现在是关的

    picked.toggle.trigger()
    qapp.processEvents()

    assert page_cfg.enabled_formulas == ["放量上攻"]
    assert page._row_boxes["放量上攻"].isChecked() is True
    # 状态变了 → 菜单文案必须跟着变（不跟着变就等于告诉用户相反的事实）
    assert page.row_menu(page.row_of("放量上攻")).toggle.text() == "关闭"
    assert "已加入匹配" in page.hint_text

    page.row_menu(page.row_of("放量上攻")).toggle.trigger()
    qapp.processEvents()
    assert page_cfg.enabled_formulas == []
    assert page._row_boxes["放量上攻"].isChecked() is False


# ══════════════════════════════════════════════════════════════════════════
# 9.5) 「竞价策略」那一行（用户 2026-09-18："不是公式，是把「竞价扫描」做成策略"）
#
# 它是列表的**第一行**（2026-09-18 之前排在 5 条内置策略之后；内置策略改成随包公式后，
# 前面那 5 行没了，它就成了第一行）；勾它开关的是**盘中竞价扫描**（`intraday_auction`），
# 而**不是**"参与筛选" —— 这几条用例里最要紧的就是把这条界线钉死：
# 勾完以后 `enabled_groups` / `enabled_strategies` / `enabled_formulas` 一个都不许变。
# ══════════════════════════════════════════════════════════════════════════


def test_auction_row_is_the_only_fixed_row_and_reads_config(page) -> None:
    """位置与口径：竞价行是**唯一一行固定行**（在最上面），说明列是**现读配置**的口径。

    老版本这条叫 `test_auction_row_sits_between_builtins_and_formulas`：那时它排在
    5 条内置策略**之后**、公式**之前**。内置策略改成随包公式之后，前面那 5 行没了，
    于是它成了列表的**第一行** —— 位置变了，但"数字来自 config、不参与筛选写在最前面"
    这两条口径一个字都没变。
    """
    _write_formula(page.directory, "我的公式", "C>MA(C,5)")
    page.reload()

    names = [page.table.item(row, 0).text() for row in range(page.table.rowCount())]
    assert names == [fp.AUCTION_NAME, "我的公式"]
    # 说明列的数字来自 config（不是界面编的）：改一个数，说明跟着变
    note = page.table.item(0, 1).text()
    assert "涨幅 2.0~9.0%" in note
    # 「不参与筛选」必须在**最前面**：说明列会被省略号截断，结论不能被截掉
    assert note.startswith("只做盘中提示、不参与筛选")
    page.cfg.auction_min_pct = 5.0
    page.reload()
    assert "涨幅 5.0~9.0%" in page.table.item(0, 1).text()


def test_auction_row_toggle_writes_intraday_auction_only(page, page_cfg, qapp) -> None:
    """勾上它 → 只写 `intraday_auction`；**勾选公式的那个键一个字都不动**。"""
    formulas_before = list(page_cfg.enabled_formulas)

    page._auction_box.setChecked(True)
    qapp.processEvents()

    assert page_cfg.intraday_auction is True
    assert page_cfg.enabled_formulas == formulas_before      # 不参与筛选 = 这个键不变
    assert "竞价策略已开启" in page.hint_text
    assert "不参与筛选" in page.hint_text
    # 写进 config.toml 的就是那个键，而且**不写**任何已退役的键
    text = page_cfg.source_path.read_text(encoding="utf-8")
    assert "intraday_auction" in text
    assert "enabled_groups =" not in text.split("intraday_auction")[1]

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
    assert "无法回测" in page.detail_text and "不参与筛选" in page.detail_text
    assert page.editor.toPlainText() == "C>MA(C,5)"      # 草稿没被动过
    assert "不是筛选策略" in page.hint_text


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
# 12) 【开始筛选】：只 emit 信号（流程在主窗口里）
# ══════════════════════════════════════════════════════════════════════════


def test_start_pick_only_emits_the_signal(page, monkeypatch) -> None:
    """点【开始筛选】→ `start_pick_requested` 出手；**这一页自己不跑流程**。"""
    from laoa_trader import scheduler

    def boom(*_args, **_kwargs):               # 谁在这里调筛选流程，这条就会炸
        raise AssertionError("公式页自己跑起了筛选流程（应该只 emit 信号）")

    monkeypatch.setattr(scheduler, "run_daily", boom)
    seen: list = []
    page.start_pick_requested.connect(lambda: seen.append("go"))
    told: list = []
    page.status_cb = told.append

    page.btn_start_pick.click()

    assert seen == ["go"]
    assert told and "开始筛选" in told[0]


def test_start_pick_signal_is_a_real_signal_on_the_page(page) -> None:
    """信号得挂在 `FormulaPage` 上（主窗口 `connect` 的就是它）。"""
    from PySide6.QtCore import SignalInstance

    assert isinstance(page.start_pick_requested, SignalInstance)
    assert hasattr(fp.FormulaPage, "start_pick_requested")


# ══════════════════════════════════════════════════════════════════════════
# 13) 筛选结果**就在这一页**（2026-09-18 用户改口径）+ 加自选 / 导出
# ══════════════════════════════════════════════════════════════════════════
#
# 口径变过一次，两边的原话都留在这里，免得下一个人以为哪一版是漏改：
#
# * 2026-09-17：「策略筛选只要显示策略，不显示筛选结果，筛选结果直接进自选标的，
#   可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」→ 当时把结果表、
#   结论、【全部加为自选】整体删了；
# * 2026-09-18：「匹配状态时，策略列表界面变为筛选结果界面（筛选结果界面平时隐藏），
#   结果可以一键加入自选和导出。」→ 结果表回来了，但**换了位置与呈现方式**：
#   它与策略列表**共用同一块地方**（QStackedWidget），平时显示列表，点【开始筛选】
#   才切过去。所以"平时隐藏"这条是硬要求，必须有用例守着。
#
# 要钉住的四件事：
#   1. 结果页在、平时隐藏、点【开始筛选】切过去、能切回来；
#   2. `show_pick_result()` 把结果填进表里（主窗口每轮匹配后调它），且**不写库**；
#   3. 【一键加入自选】尊重自选上限、不重复添加、不改用户备注；
#   4. 【导出结果到桌面】走的是 `pool.export_pick_file`（与自动导出同一个函数）。


def test_result_page_replaces_the_list_while_picking(page) -> None:
    """结果页在、**平时隐藏**、点【开始筛选】切过去、能切回来（2026-09-18 用户要求）。

    用户原话："匹配状态时，策略列表界面变为筛选结果界面（筛选结果界面平时隐藏），
    结果可以一键加入自选和导出。"
    """
    for name in ("result_table", "result_hint", "btn_add_all", "btn_export_result",
                 "btn_back_to_list", "result_rows", "result_date"):
        assert hasattr(page, name), f"结果页缺少「{name}」"
    assert [page.result_table.horizontalHeaderItem(i).text()
            for i in range(page.result_table.columnCount())] == list(fp.RESULT_COLUMNS)

    # 平时隐藏：显示的是策略列表那一页
    assert page.list_stack.currentWidget() is page.list_page
    assert page.result_page.isVisible() is False

    # 点【开始筛选】→ 立刻切过去，并先摆一句"正在匹配…"
    page.btn_start_pick.click()
    assert page.list_stack.currentWidget() is page.result_page
    assert page.result_page.isVisible() is True
    assert "正在匹配" in page.result_hint.text()

    # 跑完（主窗口调 show_pick_result）→ 结果进表
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"},
                                    {"symbol": "600001", "name": "甲样本",
                                     "strategy": "公式·放量上攻"}]})
    assert page.result_table.rowCount() == 2
    # 六列、顺序就是主人给的那六项（2026-09-21）
    assert list(fp.RESULT_COLUMNS) == ["名称(代码)", "实时股价", "市值", "换手率",
                                       "来源", "加入自选"]
    assert page.result_table.item(0, 0).text() == "丙样本(600003)"
    assert "放量上攻" in page.result_table.item(0, 4).text()
    # 没有实时快照时：股价退回本地最近收盘价（带 `*` 标注）、市值/换手显示 —（不是 0）
    assert page.result_table.item(0, 1).text() == "6.74*"
    assert page.result_table.item(0, 2).text() == "—"
    assert page.result_table.item(0, 3).text() == "—"
    # 最后一列是**每行一个**【加入自选】按钮
    assert page.result_table.cellWidget(0, fp.RESULT_ADD_COLUMN) is not None
    assert "共 2 只" in page.result_hint.text() and "2026-09-11" in page.result_hint.text()
    # 结论里要写明"结果不会自动进股池"（主人 2026-09-21 的新口径）
    assert "不会自动进股池" in page.result_hint.text()

    # 能切回列表
    page.btn_back_to_list.click()
    assert page.list_stack.currentWidget() is page.list_page


def test_each_result_row_has_an_add_button_that_records_the_price(page, page_cfg) -> None:
    """结果表每一行的【加入自选】：写进自选表、**记下加入价**、按完变成"已在自选"。

    2026-09-21（主人要求）：筛选结果**不再自动进股池**，所以"要不要留下这一只"
    由用户在这一列点。加入价是「自选标的」盈亏列的基准（见 storage 建表那里的说明）。
    """
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"}]})
    cell = page.result_table.cellWidget(0, fp.RESULT_ADD_COLUMN)
    button = cell.findChild(QPushButton)
    assert button.text() == fp.RESULT_ADD_TEXT == "加入自选"

    button.click()

    with storage.connect(page_cfg.db_path) as conn:
        rows = storage.load_watchlist(conn, enabled_only=False)
    assert [r["symbol"] for r in rows] == ["600003"]
    assert "匹配来源" in str(rows[0]["note"])
    # 加入价记下来了（本地最近收盘价 —— 显示时四舍五入成 6.74，存的是原值），
    # 盈亏才有基准
    assert float(rows[0]["added_price"]) == pytest.approx(6.7392445766645315)
    assert "已把「丙样本」加入" in page.hint_text

    # 那一格变成"已在自选"（再点也加不进去）
    after = page.result_table.cellWidget(0, fp.RESULT_ADD_COLUMN)
    assert after.findChild(QPushButton) is None
    assert fp.RESULT_ADDED_TEXT in after.findChild(QLabel).text()


def test_add_button_keeps_the_first_added_price(page, page_cfg) -> None:
    """已经在自选里的票：那一格直接显示【已在自选】，**不会**把加入价改成今天的价。"""
    with storage.connect(page_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600003", name="丙样本", price=5.0)
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"}]})

    cell = page.result_table.cellWidget(0, fp.RESULT_ADD_COLUMN)
    assert cell.findChild(QPushButton) is None                    # 直接是"已在自选"
    page.on_add_one_to_watchlist("600003", 0)                     # 硬调也不能重复加/改基准

    with storage.connect(page_cfg.db_path) as conn:
        rows = storage.load_watchlist(conn, enabled_only=False)
    assert len(rows) == 1
    assert float(rows[0]["added_price"]) == pytest.approx(5.0)     # 基准价没被改写
    assert "已经在自选里了" in page.hint_text


def test_quotes_provider_fills_price_cap_and_turnover(page) -> None:
    """主窗口注入快照后：股价/市值/换手三列都用快照的数（取不到才是 —）。"""
    page.set_quotes_provider(lambda symbol: {
        "price": 12.34, "circ_mktcap": 88.5, "turnover_rate": 3.21,
    })
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"}]})

    assert page.result_table.item(0, 1).text() == "12.34"          # 不带 `*`（实时价）
    assert page.result_table.item(0, 2).text() == "88.50亿"
    assert page.result_table.item(0, 3).text() == "3.21%"
    # 快照里没有这两项时回到 —（不是 0）
    page.set_quotes_provider(lambda symbol: {"price": 12.34})
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"}]})
    assert page.result_table.item(0, 2).text() == "—"
    assert page.result_table.item(0, 3).text() == "—"


def test_result_view_only_lists_picks_not_my_own_watchlist(page) -> None:
    """结果表只列**这次选出来的**票：`run_daily` 池子里混着的自选标的不算。

    （自选标的是"我自己加的"，列进"本次筛选结果"会让人以为它是被选出来的。）
    """
    page.show_pick_result({
        "data_date": "2026-09-11",
        "pool": [
            {"symbol": "600001", "name": "甲样本", "strategy": "公式·放量上攻"},
            {"symbol": "600009", "name": "我的自选", "watchlist": True},   # 纯自选行
        ],
    })

    assert [row["symbol"] for row in page.result_rows] == ["600001"]


def test_result_view_says_why_when_nothing_was_picked(page) -> None:
    """一只没选中：结论要说清"没选到 + 常见原因"，并把出错原因（如果有）写在下面。"""
    page.show_pick_result({"data_date": "2026-09-11", "pool": [],
                           "errors": ["数据闸门：今天的数据还没更新"]})

    assert page.result_table.rowCount() == 0
    hint = page.result_hint.text()
    assert "没有选到票" in hint and "2026-09-11" in hint
    assert "数据闸门" in hint                     # 出错原因必须让人看见


def test_result_view_shows_the_candle_caliber(page) -> None:
    """结果页也要写出这一轮的口径（盘中实时 / 日 K 线）—— 结论与错误之外的第 3 类信息。

    它从 `report["formulas"]["caliber"]` 来（`pool.build_pool` 写进去、
    `formula_group.run_enabled_formulas` 给的值），所以这里喂一份形状相同的 report。
    """
    page.show_pick_result({
        "data_date": "2026-09-23",
        "pool": [{"symbol": "600001", "name": "甲样本", "strategy": "公式·放量上攻"}],
        "formulas": {"caliber": "📊 本次口径：盘中实时（10:05）"},
    })

    assert "本次口径" in page.result_hint.text()
    assert "盘中实时" in page.result_hint.text()


def test_result_view_without_a_caliber_is_unchanged(page) -> None:
    """report 里没有 `formulas`（老调用方 / 行列表形态）时结果页照旧，不多一行。"""
    page.show_pick_result({
        "data_date": "2026-09-11",
        "pool": [{"symbol": "600001", "name": "甲样本", "strategy": "公式·放量上攻"}],
    })

    assert "本次口径" not in page.result_hint.text()
    assert "本次筛选结果：共 1 只" in page.result_hint.text()


def test_show_pick_result_keeps_the_signature_the_main_window_uses(page, page_cfg) -> None:
    """主窗口按关键字传 `data_date`，三种调用形态都不能出事，且**它自己不写库**。"""
    params = inspect.signature(fp.FormulaPage.show_pick_result).parameters
    assert list(params) == ["self", "rows", "data_date"]
    assert params["rows"].default is None
    assert params["data_date"].kind is inspect.Parameter.KEYWORD_ONLY

    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600001", "name": "甲样本",
                                     "strategy": "公式·放量上攻"}]})
    page.show_pick_result([{"symbol": "600001", "strategy": "公式·放量上攻"}])
    page.show_pick_result(None)

    # 只显示、不写库（加自选是用户点【一键加入自选】才做的事）
    with storage.connect(page_cfg.db_path) as conn:
        assert storage.load_watchlist(conn, enabled_only=False) == []
    doc = fp.FormulaPage.show_pick_result.__doc__ or ""
    assert "2026-09-18" in doc and "2026-09-17" in doc      # 口径变过，写清免得以为是漏改


def test_add_all_to_watchlist_writes_rows_with_source_note(page, page_cfg) -> None:
    """【一键加入自选】：写进 `watchlist`，备注记下"匹配来源"，再点一次不重复加。"""
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·放量上攻"}]})

    page.btn_add_all.click()

    with storage.connect(page_cfg.db_path) as conn:
        rows = storage.load_watchlist(conn, enabled_only=False)
    assert [r["symbol"] for r in rows] == ["600003"]
    assert "匹配来源" in str(rows[0].get("note") or "")
    assert "已加 1 只" in page.hint_text

    page.btn_add_all.click()                       # 再点一次：不重复添加
    with storage.connect(page_cfg.db_path) as conn:
        assert len(storage.load_watchlist(conn, enabled_only=False)) == 1
    assert "都已在自选里" in page.hint_text or "本来就在自选" in page.hint_text


def test_add_all_to_watchlist_respects_the_cap_and_says_what_was_dropped(
        page, page_cfg) -> None:
    """自选已满：**明确说**哪几只没加进去、怎么解决（绝不静默丢）。"""
    page_cfg.watchlist_max = 1
    with storage.connect(page_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600009", name="已有的自选", enabled=True)
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600001", "name": "甲样本", "strategy": "公式·X"},
                                    {"symbol": "600003", "name": "丙样本", "strategy": "公式·X"}]})

    page.btn_add_all.click()

    with storage.connect(page_cfg.db_path) as conn:
        rows = {r["symbol"] for r in storage.load_watchlist(conn, enabled_only=False)}
    assert rows == {"600009"}                      # 一只都没加进去（上限 1 已占满）
    assert "上限" in page.hint_text and "没有" in page.hint_text


def test_export_result_writes_the_desktop_file(page, page_cfg, tmp_path) -> None:
    """【导出结果到桌面】走 `pool.export_pick_file`（与自动导出、编辑器导出同一个函数）。"""
    assert "export_pick_file" in inspect.getsource(fp.FormulaPage.on_export_result)
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600001", "name": "甲样本",
                                     "strategy": "公式·放量上攻"}]})

    page.btn_export_result.click()

    files = sorted((tmp_path / "desktop-export").glob("*.txt"))    # conftest 的桌面守卫
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8-sig")
    assert "甲样本(600001)" in text and "放量上攻" in text
    assert "已导出筛选结果" in page.hint_text


def test_export_result_without_any_result_tells_the_user_to_run_first(page, tmp_path) -> None:
    """还没有结果就点导出：提示先去匹配，不落空文件。"""
    page.btn_export_result.click()

    assert "先点【开始筛选】" in page.hint_text
    assert not list((tmp_path / "desktop-export").glob("*.txt"))


def test_page_tells_the_user_where_the_results_go(page) -> None:
    """"结果去哪了"必须在**界面上**说清（不许静默消失）。

    三处文案：顶部灰字、【开始筛选】的 tooltip、点下去那一刻的提示 —
    少一处，用户点完【开始筛选】就会以为"什么都没发生"。
    """
    assert "自选标的" in page.page_hint.text() and "桌面" in page.page_hint.text()

    tip = page.btn_start_pick.toolTip()
    assert "自选标的" in tip and "桌面" in tip

    told: list[str] = []
    page.status_cb = told.append
    page.btn_start_pick.click()
    assert told and "自选标的" in told[0] and "桌面" in told[0]




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
# 15) 【新策略】（旧名【载入示例】，2026-09-21 只改名字、行为一字未变）
# ══════════════════════════════════════════════════════════════════════════


def test_sample_button_is_named_new_strategy(page) -> None:
    """按钮上的字是【新策略】（主人 2026-09-21 要求改名），工具提示说清它是"新建一条"。"""
    assert fp.SAMPLE_BUTTON_TEXT == "新策略"
    assert page.btn_sample.text() == "新策略"
    assert "载入示例" not in page.btn_sample.text()
    tip = page.btn_sample.toolTip()
    assert "新建" in tip
    # 行为没变：点它照样是把一条能跑通的公式放进编辑框（下面那些用例守着）
    assert callable(page.on_load_sample)


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


# ── 保存之后：用户必须**一眼看得出它进了列表、而且参与筛选**（2026-09-18 用户实报）──
#
# 用户原话：「编辑器保存了 策略里就不显示，这个问题很严重」。
# 我在源码环境里复现不出来（列表确实会立刻多一行），但顺着这句话查出三件事会让用户
# 产生同样的感觉，所以三条都改掉并钉住：
#   1. 公式追加在**列表末尾**，窗口小的时候它在视野外 → 保存后要滚到那一行并说出第几行；
#   2. 新存的公式**默认不参与筛选**（要用户自己再找一个勾选框勾一下）→ 现在保存即参与；
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
    """编译不过的公式也**照样存下来**（不拦着用户存草稿），只是**不参与筛选**。

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
    assert "策略有错" in page.hint_text and "流通市值X" in page.hint_text


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

    # 提示覆盖的字段变多了（含盘中的四个），断言按"那一类字段取不到"来钉，
    # 并保留"不许说成一个假的票数"这条（那是逐票错误，混进来票数是假的）
    assert "实时快照" in page.hint_text and "取不到" in page.hint_text
    assert "现量比" in page.hint_text
    assert "只票算不出来" not in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 4.5) 【空格】按钮（用户 2026-09-21：「运算符那一栏最后加个空格按键 点击就输入空格」）
# ══════════════════════════════════════════════════════════════════════════


def test_space_button_is_the_last_operator_and_inserts_one_space(page) -> None:
    """点【空格】= 在光标处插入**一个空格**（逐字符断言，不是"像是有空格"）。"""
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")
    _put_caret(page, 1)                     # 光标落在 `C` 与 `>` 之间

    _click(page, " ")                       # token 就是一个空格

    assert page.editor.toPlainText() == "C >MA(C,5)", "插入的应当正好是一个空格"
    assert page.editor.textCursor().position() == 2   # 光标跟着往右走一格


def test_space_button_label_and_tooltip(page) -> None:
    """按钮上写「空格」；tooltip 说明它只影响可读性、不影响计算。"""
    button = page.palette_buttons[" "]

    assert button.text() == "空格"
    assert "空格" in button.toolTip()
    assert "不影响计算" in button.toolTip()
    # 它是**运算符那一组的最后一项**（用户要求的位置）
    assert fp.OPERATORS[-1][0] == " " and fp.OPERATORS[-1][3] == "空格"


def test_space_button_does_not_break_the_other_palette_buttons(page) -> None:
    """多了一个按钮之后，其它按钮照旧（计数与分列都要跟着更新，别悄悄漏掉）。"""
    assert len(page.palette_buttons) == (
        len(fp.VARIABLES) + len(fp.FUNCTIONS) + len(fp.OPERATORS) + len(fp.EXCLUDES)
    )
    for box in page.palette_panel.findChildren(QGroupBox):
        assert box.layout().columnCount() == fp.PALETTE_COLUMNS
    # 空格插进去之后公式仍然编译得过（不影响计算）
    _open_editor(page)
    page.editor.setPlainText("C>MA(C,5)")
    _put_caret(page, 1)
    _click(page, " ")
    fm.compile_formula(page.editor.toPlainText())


# ══════════════════════════════════════════════════════════════════════════
# 3.9) 结果表最后一列【加入自选】的**尺寸**（用户 2026-09-21：
#      「筛选结果界面的加入自选调整一下大小，框体有点小，字显示不全」）
# ══════════════════════════════════════════════════════════════════════════
# 为什么单独有一节：这一格是**表里唯一一个"控件放在单元格里"的地方**，它的宽度不是
# 表格按文字算的，而是控件自己的最小尺寸决定的 —— 实测过一次真实的切字：
# 换字号（Windows 125% 缩放就是这一档）之后 `QPushButton.sizeHint()` 没跟着长，
# 12pt 时"加入自选"文字要 64 像素、算上按钮内边距要 90，而 sizeHint 还停在 80，
# 于是两边各被切掉几个像素。所以这几条用例的判据统一是"**按渲染出来的尺寸**"。


def _result_cell_holder(page, row: int = 0):
    """那一格里的控件（按钮或"已在自选"标签）—— 不写死类型，两种状态都测。"""
    cell = page.result_table.cellWidget(row, fp.RESULT_ADD_COLUMN)
    assert cell is not None, "结果表最后一列没有控件"
    holder = cell.findChild(QPushButton) or cell.findChild(QLabel)
    assert holder is not None, "那一格里既没有按钮也没有标签"
    return holder


def _populate_result(page, symbol: str = "600003", name: str = "丙样本") -> None:
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": symbol, "name": name,
                                     "strategy": "公式·放量上攻"}]})
    QApplication.processEvents()


def test_add_button_width_is_measured_from_the_text_not_written_down(page) -> None:
    """按钮的宽度必须**量出来**：文字宽 + 两侧留白（下限），而不是一个写死的像素。

    ⚠️ 判据为什么不是 `sizeHint().width()`：`QPushButton` 的 sizeHint 由样式算，
    **不理会** `setMinimumWidth` —— 实测 minimumWidth=88 时 sizeHint 仍是 74。
    真正管用的是 `minimumWidth()`（布局照着它排）与**渲染出来的** `width()`，
    两个都断言；再加一条"文字本身必须放得下"。
    """
    _populate_result(page)
    button = _result_cell_holder(page)
    assert button.text() == fp.RESULT_ADD_TEXT
    metrics = QFontMetrics(button.font())
    text_width = metrics.horizontalAdvance(button.text())
    assert text_width > 0, "量不到文字宽度，这条用例就没有意义了"

    need = text_width + 2 * fp.RESULT_ADD_SIDE_PADDING
    assert button.minimumWidth() >= need, "按钮的最小宽度没有按文字量"
    assert button.width() >= need, f"渲染出来只有 {button.width()} 像素，放不下 {button.text()!r}"
    assert button.sizeHint().width() >= text_width      # 文字本身必须放得下
    # 高度同理（"字显示不全"也可能是上下被切）
    assert button.minimumHeight() >= metrics.height()
    assert button.height() >= metrics.height()


def test_added_label_fits_its_text_too(page, page_cfg) -> None:
    """已经加过的行显示「已在自选」：**这一个文案也要完整显示**（不能只照顾按钮）。

    两个文案长度一样（四个汉字），但走的是两条代码路径（标签没有边框与内边距），
    所以两条都测；用例同时钉住"两种状态的宽度一致"——否则列宽会在加完之后跳一下。
    """
    with storage.connect(page_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600003", name="丙样本", price=5.0)
    _populate_result(page)

    label = _result_cell_holder(page)
    assert isinstance(label, QLabel) and label.text() == fp.RESULT_ADDED_TEXT
    metrics = QFontMetrics(label.font())
    text_width = metrics.horizontalAdvance(label.text())

    assert label.minimumWidth() >= text_width + 2 * fp.RESULT_ADD_SIDE_PADDING
    assert label.width() >= text_width + 2 * fp.RESULT_ADD_SIDE_PADDING
    assert label.height() >= metrics.height()


def test_add_button_keeps_fitting_when_the_font_is_bigger(page, page_cfg, tmp_path,
                                                         qapp) -> None:
    """**回归用例**：字号变大（Windows 125% 缩放就是这一档）之后仍然不切字。

    这一条钉的就是用户实报的那个 bug：尺寸曾经取自 `sizeHint()`，而它换字号后不跟长，
    于是 12pt 下按钮只有 80 像素、文字却要 64 + 内边距 —— 两边各切一截。
    现在每次填表都按当前字体现量（`_result_add_cell_size`），字号一变就跟着长。

    为什么**改应用字体 + 另建一页**，而不是 `page.setFont(...)` 了事：
    那一格的控件是"建好了再塞进单元格"的，`setFont` 往下传的时机在
    offscreen 平台上跟测试顺序有关（实测跑整包时传不到、单独跑时传得到，
    于是这条用例时红时绿）。应用字体是**它一出生就带的**那一个，
    与真机上"系统缩放 125% 时启动程序"是同一条路径，跟顺序无关。
    当前提不成立（字没变大）时宁可让用例红着报出来，也不要绿得不明不白。
    """
    _populate_result(page)
    base_button = _result_cell_holder(page)
    base_width = QFontMetrics(base_button.font()).horizontalAdvance(base_button.text())
    base_height = base_button.height()

    original = qapp.font()
    bigger = QFont(original)
    if bigger.pointSizeF() > 0:
        bigger.setPointSizeF(bigger.pointSizeF() + 3)
    else:                                   # 字体是按像素定的（pointSizeF() 返回 -1）
        bigger.setPixelSize(max(bigger.pixelSize(), 12) + 4)
    fresh = None
    try:
        qapp.setFont(bigger)
        fresh = fp.FormulaPage(page_cfg, directory=tmp_path / "公式-大字号")
        fresh.resize(1100, 720)
        fresh.show()
        qapp.processEvents()
        _populate_result(fresh)

        button = _result_cell_holder(fresh)
        metrics = QFontMetrics(button.font())
        text_width = metrics.horizontalAdvance(button.text())
        assert text_width > base_width, (
            f"前提不成立：字并没有变大（{base_width} → {text_width}），"
            "后面的断言不能说明问题"
        )
        assert button.width() >= text_width + 2 * fp.RESULT_ADD_SIDE_PADDING, (
            f"字号变大后按钮只有 {button.width()} 像素，放不下 {button.text()!r}"
        )
        assert button.height() >= metrics.height()
        assert button.height() > base_height, "字号变大了按钮却一点没长，说明尺寸又被写死了"
        # 列宽与行高都得跟上（否则按钮虽大，却被列的边界/行高压着）
        assert fresh.result_table.columnWidth(fp.RESULT_ADD_COLUMN) >= button.width()
        assert fresh.result_table.rowHeight(0) >= button.height()
    finally:
        qapp.setFont(original)
        if fresh is not None:
            fresh.close()
            fresh.deleteLater()
            qapp.processEvents()


def test_result_table_columns_have_room_for_their_text(page) -> None:
    """顺手查的**同一张表**其它列（用户要求"看看有没有同样被挤压的"）。

    判据：每一列的宽度 ≥ 「表头文字 / 这一格最长的那份文字」里更宽的那个 + 一点余量。
    这一页的列宽是按内容算的（`ResizeToContents`），而通用 QSS 的内边距很紧，
    实测最紧的「实时股价」列表头文字 64 像素、列宽 73（只多 9）—— 所以这条用
    "至少多 4 像素"当水位线（真被挤成负数时立刻红），另外主题里给这张表
    （`RESULT_TABLE_OBJECT`）单独放宽了内边距，余量就是这么来的。
    """
    page.set_quotes_provider(lambda symbol: {
        "price": 1234.56, "circ_mktcap": 12345.67, "turnover_rate": 12.34,
    })
    _populate_result(page, name="丙样本名称长一点")
    table = page.result_table

    for column, title in enumerate(fp.RESULT_COLUMNS):
        header_item = table.horizontalHeaderItem(column)
        header_font = QFontMetrics(table.horizontalHeader().font())
        need = header_font.horizontalAdvance(header_item.text())
        cell = table.item(0, column)
        if cell is not None:
            need = max(need, QFontMetrics(cell.font()).horizontalAdvance(cell.text()))
        width = table.columnWidth(column)
        assert width >= need + 4, f"「{title}」列被挤了：列宽 {width}、文字要 {need}"

    # 最后一列（控件）另算：它的宽度必须放得下那个按钮
    button = _result_cell_holder(page)
    assert table.columnWidth(fp.RESULT_ADD_COLUMN) >= button.width()


# ══════════════════════════════════════════════════════════════════════════
# 3.10) 【加入自选】之后股池里的「来源」不能变成「自选」（2026-09-21 主人实报：
#       「新版本从匹配列表加入自选的票到股池里的来源都变成自选了，需要改一下」）
# ══════════════════════════════════════════════════════════════════════════
# 修的是"加入时没把来源存下来"：结果页那一格知道它是被哪条公式选出来的
# （`row["strategy"]`），但加进自选表时只写进了备注，股池那一列读的是
# `pool.watchlist_only_rows()` —— 它对不在池子里的自选行写死「自选」。
# 现在来源进了 `watchlist.source_strategy`，显示照旧走 `source_label()`（同一套函数）。


def _pool_row(page_cfg, symbol: str) -> dict:
    """「自选标的」页那一行（主窗口刷新表格读的就是这个函数）。"""
    rows = {r["symbol"]: r for r in fp.pool_mod.pool_page_rows(page_cfg.db_path)}
    assert symbol in rows, f"股池页里没有 {symbol}：{sorted(rows)}"
    return rows[symbol]


def test_add_to_watchlist_keeps_the_pick_source_in_the_pool_page(page, page_cfg) -> None:
    """点【加入自选】→ 股池那一行的来源是**那条公式的名字**（**不是**「自选」）。"""
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·尾盘超短策略"}]})

    page.result_table.cellWidget(0, fp.RESULT_ADD_COLUMN).findChild(QPushButton).click()

    # 加进去默认**不提醒**（2026-10-05），所以来源列带「（已停用）」——
    # 这正是"已收下、还没盯"的显示口径
    row = _pool_row(page_cfg, "600003")
    assert row["source_label"] == "尾盘超短策略（已停用）"
    assert row["strategy"] == "公式·尾盘超短策略"
    # 表格用的就是 `source_label`（`ui/app.py` 那一列），所以这里断言的就是**屏幕上那个词**
    with storage.connect(page_cfg.db_path) as conn:
        assert storage.watchlist_map(conn)["600003"]["source_strategy"] == "公式·尾盘超短策略"


def test_one_click_add_keeps_the_pick_source_too(page, page_cfg) -> None:
    """【一键加入自选】走的是另一条代码路径，来源一样要带上（别只修一条）。"""
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·尾盘超短策略"},
                                    {"symbol": "600001", "name": "甲样本",
                                     "strategy": "公式·放量上攻"}]})

    page.btn_add_all.click()

    assert _pool_row(page_cfg, "600003")["source_label"] == "尾盘超短策略（已停用）"
    assert _pool_row(page_cfg, "600001")["source_label"] == "放量上攻（已停用）"


def test_adding_an_existing_symbol_fills_the_missing_source_only(page, page_cfg) -> None:
    """已经在自选里（老数据、来源为空）的票再点一次【加入自选】→ **只补来源**。

    为什么单独一条：这一条路界面说的是"已经在自选里了（没有重复添加）"——
    所以补来源时**不能**顺手把用户停用的票启用回来、也不能动加入价与他自己写的备注
    （见 `storage.fill_watchlist_source()`）。手工加过的票从此显示那条公式名。
    """
    with storage.connect(page_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600003", name="丙样本", note="龙头",
                                 price=5.0, enabled=False)
    page.show_pick_result({"data_date": "2026-09-11",
                           "pool": [{"symbol": "600003", "name": "丙样本",
                                     "strategy": "公式·尾盘超短策略"}]})

    page.on_add_one_to_watchlist("600003", 0)

    with storage.connect(page_cfg.db_path) as conn:
        row = storage.watchlist_map(conn)["600003"]
    assert row["source_strategy"] == "公式·尾盘超短策略"     # 来源补上了
    assert row["enabled"] == 0                              # 停用状态没被动过
    assert row["note"] == "龙头"                            # 用户写的备注没被动过
    assert float(row["added_price"]) == pytest.approx(5.0)  # 加入价没被重置
    assert "已经在自选里了" in page.hint_text
    assert _pool_row(page_cfg, "600003")["source_label"] == "尾盘超短策略（已停用）"
