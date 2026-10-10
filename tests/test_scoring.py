"""个股评分（`scoring.py`）与它进界面的那一列（2026-10-11 主人要求）。

主人原话："做进界面，自选标的 / 持仓监控加一列评分，加入自动给出。"
所以这份用例钉四件事：

1. **分数讲道理**：一路上涨的票分明显高于一路下跌的票；每一档结论（强势/弱势…）
   都能用一句人话解释；
2. **缺项不算 0**：拿不到换手率/资金流时那一项标"缺"，总分按**已得分的权重重归一**，
   并把缺了哪些项写出来（不能因为没配 Key 就给用户判个错的分）；
3. **哪一列、怎么给**：自选标的与持仓监控两张表都有「评分」列，数值来自服务缓存，
   没算出来时显示 `—`（**不是 0**），tooltip 里有维度与逐项理由；
4. **算过的不会重算**：同一只票同一天第二次 `request` 不再排队（缓存键 = 代码 + 行情日 + 模型版本）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from laoa_trader import market, scoring  # noqa: E402
from laoa_trader.strategy import formula as fm  # noqa: E402
from laoa_trader.ui import app as ui_app  # noqa: E402
from laoa_trader.ui import scores as scores_mod  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _series(symbol: str, closes: np.ndarray, *, extra: dict | None = None) -> fm.Series:
    """造一只票的序列（列数与 `closes` 一致；扩展字段铺成"只有最后一格有值"）。"""
    count = len(closes)
    values: dict[str, np.ndarray] = {}
    for key, value in (extra or {}).items():
        column = np.full(count, np.nan)
        column[-1] = float(value)
        values[key] = column
    return fm.Series(
        symbol=symbol, name=f"测试{symbol}", industry="银行",
        date=[f"2026-01-{index % 28 + 1:02d}" for index in range(count)],
        close=closes, open=closes * 0.995, high=closes * 1.01, low=closes * 0.99,
        vol=np.full(count, 1_000_000.0), amount=closes * 1_000_000.0,
        pre_close=np.r_[closes[0], closes[:-1]],
        limit_up_days=np.zeros(count), limit_up_cnt=np.zeros(count), extra=values,
    )


# ── 1) 分数讲道理 ──


def test_an_uptrend_scores_much_higher_than_a_downtrend() -> None:
    """一路上涨的票分明显高于一路下跌的票 —— 这是"这个分有没有意义"的最低要求。"""
    up = scoring.score_series(_series("600000", np.linspace(10, 14, 120),
                                      extra={"换手率": 5.0, "主力净额": 0.9}))
    down = scoring.score_series(_series("600001", np.linspace(14, 10, 120),
                                        extra={"换手率": 25.0, "主力净额": -1.2}))
    assert up.ok and down.ok
    assert up.total > down.total + 25, (up.total, down.total)
    assert up.total >= 60 and down.total <= 45, (up.total, down.total)
    assert up.advice in ("强势", "偏强")
    assert down.advice in ("偏弱", "弱势")


def test_advice_levels_are_monotonic() -> None:
    """档位只描述强弱（不是买卖建议），且必须随分数单调。"""
    scores = [0, 20, 35, 50, 70, 90, 100]
    advice = [scoring.advice_for(value) for value in scores]
    assert advice == sorted(advice, key=lambda text: [
        "弱势", "偏弱", "中性", "偏强", "强势"].index(text))
    assert scoring.advice_for(0) == "弱势" and scoring.advice_for(100) == "强势"
    # 措辞里不许出现"买入/卖出"（这个软件不是荐股工具）
    assert all("买" not in text and "卖" not in text for text in advice)


def test_result_explains_every_item() -> None:
    """可解释：每一维、每一项都在 tooltip 里，维度分的名字与权重都对得上。"""
    result = scoring.score_series(_series("600000", np.linspace(10, 14, 120)))
    assert [group.name for group in result.groups] == list(scoring.GROUPS)
    for group in result.groups:
        assert group.items, f"{group.name} 一项都没有"
        assert 0.0 <= group.score <= 100.0
        for item in group.items:
            assert item.reason.startswith(item.name)
            assert "：" in item.reason
    tip = result.tip()
    assert f"评分 {result.total:.0f}" in tip
    assert "模型 v" in tip
    for group in result.groups:
        assert f"【{group.name}】" in tip


# ── 2) 缺项不等于 0 分 ──


def test_missing_items_are_excluded_not_scored_zero() -> None:
    """**缺项按已得分的权重重归一**：少一维不该把总分往中间拽。

    判据用"同一只票、同一段行情"给两次输入：
      * 有换手率/资金流 → 那两项参与打分；
      * 没有 → 那两项进 `missing`，总分由**剩下的项**决定。
    如果实现把缺项当 0 分，缺资金流那一版会明显更低（实测差 5 分以上）。
    """
    closes = np.linspace(10, 14, 120)
    rich = scoring.score_series(_series("600000", closes,
                                        extra={"换手率": 5.0, "主力净额": 0.9}))
    poor = scoring.score_series(_series("600000", closes))
    assert "换手活跃度" in poor.missing and "主力资金净额" in poor.missing
    assert "换手活跃度" not in rich.missing
    # **直接验定义**：报出来的总分 = 只按"有可用项的维度"归一的结果。
    # 若实现把缺项当 0 分，资金那一维会以 -1 参与加权，算出来的数会明显更低
    # （这条断言就会红）。
    usable = [group for group in poor.groups
              if any(item.available for item in group.items)]
    assert usable, "至少要有一维算得出来"
    weighted = sum((group.score / 50.0 - 1.0) * scoring.GROUPS[group.name]
                   for group in usable)
    weight_sum = sum(scoring.GROUPS[group.name] for group in usable)
    expect = (weighted / weight_sum + 1) * 50
    assert abs(poor.total - expect) < 0.2, (poor.total, expect)
    # 而且资金那一维**没进分母**（它是空的）
    assert all(not item.available for item in
               next(g for g in poor.groups if g.name == "资金").items)
    assert "缺项" in poor.tip() and "没当 0 分算" in poor.tip()


def test_items_that_cannot_be_computed_are_marked_missing() -> None:
    """算不出来的项标"缺"并写清原因（不许静默当 0，也不许让整只票算不出来）。"""
    result = scoring.score_series(_series("600001", np.linspace(10, 12, 5)))
    # 只有 5 根 K 线：60 日线、20 日线那几项必然缺
    assert "站上 60 日线" in result.missing
    assert result.ok, "数据不足时也要给出一个可读的结论（按已有项归一）"
    assert any("缺" in line for line in result.tip().splitlines())


def test_unknown_symbol_reports_a_readable_note(tmp_path) -> None:
    """库里没有这只票 → 返回带 `note` 的结果（不是抛异常、也不是 0 分）。"""
    from laoa_trader.data import storage
    from laoa_trader.config import Config

    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    storage.init_db(cfg.db_path)
    results = scoring.score_symbols(cfg, cfg.db_path, ["600000"])
    assert results["600000"].ok is False
    assert "日线" in results["600000"].note
    assert scoring.format_line(results["600000"]).startswith("600000 —")


def test_cache_key_covers_day_and_model_version() -> None:
    """缓存键 = 代码 + 行情日 + 模型版本（换了任何一项都该重算）。"""
    result = scoring.ScoreResult(symbol="600000", as_of="2026-01-05")
    assert scoring.cache_key(result) == ("600000", "2026-01-05", scoring.MODEL_VERSION)


def test_model_version_is_a_version_string() -> None:
    """带模型版本：改了项/权重就 +0.01 —— 至少得是个点分数字，别写成日期或空串。"""
    assert scoring.MODEL_VERSION.count(".") == 2
    assert all(part.isdigit() for part in scoring.MODEL_VERSION.split("."))


# ── 3) 评分服务：缓存与生命周期 ──


def test_score_service_caches_and_reports(ready_cfg, qapp) -> None:
    """服务：算过的不再排队；`result()` 给得出来；`invalidate()` 之后能重算。"""
    service = scores_mod.ScoreService(ready_cfg, ready_cfg.db_path)
    try:
        assert service.result("600001") is None          # 还没算
        first = service.request(["600001"])
        assert first == ["600001"]
        # 等后台线程算完（它是真线程：读库 + 评估 17 项 × 2 条公式）
        deadline = 30
        while service.result("600001") is None and deadline > 0:
            qapp.processEvents()
            service._worker.wait(200) if service._worker is not None else None
            deadline -= 1
        result = service.result("600001")
        assert result is not None, "后台评分没落地"
        assert result.symbol == "600001"
        assert service.request(["600001"]) == []          # 算过了 → 不再排队
        service.invalidate("600001")
        assert service.request(["600001"]) == ["600001"]  # 丢掉缓存 → 重新排队
    finally:
        service.stop()


# ── 4) 界面上的那一列 ──


@pytest.fixture()
def score_window(ready_cfg, qapp):
    """一个真主窗口（收尾照 `test_ui_smoke` 那一套），**先造一条自选 + 一条持仓**。

    为什么必须自己造：`ready_cfg` 只把库写成"自检 ready"（有行情、有日历），
    里面**没有自选也没有持仓** —— 两张表会是空的，评分那一列也就无从断言。
    """
    from laoa_trader.data import storage

    with storage.connect(ready_cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
        storage.upsert_position(conn, "600002", name="高价样本", quantity=0,
                                avg_cost=11.0, note="测试持仓")
    window = ui_app.MainWindow(ready_cfg)
    try:
        yield window
    finally:
        for name in ("_timer", "_market_timer", "_auction_timer", "_flash_timer"):
            timer = getattr(window, name, None)
            if timer is not None:
                timer.stop()
        window.scheduler.stop()
        window.quotes.stop()
        window.scores.stop()
        window.shutdown()
        window.tray.hide()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_both_tables_have_a_score_column(score_window) -> None:
    """两张表都有「评分」列，位置就在「监控开关」前面（不动老列的下标）。"""
    assert ui_app.WATCH_SCORE_COLUMN == ui_app.WATCH_MONITOR_COLUMN - 1
    assert ui_app.POSITION_SCORE_COLUMN == ui_app.POSITION_MONITOR_COLUMN - 1
    assert ui_app.WATCH_HEADERS[ui_app.WATCH_SCORE_COLUMN] == "评分"
    assert ui_app.POSITION_HEADERS[ui_app.POSITION_SCORE_COLUMN] == "评分"


def test_score_cell_shows_dash_before_it_is_computed(score_window, qapp) -> None:
    """还没算出来时显示 `—`（**不是 0**）：0 分是"很弱"这个真实结论，两回事。"""
    # 清掉缓存后**立刻**重画（中间不跑事件循环：跑了后台线程可能刚好算完，
    # 那测到的就是"已经算出来"的另一条路径了 —— 下面那条用例管的是那条路径）
    score_window.scores.invalidate()
    score_window._pool_signature = None
    score_window._refresh_pool_table()
    assert score_window.pool_table.rowCount() >= 1
    cell = score_window.pool_table.item(0, ui_app.WATCH_SCORE_COLUMN)
    assert cell.text() == market.DASH
    assert "评分还没算出来" in cell.toolTip()


def test_score_reaches_the_cell_and_the_tooltip(score_window, qapp) -> None:
    """后台算完之后：格子里是数字、tooltip 里有维度与逐项理由（"自动给出"的闭环）。

    这条同时守着一个容易漏的点：评分那一格**必须进内容指纹**，
    否则算完了表格也不重画，用户看到的是永远挂着的 `—`（功能像没做）。
    """
    score_window.scores.invalidate()
    symbols = score_window._score_symbols()
    assert symbols, "自选/持仓里应当有可评分的代码"
    score_window.scores.request(symbols)
    deadline = 60
    while score_window.scores.result(symbols[0]) is None and deadline > 0:
        qapp.processEvents()
        worker = score_window.scores._worker
        if worker is not None:
            worker.wait(200)
        deadline -= 1
    score_window._pool_signature = None
    score_window._position_signature = None
    score_window._refresh_pool_table()
    score_window._refresh_positions()
    qapp.processEvents()

    cell = score_window.pool_table.item(0, ui_app.WATCH_SCORE_COLUMN)
    assert cell.text().isdigit(), cell.text()
    tip = cell.toolTip()
    assert "评分" in tip and "模型 v" in tip
    assert "【趋势】" in tip and "【量价】" in tip
    # 备注/资金流那一份行 tooltip 仍然在（"悬浮任何一格都能看到备注"的老约定）
    for column in range(score_window.pool_table.columnCount()):
        item = score_window.pool_table.item(0, column)
        assert item.toolTip(), f"第 {column} 列没有 tooltip"
