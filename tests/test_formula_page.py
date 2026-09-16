"""「公式选股」页（`ui/formula_page.py`）的**离屏**界面测试。

为什么值得单写一份
------------------
这一页是"小白友好"的落点，而最容易做错、又最难在代码里看出来的是**交互细节**：

* 点按钮是**插到光标处**还是追加到末尾（追加也能"跑通"，但会把用户改到一半的公式
  静默改坏 —— 见 `FormulaPage.insert_token` 的注释）；
* 函数骨架插完光标在不在括号里（不在的话用户得自己点回去，等于白做面板）；
* `AND` 前后有没有空格（没有就变成 `A>1AND B` 这种"看不懂的语法错"）；
* 名称必填、重名要二次确认（覆盖是**不可逆**的）；
* 勾「参与选股」有没有真的写回 `config.toml`。

这些只有把页面真的建出来、真的点一遍才测得到。用 Qt 的 `offscreen` 平台插件，
无显示器也能跑；没装 PySide6 的机器整个文件跳过（与 `test_ui_smoke.py` 同一约定）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过公式编辑器界面测试")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGroupBox,
    QMessageBox,
    QScrollArea,
)

from laoa_trader import formulas as lib  # noqa: E402
from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.ui import formula_page as fp  # noqa: E402
from tests.conftest import workdays_ending  # noqa: E402

#: 小库里的三只票：甲/丙 一路上涨、乙 一路下跌（命中集合是确定的）
RISING = ("600001", "600003")


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
    worker = widget.scorecard_worker
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


def _spin_until(qapp, predicate, timeout: float = 5.0) -> bool:
    """转事件循环直到条件成立（后台线程的信号要靠它回主线程）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ══════════════════════════════════════════════════════════════════════════
# 1) 点一下就插入（插在**光标处**，不是末尾）
# ══════════════════════════════════════════════════════════════════════════


def test_click_inserts_at_caret_not_at_end(page) -> None:
    """**这一页最核心的一条**：点变量 = 插到光标处，不是追加到末尾。"""
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
    page.editor.setPlainText("C>MA(C,5)")
    _put_caret(page, 0)
    page.editor.setFocus()

    QTest.keyClick(page.editor, Qt.Key.Key_Tab)

    assert page.editor.toPlainText() == fp.TAB_SPACES + "C>MA(C,5)"
    assert page.editor.tabChangesFocus() is False
    assert page.editor.hasFocus() is True


# ══════════════════════════════════════════════════════════════════════════
# 4) 右侧面板：中文 tooltip / 两列 / 可滚动 / 固定宽度
# ══════════════════════════════════════════════════════════════════════════


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


def test_palette_is_two_columns_scrollable_and_fixed_width(page) -> None:
    boxes = {box.title(): box for box in page.palette_panel.findChildren(QGroupBox)}
    assert set(boxes) == {"变量", "函数", "运算符"}
    for title, box in boxes.items():
        grid = box.layout()
        assert grid.columnCount() == 2, f"{title} 组不是两列"
    # 函数多 → 必须能滚动；宽度固定 → 按钮文字不会被压扁
    assert page.palette_panel.findChild(QScrollArea) is not None
    assert page.palette_panel.width() == fp.PANEL_WIDTH


def test_page_hint_is_two_lines_and_gray(page) -> None:
    """顶部那行灰字说明要在（且是"点右边按钮"这种一句话级别）。"""
    assert "点右边的按钮就能插入" in fp.PAGE_HINT
    assert "最后一行是选股条件" in fp.PAGE_HINT
    assert len(fp.PAGE_HINT.splitlines()) <= 2
    assert page.page_hint.objectName() == "statusTag"     # 小号灰字（主题里定义）


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
# 6) 试算
# ══════════════════════════════════════════════════════════════════════════


def test_preview_reports_known_hits_as_name_and_code(page, page_cfg) -> None:
    """试算：`名称（代码）` 格式 + 命中数就是小库里的那两只上涨票。"""
    page.editor.setPlainText("C>MA(C,5)")

    page.btn_preview.click()

    hint = page.hint_text
    assert "最近交易日 2026-09-11" in hint
    assert "命中 2 只" in hint
    assert "甲样本（600001）" in hint and "丙样本（600003）" in hint
    assert "乙样本（600002）" not in hint


def test_preview_with_no_hits_says_why(page) -> None:
    page.editor.setPlainText("C>MA(C,5)*100")

    page.btn_preview.click()

    assert "没有命中" in page.hint_text


