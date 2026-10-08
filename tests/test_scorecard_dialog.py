"""「策略成绩单」对话框的**离屏**界面测试（`ui/scorecard_dialog.py` + 页面上那个入口）。

这一块最容易做错、又最难在代码里看出来的是**它在数据不够时说了什么**：
成绩单的价值全在口径上，而一个「算得出数、还挺好看」的假结论比没有结论危险得多。
所以这里逐条钉住：

1. **预检**：小库（几十个交易日）打开就提示「样本不足」，并且**点明怎么补数据**
   （下载年限改成 10 + 点【下载数据】）—— 只说"样本不足"而不给下一步，等于把用户扔在原地；
2. **预检不通过也允许继续点**：按钮照样能按，但结果区必须写着「样本不足，不能当结论」；
3. **一条策略一块结果**：勾选的每一条占表格一行（样本数 / 平均收益 / 胜率 / t / 逐年），
   其中**至少有一条用例走真实的 `formulas.run_scorecard()`**（小的合成库，跑得动）——
   全用替身的话，"界面上那一排数到底从哪来"就没被验过；
4. **算不出来要看得见**：编译报错、计算抛异常都必须是表格里的 `—` + 表格下面的**中文原因**，
   不许崩、不许拿 0 顶上；
5. **复制**：纯文本里必须带**成交口径**与**数据跨度**（这份文本是要被贴出去的，
   少了这两行，一个 +3% 可以来自 10 年，也可以来自 20 个交易日）；
6. **后台与收尾**：计算在 `QThread` 里跑（主线程不阻塞）、【取消计算】要能停下、
   **还在跑的时候关窗不许把进程带走**；
7. **入口在「策略匹配」页上**：有【成绩单】按钮、tooltip 说清代价、点了会打开对话框。

全部离线（合成 SQLite 小库，不联网）；没装 PySide6 的机器整个文件跳过
（与 `test_formula_page.py` 同一约定）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过成绩单界面测试")

from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication, QHeaderView  # noqa: E402

from laoa_trader import formulas as lib  # noqa: E402
from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.hints import BTN_DOWNLOAD_TEXT, TAB_SETTINGS_TEXT  # noqa: E402
from laoa_trader.research import scorecard as sc  # noqa: E402
from laoa_trader.strategy import formula as fm  # noqa: E402
from laoa_trader.ui import formula_page as fp  # noqa: E402
from laoa_trader.ui import scorecard_dialog as sd  # noqa: E402
from tests.conftest import workdays_ending  # noqa: E402

#: 一条能选出票的策略（甲/丙 一路上涨、乙 一路下跌 —— 与 `test_formula_lib.py` 同一份数据）
RISING_FORMULA = "C>MA(C,5)"

#: 超时（秒）：合成小库上真实成绩单只要几百毫秒，给足余量但不无限等
WAIT_TIMEOUT = 30.0


def _spin_until(qapp, predicate, timeout: float = 5.0) -> bool:
    """转事件循环直到条件成立（后台线程的信号要靠它回主线程）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _seed_small_db(db_path: Path) -> list[str]:
    """3 只票 × 30 个交易日的小库（**故意过不了成绩单的门槛**：100 只 / 250 个交易日）。

    这正是出厂状态的样子（默认只导 6 个月），也是这个对话框存在的全部理由 ——
    所以预检那几条用例必须建在这种库上，而不是建一个"够大"的库假装问题不存在。
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
    return days


@pytest.fixture()
def small_db(tmp_path: Path) -> str:
    """小库的路径（`_seed_small_db` 返回的是交易日列表，别把它当路径用 —— 踩过）。"""
    path = tmp_path / "score.db"
    _seed_small_db(path)
    return str(path)


@pytest.fixture()
def small_days(small_db: str) -> int:
    """小库里的交易日数（断言里用它，而不是写死 30）。"""
    return len(workdays_ending("2026-09-11", 30))


@pytest.fixture()
def make_dialog(qapp, cfg: Config, small_db: str):
    """建对话框的工厂（顺手把用过的都记下来，收尾时把线程等干净）。

    为什么要统一收尾：留一个还在跑的 `QThread` 给后面的用例，它的信号会打到已经
    销毁的控件上，整个文件的耗时会成倍劣化（`test_formula_page.py` 的 `page` fixture
    踩过这个坑，这里同一个理由）。
    """
    created: list[sd.ScorecardDialog] = []

    def _make(targets=None, **kwargs):
        dialog = sd.ScorecardDialog(
            cfg=cfg,
            targets=targets if targets is not None else [("收盘在5日线上", RISING_FORMULA)],
            db_path=kwargs.pop("db_path", small_db),
            **kwargs,
        )
        created.append(dialog)
        return dialog

    yield _make
    for dialog in created:
        dialog._stop_threads()
        dialog.close()
    qapp.processEvents()


def _wait_precheck(dialog, qapp, timeout: float = WAIT_TIMEOUT) -> None:
    """等预检落地（`audit` 有值 = 回来了）。"""
    assert _spin_until(qapp, lambda: dialog.audit is not None, timeout), (
        f"预检 {timeout:.0f} 秒还没回来：{dialog.audit_label.text()!r}"
    )


def _wait_run(dialog, qapp, timeout: float = WAIT_TIMEOUT) -> None:
    """等计算落地（`worker` 被置回 None = 跑完了）。"""
    assert _spin_until(qapp, lambda: dialog.worker is None, timeout), (
        f"成绩单 {timeout:.0f} 秒还没跑完：{dialog.hint_label.text()!r}"
    )


def _fake_run_scorecard(**overrides):
    """一个形状正确的假 `run_scorecard`（**不联网、不读库**）。"""
    payload = {
        "formula": "收盘在5日线上",
        "conv": "D+1 收盘买 → D+2 收盘卖",
        "conv_key": lib.DEFAULT_CONVENTION_KEY,
        "min_history": 5,
        "symbols": 3,
        "samples": 12,
        "days": 8,
        "avg": 0.015,
        "win_rate": 0.5,
        "t": 2.34,
        "best": 0.06,
        "worst": -0.03,
        "dropped": 1,
        "errors": [],
        "by_year": [{"year": "2026", "n": 12, "avg": 0.015, "win": 0.5}],
        "hint": "",
        "text": "📊 策略成绩单：收盘在5日线上\n样本：12 笔 / 8 个交易日",
    }
    payload.update(overrides)
    return payload


# ══════════════════════════════════════════════════════════════════════════
# 1) 预检：小库要说清「现在有多少 / 需要多少 / 怎么补」
# ══════════════════════════════════════════════════════════════════════════


def test_precheck_on_a_small_db_says_what_is_missing_and_how_to_fix(
    make_dialog, qapp, small_days: int
) -> None:
    """几十个交易日的库：提示出现、点到怎么补数据、而且**允许继续点**。"""
    dialog = make_dialog()
    _wait_precheck(dialog, qapp)

    assert dialog.audit is not None and dialog.audit["ok"] is False
    text = dialog.audit_label.text()
    # 库里现在有多少（交易日 / 票数）—— 用户要拿这两个数去比门槛
    assert f"{small_days} 个交易日" in text
    assert "3 只票" in text
    # 成绩单要多少（门槛取自 research/scorecard.py，不是这里另写的数）
    assert f"{sc.MIN_DB_DAYS} 个交易日" in text
    assert f"{sc.MIN_DB_SYMBOLS} 只票" in text
    # 怎么补：**下一步动作**必须写出来（下载年限 + 按钮名）
    assert "怎么补" in text and "history_years" in text and "10" in text
    assert f"【{BTN_DOWNLOAD_TEXT}】" in text
    assert TAB_SETTINGS_TEXT in text
    # 结论口径：样本不足，不是"程序坏了"
    assert "样本不足" in text and "不能当结论" in text

    # 预检不通过也要能按（有人就是想先看看）
    assert dialog.btn_run.isEnabled()
    # 结果区也标了同样一句（用户很可能直接滚到表格看数字）
    assert "样本不足" in dialog.verdict_label.text()
    assert "不能当结论" in dialog.verdict_label.text()


def test_precheck_text_is_not_a_red_error_but_still_calls_for_attention(make_dialog) -> None:
    """预检不够时的语气：**醒目但不是报错**（出厂就是 6 个月数据，这是正常状态）。

    判据（不涉及渲染）：文案用 ⚠️ 起头、带「」式的下一步动作，
    而且**不出现**报错那一套（❌ / 异常名 / traceback）。
    """
    dialog = make_dialog()
    text = sd.precheck(dialog.db_path)["text"]

    assert text.startswith("⚠️")
    assert "❌" not in text
    assert "Traceback" not in text and "Error" not in text
    assert f"【{BTN_DOWNLOAD_TEXT}】" in text
    # 纯文本控件里不能留 Markdown 的加粗标记（`db_sufficiency` 的原文里带星号）
    assert "**" not in text


def test_audit_text_reuses_the_scorecard_module_thresholds() -> None:
    """门槛只能有一处定义：预检文案里的数字必须与 `research/scorecard.py` 一致。

    为什么单独钉这一条：界面里再写一套 `MIN_DB_DAYS` 是这一块最容易出的错
    —— 成绩单本体改了门槛，界面就会说出相反的话（"够了" / "不够"），
    而两边都不会报错。
    """
    info = {"exists": True, "rows": 100, "symbols": 3, "days": 30,
            "start": "2026-01-01", "end": "2026-02-01", "error": ""}
    ok, reason = sc.db_sufficiency(info)
    assert ok is False
    text = sd.audit_text(ok=ok, reason=reason, info=info)

    assert f"{sc.MIN_DB_DAYS} 个交易日" in text
    assert f"{sc.MIN_DB_SYMBOLS} 只票" in text


# ══════════════════════════════════════════════════════════════════════════
# 2) 计算：一条策略一行结果（用替身，快而确定）
# ══════════════════════════════════════════════════════════════════════════


def test_run_fills_a_row_and_copy_text_has_caliber_and_span(
    make_dialog, qapp, monkeypatch
) -> None:
    """点【开始计算】→ 出一行结果；【复制】的文本带成交口径与数据跨度。"""
    seen: dict = {}

    def fake(formula, db_path, **kwargs):
        seen["name"] = getattr(formula, "name", "")
        assert kwargs.get("progress_cb") is not None, "进度回调必须传下去（界面靠它画进度）"
        assert kwargs.get("conv_key") == lib.DEFAULT_CONVENTION_KEY
        kwargs["progress_cb"]("策略成绩单", 3, 10)
        # 停一下再返回：进度那句是**中间态**，跑得太快就被"计算结束"那句盖掉了
        # （要断言的是"回调真的走到界面上"，不是"标签最后长什么样"）
        time.sleep(0.15)
        return _fake_run_scorecard()

    monkeypatch.setattr(lib, "run_scorecard", fake)

    dialog = make_dialog([("收盘在5日线上", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    labels: list[str] = []

    def _collect() -> bool:
        labels.append(dialog.progress_label.text())
        return dialog.worker is None

    dialog.btn_run.click()
    assert _spin_until(qapp, _collect, WAIT_TIMEOUT), "成绩单没跑完"

    assert seen["name"] == "收盘在5日线上"
    assert dialog._row_cell_text(0, "策略") == "收盘在5日线上"
    assert dialog._row_cell_text(0, "样本数") == "12"
    assert dialog._row_cell_text(0, "平均收益") == "+1.50%"
    assert dialog._row_cell_text(0, "胜率") == "50.0%"
    assert dialog._row_cell_text(0, "t 值") == "2.34"
    assert "2026" in dialog._row_cell_text(0, "逐年")
    # 进度那句要能看出"在算第几条、扫了多少只票"
    assert any("第 1/1" in text and "只票" in text for text in labels), labels

    text = dialog.copy_text()
    assert "成交口径" in text and lib.DEFAULT_CONVENTION_KEY in text
    assert "数据跨度" in text and "个交易日" in text
    assert "策略成绩单" in text and "12 笔" in text          # 每条策略的正文原样带上
    # 预检没过 → 复制出去的文本也必须写着这一句（贴出去之后没人知道库有多小）
    assert "样本不足，不能当结论" in text


def test_copy_before_running_says_so_instead_of_copying_half(make_dialog, qapp) -> None:
    """没算过就点【复制】：提示"先点开始计算"，而不是导出一份空白。"""
    dialog = make_dialog()
    _wait_precheck(dialog, qapp)

    dialog.btn_copy.click()

    assert "先点【开始计算】" in dialog.hint_label.text()


# ══════════════════════════════════════════════════════════════════════════
# 3) 真实的 run_scorecard 也要走一遍（替身证明不了"数是从哪来的"）
# ══════════════════════════════════════════════════════════════════════════


def test_real_scorecard_on_a_small_library_fills_real_numbers(make_dialog, qapp) -> None:
    """**不打桩**：真跑 `formulas.run_scorecard()`（合成小库，几百毫秒）。"""
    dialog = make_dialog([("收盘在5日线上", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)

    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    outcome = dialog.outcomes[0]
    assert outcome.ok, f"真实成绩单没算出来：{outcome.error!r}"
    samples = int(dialog._row_cell_text(0, "样本数"))
    assert samples > 0, "小库上应该能算出样本（甲/丙 一路上涨）"
    assert dialog._row_cell_text(0, "平均收益") != sd.DASH
    assert dialog._row_cell_text(0, "逐年") != sd.DASH
    # 正文里写的是**绝对收益**、不是 α —— 与 run_scorecard 的口径一致
    assert "绝对收益" in dialog.copy_text()
    assert dialog.outcomes[0].result is not None
    assert dialog.outcomes[0].result["samples"] == samples


# ══════════════════════════════════════════════════════════════════════════
# 4) 算不出来：`—` + 中文原因，不崩、不填 0
# ══════════════════════════════════════════════════════════════════════════


def test_strategy_with_a_syntax_error_shows_a_chinese_reason(make_dialog, qapp) -> None:
    """写错的策略：那一行全是 `—`，原因（带行号）写在表格下面。"""
    dialog = make_dialog([("写错的策略", "C>MA(C,")])
    _wait_precheck(dialog, qapp)

    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    outcome = dialog.outcomes[0]
    assert outcome.ok is False and outcome.error
    assert "这条策略写错了" in outcome.error
    assert "行" in outcome.error                     # 引擎的中文错误带行号列号
    for column in sd.RESULT_COLUMNS[1:]:
        assert dialog._row_cell_text(0, column) == sd.DASH, f"{column} 不该有数"
    assert "写错的策略" in dialog.note_label.text()
    assert "这条策略写错了" in dialog.note_label.text()
    # 出错了也要把界面收干净（按钮还回来），而不是卡在"正在计算"
    assert dialog.btn_run.isEnabled()
    assert dialog.btn_cancel.isEnabled() is False


def test_a_failing_strategy_does_not_take_down_the_other_one(make_dialog, qapp,
                                                            monkeypatch) -> None:
    """一条炸了不影响另一条：算不出来的那条写中文原因，另一条照常出数。"""

    def fake(formula, db_path, **kwargs):
        if getattr(formula, "name", "") == "会炸的策略":
            raise fm.FormulaDataError("本地数据库不存在：X（请先在界面点【下载数据】）")
        return _fake_run_scorecard(formula="好策略")

    monkeypatch.setattr(lib, "run_scorecard", fake)

    dialog = make_dialog([("会炸的策略", RISING_FORMULA), ("好策略", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    bad, good = dialog.outcomes
    assert bad.ok is False and bad.error.startswith("算不出来：")
    assert "下载数据" in bad.error                      # 数据问题 → 下一步动作写清楚
    assert dialog._row_cell_text(0, "样本数") == sd.DASH
    assert good.ok is True
    assert dialog._row_cell_text(1, "样本数") == "12"
    assert "会炸的策略" in dialog.note_label.text()


def test_unexpected_exception_becomes_a_chinese_hint_not_a_traceback(
    make_dialog, qapp, monkeypatch
) -> None:
    """程序问题（非数据问题）：中文原因 + 指路日志，**绝不把 traceback 丢到界面上**。"""

    def fake(formula, db_path, **kwargs):
        raise RuntimeError("读库时炸了")

    monkeypatch.setattr(lib, "run_scorecard", fake)

    dialog = make_dialog([("某策略", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    error = dialog.outcomes[0].error
    assert "算不出来（RuntimeError）：读库时炸了" in error
    assert "发给作者" in error                          # 下一步动作
    assert "Traceback" not in error and "  File " not in error
    assert dialog._row_cell_text(0, "平均收益") == sd.DASH


def test_no_checked_strategy_says_so_instead_of_silently_doing_nothing(make_dialog,
                                                                      qapp) -> None:
    """一条都没勾：当场说清去哪勾，而不是按一下什么都不发生。"""
    dialog = make_dialog([])
    _wait_precheck(dialog, qapp)

    assert dialog.table.rowCount() == 0
    assert "没有勾选任何策略" in dialog.note_label.text()

    dialog.btn_run.click()

    assert "没有勾选任何策略" in dialog.hint_label.text()
    assert dialog.worker is None


def test_result_columns_are_wide_enough_for_their_numbers(make_dialog, qapp) -> None:
    """列宽必须量得下那些数字（**截断的数比空白更坏**：用户会念出半截数）。

    实测踩过一次：`平均收益 / 胜率 / 逐年` 三列被压到只剩标题那么宽，
    界面上是 `+1.0...`、`1...`、`2...` —— 数据其实是对的，但没人读得到。
    所以这里按**当前字体**现量一遍（与 `test_formula_page.py` 里量「加入自选」
    按钮宽度同一个做法：字体/缩放下会被截断的东西，只能量、不能猜）。
    """
    dialog = make_dialog([("收盘在5日线上", RISING_FORMULA)])
    dialog.show()
    qapp.processEvents()
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    _wait_run(dialog, qapp)
    qapp.processEvents()

    metrics = QFontMetrics(dialog.table.font())
    # 判据一（确定性的）：列宽**按内容量**，策略名那一列吃掉剩余 ——
    # 这是让上面那些数字量得下的**成因**，比"量出来刚好够"更能守住
    header = dialog.table.horizontalHeader()
    for index in range(1, len(sd.RESULT_COLUMNS)):
        assert header.sectionResizeMode(index) == QHeaderView.ResizeMode.ResizeToContents, (
            f"第 {index} 列不再按内容量宽度了 —— 数字会被截断成 `+1.0...`／`1...`（实测过）"
        )
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    # 判据二：按当前字体现量，每一格都量得下
    for index, column in enumerate(sd.RESULT_COLUMNS):
        text = dialog.table.item(0, index).text()
        assert text, f"{column} 这一格是空的（`—` 也要占位，不能是空串）"
        assert dialog.table.columnWidth(index) >= metrics.horizontalAdvance(text) + 4, (
            f"{column} 列太窄：{text!r} 需要 "
            f"{metrics.horizontalAdvance(text) + 4}，只有 {dialog.table.columnWidth(index)}"
        )
    # 至少这三列这一次是真的有数（否则上面那句"量得下"没验到什么）
    # 注：`t 值` 有可能是 `—`（这条合成数据每天涨幅完全一样 → 标准差为 0 → 无 t），
    # 那是**对的**，所以不把它列进来
    for column in ("样本数", "平均收益", "胜率", "逐年"):
        assert dialog._row_cell_text(0, column) != sd.DASH, f"{column} 应该有数"
    # 口径那句里不许把短键写两遍（`B B 尾盘买…` 实测出现过）
    assert "B B" not in dialog.header_label.text()


def test_nan_numbers_are_shown_as_dash_with_a_reason_not_as_nan(make_dialog, qapp,
                                                               monkeypatch) -> None:
    """算出来的数是 NaN（库里有缺失的行情价）：显示 `—` + 中文原因，**不许写 `+nan%`**。

    这条对着一个**真实存在的上游坑**：库里只要有一行缺行情价，样本里就会混进 NaN，
    `run_scorecard` 的 `avg`／`t` 跟着变成 NaN（详见交付说明里那条上游问题）。
    这个模块改不了上游的口径，但**显示层必须挡住它** —— `+nan%` 是个看着像数、
    其实是垃圾的东西，比空白危险。
    """
    nan = float("nan")

    def fake(formula, db_path, **kwargs):
        return _fake_run_scorecard(avg=nan, t=nan, win_rate=nan,
                                   text="📊 策略成绩单：收盘在5日线上\n平均收益：+nan%")

    monkeypatch.setattr(lib, "run_scorecard", fake)

    dialog = make_dialog([("收盘在5日线上", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    assert dialog.outcomes[0].ok is True          # 上游确实返回了"结果"
    for column in ("平均收益", "胜率", "t 值"):
        assert dialog._row_cell_text(0, column) == sd.DASH, f"{column} 不该显示 NaN"
    assert "nan" not in dialog._row_cell_text(0, "平均收益").lower()
    note = dialog.note_label.text()
    assert "不是一个数" in note and "不能当结论" in note
    assert "补齐数据" in note                       # 说清下一步动作
    # 复制出去的文本里也要紧跟一句说明（正文是上游排的，可能带着 +nan%）
    text = dialog.copy_text()
    assert "上面这一条不能用" in text


def test_a_real_nan_sample_from_the_library_is_reported_not_crashed(
    make_dialog, qapp, tmp_path: Path
) -> None:
    """真库里的缺失价：坏样本被剔掉，界面照常出数 —— **不许崩、不许出现 `nan`**。

    造法：某只票在进场日的收盘价写成 NULL（源里缺这一列时就是这样）——
    那一笔的收益会算成 NaN。

    历史：2026-10-08 之前，这个 NaN 会让 `statistics.stdev` 直接抛
    `AttributeError: 'float' object has no attribute 'numerator'`，整条成绩单失败，
    对话框只能显示一行中文原因（那条用例就是当时写的）。**上游已修**：
    `_forward_return()` / `compute_outcomes()` 用 `_finite_price()` 把非有限的价剔掉，
    `daily_t()` 再兜一道（见 `tests/test_scorecard.py` 的两条用例）。
    所以这条用例现在的期望是"**照常算得出数**"：坏样本少一笔，结论仍然给得出。

    公式用 `C>0`（每天都成立）而不是 `C>MA(C,5)`：后者会被那一根 NaN 掐掉信号，
    反而绕开了这个坑（NaN 参与比较一律为假）。
    """
    from tests.conftest import workdays_ending as _days

    db = tmp_path / "has_nan.db"
    storage.init_db(db)
    days = _days("2026-09-11", 30)
    with storage.connect(db) as conn:
        storage.write_stock_basic(conn, [("600001", "甲样本", "银行"),
                                         ("600002", "乙样本", "银行")])
        rows = []
        for symbol in ("600001", "600002"):
            for index, day in enumerate(days):
                close = None if (symbol == "600001" and index == 11) else 10.0
                rows.append((symbol, day, 10.0, 10.1, 9.9, close, 1e6, 1e7))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)

    dialog = make_dialog([("缺价的那条", "C>0")], db_path=str(db))
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    _wait_run(dialog, qapp)

    outcome = dialog.outcomes[0]
    # 坏样本不再让整条成绩单失败：这一条算得出数（少一笔样本），界面上是**真数字**
    assert outcome.ok is True, outcome.error
    assert outcome.result["samples"] > 0
    # 界面上一个 `nan` 都不许出现（这正是当初那个 bug 最容易被用户看见的样子）。
    # 注意**允许** `—`：`t 值` 在"每天收益都相同"（这里的合成价格全是 10.0）时
    # 标准差为 0，本来就该显示 `—`（不是错误，也不是 0）。
    for column in sd.RESULT_COLUMNS[1:]:
        text = dialog._row_cell_text(0, column)
        assert "nan" not in text.lower(), (column, text)
    assert dialog._row_cell_text(0, "样本数") not in ("", sd.DASH)
    # 结果在表里就有（策略名在「策略」列）—— 修好之后不再需要"整条失败"的那行说明，
    # 所以这里不要求 `note_label` 上有字（它只在**失败**时才会写原因）。
    assert dialog._row_cell_text(0, "策略") == "缺价的那条"


# ══════════════════════════════════════════════════════════════════════════
# 5) 后台、取消与关窗安全
# ══════════════════════════════════════════════════════════════════════════


def _slow_run_scorecard(steps: int = 4000):
    """一个"扫得很慢"的假成绩单：每扫一只票回一次进度（真路径就长这样）。"""

    def fake(formula, db_path, **kwargs):
        progress_cb = kwargs["progress_cb"]
        for index in range(1, steps + 1):
            progress_cb("策略成绩单", index, steps)
            time.sleep(0.001)
        return _fake_run_scorecard()

    return fake


def test_cancel_stops_the_run_and_keeps_what_is_already_done(make_dialog, qapp,
                                                             monkeypatch) -> None:
    """【取消计算】：线程要停、界面要收干净，已算完的留着、没算的如实写"已取消"。"""
    monkeypatch.setattr(lib, "run_scorecard", _slow_run_scorecard())

    dialog = make_dialog([("跑得慢的策略", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    dialog.btn_run.click()
    assert _spin_until(qapp, lambda: "第 1/1" in dialog.progress_label.text(), 10.0), (
        f"进度没走动：{dialog.progress_label.text()!r}"
    )

    dialog.btn_cancel.click()
    _wait_run(dialog, qapp)

    assert dialog.outcomes[0].cancelled is True
    assert dialog.outcomes[0].ok is False
    assert dialog._row_cell_text(0, "样本数") == sd.DASH
    assert "已取消" in dialog.note_label.text()
    assert "已取消" in dialog.hint_label.text() or "取消" in dialog.hint_label.text()
    assert dialog.btn_run.isEnabled()
    # 复制出去的文本里那条要写着"没算完"，不能让看的人以为它没被选上
    assert "没算完（已取消）" in dialog.copy_text()


def test_closing_while_running_does_not_take_the_process_down(make_dialog, qapp,
                                                              monkeypatch) -> None:
    """**还在算的时候关窗**：取消 + 等线程退出，窗口能关掉，进程不出事。

    这一条守的是 Qt 那个经典崩法：窗口先销毁、`QThread` 还在跑 →
    `QThread: Destroyed while thread is still running` 直接结束进程。
    """
    monkeypatch.setattr(lib, "run_scorecard", _slow_run_scorecard())

    dialog = make_dialog([("跑得慢的策略", RISING_FORMULA)])
    _wait_precheck(dialog, qapp)
    dialog.show()
    dialog.btn_run.click()
    assert _spin_until(qapp, lambda: "第 1/1" in dialog.progress_label.text(), 10.0)

    dialog.close()
    qapp.processEvents()

    assert dialog.worker is None, "关窗时必须把线程收干净"
    assert dialog.precheck_worker is None
    assert dialog.isVisible() is False


# ══════════════════════════════════════════════════════════════════════════
# 6) 「策略匹配」页上的入口
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def page_cfg(cfg: Config, tmp_path: Path) -> Config:
    """界面用的配置：一份 config.toml 落点（勾选策略会写回它）。

    这一页本身不需要行情库（成绩单的库由对话框自己拿 `cfg.db_path`），
    所以这里不造库 —— 造一份用不上的数据只会让用例变慢、并掩盖"到底谁在读库"。
    """
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("# 用户自己的注释\n", encoding="utf-8")
    return cfg


@pytest.fixture()
def page(page_cfg: Config, tmp_path: Path, qapp):
    """建出「策略匹配」页（公式目录指向 tmp_path，**绝不碰仓库里那份**）。"""
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "收盘在5日线上.txt").write_text(
        lib.formula_text("收盘在5日线上", RISING_FORMULA, "收盘价在 5 日均价之上"),
        encoding="utf-8",
    )
    widget = fp.FormulaPage(page_cfg, directory=folder)
    widget.resize(1100, 720)
    widget.show()
    qapp.processEvents()
    yield widget
    for attr in ("preview_worker",):
        worker = getattr(widget, attr, None)
        if worker is not None and worker.isRunning():
            worker.wait(5_000)
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_the_page_has_a_scorecard_entry_with_an_honest_tooltip(page) -> None:
    """页面上有【成绩单】按钮，tooltip 把代价说在前面（要扫全库、数据不足 1 年没用）。"""
    assert page.btn_scorecard.text() == "成绩单"
    tip = page.btn_scorecard.toolTip()
    assert "全库" in tip and "几十秒" in tip
    assert "1 年" in tip and "不可用" in tip
    # 它只是个入口：这一页不自己跑成绩单（口径与线程都在对话框那个模块里）
    assert not hasattr(page, "scorecard_worker")
    assert callable(lib.run_scorecard)


def test_clicking_the_entry_opens_the_dialog_with_the_checked_strategies(
    page, qapp, monkeypatch
) -> None:
    """点【成绩单】→ 打开对话框，交出去的正是**勾选**的那几条（名字 + 正文）。"""
    opened: dict = {}

    class FakeDialog:
        """替身：只记下被谁、用什么参数打开（真窗口会在 `exec()` 里等用户）。"""

        def __init__(self, parent=None, *, cfg=None, targets=None, **kwargs):
            opened["parent"] = parent
            opened["cfg"] = cfg
            opened["targets"] = list(targets or [])

        def exec(self):
            opened["exec"] = True
            return 0

    monkeypatch.setattr(fp, "ScorecardDialog", FakeDialog)

    # 一条都没勾：清单为空（对话框会如实说"没有勾选任何策略"）
    page.btn_scorecard.click()
    assert opened["exec"] is True
    assert opened["targets"] == []
    assert opened["parent"] is page and opened["cfg"] is page.cfg

    # 勾上一条（与界面上的勾选框走同一条路）：清单里就该有它
    page.cfg.enabled_formulas = ["收盘在5日线上"]
    page.reload()
    box = page._row_box(fp.ROW_FORMULA, "收盘在5日线上")
    assert box is not None and box.isChecked()

    page.btn_scorecard.click()
    assert [target.name for target in opened["targets"]] == ["收盘在5日线上"]
    target = opened["targets"][0]
    assert target.text.strip() == RISING_FORMULA, "交出去的是策略正文（不带注释头）"
    assert "# 名称" not in target.text
