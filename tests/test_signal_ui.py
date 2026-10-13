"""决策信号的界面：结论窗 + 两个【分析】入口 + 右键单只分析。

守住的规矩：

1. **手动**：开窗口本身不跑分析（没点【分析】就不许有结论、不许起线程）；
2. 两个入口（自选标的 / 持仓监控）走**两路结论集**，不串；
3. 结论窗里写清依据与免责，且**大模型消息那一块如实说"还没接"**；
4. 按钮 tooltip 是一句短的（≤45 字、单行）—— 与主界面那条硬要求同一条。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")

from laoa_trader import analysis  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.ui import signals as signals_mod  # noqa: E402
from tests._ui import close_main_window  # noqa: E402


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


class _FakeService:
    """替身：记录"点了哪几只、走哪一路"，不真去算（真算那一层在 test_signals.py）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[list[str], str]] = []
        self.stopped = False

    def analyze(self, symbols, kind=signals_mod.KIND_POOL) -> bool:
        self.calls.append((list(symbols), kind))
        return self.ok

    @property
    def running(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def window(ready_cfg, qapp):
    """一个真主窗口 + 一条自选 + 一条持仓（两张表都得有行才谈得上"分析表里这些票"）。"""
    from laoa_trader.ui import app as ui_app

    with storage.connect(ready_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="浦发样本")
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=0,
                                avg_cost=11.0, note="测试持仓")
    win = ui_app.MainWindow(ready_cfg)
    win._pool_signature = None
    win._position_signature = None
    win._refresh_pool_table()
    win._refresh_positions()
    qapp.processEvents()
    try:
        yield win
    finally:
        close_main_window(win, qapp)


def _signal(symbol: str, verdict: str = analysis.WATCH, name: str = "样本") -> object:
    return analysis.Signal(symbol=symbol, name=name, verdict=verdict,
                           headline="条件没凑齐，继续观察。",
                           evidences=[analysis.Evidence(analysis.KIND_SCORE, "评分 50（中性）",
                                                        "scoring v1.0.0")])


# ── 结论窗本身 ──


def test_signal_dialog_shows_rows_evidence_and_disclaimer(qapp) -> None:
    from laoa_trader.ui.signal_dialog import DISCLAIMER, SIGNAL_HEADERS, SignalDialog

    dialog = SignalDialog()
    try:
        dialog.set_signals([_signal("600001", analysis.ENTER, "浦发样本"),
                            _signal("600002", analysis.DROP, "半导体甲")])
        assert dialog.table.rowCount() == 2
        assert [dialog.table.horizontalHeaderItem(i).text()
                for i in range(len(SIGNAL_HEADERS))] == list(SIGNAL_HEADERS)
        assert dialog.table.item(0, 0).text() == "600001"
        assert dialog.table.item(1, 2).text() == analysis.DROP
        # 详情默认显示第一行，且带依据（用户要能自己核一遍）
        assert "评分 50（中性）" in dialog.detail.toPlainText()
        assert dialog.disclaimer.text() == DISCLAIMER
        # 选中第二行 → 详情跟着换
        dialog.table.selectRow(1)
        assert "600002" in dialog.detail.toPlainText()
        assert dialog.current_symbol() == "600002"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_signal_dialog_says_when_messages_are_not_fetched(qapp) -> None:
    """没有消息附注时要**明说**"未获取、不影响结论"，不能让用户以为消息是空的。"""
    from laoa_trader.ui.signal_dialog import SignalDialog

    dialog = SignalDialog()
    try:
        dialog.set_signals([_signal("600001")])
        text = dialog.detail.toPlainText()
        assert "未获取" in text and "不影响上面的结论" in text
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


# ── 主窗口里的两个入口 ──


def test_analyze_buttons_exist_with_short_tooltips(window) -> None:
    """两个页面各有一个【分析】按钮，tooltip 一句短的（与主界面那条硬要求一致）。"""
    for button in (window.btn_pool_analyze, window.btn_pos_analyze):
        assert button.text() == "分析"
        tip = button.toolTip()
        assert tip and "\n" not in tip and len(tip) <= 45, tip


def test_opening_the_window_does_not_analyze_anything(window) -> None:
    """**只手动**：开窗口不跑分析，结论窗也不会自己冒出来。"""
    assert getattr(window, "signal_dialog", None) is None
    assert window.signals.running is False
    assert window._signal_wanted == []


def test_pool_analyze_button_asks_for_the_pool_route(window) -> None:
    """【自选标的 → 分析】：把表里的代码交给**池子那一路**，并把结论窗亮出来。"""
    fake = _FakeService()
    window.signals = fake
    window.btn_pool_analyze.click()
    assert fake.calls, "点了【分析】却没发起分析"
    symbols, kind = fake.calls[0]
    assert symbols == window._table_symbols(window.pool_table)
    assert "600001" in symbols
    assert kind == signals_mod.KIND_POOL
    assert window.signal_dialog is not None
    assert window.signal_dialog.isVisible()
    assert "正在分析" in window.signal_dialog.status_label.text()


def test_position_analyze_button_uses_the_position_route(window) -> None:
    """持仓页那个按钮走**持仓那一路**（结论集不同，绝不能串）。"""
    fake = _FakeService()
    window.signals = fake
    window.btn_pos_analyze.click()
    symbols, kind = fake.calls[0]
    assert kind == signals_mod.KIND_POSITION
    assert "600002" in symbols


def test_busy_service_is_reported_instead_of_silently_doing_nothing(window) -> None:
    """上一轮还在跑 → 状态行如实说，而不是"点了没反应"。"""
    window.signals = _FakeService(ok=False)
    window.btn_pool_analyze.click()
    assert "还没跑完" in window.signal_dialog.status_label.text()


def test_row_menu_analyzes_only_that_row(window) -> None:
    """右键【分析】：只分析右键那一行（kind 跟着表走）。"""
    fake = _FakeService()
    window.signals = fake
    menu = window._row_menu(window.pool_table, 0, "pool")
    labels = [a.text() for a in menu.actions()]
    assert "分析" in labels
    action = next(a for a in menu.actions() if a.text() == "分析")
    action.trigger()
    assert fake.calls == [(["600001"], signals_mod.KIND_POOL)]

    menu_pos = window._row_menu(window.position_table, 0, "position")
    next(a for a in menu_pos.actions() if a.text() == "分析").trigger()
    assert fake.calls[-1] == (["600002"], signals_mod.KIND_POSITION)


# ── 结果回到窗里 ──


def test_results_are_filled_in_and_ordered_like_the_table(window) -> None:
    """结果回来 → 按**这次点的顺序**填进窗里，缺的那几只如实计数。"""
    window.signals = _FakeService()
    window.btn_pool_analyze.click()
    wanted = list(window._signal_wanted)
    assert len(wanted) >= 1
    payload = {wanted[0]: _signal(wanted[0], analysis.ENTER)}
    window._on_signals_updated(payload, signals_mod.KIND_POOL)
    dialog = window.signal_dialog
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == wanted[0]
    status = dialog.status_label.text()
    assert "1 只" in status
    if len(wanted) > 1:
        assert "没能算出来" in status
    assert "大模型消息附注还没接" in status


def test_empty_table_gets_a_popup_not_a_silent_noop(window, modal_calls) -> None:
    """表是空的 → 弹一句说清（用户操作失败要弹窗，这是主人定的口径）。"""
    window.pool_table.setRowCount(0)
    window.btn_pool_analyze.click()
    assert any("没有可分析的标的" in str(call) for call in modal_calls)