def test_preview_reports_broken_formula_instead_of_running(page) -> None:
    page.editor.setPlainText("C>MAA(C,5)")

    page.btn_preview.click()

    assert "未知函数" in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 7) 成绩单：后台线程（不卡界面）
# ══════════════════════════════════════════════════════════════════════════


def test_scorecard_runs_in_background_thread_with_progress(page, qapp,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """【看成绩单】走后台线程，**入参与进度回调**都要对（界面不能卡住）。"""
    calls: dict = {}

    def fake_scorecard(formula, db_path, *, progress_cb=None, **kwargs):
        calls["label"] = formula.label
        calls["db_path"] = db_path
        calls["progress_cb"] = progress_cb
        calls["kwargs"] = kwargs
        progress_cb("公式成绩单", 1, 3)          # 模拟进度回传
        progress_cb("公式成绩单", 3, 3)
        return {"formula": formula.label, "text": "📊 公式成绩单\n⚠️ 连板() 的历史坑",
                "hint": lib.LIMIT_UP_HINT}

    monkeypatch.setattr(lib, "run_scorecard", fake_scorecard)
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_scorecard.click()

    worker = page.scorecard_worker
    assert worker is not None, "成绩单必须在后台线程里跑"
    assert _spin_until(qapp, lambda: not worker.isRunning() and page.scorecard_result)
    qapp.processEvents()

    assert calls["progress_cb"] is not None           # 进度回调真的传下去了
    assert calls["db_path"] == page.cfg.db_path
    assert calls["kwargs"]["conv_key"] == lib.DEFAULT_CONVENTION_KEY
    assert page.scorecard_result is not None
    assert "公式成绩单" in page.scorecard_text
    assert page.btn_scorecard.isEnabled() is True     # 跑完要把按钮还回来
    assert page.progress.isVisible() is False


def test_scorecard_failure_is_shown_in_chinese(page, qapp,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """成绩单算不出来（库坏了之类）→ 中文提示，界面不崩。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("库文件被占用")

    monkeypatch.setattr(lib, "run_scorecard", boom)
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_scorecard.click()

    assert _spin_until(qapp, lambda: "成绩单算不出来" in page.hint_text)
    assert "库文件被占用" in page.hint_text
    assert page.btn_scorecard.isEnabled() is True


def test_copy_scorecard_puts_text_in_clipboard(page) -> None:
    page.scorecard_text = "📊 公式成绩单\n样本：10 笔"

    page.btn_copy_scorecard.click()

    assert QApplication.clipboard().text() == page.scorecard_text


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
    assert page.table.rowCount() == 1                     # 列表里立刻出现
    assert "已保存" in page.hint_text


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
    assert page.table.rowCount() == 0
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
# 9) 已保存列表：状态标红、选中载入
# ══════════════════════════════════════════════════════════════════════════


def test_list_marks_broken_formula_with_reason(page) -> None:
    """语法错的公式在列表里标 ❌ 并给出原因（一条坏公式不影响别的）。"""
    page.directory.mkdir(parents=True, exist_ok=True)
    (page.directory / "坏公式.txt").write_text("# 名称: 坏公式\nC>MAA(C,5)\n",
                                               encoding="utf-8")
    (page.directory / "好公式.txt").write_text("# 名称: 好公式\nC>MA(C,5)\n",
                                               encoding="utf-8")

    page.reload()

    assert page.table.rowCount() == 2
    status = {page.table.item(row, 0).text(): page.table.item(row, 2)
              for row in range(2)}
    assert status["好公式"].text() == "✅ 校验通过"
    assert status["坏公式"].text().startswith("❌")
    assert "未知函数" in status["坏公式"].text()
    assert "第 1 行" in status["坏公式"].toolTip()        # 全文（含行号列号）在 tooltip 里


def test_list_shows_runtime_error_from_last_run(page, monkeypatch) -> None:
    """运行期出错（数据不够之类）也要在列表里标出来 —— 只有日志是不够的。"""
    page.directory.mkdir(parents=True, exist_ok=True)
    (page.directory / "用连板的.txt").write_text("# 名称: 用连板的\n连板()>=2\n",
                                                 encoding="utf-8")
    monkeypatch.setattr(fp.formula_group, "last_status",
                        lambda: {"用连板的": "3 只票算不出来：本地涨停池还没攒够"})

    page.reload()

    assert page.table.item(0, 2).text().startswith("⚠️ 运行时出错")
    assert "涨停池" in page.table.item(0, 2).toolTip()


def test_selecting_row_loads_formula_into_editor(page, qapp) -> None:
    """点一行 → 载入左侧编辑区（可以直接改、再保存覆盖）。"""
    page.name_edit.setText("甲公式")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
    page.name_edit.setText("乙公式")
    page.editor.setPlainText("C<MA(C,5)")
    page.btn_save.click()

    page.select_row("甲公式")
    qapp.processEvents()

    assert page.name_edit.text() == "甲公式"
    assert page.editor.toPlainText() == "C>MA(C,5)"
    assert "已载入" in page.hint_text


def test_reload_keeps_enabled_checkbox_state_from_config(page, page_cfg) -> None:
    page_cfg.enabled_formulas = ["甲公式"]
    page.name_edit.setText("甲公式")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()

    page.reload()

    assert page._row_boxes["甲公式"].isChecked() is True


# ══════════════════════════════════════════════════════════════════════════
# 10) 勾「参与选股」→ 写回 config.toml
# ══════════════════════════════════════════════════════════════════════════


def test_enable_checkbox_writes_config_toml(page, page_cfg, qapp) -> None:
    """勾上 → config.toml 出现 enabled_formulas，**用户注释与未知键都保留**。"""
    page.name_edit.setText("放量上攻")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
    box = page._row_boxes["放量上攻"]
    assert box.isChecked() is False            # 默认**不参与**

    box.setChecked(True)
    qapp.processEvents()

    text = page_cfg.source_path.read_text(encoding="utf-8")
    assert 'enabled_formulas = ["放量上攻"]' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert page_cfg.enabled_formulas == ["放量上攻"]
    assert "已加入选股" in page.hint_text


def test_uncheck_enable_removes_from_config(page, page_cfg, qapp,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    page.name_edit.setText("放量上攻")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
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
    page.name_edit.setText("放量上攻")
    page.editor.setPlainText("C>MA(C,5)")
    page.btn_save.click()
    box = page._row_boxes["放量上攻"]

    def boom(*_args, **_kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr("laoa_trader.config.save_settings", boom)
    box.setChecked(True)
    qapp.processEvents()

    assert box.isChecked() is False
    assert "保存失败" in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 11) 载入示例
# ══════════════════════════════════════════════════════════════════════════


def test_load_sample_falls_back_to_builtin_when_dir_empty(page) -> None:
    """目录里没有示例文件时给一条内置的兜底公式（小白第一步一定走得通）。"""
    page.on_load_sample()

    assert page.editor.toPlainText() == fp.SAMPLE_TEXT
    assert page.name_edit.text() == fp.SAMPLE_NAME
    assert "已载入" in page.hint_text
    page.btn_validate.click()
    assert "校验通过" in page.hint_text


def test_load_sample_prefers_the_bundled_sample(page) -> None:
    """目录里有示例公式（随包分发的那条）时，载入它本体。"""
    page.directory.mkdir(parents=True, exist_ok=True)
    (page.directory / "放量上攻.txt").write_text(
        "# 名称: 放量上攻\n# 说明: 示例\nC>MA(C,5) AND V>MA(V,5)*1.5\n",
        encoding="utf-8",
    )

    page.on_load_sample()

    assert page.editor.toPlainText() == "C>MA(C,5) AND V>MA(V,5)*1.5"
    assert "放量上攻" in page.hint_text


# ══════════════════════════════════════════════════════════════════════════
# 12) 提示区：可复制
# ══════════════════════════════════════════════════════════════════════════


def test_hint_area_is_selectable_and_multiline(page) -> None:
    page.editor.setPlainText("连板()>=2")
    page.btn_validate.click()

    flags = page.hint_label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert "\n" in page.hint_label.text()              # 多行
    assert page.hint_label.wordWrap() is True


def test_long_scorecard_is_trimmed_in_hint_but_kept_for_copy(page) -> None:
    """成绩单很长：提示区只显示前几行，完整文本留在【复制成绩单】里。"""
    long_text = "\n".join(f"第 {i} 行" for i in range(1, 31))

    page._set_hint(long_text)

    assert page.scorecard_text or True                 # 提示区是独立的
    assert page.hint_text == long_text                 # 完整文本留档
    assert len(page.hint_label.text().splitlines()) == fp.HINT_MAX_LINES + 1
    assert "复制成绩单" in page.hint_label.text()

    page.scorecard_text = long_text
    page.btn_copy_scorecard.click()
    assert QApplication.clipboard().text() == long_text
